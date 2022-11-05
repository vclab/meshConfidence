# python3 occlusion_analysis_joint_l.py --checkpoint=data/model_checkpoint.pt --dataset=3dpw
# This runs through whole dataset and moves the occlusion over each image and generates the occluded images and error per joint and as average
import time
import math
import torch
import argparse
import cv2
from models import hmr, SMPL
import config
from datasets import BaseDataset
import torch
from torch.utils.data import DataLoader
import numpy as np
import constants
from tqdm import tqdm
import matplotlib.pylab as plt
import os
from utils.imutils import crop
import itertools
from utils.geometry import perspective_projection
from utils.imutils import transform

def get_occluded_imgs(batch, occ_size, occ_pixel, dataset_name, joint_idx, log_freq, batch_idx):
    # Get the image batch find the ground truth joint location and occlude it. This file uses the ground truth 3d joint
    # positions and projects them.

    # Prepare the required parameters
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    camera_intrinsics = batch['camera_intrinsics'].to(device)
    camera_extrinsics = batch['camera_extrinsics'].to(device)
    joint_position = batch['joint_position'].to(device)
    joint_position = joint_position.reshape(-1, 24, 3)
    batch_size = joint_position.shape[0]
    # Preparing the regressor to map 24 3DPW keypoints on to 14 joints
    joint_mapper = [8, 5, 2, 1, 4, 7, 21, 19, 17,16, 18, 20, 12, 15]
    # Get 14 ground truth joints
    joint_position = joint_position[:, joint_mapper, :]

    # Project 3D keypoints to 2D keypoints
    # Homogenious real world coordinates X, P is the projection matrix
    P = torch.matmul(camera_intrinsics, camera_extrinsics).to(device)
    temp = torch.ones((batch_size, 14, 1)).double().to(device)
    X = torch.cat((joint_position, temp), 2)
    X = X.permute(0, 2, 1)
    p = torch.matmul(P, X)
    p = torch.div(p[:,:,:], p[:,2:3,:])
    p = p[:, [0,1], :]
    # Projected 2d coordinates on image p with the shape of (batch_size, 14, 2)
    p = p.permute(0, 2, 1).cpu().numpy()
    # Process 2d keypoints to match the processed images in the dataset
    center = batch['center'].to(device)
    scale = batch['scale'].to(device)
    res = [constants.IMG_RES, constants.IMG_RES]
    new_p = np.ones((batch_size,14,2))
    for i in range(batch_size):
        for j in range(p.shape[1]):
            temp = transform(p[i,j:j+1,:][0], center[i], scale[i], res, invert=0, rot=0)
            new_p[i,j,:] = temp
    # Occlude the Images at the joint position
    images = batch['img'].to(device)
    # new_p = new_p.cpu().numpy()
    occ_images = images.clone()
    img_size = int(images[0].shape[-1])
    for i in range(batch_size):
        h_start = int(max(new_p[i, joint_idx, 1] - occ_size/2, 0))
        w_start = int(max(new_p[i, joint_idx, 0] - occ_size/2, 0))
        h_end = min(img_size, h_start + occ_size)
        w_end = min(img_size, w_start + occ_size)
        occ_images[i,0,h_start:h_end, w_start:w_end] = (occ_pixel - 0.485)/0.229
        occ_images[i,1,h_start:h_end, w_start:w_end] = (occ_pixel - 0.456)/0.224
        occ_images[i,2,h_start:h_end, w_start:w_end] = (occ_pixel - 0.406)/0.225
    
    # store the data struct
    if batch_idx % (10*log_freq) == (10*log_freq) - 1:
        out_path = "occluded_images"
        if not os.path.isdir(out_path):
            os.makedirs(out_path)
        image = occ_images[0]
        # De-normalizing the image
        image = image * torch.tensor([0.229, 0.224, 0.225], device=image.device).reshape(1, 3, 1, 1)
        image = image + torch.tensor([0.485, 0.456, 0.406], device=image.device).reshape(1, 3, 1, 1)
        # Preparing the image for visualization
        img = image[0].permute(1,2,0).cpu().numpy().copy()
        img = 255 * img[:,:,::-1]
        cv2.imwrite(os.path.join(out_path, f'occluded_{joint_idx}_{batch_idx:05d}.jpg'), img)



    return occ_images


