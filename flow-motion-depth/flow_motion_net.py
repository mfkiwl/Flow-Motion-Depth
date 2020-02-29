import torch
import torch.nn as nn
from correlation import CorrelationLayer, EpipolarCorrelationLayer
import numpy as np
import torch.nn.functional as F
from pyquaternion import Quaternion

def conv_norm(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1, bn = True):
    if bn:
        return nn.Sequential(
            nn.Conv2d(
                in_planes,
                out_planes,
                kernel_size=kernel_size,
                stride=stride,
                padding=padding,
                dilation=dilation,
                bias=False),
            nn.BatchNorm2d(out_planes),
            nn.LeakyReLU(0.1, inplace = True))
    else:
        return nn.Sequential(
            nn.Conv2d(
                in_planes,
                out_planes,
                kernel_size=kernel_size,
                stride=stride,
                padding=padding,
                dilation=dilation,
                bias=True),
            nn.LeakyReLU(0.1, inplace = True))

def predict_flow(in_planes):
    return nn.Conv2d(
        in_planes, 2, kernel_size=3, stride=1, padding=1, bias=True)

def deconv(in_planes, out_planes, kernel_size=4, stride=2, padding=1):
    return nn.ConvTranspose2d(
        in_planes, out_planes, kernel_size, stride, padding, bias=True)

class MotionNet(nn.Module):
    '''
    MotionNet calculates the motion from input
    '''
    def get_conv_block(self, input_size, output_size):
        result = [
            nn.Conv2d(
                input_size,
                output_size,
                kernel_size=3,
                stride=2,
                padding=1,
                bias=True),
            nn.LeakyReLU(0.1, inplace = True),
            nn.Conv2d(
                output_size,
                output_size,
                kernel_size=3,
                stride=2,
                padding=1,
                bias=True),
            nn.LeakyReLU(0.1, inplace = True)
        ]
        return result

    def get_linear_block(self, input_size, output_size):
        result = [
            nn.Linear(input_size, output_size, bias=True),
            nn.LeakyReLU(0.1, inplace=True)]
        return result

    def __init__(self, conv_sizes, lin_sizes, H, W):
        super(MotionNet, self).__init__()
        self.H = H
        self.W = W
        pixel_loc = np.zeros((2, H, W))
        for i in range(H):
            for j in range(W):
                pixel_loc[:, i, j] = [j,i]
        pixel_loc = pixel_loc.reshape(1, 2, H, W).astype(np.float32)
        self.pixel_loc = torch.from_numpy(pixel_loc).cuda()

        norm_flow = np.array([W/2, H/2, W/2, H/2]).astype(np.float32)
        norm_flow = norm_flow.reshape(1,4,1,1)
        self.norm_flow = torch.from_numpy(norm_flow).cuda()


        self.shrink = conv_norm(conv_sizes[0], 32, kernel_size=1, stride=1, padding=0, dilation=1, bn = False)
        conv_sizes[0] = 32 + 4

        conv_layers = []
        for i in range(len(conv_sizes)-1):
            conv_layers.extend(self.get_conv_block(conv_sizes[i], conv_sizes[i+1]))
        self.conv_layers = nn.Sequential(*conv_layers)
        
        dropout_layers = []
        for i in range(len(lin_sizes)-1):
            dropout_layers.extend(self.get_linear_block(lin_sizes[i], lin_sizes[i+1]))
        self.dropout_layers = nn.Sequential(*dropout_layers)

        self.last_layer = nn.Linear(lin_sizes[-1], 6, bias=True)

    def forward(self, x, flow):
        batch_size = x.shape[0]

        batch_pixel_loc = self.pixel_loc.repeat(batch_size, 1, 1, 1)
        flow_point = batch_pixel_loc + flow.detach()

        flow_info = torch.cat([batch_pixel_loc, flow_point], dim = 1)
        flow_info = (flow_info - self.norm_flow) / self.norm_flow
        x_shrink = self.shrink(x)
        x_cat = torch.cat([x_shrink, flow_info], dim = 1)

        conv_result = self.conv_layers(x_cat)
        conv_result = conv_result.view(batch_size , conv_result.shape[1], -1)
        conv_result = torch.mean(conv_result, dim = 2)
        predict = self.last_layer(self.dropout_layers(conv_result))
        result = torch.zeros(predict.shape, device = x.device)
        result[:, :3] = predict[:, :3]
        result[:, 3:] = F.normalize(predict[:, 3:])
        return result

