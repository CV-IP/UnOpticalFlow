# Author: Anurag Ranjan
# Copyright (c) 2019, Anurag Ranjan
# All rights reserved.
# based on github.com/ClementPinard/SfMLearner-Pytorch

import argparse
import time
import csv
import datetime
import os
import warnings

import numpy as np
import torch
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.optim
import torch.nn as nn
import torch.utils.data
import custom_transforms
import models
from utils import tensor2array, save_checkpoint
from inverse_warp import inverse_warp, inverse_warp_wmove, pose2flow, flow2oob, flow_warp, pose2flow_wmove
from loss_functions import *
from logger import TermLogger, AverageMeter
from path import Path
from itertools import chain
from tensorboardX import SummaryWriter
from flowutils.flowlib import flow_to_image


epsilon = 1e-8
warnings.filterwarnings('ignore')

parser = argparse.ArgumentParser(description='Occlusion Aware Unsupervised Learning of Optical Flow From Video',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--kitti-dir', dest='kitti_dir', type=str, default='kitti/kitti2015',
                    help='Path to kitti2015 scene flow dataset for optical flow validation')
parser.add_argument('--DEBUG', action='store_true', help='DEBUG Mode')
parser.add_argument('--name', dest='name', type=str, default='demo', required=True,
                    help='name of the experiment, checpoints are stored in checpoints/name')
parser.add_argument('--dataset-format', default='sequential', metavar='STR',
                    help='dataset format, stacked: stacked frames (from original TensorFlow code) \
                    sequential: sequential folders (easier to convert to with a non KITTI/Cityscape dataset')
parser.add_argument('--sequence-length', type=int, metavar='N', help='sequence length for training', default=3)
parser.add_argument('--rotation-mode', type=str, choices=['euler', 'quat'], default='euler',
                    help='rotation mode for PoseExpnet : euler (yaw,pitch,roll) or quaternion (last 3 coefficients)')
parser.add_argument('--padding-mode', type=str, choices=['zeros', 'border'], default='zeros',
                    help='padding mode for image warping : this is important for photometric differenciation when going outside target image.'
                         ' zeros will null gradients outside target image.'
                         ' border will only null gradients of the coordinate outside (x or y)')
parser.add_argument('--with-depth-gt', action='store_true', help='use ground truth for depth validation. \
                    You need to store it in npy 2D arrays see data/kitti_raw_loader.py for an example')
parser.add_argument('--with-flow-gt', action='store_true', help='use ground truth for flow validation. \
                    see data/validation_flow for an example')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers')
parser.add_argument('--epochs', default=200, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--epoch-size', default=0, type=int, metavar='N',
                    help='manual epoch size (will match dataset size if not set)')
parser.add_argument('-b', '--batch-size', default=4, type=int,
                    metavar='N', help='mini-batch size')
parser.add_argument('--lr', '--learning-rate', default=2e-4, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum for sgd, alpha parameter for adam')
parser.add_argument('--beta', default=0.999, type=float, metavar='M',
                    help='beta parameters for adam')
parser.add_argument('--weight-decay', '--wd', default=0, type=float,
                    metavar='W', help='weight decay')
parser.add_argument('--print-freq', default=10, type=int,
                    metavar='N', help='print frequency')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--smoothness-type', dest='smoothness_type', type=str, default='edgeaware', choices=['edgeaware', 'regular'],
                    help='edgeaware regular')
parser.add_argument('--data-normalization', dest='data_normalization', type=str, default='global', choices=['local', 'global'],
                    help='Compute mean-std locally or globally')
parser.add_argument('--nlevels', dest='nlevels', type=int, default=1,
                    help='number of levels in multiscale. Options: 6')
parser.add_argument('--flownet', dest='flownet', type=str, default='FlowNetS', choices=['Back2Future', 'FlowNetC6','UnFlow','Back2FutureS'],
                    help='flow network architecture. Options: FlowNetC6 | Back2Future')
parser.add_argument('--pretrained-flow', dest='pretrained_flow', default=None, metavar='PATH',
                    help='path to pre-trained Flow net model')

