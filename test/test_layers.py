import torch
from torch.autograd import Variable
import torch.nn as nn

#################################################################
## RT INVERSE
def RtInverse(input):
	# Check dimensions
	bsz, nse3, nrows, ncols = input.size()
	assert (nrows == 3 and ncols == 4);
	# Init for FWD pass
	input_v = input.view(-1, 3, 4);
	r = input_v.narrow(2, 0, 3);
	t = input_v.narrow(2, 3, 1);
	# Compute output = [r^T -r^T * t]
	r_o = r.transpose(1,2);
	t_o = torch.bmm(r_o, t).mul_(-1);
	return torch.cat([r_o, t_o],2).view_as(input).contiguous();

def RtInverse_bwd(input, grad_output):
	input_v = input.view(-1,3,4);
	grad_v  = grad_output.view(-1,3,4)
	r = input_v.narrow(2, 0, 3);
	t = input_v.narrow(2, 3, 1);
	ro_g = grad_v.narrow(2, 0, 3);
	to_g = grad_v.narrow(2, 3, 1);
	ri_g = torch.bmm(t,to_g.transpose(1,2)).mul_(-1).add_(ro_g.transpose(1,2))
	ti_g = torch.bmm(r,to_g).mul_(-1)
	return torch.cat([ri_g, ti_g],2).view_as(input).contiguous();

### Test RT Inverse
# Input/Target
input1 = Variable(torch.rand(2,2,3,4), requires_grad=True)
target = Variable(torch.rand(2,2,3,4))

# Auto-grad
output = RtInverse(input1)
err    = nn.MSELoss()(output,target)
err.backward()
gauto = input1.grad.clone()

# Analytical grad
input1.grad.data.zero_();
from layers.RtInverse import RtInverse as RtInverseA
output1 = RtInverseA()(input1);
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = input1.grad.clone()

'''
# Analytical grad
output1 = Variable(output.data.clone(), requires_grad=True)
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = RtInverse_bwd(input1.clone(), output1.grad.clone())
'''

# Compare
diff = gauto - ganalytical
print("{}, {}, {}".format(diff.data.max(),diff.data.min(),diff.data.abs().view(-1).median(0)[0][0]));

# Grad-Check
from torch.autograd import gradcheck
Rt = RtInverseA();
torch.set_default_tensor_type('torch.DoubleTensor')
input1 = Variable(torch.rand(2,2,3,4), requires_grad=True)
assert(gradcheck(Rt, [input1]));

#################################################################
## COMPOSE RT PAIR
def ComposeRtPair(A, B):
	# Check dimensions
	_, _, num_rows, num_cols = A.size()
	assert (num_rows == 3 and num_cols == 4);
	assert (A.is_same_size(B));

	# Init for FWD pass
	Av = A.view(-1,3,4);
	Bv = B.view(-1,3,4);
	rA, rB = Av.narrow(2,0,3), Bv.narrow(2,0,3);
	tA, tB = Av.narrow(2,3,1), Bv.narrow(2,3,1);

	# Compute output
	r  = torch.bmm(rA, rB);
	t  = torch.baddbmm(tA, rA, tB);
	return torch.cat([r,t],2).view_as(A).contiguous()

def ComposeRtPair_bwd(A,B,grad):
	# Init for FWD pass
	Av = A.view(-1, 3, 4);
	Bv = B.view(-1, 3, 4);
	rA, rB = Av.narrow(2, 0, 3), Bv.narrow(2, 0, 3);
	tA, tB = Av.narrow(2, 3, 1), Bv.narrow(2, 3, 1);
	r_g = grad.view(-1,3,4).narrow(2,0,3);
	t_g = grad.view(-1,3,4).narrow(2,3,1);

	# Compute gradients w.r.t translations tA & tB
	# t = rA * tB + tA
	tA_g = t_g; # tA_g = t_g
	tB_g = torch.bmm(rA.transpose(1,2), t_g); # tB_g = rA ^ T * t_g

	# Compute gradients w.r.t rotations rA & rB
	# r = rA * rB
	rA_g = torch.bmm(r_g, rB.transpose(1,2)).baddbmm_(t_g, tB.transpose(1,2)); # rA_g = r_g * rB ^ T + (t_g * tB ^ T)
	rB_g = torch.bmm(rA.transpose(1,2), r_g); # rB_g = rA ^ T * r_g (similar to translation grad, but with 3 column vectors)

	# Return
	return torch.cat([rA_g,tA_g],2).view_as(A).contiguous(), torch.cat([rB_g,tB_g],2).view_as(A).contiguous()

### Test Compose Rt Pair
# Input/Target
input1 = Variable(torch.rand(2,2,3,4), requires_grad=True)
input2 = Variable(torch.rand(2,2,3,4), requires_grad=True)
target = Variable(torch.rand(2,2,3,4))

