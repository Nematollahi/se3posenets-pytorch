# Global imports
import argparse
import os
import sys
import shutil
import time
import numpy as np
import matplotlib.pyplot as plt
import random

# Torch imports
import torch
import torch.nn as nn
import torch.optim
import torch.utils.data
from torch.autograd import Variable
import torchvision

# Local imports
import se3layers as se3nn
import data
import ctrlnets
import util
from util import AverageMeter

# Parse arguments
parser = argparse.ArgumentParser(description='SE3-Pose-Nets Training')

# Dataset options
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('-b', '--batch-size', default=32, type=int,
                    metavar='N', help='mini-batch size (default: 32)')
parser.add_argument('-j', '--num-workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--train-per', default=0.6, type=float,
                    metavar='FRAC', help='fraction of data for the training set (default: 0.6)')
parser.add_argument('--val-per', default=0.15, type=float,
                    metavar='FRAC', help='fraction of data for the validation set (default: 0.15)')
parser.add_argument('--img-scale', default=1e-4, type=float,
                    metavar='IS', help='conversion scalar from depth resolution to meters (default: 1e-4)')
parser.add_argument('--step-len', default=1, type=int,
                    metavar='N', help='number of frames separating each example in the training sequence (default: 1)')
parser.add_argument('--seq-len', default=1, type=int,
                    metavar='N', help='length of the training sequence (default: 1)')
parser.add_argument('--ctrl-type', default='actdiffvel', type=str,
                    metavar='STR', help='Control type: actvel | actacc | comvel | comacc | comboth | [actdiffvel] | comdiffvel')

# Model options
parser.add_argument('--no-batch-norm', action='store_true', default=False,
                    help='disables batch normalization (default: False)')
parser.add_argument('--pre-conv', action='store_true', default=False,
                    help='puts batch normalization and non-linearity before the convolution / de-convolution (default: False)')
parser.add_argument('--nonlin', default='prelu', type=str,
                    metavar='NONLIN', help='type of non-linearity to use: [prelu] | relu | tanh | sigmoid | elu')
parser.add_argument('--se3-type', default='se3aa', type=str,
                    metavar='SE3', help='SE3 parameterization: [se3aa] | se3quat | se3spquat | se3euler | affine')
parser.add_argument('--pred-pivot', action='store_true', default=False,
                    help='Predict pivot in addition to the SE3 parameters (default: False)')
parser.add_argument('-n', '--num-se3', type=int, default=8,
                    help='Number of SE3s to predict (default: 8)')
parser.add_argument('--init-transse3-iden', action='store_true', default=False,
                    help='Initialize the weights for the SE3 prediction layer of the transition model to predict identity')
parser.add_argument('--init-posese3-iden', action='store_true', default=False,
                    help='Initialize the weights for the SE3 prediction layer of the pose-mask model to predict identity')
parser.add_argument('--decomp-model', action='store_true', default=False,
                    help='Use a separate encoder for predicting the pose and masks')
parser.add_argument('--local-delta-se3', action='store_true', default=False,
                    help='Predicted delta-SE3 operates in local co-ordinates not global co-ordinates, '
                         'so if we predict "D", full-delta = P1 * D * P1^-1, P2 = P1 * D')
parser.add_argument('--use-ntfm-delta', action='store_true', default=False,
                    help='Uses the variant of the NTFM3D layer that computes the weighted avg. delta')
parser.add_argument('--wide-model', action='store_true', default=False,
                    help='Wider network')

# Mask options
parser.add_argument('--use-wt-sharpening', action='store_true', default=False,
                    help='use weight sharpening for the mask prediction (instead of the soft-mask model) (default: False)')
parser.add_argument('--sharpen-start-iter', default=0, type=int,
                    metavar='N', help='Start the weight sharpening from this training iteration (default: 0)')
parser.add_argument('--sharpen-rate', default=1.0, type=float,
                    metavar='W', help='Slope of the weight sharpening (default: 1.0)')
parser.add_argument('--use-sigmoid-mask', action='store_true', default=False,
                    help='treat each mask channel independently using the sigmoid non-linearity. Pixel can belong to multiple masks (default: False)')

# Loss options
parser.add_argument('--pt-wt', default=0.01, type=float,
                    metavar='WT', help='Weight for the 3D point loss - only FWD direction (default: 0.01)')
parser.add_argument('--consis-wt', default=0.01, type=float,
                    metavar='WT', help='Weight for the pose consistency loss (default: 0.01)')
parser.add_argument('--loss-scale', default=10000, type=float,
                    metavar='WT', help='Default scale factor for all the losses (default: 1000)')