class FlowMotionNet(nn.Module):
    '''
    FlowMotionNet calculates the optical flow between two images
    It is almost the same as PWCNet, except:
    (1) the correlation implementation,
    (2) we further predicts the uncertainity of the flow
    '''
    def __init__(self, md=4):
        """
        input: md --- maximum displacement (for correlation. default: 4), after warpping
        """
        super(FlowMotionNet, self).__init__()

        self.conv1a = conv_norm(3, 16, kernel_size=3, stride=2)
        self.conv1aa = conv_norm(16, 16, kernel_size=3, stride=1)
        self.conv1b = conv_norm(16, 16, kernel_size=3, stride=1)

        self.conv2a = conv_norm(16, 32, kernel_size=3, stride=2)
        self.conv2aa = conv_norm(32, 32, kernel_size=3, stride=1)
        self.conv2b = conv_norm(32, 32, kernel_size=3, stride=1)

        self.conv3a = conv_norm(32, 64, kernel_size=3, stride=2)
        self.conv3aa = conv_norm(64, 64, kernel_size=3, stride=1)
        self.conv3b = conv_norm(64, 64, kernel_size=3, stride=1)

        self.conv4a = conv_norm(64, 96, kernel_size=3, stride=2)
        self.conv4aa = conv_norm(96, 96, kernel_size=3, stride=1)
        self.conv4b = conv_norm(96, 96, kernel_size=3, stride=1)

        self.conv5a = conv_norm(96, 128, kernel_size=3, stride=2)
        self.conv5aa = conv_norm(128, 128, kernel_size=3, stride=1)
        self.conv5b = conv_norm(128, 128, kernel_size=3, stride=1)

        self.corr = CorrelationLayer(4)
        self.leakyRELU = nn.LeakyReLU(0.1, inplace = True)

        nd = (2 * md + 1)**2
        pd = [128, 96, 64, 32, 32]
        dd = np.cumsum(pd)

        od = nd + 128
        self.conv5_0 = conv_norm(od, pd[0], kernel_size=3, stride=1, bn = False)
        self.conv5_1 = conv_norm(od + dd[0], pd[1], kernel_size=3, stride=1, bn = False)
        self.conv5_2 = conv_norm(od + dd[1], pd[2], kernel_size=3, stride=1, bn = False)
        self.conv5_3 = conv_norm(od + dd[2], pd[3], kernel_size=3, stride=1, bn = False)
        self.conv5_4 = conv_norm(od + dd[3], pd[4], kernel_size=3, stride=1, bn = False)
        self.predict_flow5 = predict_flow(od + dd[4])
        self.deconv5 = nn.UpsamplingBilinear2d(scale_factor=2)
        self.upfeat5 = deconv(
            od + dd[4], 2, kernel_size=4, stride=2, padding=1)

        od = nd + 96 + 4
        self.conv4_0 = conv_norm(od, pd[0], kernel_size=3, stride=1, bn = False)
        self.conv4_1 = conv_norm(od + dd[0], pd[1], kernel_size=3, stride=1, bn = False)
        self.conv4_2 = conv_norm(od + dd[1], pd[2], kernel_size=3, stride=1, bn = False)
        self.conv4_3 = conv_norm(od + dd[2], pd[3], kernel_size=3, stride=1, bn = False)
        self.conv4_4 = conv_norm(od + dd[3], pd[4], kernel_size=3, stride=1, bn = False)
        self.predict_flow4 = predict_flow(od + dd[4])
        self.deconv4 = nn.UpsamplingBilinear2d(scale_factor=2)
        self.upfeat4 = deconv(
            od + dd[4], 2, kernel_size=4, stride=2, padding=1)

        od = nd + 64 + 4
        self.conv3_0 = conv_norm(od, pd[0], kernel_size=3, stride=1, bn = False)
        self.conv3_1 = conv_norm(od + dd[0], pd[1], kernel_size=3, stride=1, bn = False)
        self.conv3_2 = conv_norm(od + dd[1], pd[2], kernel_size=3, stride=1, bn = False)
        self.conv3_3 = conv_norm(od + dd[2], pd[3], kernel_size=3, stride=1, bn = False)
        self.conv3_4 = conv_norm(od + dd[3], pd[4], kernel_size=3, stride=1, bn = False)
        self.predict_flow3 = predict_flow(od + dd[4])
        self.deconv3 = nn.UpsamplingBilinear2d(scale_factor=2)
        self.upfeat3 = deconv(
            od + dd[4], 2, kernel_size=4, stride=2, padding=1)

        self.motion_3 = MotionNet(
            conv_sizes = [od + dd[4], 64, 128, 256],
            lin_sizes = [256, 256, 256],
            H = 32,
            W = 40)

        self.epi_corr2 = EpipolarCorrelationLayer(maxd=range(-4,5), mind = range(-2,3), H = 64, W = 80)
        nd = (4*2+1)*(2*2+1) + 4
        od = nd + 32 + 4
        pd = [96, 64, 64, 32, 32]
        dd = np.cumsum(pd)
        self.conv2_0 = conv_norm(od, pd[0], kernel_size=3, stride=1, bn = False)
        self.conv2_1 = conv_norm(od + dd[0], pd[1], kernel_size=3, stride=1, bn = False)
        self.conv2_2 = conv_norm(od + dd[1], pd[2], kernel_size=3, stride=1, bn = False)
        self.conv2_3 = conv_norm(od + dd[2], pd[3], kernel_size=3, stride=1, bn = False)
        self.conv2_4 = conv_norm(od + dd[3], pd[4], kernel_size=3, stride=1, bn = False)
        self.predict_flow2 = predict_flow(od + dd[4])
        self.deconv2 = nn.UpsamplingBilinear2d(scale_factor=2)
        self.upfeat2 = deconv(
            od + dd[4], 2, kernel_size=4, stride=2, padding=1)

        self.motion_2 = MotionNet(
            conv_sizes = [od + dd[4], 64, 128, 256, 512],
            lin_sizes = [512, 256, 256],
            H = 64,
            W = 80)

        self.epi_corr1 = EpipolarCorrelationLayer(maxd=range(-3,4), mind = range(-1,2), H = 128, W = 160)
        nd = (3*2+1)*(1*2+1) + 4
        od = nd + 16 + 4
        pd = [64, 64, 64, 32, 32]
        dd = np.cumsum(pd)
        self.conv1_0 = conv_norm(od, pd[0], kernel_size=3, stride=1, bn = False)
        self.conv1_1 = conv_norm(od + dd[0], pd[1], kernel_size=3, stride=1, bn = False)
        self.conv1_2 = conv_norm(od + dd[1], pd[2], kernel_size=3, stride=1, bn = False)
        self.conv1_3 = conv_norm(od + dd[2], pd[3], kernel_size=3, stride=1, bn = False)
        self.conv1_4 = conv_norm(od + dd[3], pd[4], kernel_size=3, stride=1, bn = False)
        self.predict_flow1 = predict_flow(od + dd[4])
        self.last_layer_size = od + dd[4]
        self.motion_1 = MotionNet(
            conv_sizes = [od + dd[4], 64, 128, 256, 512, 512],
            lin_sizes = [512, 256, 256],
            H = 128,
            W = 160)

        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
                nn.init.kaiming_normal_(m.weight.data, mode='fan_in')
                if m.bias is not None:
                    m.bias.data.zero_()

    def warp(self, x, flo):
        """
        warp an image/tensor (im2) back to im1, according to the optical flow
        x: [B, C, H, W] (im2)
        flo: [B, 2, H, W] flow
        """
        B, C, H, W = x.size()
        # mesh grid
        xx = torch.arange(0, W).view(1, -1).repeat(H, 1)
        yy = torch.arange(0, H).view(-1, 1).repeat(1, W)
        xx = xx.view(1, 1, H, W).repeat(B, 1, 1, 1)
        yy = yy.view(1, 1, H, W).repeat(B, 1, 1, 1)
        grid = torch.cat((xx, yy), 1).float()

        if x.is_cuda:
            grid = grid.cuda()
        vgrid = grid + flo

        # scale grid to [-1,1]
        vgrid[:, 0, :, :] = 2.0 * vgrid[:, 0, :, :] / max(W - 1, 1) - 1.0
        vgrid[:, 1, :, :] = 2.0 * vgrid[:, 1, :, :] / max(H - 1, 1) - 1.0

        vgrid = vgrid.permute(0, 2, 3, 1)
        output = nn.functional.grid_sample(x, vgrid)
        mask = torch.ones(x.size()).cuda()
        mask = nn.functional.grid_sample(mask, vgrid)

        mask[mask < 0.9999] = 0
        mask[mask > 0] = 1

        return output * mask

    def get_motion(self, predicted_motion):
        # actually we can decode the motion vector on GPU and this will 
        # 1, preserve the gradient and 2, accelerate the code
        cpu_vector = predicted_motion.detach().cpu().numpy()
        batch_num = predicted_motion.shape[0]
        Rs = np.zeros((batch_num, 3, 3))
        Ts = np.zeros((batch_num, 3, 1))
        for i in range(batch_num):
            q = Quaternion(
                axis=cpu_vector[i, :3],
                radians=np.linalg.norm(cpu_vector[i, :3]))
            Rs[i,:,:] = q.rotation_matrix
            Ts[i, :, 0] = cpu_vector[i, 3:]
        Rs = torch.from_numpy(Rs.astype(np.float32)).cuda()
        Ts = torch.from_numpy(Ts.astype(np.float32)).cuda()
        return Rs, Ts

    def forward(self, x):
        im1 = x[:, :3, :, :]
        im2 = x[:, 3:, :, :]

        c11 = self.conv1b(self.conv1aa(self.conv1a(im1)))
        c21 = self.conv1b(self.conv1aa(self.conv1a(im2)))
        c12 = self.conv2b(self.conv2aa(self.conv2a(c11)))
        c22 = self.conv2b(self.conv2aa(self.conv2a(c21)))
        c13 = self.conv3b(self.conv3aa(self.conv3a(c12)))
        c23 = self.conv3b(self.conv3aa(self.conv3a(c22)))
        c14 = self.conv4b(self.conv4aa(self.conv4a(c13)))
        c24 = self.conv4b(self.conv4aa(self.conv4a(c23)))
        c15 = self.conv5b(self.conv5aa(self.conv5a(c14)))
        c25 = self.conv5b(self.conv5aa(self.conv5a(c24)))

        corr5 = self.corr(c15, c25)
        corr5 = self.leakyRELU(corr5)
        x = torch.cat((corr5, c15), 1)
        x = torch.cat((self.conv5_0(x), x), 1)
        x = torch.cat((self.conv5_1(x), x), 1)
        x = torch.cat((self.conv5_2(x), x), 1)
        x = torch.cat((self.conv5_3(x), x), 1)
        x = torch.cat((self.conv5_4(x), x), 1)
        flow5 = self.predict_flow5(x)
        up_flow5 = self.deconv5(flow5) * 2.0
        up_feat5 = self.upfeat5(x)

        warp4 = self.warp(c24, up_flow5)
        corr4 = self.corr(c14, warp4)
        corr4 = self.leakyRELU(corr4)
        x = torch.cat((corr4, c14, up_flow5, up_feat5), 1)
        x = torch.cat((self.conv4_0(x), x), 1)
        x = torch.cat((self.conv4_1(x), x), 1)
        x = torch.cat((self.conv4_2(x), x), 1)
        x = torch.cat((self.conv4_3(x), x), 1)
        x = torch.cat((self.conv4_4(x), x), 1)
        flow4 = self.predict_flow4(x)
        up_flow4 = self.deconv4(flow4) * 2.0
        up_feat4 = self.upfeat4(x)

        warp3 = self.warp(c23, up_flow4)
        corr3 = self.corr(c13, warp3)
        corr3 = self.leakyRELU(corr3)

        x = torch.cat((corr3, c13, up_flow4, up_feat4), 1)
        x = torch.cat((self.conv3_0(x), x), 1)
        x = torch.cat((self.conv3_1(x), x), 1)
        x = torch.cat((self.conv3_2(x), x), 1)
        x = torch.cat((self.conv3_3(x), x), 1)
        x = torch.cat((self.conv3_4(x), x), 1)
        flow3 = self.predict_flow3(x)
        up_flow3 = self.deconv3(flow3) * 2.0
        up_feat3 = self.upfeat3(x)
        predicted_motion3 = self.motion_3(x, flow3)
        Rs3, Ts3 = self.get_motion(predicted_motion3)

        corr2 = self.epi_corr2(c12, c22, Rs3, Ts3, up_flow3)
        corr2 = self.leakyRELU(corr2)
        x = torch.cat((corr2, c12, up_flow3, up_feat3), 1)
        x = torch.cat((self.conv2_0(x), x), 1)
        x = torch.cat((self.conv2_1(x), x), 1)
        x = torch.cat((self.conv2_2(x), x), 1)
        x = torch.cat((self.conv2_3(x), x), 1)
        x = torch.cat((self.conv2_4(x), x), 1)
        flow2 = self.predict_flow2(x)
        up_flow2 = self.deconv2(flow2) * 2.0
        up_feat2 = self.upfeat2(x)
        predicted_motion2 = self.motion_2(x, flow2)
        Rs2, Ts2 = self.get_motion(predicted_motion2)

        corr1 = self.epi_corr1(c11, c21, Rs2, Ts2, up_flow2)
        corr1 = self.leakyRELU(corr1)
        x = torch.cat((corr1, c11, up_flow2, up_feat2), 1)
        x = torch.cat((self.conv1_0(x), x), 1)
        x = torch.cat((self.conv1_1(x), x), 1)
        x = torch.cat((self.conv1_2(x), x), 1)
        x = torch.cat((self.conv1_3(x), x), 1)
        x = torch.cat((self.conv1_4(x), x), 1)
        self.last_layer = x
        flow1 = self.predict_flow1(x)
        predicted_motion1 = self.motion_1(x, flow1)

        flows = [flow1, flow2, flow3, flow4, flow5]
        motions = [predicted_motion1, predicted_motion2, predicted_motion3]
        return flows, motions