parser.add_argument('--spatial-normalize', dest='spatial_normalize', action='store_true', help='spatially normalize depth maps')
parser.add_argument('--robust', dest='robust', action='store_true', help='train using robust losses')
parser.add_argument('--min', dest='min', action='store_true', help='train using min losses')
parser.add_argument('--no-non-rigid-mask', dest='no_non_rigid_mask', action='store_true', help='will not use mask on loss of non-rigid flow')
parser.add_argument('--joint-mask-for-depth', dest='joint_mask_for_depth', action='store_true', help='use joint mask from masknet and consensus mask for depth training')

parser.add_argument('--fix-flownet', dest='fix_flownet', action='store_true', help='do not train flownet')

parser.add_argument('--alternating', dest='alternating', action='store_true', help='minimize only one network at a time')
parser.add_argument('--clamp-masks', dest='clamp_masks', action='store_true', help='threshold masks for training')
parser.add_argument('--fix-posemasknet', dest='fix_posemasknet', action='store_true', help='fix pose and masknet')
parser.add_argument('--seed', default=0, type=int, help='seed for random functions, and network initialization')
parser.add_argument('--log-summary', default='progress_log_summary.csv', metavar='PATH',
                    help='csv where to save per-epoch train and valid stats')
parser.add_argument('--log-full', default='progress_log_full.csv', metavar='PATH',
                    help='csv where to save per-gradient descent train stats')
parser.add_argument('-qch', '--qch', type=float, help='q value for charbonneir', metavar='W', default=0.5)
parser.add_argument('-wrig', '--wrig', type=float, help='consensus imbalance weight', metavar='W', default=1.0)
parser.add_argument('-wbce', '--wbce', type=float, help='weight for binary cross entropy loss', metavar='W', default=0.5)
parser.add_argument('-wssim', '--wssim', type=float, help='weight for ssim loss', metavar='W', default=0.0)
parser.add_argument('-wconsis', '--wconsis', type=float, help='weight for consistancy loss', metavar='W', default=0.0)
parser.add_argument('-pc', '--cam-photo-loss-weight', type=float, help='weight for camera photometric loss for rigid pixels', metavar='W', default=1)
parser.add_argument('-pf1st', '--flow-photo-loss-weight-first', type=float, help='weight for photometric loss for non rigid optical flow first order', metavar='W', default=0.03)
parser.add_argument('-pf2nd', '--flow-photo-loss-weight-second', type=float, help='weight for photometric loss for non rigid optical flow second order', metavar='W', default=3.0)

parser.add_argument('-cv', '--velocity-consis-loss-weight', type=float, help='weight for cv loss for non rigid optical flow', metavar='W', default=1)
parser.add_argument('-m', '--mask-loss-weight', type=float, help='weight for explainabilty mask loss', metavar='W', default=0)

parser.add_argument('-s1st', '--smooth-loss-weight-first', type=float, help='weight for disparity smoothness loss first order', metavar='W', default=0.1)
parser.add_argument('-s2nd', '--smooth-loss-weight-second', type=float, help='weight for disparity smoothness loss second order', metavar='W', default=0.1)

parser.add_argument('-dc', '--depth-consis-weight', type=float, help='weight for disparity smoothness loss', metavar='W', default=0.1)
parser.add_argument('-dfc', '--consensus-loss-weight', type=float, help='weight for mask consistancy', metavar='W', default=0.1)
parser.add_argument('-a', '--alpha', type=float, help='weight for edge aware', metavar='W', default=10)
parser.add_argument('-epi', '--epipolar-loss-weight', type=float, help='weight for mask consistancy', metavar='W', default=0.01)
parser.add_argument('-tri', '--triangulation-loss-weight', type=float, help='weight for mask consistancy', metavar='W', default=0.01)
parser.add_argument('--THRESH', '--THRESH', type=float, help='threshold for masks', metavar='W', default=0.01)
parser.add_argument('--lambda-oob', type=float, help='weight on the out of bound pixels', default=0)
parser.add_argument('--log-output', action='store_true', help='will log dispnet outputs and warped imgs at validation step')
parser.add_argument('--log-terminal', action='store_true', help='will display progressbar at terminal')
parser.add_argument('--resume', action='store_true', help='resume from checkpoint')
parser.add_argument('-f', '--training-output-freq', type=int, help='frequence for outputting dispnet outputs and warped imgs at training for all scales if 0 will not output',
                    metavar='N', default=0)
