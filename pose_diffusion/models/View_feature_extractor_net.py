import torch
import torch.nn as nn

#定义残差块，供NoisyTargetViewEncoder使用
class Image2DResBlockWithTV(nn.Module):
    def __init__(self, dim, tdim, vdim):
        super().__init__()
        norm = lambda c: nn.GroupNorm(8, c)
        self.time_embed = nn.Conv2d(tdim, dim, 1, 1)
        self.view_embed = nn.Conv2d(vdim, dim, 1, 1)
        self.conv = nn.Sequential(
            norm(dim),
            nn.SiLU(True),
            nn.Conv2d(dim, dim, 3, 1, 1),
            norm(dim),
            nn.SiLU(True),
            nn.Conv2d(dim, dim, 3, 1, 1),
        )

    def forward(self, x, t, v):
        return x+self.conv(x+self.time_embed(t)+self.view_embed(v))

#将输入图像进行特征编码，并整合时间（t）和视点（v）信息，貌似没有直接添加噪声
class NoisyTargetViewEncoder(nn.Module):
    def __init__(self, time_embed_dim, viewpoint_dim, run_dim=16, output_dim=8):
        super().__init__()

        self.init_conv = nn.Conv2d(4, run_dim, 3, 1, 1)
        self.out_conv0 = Image2DResBlockWithTV(run_dim, time_embed_dim, viewpoint_dim)
        self.out_conv1 = Image2DResBlockWithTV(run_dim, time_embed_dim, viewpoint_dim)
        self.out_conv2 = Image2DResBlockWithTV(run_dim, time_embed_dim, viewpoint_dim)
        self.final_out = nn.Sequential(
            nn.GroupNorm(8, run_dim),
            nn.SiLU(True),
            nn.Conv2d(run_dim, output_dim, 3, 1, 1)
        )

    def forward(self, x, t, v):
        B, DT = t.shape
        t = t.view(B, DT, 1, 1)
        B, DV = v.shape
        v = v.view(B, DV, 1, 1)

        x = self.init_conv(x)
        x = self.out_conv0(x, t, v)
        x = self.out_conv1(x, t, v)
        x = self.out_conv2(x, t, v)
        x = self.final_out(x)
        return x

class SpatialUpTimeBlock(nn.Module):
    def __init__(self, x_in_dim, t_in_dim, out_dim):
        super().__init__()
        norm_act = lambda c: nn.GroupNorm(8, c)
        self.t_conv = nn.Conv3d(t_in_dim, x_in_dim, 1, 1)  # 16
        self.norm = norm_act(x_in_dim)
        self.silu = nn.SiLU(True)
        self.conv = nn.ConvTranspose3d(x_in_dim, out_dim, kernel_size=3, padding=1, output_padding=1, stride=2)

    def forward(self, x, t):
        x = x + self.t_conv(t)
        return self.conv(self.silu(self.norm(x)))

class SpatialTimeBlock(nn.Module):
    def __init__(self, x_in_dim, t_in_dim, out_dim, stride):
        super().__init__()
        norm_act = lambda c: nn.GroupNorm(8, c)
        self.t_conv = nn.Conv3d(t_in_dim, x_in_dim, 1, 1)  # 16
        self.bn = norm_act(x_in_dim)
        self.silu = nn.SiLU(True)
        self.conv = nn.Conv3d(x_in_dim, out_dim, 3, stride=stride, padding=1)

    def forward(self, x, t):
        x = x + self.t_conv(t)
        return self.conv(self.silu(self.bn(x)))