# Training options
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training (default: False)')
parser.add_argument('--use-pin-memory', action='store_true', default=False,
                    help='Use pin memory - note that this uses additional CPU RAM (default: False)')
parser.add_argument('--epochs', default=100, type=int, metavar='N',
                    help='number of total epochs to run (default: 100)')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts) (default: 0)')
parser.add_argument('--train-ipe', default=2000, type=int, metavar='N',
                    help='number of training iterations per epoch (default: 1000)')
parser.add_argument('--val-ipe', default=500, type=int, metavar='N',
                    help='number of validation iterations per epoch (default: 500)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on test set (default: False)')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')

# Optimization options
parser.add_argument('-o', '--optimization', default='adam', type=str,
                    metavar='OPTIM', help='type of optimization: sgd | [adam]')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    metavar='LR', help='initial learning rate (default: 1e-3)')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum (default: 0.9)')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--lr-decay', default=0.1, type=float, metavar='M',
                    help='Decay learning rate by this value every decay-epochs (default: 0.1)')
parser.add_argument('--decay-epochs', default=30, type=int,
                    metavar='M', help='Decay learning rate every this many epochs (default: 10)')

# Display/Save options
parser.add_argument('--disp-freq', '-p', default=20, type=int,
                    metavar='N', help='print/disp/save frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-s', '--save-dir', default='results', type=str,
                    metavar='PATH', help='directory to save results in. If it doesnt exist, will be created. (default: results/)')

