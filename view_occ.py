# Author: Anurag Ranjan
# Copyright (c) 2018, Max Planck Society 

import argparse
import torch
import torch.nn as nn
from path import Path
from torch.autograd import Variable
from torchvision.transforms import ToTensor

from scipy.misc import imread, imresize
from tqdm import tqdm
import numpy as np
import models
import custom_transforms
from flowutils import flow_io
from loss_functions import  *

parser = argparse.ArgumentParser(description='Code to test performace of Back2Future models on KITTI benchmarks',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--flownet', dest='flownet', type=str, default='Back2Future', choices=['Back2Future','Back2FutureS','FlowNetC6'],
                    help='flow network architecture. Options: FlowNetS | SpyNet')
parser.add_argument('--pretrained-flow', dest='pretrained_flow', default=None, metavar='PATH',
                    help='path to pre-trained Flow net model')
parser.add_argument('--kitti-dir', dest='kitti_dir', default=None, metavar='PATH',
                    help='path to KITTI 2015 directory')


def main():
    global args
    normalize = Normalize(mean=[0.5, 0.5, 0.5],
                                            std=[0.5, 0.5, 0.5])
    args = parser.parse_args()
    flow_loader_h, flow_loader_w = 256, 832
    # valid_flow_transform = Scale(h=flow_loader_h, w=flow_loader_w)

    valid_flow_transform = Compose([Scale(h=flow_loader_h, w=flow_loader_w),
                            ArrayToTensor(), normalize])

    val_flow_set = KITTI2015(root=args.kitti_dir,
                                transform=valid_flow_transform)

    val_flow_loader = torch.utils.data.DataLoader(val_flow_set, batch_size=1, shuffle=False,
        num_workers=2, pin_memory=True)

    flow_net = getattr(models, args.flownet)(nlevels=6).cuda()
    if args.pretrained_flow:
        print("=> using pre-trained weights from {}".format(args.pretrained_flow))
        weights = torch.load(args.pretrained_flow)
        flow_net.load_state_dict(weights['state_dict'])#, strict=False)
    flow_net.eval()
    error_names = ['epe_total']
    errors = AverageMeter(i=len(error_names))

    for i, (tgt_img, ref_imgs, flow_gt) in enumerate(tqdm(val_flow_loader)):
        tgt_img_var = Variable(tgt_img.cuda(), volatile=True)
        ref_imgs_var = [Variable(img.cuda(), volatile=True) for img in ref_imgs]
        flow_gt_var = Variable(flow_gt.cuda(), volatile=True)

        # compute output
        flow_fwd = flow_net(tgt_img_var, ref_imgs_var[1])
        epe = compute_epe(gt=flow_gt_var, pred=flow_fwd[0].unsqueeze(0))
        errors.update(epe)

    print("Averge EPE",errors.avg )

class Compose(object):
    def __init__(self, transforms):
        self.transforms = transforms

    def __call__(self, images):
        for t in self.transforms:
            images = t(images)
        return images

class Scale(object):
    """Scales images to a particular size"""
    def __init__(self, h, w):
        self.h = h
        self.w = w

    def __call__(self, images):

        in_h, in_w, _ = images[0].shape
        scaled_h, scaled_w = self.h , self.w
        scaled_images = [imresize(im, (scaled_h, scaled_w)) for im in images]

        return scaled_images


class Normalize(object):
    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def __call__(self, images):
        for tensor in images:
            for t, m, s in zip(tensor, self.mean, self.std):
                t.sub_(m).div_(s)
        return images

class ArrayToTensor(object):
    """Converts a list of numpy.ndarray (H x W x C) along with a intrinsics matrix to a list of torch.FloatTensor of shape (C x H x W) with a intrinsics tensor."""

    def __call__(self, images):
        tensors = []
        for im in images:
            # put it from HWC to CHW format
            im = np.transpose(im, (2, 0, 1))
            # handle numpy array
            tensors.append(torch.from_numpy(im).float()/255)
        return tensors

