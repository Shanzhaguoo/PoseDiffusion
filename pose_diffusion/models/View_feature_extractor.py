from pathlib import Path

import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from skimage.io import imsave
from torch.optim.lr_scheduler import LambdaLR
from tqdm import tqdm

from models.View_feature_extractor_net import NoisyTargetViewEncoder, SpatialTime3DNet, FrustumTV3DNet

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

class SyncMultiviewDiffusion(pl.LightningModule):
    def __init__(self, unet_config, scheduler_config,
                 finetune_unet=False, finetune_projection=True,
                 view_num=16, image_size=256,
                 cfg_scale=3.0, output_num=8, batch_view_num=4,
                 drop_conditions=False, drop_scheme='default',
                 clip_image_encoder_path="/apdcephfs/private_rondyliu/projects/clip/ViT-L-14.pt",
                 sample_type='ddim', sample_steps=200):
        super().__init__()

        self.finetune_unet = finetune_unet
        self.finetune_projection = finetune_projection

        self.view_num = view_num
        self.viewpoint_dim = 4
        self.output_num = output_num
        self.image_size = image_size

        self.batch_view_num = batch_view_num
        self.cfg_scale = cfg_scale

        self.clip_image_encoder_path = clip_image_encoder_path

        self._init_time_step_embedding()
        self._init_first_stage()
        self._init_schedule()
        self._init_multiview()
        self._init_clip_image_encoder()
        self._init_clip_projection()

        self.spatial_volume = SpatialVolumeNet(self.time_embed_dim, self.viewpoint_dim, self.view_num)
        self.model = UNetWrapper(unet_config, drop_conditions=drop_conditions, drop_scheme=drop_scheme)
        self.scheduler_config = scheduler_config

        latent_size = image_size//8
        if sample_type=='ddim':
            self.sampler = SyncDDIMSampler(self, sample_steps , "uniform", 1.0, latent_size=latent_size)
        else:
            raise NotImplementedError

    def _init_clip_projection(self):
        self.cc_projection = nn.Linear(772, 768)
        nn.init.eye_(list(self.cc_projection.parameters())[0][:768, :768])
        nn.init.zeros_(list(self.cc_projection.parameters())[1])
        self.cc_projection.requires_grad_(True)

        if not self.finetune_projection:
            disable_training_module(self.cc_projection)

    def _init_multiview(self):
        K, azs, _, _, poses = read_pickle(f'meta_info/camera-{self.view_num}.pkl')
        default_image_size = 256
        ratio = self.image_size/default_image_size
        K = np.diag([ratio,ratio,1]) @ K
        K = torch.from_numpy(K.astype(np.float32)) # [3,3]
        K = K.unsqueeze(0).repeat(self.view_num,1,1)        # N,3,3
        poses = torch.from_numpy(poses.astype(np.float32))  # N,3,4
        self.register_buffer('poses', poses)
        self.register_buffer('Ks', K)
        azs = (azs + np.pi) % (np.pi * 2) - np.pi # scale to [-pi,pi] and the index=0 has az=0
        self.register_buffer('azimuth', torch.from_numpy(azs.astype(np.float32)))

    #描述图像在不同视点下的相对位置和方向的嵌入
    def get_viewpoint_embedding(self, batch_size, elevation_ref):
        """
        @param batch_size:
        @param elevation_ref: B
        @return:
        """
        azimuth_input = self.azimuth[0].unsqueeze(0) # 1
        azimuth_target = self.azimuth # N
        elevation_input = -elevation_ref # note that zero123 use a negative elevation here!!!
        elevation_target = -np.deg2rad(30)
        d_e = elevation_target - elevation_input # B
        N = self.azimuth.shape[0]
        B = batch_size
        d_e = d_e.unsqueeze(1).repeat(1, N)
        d_a = azimuth_target - azimuth_input # N
        d_a = d_a.unsqueeze(0).repeat(B, 1)
        d_z = torch.zeros_like(d_a)
        embedding = torch.stack([d_e, torch.sin(d_a), torch.cos(d_a), d_z], -1) # B,N,4
        return embedding

    def _init_first_stage(self):
        first_stage_config={
            "target": "ldm.models.autoencoder.AutoencoderKL",
            "params": {
                "embed_dim": 4,
                "monitor": "val/rec_loss",
                "ddconfig":{
                  "double_z": True,
                  "z_channels": 4,
                  "resolution": self.image_size,
                  "in_channels": 3,
                  "out_ch": 3,
                  "ch": 128,
                  "ch_mult": [1,2,4,4],
                  "num_res_blocks": 2,
                  "attn_resolutions": [],
                  "dropout": 0.0
                },
                "lossconfig": {"target": "torch.nn.Identity"},
            }
        }
        self.first_stage_scale_factor = 0.18215
        self.first_stage_model = instantiate_from_config(first_stage_config)
        self.first_stage_model = disable_training_module(self.first_stage_model)

    def _init_clip_image_encoder(self):
        self.clip_image_encoder = FrozenCLIPImageEmbedder(model=self.clip_image_encoder_path)
        self.clip_image_encoder = disable_training_module(self.clip_image_encoder)

    def _init_schedule(self):
        self.num_timesteps = 1000
        linear_start = 0.00085
        linear_end = 0.0120
        num_timesteps = 1000
        betas = torch.linspace(linear_start ** 0.5, linear_end ** 0.5, num_timesteps, dtype=torch.float32) ** 2 # T
        assert betas.shape[0] == self.num_timesteps

        # all in float64 first
        alphas = 1. - betas
        alphas_cumprod = torch.cumprod(alphas, dim=0) # T
        alphas_cumprod_prev = torch.cat([torch.ones(1, dtype=torch.float64), alphas_cumprod[:-1]], 0)
        posterior_variance = betas * (1. - alphas_cumprod_prev) / (1. - alphas_cumprod) # T
        posterior_log_variance_clipped = torch.log(torch.clamp(posterior_variance, min=1e-20))
        posterior_log_variance_clipped = torch.clamp(posterior_log_variance_clipped, min=-10)

        self.register_buffer("betas", betas.float())
        self.register_buffer("alphas", alphas.float())
        self.register_buffer("alphas_cumprod", alphas_cumprod.float())
        self.register_buffer("sqrt_alphas_cumprod", torch.sqrt(alphas_cumprod).float())
        self.register_buffer("sqrt_one_minus_alphas_cumprod", torch.sqrt(1 - alphas_cumprod).float())
        self.register_buffer("posterior_variance", posterior_variance.float())
        self.register_buffer('posterior_log_variance_clipped', posterior_log_variance_clipped.float())

    def _init_time_step_embedding(self):
        self.time_embed_dim = 256
        self.time_embed = nn.Sequential(
            nn.Linear(self.time_embed_dim, self.time_embed_dim),
            nn.SiLU(True),
            nn.Linear(self.time_embed_dim, self.time_embed_dim),
        )

    def encode_first_stage(self, x, sample=True):
        with torch.no_grad():
            posterior = self.first_stage_model.encode(x)  # b,4,h//8,w//8
            if sample:
                return posterior.sample().detach() * self.first_stage_scale_factor
            else:
                return posterior.mode().detach() * self.first_stage_scale_factor

    def decode_first_stage(self, z):
        with torch.no_grad():
            z = 1. / self.first_stage_scale_factor * z
            return self.first_stage_model.decode(z)

    def prepare(self, batch):
        # encode target
        if 'target_image' in batch:
            image_target = batch['target_image'].permute(0, 1, 4, 2, 3) # b,n,3,h,w
            N = image_target.shape[1]
            x = [self.encode_first_stage(image_target[:,ni], True) for ni in range(N)]
            x = torch.stack(x, 1) # b,n,4,h//8,w//8
        else:
            x = None

        image_input = batch['input_image'].permute(0, 3, 1, 2)
        elevation_input = batch['input_elevation'][:, 0] # b
        x_input = self.encode_first_stage(image_input)
        input_info = {'image': image_input, 'elevation': elevation_input, 'x': x_input}
        with torch.no_grad():
            clip_embed = self.clip_image_encoder.encode(image_input)
        return x, clip_embed, input_info

    def embed_time(self, t):
        t_embed = timestep_embedding(t, self.time_embed_dim, repeat_only=False) # B,TED
        t_embed = self.time_embed(t_embed) # B,TED
        return t_embed

    def get_target_view_feats(self, x_input, spatial_volume, clip_embed, t_embed, v_embed, target_index):
        """
        @param x_input:        B,4,H,W
        @param spatial_volume: B,C,V,V,V
        @param clip_embed:     B,1,768
        @param t_embed:        B,t_dim
        @param v_embed:        B,N,v_dim
        @param target_index:   B,TN
        @return:
            tensors of size B*TN,*
        """
        B, _, H, W = x_input.shape
        frustum_volume_feats, frustum_volume_depth = self.spatial_volume.construct_view_frustum_volume(spatial_volume, t_embed, v_embed, self.poses, self.Ks, target_index)

        # clip
        TN = target_index.shape[1]
        v_embed_ = v_embed[torch.arange(B)[:,None], target_index].view(B*TN, self.viewpoint_dim) # B*TN,v_dim
        clip_embed_ = clip_embed.unsqueeze(1).repeat(1,TN,1,1).view(B*TN,1,768)
        clip_embed_ = self.cc_projection(torch.cat([clip_embed_, v_embed_.unsqueeze(1)], -1))  # B*TN,1,768

        x_input_ = x_input.unsqueeze(1).repeat(1, TN, 1, 1, 1).view(B * TN, 4, H, W)

        x_concat = x_input_
        return clip_embed_, frustum_volume_feats, x_concat

    def training_step(self, batch):
        B = batch['target_image'].shape[0]
        time_steps = torch.randint(0, self.num_timesteps, (B,), device=self.device).long()

        x, clip_embed, input_info = self.prepare(batch)
        x_noisy, noise = self.add_noise(x, time_steps)  # B,N,4,H,W

        N = self.view_num
        target_index = torch.randint(0, N, (B, 1), device=self.device).long() # B, 1
        v_embed = self.get_viewpoint_embedding(B, input_info['elevation']) # N,v_dim

        t_embed = self.embed_time(time_steps)
        spatial_volume = self.spatial_volume.construct_spatial_volume(x_noisy, t_embed, v_embed, self.poses, self.Ks)

        clip_embed, volume_feats, x_concat = self.get_target_view_feats(input_info['x'], spatial_volume, clip_embed, t_embed, v_embed, target_index)

        x_noisy_ = x_noisy[torch.arange(B)[:,None],target_index][:,0] # B,4,H,W
        noise_predict = self.model(x_noisy_, time_steps, clip_embed, volume_feats, x_concat, is_train=True) # B,4,H,W

        noise_target = noise[torch.arange(B)[:,None],target_index][:,0] # B,4,H,W
        # loss simple for diffusion
        loss_simple = torch.nn.functional.mse_loss(noise_target, noise_predict, reduction='none')
        loss = loss_simple.mean()
        self.log('sim', loss_simple.mean(), prog_bar=True, logger=True, on_step=True, on_epoch=True, rank_zero_only=True)

        # log others
        lr = self.optimizers().param_groups[0]['lr']
        self.log('lr', lr, prog_bar=True, logger=True, on_step=True, on_epoch=False, rank_zero_only=True)
        self.log("step", self.global_step, prog_bar=True, logger=True, on_step=True, on_epoch=False, rank_zero_only=True)
        return loss

    def add_noise(self, x_start, t):
        """
        @param x_start: B,*
        @param t:       B,
        @return:
        """
        B = x_start.shape[0]
        noise = torch.randn_like(x_start) # B,*

        sqrt_alphas_cumprod_  = self.sqrt_alphas_cumprod[t] # B,
        sqrt_one_minus_alphas_cumprod_ = self.sqrt_one_minus_alphas_cumprod[t] # B
        sqrt_alphas_cumprod_ = sqrt_alphas_cumprod_.view(B, *[1 for _ in range(len(x_start.shape)-1)])
        sqrt_one_minus_alphas_cumprod_ = sqrt_one_minus_alphas_cumprod_.view(B, *[1 for _ in range(len(x_start.shape)-1)])
        x_noisy = sqrt_alphas_cumprod_ * x_start + sqrt_one_minus_alphas_cumprod_ * noise
        return x_noisy, noise

    def sample(self, sampler, batch, cfg_scale, batch_view_num, return_inter_results=False, inter_interval=50, inter_view_interval=2):
        _, clip_embed, input_info = self.prepare(batch)
        x_sample, inter = sampler.sample(input_info, clip_embed, unconditional_scale=cfg_scale, log_every_t=inter_interval, batch_view_num=batch_view_num)

        N = x_sample.shape[1]
        x_sample = torch.stack([self.decode_first_stage(x_sample[:, ni]) for ni in range(N)], 1)
        if return_inter_results:
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            inter = torch.stack(inter['x_inter'], 2) # # B,N,T,C,H,W
            B,N,T,C,H,W = inter.shape
            inter_results = []
            for ni in tqdm(range(0, N, inter_view_interval)):
                inter_results_ = []
                for ti in range(T):
                    inter_results_.append(self.decode_first_stage(inter[:, ni, ti]))
                inter_results.append(torch.stack(inter_results_, 1)) # B,T,3,H,W
            inter_results = torch.stack(inter_results,1) # B,N,T,3,H,W
            return x_sample, inter_results
        else:
            return x_sample

    def log_image(self,  x_sample, batch, step, output_dir):
        process = lambda x: ((torch.clip(x, min=-1, max=1).cpu().numpy() * 0.5 + 0.5) * 255).astype(np.uint8)
        B = x_sample.shape[0]
        N = x_sample.shape[1]
        image_cond = []
        for bi in range(B):
            img_pr_ = concat_images_list(process(batch['input_image'][bi]),*[process(x_sample[bi, ni].permute(1, 2, 0)) for ni in range(N)])
            image_cond.append(img_pr_)

        output_dir = Path(output_dir)
        imsave(str(output_dir/f'{step}.jpg'), concat_images_list(*image_cond, vert=True))

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        if batch_idx==0 and self.global_rank==0:
            self.eval()
            step = self.global_step
            batch_ = {}
            for k, v in batch.items(): batch_[k] = v[:self.output_num]
            x_sample = self.sample(self.sampler, batch_, self.cfg_scale, self.batch_view_num)
            output_dir = Path(self.image_dir) / 'images' / 'val'
            output_dir.mkdir(exist_ok=True, parents=True)
            self.log_image(x_sample, batch, step, output_dir=output_dir)

    def configure_optimizers(self):
        lr = self.learning_rate
        print(f'setting learning rate to {lr:.4f} ...')
        paras = []
        if self.finetune_projection:
            paras.append({"params": self.cc_projection.parameters(), "lr": lr},)
        if self.finetune_unet:
            paras.append({"params": self.model.parameters(), "lr": lr},)
        else:
            paras.append({"params": self.model.get_trainable_parameters(), "lr": lr},)

        paras.append({"params": self.time_embed.parameters(), "lr": lr*10.0},)
        paras.append({"params": self.spatial_volume.parameters(), "lr": lr*10.0},)

        opt = torch.optim.AdamW(paras, lr=lr)

        scheduler = instantiate_from_config(self.scheduler_config)
        print("Setting up LambdaLR scheduler...")
        scheduler = [{'scheduler': LambdaLR(opt, lr_lambda=scheduler.schedule), 'interval': 'step', 'frequency': 1}]
        return [opt], scheduler