################ MAIN
#@profile
def main():
    # Parse args
    global args, num_train_iter
    args = parser.parse_args()
    args.cuda       = not args.no_cuda and torch.cuda.is_available()
    args.batch_norm = not args.no_batch_norm

    ### Create save directory and start tensorboard logger
    util.create_dir(args.save_dir)  # Create directory
    now = time.strftime("%c")
    tblogger = util.TBLogger(args.save_dir + '/logs/' + now)  # Start tensorboard logger

    # TODO: Fix logfile to save prints - create new one if it is evaluated / resumed
    '''
    # Create logfile to save prints
    logfile = open(args.save_dir + '/logs/' + now + '/logfile.txt', 'w')
    backup = sys.stdout
    sys.stdout = Tee(sys.stdout, logfile)
    '''
    
    ########################
    ############ Parse options
    # Set seed
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    # Setup extra options
    args.img_ht, args.img_wd, args.img_suffix = 240, 320, 'sub'
    args.num_ctrl = 14 if (args.ctrl_type.find('both') >= 0) else 7 # Number of control dimensions
    print('Ht: {}, Wd: {}, Suffix: {}, Num ctrl: {}'.format(args.img_ht, args.img_wd, args.img_suffix, args.num_ctrl))

    # Read mesh ids and camera data
    load_dir = args.data.split(',,')[0]
    args.baxter_labels = data.read_baxter_labels_file(load_dir + '/statelabels.txt')
    args.mesh_ids      = args.baxter_labels['meshIds']
    args.cam_extrinsics = data.read_cameradata_file(load_dir + '/cameradata.txt')

    # SE3 stuff
    assert (args.se3_type in ['se3euler', 'se3aa', 'se3quat', 'affine', 'se3spquat']), 'Unknown SE3 type: ' + args.se3_type
    args.se3_dim = ctrlnets.get_se3_dimension(args.se3_type, args.pred_pivot)
    print('Predicting {} SE3s of type: {}. Dim: {}'.format(args.num_se3, args.se3_type, args.se3_dim))

    # Camera parameters
    args.cam_intrinsics = {'fx': 589.3664541825391/2,
                           'fy': 589.3664541825391/2,
                           'cx': 320.5/2,
                           'cy': 240.5/2}
    args.cam_intrinsics['xygrid'] = data.compute_camera_xygrid_from_intrinsics(args.img_ht, args.img_wd,
                                                                               args.cam_intrinsics)

    # Sequence stuff
    print('Step length: {}, Seq length: {}'.format(args.step_len, args.seq_len))

    # Loss parameters
    print('Loss scale: {}, Loss weights => PT: {}, CONSIS: {}'.format(
        args.loss_scale, args.pt_wt, args.consis_wt))

    # Weight sharpening stuff
    if args.use_wt_sharpening:
        print('Using weight sharpening to encourage binary mask prediction. Start iter: {}, Rate: {}'.format(
            args.sharpen_start_iter, args.sharpen_rate))
        assert not args.use_sigmoid_mask, "Cannot set both weight sharpening and sigmoid mask options together"
    elif args.use_sigmoid_mask:
        print('Using sigmoid to generate masks, treating each channel independently. A pixel can belong to multiple masks now')
    else:
        print('Using soft-max + weighted 3D transform loss to encourage mask prediction')

    # NTFM3D-Delta
    if args.use_ntfm_delta:
        print('Using the variant of NTFM3D that computes a weighted avg. flow per point using the SE3 transforms')

    # Decomp model
    if args.decomp_model:
        assert args.seq_len > 1, "Decomposed pose/mask encoders can be used only with multi-step models"

    # Wide model
    if args.wide_model:
        print('Using a wider network!')

    # TODO: Add option for using encoder pose for tfm t2

    ########################
    ############ Load datasets
    # Get datasets
    baxter_data     = data.read_recurrent_baxter_dataset(args.data, args.img_suffix,
                                                         step_len = args.step_len, seq_len = args.seq_len,
                                                         train_per = args.train_per, val_per = args.val_per)
    disk_read_func  = lambda d, i: data.read_baxter_sequence_from_disk(d, i, img_ht = args.img_ht, img_wd = args.img_wd,
                                                                       img_scale = args.img_scale, ctrl_type = 'actdiffvel',
                                                                       mesh_ids = args.mesh_ids,
                                                                       camera_extrinsics = args.cam_extrinsics,
                                                                       camera_intrinsics = args.cam_intrinsics,
                                                                       compute_bwdflows=False) # No need for BWD flows
    train_dataset = data.BaxterSeqDataset(baxter_data, disk_read_func, 'train') # Train dataset
    val_dataset   = data.BaxterSeqDataset(baxter_data, disk_read_func, 'val')   # Val dataset
    test_dataset  = data.BaxterSeqDataset(baxter_data, disk_read_func, 'test')  # Test dataset
    print('Dataset size => Train: {}, Validation: {}, Test: {}'.format(len(train_dataset), len(val_dataset), len(test_dataset)))

    # Create a data-collater for combining the samples of the data into batches along with some post-processing
    # TODO: Batch along dim 1 instead of dim 0

    # Create dataloaders (automatically transfer data to CUDA if args.cuda is set to true)
    train_loader = DataEnumerator(util.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
                                        num_workers=args.num_workers, pin_memory=args.use_pin_memory,
                                        collate_fn=train_dataset.collate_batch))
    val_loader   = DataEnumerator(util.DataLoader(val_dataset, batch_size=args.batch_size, shuffle=True,
                                        num_workers=args.num_workers, pin_memory=args.use_pin_memory,
                                        collate_fn=val_dataset.collate_batch))

    ########################
    ############ Load models & optimization stuff

    ### Load the model
    print('Using multi-step SE3-Pose-Model')
    num_train_iter = 0
    model = ctrlnets.MultiStepSE3PoseModel(num_ctrl=args.num_ctrl, num_se3=args.num_se3,
                                           se3_type=args.se3_type, use_pivot=args.pred_pivot, use_kinchain=False,
                                           input_channels=3, use_bn=args.batch_norm, nonlinearity=args.nonlin,
                                           init_posese3_iden=args.init_posese3_iden, init_transse3_iden=args.init_transse3_iden,
                                           use_wt_sharpening=args.use_wt_sharpening, sharpen_start_iter=args.sharpen_start_iter,
                                           sharpen_rate=args.sharpen_rate, pre_conv=args.pre_conv, decomp_model=args.decomp_model,
                                           use_sigmoid_mask=args.use_sigmoid_mask, local_delta_se3=args.local_delta_se3,
                                           wide=args.wide_model)
    if args.cuda:
        model.cuda() # Convert to CUDA if enabled

    ### Load optimizer
    optimizer = load_optimizer(args.optimization, model.parameters(), lr=args.lr,
                               momentum=args.momentum, weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        # TODO: Save path to TB log dir, save new log there again
        # TODO: Reuse options in args (see what all to use and what not)
        # TODO: Use same num train iters as the saved checkpoint
        # TODO: Print some stats on the training so far, reset best validation loss, best epoch etc
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint       = torch.load(args.resume)
            loadargs         = checkpoint['args']
            args.start_epoch = checkpoint['epoch']
            num_train_iter   = checkpoint['train_iter']
            try:
                model.load_state_dict(checkpoint['state_dict']) # BWDs compatibility (TODO: remove)
            except:
                model.load_state_dict(checkpoint['model_state_dict'])
            assert (loadargs.optimization == args.optimization), "Optimizer in saved checkpoint ({}) does not match current argument ({})".format(
                    loadargs.optimization, args.optimization)
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            print("=> loaded checkpoint '{}' (epoch {}, train iter {})"
                  .format(args.resume, checkpoint['epoch'], num_train_iter))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    ########################
    ############ Test (don't create the data loader unless needed, creates 4 extra threads)
    if args.evaluate:
        # Delete train and val loaders
        del train_loader, val_loader

        # TODO: Move this to before the train/val loader creation??
        print('==== Evaluating pre-trained network on test data ===')
        args.imgdisp_freq = 10 * args.disp_freq  # Tensorboard log frequency for the image data
        sampler = torch.utils.data.dataloader.SequentialSampler(test_dataset)  # Run sequentially along the test dataset
        test_loader = DataEnumerator(util.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False,
                                        num_workers=args.num_workers, sampler=sampler, pin_memory=args.use_pin_memory,
                                        collate_fn=test_dataset.collate_batch))
        iterate(test_loader, model, tblogger, len(test_loader), mode='test')
        return # Finish

    ########################
    ############ Train / Validate
    best_val_loss, best_epoch = float("inf"), 0
    args.imgdisp_freq = 5 * args.disp_freq # Tensorboard log frequency for the image data
    for epoch in range(args.start_epoch, args.epochs):
        # Adjust learning rate
        adjust_learning_rate(optimizer, epoch, args.lr_decay, args.decay_epochs)

        # Train for one epoch
        tr_loss, tr_ptloss, tr_consisloss, \
            tr_flowsum, tr_flowavg = iterate(train_loader, model, tblogger, args.train_ipe,
                                             mode='train', optimizer=optimizer, epoch=epoch+1)

        # Log values and gradients of the parameters (histogram)
        # NOTE: Doing this in the loop makes the stats file super large / tensorboard processing slow
        for tag, value in model.named_parameters():
            tag = tag.replace('.', '/')
            tblogger.histo_summary(tag, util.to_np(value.data), epoch + 1)
            tblogger.histo_summary(tag + '/grad', util.to_np(value.grad), epoch + 1)

        # Evaluate on validation set
        val_loss, val_ptloss, val_consisloss, \
            val_flowsum, val_flowavg = iterate(val_loader, model, tblogger, args.val_ipe,
                                                   mode='val', epoch=epoch+1)

        # Find best loss
        is_best       = (val_loss.avg < best_val_loss)
        prev_best_loss  = best_val_loss
        prev_best_epoch = best_epoch
        if is_best:
            best_val_loss = val_loss.avg
            best_epoch    = epoch+1
            print('==== Epoch: {}, Improved on previous best loss ({}) from epoch {}. Current: {} ===='.format(
                                    epoch+1, prev_best_loss, prev_best_epoch, val_loss.avg))
        else:
            print('==== Epoch: {}, Did not improve on best loss ({}) from epoch {}. Current: {} ===='.format(
                epoch + 1, prev_best_loss, prev_best_epoch, val_loss.avg))

        # Save checkpoint
        save_checkpoint({
            'epoch': epoch+1,
            'args' : args,
            'best_loss'  : best_val_loss,
            'train_stats': {'loss': tr_loss, 'ptloss': tr_ptloss,
                            'consisloss': tr_consisloss,
                            'flowsum': tr_flowsum, 'flowavg': tr_flowavg,
                            'niters': train_loader.niters, 'nruns': train_loader.nruns,
                            'totaliters': train_loader.iteration_count()
                            },
            'val_stats'  : {'loss': val_loss, 'ptloss': val_ptloss,
                            'consisloss': val_consisloss,
                            'flowsum': val_flowsum, 'flowavg': val_flowavg,
                            'niters': val_loader.niters, 'nruns': val_loader.nruns,
                            'totaliters': val_loader.iteration_count()
                            },
            'train_iter' : num_train_iter,
            'model_state_dict' : model.state_dict(),
            'optimizer_state_dict' : optimizer.state_dict(),
        }, is_best, savedir=args.save_dir)
        print('\n')

    # Delete train and val data loaders
    del train_loader, val_loader

    # Do final testing (if not asked to evaluate)
    # (don't create the data loader unless needed, creates 4 extra threads)
    print('==== Evaluating trained network on test data ====')
    args.imgdisp_freq = 10 * args.disp_freq # Tensorboard log frequency for the image data
    sampler = torch.utils.data.dataloader.SequentialSampler(test_dataset)  # Run sequentially along the test dataset
    test_loader = DataEnumerator(util.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False,
                                    num_workers=args.num_workers, sampler=sampler, pin_memory=args.use_pin_memory,
                                    collate_fn=test_dataset.collate_batch))
    iterate(test_loader, model, tblogger, len(test_loader), mode='test', epoch=args.epochs)
    print('==== Best validation loss: {} was from epoch: {} ===='.format(best_val_loss,
                                                                         best_epoch))

    # Close log file
    #logfile.close()