class KITTI2015(torch.utils.data.Dataset):
    """
        Kitti 2015 loader
    """

    def __init__(self, root, transform=None, N=200, train=True, seed=0):
        self.root = Path(root)
        self.scenes = range(N)
        self.N = N
        self.transform = transform
        self.phase = 'training' if train else 'testing'
        self.seq_ids = [9, 11]

    def __getitem__(self, index):
        tgt_img_path =  self.root.joinpath('data_scene_flow_multiview', self.phase, 'image_2',str(index).zfill(6)+'_10.png')
        ref_img_paths = [self.root.joinpath('data_scene_flow_multiview', self.phase, 'image_2',str(index).zfill(6)+'_'+str(k).zfill(2)+'.png') for k in self.seq_ids]
        gt_flow_path = self.root.joinpath('data_scene_flow', self.phase, 'flow_occ', str(index).zfill(6)+'_10.png')

        tgt_img = load_as_float(tgt_img_path)
        ref_imgs = [load_as_float(ref_img) for ref_img in ref_img_paths]

        u,v,valid = flow_io.flow_read_png(gt_flow_path)
        gtFlow = np.dstack((u,v,valid))
        gtFlow = torch.FloatTensor(gtFlow.transpose(2,0,1))

        if self.transform is not None:
            imgs = self.transform([tgt_img] + ref_imgs)
            tgt_img = imgs[0]
            ref_imgs = imgs[1:]

        return tgt_img, ref_imgs, gtFlow

    def __len__(self):
        return self.N


# class Scale(object):
#     """Scales images to a particular size"""
#     def __init__(self, h, w):
#         self.h = h
#         self.w = w

#     def __call__(self, images):
#         in_h, in_w, _ = images[0].shape
#         scaled_h, scaled_w = self.h , self.w
#         scaled_images = [ToTensor()(imresize(im, (scaled_h, scaled_w))) for im in images]
#         return scaled_images

def compute_occ_photometric_error(flow, tgt_img, ref_imgs, occ_mask):
    
    return occ_photometric, noc_photometric

def compute_epe(gt, pred):
    # print(pred.size())
    _, _, h_pred, w_pred = pred.size()
    bs, nc, h_gt, w_gt = gt.size()

    u_gt, v_gt = gt[:,0,:,:], gt[:,1,:,:]
    pred = nn.functional.upsample(pred, size=(h_gt, w_gt), mode='bilinear')
    u_pred = pred[:,0,:,:] * (w_gt/w_pred)
    v_pred = pred[:,1,:,:] * (h_gt/h_pred)

    epe = torch.sqrt(torch.pow((u_gt - u_pred), 2) + torch.pow((v_gt - v_pred), 2))

    if nc == 3:
        valid = gt[:,2,:,:]
        epe = epe * valid
        avg_epe = epe.sum()/(valid.sum() + 1e-6)
    else:
        avg_epe = epe.sum()/(bs*h_gt*w_gt)

    if type(avg_epe) == Variable: avg_epe = avg_epe.data
    return avg_epe.item()

def load_as_float(path):
    return imread(path).astype(np.float32)

class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, i=1, precision=3):
        self.meters = i
        self.precision = precision
        self.reset(self.meters)

    def reset(self, i):
        self.val = [0]*i
        self.avg = [0]*i
        self.sum = [0]*i
        self.count = 0

    def update(self, val, n=1):
        if not isinstance(val, list):
            val = [val]
        assert(len(val) == self.meters)
        self.count += n
        for i,v in enumerate(val):
            self.val[i] = v
            self.sum[i] += v * n
            self.avg[i] = self.sum[i] / self.count

    def __repr__(self):
        val = ' '.join(['{:.{}f}'.format(v, self.precision) for v in self.val])
        avg = ' '.join(['{:.{}f}'.format(a, self.precision) for a in self.avg])
        return '{} ({})'.format(val, avg)

if __name__ == '__main__':
    main()