#对应的3D CNN和spatial volume步骤
class SpatialTime3DNet(nn.Module):
        def __init__(self, time_dim=256, input_dim=128, dims=(32, 64, 128, 256)):
            super().__init__()
            d0, d1, d2, d3 = dims
            dt = time_dim

            self.init_conv = nn.Conv3d(input_dim, d0, 3, 1, 1)  # 32
            self.conv0 = SpatialTimeBlock(d0, dt, d0, stride=1)

            self.conv1 = SpatialTimeBlock(d0, dt, d1, stride=2)
            self.conv2_0 = SpatialTimeBlock(d1, dt, d1, stride=1)
            self.conv2_1 = SpatialTimeBlock(d1, dt, d1, stride=1)

            self.conv3 = SpatialTimeBlock(d1, dt, d2, stride=2)
            self.conv4_0 = SpatialTimeBlock(d2, dt, d2, stride=1)
            self.conv4_1 = SpatialTimeBlock(d2, dt, d2, stride=1)

            self.conv5 = SpatialTimeBlock(d2, dt, d3, stride=2)
            self.conv6_0 = SpatialTimeBlock(d3, dt, d3, stride=1)
            self.conv6_1 = SpatialTimeBlock(d3, dt, d3, stride=1)

            self.conv7 = SpatialUpTimeBlock(d3, dt, d2)
            self.conv8 = SpatialUpTimeBlock(d2, dt, d1)
            self.conv9 = SpatialUpTimeBlock(d1, dt, d0)

        def forward(self, x, t):
            B, C = t.shape
            t = t.view(B, C, 1, 1, 1)

            x = self.init_conv(x)
            conv0 = self.conv0(x, t)

            x = self.conv1(conv0, t)
            x = self.conv2_0(x, t)
            conv2 = self.conv2_1(x, t)

            x = self.conv3(conv2, t)
            x = self.conv4_0(x, t)
            conv4 = self.conv4_1(x, t)

            x = self.conv5(conv4, t)
            x = self.conv6_0(x, t)
            x = self.conv6_1(x, t)

            x = conv4 + self.conv7(x, t)
            x = conv2 + self.conv8(x, t)
            x = conv0 + self.conv9(x, t)
            return x

class FrustumTVBlock(nn.Module):
    def __init__(self, x_dim, t_dim, v_dim, out_dim, stride):
        super().__init__()
        norm_act = lambda c: nn.GroupNorm(8, c)
        self.t_conv = nn.Conv3d(t_dim, x_dim, 1, 1) # 16
        self.v_conv = nn.Conv3d(v_dim, x_dim, 1, 1) # 16
        self.bn = norm_act(x_dim)
        self.silu = nn.SiLU(True)
        self.conv = nn.Conv3d(x_dim, out_dim, 3, stride=stride, padding=1)

    def forward(self, x, t, v):
        x = x + self.t_conv(t) + self.v_conv(v)
        return self.conv(self.silu(self.bn(x)))

class FrustumTVUpBlock(nn.Module):
    def __init__(self, x_dim, t_dim, v_dim, out_dim):
        super().__init__()
        norm_act = lambda c: nn.GroupNorm(8, c)
        self.t_conv = nn.Conv3d(t_dim, x_dim, 1, 1) # 16
        self.v_conv = nn.Conv3d(v_dim, x_dim, 1, 1) # 16
        self.norm = norm_act(x_dim)
        self.silu = nn.SiLU(True)
        self.conv = nn.ConvTranspose3d(x_dim, out_dim, kernel_size=3, padding=1, output_padding=1, stride=2)

    def forward(self, x, t, v):
        x = x + self.t_conv(t) + self.v_conv(v)
        return self.conv(self.silu(self.norm(x)))