################# HELPER FUNCTIONS

### Main iterate function (train/test/val)
def iterate(data_loader, model, tblogger, num_iters,
            mode='test', optimizer=None, epoch=0):
    # Get global stuff?
    global num_train_iter

    # Setup avg time & stats:
    data_time, fwd_time, bwd_time, viz_time  = AverageMeter(), AverageMeter(), AverageMeter(), AverageMeter()
    lossm, ptlossm, consislossm = AverageMeter(), AverageMeter(), AverageMeter()
    flowlossm_sum, flowlossm_avg = AverageMeter(), AverageMeter()

    # Switch model modes
    train = True if (mode == 'train') else False
    if train:
        assert (optimizer is not None), "Please pass in an optimizer if we are iterating in training mode"
        model.train()
    else:
        assert (mode == 'test' or mode == 'val'), "Mode can be train/test/val. Input: {}"+mode
        model.eval()

    # Create a closure to get the outputs of the delta-se3 prediction layers
    predictions = {}
    def get_output(name):
        def hook(self, input, result):
            predictions[name] = result
        return hook
    model.transitionmodel.deltase3decoder.register_forward_hook(get_output('deltase3'))

    # Point predictor
    # NOTE: The prediction outputs of both layers are the same if mask normalization is used, if sigmoid the outputs are different
    # NOTE: Gradients are same for pts & tfms if mask normalization is used, always different for the masks
    ptpredlayer = se3nn.NTfm3DDelta if args.use_ntfm_delta else se3nn.NTfm3D

    # Run an epoch
    print('========== Mode: {}, Starting epoch: {}, Num iters: {} =========='.format(
        mode, epoch, num_iters))
    deftype = 'torch.cuda.FloatTensor' if args.cuda else 'torch.FloatTensor' # Default tensor type
    pt_wt, consis_wt = args.pt_wt * args.loss_scale, args.consis_wt * args.loss_scale
    for i in xrange(num_iters):
        # ============ Load data ============#
        # Start timer
        start = time.time()

        # Get a sample
        j, sample = data_loader.next()

        # Get inputs and targets (as variables)
        # Currently batchsize is the outer dimension
        pts    = util.to_var(sample['points'].type(deftype), requires_grad=True)
        ctrls  = util.to_var(sample['controls'].type(deftype), requires_grad=True)
        tarpts = util.to_var(sample['fwdflows'].type(deftype), requires_grad=False)
        tarpts.data.add_(pts.data.narrow(1,0,1).expand_as(tarpts.data)) # Add "k"-step flows to the initial point cloud

        # Measure data loading time
        data_time.update(time.time() - start)

        # ============ FWD pass + Compute loss ============#
        # Start timer
        start = time.time()

        ### Run a FWD pass through the network (multi-step)
        # Predict the poses and masks
        poses, initmask = [], None
        for k in xrange(pts.size(1)):
            # Predict the pose and mask at time t = 0
            # For all subsequent timesteps, predict only the poses
            if(k == 0):
                p, initmask = model.forward_pose_mask(pts[:,k], train_iter=num_train_iter)
            else:
                p = model.forward_only_pose(pts[:,k])
            poses.append(p)

        # Make next-pose predictions & corresponding 3D point predictions using the transition model
        # We use pose_0 and [ctrl_0, ctrl_1, .., ctrl_(T-1)] to make the predictions
        deltaposes, compdeltaposes, transposes = [], [], []
        for k in xrange(args.seq_len):
            # Get current pose
            if (k == 0):
                pose = poses[0] # Use initial pose
            else:
                pose = transposes[k-1] # Use previous predicted pose

            # Predict next pose based on curr pose, control
            delta, trans = model.forward_next_pose(pose, ctrls[:,k])
            deltaposes.append(delta)
            transposes.append(trans)

            # Compose the deltas over time (T4 = T4 * T3^-1 * T3 * T2^-1 * T2 * T1^-1 * T1 = delta_4 * delta_3 * delta_2 * T1
            if (k == 0):
                compdeltaposes.append(delta)
            else:
                compdeltaposes.append(se3nn.ComposeRtPair()(delta, compdeltaposes[k-1])) # delta_full = delta_curr * delta_prev

        # Now compute the losses across the sequence
        # We use point loss in the FWD dirn and Consistency loss between poses
        # For the point loss, we use the initial point cloud and mask &
        # predict in a sequence based on the predicted changes in poses
        predpts, ptloss, consisloss, loss = [], torch.zeros(args.seq_len), torch.zeros(args.seq_len), 0
        for k in xrange(args.seq_len):
            # Get current point cloud
            if (k == 0):
                currpts = pts[:,0]  # Use initial point cloud
            else:
                currpts = predpts[k-1]  # Use previous predicted point cloud

            # Predict transformed point cloud based on the total delta-transform so far
            nextpts = ptpredlayer()(currpts, initmask, compdeltaposes[k])
            predpts.append(nextpts)

            # Compute 3D point loss
            # For soft mask model, compute losses without predicting points (using composed transforms). Otherwise use predicted pts
            if args.use_wt_sharpening or args.use_sigmoid_mask:
                # Squared error between the predicted points and target points (Same as MSE loss)
                currptloss = pt_wt * ctrlnets.BiMSELoss(nextpts, tarpts[:,k])
            else:
                # Use the weighted 3D transform loss, do not use explicitly predicted points
                currptloss = pt_wt * se3nn.Weighted3DTransformLoss()(currpts, initmask, compdeltaposes[k], tarpts[:,k])  # Weighted point loss!

            # Compute pose consistency loss
            currconsisloss = consis_wt * ctrlnets.BiMSELoss(transposes[k], poses[k+1])  # Enforce consistency between pose predicted by encoder & pose from transition model

            # Append to total loss
            loss += currptloss + currconsisloss
            ptloss[k]     = currptloss.data[0]
            consisloss[k] = currconsisloss.data[0]

        # Update stats
        ptlossm.update(ptloss)
        consislossm.update(consisloss)
        lossm.update(loss.data[0])

        # Measure FWD time
        fwd_time.update(time.time() - start)

        # ============ Gradient backpass + Optimizer step ============#
        # Compute gradient and do optimizer update step (if in training mode)
        if (train):
            # Start timer
            start = time.time()

            # Backward pass & optimize
            optimizer.zero_grad() # Zero gradients
            loss.backward()       # Compute gradients - BWD pass
            optimizer.step()      # Run update step

            # Increment number of training iterations by 1
            num_train_iter += 1

            # Measure BWD time
            bwd_time.update(time.time() - start)

        # ============ Visualization ============#
        # Start timer
        start = time.time()

        # Compute flow predictions and errors
        # NOTE: I'm using CUDA here to speed up computation by ~4x
        predflows = torch.cat([(x.data - pts.data[:,0]).unsqueeze(1) for x in predpts], 1)
        flows = sample['fwdflows'].type(deftype)
        flowloss_sum, flowloss_avg, _, _ = compute_flow_errors(predflows, flows)

        # Update stats
        flowlossm_sum.update(flowloss_sum); flowlossm_avg.update(flowloss_avg)

        # Display/Print frequency
        if i % args.disp_freq == 0:
            ### Print statistics
            print_stats(mode, epoch=epoch, curr=i+1, total=num_iters,
                        samplecurr=j+1, sampletotal=len(data_loader),
                        loss=lossm, ptloss=ptlossm, consisloss=consislossm,
                        flowloss_sum=flowlossm_sum, flowloss_avg=flowlossm_avg)

            ### Print stuff if we have weight sharpening enabled
            if args.use_wt_sharpening:
                try:
                    noise_std, pow = model.posemaskmodel.compute_wt_sharpening_stats(train_iter=num_train_iter)
                except:
                    noise_std, pow = model.maskmodel.compute_wt_sharpening_stats(train_iter=num_train_iter)
                print('\tWeight sharpening => Num training iters: {}, Noise std: {:.4f}, Power: {:.3f}'.format(
                    num_train_iter, noise_std, pow))

            ### Print time taken
            print('\tTime => Data: {data.val:.3f} ({data.avg:.3f}), '
                        'Fwd: {fwd.val:.3f} ({fwd.avg:.3f}), '
                        'Bwd: {bwd.val:.3f} ({bwd.avg:.3f}), '
                        'Viz: {viz.val:.3f} ({viz.avg:.3f})'.format(
                    data=data_time, fwd=fwd_time, bwd=bwd_time, viz=viz_time))

            ### TensorBoard logging
            # (1) Log the scalar values
            iterct = data_loader.iteration_count() # Get total number of iterations so far
            info = {
                mode+'-loss': loss.data[0],
                mode+'-pt3dloss': ptloss.sum(),
                mode+'-consisloss': consisloss.sum(),
            }
            for tag, value in info.items():
                tblogger.scalar_summary(tag, value, iterct)

            # (2) Log images & print predicted SE3s
            # TODO: Numpy or matplotlib
            if i % args.imgdisp_freq == 0:

                ## Log the images (at a lower rate for now)
                id = random.randint(0, sample['points'].size(0)-1)

                # Concat the flows, depths and masks into one tensor
                flowdisp  = torchvision.utils.make_grid(torch.cat([flows.narrow(0,id,1),
                                                                   predflows.narrow(0,id,1)], 0).cpu().view(-1, 3, args.img_ht, args.img_wd),
                                                        nrow=args.seq_len, normalize=True, range=(-0.01, 0.01))
                depthdisp = torchvision.utils.make_grid(sample['points'][id].narrow(1,2,1), normalize=True, range=(0.0,3.0))
                maskdisp  = torchvision.utils.make_grid(torch.cat([initmask.data.narrow(0,id,1)], 0).cpu().view(-1, 1, args.img_ht, args.img_wd),
                                                        nrow=args.num_se3, normalize=True, range=(0,1))
                # Show as an image summary
                info = { mode+'-depths': util.to_np(depthdisp.narrow(0,0,1)),
                         mode+'-flows' : util.to_np(flowdisp.unsqueeze(0)),
                         mode+'-masks' : util.to_np(maskdisp.narrow(0,0,1))
                }
                for tag, images in info.items():
                    tblogger.image_summary(tag, images, iterct)

                ## Print the predicted delta-SE3s
                print '\tPredicted delta-SE3s @ t=2:', predictions['deltase3'].data[id].view(args.num_se3,
                                                                                             args.se3_dim).cpu()

                ## Print the predicted mask values
                print('\tPredicted mask stats:')
                for k in xrange(args.num_se3):
                    print('\tMax: {:.4f}, Min: {:.4f}, Mean: {:.4f}, Std: {:.4f}, Median: {:.4f}, Pred 1: {}'.format(
                        initmask.data[id,k].max(), initmask.data[id,k].min(), initmask.data[id,k].mean(),
                        initmask.data[id,k].std(), initmask.data[id,k].view(-1).cpu().float().median(),
                        (initmask.data[id,k] - 1).abs().le(1e-5).sum()))
                print('')

        # Measure viz time
        viz_time.update(time.time() - start)

    ### Print stats at the end
    print('========== Mode: {}, Epoch: {}, Final results =========='.format(mode, epoch))
    print_stats(mode, epoch=epoch, curr=num_iters, total=num_iters,
                samplecurr=data_loader.niters+1, sampletotal=len(data_loader),
                loss=lossm, ptloss=ptlossm, consisloss=consislossm,
                flowloss_sum=flowlossm_sum, flowloss_avg=flowlossm_avg)
    print('========================================================')

    # Return the loss & flow loss
    return lossm, ptlossm, consislossm, \
           flowlossm_sum, flowlossm_avg

