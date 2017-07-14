import csv
import torch
from skimage import io, transform
import numpy as np
import cv2

############
### Helper functions for reading baxter data

# Read baxter state files
def read_baxter_state_file(filename):
    ret = {}
    with open(filename, 'rb') as csvfile:
        spamreader = csv.reader(csvfile, delimiter=' ', quoting=csv.QUOTE_NONNUMERIC)
        ret['actjtpos'] = torch.Tensor(spamreader.next()[0:-1]) # Last element is a string due to the way the file is created
        ret['actjtvel'] = torch.Tensor(spamreader.next()[0:-1])
        ret['actjteff'] = torch.Tensor(spamreader.next()[0:-1])
        ret['comjtpos'] = torch.Tensor(spamreader.next()[0:-1])
        ret['comjtvel'] = torch.Tensor(spamreader.next()[0:-1])
        ret['comjtacc'] = torch.Tensor(spamreader.next()[0:-1])
        ret['tarendeffpos'] = torch.Tensor(spamreader.next()[0:-1])
    return ret

# Read baxter SE3 state file for all the joints
def read_baxter_se3state_file(filename):
    # Read all the lines in the SE3-state file
    lines = []
    with open(filename, 'rb') as csvfile:
        spamreader = csv.reader(csvfile, delimiter=' ', quoting=csv.QUOTE_NONNUMERIC)
        for row in spamreader:
            if len(row)  == 0:
                continue
            if type(row[-1]) == str: # In case we have a string at the end of the list
                row = row[0:-1]
            lines.append(torch.Tensor(row))
    # Parse the SE3-states
    ret, ctr = {}, 0
    while (ctr < len(lines)):
        id      = int(lines[ctr][0])            # ID of mesh
        data    = lines[ctr+1].view(3,4)   # Transform data
        T       = torch.cat([data[0:3,1:4], data[0:3,0]], 1) # [R | t]
        ret[id] = T # Add to list of transforms
        ctr += 2    # Increment counter
    return ret

# Read baxter joint labels and their corresponding mesh index value
def read_baxter_labels_file(filename):
    ret = {}
    with open(filename, 'rb') as csvfile:
        spamreader     = csv.reader(csvfile, delimiter=' ')
        ret['frames']  = spamreader.next()[0:-1]
        ret['meshids'] = torch.IntTensor( [int(x) for x in spamreader.next()[0:-1]] )
    return ret

# Read baxter camera data file
def read_cameradata_file(filename):
    # Read lines in the file
    lines = []
    with open(filename, 'rb') as csvfile:
        spamreader = csv.reader(csvfile, delimiter=' ')
        for row in spamreader:
            lines.append([x for x in row if x != ''])
    # Compute modelview and camera parameter matrix
    ret = {}
    ret['modelview'] = torch.Tensor([float(x) for x in lines[1] + lines[2] + lines[3] + lines[4]]).view(4,4).clone()
    ret['camparam']  = torch.Tensor([float(x) for x in lines[6] + lines[7] + lines[8] + lines[9]]).view(4,4).clone()
    return ret

############
### Helper functions for reading image data

# Read depth image from disk
def read_depth_image(filename, ht = 240, wd = 320, scale = 1e-4):
    imgf = cv2.imread(filename, -1).astype(np.int16) * scale # Read image (unsigned short), convert to short & scale to get float
    if (imgf.shape[0] != int(ht) or imgf.shape[1] != int(wd)):
        imgscale = cv2.resize(imgf, (int(wd), int(ht)), interpolation=cv2.INTER_NEAREST) # Resize image with no interpolation (NN lookup)
    else:
        imgscale = imgf
    return torch.Tensor(imgscale).unsqueeze(0) # Add extra dimension

# Read flow image from disk
def read_flow_image_xyz(filename, ht = 240, wd = 320, scale = 1e-4):
    imgf = cv2.imread(filename, -1).astype(np.int16) * scale # Read image (unsigned short), convert to short & scale to get float
    if (imgf.shape[0] != int(ht) or imgf.shape[1] != int(wd)):
        imgscale = cv2.resize(imgf, (int(wd), int(ht)), interpolation=cv2.INTER_NEAREST) # Resize image with no interpolation (NN lookup)
    else:
        imgscale = imgf
    return torch.Tensor(imgscale.transpose((2,0,1))) # NOTE: OpenCV reads BGR so it's already xyz when it is read