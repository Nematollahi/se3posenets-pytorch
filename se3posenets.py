import torch
import torch.nn as nn


import se3layers as se3nn
import ctrlnets
import se3

####################################
### Pose-Mask Encoder (single encoder that predicts both poses and masks)
# Model that takes in "depth/point cloud" to generate "k"-channel masks and "k" poses represented as [R|t]
# NOTE: We can set a conditional flag that makes it predict both poses/masks or just poses
class PoseMaskEncoder(nn.Module):
    def __init__(self, num_se3, se3_type='se3aa', use_pivot=False, use_kinchain=False, pre_conv=False,
                 input_channels=3, use_bn=True, nonlinearity='prelu', init_se3_iden=False,
                 use_wt_sharpening=False, sharpen_start_iter=0, sharpen_rate=1,
                 use_sigmoid_mask=False, wide=False, use_jt_angles=False, num_state=7,
                 full_res=False, noise_stop_iter=1e6):
        super(PoseMaskEncoder, self).__init__()

        ###### Choose type of convolution
        if pre_conv:
            print('Using BN + Non-Linearity + Conv/Deconv architecture')
        ConvType   = ctrlnets.PreConv2D if pre_conv else ctrlnets.BasicConv2D
        DeconvType = ctrlnets.PreDeconv2D if pre_conv else ctrlnets.BasicDeconv2D

        ###### Encoder
        # Create conv-encoder (large net => 5 conv layers with pooling)
        chn = [32, 64, 128, 256, 256, 256] if wide else [8, 16, 32, 64, 128, 128]  # Num channels
        self.conv1 = ConvType(input_channels, chn[0], kernel_size=9, stride=1, padding=4,
                              use_pool=True, use_bn=use_bn, nonlinearity=nonlinearity)  # 9x9, 240x320 -> 120x160
        self.conv2 = ConvType(chn[0], chn[1], kernel_size=7, stride=1, padding=3,
                              use_pool=True, use_bn=use_bn, nonlinearity=nonlinearity)  # 7x7, 120x160 -> 60x80
        self.conv3 = ConvType(chn[1], chn[2], kernel_size=5, stride=1, padding=2,
                              use_pool=True, use_bn=use_bn, nonlinearity=nonlinearity)  # 5x5, 60x80 -> 30x40
        self.conv4 = ConvType(chn[2], chn[3], kernel_size=3, stride=1, padding=1,
                              use_pool=True, use_bn=use_bn, nonlinearity=nonlinearity)  # 3x3, 30x40 -> 15x20
        self.conv5 = ConvType(chn[3], chn[4], kernel_size=3, stride=1, padding=1,
                              use_pool=True, use_bn=use_bn, nonlinearity=nonlinearity)  # 3x3, 15x20 -> 7x10
        self.celem = chn[4] * 7 * 10

        ###### Switch between full-res vs not
        self.full_res = full_res
        if self.full_res:
            ###### Conv 6
            self.conv6 = ConvType(chn[4], chn[5], kernel_size=3, stride=1, padding=1,
                                  use_pool=True, use_bn=use_bn, nonlinearity=nonlinearity)  # 3x3, 15x20 -> 7x10
            self.celem = chn[5] * 7 * 10

            ###### Mask Decoder
            # Create deconv-decoder (FCN style, has skip-add connections to conv outputs)
            self.conv1x1 = ConvType(chn[5], chn[5], kernel_size=1, stride=1, padding=0,
                                    use_pool=False, use_bn=use_bn, nonlinearity=nonlinearity)  # 1x1, 7x10 -> 7x10
            self.deconv1 = DeconvType(chn[5], chn[4], kernel_size=(3, 4), stride=2, padding=(0, 1),
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 3x4, 7x10 -> 15x20
            self.deconv2 = DeconvType(chn[4], chn[3], kernel_size=4, stride=2, padding=1,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 4x4, 15x20 -> 30x40
            self.deconv3 = DeconvType(chn[3], chn[2], kernel_size=6, stride=2, padding=2,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 6x6, 30x40 -> 60x80
            self.deconv4 = DeconvType(chn[2], chn[1], kernel_size=6, stride=2, padding=2,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 6x6, 60x80 -> 120x160
            self.deconv5 = DeconvType(chn[1], chn[0], kernel_size=6, stride=2, padding=2,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 6x6, 120x160 -> 240x320
            if pre_conv:
                # Can fit an extra BN + Non-linearity
                self.deconv6 = DeconvType(chn[0], num_se3, kernel_size=8, stride=2, padding=3,
                                          use_bn=use_bn, nonlinearity=nonlinearity)  # 8x8, 240x320 -> 480x640
            else:
                self.deconv6 = nn.ConvTranspose2d(chn[0], num_se3, kernel_size=8, stride=2,
                                                  padding=3)  # 8x8, 120x160 -> 240x320
        else:
            ###### Mask Decoder
            # Create deconv-decoder (FCN style, has skip-add connections to conv outputs)
            self.conv1x1 = ConvType(chn[4], chn[4], kernel_size=1, stride=1, padding=0,
                                    use_pool=False, use_bn=use_bn, nonlinearity=nonlinearity)  # 1x1, 7x10 -> 7x10
            self.deconv1 = DeconvType(chn[4], chn[3], kernel_size=(3, 4), stride=2, padding=(0, 1),
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 3x4, 7x10 -> 15x20
            self.deconv2 = DeconvType(chn[3], chn[2], kernel_size=4, stride=2, padding=1,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 4x4, 15x20 -> 30x40
            self.deconv3 = DeconvType(chn[2], chn[1], kernel_size=6, stride=2, padding=2,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 6x6, 30x40 -> 60x80
            self.deconv4 = DeconvType(chn[1], chn[0], kernel_size=6, stride=2, padding=2,
                                      use_bn=use_bn, nonlinearity=nonlinearity)  # 6x6, 60x80 -> 120x160
            if pre_conv:
                # Can fit an extra BN + Non-linearity
                self.deconv5 = DeconvType(chn[0], num_se3, kernel_size=8, stride=2, padding=3,
                                          use_bn=use_bn, nonlinearity=nonlinearity)  # 8x8, 120x160 -> 240x320
            else:
                self.deconv5 = nn.ConvTranspose2d(chn[0], num_se3, kernel_size=8, stride=2,
                                                  padding=3)  # 8x8, 120x160 -> 240x320

        # Normalize to generate mask (wt-sharpening vs sigmoid-mask vs soft-mask model)
        self.use_wt_sharpening = use_wt_sharpening
        if use_wt_sharpening:
            self.sharpen_start_iter = sharpen_start_iter # Start iter for the sharpening
            self.sharpen_rate = sharpen_rate # Rate for sharpening
            self.maskdecoder = ctrlnets.sharpen_masks # Use the weight-sharpener
            self.noise_stop_iter = noise_stop_iter
        elif use_sigmoid_mask:
            #lastdeconvlayer = self.deconv5.deconv if pre_conv else self.deconv5
            #init_sigmoidmask_bg(lastdeconvlayer, num_se3) # Initialize last deconv layer to predict BG | all zeros
            self.maskdecoder = nn.Sigmoid() # No normalization, each pixel can belong to multiple masks but values between (0-1)
        else:
            self.maskdecoder = nn.Softmax2d() # SoftMax normalization

        ###### Encode jt angles
        self.use_jt_angles = use_jt_angles
        jdim = 0
        if self.use_jt_angles:
            jdim = 256
            self.jtencoder = nn.Sequential(
                nn.Linear(num_state, 128),
                ctrlnets.get_nonlinearity(nonlinearity),
                nn.Linear(128, 256),
                ctrlnets.get_nonlinearity(nonlinearity),
            )

        ###### Pose Decoder
        # Create SE3 decoder (take conv output, reshape, run FC layers to generate "num_se3" poses)
        self.se3_dim = ctrlnets.get_se3_dimension(se3_type=se3_type, use_pivot=use_pivot)
        self.num_se3 = num_se3
        sdim = 256 if wide else 128 # NOTE: This was 64 before! #128
        self.se3decoder  = nn.Sequential(
                                nn.Linear(self.celem + jdim, sdim),
                                ctrlnets.get_nonlinearity(nonlinearity),
                                nn.Linear(sdim, self.num_se3 * self.se3_dim)  # Predict the SE3s from the conv-output
                           )

        # Initialize the SE3 decoder to predict identity SE3
        if init_se3_iden:
            print("Initializing SE3 prediction layer of the pose-mask model to predict identity transform")
            layer = self.se3decoder[2]  # Get final SE3 prediction module
            ctrlnets.init_se3layer_identity(layer, num_se3, se3_type)  # Init to identity

        # Create pose decoder (convert to r/t)
        self.se3_type = se3_type
        self.posedecoder = nn.Sequential()
        #if se3_type != 'se3aar':
        self.posedecoder.add_module('se3rt', lambda x: se3.SE3ToRt(x, se3_type, use_pivot)) # Convert to Rt
        if use_pivot:
            self.posedecoder.add_module('pivotrt', se3.CollapseRtPivots) # Collapse pivots
        if use_kinchain:
            self.posedecoder.add_module('kinchain', se3nn.ComposeRt(rightToLeft=False)) # Kinematic chain

    def compute_wt_sharpening_stats(self, train_iter=0):
        citer = 1 + (train_iter - self.sharpen_start_iter)
        noise_std, pow = 0, 1
        if (citer > 0):
            noise_std = min((citer/125000.0) * self.sharpen_rate, 0.1) # Should be 0.1 by ~12500 iters from start (if rate=1)
            if hasattr(self, 'noise_stop_iter') and train_iter > self.noise_stop_iter:
                noise_std = 0
            pow = min(1 + (citer/500.0) * self.sharpen_rate, 100) # Should be 26 by ~12500 iters from start (if rate=1)
        return noise_std, pow

    def forward(self, z, predict_masks=True, train_iter=0):
        if self.use_jt_angles or type(z) == list:
            x,j = z # Pts, Jt angles
        else:
            x = z

        # Run conv-encoder to generate embedding
        c1 = self.conv1(x)
        c2 = self.conv2(c1)
        c3 = self.conv3(c2)
        c4 = self.conv4(c3)
        c5 = self.conv5(c4)
        if self.full_res:
            c6 = self.conv6(c5)
            cout = c6
        else:
            cout = c5

        # Run jt-encoder & concatenate the embeddings
        if self.use_jt_angles:
            j = self.jtencoder(j)
            p = torch.cat([cout.view(-1, self.celem), j], 1)
        else:
            p = cout.view(-1, self.celem)

        # Run pose-decoder to predict poses
        p = self.se3decoder(p)
        p = p.view(-1, self.num_se3, self.se3_dim)
        self.se3output = p.clone() # SE3 prediction
        #if self.se3_type == 'se3aar':
        #    p = se3ToRt(p) # Use new se3ToRt layer!
        p = self.posedecoder(p)

        # Run mask-decoder to predict a smooth mask
        # NOTE: Conditional based on input flag
        if predict_masks:
            ### Full-res vs Not
            if self.full_res:
                # Run mask-decoder to predict a smooth mask
                m = self.conv1x1(c6)
                m = self.deconv1(m, c5)
                m = self.deconv2(m, c4)
                m = self.deconv3(m, c3)
                m = self.deconv4(m, c2)
                m = self.deconv5(m, c1)
                m = self.deconv6(m)
            else:
                # Run mask-decoder to predict a smooth mask
                m = self.conv1x1(c5)
                m = self.deconv1(m, c4)
                m = self.deconv2(m, c3)
                m = self.deconv3(m, c2)
                m = self.deconv4(m, c1)
                m = self.deconv5(m)

            # Predict a mask (either wt-sharpening or sigmoid-mask or soft-mask approach)
            # Normalize to sum across 1 along the channels (only for weight sharpening or soft-mask)
            if self.use_wt_sharpening:
                noise_std, pow = self.compute_wt_sharpening_stats(train_iter=train_iter)
                m = self.maskdecoder(m, add_noise=self.training, noise_std=noise_std, pow=pow)
            else:
                m = self.maskdecoder(m)

            # Return both
            return p, m
        else:
            return p


####################################
### Transition model (predicts change in poses based on the applied control)
# Takes in [pose_t, ctrl_t] and generates delta pose between t & t+1
class TransitionModel(nn.Module):
    def __init__(self, num_ctrl, num_se3, delta_pivot='', se3_type='se3aa',
                 use_kinchain=False, nonlinearity='prelu', init_se3_iden=False,
                 local_delta_se3=False, use_jt_angles=False, num_state=7):
        super(TransitionModel, self).__init__()
        self.se3_dim = ctrlnets.get_se3_dimension(se3_type=se3_type, use_pivot=(delta_pivot == 'pred')) # Only if we are predicting directly
        self.num_se3 = num_se3

        # Pose encoder
        pdim = [128, 256]
        self.poseencoder = nn.Sequential(
                                nn.Linear(self.num_se3 * 12, pdim[0]),
                                ctrlnets.get_nonlinearity(nonlinearity),
                                nn.Linear(pdim[0], pdim[1]),
                                ctrlnets.get_nonlinearity(nonlinearity)
                            )

        # Control encoder
        cdim = [128, 256] #[64, 128]
        self.ctrlencoder = nn.Sequential(
                                nn.Linear(num_ctrl, cdim[0]),
                                ctrlnets.get_nonlinearity(nonlinearity),
                                nn.Linear(cdim[0], cdim[1]),
                                ctrlnets.get_nonlinearity(nonlinearity)
                            )

        # Jt angle encoder
        jdim = [0, 0]
        self.use_jt_angles = use_jt_angles
        if use_jt_angles:
            jdim = [128, 256]  # [64, 128]
            self.jtangleencoder = nn.Sequential(
                nn.Linear(num_state, jdim[0]),
                ctrlnets.get_nonlinearity(nonlinearity),
                nn.Linear(jdim[0], jdim[1]),
                ctrlnets.get_nonlinearity(nonlinearity)
            )

        # SE3 decoder
        self.deltase3decoder = nn.Sequential(
            nn.Linear(pdim[1]+cdim[1]+jdim[1], 256),
            ctrlnets.get_nonlinearity(nonlinearity),
            nn.Linear(256, 128),
            ctrlnets.get_nonlinearity(nonlinearity),
            nn.Linear(128, self.num_se3 * self.se3_dim)
        )

        # Initialize the SE3 decoder to predict identity SE3
        if init_se3_iden:
            print("Initializing SE3 prediction layer of the transition model to predict identity transform")
            layer = self.deltase3decoder[4]  # Get final SE3 prediction module
            ctrlnets.init_se3layer_identity(layer, num_se3, se3_type) # Init to identity

        # Create pose decoder (convert to r/t)
        self.se3_type    = se3_type
        self.delta_pivot = delta_pivot
        self.inp_pivot   = (self.delta_pivot != '') and (self.delta_pivot != 'pred') # Only for these 2 cases, no pivot is passed in as input
        self.deltaposedecoder = nn.Sequential()
        #if se3_type != 'se3aar':
        self.deltaposedecoder.add_module('se3rt', lambda x: se3.SE3ToRt(x, se3_type, (self.delta_pivot != '')))  # Convert to Rt
        if (self.delta_pivot != ''):
            self.deltaposedecoder.add_module('pivotrt', se3.CollapseRtPivots)  # Collapse pivots
        #if use_kinchain:
        #    self.deltaposedecoder.add_module('kinchain', se3nn.ComposeRt(rightToLeft=False))  # Kinematic chain

        # Compose deltas with prev pose to get next pose
        # It predicts the delta transform of the object between time "t1" and "t2": p_t2_to_t1: takes a point in t2 and converts it to t1's frame of reference
	    # So, the full transform is: p_t2 = p_t1 * p_t2_to_t1 (or: p_t2 (t2 to cam) = p_t1 (t1 to cam) * p_t2_to_t1 (t2 to t1))
        self.posedecoder = se3.ComposeRtPair

        # In case the predicted delta (D) is in the local frame of reference, we compute the delta in the global reference
        # system in the following way:
        # SE3_2 = SE3_1 * D_local (this takes a point from the local reference frame to the global frame)
        # D_global = SE3_1 * D_local * SE3_1^-1 (this takes a point in the global reference frame, transforms it and returns a point in the same reference frame)
        self.local_delta_se3 = local_delta_se3
        if self.local_delta_se3:
            print('Deltas predicted by transition model will affect points in local frame of reference')
            self.rtinv = se3.RtInverse
            self.globaldeltadecoder = se3.ComposeRtPair

    def forward(self, x):
        # Run the forward pass
        if self.use_jt_angles:
            if self.inp_pivot:
                p, j, c, pivot = x # Pose, Jtangles, Control, Pivot
            else:
                p, j, c = x # Pose, Jtangles, Control
        else:
            if self.inp_pivot:
                p, c, pivot = x # Pose, Control, Pivot
            else:
                p, c = x # Pose, Control
        pv = p.view(-1, self.num_se3*12) # Reshape pose
        pe = self.poseencoder(pv)    # Encode pose
        ce = self.ctrlencoder(c)     # Encode ctrl
        if self.use_jt_angles:
            je = self.jtangleencoder(j)  # Encode jt angles
            x = torch.cat([pe,je,ce], 1) # Concatenate encoded vectors
        else:
            x = torch.cat([pe,ce], 1)    # Concatenate encoded vectors
        x = self.deltase3decoder(x)  # Predict delta-SE3
        x = x.view(-1, self.num_se3, self.se3_dim)
        if self.inp_pivot: # For these two cases, we don't need to handle anything
            x = torch.cat([x, pivot.view(-1, self.num_se3, 3)], 2) # Use externally provided pivots
        #if self.se3_type == 'se3aar':
        #    x = se3ToRt(x) # Use new se3ToRt layer!
        x = self.deltaposedecoder(x)  # Convert delta-SE3 to delta-Pose (can be in local or global frame of reference)
        if self.local_delta_se3:
            # Predicted delta is in the local frame of reference, can't use it directly
            z = self.posedecoder(p, x) # SE3_2 = SE3_1 * D_local (takes a point in local frame to global frame)
            y = x #TODO: This is not correct, fix it! self.globaldeltadecoder(z, self.rtinv(p)) # D_global = SE3_2 * SE3_1^-1 = SE3_1 * D_local * SE3_1^-1 (from global to global)
        else:
            # Predicted delta is already in the global frame of reference, use it directly (from global to global)
            z = self.posedecoder(x, p) # Compose predicted delta & input pose to get next pose (SE3_2 = SE3_2 * SE3_1^-1 * SE3_1)
            y = x # D = SE3_2 * SE3_1^-1 (global to global)

        # Return
        return [y, z] # Return both the deltas (in global frame) and the composed next pose


####################################
### Multi-step version of the SE3-Pose-Model
### Currently, this has a separate pose & mask predictor as well as a transition model
### NOTE: The forward pass is not currently implemented, this needs to be done outside
class MultiStepSE3PoseModel(nn.Module):
    def __init__(self, num_ctrl, num_se3, se3_type='se3aa', use_pivot=False, delta_pivot='',
                 use_kinchain=False, input_channels=3, use_bn=True, pre_conv=False, decomp_model=False,
                 nonlinearity='prelu', init_posese3_iden= False, init_transse3_iden = False,
                 use_wt_sharpening=False, sharpen_start_iter=0, sharpen_rate=1,
                 use_sigmoid_mask=False, local_delta_se3=False, wide=False,
                 use_jt_angles=False, use_jt_angles_trans=False, num_state=7,
                 full_res=False, noise_stop_iter=1e6, trans_type='default',
                 posemask_type='default'):
        super(MultiStepSE3PoseModel, self).__init__()

        # Initialize the pose & mask model
        # self.decomp_model = decomp_model
        # if self.decomp_model:
        #     print('Using separate networks for pose and mask prediction')
        #     self.posemodel = PoseEncoder(num_se3=num_se3, se3_type=se3_type, use_pivot=use_pivot,
        #                                  use_kinchain=use_kinchain, input_channels=input_channels,
        #                                  init_se3_iden=init_posese3_iden, use_bn=use_bn, pre_conv=pre_conv,
        #                                  nonlinearity=nonlinearity, wide=wide,
        #                                  use_jt_angles=use_jt_angles, num_state=num_state,
        #                                  full_res=full_res)
        #     self.maskmodel = MaskEncoder(num_se3=num_se3, input_channels=input_channels,
        #                                  use_bn=use_bn, pre_conv=pre_conv,
        #                                  nonlinearity=nonlinearity, use_wt_sharpening=use_wt_sharpening,
        #                                  sharpen_start_iter=sharpen_start_iter, sharpen_rate=sharpen_rate,
        #                                  use_sigmoid_mask=use_sigmoid_mask, wide=wide,
        #                                  full_res=full_res, noise_stop_iter=noise_stop_iter)
        # else:
        #     if posemask_type == 'default':
        #         print('Using default network for pose & mask prediction')
        #         posemaskfn = PoseMaskEncoder
        #     elif posemask_type == 'unet':
        #         import unet
        #         print('Using U-Net for pose & mask prediction')
        #         posemaskfn = unet.UNetPoseMaskEncoder
        #     else:
        #         assert False, "Unknown pose mask model type input: {}".format(posemask_type)
        self.posemaskmodel = PoseMaskEncoder(num_se3=num_se3, se3_type=se3_type, use_pivot=use_pivot,
                                        use_kinchain=use_kinchain, input_channels=input_channels,
                                        init_se3_iden=init_posese3_iden, use_bn=use_bn, pre_conv=pre_conv,
                                        nonlinearity=nonlinearity, use_wt_sharpening=use_wt_sharpening,
                                        sharpen_start_iter=sharpen_start_iter, sharpen_rate=sharpen_rate,
                                        use_sigmoid_mask=use_sigmoid_mask, wide=wide,
                                        use_jt_angles=use_jt_angles, num_state=num_state,
                                        full_res=full_res, noise_stop_iter=noise_stop_iter)

        # # Initialize the transition model
        # if trans_type == 'default':
        #     print('Using default transition model')
        #     transfn = TransitionModel
        # elif trans_type == 'linear':
        #     print('Using linear transition model')
        #     transfn = LinearTransitionModel
        # elif trans_type == 'simple':
        #     print('Using simple transition model')
        #     import transnets
        #     transfn = lambda **v: transnets.SimpleTransitionModel(wide=False, **v)
        # elif trans_type == 'simplewide':
        #     print('Using simple-wide transition model')
        #     import transnets
        #     transfn = lambda **v: transnets.SimpleTransitionModel(wide=True, **v)
        # elif trans_type == 'locallinear':
        #     print('Using local linear transition model')
        #     transfn = LocalLinearTransitionModel
        # elif trans_type == 'locallineardelta':
        #     print('Using local linear delta transition model')
        #     transfn = LocalLinearDeltaTransitionModel
        # else:
        #     assert False, "Unknown transition model type input: {}".format(trans_type)
        self.transitionmodel = TransitionModel(num_ctrl=num_ctrl, num_se3=num_se3, delta_pivot=delta_pivot,
                                       se3_type=se3_type, use_kinchain=use_kinchain,
                                       nonlinearity=nonlinearity, init_se3_iden = init_transse3_iden,
                                       local_delta_se3=local_delta_se3,
                                       use_jt_angles=use_jt_angles_trans, num_state=num_state)

        # Options
        self.use_jt_angles = use_jt_angles
        self.use_jt_angles_trans = use_jt_angles_trans

    # Predict pose only
    def forward_only_pose(self, x):
        ptcloud, jtangles = x
        inp = [ptcloud, jtangles] if self.use_jt_angles else ptcloud
        if self.decomp_model:
            return self.posemodel(inp)
        else:
            return self.posemaskmodel(inp, predict_masks=False) # returns only pose

    # Predict both pose and mask
    def forward_pose_mask(self, x, train_iter=0):
        ptcloud, jtangles = x
        inp = [ptcloud, jtangles] if self.use_jt_angles else ptcloud
        if self.decomp_model:
            pose = self.posemodel(inp)
            mask = self.maskmodel(ptcloud, train_iter=train_iter)
            return pose, mask
        else:
            return self.posemaskmodel(inp, train_iter=train_iter, predict_masks=True) # Predict both

    # Predict next pose based on current pose and control
    def forward_next_pose(self, pose, ctrl, jtangles=None, pivots=None):
        inp = [pose, jtangles, ctrl] if self.use_jt_angles_trans else [pose, ctrl]
        if self.transitionmodel.inp_pivot:
            inp.append(pivots)
        return self.transitionmodel(inp)

    # Forward pass through the model
    def forward(self, x):
        print('Forward pass for Multi-Step SE3-Pose-Model is not yet implemented')
        raise NotImplementedError