parser.add_argument('--with-mask', type=bool, metavar='W', default=True, choices=[True, False],
                    help='with the the mask for moving objects and occlusions or not')
parser.add_argument('--with-ssim', type=bool, metavar='W', default=True, choices=[True, False],
                    help='with the the mask for moving objects and occlusions or not')

best_error = -1
n_iter = 0


def main():
    global args, best_error, n_iter
    args = parser.parse_args()
    if args.dataset_format == 'stacked':
        from datasets.stacked_sequence_folders import SequenceFolder
    elif args.dataset_format == 'sequential':
        from datasets.sequence_folders import SequenceFolder
    save_path = Path(args.name)
    args.save_path = 'checkpoints'/save_path #/timestamp
    print('=> will save everything to {}'.format(args.save_path))
    args.save_path.makedirs_p()
    torch.manual_seed(args.seed)
    if args.alternating:
        args.alternating_flags = np.array([False,False,True])

    training_writer = SummaryWriter(args.save_path)
    output_writers = []
    if args.log_output:
        for i in range(3):
            output_writers.append(SummaryWriter(args.save_path/'valid'/str(i)))

    # Data loading code
    flow_loader_h, flow_loader_w = 256, 832

    if args.data_normalization =='global':
        normalize = custom_transforms.Normalize(mean=[0.5, 0.5, 0.5],
                                                std=[0.5, 0.5, 0.5])
    elif args.data_normalization =='local':
        normalize = custom_transforms.NormalizeLocally()


    train_transform = custom_transforms.Compose([
        custom_transforms.RandomHorizontalFlip(),
        custom_transforms.RandomScaleCrop(),
        custom_transforms.ArrayToTensor(),
        normalize
    ])
 

    valid_transform = custom_transforms.Compose([custom_transforms.ArrayToTensor(), normalize])

    valid_flow_transform = custom_transforms.Compose([custom_transforms.Scale(h=flow_loader_h, w=flow_loader_w),
                            custom_transforms.ArrayToTensor(), normalize])

    print("=> fetching scenes in '{}'".format(args.data))
    train_set = SequenceFolder(
        args.data,
        transform=train_transform,
        seed=args.seed,
        train=True,
        sequence_length=args.sequence_length
    )

    # if no Groundtruth is avalaible, Validation set is the same type as training set to measure photometric loss from warping
    
    val_set = SequenceFolder(
        args.data,
        transform=valid_transform,
        seed=args.seed,
        train=False,
        sequence_length=args.sequence_length,
    )

    if args.with_flow_gt:
        from datasets.validation_flow import ValidationFlow
        val_flow_set = ValidationFlow(root=args.kitti_dir,
                                        sequence_length=args.sequence_length, transform=valid_flow_transform)

    if args.DEBUG:
        train_set.__len__ = 32
        train_set.samples = train_set.samples[:32]

    print('{} samples found in {} train scenes'.format(len(train_set), len(train_set.scenes)))
    print('{} samples found in {} valid scenes'.format(len(val_set), len(val_set.scenes)))
    train_loader = torch.utils.data.DataLoader(
        train_set, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True, drop_last=True)
    val_loader = torch.utils.data.DataLoader(
        val_set, batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True, drop_last=True)

    if args.with_flow_gt:
        val_flow_loader = torch.utils.data.DataLoader(val_flow_set, batch_size=1,               # batch size is 1 since images in kitti have different sizes
                        shuffle=False, num_workers=args.workers, pin_memory=True, drop_last=True)

    if args.epoch_size == 0:
        args.epoch_size = len(train_loader)

    # create model
    print("=> creating model")
    
    if args.flownet=='SpyNet':
        flow_net = getattr(models, args.flownet)(nlevels=args.nlevels, pre_normalization=normalize).cuda()
    else:
        flow_net = getattr(models, args.flownet)(nlevels=args.nlevels).cuda()

    # load pre-trained weights

    if args.pretrained_flow:
        print("=> using pre-trained weights for FlowNet")
        weights = torch.load(args.pretrained_flow)
        flow_net.load_state_dict(weights['state_dict'])
    # else:
        #flow_net.init_weights()


    if args.resume:
        print("=> resuming from checkpoint")  
        flownet_weights = torch.load(args.save_path/'flownet_checkpoint.pth.tar')
        flow_net.load_state_dict(flownet_weights['state_dict'])


    # import ipdb; ipdb.set_trace()
    cudnn.benchmark = True
    flow_net = torch.nn.DataParallel(flow_net)

    print('=> setting adam solver')
    parameters = chain(flow_net.parameters())
    optimizer = torch.optim.Adam(parameters, args.lr,
                                 betas=(args.momentum, args.beta),
                                 weight_decay=args.weight_decay)

    milestones = [300]
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones, gamma=0.1, last_epoch=-1)

    if args.min:
        print("using min method")

    if args.resume and (args.save_path/'optimizer_checkpoint.pth.tar').exists():
        print("=> loading optimizer from checkpoint")
        optimizer_weights = torch.load(args.save_path/'optimizer_checkpoint.pth.tar')
        optimizer.load_state_dict(optimizer_weights['state_dict'])

    with open(args.save_path/args.log_summary, 'w') as csvfile:
        writer = csv.writer(csvfile, delimiter='\t')
        writer.writerow(['train_loss', 'validation_loss'])

    with open(args.save_path/args.log_full, 'w') as csvfile:
        writer = csv.writer(csvfile, delimiter='\t')
        writer.writerow(['train_loss', 'photo_cam_loss', 'photo_flow_loss', 'explainability_loss', 'smooth_loss'])

    if args.log_terminal:
        logger = TermLogger(n_epochs=args.epochs, train_size=min(len(train_loader), args.epoch_size), valid_size=len(val_loader))
        logger.epoch_bar.start()
    else:
        logger=None

    for epoch in range(args.epochs):
        scheduler.step()

        if args.fix_flownet:
            for fparams in flow_net.parameters():
                fparams.requires_grad = False

        if args.log_terminal:
            logger.epoch_bar.update(epoch)
            logger.reset_train_bar()

        # train for one epoch
        train_loss = train(train_loader, flow_net, optimizer, args.epoch_size, logger, training_writer)

        if args.log_terminal:
            logger.train_writer.write(' * Avg Loss : {:.3f}'.format(train_loss))
            logger.reset_valid_bar()


        if args.with_flow_gt:
            flow_errors, flow_error_names = validate_flow_with_gt(val_flow_loader, flow_net, epoch, logger, output_writers)

            error_string = ', '.join('{} : {:.3f}'.format(name, error) for name, error in zip(flow_error_names, flow_errors))

            if args.log_terminal:
                logger.valid_writer.write(' * Avg {}'.format(error_string))
            else:
                print('Epoch {} completed'.format(epoch))

            for error, name in zip(flow_errors, flow_error_names):
                training_writer.add_scalar(name, error, epoch)

        
        decisive_error = flow_errors[0]
        if best_error < 0:
            best_error = decisive_error

        # remember lowest error and save checkpoint
        is_best = decisive_error <= best_error
        best_error = min(best_error, decisive_error)
        save_checkpoint(
            args.save_path, {
                'epoch': epoch + 1,
                'state_dict': flow_net.module.state_dict()
            }, {
                'epoch': epoch + 1,
                'state_dict': optimizer.state_dict()
            },
            is_best)

        with open(args.save_path/args.log_summary, 'a') as csvfile:
            writer = csv.writer(csvfile, delimiter='\t')
            writer.writerow([train_loss, decisive_error])
    if args.log_terminal:
        logger.epoch_bar.finish()