# Auto-grad
output = ComposeRtPair(input1, input2)
err    = nn.MSELoss()(output,target)
err.backward()
gauto1, gauto2 = input1.grad.clone(), input2.grad.clone()

# Analytical grad
input1.grad.data.zero_(); input2.grad.data.zero_();
from layers.ComposeRtPair import ComposeRtPair as ComposeRtPairA
output1 = ComposeRtPairA()(input1, input2);
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical1, ganalytical2 = input1.grad.clone(), input2.grad.clone()

'''
# Analytical grad
output1 = Variable(output.data.clone(), requires_grad=True);
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical1, ganalytical2 = ComposeRtPair_bwd(input1.clone(), input2.clone(), output1.grad.clone())
'''

# Compare
diff1 = gauto1 - ganalytical1
diff2 = gauto2 - ganalytical2
print("{}, {}, {}".format(diff1.data.max(),diff1.data.min(),diff1.data.abs().view(-1).median(0)[0][0]));
print("{}, {}, {}".format(diff2.data.max(),diff2.data.min(),diff2.data.abs().view(-1).median(0)[0][0]));

# Grad-Check
from layers.ComposeRtPair import ComposeRtPair as ComposeRtPairA
from torch.autograd import gradcheck
CRt = ComposeRtPairA();
torch.set_default_tensor_type('torch.DoubleTensor')
input1 = Variable(torch.rand(2,2,3,4), requires_grad=True)
input2 = Variable(torch.rand(2,2,3,4), requires_grad=True)
assert(gradcheck(CRt, [input1, input2]));

###############################################################
## NTfm3D

# Grad-Check
import torch
from layers.NTfm3D import NTfm3D
from torch.autograd import gradcheck, Variable
n = NTfm3D();
torch.set_default_tensor_type('torch.DoubleTensor')
pts   = Variable(torch.rand(2,3,4,4), requires_grad=True)
masks = Variable(torch.rand(2,4,4,4), requires_grad=True)
tfms  = Variable(torch.rand(2,4,3,4), requires_grad=True)
assert(gradcheck(n, [pts, masks, tfms]))

#################################################################
## CollapseRtPivots
def CollapseRtPivots(input):
	# Check dimensions
	bsz, nse3, nrows, ncols = input.size()
	assert (nrows == 3 and ncols == 5);

	# Init for FWD pass
	input_v = input.view(-1, 3, 5);
	r = input_v.narrow(2, 0, 3);
	t = input_v.narrow(2, 3, 1);
	p = input_v.narrow(2, 4, 1);

	# Compute output = [r, t + p - Rp]
	r_o = r;
	t_o = t + p - torch.bmm(r, p);
	return torch.cat([r_o, t_o],2).view(bsz, nse3, 3, 4).contiguous();

def CollapseRtPivots_bwd(input, grad_output):
	input_v = input.view(-1,3,5);
	grad_v  = grad_output.view(-1,3,4)
	r = input_v.narrow(2, 0, 3);
	t = input_v.narrow(2, 3, 1);
	p = input_v.narrow(2, 4, 1);
	ro_g = grad_v.narrow(2, 0, 3);
	to_g = grad_v.narrow(2, 3, 1);
	# Compute grads
	ri_g = ro_g - torch.bmm(to_g, p.transpose(1,2)) # r_g = ro_g - (to_g * p^T)
	ti_g = to_g										# t_g = to_g
	pi_g = to_g - torch.bmm(r.transpose(1,2), to_g) # p_g = to_g - (R^T * to_g)

	return torch.cat([ri_g, ti_g, pi_g],2).view_as(input).contiguous();

### Test RT Inverse
# Input/Target
input1 = Variable(torch.rand(2,2,3,5), requires_grad=True)
target = Variable(torch.rand(2,2,3,4))

# Auto-grad
output = CollapseRtPivots(input1)
err    = nn.MSELoss()(output,target)
err.backward()
gauto = input1.grad.clone()

# Analytical grad
input1.grad.data.zero_();
from layers.CollapseRtPivots import CollapseRtPivots as CollapseRtPivotsA
output1 = CollapseRtPivotsA()(input1);
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = input1.grad.clone()

'''
# Analytical grad
output1 = Variable(output.data.clone(), requires_grad=True)
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = CollapseRtPivots_bwd(input1.clone(), output1.grad.clone())
'''

# Compare
diff = gauto - ganalytical
print("{}, {}, {}".format(diff.data.max(),diff.data.min(),diff.data.abs().view(-1).median(0)[0][0]));

# Grad-Check
import torch
from torch.autograd import gradcheck, Variable
from layers.CollapseRtPivots import CollapseRtPivots
CoRt = CollapseRtPivots();
torch.set_default_tensor_type('torch.DoubleTensor')
input1 = Variable(torch.rand(2,2,3,5), requires_grad=True)
assert(gradcheck(CoRt, [input1]));