### Print statistics
def print_stats(mode, epoch, curr, total, samplecurr, sampletotal,
                loss, ptloss, consisloss,
                flowloss_sum, flowloss_avg):
    # Print loss
    print('Mode: {}, Epoch: [{}/{}], Iter: [{}/{}], Sample: [{}/{}], '
          'Loss: {loss.val:.4f} ({loss.avg:.4f})'.format(
        mode, epoch, args.epochs, curr, total, samplecurr,
        sampletotal, loss=loss))

    # Print flow loss per timestep
    bsz = args.batch_size
    for k in xrange(args.seq_len):
        print('\tStep: {}, Pt: {:.3f} ({:.3f}), Consis: {:.3f} ({:.3f}), '
              'Flow => Sum: {:.3f} ({:.3f}), Avg: {:.6f} ({:.6f}), '.format(
            1 + k * args.step_len,
            ptloss.val[k], ptloss.avg[k], consisloss.val[k], consisloss.avg[k],
            flowloss_sum.val[k] / bsz, flowloss_sum.avg[k] / bsz,
            flowloss_avg.val[k] / bsz, flowloss_avg.avg[k] / bsz))

### Load optimizer
def load_optimizer(optim_type, parameters, lr=1e-3, momentum=0.9, weight_decay=1e-4):
    if optim_type == 'sgd':
        optimizer = torch.optim.SGD(params=parameters, lr=lr, momentum=momentum,
                                    weight_decay=weight_decay)
    elif optim_type == 'adam':
        optimizer = torch.optim.Adam(params=parameters, lr = lr, weight_decay= weight_decay)
    else:
        assert False, "Unknown optimizer type: " + optim_type
    return optimizer

