import argparse

import torch.distributed as dist
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torchsummary import summary
import test as test  # import test.py to get mAP after each epoch
from models import *
from utils.datasets import *
from utils.utils import *
from utils.prune_utils import *

mixed_precision = True
try:  # Mixed precision training https://github.com/NVIDIA/apex
    from apex import amp
except:
    mixed_precision = False  # not installed

wdir = 'weights' + os.sep  # weights dir
last = wdir + 'last.pt'
best = wdir + 'best.pt'
results_file = 'results.txt'

# Hyperparameters (j-series, 50.5 mAP yolov3-320) evolved by @ktian08 https://github.com/ultralytics/yolov3/issues/310
hyp = {'giou': 1.582,  # giou loss gain
       'cls': 27.76,  # cls loss gain  (CE=~1.0, uCE=~20)
       'cls_pw': 1.446,  # cls BCELoss positive_weight
       'obj': 21.35,  # obj loss gain (*=80 for uBCE with 80 classes)
       'obj_pw': 3.941,  # obj BCELoss positive_weight
       'iou_t': 0.2635,  # iou training threshold
       'lr0': 0.002324,  # initial learning rate (SGD=1E-3, Adam=9E-5)
       'lrf': -4.,  # final LambdaLR learning rate = lr0 * (10 ** lrf)
       'momentum': 0.97,  # SGD momentum
       'weight_decay': 0.0004569,  # optimizer weight decay
       'fl_gamma': 0.5,  # focal loss gamma
       'hsv_h': 0.01,  # image HSV-Hue augmentation (fraction)
       'hsv_s': 0.5703,  # image HSV-Saturation augmentation (fraction)
       'hsv_v': 0.3174,  # image HSV-Value augmentation (fraction)
       'degrees': 1.113,  # image rotation (+/- deg)
       'translate': 0.06797,  # image translation (+/- fraction)
       'scale': 0.1059,  # image scale (+/- gain)
       'shear': 0.5768}  # image shear (+/- deg)

# Overwrite hyp with hyp*.txt (optional)
f = glob.glob('hyp*.txt')
if f:
    for k, v in zip(hyp.keys(), np.loadtxt(f[0])):
        hyp[k] = v