class SyncMultiviewDiffusion(pl.LightningModule):
    def __init__(self, unet_config, scheduler_config,
                 finetune_unet=False, finetune_projection=True,
                 view_num=16, image_size=256,
                 cfg_scale=3.0, output_num=8, batch_view_num=4,
                 drop_conditions=False, drop_scheme='default',
                 clip_image_encoder_path="/apdcephfs/private_rondyliu/projects/clip/ViT-L-14.pt",
                 sample_type='ddim', sample_steps=200):
        super().__init__()

        self.finetune_unet = finetune_unet
        self.finetune_projection = finetune_projection

        self.view_num = view_num
        self.viewpoint_dim = 4
        self.output_num = output_num
        self.image_size = image_size

        self.batch_view_num = batch_view_num
        self.cfg_scale = cfg_scale

        self.clip_image_encoder_path = clip_image_encoder_path

        self._init_time_step_embedding()
        self._init_first_stage()
        self._init_schedule()
        self._init_multiview()
        self._init_clip_image_encoder()
        self._init_clip_projection()

        self.spatial_volume = SpatialVolumeNet(self.time_embed_dim, self.viewpoint_dim, self.view_num)
        self.model = UNetWrapper(unet_config, drop_conditions=drop_conditions, drop_scheme=drop_scheme)
        self.scheduler_config = scheduler_config

        latent_size = image_size//8
        if sample_type=='ddim':
            self.sampler = SyncDDIMSampler(self, sample_steps , "uniform", 1.0, latent_size=latent_size)
        else:
            raise NotImplementedError

    def _init_clip_projection(self):
        self.cc_projection = nn.Linear(772, 768)
        nn.init.eye_(list(self.cc_projection.parameters())[0][:768, :768])
        nn.init.zeros_(list(self.cc_projection.parameters())[1])
        self.cc_projection.requires_grad_(True)

        if not self.finetune_projection:
            disable_training_module(self.cc_projection)

    def _init_multiview(self):
        K, azs, _, _, poses = read_pickle(f'meta_info/camera-{self.view_num}.pkl')
        default_image_size = 256
        ratio = self.image_size/default_image_size
        K = np.diag([ratio,ratio,1]) @ K
        K = torch.from_numpy(K.astype(np.float32)) # [3,3]
        K = K.unsqueeze(0).repeat(self.view_num,1,1)        # N,3,3
        poses = torch.from_numpy(poses.astype(np.float32))  # N,3,4
        self.register_buffer('poses', poses)
        self.register_buffer('Ks', K)
        azs = (azs + np.pi) % (np.pi * 2) - np.pi # scale to [-pi,pi] and the index=0 has az=0
        self.register_buffer('azimuth', torch.from_numpy(azs.astype(np.float32)))

    #描述图像在不同视点下的相对位置和方向的嵌入
    def get_viewpoint_embedding(self, batch_size, elevation_ref):
        """
        @param batch_size:
        @param elevation_ref: B
        @return:
        """
        azimuth_input = self.azimuth[0].unsqueeze(0) # 1
        azimuth_target = self.azimuth # N
        elevation_input = -elevation_ref # note that zero123 use a negative elevation here!!!
        elevation_target = -np.deg2rad(30)
        d_e = elevation_target - elevation_input # B
        N = self.azimuth.shape[0]
        B = batch_size
        d_e = d_e.unsqueeze(1).repeat(1, N)
        d_a = azimuth_target - azimuth_input # N
        d_a = d_a.unsqueeze(0).repeat(B, 1)
        d_z = torch.zeros_like(d_a)
        embedding = torch.stack([d_e, torch.sin(d_a), torch.cos(d_a), d_z], -1) # B,N,4
        return embedding

    def _init_first_stage(self):
        first_stage_config={
            "target": "ldm.models.autoencoder.AutoencoderKL",
            "params": {
                "embed_dim": 4,
                "monitor": "val/rec_loss",
                "ddconfig":{
                  "double_z": True,
                  "z_channels": 4,
                  "resolution": self.image_size,
                  "in_channels": 3,
                  "out_ch": 3,
                  "ch": 128,
                  "ch_mult": [1,2,4,4],
                  "num_res_blocks": 2,
                  "attn_resolutions": [],
                  "dropout": 0.0
                },
                "lossconfig": {"target": "torch.nn.Identity"},
            }
        }
        self.first_stage_scale_factor = 0.18215
        self.first_stage_model = instantiate_from_config(first_stage_config)
        self.first_stage_model = disable_training_module(self.first_stage_model)

    def _init_clip_image_encoder(self):
        self.clip_image_encoder = FrozenCLIPImageEmbedder(model=self.clip_image_encoder_path)
        self.clip_image_encoder = disable_training_module(self.clip_image_encoder)

    def _init_schedule(self):
        self.num_timesteps = 1000
        linear_start = 0.00085
        linear_end = 0.0120
        num_timesteps = 1000
        betas = torch.linspace(linear_start ** 0.5, linear_end ** 0.5, num_timesteps, dtype=torch.float32) ** 2 # T
        assert betas.shape[0] == self.num_timesteps

        # all in float64 first
        alphas = 1. - betas
        alphas_cumprod = torch.cumprod(alphas, dim=0) # T
        alphas_cumprod_prev = torch.cat([torch.ones(1, dtype=torch.float64), alphas_cumprod[:-1]], 0)
        posterior_variance = betas * (1. - alphas_cumprod_prev) / (1. - alphas_cumprod) # T
        posterior_log_variance_clipped = torch.log(torch.clamp(posterior_variance, min=1e-20))
        posterior_log_variance_clipped = torch.clamp(posterior_log_variance_clipped, min=-10)

        self.register_buffer("betas", betas.float())
        self.register_buffer("alphas", alphas.float())
        self.register_buffer("alphas_cumprod", alphas_cumprod.float())
        self.register_buffer("sqrt_alphas_cumprod", torch.sqrt(alphas_cumprod).float())
        self.register_buffer("sqrt_one_minus_alphas_cumprod", torch.sqrt(1 - alphas_cumprod).float())
        self.register_buffer("posterior_variance", posterior_variance.float())
        self.register_buffer('posterior_log_variance_clipped', posterior_log_variance_clipped.float())

    def _init_time_step_embedding(self):
        self.time_embed_dim = 256
        self.time_embed = nn.Sequential(
            nn.Linear(self.time_embed_dim, self.time_embed_dim),
            nn.SiLU(True),
            nn.Linear(self.time_embed_dim, self.time_embed_dim),
        )

    def encode_first_stage(self, x, sample=True):
        with torch.no_grad():
            posterior = self.first_stage_model.encode(x)  # b,4,h//8,w//8
            if sample:
                return posterior.sample().detach() * self.first_stage_scale_factor
            else:
                return posterior.mode().detach() * self.first_stage_scale_factor

    def decode_first_stage(self, z):
        with torch.no_grad():
            z = 1. / self.first_stage_scale_factor * z
            return self.first_stage_model.decode(z)

    def prepare(self, batch):
        # encode target
        if 'target_image' in batch:
            image_target = batch['target_image'].permute(0, 1, 4, 2, 3) # b,n,3,h,w
            N = image_target.shape[1]
            x = [self.encode_first_stage(image_target[:,ni], True) for ni in range(N)]
            x = torch.stack(x, 1) # b,n,4,h//8,w//8
        else:
            x = None

        image_input = batch['input_image'].permute(0, 3, 1, 2)
        elevation_input = batch['input_elevation'][:, 0] # b
        x_input = self.encode_first_stage(image_input)
        input_info = {'image': image_input, 'elevation': elevation_input, 'x': x_input}
        with torch.no_grad():
            clip_embed = self.clip_image_encoder.encode(image_input)
        return x, clip_embed, input_info

    def embed_time(self, t):
        t_embed = timestep_embedding(t, self.time_embed_dim, repeat_only=False) # B,TED
        t_embed = self.time_embed(t_embed) # B,TED
        return t_embed

    def get_target_view_feats(self, x_input, spatial_volume, clip_embed, t_embed, v_embed, target_index):
        """
        @param x_input:        B,4,H,W
        @param spatial_volume: B,C,V,V,V
        @param clip_embed:     B,1,768
        @param t_embed:        B,t_dim
        @param v_embed:        B,N,v_dim
        @param target_index:   B,TN
        @return:
            tensors of size B*TN,*
        """
        B, _, H, W = x_input.shape
        frustum_volume_feats, frustum_volume_depth = self.spatial_volume.construct_view_frustum_volume(spatial_volume, t_embed, v_embed, self.poses, self.Ks, target_index)

        # clip
        TN = target_index.shape[1]
        v_embed_ = v_embed[torch.arange(B)[:,None], target_index].view(B*TN, self.viewpoint_dim) # B*TN,v_dim
        clip_embed_ = clip_embed.unsqueeze(1).repeat(1,TN,1,1).view(B*TN,1,768)
        clip_embed_ = self.cc_projection(torch.cat([clip_embed_, v_embed_.unsqueeze(1)], -1))  # B*TN,1,768

        x_input_ = x_input.unsqueeze(1).repeat(1, TN, 1, 1, 1).view(B * TN, 4, H, W)

        x_concat = x_input_
        return clip_embed_, frustum_volume_feats, x_concat

    def training_step(self, batch):
        B = batch['target_image'].shape[0]
        time_steps = torch.randint(0, self.num_timesteps, (B,), device=self.device).long()

        x, clip_embed, input_info = self.prepare(batch)
        x_noisy, noise = self.add_noise(x, time_steps)  # B,N,4,H,W

        N = self.view_num
        target_index = torch.randint(0, N, (B, 1), device=self.device).long() # B, 1
        v_embed = self.get_viewpoint_embedding(B, input_info['elevation']) # N,v_dim

        t_embed = self.embed_time(time_steps)
        spatial_volume = self.spatial_volume.construct_spatial_volume(x_noisy, t_embed, v_embed, self.poses, self.Ks)

        clip_embed, volume_feats, x_concat = self.get_target_view_feats(input_info['x'], spatial_volume, clip_embed, t_embed, v_embed, target_index)

        x_noisy_ = x_noisy[torch.arange(B)[:,None],target_index][:,0] # B,4,H,W
        noise_predict = self.model(x_noisy_, time_steps, clip_embed, volume_feats, x_concat, is_train=True) # B,4,H,W

        noise_target = noise[torch.arange(B)[:,None],target_index][:,0] # B,4,H,W
        # loss simple for diffusion
        loss_simple = torch.nn.functional.mse_loss(noise_target, noise_predict, reduction='none')
        loss = loss_simple.mean()
        self.log('sim', loss_simple.mean(), prog_bar=True, logger=True, on_step=True, on_epoch=True, rank_zero_only=True)

        # log others
        lr = self.optimizers().param_groups[0]['lr']
        self.log('lr', lr, prog_bar=True, logger=True, on_step=True, on_epoch=False, rank_zero_only=True)
        self.log("step", self.global_step, prog_bar=True, logger=True, on_step=True, on_epoch=False, rank_zero_only=True)
        return loss

    def add_noise(self, x_start, t):
        """
        @param x_start: B,*
        @param t:       B,
        @return:
        """
        B = x_start.shape[0]
        noise = torch.randn_like(x_start) # B,*

        sqrt_alphas_cumprod_  = self.sqrt_alphas_cumprod[t] # B,
        sqrt_one_minus_alphas_cumprod_ = self.sqrt_one_minus_alphas_cumprod[t] # B
        sqrt_alphas_cumprod_ = sqrt_alphas_cumprod_.view(B, *[1 for _ in range(len(x_start.shape)-1)])
        sqrt_one_minus_alphas_cumprod_ = sqrt_one_minus_alphas_cumprod_.view(B, *[1 for _ in range(len(x_start.shape)-1)])
        x_noisy = sqrt_alphas_cumprod_ * x_start + sqrt_one_minus_alphas_cumprod_ * noise
        return x_noisy, noise

    def sample(self, sampler, batch, cfg_scale, batch_view_num, return_inter_results=False, inter_interval=50, inter_view_interval=2):
        _, clip_embed, input_info = self.prepare(batch)
        x_sample, inter = sampler.sample(input_info, clip_embed, unconditional_scale=cfg_scale, log_every_t=inter_interval, batch_view_num=batch_view_num)

        N = x_sample.shape[1]
        x_sample = torch.stack([self.decode_first_stage(x_sample[:, ni]) for ni in range(N)], 1)
        if return_inter_results:
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            inter = torch.stack(inter['x_inter'], 2) # # B,N,T,C,H,W
            B,N,T,C,H,W = inter.shape
            inter_results = []
            for ni in tqdm(range(0, N, inter_view_interval)):
                inter_results_ = []
                for ti in range(T):
                    inter_results_.append(self.decode_first_stage(inter[:, ni, ti]))
                inter_results.append(torch.stack(inter_results_, 1)) # B,T,3,H,W
            inter_results = torch.stack(inter_results,1) # B,N,T,3,H,W
            return x_sample, inter_results
        else:
            return x_sample

    def log_image(self,  x_sample, batch, step, output_dir):
        process = lambda x: ((torch.clip(x, min=-1, max=1).cpu().numpy() * 0.5 + 0.5) * 255).astype(np.uint8)
        B = x_sample.shape[0]
        N = x_sample.shape[1]
        image_cond = []
        for bi in range(B):
            img_pr_ = concat_images_list(process(batch['input_image'][bi]),*[process(x_sample[bi, ni].permute(1, 2, 0)) for ni in range(N)])
            image_cond.append(img_pr_)

        output_dir = Path(output_dir)
        imsave(str(output_dir/f'{step}.jpg'), concat_images_list(*image_cond, vert=True))

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        if batch_idx==0 and self.global_rank==0:
            self.eval()
            step = self.global_step
            batch_ = {}
            for k, v in batch.items(): batch_[k] = v[:self.output_num]
            x_sample = self.sample(self.sampler, batch_, self.cfg_scale, self.batch_view_num)
            output_dir = Path(self.image_dir) / 'images' / 'val'
            output_dir.mkdir(exist_ok=True, parents=True)
            self.log_image(x_sample, batch, step, output_dir=output_dir)

    def configure_optimizers(self):
        lr = self.learning_rate
        print(f'setting learning rate to {lr:.4f} ...')
        paras = []
        if self.finetune_projection:
            paras.append({"params": self.cc_projection.parameters(), "lr": lr},)
        if self.finetune_unet:
            paras.append({"params": self.model.parameters(), "lr": lr},)
        else:
            paras.append({"params": self.model.get_trainable_parameters(), "lr": lr},)

        paras.append({"params": self.time_embed.parameters(), "lr": lr*10.0},)
        paras.append({"params": self.spatial_volume.parameters(), "lr": lr*10.0},)

        opt = torch.optim.AdamW(paras, lr=lr)

        scheduler = instantiate_from_config(self.scheduler_config)
        print("Setting up LambdaLR scheduler...")
        scheduler = [{'scheduler': LambdaLR(opt, lr_lambda=scheduler.schedule), 'interval': 'step', 'frequency': 1}]
        return [opt], scheduler