### Save checkpoint
def save_checkpoint(state, is_best, savedir='.', filename='checkpoint.pth.tar'):
    savefile = savedir + '/' + filename
    torch.save(state, savefile)
    if is_best:
        shutil.copyfile(savefile, savedir + '/model_best.pth.tar')

### Compute flow errors (flows are size: B x S x 3 x H x W)
def compute_flow_errors(predflows, gtflows):
    batch, seq = predflows.size(0), predflows.size(1) # B x S x 3 x H x W
    num_pts, loss_sum, loss_avg, nz = torch.zeros(seq), torch.zeros(seq), torch.zeros(seq), torch.zeros(seq)
    for k in xrange(seq):
        # Compute errors per dataset
        # !!!!!!!!! :ge(1e-3) returns a ByteTensor and if u sum within byte tensors, the max value we can get is 255 !!!!!!!!!
        num_pts_d  = torch.abs(gtflows[:,k]).sum(1).ge(1e-3).float().view(batch, -1).sum(1) # Num pts with flow per dataset
        loss_sum_d = (predflows[:,k] - gtflows[:,k]).pow(2).view(batch, -1).sum(1).float()  # Sum of errors per dataset

        # Sum up errors per batch
        num_pts[k]  = num_pts_d.sum()               # Num pts that have non-zero flow
        loss_sum[k] = loss_sum_d.sum()              # Sum of total flow loss across the batch
        for j in xrange(batch):
            if (num_pts_d[j] > 0):
                loss_avg[k] += (loss_sum_d[j] / num_pts_d[j]) # Sum of per-point loss across the batch
                nz[k]       += 1 # We have one more dataset with non-zero num pts that move
    # Return
    return loss_sum, loss_avg, num_pts, nz

