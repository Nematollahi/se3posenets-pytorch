# Global imports
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
import torch.nn.functional as F
import torch.optim
import torch.utils.data
from torch.autograd import Variable
import torchvision
torch.multiprocessing.set_sharing_strategy('file_system')

# Local imports
import se3layers as se3nn
import blockdata
import ctrlnets
import util
from util import AverageMeter, Tee, DataEnumerator
import helperfuncs as helpers

## New layers
import se3
import se3posenets

#### Setup options
# Common
import argparse
import options
parser = options.setup_common_block_options()

# Mask consistency loss options
parser.add_argument('--mask-consis-wt', default=0.0, type=float,
                    metavar='WT', help='Weight for the mask consistency loss - only FWD direction (default: 0.01)')
parser.add_argument('--mask-consis-loss-type', default='mse', type=str,
                    metavar='STR', help='Type of loss to use for mask consistency errors, '
                                        '(default: mse | abs | kl | kllog)')
parser.add_argument('--pre-mask-consis', action='store_true', default=False,
                    help='Use the pre-sharpened activations for mask consistency loss (default: False)')

# Define xrange
try:
    a = xrange(1)
except NameError: # Not defined in Python 3.x
    def xrange(*args):
        return iter(range(*args))

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

    # Create logfile to save prints
    logfile = open(args.save_dir + '/logs/' + now + '/logfile.txt', 'w')
    backup = sys.stdout
    sys.stdout = Tee(sys.stdout, logfile)

    ########################
    ############ Parse options
    # Set seed
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    # Setup datasets
    train_dataset, val_dataset, test_dataset = blockdata.parse_options_and_setup_block_dataset_loader(args)

    # Create a data-collater for combining the samples of the data into batches along with some post-processing
    if args.evaluate:
        # Load only test loader
        args.imgdisp_freq = 10 * args.disp_freq  # Tensorboard log frequency for the image data
        sampler = torch.utils.data.dataloader.SequentialSampler(test_dataset)  # Run sequentially along the test dataset
        test_loader = DataEnumerator(util.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False,
                                                     num_workers=args.num_workers, sampler=sampler,
                                                     pin_memory=False, collate_fn=test_dataset.collate_batch))
    else:
        # Create dataloaders (automatically transfer data to CUDA if args.cuda is set to true)
        train_loader = DataEnumerator(util.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
                                                      num_workers=args.num_workers, pin_memory=False,
                                                      collate_fn=train_dataset.collate_batch))
        val_loader = DataEnumerator(util.DataLoader(val_dataset, batch_size=args.batch_size, shuffle=True,
                                                    num_workers=args.num_workers, pin_memory=False,
                                                    collate_fn=val_dataset.collate_batch))

    ########################
    ############ Load models & optimization stuff

    ### Load the model
    num_train_iter = 0
    num_input_channels = 3 # Num input channels
    if args.use_xyzrgb:
        num_input_channels = 6
    model = se3posenets.MultiStepSE3PoseModel(
                    num_ctrl=args.num_ctrl, num_se3=args.num_se3, se3_type=args.se3_type,
                    input_channels=num_input_channels, use_bn=args.batch_norm, nonlinearity=args.nonlin,
                    init_posese3_iden=args.init_posese3_iden, init_transse3_iden=args.init_transse3_iden,
                    use_wt_sharpening=args.use_wt_sharpening, sharpen_start_iter=args.sharpen_start_iter,
                    sharpen_rate=args.sharpen_rate, wide=args.wide_model, use_jt_angles=args.use_jt_angles,
                    num_state=args.num_ctrl, noise_stop_iter=args.noise_stop_iter,
                    use_se3nn=args.use_se3nn)
    if args.cuda:
        model.cuda() # Convert to CUDA if enabled

    ### Load optimizer
    optimizer = helpers.load_optimizer(args.optimization, model.parameters(), lr=args.lr,
                               momentum=args.momentum, weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint       = torch.load(args.resume)
            loadargs         = checkpoint['args']
            args.start_epoch = checkpoint['epoch']
            if args.reset_train_iter:
                num_train_iter   = 0 # Reset to 0
            else:
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
            best_loss    = checkpoint['best_loss'] if 'best_loss' in checkpoint else float("inf")
            best_floss   = checkpoint['best_flow_loss'] if 'best_flow_loss' in checkpoint else float("inf")
            best_fcloss  = checkpoint['best_flowconsis_loss'] if 'best_flowconsis_loss' in checkpoint else float("inf")
            best_epoch   = checkpoint['best_epoch'] if 'best_epoch' in checkpoint else 0
            best_fepoch  = checkpoint['best_flow_epoch'] if 'best_flow_epoch' in checkpoint else 0
            best_fcepoch = checkpoint['best_flowconsis_epoch'] if 'best_flowconsis_epoch' in checkpoint else 0
            print('==== Best validation loss: {} was from epoch: {} ===='.format(best_loss, best_epoch))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
    else:
        best_loss, best_floss, best_fcloss    = float("inf"), float("inf"), float("inf")
        best_epoch, best_fepoch, best_fcepoch = 0, 0, 0

    ########################
    ############ Test (don't create the data loader unless needed, creates 4 extra threads)
    if args.evaluate:
        print('==== Evaluating pre-trained network on test data ===')
        test_stats = iterate(test_loader, model, tblogger, len(test_loader), mode='test')

        # Save final test error
        helpers.save_checkpoint({
            'args': args,
            'test_stats': {'stats': test_stats,
                           'niters': test_loader.niters, 'nruns': test_loader.nruns,
                           'totaliters': test_loader.iteration_count(),
                           'ids': test_stats.data_ids,
                           },
        }, False, savedir=args.save_dir, filename='test_stats.pth.tar')

        # Close log file & return
        logfile.close()
        return

    ## Create a file to log different validation errors over training epochs
    statstfile = open(args.save_dir + '/epochtrainstats.txt', 'w')
    statsvfile = open(args.save_dir + '/epochvalstats.txt', 'w')
    statstfile.write("Epoch, Loss, Ptloss, Consisloss, Flowerrsum, Flowerravg, Consiserr\n")
    statsvfile.write("Epoch, Loss, Ptloss, Consisloss, Flowerrsum, Flowerravg, Consiserr\n")

    ########################
    ############ Train / Validate
    args.imgdisp_freq = 5 * args.disp_freq # Tensorboard log frequency for the image data
    train_ids, val_ids = [], []
    for epoch in range(args.start_epoch, args.epochs):
        # Adjust learning rate
        adjust_learning_rate(optimizer, epoch, args.lr_decay, args.decay_epochs, args.min_lr)

        # Train for one epoch
        train_stats = iterate(train_loader, model, tblogger, args.train_ipe,
                           mode='train', optimizer=optimizer, epoch=epoch+1)
        train_ids += train_stats.data_ids

        # Evaluate on validation set
        val_stats = iterate(val_loader, model, tblogger, args.val_ipe,
                            mode='val', epoch=epoch+1)
        val_ids += val_stats.data_ids

        # Find best losses
        val_loss, val_floss, val_fcloss = val_stats.loss.avg, \
                                          val_stats.ptloss.avg.sum(), \
                                          val_stats.ptloss.avg.sum() + val_stats.consisloss.avg.sum()
        is_best, is_fbest, is_fcbest    = (val_loss < best_loss), (val_floss < best_floss), (val_fcloss < best_fcloss)
        prev_best_loss, prev_best_floss, prev_best_fcloss    = best_loss, best_floss, best_fcloss
        prev_best_epoch, prev_best_fepoch, prev_best_fcepoch = best_epoch, best_fepoch, best_fcepoch
        s, sf, sfc = 'SAME', 'SAME', 'SAME'
        if is_best:
            best_loss, best_epoch, s       = val_loss, epoch+1, 'IMPROVED'
        if is_fbest:
            best_floss, best_fepoch, sf    = val_floss, epoch+1, 'IMPROVED'
        if is_fcbest:
            best_fcloss, best_fcepoch, sfc = val_fcloss, epoch+1, 'IMPROVED'
        print('==== [LOSS]   Epoch: {}, Status: {}, Previous best: {:.5f}/{}. Current: {:.5f}/{} ===='.format(
                                    epoch+1, s, prev_best_loss, prev_best_epoch, best_loss, best_epoch))
        print('==== [FLOSS]  Epoch: {}, Status: {}, Previous best: {:.5f}/{}. Current: {:.5f}/{} ===='.format(
                                    epoch+1, sf, prev_best_floss, prev_best_fepoch, best_floss, best_fepoch))
        print('==== [FCLOSS] Epoch: {}, Status: {}, Previous best: {:.5f}/{}. Current: {:.5f}/{} ===='.format(
                                    epoch+1, sfc, prev_best_fcloss, prev_best_fcepoch, best_loss, best_fcepoch))

        # Write losses to stats file
        statstfile.write("{}, {}, {}, {}, {}, {}, {}\n".format(epoch+1, train_stats.loss.avg,
                                                                       train_stats.ptloss.avg.sum(),
                                                                       train_stats.consisloss.avg.sum(),
                                                                       train_stats.flowerr_sum.avg.sum()/args.batch_size,
                                                                       train_stats.flowerr_avg.avg.sum()/args.batch_size,
                                                                       train_stats.consiserr.avg.sum()))
        statsvfile.write("{}, {}, {}, {}, {}, {}, {}\n".format(epoch + 1, val_stats.loss.avg,
                                                                       val_stats.ptloss.avg.sum(),
                                                                       val_stats.consisloss.avg.sum(),
                                                                       val_stats.flowerr_sum.avg.sum() / args.batch_size,
                                                                       val_stats.flowerr_avg.avg.sum() / args.batch_size,
                                                                       val_stats.consiserr.avg.sum()))

        # Save checkpoint
        helpers.save_checkpoint({
            'epoch': epoch+1,
            'args' : args,
            'best_loss'            : best_loss,
            'best_flow_loss'       : best_floss,
            'best_flowconsis_loss' : best_fcloss,
            'best_epoch'           : best_epoch,
            'best_flow_epoch'      : best_fepoch,
            'best_flowconsis_epoch': best_fcepoch,
            'train_stats': {'stats': train_stats,
                            'niters': train_loader.niters, 'nruns': train_loader.nruns,
                            'totaliters': train_loader.iteration_count(),
                            'ids': train_ids,
                            },
            'val_stats'  : {'stats': val_stats,
                            'niters': val_loader.niters, 'nruns': val_loader.nruns,
                            'totaliters': val_loader.iteration_count(),
                            'ids': val_ids,
                            },
            'train_iter' : num_train_iter,
            'model_state_dict' : model.state_dict(),
            'optimizer_state_dict' : optimizer.state_dict(),
        }, is_best, is_fbest, is_fcbest, savedir=args.save_dir, filename='checkpoint.pth.tar') #_{}.pth.tar'.format(epoch+1))
        print('\n')

    # Delete train and val data loaders
    del train_loader, val_loader

    # Load best model for testing (not latest one)
    print("=> loading best model from '{}'".format(args.save_dir + "/model_flow_best.pth.tar"))
    checkpoint = torch.load(args.save_dir + "/model_flow_best.pth.tar")
    num_train_iter = checkpoint['train_iter']
    try:
        model.load_state_dict(checkpoint['state_dict'])  # BWDs compatibility (TODO: remove)
    except:
        model.load_state_dict(checkpoint['model_state_dict'])
    print("=> loaded best checkpoint (epoch {}, train iter {})"
          .format(checkpoint['epoch'], num_train_iter))
    best_epoch   = checkpoint['best_epoch'] if 'best_epoch' in checkpoint else 0
    best_fepoch  = checkpoint['best_flow_epoch'] if 'best_flow_epoch' in checkpoint else 0
    best_fcepoch = checkpoint['best_flowconsis_epoch'] if 'best_flowconsis_epoch' in checkpoint else 0
    print('==== Best validation loss: {:.5f} was from epoch: {} ===='.format(checkpoint['best_loss'],
                                                                         best_epoch))
    print('==== Best validation flow loss: {:.5f} was from epoch: {} ===='.format(checkpoint['best_flow_loss'],
                                                                         best_fepoch))
    print('==== Best validation flow-consis loss: {:.5f} was from epoch: {} ===='.format(checkpoint['best_flowconsis_loss'],
                                                                         best_fcepoch))

    # Do final testing (if not asked to evaluate)
    # (don't create the data loader unless needed, creates 4 extra threads)
    print('==== Evaluating trained network on test data ====')
    args.imgdisp_freq = 10 * args.disp_freq # Tensorboard log frequency for the image data
    sampler = torch.utils.data.dataloader.SequentialSampler(test_dataset)  # Run sequentially along the test dataset
    test_loader = DataEnumerator(util.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False,
                                    num_workers=args.num_workers, sampler=sampler, pin_memory=args.use_pin_memory,
                                    collate_fn=test_dataset.collate_batch))
    test_stats = iterate(test_loader, model, tblogger, len(test_loader),
                         mode='test', epoch=args.epochs)
    print('==== Best validation loss: {:.5f} was from epoch: {} ===='.format(checkpoint['best_loss'],
                                                                         best_epoch))
    print('==== Best validation flow loss: {:.5f} was from epoch: {} ===='.format(checkpoint['best_flow_loss'],
                                                                         best_fepoch))
    print('==== Best validation flow-consis loss: {:.5f} was from epoch: {} ===='.format(checkpoint['best_flowconsis_loss'],
                                                                         best_fcepoch))

    # Save final test error
    helpers.save_checkpoint({
        'args': args,
        'test_stats': {'stats': test_stats,
                       'niters': test_loader.niters, 'nruns': test_loader.nruns,
                       'totaliters': test_loader.iteration_count(),
                       'ids': test_stats.data_ids,
                       },
    }, is_best=False, savedir=args.save_dir, filename='test_stats.pth.tar')

    # Write test stats to val stats file at the end
    statsvfile.write("{}, {}, {}, {}, {}, {}, {}\n".format(checkpoint['epoch'], test_stats.loss.avg,
                                                                   test_stats.ptloss.avg.sum(),
                                                                   test_stats.consisloss.avg.sum(),
                                                                   test_stats.flowerr_sum.avg.sum() / args.batch_size,
                                                                   test_stats.flowerr_avg.avg.sum() / args.batch_size,
                                                                   test_stats.consiserr.avg.sum()))
    statsvfile.close(); statstfile.close()

    # Close log file
    logfile.close()