def train():
    cfg = opt.cfg
    t_cfg = opt.t_cfg  # teacher model cfg for knowledge distillation
    data = opt.data
    img_size = opt.img_size
    epochs = 1 if opt.prebias else opt.epochs  # 500200 batches at bs 64, 117263 images = 273 epochs
    batch_size = opt.batch_size
    accumulate = opt.accumulate  # effective bs = batch_size * accumulate = 16 * 4 = 64
    weights = opt.weights  # initial training weights
    t_weights = opt.t_weights  # teacher model weights

    if 'pw' not in opt.arc:  # remove BCELoss positive weights
        hyp['cls_pw'] = 1.
        hyp['obj_pw'] = 1.

    # Initialize
    init_seeds()
    multi_scale = opt.multi_scale

    if multi_scale:
        img_sz_min = round(img_size / 32 / 1.5) + 1
        img_sz_max = round(img_size / 32 * 1.5) - 1
        img_size = img_sz_max * 32  # initiate with maximum multi_scale size
        print('Using multi-scale %g - %g' % (img_sz_min * 32, img_size))

    # Configure run
    data_dict = parse_data_cfg(data)
    train_path = data_dict['train']
    nc = int(data_dict['classes'])  # number of classes

    # Remove previous results
    for f in glob.glob('*_batch*.jpg') + glob.glob(results_file):
        os.remove(f)

    # Initialize model
    model = Darknet(cfg, arc=opt.arc, quantized=opt.quantized, qlayers=opt.qlayers).to(device)
    summary(model, input_size=(3, opt.img_size, opt.img_size))
    if t_cfg:
        t_model = Darknet(t_cfg, (img_size, img_size), arc=opt.arc, quantized=-1, qlayers=-1).to(device)
    # Optimizer
    pg0, pg1 = [], []  # optimizer parameter groups
    for k, v in dict(model.named_parameters()).items():
        if 'Conv2d.weight' in k:
            pg1 += [v]  # parameter group 1 (apply weight_decay)
        else:
            pg0 += [v]  # parameter group 0

    if opt.adam:
        optimizer = optim.Adam(pg0, lr=hyp['lr0'])
        # optimizer = AdaBound(pg0, lr=hyp['lr0'], final_lr=0.1)
    else:
        optimizer = optim.SGD(pg0, lr=hyp['lr0'], momentum=hyp['momentum'], nesterov=True)
    optimizer.add_param_group({'params': pg1, 'weight_decay': hyp['weight_decay']})  # add pg1 with weight_decay
    del pg0, pg1

    cutoff = -1  # backbone reaches to cutoff layer
    start_epoch = 0
    best_fitness = float('inf')
    if weights != 'None':
        attempt_download(weights)
        if weights.endswith('.pt'):  # pytorch format
            # possible weights are 'last.pt', 'yolov3-spp.pt', 'yolov3-tiny.pt' etc.
            if opt.bucket:
                os.system('gsutil cp gs://%s/last.pt %s' % (opt.bucket, last))  # download from bucket
            chkpt = torch.load(weights, map_location=device)

            # load model
            # if opt.transfer:
            chkpt['model'] = {k: v for k, v in chkpt['model'].items() if model.state_dict()[k].numel() == v.numel()}
            model.load_state_dict(chkpt['model'], strict=False)
            # else:
            #    model.load_state_dict(chkpt['model'])

            # load optimizer
            if chkpt['optimizer'] is not None:
                optimizer.load_state_dict(chkpt['optimizer'])
                best_fitness = chkpt['best_fitness']

            # load results
            if chkpt.get('training_results') is not None:
                with open(results_file, 'w') as file:
                    file.write(chkpt['training_results'])  # write results.txt

            start_epoch = chkpt['epoch'] + 1
            del chkpt

        elif len(weights) > 0:  # darknet format
            # possible weights are 'yolov3.weights', 'yolov3-tiny.conv.15',  'darknet53.conv.74' etc.
            cutoff = load_darknet_weights(model, weights)
    else:
        cutoff = 64

    if t_cfg:
        if t_weights.endswith('.pt'):
            t_model.load_state_dict(torch.load(t_weights, map_location=device)['model'])
        elif t_weights.endswith('.weights'):
            load_darknet_weights(t_model, t_weights)
        else:
            raise Exception('pls provide proper teacher weights for knowledge distillation')
        if not mixed_precision:
            t_model.eval()
        print('<.....................using knowledge distillation.......................>')
        print('teacher model:', t_weights, '\n')

    if opt.transfer or opt.prebias:  # transfer learning edge (yolo) layers
        nf = int(model.module_defs[model.yolo_layers[0] - 1]['filters'])  # yolo layer size (i.e. 255)

        if opt.prebias:
            for p in optimizer.param_groups:
                # lower param count allows more aggressive training settings: i.e. SGD ~0.1 lr0, ~0.9 momentum
                p['lr'] *= 100  # lr gain
                if p.get('momentum') is not None:  # for SGD but not Adam
                    p['momentum'] *= 0.9

        for p in model.parameters():
            if opt.prebias and p.numel() == nf:  # train (yolo biases)
                p.requires_grad = True
            elif opt.transfer and p.shape[0] == nf:  # train (yolo biases+weights)
                p.requires_grad = True
            else:  # freeze layer
                p.requires_grad = False

    # Scheduler https://github.com/ultralytics/yolov3/issues/238
    # lf = lambda x: 1 - x / epochs  # linear ramp to zero
    # lf = lambda x: 10 ** (hyp['lrf'] * x / epochs)  # exp ramp
    # lf = lambda x: 1 - 10 ** (hyp['lrf'] * (1 - x / epochs))  # inverse exp ramp
    # scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)
    # scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=range(59, 70, 1), gamma=0.8)  # gradual fall to 0.1*lr0
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[round(opt.epochs * x) for x in [0.8, 0.9]], gamma=0.1)
    scheduler.last_epoch = start_epoch - 1

    # # Plot lr schedule
    # y = []
    # for _ in range(epochs):
    #     scheduler.step()
    #     y.append(optimizer.param_groups[0]['lr'])
    # plt.plot(y, label='LambdaLR')
    # plt.xlabel('epoch')
    # plt.ylabel('LR')
    # plt.tight_layout()
    # plt.savefig('LR.png', dpi=300)

    # Mixed precision training https://github.com/NVIDIA/apex
    if mixed_precision:
        if t_cfg:
            [model, t_model], optimizer = amp.initialize([model, t_model], optimizer, opt_level='O1', verbosity=1)
        else:
            model, optimizer = amp.initialize(model, optimizer, opt_level='O1', verbosity=1)

    # Initialize distributed training
    if torch.cuda.device_count() > 1:
        dist.init_process_group(backend='nccl',  # 'distributed backend'
                                init_method='tcp://127.0.0.1:9999',  # distributed training init method
                                world_size=1,  # number of nodes for distributed training
                                rank=0)  # distributed training node rank
        model = torch.nn.parallel.DistributedDataParallel(model)
        model.yolo_layers = model.module.yolo_layers  # move yolo layer indices to top level

    # 获得要剪枝的层

    if hasattr(model, 'module'):
        print('muti-gpus sparse')
        if opt.prune == 1:
            print('shortcut sparse training')
            _, _, prune_idx, _, _ = parse_module_defs2(model.module.module_defs)
        elif opt.prune == 0:
            print('normal sparse training ')
            _, _, prune_idx = parse_module_defs(model.module.module_defs)
        elif opt.prune == 2:
            print('tiny yolo normal sparse traing')
            _, _, prune_idx = parse_module_defs3(model.module.module_defs)

    else:
        print('single-gpu sparse')
        if opt.prune == 1:
            print('shortcut sparse training')
            _, _, prune_idx, _, _ = parse_module_defs2(model.module_defs)
        elif opt.prune == 0:
            print('normal sparse training')
            _, _, prune_idx = parse_module_defs(model.module_defs)
        elif opt.prune == 2:
            print('tiny yolo normal sparse traing')
            _, _, prune_idx = parse_module_defs3(model.module_defs)

    # Dataset
    dataset = LoadImagesAndLabels(train_path,
                                  img_size,
                                  batch_size,
                                  augment=True,
                                  hyp=hyp,  # augmentation hyperparameters
                                  rect=opt.rect,  # rectangular training
                                  image_weights=opt.img_weights,
                                  cache_labels=True if epochs > 10 else False,
                                  cache_images=False if opt.prebias else opt.cache_images)

    # Dataloader
    dataloader = torch.utils.data.DataLoader(dataset,
                                             batch_size=batch_size,
                                             num_workers=min([os.cpu_count(), batch_size, 16]),
                                             shuffle=not opt.rect,  # Shuffle=True unless rectangular training is used
                                             pin_memory=True,
                                             collate_fn=dataset.collate_fn)

    # Start training
    model.nc = nc  # attach number of classes to model
    model.arc = opt.arc  # attach yolo architecture
    model.hyp = hyp  # attach hyperparameters to model
    # model.class_weights = labels_to_class_weights(dataset.labels, nc).to(device)  # attach class weights
    torch_utils.model_info(model, report='summary')  # 'full' or 'summary'
    nb = len(dataloader)
    maps = np.zeros(nc)  # mAP per class
    results = (0, 0, 0, 0, 0, 0, 0)  # 'P', 'R', 'mAP', 'F1', 'val GIoU', 'val Objectness', 'val Classification'
    t0 = time.time()
    print('Starting %s for %g epochs...' % ('prebias' if opt.prebias else 'training', epochs))
    for epoch in range(start_epoch, epochs):  # epoch ------------------------------------------------------------------
        model.train()
        print(('\n' + '%10s' * 8) % ('Epoch', 'gpu_mem', 'GIoU', 'obj', 'cls', 'total', 'targets', 'img_size'))
        # 稀疏化标志
        sr_flag = get_sr_flag(epoch, opt.sr)

        # Freeze backbone at epoch 0, unfreeze at epoch 1 (optional)
        freeze_backbone = False
        if freeze_backbone and epoch < 2:
            for name, p in model.named_parameters():
                if int(name.split('.')[1]) < cutoff:  # if layer < 75
                    p.requires_grad = False if epoch == 0 else True

        # Update image weights (optional)
        if dataset.image_weights:
            w = model.class_weights.cpu().numpy() * (1 - maps) ** 2  # class weights
            image_weights = labels_to_image_weights(dataset.labels, nc=nc, class_weights=w)
            dataset.indices = random.choices(range(dataset.n), weights=image_weights, k=dataset.n)  # rand weighted idx

        mloss = torch.zeros(4).to(device)  # mean losses
        pbar = tqdm(enumerate(dataloader), total=nb)  # progress bar
        for i, (imgs, targets, paths, _) in pbar:  # batch -------------------------------------------------------------
            ni = i + nb * epoch  # number integrated batches (since train start)
            imgs = imgs.to(device)
            targets = targets.to(device)

            # Multi-Scale training
            if multi_scale:
                if ni / accumulate % 10 == 0:  #  adjust (67% - 150%) every 10 batches
                    img_size = random.randrange(img_sz_min, img_sz_max + 1) * 32
                sf = img_size / max(imgs.shape[2:])  # scale factor
                if sf != 1:
                    ns = [math.ceil(x * sf / 32.) * 32 for x in imgs.shape[2:]]  # new shape (stretched to 32-multiple)
                    imgs = F.interpolate(imgs, size=ns, mode='bilinear', align_corners=False)

            # Plot images with bounding boxes
            if ni == 0:
                fname = 'train_batch%g.jpg' % i
                plot_images(imgs=imgs, targets=targets, paths=paths, fname=fname)
                if tb_writer:
                    tb_writer.add_image(fname, cv2.imread(fname)[:, :, ::-1], dataformats='HWC')

            # Hyperparameter burn-in
            # n_burn = nb - 1  # min(nb // 5 + 1, 1000)  # number of burn-in batches
            # if ni <= n_burn:
            #     for m in model.named_modules():
            #         if m[0].endswith('BatchNorm2d'):
            #             m[1].momentum = 1 - i / n_burn * 0.99  # BatchNorm2d momentum falls from 1 - 0.01
            #     g = (i / n_burn) ** 4  # gain rises from 0 - 1
            #     for x in optimizer.param_groups:
            #         x['lr'] = hyp['lr0'] * g
            #         x['weight_decay'] = hyp['weight_decay'] * g

            # Run model
            pred = model(imgs)

            # Compute loss
            loss, loss_items = compute_loss(pred, targets, model)
            if not torch.isfinite(loss):
                print('WARNING: non-finite loss, ending training ', loss_items)
                return results

            soft_target = 0
            reg_ratio = 0  # 表示有多少target的回归是不如老师的，这时学生会跟gt再学习
            if t_cfg:
                if mixed_precision:
                    with torch.no_grad():
                        output_t = t_model(imgs)
                else:
                    _, output_t = t_model(imgs)
                if opt.KDstr == 1:
                    soft_target = compute_lost_KD(pred, output_t, model.nc, imgs.size(0))
                elif opt.KDstr == 2:
                    soft_target, reg_ratio = compute_lost_KD2(model, targets, pred, output_t)
                else:
                    print("please select KD strategy!")
                loss += soft_target
            # Scale loss by nominal batch_size of 64
            loss *= batch_size / 64

            # Compute gradient
            if mixed_precision:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            # 对要剪枝层的γ参数稀疏化
            if hasattr(model, 'module'):
                if opt.prune != -1:
                    BNOptimizer.updateBN(sr_flag, model.module.module_list, opt.s, prune_idx)
            else:
                BNOptimizer.updateBN(sr_flag, model.module_list, opt.s, prune_idx)

            # Accumulate gradient for x batches before optimizing
            if ni % accumulate == 0:
                optimizer.step()
                optimizer.zero_grad()

            # Print batch results
            mloss = (mloss * i + loss_items) / (i + 1)  # update mean losses
            mem = torch.cuda.memory_cached() / 1E9 if torch.cuda.is_available() else 0  # (GB)
            s = ('%10s' * 2 + '%10.3g' * 6) % (
                '%g/%g' % (epoch, epochs - 1), '%.3gG' % mem, *mloss, len(targets), img_size)
            pbar.set_description(s)

            # end batch ------------------------------------------------------------------------------------------------

        # Update scheduler
        scheduler.step()

        # Process epoch results
        final_epoch = epoch + 1 == epochs
        if opt.prebias:
            print_model_biases(model)
        else:
            # Calculate mAP (always test final epoch, skip first 10 if opt.nosave)
            if not (opt.notest or (opt.nosave and epoch < 10)) or final_epoch:
                with torch.no_grad():
                    results, maps = test.test(cfg,
                                              data,
                                              batch_size=batch_size,
                                              img_size=opt.img_size,
                                              model=model,
                                              conf_thres=0.001 if final_epoch and epoch > 0 else 0.1,  # 0.1 for speed
                                              save_json=final_epoch and epoch > 0 and 'coco.data' in data,
                                              quantized=opt.quantized,
                                              qlayers=opt.qlayers)

        # Write epoch results
        with open(results_file, 'a') as f:
            f.write(s + '%10.3g' * 7 % results + '\n')  # P, R, mAP, F1, test_losses=(GIoU, obj, cls)

        # Write Tensorboard results
        if tb_writer:
            x = list(mloss) + list(results)
            titles = ['GIoU', 'Objectness', 'Classification', 'Train loss',
                      'Precision', 'Recall', 'mAP', 'F1', 'val GIoU', 'val Objectness', 'val Classification']
            for xi, title in zip(x, titles):
                tb_writer.add_scalar(title, xi, epoch)

        # Update best mAP
        fitness = sum(results[4:])  # total loss
        if fitness < best_fitness:
            best_fitness = fitness

        # Save training results
        save = (not opt.nosave) or (final_epoch and not opt.evolve) or opt.prebias
        if save:
            with open(results_file, 'r') as f:
                # Create checkpoint
                chkpt = {'epoch': epoch,
                         'best_fitness': best_fitness,
                         'training_results': f.read(),
                         'model': model.module.state_dict() if type(
                             model) is nn.parallel.DistributedDataParallel else model.state_dict(),
                         'optimizer': None if final_epoch else optimizer.state_dict()}

            # Save last checkpoint
            torch.save(chkpt, last)
            if opt.bucket and not opt.prebias:
                os.system('gsutil cp %s gs://%s' % (last, opt.bucket))  # upload to bucket

            # Save best checkpoint
            if best_fitness == fitness:
                torch.save(chkpt, best)

            # Save backup every 10 epochs (optional)
            if epoch > 0 and epoch % 10 == 0:
                torch.save(chkpt, wdir + 'backup%g.pt' % epoch)

            # Delete checkpoint
            del chkpt

        # end epoch ----------------------------------------------------------------------------------------------------

    # end training
    if len(opt.name):
        os.rename('results.txt', 'results_%s.txt' % opt.name)
        os.rename(wdir + 'best.pt', wdir + 'best_%s.pt' % opt.name)
    plot_results()  # save as results.png
    print('%g epochs completed in %.3f hours.\n' % (epoch - start_epoch + 1, (time.time() - t0) / 3600))
    dist.destroy_process_group() if torch.cuda.device_count() > 1 else None
    torch.cuda.empty_cache()

    # save to cloud
    # os.system(gsutil cp results.txt gs://...)
    # os.system(gsutil cp weights/best.pt gs://...)

    return results