### Compute flow errors per mask (flows are size: B x S x 3 x H x W)
def compute_flow_errors_per_mask(predflows, gtflows, gtmasks):
    batch, seq, nse3 = predflows.size(0), predflows.size(1), gtmasks.size(2)  # B x S x 3 x H x W
    num_pts, loss_sum, loss_avg, nz = torch.zeros(seq,nse3), torch.zeros(seq,nse3), torch.zeros(seq,nse3), torch.zeros(seq,nse3)
    for k in xrange(seq):
        mask = torch.abs(gtflows[:,k]).sum(1).ge(1e-3).float()        # Set of points that move in the current scene
        err  = (predflows[:,k] - gtflows[:,k]).pow(2).sum(1).float()  # Flow error for current step
        for j in xrange(nse3):  # Iterate over the mask-channels
            # Compute error per dataset
            maskc       = gtmasks[:,j].clone().float() * mask   # Pts belonging to current link that move in scene
            num_pts_d   = gtmasks[:,j].clone().view(batch, -1).sum(1).float() # Num pts per mask per dataset
            loss_sum_d  = (err * maskc).view(batch, -1).sum(1)  # Flow error sum per mask per dataset

            # Sum up errors actoss the batch
            num_pts[k][j]   = num_pts_d.sum()   # Num pts that have non-zero flow
            loss_sum[k][j]  = loss_sum_d.sum()  # Sum of total flow loss across batch
            for i in xrange(batch):
                if (num_pts_d[i] > 0):
                    loss_avg[k][j]  += (loss_sum_d[i] / num_pts_d[i]) # Sum of per-point flow across batch
                    nz[k][j]        += 1 # One more dataset
    # Return
    return loss_sum, loss_avg, num_pts, nz