################# HELPER FUNCTIONS

### Main iterate function (train/test/val)
def iterate(data_loader, model, tblogger, num_iters,
            mode='test', optimizer=None, epoch=0):
    # Get global stuff?
    global num_train_iter

    # Setup avg time & stats:
    data_time, fwd_time, bwd_time, viz_time  = AverageMeter(), AverageMeter(), AverageMeter(), AverageMeter()

    # Save all stats into a namespace
    stats = argparse.Namespace()
    stats.loss, stats.ptloss, stats.consisloss  = AverageMeter(), AverageMeter(), AverageMeter()
    stats.flowerr_sum, stats.flowerr_avg        = AverageMeter(), AverageMeter()
    stats.motionerr_sum, stats.motionerr_avg    = AverageMeter(), AverageMeter()
    stats.stillerr_sum, stats.stillerr_avg      = AverageMeter(), AverageMeter()
    stats.consiserr                             = AverageMeter()
    stats.maskconsisloss                        = AverageMeter()
    stats.data_ids = []
    if mode == 'test':
        # Save the flow errors and poses if in "testing" mode
        stats.motion_err, stats.motion_npt, stats.still_err, stats.still_npt = [], [], [], []
        stats.predposes, stats.predtransposes, stats.preddeltas, stats.ctrls = [], [], [], []
        stats.poses = []

    # Switch model modes
    train = (mode == 'train')
    if train:
        assert (optimizer is not None), "Please pass in an optimizer if we are iterating in training mode"
        model.train()
    else:
        assert (mode == 'test' or mode == 'val'), "Mode can be train/test/val. Input: {}"+mode
        model.eval()

    # Run an epoch
    print('========== Mode: {}, Starting epoch: {}, Num iters: {} =========='.format(
        mode, epoch, num_iters))
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    pt_wt, consis_wt = args.pt_wt * args.loss_scale, args.consis_wt * args.loss_scale
    mask_consis_wt = args.mask_consis_wt * args.loss_scale
    tfmlayer = se3nn.NTfm3D()
    for i in xrange(num_iters):
        # ============ Load data ============#
        # Start timer
        start = time.time()

        # Get a sample
        j, sample = data_loader.next()
        stats.data_ids.append(sample['id'].clone())

        # Get inputs and targets (as variables)
        # Currently batchsize is the outer dimension
        pts      = util.req_grad(sample['points'].to(device), train) # Need gradients
        ctrls    = util.req_grad(sample['controls'].to(device), train) # Need gradients
        fwdflows = util.req_grad(sample['fwdflows'].to(device), False) # No gradients
        fwdvis   = util.req_grad(sample['fwdvisibilities'].float().to(device), False)

        # Get XYZRGB input
        if args.use_xyzrgb:
            rgb = util.req_grad(sample['rgbs'].type_as(pts)/255.0, train) # Normalize RGB to 0-1
            netinput = torch.cat([pts, rgb], 2) # Concat along channels dimension
        else:
            netinput = pts # XYZ

        # Get fwd pixel associations
        fwdpixelassocs = util.req_grad(sample['fwdassocpixelids'].to(device), False)

        # Get jt angles
        states = util.req_grad(sample['states'].to(device), train) #[:, :, args.ctrlids_in_state].to(device), requires_grad=train)

        # Measure data loading time
        data_time.update(time.time() - start)

        # ============ FWD pass + Compute loss ============#
        # Start timer
        start = time.time()

        ####### Run a FWD pass through the network (multi-step)
        # Predict the poses and masks
        pose0, mask0 = model.forward_pose_mask([netinput[:,0], states[:,0]], train_iter=num_train_iter)
        mask0pre = model.posemaskmodel.pre_sharpen_mask.clone()
        pose1, mask1 = model.forward_pose_mask([netinput[:,1], states[:,1]], train_iter=num_train_iter)
        mask1pre = model.posemaskmodel.pre_sharpen_mask.clone()

        # Predict next pose based on pose & ctrl
        deltapose01, transpose1 = model.forward_next_pose(pose0, ctrls[:,0])

        # Predict transformed 3D points
        predpts = tfmlayer(pts[:,0], mask0, deltapose01)

        ####### Compute losses - We use point loss in the FWD dirn and Consistency loss between poses
        ### 3D loss
        # If motion-normalized loss, pass in GT flows
        inputs, targets = predpts - pts[:,0], fwdflows[:,0]
        if args.motion_norm_loss:
            motion = targets  # Use either delta-flows or full-flows
            currptloss = pt_wt * ctrlnets.MotionNormalizedLoss3D(inputs, targets, motion=motion,
                                                                 loss_type=args.loss_type, wts=fwdvis[:,0])
        else:
            currptloss = pt_wt * ctrlnets.Loss3D(inputs, targets, loss_type=args.loss_type, wts=fwdvis[:,0])

        ### Consistency loss (between t & t+1)
        # Poses from encoder @ t & @ t+1 should be separated by delta from t->t+1
        # NOTE: For the consistency loss, the loss is only backpropagated to the encoder poses, not to the deltas
        delta = deltapose01.detach()  # Break the graph here
        nextpose_trans = model.transitionmodel.posedecoder(delta, pose0)
        currconsisloss = consis_wt * ctrlnets.BiMSELoss(nextpose_trans, pose1)

        ### Mask Consistency Loss (between t & t+1)
        currmaskconsisloss, maskconsisloss = 0, torch.zeros(1)
        if mask_consis_wt > 0:
            if args.pre_mask_consis:
                if args.mask_consis_loss_type == 'kl':
                    mask0in, mask1in = F.softmax(mask0pre), F.softmax(mask1pre) # Softmax the pre-sharpening activations
                elif args.mask_consis_loss_type == 'kllog':
                    mask0in, mask1in = F.log_softmax(mask0pre), F.log_softmax(mask1pre) # Expects log inputs
                else:
                    mask0in, mask1in = mask0pre, mask1pre
            else:
                assert(args.mask_consis_loss_type is not 'kllog') # We have mask outputs which are not logs
                mask0in, mask1in = mask0, mask1
            currmaskconsisloss = mask_consis_wt * ctrlnets.MaskConsistencyLoss(mask0in, mask1in,
                                                                               fwdpixelassocs[:,0],
                                                                               args.mask_consis_loss_type)
            maskconsisloss = torch.Tensor([currmaskconsisloss.item()])

        # Append to total loss
        loss = currptloss + currconsisloss + currmaskconsisloss
        ptloss     = torch.Tensor([currptloss.item()])
        consisloss = torch.Tensor([currconsisloss.item()])

        # Update stats
        stats.ptloss.update(ptloss)
        stats.consisloss.update(consisloss)
        stats.maskconsisloss.update(maskconsisloss)
        stats.loss.update(loss.item())

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
        # Make sure to not add to the computation graph (will memory leak otherwise)!
        with torch.no_grad():

            # Start timer
            start = time.time()

            # Compute flow predictions and errors
            # NOTE: I'm using CUDA here to speed up computation by ~4x
            predflows = torch.cat([(predpts - pts[:,0]).unsqueeze(1)], 1)
            flows = fwdflows
            if args.use_only_da_for_flows:
                # If using only DA then pts that are not visible will not have GT flows, so we shouldn't take them into
                # account when computing the flow errors
                flowerr_sum, flowerr_avg, \
                    motionerr_sum, motionerr_avg,\
                    stillerr_sum, stillerr_avg,\
                    motion_err, motion_npt,\
                    still_err, still_npt         = helpers.compute_masked_flow_errors(predflows * fwdvis, flows) # Zero out flows for non-visible points
            else:
                flowerr_sum, flowerr_avg, \
                    motionerr_sum, motionerr_avg, \
                    stillerr_sum, stillerr_avg, \
                    motion_err, motion_npt, \
                    still_err, still_npt         = helpers.compute_masked_flow_errors(predflows, flows)

            # Update stats
            stats.flowerr_sum.update(flowerr_sum); stats.flowerr_avg.update(flowerr_avg)
            stats.motionerr_sum.update(motionerr_sum); stats.motionerr_avg.update(motionerr_avg)
            stats.stillerr_sum.update(stillerr_sum); stats.stillerr_avg.update(stillerr_avg)
            if mode == 'test':
                stats.motion_err.append(motion_err); stats.motion_npt.append(motion_npt)
                stats.still_err.append(still_err); stats.still_npt.append(still_npt)

            # Save poses if in test mode
            if (mode == 'test') and (args.detailed_test_stats):
                stats.predposes.append([x.cpu().float() for x in [pose0, pose1]])
                stats.predtransposes.append([transpose1.cpu().float()])
                stats.preddeltas.append([deltapose01.cpu().float()])
                stats.ctrls.append(ctrls.cpu().float())
                stats.poses.append(sample['poses'])

            ### Pose consistency error
            # Compute consistency error for display
            consiserror, consiserrormax = torch.zeros(args.seq_len), torch.zeros(args.seq_len)
            consiserrormax[0] = (pose1 - transpose1).abs().max()
            consiserror[0]    = ctrlnets.BiAbsLoss(pose1, transpose1)
            stats.consiserr.update(consiserror)

            # Display/Print frequency
            bsz = pts.size(0)
            if i % args.disp_freq == 0:
                ### Print statistics
                print_stats(mode, epoch=epoch, curr=i+1, total=num_iters,
                            samplecurr=j+1, sampletotal=len(data_loader),
                            stats=stats, bsz=bsz)

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
                    mode+'-loss': loss.item(),
                    mode+'-pt3dloss': ptloss.sum(),
                    mode+'-consisloss': consisloss.sum(),
                    mode+'-consiserr': consiserror.sum(),
                    mode+'-consiserrmax': consiserrormax.sum(),
                    mode+'-flowerrsum': flowerr_sum.sum()/bsz,
                    mode+'-flowerravg': flowerr_avg.sum()/bsz,
                    mode+'-motionerrsum': motionerr_sum.sum()/bsz,
                    mode+'-motionerravg': motionerr_avg.sum()/bsz,
                    mode+'-stillerrsum': stillerr_sum.sum() / bsz,
                    mode+'-stillerravg': stillerr_avg.sum() / bsz,
                    mode+'-maskconsissloss': maskconsisloss.sum(),
                }
                if mode == 'train':
                    info[mode+'-lr'] = args.curr_lr # Plot current learning rate
                for tag, value in info.items():
                    tblogger.scalar_summary(tag, value, iterct)

                # (2) Log images & print predicted SE3s
                if i % args.imgdisp_freq == 0:

                    ## Log the images (at a lower rate for now)
                    id = random.randint(0, sample['points'].size(0)-1)

                    # Render the predicted and GT poses onto the depth
                    depths = []
                    poses, transposes = [pose0, pose1], \
                                        [transpose1]
                    for k in xrange(args.seq_len+1):
                        gtpose    = sample['poses'][id, k]
                        predpose  = poses[k][id].cpu().float()
                        predposet = transposes[k-1][id].cpu().float() if (k > 0) else None
                        gtdepth   = helpers.normalize_img(sample['points'][id,k,2:].expand(3,args.img_ht,args.img_wd).permute(1,2,0), min=0, max=3)
                        cam_intrinsics = data_loader.data.dataset.datasets[0]['camera_intrinsics']
                        for n in xrange(args.num_se3):
                            # Pose_1 (GT/Pred)
                            if n < gtpose.size(0):
                                util.draw_3d_frame(gtdepth, gtpose[n], [0,0,1], cam_intrinsics, pixlength=15.0) # GT pose: Blue
                            util.draw_3d_frame(gtdepth, predpose[n], [0,1,0], cam_intrinsics, pixlength=15.0) # Pred pose: Green
                            if predposet is not None:
                                util.draw_3d_frame(gtdepth, predposet[n], [1,0,0], cam_intrinsics, pixlength=15.0)  # Transition model pred pose: Red
                        depths.append(gtdepth)
                    depthdisp = torch.cat(depths, 1).permute(2,0,1) # Concatenate along columns (3 x 240 x 320*seq_len+1 image)

                    # Concat the flows, depths and masks into one tensor
                    flowdisp  = torchvision.utils.make_grid(torch.cat([flows.narrow(0,id,1),
                                                                       predflows.narrow(0,id,1)], 0).cpu().view(-1, 3, args.img_ht, args.img_wd),
                                                            nrow=args.seq_len, normalize=True, range=(-0.01, 0.01))
                    #depthdisp = torchvision.utils.make_grid(sample['points'][id].narrow(1,2,1), normalize=True, range=(0.0,3.0))
                    maskdisp  = torchvision.utils.make_grid(torch.cat([mask0.narrow(0,id,1)], 0).cpu().view(-1, 1, args.img_ht, args.img_wd),
                                                            nrow=args.num_se3, normalize=True, range=(0,1))

                    # Display RGB
                    if args.use_xyzrgb:
                        rgbdisp = torchvision.utils.make_grid(sample['rgbs'][id].float().view(-1, 3, args.img_ht, args.img_wd),
                                                                nrow=args.seq_len, normalize=True, range=(0.0,255.0))

                    # Show as an image summary
                    info = { mode+'-depths': util.to_np(depthdisp.unsqueeze(0)),
                             mode+'-flows' : util.to_np(flowdisp.unsqueeze(0)),
                             mode+'-masks' : util.to_np(maskdisp.narrow(0,0,1))
                    }
                    if args.use_xyzrgb:
                        info[mode+'-rgbs'] = util.to_np(rgbdisp.unsqueeze(0)) # Optional RGB
                    for tag, images in info.items():
                        tblogger.image_summary(tag, images, iterct)

                    ## Print the predicted delta-SE3s
                    #deltase3s = deltapose01[id].view(args.num_se3, -1).cpu()
                    #print('\tPredicted delta-SE3s from t=0-1:', deltase3s)

                    ## Print the predicted mask values
                    print('\tPredicted mask stats:')
                    for k in xrange(args.num_se3):
                        print('\tMax: {:.4f}, Min: {:.4f}, Mean: {:.4f}, Std: {:.4f}, Median: {:.4f}, Pred 1: {}'.format(
                            mask0[id,k].max(), mask0[id,k].min(), mask0[id,k].mean(),
                            mask0[id,k].std(), mask0[id,k].view(-1).cpu().float().median(),
                            (mask0[id,k] - 1).abs().le(1e-5).sum()))
                    print('')

            # Measure viz time
            viz_time.update(time.time() - start)

    ### Print stats at the end
    print('========== Mode: {}, Epoch: {}, Final results =========='.format(mode, epoch))
    print_stats(mode, epoch=epoch, curr=num_iters, total=num_iters,
                samplecurr=data_loader.niters+1, sampletotal=len(data_loader),
                stats=stats)
    print('========================================================')

    # Return the loss & flow loss
    return stats