def prebias():
    # trains output bias layers for 1 epoch and creates new backbone
    if opt.prebias:
        train()  # transfer-learn yolo biases for 1 epoch
        create_backbone(last)  # saved results as backbone.pt
        opt.weights = wdir + 'backbone.pt'  # assign backbone
        opt.prebias = False  # disable prebias


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=273)  # 500200 batches at bs 16, 117263 images = 273 epochs
    parser.add_argument('--batch_size', type=int, default=16)  # effective bs = batch_size * accumulate = 16 * 4 = 64
    parser.add_argument('--accumulate', type=int, default=2, help='batches to accumulate before optimizing')
    parser.add_argument('--cfg', type=str, default='cfg/yolov3-hand.cfg', help='cfg file path')
    parser.add_argument('--t_cfg', type=str, default='', help='teacher model cfg file path for knowledge distillation')
    parser.add_argument('--data', type=str, default='data/oxfordhand.data', help='*.data file path')
    parser.add_argument('--multi_scale', action='store_true', help='adjust (67% - 150%) img_size every 10 batches')
    parser.add_argument('--img_size', type=int, default=416, help='inference size (pixels)')
    parser.add_argument('--rect', action='store_true', help='rectangular training')
    parser.add_argument('--resume', action='store_true', help='resume training from last.pt')
    parser.add_argument('--transfer', action='store_true', help='transfer learning')
    parser.add_argument('--nosave', action='store_true', help='only save final checkpoint')
    parser.add_argument('--notest', action='store_true', help='only test final epoch')
    parser.add_argument('--evolve', action='store_true', help='evolve hyperparameters')
    parser.add_argument('--bucket', type=str, default='', help='gsutil bucket')
    parser.add_argument('--img_weights', action='store_true', help='select training images by weight')
    parser.add_argument('--cache_images', action='store_true', help='cache images for faster training')
    parser.add_argument('--weights', type=str, default='weights/yolov3.weights',
                        help='initial weights')  # i.e. weights/darknet.53.conv.74
    parser.add_argument('--t_weights', type=str, default='', help='teacher model weights')
    parser.add_argument('--KDstr', type=int, default=-1, help='KD strategy')
    parser.add_argument('--arc', type=str, default='default', help='yolo architecture')  # defaultpw, uCE, uBCE
    parser.add_argument('--prebias', action='store_true', help='transfer-learn yolo biases prior to training')
    parser.add_argument('--name', default='', help='renames results.txt to results_name.txt if supplied')
    parser.add_argument('--device', default='', help='device id (i.e. 0 or 0,1) or cpu')
    parser.add_argument('--adam', action='store_true', help='use adam optimizer')
    parser.add_argument('--var', type=float, help='debug variable')
    parser.add_argument('--sparsity-regularization', '-sr', dest='sr', action='store_true',
                        help='train with channel sparsity regularization')
    parser.add_argument('--s', type=float, default=0.001, help='scale sparse rate')
    parser.add_argument('--prune', type=int, default=-1,
                        help='0:nomal prune or regular prune 1:shortcut prune 2:tiny prune')
    parser.add_argument('--quantized', type=int, default=-1,
                        help='0:quantization way one Ternarized weight and 8bit activation')
    parser.add_argument('--qlayers', type=int, default=-1,
                        help='0:no quantization , x:The shallow layer of current quantized layers(from deep to shallow)')

    opt = parser.parse_args()
    opt.weights = last if opt.resume else opt.weights
    print(opt)
    device = torch_utils.select_device(opt.device, apex=mixed_precision)

    tb_writer = None
    if not opt.evolve:  # Train normally
        try:
            # Start Tensorboard with "tensorboard --logdir=runs", view at http://localhost:6006/
            from torch.utils.tensorboard import SummaryWriter

            tb_writer = SummaryWriter()
        except:
            pass

        prebias()  # optional
        train()  # train normally

    # Evolve hyperparameters (optional)
    else:
        opt.notest = True  # only test final epoch
        opt.nosave = True  # only save final checkpoint
        if opt.bucket:
            os.system('gsutil cp gs://%s/evolve.txt .' % opt.bucket)  # download evolve.txt if exists

        for _ in range(1):  # generations to evolve
            if os.path.exists('evolve.txt'):  # if evolve.txt exists: select best hyps and mutate
                # Select parent(s)
                x = np.loadtxt('evolve.txt', ndmin=2)
                parent = 'weighted'  # parent selection method: 'single' or 'weighted'
                if parent == 'single' or len(x) == 1:
                    x = x[fitness(x).argmax()]
                elif parent == 'weighted':  # weighted combination
                    n = min(10, x.shape[0])  # number to merge
                    x = x[np.argsort(-fitness(x))][:n]  # top n mutations
                    w = fitness(x) - fitness(x).min()  # weights
                    x = (x[:n] * w.reshape(n, 1)).sum(0) / w.sum()  # new parent
                for i, k in enumerate(hyp.keys()):
                    hyp[k] = x[i + 7]

                # Mutate
                np.random.seed(int(time.time()))
                s = [.2, .2, .2, .2, .2, .2, .2, .0, .02, .2, .2, .2, .2, .2, .2, .2, .2, .2]  # sigmas
                for i, k in enumerate(hyp.keys()):
                    x = (np.random.randn(1) * s[i] + 1) ** 2.0  # plt.hist(x.ravel(), 300)
                    hyp[k] *= float(x)  # vary by sigmas

            # Clip to limits
            keys = ['lr0', 'iou_t', 'momentum', 'weight_decay', 'hsv_s', 'hsv_v', 'translate', 'scale', 'fl_gamma']
            limits = [(1e-5, 1e-2), (0.00, 0.70), (0.60, 0.98), (0, 0.001), (0, .9), (0, .9), (0, .9), (0, .9), (0, 3)]
            for k, v in zip(keys, limits):
                hyp[k] = np.clip(hyp[k], v[0], v[1])

            # Train mutation
            prebias()
            results = train()

            # Write mutation results
            print_mutation(hyp, results, opt.bucket)

            # Plot results
            # plot_evolution_results(hyp)