def train(train_loader, flow_net, optimizer, epoch_size, logger=None, train_writer=None):
    global args, n_iter
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter(precision=4)

    # switch to train mode
    flow_net.train()

    end = time.time()

    for i, (tgt_img, ref_imgs, intrinsics, intrinsics_inv) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)
        tgt_img_var = Variable(tgt_img.cuda())
        ref_imgs_var = [Variable(img.cuda()) for img in ref_imgs]

        if args.flownet == 'Back2Future':
            flow_fwd, flow_bwd = flow_net(tgt_img_var, ref_imgs_var)
        else:
            flow_fwd = flow_net(tgt_img_var, ref_imgs_var[1])
            flow_bwd = flow_net(tgt_img_var, ref_imgs_var[0])
            
        loss_smooth = torch.zeros(1).cuda() 
        loss_flow_recon = torch.zeros(1).cuda()
        loss_velocity_consis = torch.zeros(1).cuda()

        if args.flow_photo_loss_weight_first: 
            if args.min:
                loss_flow_recon += args.flow_photo_loss_weight_first*photometric_flow_min_loss(tgt_img_var, ref_imgs_var, [flow_bwd, flow_fwd],
                                                lambda_oob=args.lambda_oob, qch=args.qch, wssim=args.wssim)
            else:
                loss_flow_recon += args.flow_photo_loss_weight_first*photometric_flow_loss(tgt_img_var, ref_imgs_var, [flow_bwd, flow_fwd],
                                                lambda_oob=args.lambda_oob, qch=args.qch, wssim=args.wssim)

        if args.flow_photo_loss_weight_second: 
            if args.min:
                
                loss_per, loss_weight= photometric_flow_gradient_min_loss(tgt_img_var, ref_imgs_var, [flow_bwd, flow_fwd],
                                                lambda_oob=args.lambda_oob, qch=args.qch, wssim=args.wssim)
                loss_flow_recon += args.flow_photo_loss_weight_second * loss_per
            else:
                loss_flow_recon += args.flow_photo_loss_weight_second*photometric_flow_gradient_loss(tgt_img_var, ref_imgs_var, [flow_bwd, flow_fwd],
                                                lambda_oob=args.lambda_oob, qch=args.qch, wssim=args.wssim)
            

        if args.smooth_loss_weight_first:
            if args.smoothness_type == "regular":
                loss_smooth += args.smooth_loss_weight_first*(smooth_loss(flow_fwd) + smooth_loss(flow_bwd))
            elif args.smoothness_type == "edgeaware":
                loss_smooth += args.smooth_loss_weight_first*(edge_aware_smoothness_loss(tgt_img_var, flow_fwd)+edge_aware_smoothness_loss(tgt_img_var, flow_bwd))

        if args.smooth_loss_weight_second:
            if args.smoothness_type == "regular":
                loss_smooth += args.smooth_loss_weight_second*(smooth_loss(flow_fwd) + smooth_loss(flow_bwd))
            elif args.smoothness_type == "edgeaware":
                loss_smooth = args.smooth_loss_weight_second*(edge_aware_smoothness_second_order_loss_change_weight(tgt_img_var, flow_bwd, args.alpha)\
                    + edge_aware_smoothness_second_order_loss_change_weight(tgt_img_var, flow_fwd, args.alpha))


        if args.velocity_consis_loss_weight:
            loss_velocity_consis = args.velocity_consis_loss_weight*flow_velocity_consis_loss( [flow_bwd, flow_fwd])


        loss = loss_smooth + loss_flow_recon + loss_velocity_consis
        
        if i > 0 and n_iter % args.print_freq == 0:
            train_writer.add_scalar('flow_photometric_error', loss_flow_recon.item(), n_iter)
            train_writer.add_scalar('flow_smoothness_loss', loss_smooth.item(), n_iter)
            train_writer.add_scalar('velocity_consis_loss', loss_velocity_consis.item(), n_iter)
            train_writer.add_scalar('total_loss', loss.item(), n_iter)


        if args.training_output_freq > 0 and n_iter % args.training_output_freq == 0:

            train_writer.add_image('train Input', tensor2array(tgt_img[0]), n_iter)
            train_writer.add_image('train Flow FWD Output',flow_to_image(tensor2array(flow_fwd[0].data[0].cpu())) , n_iter )
            train_writer.add_image('train Flow BWD Output',flow_to_image(tensor2array(flow_bwd[0].data[0].cpu())) , n_iter )

            loss_weight_bwd = loss_weight[0][0,0,:,:].unsqueeze(0)
            loss_weight_fwd = loss_weight[0][0,1,:,:].unsqueeze(0)

            train_writer.add_image('loss_weight_bwd', tensor2array(loss_weight_bwd.data[0].cpu(), max_value=None, colormap='bone'), n_iter)
            train_writer.add_image('loss_weight_fwd', tensor2array(loss_weight_fwd.data[0].cpu(), max_value=None, colormap='bone'), n_iter)


            train_writer.add_image('train Flow FWD error Image',tensor2array(flow_warp(tgt_img_var-ref_imgs_var[1],flow_fwd[0]).data[0].cpu()) , n_iter )
            train_writer.add_image('train Flow BWD error Image',tensor2array(flow_warp(tgt_img_var-ref_imgs_var[0],flow_bwd[0]).data[0].cpu()) , n_iter )

            
        # record loss and EPE
        losses.update(loss.item(), args.batch_size)

        # compute gradient and do Adam step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if args.log_terminal:
            logger.train_bar.update(i+1)
            if i % args.print_freq == 0:
                logger.train_writer.write('Train: Time {} Data {} Loss {}'.format(batch_time, data_time, losses))
        if i >= epoch_size - 1:
            break

        n_iter += 1

    return losses.avg[0]