#对应View frustum volume，视锥体输出target view下图像的features
class FrustumTV3DNet(nn.Module):
    def __init__(self, in_dim, t_dim, v_dim, dims=(32, 64, 128, 256)):
        super().__init__()
        self.conv0 = nn.Conv3d(in_dim, dims[0], 3, 1, 1) # 32

        self.conv1 = FrustumTVBlock(dims[0], t_dim, v_dim, dims[1], 2)
        self.conv2 = FrustumTVBlock(dims[1], t_dim, v_dim, dims[1], 1)

        self.conv3 = FrustumTVBlock(dims[1], t_dim, v_dim, dims[2], 2)
        self.conv4 = FrustumTVBlock(dims[2], t_dim, v_dim, dims[2], 1)

        self.conv5 = FrustumTVBlock(dims[2], t_dim, v_dim, dims[3], 2)
        self.conv6 = FrustumTVBlock(dims[3], t_dim, v_dim, dims[3], 1)

        self.up0 = FrustumTVUpBlock(dims[3], t_dim, v_dim, dims[2])
        self.up1 = FrustumTVUpBlock(dims[2], t_dim, v_dim, dims[1])
        self.up2 = FrustumTVUpBlock(dims[1], t_dim, v_dim, dims[0])

    def forward(self, x, t, v):
        B,DT = t.shape
        t = t.view(B,DT,1,1,1)
        B,DV = v.shape
        v = v.view(B,DV,1,1,1)

        b, _, d, h, w = x.shape
        x0 = self.conv0(x)
        x1 = self.conv2(self.conv1(x0, t, v), t, v)
        x2 = self.conv4(self.conv3(x1, t, v), t, v)
        x3 = self.conv6(self.conv5(x2, t, v), t, v)

        x2 = self.up0(x3, t, v) + x2
        x1 = self.up1(x2, t, v) + x1
        x0 = self.up2(x1, t, v) + x0
        return {w: x0, w//2: x1, w//4: x2, w//8: x3}#还不是最终特征




class SpatialVolumeNet(nn.Module):
    def __init__(self, time_dim, view_dim, view_num,
                 input_image_size=256, frustum_volume_depth=48,
                 spatial_volume_size=32, spatial_volume_length=0.5,
                 frustum_volume_length=0.86603 # sqrt(3)/2
                 ):
        super().__init__()
        self.target_encoder = NoisyTargetViewEncoder(time_dim, view_dim, output_dim=16)
        self.spatial_volume_feats = SpatialTime3DNet(input_dim=16 * view_num, time_dim=time_dim, dims=(64, 128, 256, 512))
        self.frustum_volume_feats = FrustumTV3DNet(64, time_dim, view_dim, dims=(64, 128, 256, 512))

        self.frustum_volume_length = frustum_volume_length
        self.input_image_size = input_image_size
        self.spatial_volume_size = spatial_volume_size
        self.spatial_volume_length = spatial_volume_length

        self.frustum_volume_size = self.input_image_size // 8
        self.frustum_volume_depth = frustum_volume_depth
        self.time_dim = time_dim
        self.view_dim = view_dim
        self.default_origin_depth = 1.5 # our rendered images are 1.5 away from the origin, we assume camera is 1.5 away from the origin

    def construct_spatial_volume(self, x, t_embed, v_embed, target_poses, target_Ks):
        """
        @param x:            B,N,4,H,W
        @param t_embed:      B,t_dim
        @param v_embed:      B,N,v_dim
        @param target_poses: N,3,4
        @param target_Ks:    N,3,3
        @return:
        """
        B, N, _, H, W = x.shape
        V = self.spatial_volume_size
        device = x.device

        spatial_volume_verts = torch.linspace(-self.spatial_volume_length, self.spatial_volume_length, V, dtype=torch.float32, device=device)
        spatial_volume_verts = torch.stack(torch.meshgrid(spatial_volume_verts, spatial_volume_verts, spatial_volume_verts), -1)
        spatial_volume_verts = spatial_volume_verts.reshape(1, V ** 3, 3)[:, :, (2, 1, 0)]
        spatial_volume_verts = spatial_volume_verts.view(1, V, V, V, 3).permute(0, 4, 1, 2, 3).repeat(B, 1, 1, 1, 1)

        # encode source features
        t_embed_ = t_embed.view(B, 1, self.time_dim).repeat(1, N, 1).view(B, N, self.time_dim)
        # v_embed_ = v_embed.view(1, N, self.view_dim).repeat(B, 1, 1).view(B, N, self.view_dim)
        v_embed_ = v_embed
        target_Ks = target_Ks.unsqueeze(0).repeat(B, 1, 1, 1)
        target_poses = target_poses.unsqueeze(0).repeat(B, 1, 1, 1)

        # extract 2D image features
        spatial_volume_feats = []
        # project source features
        for ni in range(0, N):
            pose_source_ = target_poses[:, ni]
            K_source_ = target_Ks[:, ni]
            x_ = self.target_encoder(x[:, ni], t_embed_[:, ni], v_embed_[:, ni])
            C = x_.shape[1]

            coords_source = get_warp_coordinates(spatial_volume_verts, x_.shape[-1], self.input_image_size, K_source_, pose_source_).view(B, V, V * V, 2)
            unproj_feats_ = F.grid_sample(x_, coords_source, mode='bilinear', padding_mode='zeros', align_corners=True)
            unproj_feats_ = unproj_feats_.view(B, C, V, V, V)
            spatial_volume_feats.append(unproj_feats_)

        spatial_volume_feats = torch.stack(spatial_volume_feats, 1) # B,N,C,V,V,V
        N = spatial_volume_feats.shape[1]
        spatial_volume_feats = spatial_volume_feats.view(B, N*C, V, V, V)

        spatial_volume_feats = self.spatial_volume_feats(spatial_volume_feats, t_embed)  # b,64,32,32,32
        return spatial_volume_feats

    def construct_view_frustum_volume(self, spatial_volume, t_embed, v_embed, poses, Ks, target_indices):
        """
        @param spatial_volume:    B,C,V,V,V
        @param t_embed:           B,t_dim
        @param v_embed:           B,N,v_dim
        @param poses:             N,3,4
        @param Ks:                N,3,3
        @param target_indices:    B,TN
        @return: B*TN,C,H,W
        """
        B, TN = target_indices.shape
        H, W = self.frustum_volume_size, self.frustum_volume_size
        D = self.frustum_volume_depth
        V = self.spatial_volume_size

        near = torch.ones(B * TN, 1, H, W, dtype=spatial_volume.dtype, device=spatial_volume.device) * self.default_origin_depth - self.frustum_volume_length
        far = torch.ones(B * TN, 1, H, W, dtype=spatial_volume.dtype, device=spatial_volume.device) * self.default_origin_depth + self.frustum_volume_length

        target_indices = target_indices.view(B*TN) # B*TN
        poses_ = poses[target_indices] # B*TN,3,4
        Ks_ = Ks[target_indices] # B*TN,3,4
        volume_xyz, volume_depth = create_target_volume(D, self.frustum_volume_size, self.input_image_size, poses_, Ks_, near, far) # B*TN,3 or 1,D,H,W

        volume_xyz_ = volume_xyz / self.spatial_volume_length  # since the spatial volume is constructed in [-spatial_volume_length,spatial_volume_length]
        volume_xyz_ = volume_xyz_.permute(0, 2, 3, 4, 1)  # B*TN,D,H,W,3
        spatial_volume_ = spatial_volume.unsqueeze(1).repeat(1, TN, 1, 1, 1, 1).view(B * TN, -1, V, V, V)
        volume_feats = F.grid_sample(spatial_volume_, volume_xyz_, mode='bilinear', padding_mode='zeros', align_corners=True) # B*TN,C,D,H,W

        v_embed_ = v_embed[torch.arange(B)[:,None], target_indices.view(B,TN)].view(B*TN, -1) # B*TN
        t_embed_ = t_embed.unsqueeze(1).repeat(1,TN,1).view(B*TN,-1)
        volume_feats_dict = self.frustum_volume_feats(volume_feats, t_embed_, v_embed_)
        return volume_feats_dict, volume_depth