def run_dataset(args, joint_index):
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    # Load the dataloader
    dataset_name = args.dataset
    occ_size = args.occ_size
    occ_pixel = args.pixel
    dataset = BaseDataset(None, dataset_name, is_train=False)
    batch_size = args.batch_size
    data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=False)
    # MPJPE error for the non-parametric and parametric shapes
    mpjpe_org = np.zeros(len(dataset))
    log_freq = args.log_freq
    # Load the model
    model = hmr(config.SMPL_MEAN_PARAMS)
    checkpoint = torch.load(args.checkpoint, map_location=device)
    model.load_state_dict(checkpoint['model'], strict=False)
    model.eval()
    model.to(device)
    # Load SMPL model
    smpl_neutral = SMPL(config.SMPL_MODEL_DIR,
                        create_transl=False).to(device)
    smpl_male = SMPL(config.SMPL_MODEL_DIR,
                     gender='male',
                     create_transl=False).to(device)
    smpl_female = SMPL(config.SMPL_MODEL_DIR,
                       gender='female',
                       create_transl=False).to(device)
    # Regressor for H36m joints
    J_regressor = torch.from_numpy(np.load(config.JOINT_REGRESSOR_H36M)).float()
    joint_mapper_h36m = constants.H36M_TO_J17 if dataset_name == 'mpi-inf-3dhp' else constants.H36M_TO_J14
    joint_mapper_gt = constants.J24_TO_J17 if dataset_name == 'mpi-inf-3dhp' else constants.J24_TO_J14

    val_images_errors= []
    mpjpe = np.zeros(len(dataset))
    mpjpe_occluded = np.zeros(len(dataset))
    for batch_idx, batch in enumerate(tqdm(data_loader, desc='Eval', total=len(data_loader))):
        images = batch['img'].to(device)
        curr_batch_size = images.shape[0]
        # Get occluded images
        # joint_inx = args.joint
        joint_inx = joint_index
        occ_images = get_occluded_imgs(batch, occ_size, occ_pixel, dataset_name, joint_inx, log_freq, batch_idx)
        batch['img'] = occ_images
        error = get_error(batch, model, dataset_name, args, smpl_neutral, smpl_male, smpl_female, J_regressor, joint_mapper_h36m, joint_mapper_gt)
        mpjpe_occluded[batch_idx * batch_size:batch_idx * batch_size + curr_batch_size] = error

        # # Print intermediate results during evaluation
        # if batch_idx % log_freq == log_freq - 1:
        #     # print('MPJPE: ' + str(1000 * mpjpe[:batch_idx * batch_size].mean()))
        #     print('MPJPE_Occluded: ' + str(1000 * mpjpe_occluded[:batch_idx * batch_size].mean()))
        #     print()
    # Print final results during evaluation
    print('*** Final Results ***')
    # print()
    # print('mpjpe: ' + str(1000 * mpjpe.mean()))
    # print()
    print('mpjpe_occluded: ' + str(1000 * mpjpe_occluded.mean()))
    print()
    return 1000 * mpjpe_occluded.mean()

     


def get_error(batch, model, dataset_name, args, smpl_neutral, smpl_male, smpl_female,
                J_regressor, joint_mapper_h36m, joint_mapper_gt):
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    # Get ground truth annotations from the batch
    gt_pose = batch['pose'].to(device)
    gt_betas = batch['betas'].to(device)
    gt_vertices = smpl_neutral(betas=gt_betas, body_pose=gt_pose[:, 3:], global_orient=gt_pose[:, :3]).vertices
    images = batch['img'].to(device)
    gender = batch['gender'].to(device)
        
    with torch.no_grad():
        pred_rotmat, pred_betas, pred_camera = model(images)
        pred_output = smpl_neutral(betas=pred_betas, body_pose=pred_rotmat[:,1:], global_orient=pred_rotmat[:,0].unsqueeze(1), pose2rot=False)
        pred_vertices = pred_output.vertices
    # Regressor broadcasting
    J_regressor_batch = J_regressor[None, :].expand(pred_vertices.shape[0], -1, -1).to(device)
    # Get 14 ground truth joints
    if 'h36m' in dataset_name or 'mpi-inf' in dataset_name:
        gt_keypoints_3d = batch['pose_3d'].to(device)
        gt_keypoints_3d = gt_keypoints_3d[:, joint_mapper_gt, :-1]
    # For 3DPW get the 14 common joints from the rendered shape
    else:
        gt_vertices = smpl_male(global_orient=gt_pose[:,:3], body_pose=gt_pose[:,3:], betas=gt_betas).vertices 
        gt_vertices_female = smpl_female(global_orient=gt_pose[:,:3], body_pose=gt_pose[:,3:], betas=gt_betas).vertices 
        gt_vertices[gender==1, :, :] = gt_vertices_female[gender==1, :, :]
        gt_keypoints_3d = torch.matmul(J_regressor_batch, gt_vertices)
        gt_pelvis = gt_keypoints_3d[:, [0],:].clone()
        gt_keypoints_3d = gt_keypoints_3d[:, joint_mapper_h36m, :]
        gt_keypoints_3d = gt_keypoints_3d - gt_pelvis
    # Get 14 predicted joints from the mesh
    pred_keypoints_3d = torch.matmul(J_regressor_batch, pred_vertices)
    pred_pelvis = pred_keypoints_3d[:, [0],:].clone()
    pred_keypoints_3d = pred_keypoints_3d[:, joint_mapper_h36m, :]
    pred_keypoints_3d = pred_keypoints_3d - pred_pelvis 
    # Absolute error (MPJPE)
    # error = torch.sqrt(((pred_keypoints_3d - gt_keypoints_3d) ** 2).sum(dim=-1)).cpu().numpy()
    error = torch.sqrt(((pred_keypoints_3d - gt_keypoints_3d) ** 2).sum(dim=-1)).mean(dim=-1).cpu().numpy()
    return error
    
if __name__ == '__main__':
    start = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', default=None) # Path to network checkpoint
    parser.add_argument('--dataset', type=str, default='3dpw')  # Path of the input image
    parser.add_argument('--occ_size', type=int, default='40')  # Size of occluding window
    parser.add_argument('--pixel', type=int, default='1')  # Occluding window - pixel values
    parser.add_argument('--joint', type=int, default='13')  
    """ Joint index    joint_names = ['Right Ankle','Right Knee', 'Right Hip','Left Hip','Left Knee','Left Ankle','Right Wrist','Right Elbow',
                                                    'Right Shoulder', 'Left Shoulder', 'Left Elbow', 'Left Wrist', 'Neck', 'Top of Head']"""
    parser.add_argument('--batch_size', default=16) # Batch size for testing
    parser.add_argument('--log_freq', default=50, type=int) # Frequency of printing intermediate results
    args = parser.parse_args()
    mpjpe_occluded_list = []
    for i in range(3,9):
        joint_index = i
        mpjpe_occluded = run_dataset(args, joint_index)
        mpjpe_occluded_list.append(mpjpe_occluded)
    print(mpjpe_occluded_list)
    end = time.time()
    print("Time: ", end - start)