#################################################################
## ComposeRt

# Forward pair
def forwardPair(A, B):
	# Check dimensions
	batch_size, num_rows, num_cols = A.size()
	assert (num_rows == 3 and num_cols == 4);
	assert (A.is_same_size(B));

	# Init for FWD pass
	rA = A.narrow(2, 0, 3);
	rB = B.narrow(2, 0, 3);
	tA = A.narrow(2, 3, 1);
	tB = B.narrow(2, 3, 1);

	# Init output
	r  = torch.bmm(rA, rB)
	t  = torch.baddbmm(tA, rA, tB);
	return torch.cat([r,t],2);

def backwardPair(grad, A, B):
	# Setup vars
	rA = A.narrow(2, 0, 3);
	rB = B.narrow(2, 0, 3);
	tA = A.narrow(2, 3, 1);
	tB = B.narrow(2, 3, 1);
	r_g = grad.narrow(2, 0, 3);
	t_g = grad.narrow(2, 3, 1);

	# Compute gradients w.r.t translations tA & tB
	# t = rA * tB + tA
	tA_g = t_g;
	tB_g = torch.bmm(rA.transpose(1,2), t_g);

	# Compute gradients w.r.t rotations rA & rB
	# r = rA * rB
	rA_g = torch.bmm(r_g, rB.transpose(1,2)).baddbmm_(t_g, tB.transpose(1,2)); # rA_g = r_g * rB ^ T + (t_g * tB ^ T)
	rB_g = torch.bmm(rA.transpose(1,2), r_g);  # rB_g = rA ^ T * r_g (similar to translation grad, but with 3 column vectors)

	# Return
	return torch.cat([rA_g, tA_g],2), torch.cat([rB_g, tB_g],2);

## FWD fn
def ComposeRt(input, rightToLeft=False):
	# Check dimensions
	batch_size, num_se3, num_rows, num_cols = input.size()
	assert (num_rows == 3 and num_cols == 4);

	# Compute output
	output = input.clone()
	for n in xrange(1, num_se3):  # 1,2,3,...nSE3-1
		if rightToLeft:  # Append to left
			output[:,n,:,:] = forwardPair(input[:,n,:,:], output[:,n-1,:,:].clone())  # T'_n = T_n * T'_n-1
		else:  # Append to right
			output[:,n,:,:] = forwardPair(output[:,n-1,:,:].clone(), input[:,n,:,:])  # T'_n = T'_n-1 * T_n
	return output

def ComposeRt_bwd(input, output, grad_output, rightToLeft=False):
	# Get input and check for dimensions
	batch_size, num_se3, num_rows, num_cols = input.size()
	assert (num_rows == 3 and num_cols == 4);

	# Temp memory for gradient computation
	temp = grad_output.clone()

	# Compute gradient w.r.t input
	grad_input = input.clone()
	for n in xrange(num_se3 - 1, 0, -1):  # nSE3-1,...2,1
		if rightToLeft:
			grad_input[:,n,:,:], B_g = backwardPair(temp[:,n,:,:], input[:,n,:,:], output[:,n-1,:,:])  # T'_n = T_n * T'_n-1
			temp[:,n-1,:,:] += B_g
		else:
			A_g, grad_input[:,n,:,:] = backwardPair(temp[:,n,:,:], output[:,n-1,:,:], input[:,n,:,:])  # T'_n = T'_n-1 * T_n
			temp[:,n-1,:,:] += A_g
	grad_input[:,0,:,:] = temp[:,0,:,:]

	return grad_input

### Test ComposeRt
# Input/Target
input1 = Variable(torch.rand(2,8,3,4), requires_grad=True)
target = Variable(torch.rand(2,8,3,4))

# Auto-grad
output = ComposeRt(input1)
err    = nn.MSELoss()(output,target)
err.backward()
gauto = input1.grad.clone()

# Analytical grad
input1.grad.data.zero_();
from layers.ComposeRt import ComposeRt as ComposeRtA
output1 = ComposeRtA()(input1);
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = input1.grad.clone()

'''
# Analytical grad
output1 = Variable(output.data.clone(), requires_grad=True)
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = ComposeRt_bwd(input1.clone(), output1.clone(), output1.grad.clone())
'''

# Compare
diff = gauto - ganalytical
print("{}, {}, {}".format(diff.data.max(),diff.data.min(),diff.data.abs().view(-1).median(0)[0][0]));

# Grad-Check
import torch
from torch.autograd import gradcheck, Variable
from layers.ComposeRt import ComposeRt
CoRt = ComposeRt();
torch.set_default_tensor_type('torch.DoubleTensor')
input1 = Variable(torch.rand(2,8,3,4), requires_grad=True)
assert(gradcheck(CoRt, [input1]));

