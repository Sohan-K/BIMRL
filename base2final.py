import gym
import warnings
import numpy as np
import torch
from torch.nn import functional as F
from utils import helpers as utl
from models.decoder import StateTransitionDecoder, RewardDecoder, TaskDecoder, ValueDecoder, ActionDecoder
from brim_core.brim_core import BRIMCore
from utils.storage_vae import RolloutStorageVAE
from utils.helpers import get_task_dim, get_num_tasks, get_latent_for_policy
from utils.helpers import MinigridMLPTargetEmbeddingNet
from utils.helpers import MinigridMLPEmbeddingNet


device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def compute_memory_loss():
    return


def compute_returns(next_value, rewards, value_preds, returns, gamma, tau, use_gae, masks, bad_masks, use_proper_time_limits):
    if use_proper_time_limits:
        if use_gae:
            value_preds[-1] = next_value
            gae = 0
            for step in reversed(range(rewards.size(0))):
                delta = rewards[step] + gamma * value_preds[step + 1] * masks[step + 1] - value_preds[step]
                gae = delta + gamma * tau * masks[step + 1] * gae
                gae = gae * bad_masks[step + 1]
                returns[step] = gae + value_preds[step]
        else:
            returns[-1] = next_value
            for step in reversed(range(rewards.size(0))):
                returns[step] = (returns[step + 1] * gamma * masks[step + 1] + rewards[step]) * bad_masks[
                    step + 1] + (1 - bad_masks[step + 1]) * value_preds[step]
    else:
        if use_gae:
            value_preds[-1] = next_value
            gae = 0
            for step in reversed(range(rewards.size(0))):
                delta = rewards[step] + gamma * value_preds[step + 1] * masks[step + 1] - value_preds[step]
                gae = delta + gamma * tau * masks[step + 1] * gae
                returns[step] = gae + value_preds[step]
        else:
            returns[-1] = next_value
            for step in reversed(range(rewards.size(0))):
                returns[step] = returns[step + 1] * gamma * masks[step + 1] + rewards[step]


def compute_loss_action(action_pred, action):
    action = action.long()
    action = action.reshape(*action.shape[:-1])
    action = action.reshape((action.shape[2], action.shape[0], action.shape[1]))
    action_pred = action_pred.reshape((action_pred.shape[2], action_pred.shape[3], action_pred.shape[0], action_pred.shape[1]))
    criterion = torch.nn.NLLLoss(reduction='none')
    loss = criterion(action_pred, action)
    loss = loss.reshape((loss.shape[1], loss.shape[2], loss.shape[0]))
    return loss


def compute_loss_value(values, value_preds, return_batch, n_step_v_loss, clip_param=0.2):
    if n_step_v_loss == 'huber':
        value_pred_clipped = value_preds + (values - value_preds).clamp(-clip_param, clip_param)
        value_losses = F.smooth_l1_loss(values, return_batch, reduction='none')
        value_losses_clipped = F.smooth_l1_loss(value_pred_clipped, return_batch, reduction='none')
        value_loss = 0.5 * torch.max(value_losses, value_losses_clipped).mean(dim=-1)
    elif n_step_v_loss == 'norm2_ret':
        value_loss = F.mse_loss(values, return_batch, reduction='none').mean(dim=-1)
    elif n_step_v_loss == 'norm2_val':
        value_loss = F.mse_loss(values, value_preds, reduction='none').mean(dim=-1)
    else:
        raise NotImplementedError
    return value_loss