### Print statistics
def print_stats(mode, epoch, curr, total, samplecurr, sampletotal,
                stats, bsz=None):
    # Print loss
    bsz = args.batch_size if bsz is None else bsz
    print('Mode: {}, Epoch: [{}/{}], Iter: [{}/{}], Sample: [{}/{}], Batch size: {}, '
          'Loss: {loss.val:.4f} ({loss.avg:.4f}), '.format(
        mode, epoch, args.epochs, curr, total, samplecurr,
        sampletotal, bsz, loss=stats.loss))

    # Print flow loss per timestep
    for k in xrange(args.seq_len):
        print('\tStep: {}, Pt: {:.3f} ({:.3f}), '
              'Consis: {:.3f}/{:.4f} ({:.3f}/{:.4f}), '
              'Mask-Consis: {:.3f} ({:.4f}),'
              'Flow => Sum: {:.3f} ({:.3f}), Avg: {:.3f} ({:.3f}), '
              'Motion/Still => Sum: {:.3f}/{:.3f}, Avg: {:.3f}/{:.3f}'
            .format(
            1 + k * args.step_len,
            stats.ptloss.val[k], stats.ptloss.avg[k],
            stats.consisloss.val[k], stats.consisloss.avg[k],
            stats.consiserr.val[k], stats.consiserr.avg[k],
            stats.maskconsisloss.val[k], stats.maskconsisloss.avg[k],
            stats.flowerr_sum.val[k] / bsz, stats.flowerr_sum.avg[k] / bsz,
            stats.flowerr_avg.val[k] / bsz, stats.flowerr_avg.avg[k] / bsz,
            stats.motionerr_sum.avg[k] / bsz, stats.stillerr_sum.avg[k] / bsz,
            stats.motionerr_avg.avg[k] / bsz, stats.stillerr_avg.avg[k] / bsz,
        ))

### Adjust learning rate
def adjust_learning_rate(optimizer, epoch, decay_rate=0.1, decay_epochs=10, min_lr=1e-5):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (decay_rate ** (epoch // decay_epochs))
    lr = min_lr if (args.lr < min_lr) else lr # Clamp at min_lr
    print("======== Epoch: {}, Initial learning rate: {}, Current: {}, Min: {} =========".format(
        epoch, args.lr, lr, min_lr))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    args.curr_lr = lr

################ RUN MAIN
if __name__ == '__main__':
    main()