### Normalize image
def normalize_img(img, min=-0.01, max=0.01):
    return (img - min) / (max - min)

### Adjust learning rate
def adjust_learning_rate(optimizer, epoch, decay_rate=0.1, decay_epochs=10):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (decay_rate ** (epoch // decay_epochs))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

################# HELPER CLASSES

### Enumerate oer data
class DataEnumerator(object):
    """Allows iterating over a data loader easily"""
    def __init__(self, data):
        self.data   = data # Store the data
        self.len    = len(self.data) # Number of samples in the entire data
        self.niters = 0    # Num iterations in current run
        self.nruns  = 0    # Num rounds over the entire data
        self.enumerator = enumerate(self.data) # Keeps an iterator around

    def next(self):
        try:
            sample = self.enumerator.next() # Get next sample
        except StopIteration:
            self.enumerator = enumerate(self.data) # Reset enumerator once it reaches the end
            self.nruns += 1 # Done with one complete run of the data
            self.niters = 0 # Num iters in current run
            sample = self.enumerator.next() # Get next sample
            #print('Completed a run over the data. Num total runs: {}, Num total iters: {}'.format(
            #    self.nruns, self.niters+1))
        self.niters += 1 # Increment iteration count
        return sample # Return sample

    def __len__(self):
        return len(self.data)

    def iteration_count(self):
        return (self.nruns * self.len) + self.niters

### Write to stdout and log file
class Tee(object):
    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        for f in self.files:
            f.write(obj)

################ RUN MAIN
if __name__ == '__main__':
    main()