def compute_loss_state(state_pred, next_obs, state_pred_type):
    if state_pred_type == 'deterministic':
        loss_state = (state_pred - next_obs).pow(2).mean(dim=-1)
    elif state_pred_type == 'gaussian':
        state_pred_mean = state_pred[:, :state_pred.shape[1] // 2]
        state_pred_std = torch.exp(0.5 * state_pred[:, state_pred.shape[1] // 2:])
        m = torch.distributions.normal.Normal(state_pred_mean, state_pred_std)
        loss_state = -m.log_prob(next_obs).mean(dim=-1)
    else:
        raise NotImplementedError
    return loss_state


def compute_loss_reward(rew_pred, reward, rew_pred_type):
    if rew_pred_type == 'categorical':
        rew_pred = F.softmax(rew_pred, dim=-1)
    elif rew_pred_type == 'bernoulli':
        rew_pred = torch.sigmoid(rew_pred)

    rew_target = (reward == 1).float()
    if rew_pred_type == 'deterministic':
        loss_rew = (rew_pred - reward).pow(2).mean(dim=-1)
    elif rew_pred_type in ['categorical', 'bernoulli']:
        loss_rew = F.binary_cross_entropy(rew_pred, rew_target, reduction='none').mean(dim=-1)
    else:
        raise NotImplementedError
    return loss_rew


def avg_loss(state_reconstruction_loss, vae_avg_elbo_terms, vae_avg_reconstruction_terms):
    # avg/sum across individual ELBO terms
    if vae_avg_elbo_terms:
        state_reconstruction_loss = state_reconstruction_loss.mean(dim=0)
    else:
        state_reconstruction_loss = state_reconstruction_loss.sum(dim=0)
    # avg/sum across individual reconstruction terms
    if vae_avg_reconstruction_terms:
        state_reconstruction_loss = state_reconstruction_loss.mean(dim=0)
    else:
        state_reconstruction_loss = state_reconstruction_loss.sum(dim=0)
    # average across tasks
    state_reconstruction_loss = state_reconstruction_loss.mean()
    return state_reconstruction_loss


class Base2Final:
    """
    VAE of VariBAD:
    - has an encoder and decoder
    - can compute the ELBO loss
    - can update the VAE (encoder+decoder)
    """
    def __init__(self, args, logger, get_iter_idx, exploration_num_processes, exploitation_num_processes):

        self.args = args
        self.logger = logger
        self.get_iter_idx = get_iter_idx
        self.task_dim = get_task_dim(self.args)
        self.num_tasks = get_num_tasks(self.args)

        memory_params = \
            self.args.use_hebb,\
            self.args.use_gen,\
            self.args.read_num_head,\
            self.args.combination_num_head, \
            self.args.memory_state_embedding + 2*self.args.task_inference_latent_dim,\
            self.args.rim_level1_output_dim,\
            self.args.max_trajectory_len,\
            self.args.max_rollouts_per_task,\
            self.args.w_max,\
            self.args.memory_state_embedding,\
            self.args.general_key_encoder_layer,\
            self.args.general_value_encoder_layer,\
            self.args.general_query_encoder_layer,\
            self.args.episodic_key_encoder_layer,\
            self.args.episodic_value_encoder_layer,\
            self.args.hebbian_key_encoder_layer,\
            self.args.hebbian_value_encoder_layer,\
            self.args.state_dim,\
            self.args.rim_query_size,\
            self.args.rim_hidden_state_to_query_layers,\
            self.args.read_memory_to_value_layer,\
            self.args.read_memory_to_key_layer, \
            self.args.hebb_learning_rate

        self.brim_core = self.initialise_brim_core(memory_params=memory_params)

        # initialise the decoders (returns None for unused decoders)
        self.state_decoder, self.reward_decoder, self.task_decoder, self.exploration_value_decoder, self.exploitation_value_decoder, self.action_decoder = self.initialise_decoder()

        if self.args.bebold_intrinsic_reward:
            self.random_target_network = MinigridMLPTargetEmbeddingNet(args).to(device=device)
            self.predictor_network = MinigridMLPEmbeddingNet(args).to(device=device)
        else:
            self.random_target_network = None
            self.predictor_network = None

        # initialise rollout storage for the VAE update
        # (this differs from the data that the on-policy RL algorithm uses)
        self.exploration_rollout_storage = RolloutStorageVAE(num_processes=exploration_num_processes,
                                                             max_trajectory_len=self.args.max_trajectory_len,
                                                             zero_pad=True,
                                                             max_num_rollouts=self.args.size_vae_buffer,
                                                             state_dim=self.args.state_dim,
                                                             action_dim=self.args.action_dim,
                                                             vae_buffer_add_thresh=self.args.vae_buffer_add_thresh,
                                                             task_dim=self.task_dim,
                                                             save_intrinsic_reward=True
                                                             )
        self.exploitation_rollout_storage = RolloutStorageVAE(num_processes=exploitation_num_processes,
                                                              max_trajectory_len=self.args.max_trajectory_len,
                                                              zero_pad=True,
                                                              max_num_rollouts=self.args.size_vae_buffer,
                                                              state_dim=self.args.state_dim,
                                                              action_dim=self.args.action_dim,
                                                              vae_buffer_add_thresh=self.args.vae_buffer_add_thresh,
                                                              task_dim=self.task_dim,
                                                              )
        # initalise optimiser for the brim_core and decoders
        decoder_params = []
        if not self.args.disable_decoder:
            if self.args.decode_reward:
                decoder_params.extend(self.reward_decoder.parameters())
            if self.args.decode_state:
                decoder_params.extend(self.state_decoder.parameters())
            if self.args.decode_task:
                decoder_params.extend(self.task_decoder.parameters())
            if self.args.decode_action:
                decoder_params.extend(self.action_decoder.parameters())
            if self.args.use_rim_level2:
                decoder_params.extend(self.exploration_value_decoder.parameters())
                decoder_params.extend(self.exploitation_value_decoder.parameters())

        brim_core_params = []
        brim_core_params.extend(self.brim_core.brim.model.parameters())
        brim_core_params.extend(self.brim_core.brim.vae_encoder.parameters())

        self.hebb_meta_params = None
        if self.args.use_memory and self.args.use_hebb:
            self.hebb_meta_params = torch.optim.Adam([self.brim_core.brim.A, self.brim_core.brim.B], lr=self.args.lr_vae)

        if self.args.bebold_intrinsic_reward:
            self.optimiser_vae = torch.optim.Adam([*brim_core_params,
                                                   *decoder_params,
                                                   *self.predictor_network.parameters()], lr=self.args.lr_vae)
        else:
            self.optimiser_vae = torch.optim.Adam([*brim_core_params, *decoder_params], lr=self.args.lr_vae)

    def initialise_brim_core(self, memory_params):
        """ Initialises and returns an Brim Core """
        brim_core = BRIMCore(
            use_memory=self.args.use_memory,
            use_hebb=self.args.use_hebb,
            use_gen=self.args.use_gen,
            use_stateful_vision_core=self.args.use_stateful_vision_core,
            use_rim_level1=self.args.use_rim_level1,
            use_rim_level2=self.args.use_rim_level2,
            use_rim_level3=self.args.use_rim_level3,
            rim_top_down_level2_level1=self.args.rim_top_down_level2_level1,
            rim_top_down_level3_level2=self.args.rim_top_down_level3_level2,
            # brim
            use_gru_or_rim=self.args.use_gru_or_rim,
            rim_level1_hidden_size=self.args.rim_level1_hidden_size,
            rim_level2_hidden_size=self.args.rim_level2_hidden_size,
            rim_level3_hidden_size=self.args.rim_level3_hidden_size,
            rim_level1_output_dim=self.args.rim_level1_output_dim,
            rim_level2_output_dim=self.args.rim_level2_output_dim,
            rim_level3_output_dim=self.args.rim_level3_output_dim,
            rim_level1_num_modules=self.args.rim_level1_num_modules,
            rim_level2_num_modules=self.args.rim_level2_num_modules,
            rim_level3_num_modules=self.args.rim_level3_num_modules,
            rim_level1_topk=self.args.rim_level1_topk,
            rim_level2_topk=self.args.rim_level2_topk,
            rim_level3_topk=self.args.rim_level3_topk,
            brim_layers_before_rim_level1=self.args.brim_layers_before_rim_level1,
            brim_layers_before_rim_level2=self.args.brim_layers_before_rim_level2,
            brim_layers_before_rim_level3=self.args.brim_layers_before_rim_level3,
            brim_layers_after_rim_level1=self.args.brim_layers_after_rim_level1,
            brim_layers_after_rim_level2=self.args.brim_layers_after_rim_level2,
            brim_layers_after_rim_level3=self.args.brim_layers_after_rim_level3,
            rim_level1_condition_on_task_inference_latent=self.args.rim_level1_condition_on_task_inference_latent,
            rim_level2_condition_on_task_inference_latent=self.args.rim_level2_condition_on_task_inference_latent,
            # vae encoder
            vae_encoder_layers_before_gru=self.args.vae_encoder_layers_before_gru,
            vae_encoder_hidden_size=self.args.vae_encoder_gru_hidden_size,
            vae_encoder_layers_after_gru=self.args.vae_encoder_layers_after_gru,
            task_inference_latent_dim=self.args.task_inference_latent_dim,
            action_dim=self.args.action_dim,
            action_embed_dim=self.args.action_embedding_size,
            state_dim=self.args.state_dim,
            state_embed_dim=self.args.state_embedding_size,
            reward_size=1,
            reward_embed_size=self.args.reward_embedding_size,
            new_impl=self.args.new_impl,
            vae_loss_throughout_vae_encoder_from_rim_level3=self.args.vae_loss_throughout_vae_encoder_from_rim_level3,
            residual_task_inference_latent=self.args.residual_task_inference_latent,
            rim_output_size_to_vision_core=self.args.rim_output_size_to_vision_core,
            memory_params=memory_params,
            pass_gradient_to_rim_from_state_encoder=self.args.pass_gradient_to_rim_from_state_encoder,
            shared_embedding_network=self.args.shared_embedding_network
        ).to(device)
        return brim_core

    def initialise_decoder(self):
        """ Initialises and returns the (state/reward/task) decoder as specified in self.args """

        if self.args.disable_decoder:
            return None, None, None

        if self.args.use_rim_level3:
            latent_dim = self.args.rim_level3_output_dim
            if self.args.residual_task_inference_latent:
                latent_dim += self.args.task_inference_latent_dim
                if self.args.disable_stochasticity_in_latent:
                    # double latent dimension (input size to decoder) if we use a deterministic latents (for easier comparison)
                    latent_dim += self.args.task_inference_latent_dim
        else:
            assert self.args.residual_task_inference_latent is None
            latent_dim = self.args.task_inference_latent_dim
            # double latent dimension (input size to decoder) if we use a deterministic latents (for easier comparison)
            if self.args.disable_stochasticity_in_latent:
                latent_dim *= 2

        if self.args.decode_action:
            action_decoder = ActionDecoder(
                layers=self.args.action_decoder_layers,
                latent_dim=latent_dim,
                state_dim=self.args.state_dim,
                state_embed_dim=self.args.state_embedding_size,
                state_simulator_hidden_size=self.args.state_simulator_hidden_size,
                action_space=self.args.action_space,
                n_step_action_prediction=self.args.n_step_action_prediction,
                n_prediction=self.args.n_prediction
            ).to(device)
        else:
            action_decoder = None

        # initialise state decoder for VAE
        if self.args.decode_state:
            state_decoder = StateTransitionDecoder(
                layers=self.args.state_decoder_layers,
                latent_dim=latent_dim,
                action_dim=self.args.action_dim,
                action_embed_dim=self.args.action_embedding_size,
                state_dim=self.args.state_dim,
                state_embed_dim=self.args.state_embedding_size,
                action_simulator_hidden_size=self.args.action_simulator_hidden_size,
                pred_type=self.args.state_pred_type,
                n_step_state_prediction=self.args.n_step_state_prediction,
                n_prediction=self.args.n_prediction,
            ).to(device)
        else:
            state_decoder = None

        # initialise reward decoder for VAE
        if self.args.decode_reward:
            reward_decoder = RewardDecoder(
                layers=self.args.reward_decoder_layers,
                latent_dim=latent_dim,
                state_dim=self.args.state_dim,
                state_embed_dim=self.args.state_embedding_size,
                action_dim=self.args.action_dim,
                action_embed_dim=self.args.action_embedding_size,
                reward_simulator_hidden_size=self.args.reward_simulator_hidden_size,
                num_states=self.args.num_states,
                multi_head=self.args.multihead_for_reward,
                pred_type=self.args.rew_pred_type,
                input_prev_state=self.args.input_prev_state,
                input_action=self.args.input_action,
                n_step_reward_prediction=self.args.n_step_reward_prediction,
                n_prediction=self.args.n_prediction
            ).to(device)
        else:
            reward_decoder = None

        if self.args.use_rim_level2:
            exploration_value_decoder = ValueDecoder(
                layers=self.args.value_decoder_layers,
                latent_dim=self.args.rim_level2_output_dim,
                action_dim=self.args.action_dim,
                action_embed_dim=self.args.action_embedding_size,
                state_dim=self.args.state_dim,
                state_embed_dim=self.args.state_embedding_size,
                value_simulator_hidden_size=self.args.value_simulator_hidden_size,
                pred_type=self.args.task_pred_type,
                n_prediction=self.args.n_prediction).to(device)
            exploitation_value_decoder = ValueDecoder(
                layers=self.args.value_decoder_layers,
                latent_dim=self.args.rim_level2_output_dim,
                action_dim=self.args.action_dim,
                action_embed_dim=self.args.action_embedding_size,
                state_dim=self.args.state_dim,
                state_embed_dim=self.args.state_embedding_size,
                value_simulator_hidden_size=self.args.value_simulator_hidden_size,
                pred_type=self.args.task_pred_type,
                n_prediction=self.args.n_prediction).to(device)
        else:
            exploration_value_decoder = None
            exploitation_value_decoder = None

        # initialise task decoder for VAE
        if self.args.decode_task:
            task_decoder = TaskDecoder(
                latent_dim=latent_dim,
                layers=self.args.task_decoder_layers,
                task_dim=self.task_dim,
                num_tasks=self.num_tasks,
                pred_type=self.args.task_pred_type,
            ).to(device)
        else:
            task_decoder = None

        return state_decoder, reward_decoder, task_decoder, exploration_value_decoder, exploitation_value_decoder, action_decoder

    def compute_action_reconstruction_loss(self,
                                           # input
                                           latent_state,
                                           prev_state,
                                           next_state,
                                           n_step_next_state,
                                           # target
                                           action,
                                           n_step_action,
                                           n_step_action_prediction,
                                           return_predictions=False,
                                           ):
        action_pred = self.action_decoder(latent_state,
                                          prev_state,
                                          next_state,
                                          n_step_next_state,
                                          n_step_action_prediction=n_step_action_prediction)

        if not n_step_action_prediction:
            action_pred = action_pred[0]
            loss_state = compute_loss_action(action_pred, action)
            if return_predictions:
                return loss_state, action_pred
            else:
                return loss_state
        else:
            losses = list()
            for i in range(self.args.n_prediction + 1):
                if i == 0:
                    losses.append(compute_loss_action(action_pred[i], action))
                else:
                    losses.append(compute_loss_action(action_pred[i], n_step_action[i - 1]))
            if return_predictions:
                # just return prediction of next step
                return losses, action_pred[0]
            else:
                return losses

    def compute_state_reconstruction_loss(self, latent, prev_obs, next_obs, action, n_step_action, n_step_next_obs, n_step_state_prediction, return_predictions=False):
        """ Compute state reconstruction loss.
        (No reduction of loss along batch dimension is done here; sum/avg has to be done outside) """
        state_pred = self.state_decoder(latent,
                                        prev_obs,
                                        action,
                                        n_step_action,
                                        n_step_state_prediction=n_step_state_prediction)

        if not n_step_state_prediction:
            state_pred = state_pred[0]
            loss_state = compute_loss_state(state_pred, next_obs, self.args.state_pred_type)
            if return_predictions:
                return loss_state, state_pred
            else:
                return loss_state
        else:
            losses = list()
            for i in range(self.args.n_prediction+1):
                if i == 0:
                    losses.append(compute_loss_state(state_pred[i], next_obs, self.args.state_pred_type))
                else:
                    losses.append(compute_loss_state(state_pred[i], n_step_next_obs[i-1], self.args.state_pred_type))
            if return_predictions:
                # just return prediction of next step
                return losses, state_pred[0]
            else:
                return losses

    def compute_rew_reconstruction_loss(self, latent, prev_obs, next_obs, action, reward, n_step_next_obs, n_step_actions, n_step_rewards, return_predictions=False):
        """ Compute reward reconstruction loss.
        (No reduction of loss along batch dimension is done here; sum/avg has to be done outside) """
        rew_pred = self.reward_decoder(latent,
                                       next_obs,
                                       prev_obs,
                                       action.float(),
                                       n_step_next_obs,
                                       n_step_actions)
        if not self.args.n_step_reward_prediction:
            rew_pred = rew_pred[0]
            loss_rew = compute_loss_reward(rew_pred, reward, self.args.rew_pred_type)
            if return_predictions:
                return loss_rew, rew_pred
            else:
                return loss_rew
        else:
            losses = list()
            for i in range(self.args.n_prediction + 1):
                if i == 0:
                    losses.append(compute_loss_reward(rew_pred[i], reward, self.args.rew_pred_type))
                else:
                    losses.append(compute_loss_reward(rew_pred[i], n_step_rewards[i-1], self.args.rew_pred_type))
            if return_predictions:
                # just return prediction of next step
                return losses, rew_pred[0]
            else:
                return losses

    def compute_task_reconstruction_loss(self, latent, task, return_predictions=False):
        """ Compute task reconstruction loss.
        (No reduction of loss along batch dimension is done here; sum/avg has to be done outside) """

        task_pred = self.task_decoder(latent)

        if self.args.task_pred_type == 'task_id':
            env = gym.make(self.args.env_name)
            task_target = env.task_to_id(task).to(device)
            # expand along first axis (number of ELBO terms)
            task_target = task_target.expand(task_pred.shape[:-1]).reshape(-1)
            loss_task = F.cross_entropy(task_pred.view(-1, task_pred.shape[-1]),
                                        task_target, reduction='none').view(task_pred.shape[:-1])
        elif self.args.task_pred_type == 'task_description':
            loss_task = (task_pred - task).pow(2).mean(dim=-1)
        else:
            raise NotImplementedError

        if return_predictions:
            return loss_task, task_pred
        else:
            return loss_task

    def compute_kl_loss(self, latent_mean, latent_logvar, elbo_indices):
        # -- KL divergence
        if self.args.kl_to_gauss_prior:
            kl_divergences = (- 0.5 * (1 + latent_logvar - latent_mean.pow(2) - latent_logvar.exp()).sum(dim=-1))
        else:
            gauss_dim = latent_mean.shape[-1]
            # add the gaussian prior
            all_means = torch.cat((torch.zeros(1, *latent_mean.shape[1:]).to(device), latent_mean))
            all_logvars = torch.cat((torch.zeros(1, *latent_logvar.shape[1:]).to(device), latent_logvar))
            # https://arxiv.org/pdf/1811.09975.pdf
            # KL(N(mu,E)||N(m,S)) = 0.5 * (log(|S|/|E|) - K + tr(S^-1 E) + (m-mu)^T S^-1 (m-mu)))
            mu = all_means[1:]
            m = all_means[:-1]
            logE = all_logvars[1:]
            logS = all_logvars[:-1]
            kl_divergences = 0.5 * (torch.sum(logS, dim=-1) - torch.sum(logE, dim=-1) - gauss_dim + torch.sum(
                1 / torch.exp(logS) * torch.exp(logE), dim=-1) + ((m - mu) / torch.exp(logS) * (m - mu)).sum(dim=-1))

        # returns, for each ELBO_t term, one KL (so H+1 kl's)
        if elbo_indices is not None:
            return kl_divergences[elbo_indices]
        else:
            return kl_divergences

    def sum_reconstruction_terms(self, losses, idx_traj, len_encoder, trajectory_lens):

        """ Sums the reconstruction errors along episode horizon """
        if len(np.unique(trajectory_lens)) == 1 and not self.args.decode_only_past:
            # if for each embedding we decode the entire trajectory, we have a matrix and can sum along dim 1
            losses = losses.sum(dim=1)
        else:
            # otherwise, we loop and sum along the trajectory which we decoded (sum in ELBO_t)
            start_idx = 0
            partial_reconstruction_loss = []
            for i, idx_timestep in enumerate(len_encoder[idx_traj]):
                if self.args.decode_only_past:
                    dec_from = 0
                    dec_until = idx_timestep
                else:
                    dec_from = 0
                    dec_until = trajectory_lens[idx_traj]
                end_idx = start_idx + (dec_until - dec_from)
                if end_idx - start_idx != 0:
                    partial_reconstruction_loss.append(losses[start_idx:end_idx].sum())
                start_idx = end_idx
            losses = torch.stack(partial_reconstruction_loss)
        return losses

    def compute_value_reconstruction_loss(self,
                                          brim_output_level2,
                                          prev_obs,
                                          rewards,
                                          actions,
                                          value_next_state,
                                          returns_next_state,
                                          n_step_actions,
                                          n_step_rewards,
                                          n_step_value_next_state,
                                          n_step_returns_next_state,
                                          value_decoder
                                          ):

        value_pred = value_decoder(
            # general info
            brim_output_level2,
            prev_obs,
            # for one step value prediction
            rewards,
            actions,
            # for n step value prediction
            n_step_actions,
            n_step_rewards)

        losses = list()
        for i in range(self.args.n_prediction + 1):
            if i == 0:
                losses.append(compute_loss_value(value_pred[i], value_next_state, returns_next_state, n_step_v_loss=self.args.n_step_v_loss))
            else:
                losses.append(compute_loss_value(value_pred[i], n_step_value_next_state[i - 1], n_step_returns_next_state[i - 1], n_step_v_loss=self.args.n_step_v_loss))
        return losses

    def compute_value_loss(self,
                           # input
                           brim_output_level2,
                           vae_prev_obs,
                           vae_actions,
                           vae_rewards,
                           # target
                           value_next_state,
                           returns_next_state,
                           # general
                           trajectory_lens,
                           value_decoder):
        num_unique_trajectory_lens = len(np.unique(trajectory_lens))
        assert (num_unique_trajectory_lens == 1) or (self.args.vae_subsample_elbos and self.args.vae_subsample_decodes)
        assert not self.args.decode_only_past
        max_traj_len = np.max(trajectory_lens)

        # input
        n_step_actions = list()
        n_step_rewards = list()
        # target
        n_step_value_next_state = list()
        n_step_returns_next_state = list()

        vae_actions_len = vae_actions.shape[0]
        vae_rewards_len = vae_rewards.shape[0]
        value_next_state_len = value_next_state.shape[0]
        returns_next_state_len = returns_next_state.shape[0]

        for i in range(self.args.n_prediction):
            # for n last step of trajectory some n_step actions fill with not correct data -
            # if vas_subsample big enough this issue not effective
            if max_traj_len + i + 1 >= vae_actions_len:
                n_step_actions.append(torch.cat((vae_actions[i + 1:vae_actions_len], torch.zeros(
                    size=((max_traj_len + i + 1) - vae_actions_len, *vae_actions.shape[1:]), device=device))))
            else:
                n_step_actions.append(vae_actions[i + 1:max_traj_len + i + 1])

            if max_traj_len + i + 1 >= vae_rewards_len:
                n_step_rewards.append(torch.cat((vae_rewards[i + 1:vae_rewards_len], torch.zeros(
                    size=((max_traj_len + i + 1) - vae_rewards_len, *vae_rewards.shape[1:]), device=device))))
            else:
                n_step_rewards.append(vae_rewards[i + 1:max_traj_len + i + 1])

            if max_traj_len + i + 1 >= value_next_state_len:
                n_step_value_next_state.append(torch.cat((value_next_state[i + 1: value_next_state_len], torch.zeros(
                    size=((max_traj_len + i + 1) - value_next_state_len, *value_next_state.shape[1:]), device=device))))
            else:
                n_step_value_next_state.append(value_next_state[i + 1:max_traj_len + i + 1])

            if max_traj_len + i + 1 >= returns_next_state_len:
                n_step_returns_next_state.append(torch.cat((returns_next_state[i + 1: returns_next_state_len], torch.zeros(
                    size=((max_traj_len + i + 1) - returns_next_state_len, *returns_next_state.shape[1:]), device=device))))
            else:
                n_step_returns_next_state.append(returns_next_state[i+1:max_traj_len + i + 1])

        brim_output_level2 = brim_output_level2[:max_traj_len + 1]
        vae_prev_obs = vae_prev_obs[:max_traj_len]
        vae_actions = vae_actions[:max_traj_len]
        vae_rewards = vae_rewards[:max_traj_len]
        value_next_state = value_next_state[:max_traj_len]
        returns_next_state = returns_next_state[:max_traj_len]

        num_elbos = brim_output_level2.shape[0]
        num_decodes = vae_prev_obs.shape[0]
        batchsize = brim_output_level2.shape[1]  # number of trajectories

        if self.args.vae_subsample_elbos is not None:
            # randomly choose which elbo's to subsample
            if num_unique_trajectory_lens == 1:
                elbo_indices = torch.LongTensor(self.args.vae_subsample_elbos * batchsize).random_(0,
                                                                                                   num_elbos)  # select diff elbos for each task
            else:
                # if we have different trajectory lengths, subsample elbo indices separately
                # up to their maximum possible encoding length;
                # only allow duplicates if the sample size would be larger than the number of samples
                elbo_indices = np.concatenate([np.random.choice(range(0, t + 1), self.args.vae_subsample_elbos,
                                                                replace=self.args.vae_subsample_elbos > (t + 1)) for
                                               t in trajectory_lens])
                if max_traj_len < self.args.vae_subsample_elbos:
                    warnings.warn('The required number of ELBOs is larger than the shortest trajectory, '
                                  'so there will be duplicates in your batch.'
                                  'To avoid this use --split_batches_by_elbo or --split_batches_by_task.')
            task_indices = torch.arange(batchsize).repeat(self.args.vae_subsample_elbos)  # for selection mask
            brim_output_level2 = brim_output_level2[elbo_indices, task_indices, :].reshape((self.args.vae_subsample_elbos, batchsize, -1))
            num_elbos = brim_output_level2.shape[0]
        else:
            elbo_indices = None

        dec_prev_obs = vae_prev_obs.unsqueeze(0).expand((num_elbos, *vae_prev_obs.shape))
        dec_actions = vae_actions.unsqueeze(0).expand((num_elbos, *vae_actions.shape))
        dec_rewards = vae_rewards.unsqueeze(0).expand((num_elbos, *vae_rewards.shape))
        dec_value_next_state = value_next_state.unsqueeze(0).expand((num_elbos, *value_next_state.shape))
        dec_returns_next_state = returns_next_state.unsqueeze(0).expand((num_elbos, *returns_next_state.shape))

        dec_n_step_actions = list()
        for i in range(self.args.n_prediction):
            dec_n_step_actions.append(n_step_actions[i].unsqueeze(0).expand((num_elbos, *n_step_actions[i].shape)))

        dec_n_step_rewards = list()
        for i in range(self.args.n_prediction):
            dec_n_step_rewards.append(n_step_rewards[i].unsqueeze(0).expand((num_elbos, *n_step_rewards[i].shape)))

        dec_n_step_value_next_state = list()
        for i in range(self.args.n_prediction):
            dec_n_step_value_next_state.append(n_step_value_next_state[i].unsqueeze(0).expand((num_elbos, *n_step_value_next_state[i].shape)))

        dec_n_step_returns_next_state = list()
        for i in range(self.args.n_prediction):
            dec_n_step_returns_next_state.append(n_step_returns_next_state[i].unsqueeze(0).expand((num_elbos, *n_step_returns_next_state[i].shape)))

        if self.args.vae_subsample_decodes is not None:
            # shape before: vae_subsample_elbos * num_decodes * batchsize * dim
            # shape after: vae_subsample_elbos * vae_subsample_decodes * batchsize * dim
            # (Note that this will always have duplicates given how we set up the code)
            indices0 = torch.arange(num_elbos).repeat(self.args.vae_subsample_decodes * batchsize)
            if num_unique_trajectory_lens == 1:
                indices1 = torch.LongTensor(num_elbos * self.args.vae_subsample_decodes * batchsize).random_(0, num_decodes)
            else:
                indices1 = np.concatenate([np.random.choice(range(0, t), num_elbos * self.args.vae_subsample_decodes,
                                                            replace=True) for t in trajectory_lens])
            indices2 = torch.arange(batchsize).repeat(num_elbos * self.args.vae_subsample_decodes)
            dec_prev_obs = dec_prev_obs[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_actions = dec_actions[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_rewards = dec_rewards[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_value_next_state = dec_value_next_state[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_returns_next_state = dec_returns_next_state[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))

            for i in range(self.args.n_prediction):
                dec_n_step_actions[i] = dec_n_step_actions[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
                dec_n_step_rewards[i] = dec_n_step_rewards[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))

                dec_n_step_value_next_state[i] = dec_n_step_value_next_state[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
                dec_n_step_returns_next_state[i] = dec_n_step_returns_next_state[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))

            num_decodes = dec_prev_obs.shape[1]
            dec_brim_output_level2 = brim_output_level2.unsqueeze(0).expand((num_decodes, *brim_output_level2.shape)).transpose(1, 0)

            value_reconstruction_loss = self.compute_value_reconstruction_loss(dec_brim_output_level2,
                                                                               dec_prev_obs,
                                                                               dec_rewards,
                                                                               dec_actions,
                                                                               dec_value_next_state,
                                                                               dec_returns_next_state,
                                                                               dec_n_step_actions,
                                                                               dec_n_step_rewards,
                                                                               dec_n_step_value_next_state,
                                                                               dec_n_step_returns_next_state,
                                                                               value_decoder)
            losses = torch.zeros(size=(self.args.n_prediction + 1, 1)).to(device)
            for i in range(self.args.n_prediction + 1):
                losses[i] = avg_loss(value_reconstruction_loss[i], self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
            if self.args.vae_avg_n_step_prediction:
                value_reconstruction_loss = losses.mean(dim=0)[0]
            else:
                value_reconstruction_loss = losses.sum(dim=0)[0]
            return value_reconstruction_loss.mean()

    def compute_loss(self, brim_output5, latent_mean, latent_logvar, vae_prev_obs, vae_next_obs, vae_actions,
                     vae_rewards, vae_tasks, trajectory_lens):
        """
        Computes the VAE loss for the given data.
        Batches everything together and therefore needs all trajectories to be of the same length.
        (Important because we need to separate ELBOs and decoding terms so can't collapse those dimensions)
        """

        num_unique_trajectory_lens = len(np.unique(trajectory_lens))
        assert (num_unique_trajectory_lens == 1) or (self.args.vae_subsample_elbos and self.args.vae_subsample_decodes)
        assert not self.args.decode_only_past
        max_traj_len = np.max(trajectory_lens)

        # prepare data for state n step prediction
        n_step_actions = None
        n_step_next_obs = None
        n_step_rewards = None
        n_step_state_prediction = self.args.n_step_state_prediction and self.args.decode_state
        n_step_reward_prediction = self.args.n_step_reward_prediction and self.args.decode_reward
        n_step_action_prediction = self.args.n_step_action_prediction and self.args.decode_action
        if n_step_state_prediction or n_step_reward_prediction:
            # if vae_sub_sample >> n_prediction get better result
            n_step_actions = list()
            n_step_next_obs = list()
            n_step_rewards = list()
            vae_actions_len = vae_actions.shape[0]
            vae_next_obs_len = vae_next_obs.shape[0]
            vae_rewards_len = vae_rewards.shape[0]
            for i in range(self.args.n_prediction):
                # for n last step of trajectory some n_step actions fill with not correct data -
                # if vas_subsample big enough this issue not effective
                if max_traj_len + i + 1 >= vae_actions_len:
                    n_step_actions.append(torch.cat((vae_actions[i+1:vae_actions_len], torch.zeros(size=((max_traj_len + i + 1) - vae_actions_len, *vae_actions.shape[1:]), device=device))))
                else:
                    n_step_actions.append(vae_actions[i + 1:max_traj_len + i + 1])

                # n step next obs is also required for target state in state prediction & for action decoder (will add in future)
                if max_traj_len + i +1 >= vae_next_obs_len:
                    n_step_next_obs.append(torch.cat((vae_next_obs[i + 1:vae_next_obs_len], torch.zeros(
                        size=((max_traj_len + i + 1) - vae_next_obs_len, *vae_next_obs.shape[1:]), device=device))))
                else:
                    n_step_next_obs.append(vae_next_obs[i + 1:max_traj_len + i + 1])

                if max_traj_len + i + 1 >= vae_rewards_len:
                    n_step_rewards.append(torch.cat((vae_rewards[i + 1:vae_rewards_len], torch.zeros(
                        size=((max_traj_len + i + 1) - vae_rewards_len, *vae_rewards.shape[1:]), device=device))))
                else:
                    n_step_rewards.append(vae_rewards[i + 1:max_traj_len + i + 1])

        # cut down the batch to the longest trajectory length
        # this way we can preserve the structure
        # but we will waste some computation on zero-padded trajectories that are shorter than max_traj_len
        latent_mean = latent_mean[:max_traj_len+1]
        latent_logvar = latent_logvar[:max_traj_len+1]
        brim_output5 = brim_output5[:max_traj_len+1]
        vae_prev_obs = vae_prev_obs[:max_traj_len]
        vae_next_obs = vae_next_obs[:max_traj_len]
        vae_actions = vae_actions[:max_traj_len]
        vae_rewards = vae_rewards[:max_traj_len]

        # take one sample for each ELBO term
        if not self.args.disable_stochasticity_in_latent:
            latent_samples = self.brim_core._sample_gaussian(latent_mean, latent_logvar)
        else:
            latent_samples = torch.cat((latent_mean, latent_logvar), dim=-1)

        num_elbos = latent_samples.shape[0]
        num_decodes = vae_prev_obs.shape[0]
        batchsize = latent_samples.shape[1]  # number of trajectories

        # subsample elbo terms
        #   shape before: num_elbos * batchsize * dim
        #   shape after: vae_subsample_elbos * batchsize * dim
        if self.args.vae_subsample_elbos is not None:
            # randomly choose which elbo's to subsample
            if num_unique_trajectory_lens == 1:
                elbo_indices = torch.LongTensor(self.args.vae_subsample_elbos * batchsize).random_(0, num_elbos)    # select diff elbos for each task
            else:
                # if we have different trajectory lengths, subsample elbo indices separately
                # up to their maximum possible encoding length;
                # only allow duplicates if the sample size would be larger than the number of samples
                elbo_indices = np.concatenate([np.random.choice(range(0, t + 1), self.args.vae_subsample_elbos,
                                                                replace=self.args.vae_subsample_elbos > (t+1)) for t in trajectory_lens])
                if max_traj_len < self.args.vae_subsample_elbos:
                    warnings.warn('The required number of ELBOs is larger than the shortest trajectory, '
                                  'so there will be duplicates in your batch.'
                                  'To avoid this use --split_batches_by_elbo or --split_batches_by_task.')
            task_indices = torch.arange(batchsize).repeat(self.args.vae_subsample_elbos)  # for selection mask
            latent_samples = latent_samples[elbo_indices, task_indices, :].reshape((self.args.vae_subsample_elbos, batchsize, -1))
            brim_output5 = brim_output5[elbo_indices, task_indices, :].reshape((self.args.vae_subsample_elbos, batchsize, -1))
            num_elbos = latent_samples.shape[0]
        else:
            elbo_indices = None

        # expand the state/rew/action inputs to the decoder (to match size of latents)
        # shape will be: [num tasks in batch] x [num elbos] x [len trajectory (reconstrution loss)] x [dimension]
        dec_prev_obs = vae_prev_obs.unsqueeze(0).expand((num_elbos, *vae_prev_obs.shape))
        dec_next_obs = vae_next_obs.unsqueeze(0).expand((num_elbos, *vae_next_obs.shape))
        dec_actions = vae_actions.unsqueeze(0).expand((num_elbos, *vae_actions.shape))
        dec_rewards = vae_rewards.unsqueeze(0).expand((num_elbos, *vae_rewards.shape))
        dec_n_step_actions = None
        dec_n_step_next_obs = None
        dec_n_step_rewards = None
        if n_step_state_prediction or n_step_reward_prediction:
            dec_n_step_actions = list()
            for i in range(self.args.n_prediction):
                dec_n_step_actions.append(n_step_actions[i].unsqueeze(0).expand((num_elbos, *n_step_actions[i].shape)))

            dec_n_step_next_obs = list()
            for i in range(self.args.n_prediction):
                dec_n_step_next_obs.append(n_step_next_obs[i].unsqueeze(0).expand((num_elbos, *n_step_next_obs[i].shape)))

            dec_n_step_rewards = list()
            for i in range(self.args.n_prediction):
                dec_n_step_rewards.append(n_step_rewards[i].unsqueeze(0).expand((num_elbos, *n_step_rewards[i].shape)))

        # subsample reconstruction terms
        if self.args.vae_subsample_decodes is not None:
            # shape before: vae_subsample_elbos * num_decodes * batchsize * dim
            # shape after: vae_subsample_elbos * vae_subsample_decodes * batchsize * dim
            # (Note that this will always have duplicates given how we set up the code)
            indices0 = torch.arange(num_elbos).repeat(self.args.vae_subsample_decodes * batchsize)
            if num_unique_trajectory_lens == 1:
                indices1 = torch.LongTensor(num_elbos * self.args.vae_subsample_decodes * batchsize).random_(0, num_decodes)
            else:
                indices1 = np.concatenate([np.random.choice(range(0, t), num_elbos * self.args.vae_subsample_decodes,
                                                            replace=True) for t in trajectory_lens])
            indices2 = torch.arange(batchsize).repeat(num_elbos * self.args.vae_subsample_decodes)
            dec_prev_obs = dec_prev_obs[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_next_obs = dec_next_obs[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_actions = dec_actions[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            dec_rewards = dec_rewards[indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            if n_step_state_prediction or n_step_reward_prediction:
                for i in range(self.args.n_prediction):
                    dec_n_step_actions[i] = dec_n_step_actions[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
                    dec_n_step_next_obs[i] = dec_n_step_next_obs[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
                    dec_n_step_rewards[i] = dec_n_step_rewards[i][indices0, indices1, indices2, :].reshape((num_elbos, self.args.vae_subsample_decodes, batchsize, -1))
            num_decodes = dec_prev_obs.shape[1]

        # expand the latent (to match the number of state/rew/action inputs to the decoder)
        # shape will be: [num tasks in batch] x [num elbos] x [len trajectory (reconstrution loss)] x [dimension]
        dec_embedding = latent_samples.unsqueeze(0).expand((num_decodes, *latent_samples.shape)).transpose(1, 0)
        dec_brim_output5 = brim_output5.unsqueeze(0).expand((num_decodes, *brim_output5.shape)).transpose(1, 0)
        # if use rim in VAE decoder use output of rim level 3 instead of VAE encoder output
        if self.args.use_rim_level3:
            if self.args.residual_task_inference_latent:
                dec_embedding = torch.cat((dec_embedding, dec_brim_output5), dim=-1)
            else:
                dec_embedding = dec_brim_output5

        if self.args.decode_reward:
            # compute reconstruction loss for this trajectory (for each timestep that was encoded, decode everything and sum it up)
            # shape: [num_elbo_terms] x [num_reconstruction_terms] x [num_trajectories]
            rew_reconstruction_loss = self.compute_rew_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs,
                                                                           dec_actions, dec_rewards, dec_n_step_next_obs, dec_n_step_actions, dec_n_step_rewards)

            if self.args.n_step_reward_prediction:
                losses = torch.zeros(size=(self.args.n_prediction + 1, 1)).to(device)
                alpha = 1.0
                for i in range(self.args.n_prediction + 1):
                    losses[i] = alpha * avg_loss(rew_reconstruction_loss[i], self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
                    if self.args.use_discount_n_prediction:
                        alpha *= self.args.discount_n_prediction_coef
                if self.args.vae_avg_n_step_prediction:
                    rew_reconstruction_loss = losses.mean(dim=0)
                else:
                    rew_reconstruction_loss = losses.sum(dim=0)
            else:
                rew_reconstruction_loss = avg_loss(rew_reconstruction_loss, self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
        else:
            rew_reconstruction_loss = 0

        if self.args.decode_state:
            state_reconstruction_loss = self.compute_state_reconstruction_loss(dec_embedding, dec_prev_obs,
                                                                               dec_next_obs, dec_actions, dec_n_step_actions, dec_n_step_next_obs, n_step_state_prediction=self.args.n_step_state_prediction)
            if n_step_state_prediction:
                losses = torch.zeros(size=(self.args.n_prediction+1, 1)).to(device)
                alpha = 1.0
                for i in range(self.args.n_prediction+1):
                    losses[i] = alpha * avg_loss(state_reconstruction_loss[i], self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
                    if self.args.use_discount_n_prediction:
                        alpha *= self.args.discount_n_prediction_coef
                if self.args.vae_avg_n_step_prediction:
                    state_reconstruction_loss = losses.mean(dim=0)
                else:
                    state_reconstruction_loss = losses.sum(dim=0)
            else:
                state_reconstruction_loss = avg_loss(state_reconstruction_loss, self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
        else:
            state_reconstruction_loss = 0

        if self.args.decode_action:
            action_reconstruction_loss = self.compute_action_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs,
                                                                                 dec_n_step_next_obs, dec_actions, dec_n_step_actions, n_step_action_prediction=self.args.n_step_action_prediction)
            if n_step_action_prediction:
                losses = torch.zeros(size=(self.args.n_prediction+1, 1)).to(device)
                alpha = 1.0
                for i in range(self.args.n_prediction+1):
                    losses[i] = alpha * avg_loss(action_reconstruction_loss[i], self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
                    if self.args.use_discount_n_prediction:
                        alpha *= self.args.discount_n_prediction_coef
                if self.args.vae_avg_n_step_prediction:
                    action_reconstruction_loss = losses.mean(dim=0)
                else:
                    action_reconstruction_loss = losses.sum(dim=0)
            else:
                action_reconstruction_loss = avg_loss(action_reconstruction_loss, self.args.vae_avg_elbo_terms, self.args.vae_avg_reconstruction_terms)
        else:
            action_reconstruction_loss = 0

        if self.args.decode_task:
            task_reconstruction_loss = self.compute_task_reconstruction_loss(latent_samples, vae_tasks)
            # avg/sum across individual ELBO terms
            if self.args.vae_avg_elbo_terms:
                task_reconstruction_loss = task_reconstruction_loss.mean(dim=0)
            else:
                task_reconstruction_loss = task_reconstruction_loss.sum(dim=0)
            # sum the elbos, average across tasks
            task_reconstruction_loss = task_reconstruction_loss.sum(dim=0).mean()
        else:
            task_reconstruction_loss = 0

        if not self.args.disable_stochasticity_in_latent:
            # compute the KL term for each ELBO term of the current trajectory
            kl_loss = self.compute_kl_loss(latent_mean, latent_logvar, elbo_indices)
            # avg/sum the elbos
            if self.args.vae_avg_elbo_terms:
                kl_loss = kl_loss.mean(dim=0)
            else:
                kl_loss = kl_loss.sum(dim=0)
            # average across tasks
            kl_loss = kl_loss.sum(dim=0).mean()
        else:
            kl_loss = 0

        return rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, action_reconstruction_loss, kl_loss

    def compute_vae_loss(self, update=False):
        """
        Returns the VAE loss
        """
        exploration_rollout_storage_ready = self.exploration_rollout_storage.ready_for_update()
        exploitation_rollout_storage_ready = self.exploitation_rollout_storage.ready_for_update()

        if self.args.vae_fill_just_with_exploration_experience:
            if not exploration_rollout_storage_ready:
                return 0
        else:
            if not exploitation_rollout_storage_ready and not exploration_rollout_storage_ready:
                return 0

        if self.args.disable_decoder and self.args.disable_stochasticity_in_latent:
            return 0

        use_exploitation_data = exploitation_rollout_storage_ready and not self.args.vae_fill_just_with_exploration_experience
        use_exploration_data = exploration_rollout_storage_ready

        assert use_exploration_data or use_exploitation_data

        # get a mini-batch
        if use_exploration_data:
            exploration_vae_prev_obs, exploration_vae_next_obs, exploration_vae_actions, exploration_vae_rewards, exploration_vae_tasks, \
            exploration_trajectory_lens = self.exploration_rollout_storage.get_batch(batchsize=self.args.vae_batch_num_trajs)

        if use_exploitation_data:
            exploitation_vae_prev_obs, exploitation_vae_next_obs, exploitation_vae_actions, exploitation_vae_rewards, exploitation_vae_tasks, \
            exploitation_trajectory_lens = self.exploitation_rollout_storage.get_batch(batchsize=self.args.vae_batch_num_trajs)

        if use_exploration_data and use_exploitation_data:
            # concat above fetched data
            vae_prev_obs = torch.cat((exploration_vae_prev_obs, exploitation_vae_prev_obs), dim=1)
            vae_next_obs = torch.cat((exploration_vae_next_obs, exploitation_vae_next_obs), dim=1)
            vae_actions = torch.cat((exploration_vae_actions, exploitation_vae_actions), dim=1)
            vae_rewards = torch.cat((exploration_vae_rewards, exploitation_vae_rewards), dim=1)
            vae_tasks = torch.cat((exploration_vae_tasks, exploitation_vae_tasks), dim=1)
            trajectory_lens = np.concatenate((exploration_trajectory_lens, exploitation_trajectory_lens), axis=0)
        elif use_exploration_data:
            vae_prev_obs = exploration_vae_prev_obs
            vae_next_obs = exploration_vae_next_obs
            vae_actions = exploration_vae_actions
            vae_rewards = exploration_vae_rewards
            vae_tasks = exploration_vae_tasks
            trajectory_lens = exploration_trajectory_lens
        elif use_exploitation_data:
            vae_prev_obs = exploitation_vae_prev_obs
            vae_next_obs = exploitation_vae_next_obs
            vae_actions = exploitation_vae_actions
            vae_rewards = exploitation_vae_rewards
            vae_tasks = exploitation_vae_tasks
            trajectory_lens = exploitation_trajectory_lens
        else:
            raise Exception('both of use_exploration_data and use_exploitation_data')
            vae_prev_obs = None
            vae_next_obs = None
            vae_actions = None
            vae_rewards = None
            vae_tasks = None
            trajectory_lens = None

        # vae_prev_obs will be of size: max trajectory len x num trajectories x dimension of observations
        # pass through brim_core (outputs will be: (max_traj_len+1) x number of rollouts x latent_dim -- includes the prior!)
        brim_output5, latent_mean, latent_logvar = self.brim_core.forward_level3(actions=vae_actions,
                                                                                 states=vae_next_obs,
                                                                                 rewards=vae_rewards,
                                                                                 brim_hidden_state=None,
                                                                                 task_inference_hidden_state=None,
                                                                                 return_prior=True,
                                                                                 sample=True,
                                                                                 detach_every=self.args.tbptt_stepsize if hasattr(self.args, 'tbptt_stepsize') else None,
                                                                                 prev_state=vae_prev_obs[0, :, :])

        losses = self.compute_loss(brim_output5, latent_mean, latent_logvar, vae_prev_obs, vae_next_obs, vae_actions,
                                   vae_rewards, vae_tasks, trajectory_lens)
        rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, action_reconstruction_loss, kl_loss = losses

        # VAE loss = KL loss + reward reconstruction + state transition reconstruction
        # take average (this is the expectation over p(M))
        loss = (self.args.rew_loss_coeff * rew_reconstruction_loss +
                self.args.state_loss_coeff * state_reconstruction_loss +
                self.args.task_loss_coeff * task_reconstruction_loss +
                self.args.action_loss_coeff * action_reconstruction_loss +
                self.args.kl_weight * kl_loss).mean()

        # make sure we can compute gradients
        if not self.args.disable_stochasticity_in_latent:
            assert kl_loss.requires_grad
        if self.args.decode_reward:
            assert rew_reconstruction_loss.requires_grad
        if self.args.decode_state:
            assert state_reconstruction_loss.requires_grad
        if self.args.decode_task:
            assert task_reconstruction_loss.requires_grad
        if self.args.decode_action:
            assert action_reconstruction_loss.requires_grad

        # overall loss
        elbo_loss = loss.mean()

        if update:
            self.optimiser_vae.zero_grad()
            elbo_loss.backward()
            self.optimiser_vae.step()
            # clip gradients
            # nn.utils.clip_grad_norm_(self.encoder.parameters(), self.args.a2c_max_grad_norm)
            # nn.utils.clip_grad_norm_(reward_decoder.parameters(), self.args.max_grad_norm)

        self.log(elbo_loss, rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, action_reconstruction_loss, kl_loss)

        return elbo_loss

    def compute_n_step_value_prediction_loss(self, policy, activated_branch):
        if activated_branch == 'exploration':
            if not self.exploration_rollout_storage.ready_for_update():
                return 0
            vae_prev_obs, vae_next_obs, vae_actions, vae_rewards, vae_tasks, masks, bad_masks,\
            trajectory_lens = self.exploration_rollout_storage.get_batch(batchsize=self.args.vae_batch_num_trajs, value_prediction=True)

            brim_output_level1, brim_output_level2, brim_output_level3, _, \
            latent_sample, latent_mean, latent_logvar, _, policy_embedded_state = self.brim_core.forward_exploration_branch(
                actions=vae_actions,
                states=vae_next_obs,
                rewards=vae_rewards,
                brim_hidden_state=None,
                task_inference_hidden_state=None,
                return_prior=True,
                sample=True,
                detach_every=None,
                policy=policy,
                prev_state=vae_prev_obs[0, :, :])

        elif activated_branch == 'exploitation':
            if not self.exploitation_rollout_storage.ready_for_update():
                return 0
            vae_prev_obs, vae_next_obs, vae_actions, vae_rewards, vae_tasks, masks, bad_masks,\
            trajectory_lens = self.exploitation_rollout_storage.get_batch(batchsize=self.args.vae_batch_num_trajs, value_prediction=True)

            brim_output_level1, brim_output_level2, brim_output_level3, _, \
            latent_sample, latent_mean, latent_logvar, _, policy_embedded_state = self.brim_core.forward_exploitation_branch(
                actions=vae_actions,
                states=vae_next_obs,
                rewards=vae_rewards,
                brim_hidden_state=None,
                task_inference_hidden_state=None,
                return_prior=True,
                sample=True,
                detach_every=None,
                policy=policy,
                prev_state=vae_prev_obs[0, :, :])
        else:
            raise NotImplementedError

        task_inference_latent = get_latent_for_policy(sample_embeddings=self.args.sample_embeddings,
                                                      add_nonlinearity_to_latent=self.args.add_nonlinearity_to_latent,
                                                      latent_sample=latent_sample, latent_mean=latent_mean,
                                                      latent_logvar=latent_logvar)

        states = policy_embedded_state.detach()
        value_states = policy.get_value(states.view(-1, self.args.policy_state_embedding_dim),
                                        task_inference_latent.view(-1, self.args.task_inference_latent_dim*2),
                                        brim_output_level1.view(-1, self.args.rim_level1_output_dim),
                                        None, vae_tasks).detach()
        shape = states.shape[:-1]

        value_states = value_states.reshape((*shape, 1))
        # returns is our target
        returns = torch.zeros(size=(shape[0],  shape[1], 1), device=device)
        compute_returns(next_value=value_states[-1],
                        rewards=vae_rewards,
                        value_preds=value_states,
                        returns=returns,
                        gamma=self.args.policy_gamma,
                        tau=self.args.policy_tau,
                        use_gae=self.args.policy_use_gae,
                        masks=masks,
                        bad_masks=bad_masks,
                        use_proper_time_limits=True)
        returns = returns.detach()
        returns_next_state = returns[1:]
        value_next_state = value_states[1:]

        n_step_value_pred_loss = self.compute_value_loss(
                                                        # input
                                                        brim_output_level2,
                                                        vae_prev_obs,
                                                        vae_actions,
                                                        vae_rewards,
                                                        # target
                                                        value_next_state,
                                                        returns_next_state,
                                                        trajectory_lens,
                                                        value_decoder=self.exploration_value_decoder if activated_branch == 'exploration' else self.exploitation_value_decoder)
        self.log_value_prediction(n_step_value_pred_loss, policy_type=activated_branch)
        return n_step_value_pred_loss

    def compute_memory_loss(self, policy, activated_branch):
        if activated_branch == 'exploration':
            exploration_rollout_storage_ready = self.exploration_rollout_storage.ready_for_update()
            if not exploration_rollout_storage_ready:
                return 0
            vae_prev_obs, vae_next_obs, vae_actions, vae_rewards, vae_tasks, done_task, done_episode, trajectory_lens = self.exploration_rollout_storage.get_batch(
                batchsize=self.args.vae_batch_num_trajs, memory_batch=True)
            max_len = max(trajectory_lens)

            brim_output_level1, brim_output_level2, brim_output_level3, _, \
            latent_sample, latent_mean, latent_logvar, _, policy_embedded_state = self.brim_core.forward_exploration_branch(
                actions=vae_actions,
                states=vae_next_obs,
                rewards=vae_rewards,
                brim_hidden_state=None,
                task_inference_hidden_state=None,
                return_prior=True,
                sample=True,
                detach_every=None,
                policy=policy,
                prev_state=vae_prev_obs[0, :, :])
        elif activated_branch == 'exploitation':
            exploitation_rollout_storage_ready = self.exploitation_rollout_storage.ready_for_update()
            if not exploitation_rollout_storage_ready:
                return 0
            vae_prev_obs, vae_next_obs, vae_actions, vae_rewards, vae_tasks, done_task, done_episode, trajectory_lens = self.exploitation_rollout_storage.get_batch(
                batchsize=self.args.vae_batch_num_trajs, memory_batch=True)
            max_len = max(trajectory_lens)

            brim_output_level1, brim_output_level2, brim_output_level3, brim_hidden_states, \
            latent_sample, latent_mean, latent_logvar, _, policy_embedded_state = self.brim_core.forward_exploitation_branch(
                actions=vae_actions,
                states=vae_next_obs,
                rewards=vae_rewards,
                brim_hidden_state=None,
                task_inference_hidden_state=None,
                return_prior=True,
                sample=True,
                detach_every=None,
                policy=policy,
                prev_state=vae_prev_obs[0, :, :])
        else:
            raise NotImplementedError

        state = vae_next_obs[:max_len]  # 399

        rim_output = brim_output_level1[1:max_len+1]  # 399
        latent_sample = latent_sample[1:max_len+1]
        latent_mean = latent_mean[1:max_len+1]
        latent_logvar = latent_logvar[1:max_len+1]
        done_task = done_task[0:max_len+1]
        done_episode = done_episode[0:max_len+1]

        latent = utl.get_latent_for_policy(
            sample_embeddings=False,
            add_nonlinearity_to_latent=False,
            latent_sample=latent_sample,
            latent_mean=latent_mean,
            latent_logvar=latent_logvar).detach().clone()

        self.brim_core.brim.model.memory.prior(batch_size=state.shape[1], activated_branch=activated_branch)

        for i in range(max_len):
            self.brim_core.brim.model.memory.reset(done_task[i], done_episode[i], activated_branch)
            self.brim_core.brim.model.memory.write(key=(state[i], latent[i]), value=rim_output[i], rpe=None, activated_branch=activated_branch)
        res = []
        idx = torch.randperm(state.shape[0])
        target = rim_output[idx, :, :]
        level = 0 if activated_branch == 'exploration' else 1
        brim_hidden_states = brim_hidden_states[1:max_len+1, :, level, :]
        for i in range(len(idx)):
            res.append(self.brim_core.brim.model.memory.read(query=(state[idx[i]], latent[idx[i]]), rim_hidden_state=brim_hidden_states[idx[i]], activated_branch=activated_branch))
        res = torch.stack(res)
        memory_loss = (res - target).pow(2).sum()
        self.log_memory_loss(memory_loss, activated_branch)
        return memory_loss

    def log(self, elbo_loss, rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, action_reconstruction_loss, kl_loss):

        curr_iter_idx = self.get_iter_idx()
        if curr_iter_idx % self.args.log_interval == 0:

            if self.args.decode_reward:
                self.logger.add('vae_losses/reward_reconstr_err', rew_reconstruction_loss.mean(), curr_iter_idx)
            if self.args.decode_state:
                self.logger.add('vae_losses/state_reconstr_err', state_reconstruction_loss.mean(), curr_iter_idx)
            if self.args.decode_task:
                self.logger.add('vae_losses/task_reconstr_err', task_reconstruction_loss.mean(), curr_iter_idx)
            if self.args.decode_action:
                self.logger.add('vae_losses/action_reconstr_err', action_reconstruction_loss.mean(), curr_iter_idx)

            if not self.args.disable_stochasticity_in_latent:
                self.logger.add('vae_losses/kl', kl_loss.mean(), curr_iter_idx)
            self.logger.add('vae_losses/sum', elbo_loss, curr_iter_idx)

    def log_value_prediction(self, value_reconstruction_loss, policy_type):
        curr_iter_idx = self.get_iter_idx()
        if curr_iter_idx % self.args.log_interval == 0:
            self.logger.add(f'n_step_value_pred_loss/value_reconstr_err_{policy_type}', value_reconstruction_loss.mean(), curr_iter_idx)

    def log_memory_loss(self, memory_reconstruction_loss, policy_type):
        curr_iter_idx = self.get_iter_idx()
        if curr_iter_idx % self.args.log_interval == 0:
            self.logger.add(f'memory_loss_{policy_type}', memory_reconstruction_loss.mean(), curr_iter_idx)