#################################################################
## DepthImageToDensePoints3D

def DepthImageToDensePoints3D(depth,height,width,fy,fx,cy,cx):
	# Check dimensions (B x 1 x H x W)
	batch_size, num_channels, num_rows, num_cols = depth.size()
	assert (num_channels == 1)
	assert (num_rows == height)
	assert (num_cols == width)

	# Generate basegrid once
	base_grid = depth.expand(batch_size,3,num_rows,num_cols).clone()  # (x,y,1)
	for j in xrange(0, width):  # +x is increasing columns
		base_grid[:, 0, :, j] = (j - cx) / fx
	for i in xrange(0, height):  # +y is increasing rows
		base_grid[:, 1, i, :] = (i - cy) / fy
	base_grid[:,2,:,:] = 1

	# Compute output = (x, y, depth)
	z  = depth  # z = depth
	xy = (z.expand(batch_size, 2, num_rows, num_cols) *
		  base_grid.narrow(1, 0, 2).expand(batch_size, 2, num_rows, num_cols))

	# Return
	return torch.cat([xy, z], 1)

### Test DepthImageToDense3DPoints
# Input/Target
scale = 0.5/8
ht,wd,fy,fx,cy,cx = int(480*scale), int(640*scale), 589*scale, 589*scale, 240*scale, 320*scale
input  = Variable(torch.rand(2,1,ht,wd), requires_grad=True)
target = Variable(torch.rand(2,3,ht,wd))

# Auto-grad
output = DepthImageToDense3DPoints(input,ht,wd,fy,fx,cy,cx)
err    = nn.MSELoss()(output,target)
err.backward()
gauto = input.grad.clone()

# Analytical grad
input.grad.data.zero_()
from layers.DepthImageToDense3DPoints import DepthImageToDense3DPoints as DepthImageToDense3DPointsA
output1 = DepthImageToDense3DPointsA(ht,wd,fy,fx,cy,cx)(input)
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = input.grad.clone()

# Compare
diff = gauto - ganalytical
print("{}, {}, {}".format(diff.data.max(),diff.data.min(),diff.data.abs().view(-1).median(0)[0][0]));

# Grad-Check
import torch
from torch.autograd import gradcheck, Variable
from layers.DepthImageToDense3DPoints import DepthImageToDense3DPoints
scale = 0.5/8
ht,wd,fy,fx,cy,cx = int(480*scale), int(640*scale), 589*scale, 589*scale, 240*scale, 320*scale
DPts = DepthImageToDense3DPoints(ht,wd,fy,fx,cy,cx)
torch.set_default_tensor_type('torch.FloatTensor')
input = Variable(torch.rand(2,1,ht,wd), requires_grad=True)
assert(gradcheck(DPts, [input]));

#################################################################
## Noise

import torch
from torch.autograd import Variable
import torch.nn as nn

def Noise(input, train, max_std, slope_std, iter_count, start_iter):
	iter = 1 + (iter_count[0] - start_iter)
	if (iter > 0):
		std = min((iter / 125000) * slope_std, max_std)
	else:
		std = 0
	if train:
		noise = input.clone()
		if std == 0:
			noise.fill_(0)  # no noise
		else:
			noise.data.normal_(0, std)  # Gaussian noise with 0-mean, std-standard deviation
		output = input + noise
	else:
		output = input

	# Return
	return output

### Test Noise
# Input/Target
train, max_std, slope_std, iter_count, start_iter = False, 0.1, 2, torch.FloatTensor([1000]), 1001
input  = Variable(torch.rand(2,4,9,9), requires_grad=True)
target = Variable(torch.rand(2,4,9,9))

# Auto-grad
output = Noise(input, train, max_std, slope_std, iter_count, start_iter)
err    = nn.MSELoss()(output,target)
err.backward()
gauto = input.grad.clone()

# Analytical grad
input.grad.data.zero_()
from layers.Noise import Noise as NoiseA
output1 = NoiseA(max_std, slope_std, iter_count, start_iter)(input)
err1 	= nn.MSELoss()(output1, target)
err1.backward()
ganalytical = input.grad.clone()

# Compare
diff = gauto - ganalytical
print("{}, {}, {}".format(diff.data.max(),diff.data.min(),diff.data.abs().view(-1).median(0)[0][0]));

# Grad-Check
import torch
from torch.autograd import gradcheck, Variable
from layers.Noise import Noise
train, max_std, slope_std, iter_count, start_iter = True, 0.1, 2, torch.FloatTensor([1000]), 0
Ns = Noise(max_std, slope_std, iter_count, start_iter)
torch.set_default_tensor_type('torch.DoubleTensor')
input = Variable(torch.rand(2,4,9,9), requires_grad=True)
assert(gradcheck(Ns, [input]));