def validate_flow_with_gt(val_loader, flow_net, epoch, logger, output_writers=[]):
    global args
    batch_time = AverageMeter()
    error_names = ['epe_total', 'epe_rigid', 'epe_non_rigid', 'outliers']
    errors = AverageMeter(i=len(error_names))
    log_outputs = len(output_writers) > 0

    # switch to evaluate mode
    flow_net.eval()

    end = time.time()

    for i, (tgt_img, ref_imgs, intrinsics, intrinsics_inv, flow_gt, obj_map_gt) in enumerate(val_loader):
        tgt_img_var = Variable(tgt_img.cuda(), volatile=True)
        ref_imgs_var = [Variable(img.cuda(), volatile=True) for img in ref_imgs]

        flow_gt_var = Variable(flow_gt.cuda(), volatile=True)
        obj_map_gt_var = Variable(obj_map_gt.cuda(), volatile=True)

        if args.flownet == 'Back2Future':
            flow_fwd, flow_bwd= flow_net(tgt_img_var, ref_imgs_var)
        else:
            flow_fwd = flow_net(tgt_img_var, ref_imgs_var[1])
            flow_bwd = flow_net(tgt_img_var, ref_imgs_var[0])


        if args.DEBUG:
            flow_fwd_x = flow_fwd[:,0].view(-1).abs().data
            flow_gt_var_x = flow_gt_var[:,0].view(-1).abs().data
       
        flow_fwd_non_rigid =  flow_fwd
        total_flow = flow_fwd

        obj_map_gt_var_expanded = obj_map_gt_var.unsqueeze(1).type_as(flow_fwd)

        if log_outputs and i % 10 == 0 and i/10 < len(output_writers):
            index = int(i//10)
            if epoch == 0:
                output_writers[index].add_image('val flow Input', tensor2array(tgt_img[0]), 0)
                flow_to_show = flow_gt[0][:2,:,:].cpu()
                output_writers[index].add_image('val target Flow', flow_to_image(tensor2array(flow_to_show)), epoch)

            output_writers[index].add_image('val Non-rigid Flow Output', flow_to_image(tensor2array(flow_fwd_non_rigid.data[0].cpu())), epoch)
            
        if np.isnan(flow_gt.sum().item()) or np.isnan(total_flow.data.sum().item()):
            print('NaN encountered')
        _epe_errors = compute_all_epes(flow_gt_var, flow_fwd, flow_fwd, (1-obj_map_gt_var_expanded) )
        errors.update(_epe_errors)

        batch_time.update(time.time() - end)
        end = time.time()
        if args.log_terminal:
            logger.valid_bar.update(i)
            if i % args.print_freq == 0:
                logger.valid_writer.write('valid: Time {} Abs Error {:.4f} ({:.4f})'.format(batch_time, errors.val[0], errors.avg[0]))

    if args.log_terminal:
        logger.valid_bar.update(len(val_loader))

    return errors.avg, error_names





if __name__ == '__main__':
    import sys
    with open("experiment_recorder.md", "a") as f:
        f.write('\n python3 ' + ' '.join(sys.argv))
    main()
