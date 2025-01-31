import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from utils import helpers as utl


class LRPolicy(object):
    def __init__(self, train_steps):
        self.train_steps = train_steps

    def __call__(self, epoch):
        return 1 - epoch / self.train_steps


class PPO:
    def __init__(self,
                 args,
                 actor_critic,
                 value_loss_coef,
                 entropy_coef,
                 policy_optimiser,
                 policy_anneal_lr,
                 train_steps,
                 optimiser_vae=None,
                 lr=None,
                 clip_param=0.2,
                 ppo_epoch=5,
                 num_mini_batch=5,
                 eps=None,
                 use_huber_loss=True,
                 use_clipped_value_loss=True,
                 hebb_meta_params_optim=None
                 ):
        self.args = args

        # the model
        self.actor_critic = actor_critic

        self.clip_param = clip_param
        self.ppo_epoch = ppo_epoch
        self.num_mini_batch = num_mini_batch

        self.value_loss_coef = value_loss_coef
        self.entropy_coef = entropy_coef

        self.use_clipped_value_loss = use_clipped_value_loss
        self.use_huber_loss = use_huber_loss

        self.use_state_encoder = self.args.policy_state_embedding_dim is not None and self.args.pass_state_to_policy
        self.use_task_inference_latent_encoder = self.args.policy_task_inference_latent_embedding_dim is not None and self.args.pass_task_inference_latent_to_policy
        self.use_belief_encoder = self.args.policy_belief_embedding_dim is not None and self.args.pass_belief_to_policy
        self.use_task_encoder = self.args.policy_task_embedding_dim is not None and self.args.pass_task_to_policy
        self.use_rim_level1_output_encoder = self.args.policy_rim_level1_output_embedding_dim is not None and self.args.use_rim_level1
        # optimiser
        if policy_optimiser == 'adam':
            use_state_encoder = self.args.policy_state_embedding_dim is not None and self.args.pass_state_to_policy
            # TODO
            if False and use_state_encoder and self.args.use_stateful_vision_core:
                pass
                # use_task_inference_latent_encoder = self.args.policy_task_inference_latent_embedding_dim is not None and self.args.pass_task_inference_latent_to_policy
                # use_belief_encoder = self.args.policy_belief_embedding_dim is not None and self.args.pass_belief_to_policy
                # use_task_encoder = self.args.policy_task_embedding_dim is not None and self.args.pass_task_to_policy
                # use_rim_level1_output_encoder = self.args.policy_rim_level1_output_embedding_dim is not None and self.args.use_rim_level1
                # params = []
                # if use_task_inference_latent_encoder:
                #     params.extend(actor_critic.task_inference_latent_encoder.parameters())
                # if use_belief_encoder:
                #     params.extend(actor_critic.belief_encoder.parameters())
                # if use_task_encoder:
                #     params.extend(actor_critic.task_encoder.parameters())
                # if use_rim_level1_output_encoder:
                #     params.extend(actor_critic.rim_level1_output_encoder.parameters())
                # params.extend(actor_critic.actor_layers.parameters())
                # params.extend(actor_critic.critic_layers.parameters())
                # params.extend(actor_critic.critic_linear.parameters())
                #
                # self.optimiser = optim.Adam([{'params': [*params], 'lr':lr},
                #                              {'params': self.actor_critic.state_encoder.parameters(), 'lr': self.args.lr_vae}], eps=eps)
            else:
                self.optimiser = optim.Adam(actor_critic.parameters(), lr=lr, eps=eps)
        elif policy_optimiser == 'rmsprop':
            raise NotImplementedError
            self.optimiser = optim.RMSprop(actor_critic.parameters(), lr=lr, eps=eps, alpha=0.99)
        self.optimiser_vae = optimiser_vae
        self.hebb_meta_params_optim = hebb_meta_params_optim

        self.lr_scheduler_policy = None
        self.lr_scheduler_encoder = None
        self.lr_scheduler_hebb_meta = None
        if policy_anneal_lr:
            self.lr_scheduler_policy = optim.lr_scheduler.LambdaLR(self.optimiser, lr_lambda=LRPolicy(train_steps=train_steps))
            if hasattr(self.args, 'rlloss_through_encoder') and self.args.rlloss_through_encoder:
                self.lr_scheduler_encoder = optim.lr_scheduler.LambdaLR(self.optimiser_vae, lr_lambda=LRPolicy(train_steps=train_steps))
        if self.args.use_memory and self.args.use_hebb:
            self.lr_scheduler_hebb_meta = optim.lr_scheduler.StepLR(self.hebb_meta_params_optim, step_size=20, gamma=0.1)

    def update(self,
               policy_storage,
               encoder=None,  # variBAD encoder
               rlloss_through_encoder=False,  # whether or not to backprop RL loss through encoder
               compute_vae_loss=None,  # function that can compute the VAE loss
               compute_n_step_value_prediction_loss=None,  # function that can compute the n step value prediction loss
               compute_memory_loss=None,
               activated_branch='exploration',
               random_target_network=None,
               predictor_network=None
               ):

        # -- get action values --
        advantages = policy_storage.returns[:-1] - policy_storage.value_preds[:-1]
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        # if this is true, we will update the VAE at every PPO update
        # otherwise, we update it after we update the policy
        if rlloss_through_encoder:
            # recompute embeddings (to build computation graph)
            utl.recompute_embeddings(self.actor_critic, policy_storage, encoder, sample=False, update_idx=0,
                                     detach_every=self.args.tbptt_stepsize if hasattr(self.args, 'tbptt_stepsize') else None,
                                     activated_branch=activated_branch)

        # update the normalisation parameters of policy inputs before updating
        self.actor_critic.update_rms(args=self.args, policy_storage=policy_storage)

        value_loss_epoch = 0
        action_loss_epoch = 0
        dist_entropy_epoch = 0
        loss_epoch = 0
        for e in range(self.ppo_epoch):

            data_generator = policy_storage.feed_forward_generator(advantages, self.num_mini_batch)
            for sample in data_generator:

                state_batch, belief_batch, task_batch, \
                actions_batch, latent_sample_batch, latent_mean_batch, latent_logvar_batch,\
                brim_output_level1_batch, brim_output_level2_batch, brim_output_level3_batch, policy_embedded_state_batch,\
                value_preds_batch, return_batch, old_action_log_probs_batch, adv_targ = sample

                if not rlloss_through_encoder:
                    state_batch = state_batch.detach()
                    if latent_sample_batch is not None:
                        latent_sample_batch = latent_sample_batch.detach()
                        latent_mean_batch = latent_mean_batch.detach()
                        latent_logvar_batch = latent_logvar_batch.detach()

                latent_batch = utl.get_latent_for_policy(sample_embeddings=self.args.sample_embeddings,
                                                         add_nonlinearity_to_latent=self.args.add_nonlinearity_to_latent,
                                                         latent_sample=latent_sample_batch,
                                                         latent_mean=latent_mean_batch,
                                                         latent_logvar=latent_logvar_batch
                                                         )

                # Reshape to do in a single forward pass for all steps
                values, action_log_probs, dist_entropy, action_mean, action_logstd = \
                    self.actor_critic.evaluate_actions(embedded_state=policy_embedded_state_batch, latent=latent_batch, brim_output_level1=brim_output_level1_batch,
                                                       belief=belief_batch, task=task_batch,
                                                       action=actions_batch, return_action_mean=True
                                                       )
                # zero out the gradients
                self.optimiser.zero_grad()
                if rlloss_through_encoder:
                    self.optimiser_vae.zero_grad()
                if self.args.use_memory and self.args.use_hebb:
                    self.hebb_meta_params_optim.zero_grad()

                ratio = torch.exp(action_log_probs -
                                  old_action_log_probs_batch)
                surr1 = ratio * adv_targ
                surr2 = torch.clamp(ratio, 1.0 - self.clip_param, 1.0 + self.clip_param) * adv_targ
                action_loss = -torch.min(surr1, surr2).mean()

                if self.use_huber_loss and self.use_clipped_value_loss:
                    value_pred_clipped = value_preds_batch + (values - value_preds_batch).clamp(-self.clip_param,
                                                                                                self.clip_param)
                    value_losses = F.smooth_l1_loss(values, return_batch, reduction='none')
                    value_losses_clipped = F.smooth_l1_loss(value_pred_clipped, return_batch, reduction='none')
                    value_loss = 0.5 * torch.max(value_losses, value_losses_clipped).mean()
                elif self.use_huber_loss:
                    value_loss = F.smooth_l1_loss(values, return_batch)
                elif self.use_clipped_value_loss:
                    value_pred_clipped = value_preds_batch + (values - value_preds_batch).clamp(-self.clip_param,
                                                                                                self.clip_param)
                    value_losses = (values - return_batch).pow(2)
                    value_losses_clipped = (value_pred_clipped - return_batch).pow(2)
                    value_loss = 0.5 * torch.max(value_losses, value_losses_clipped).mean()
                else:
                    value_loss = 0.5 * (return_batch - values).pow(2).mean()

                # compute policy loss and backprop
                loss = value_loss * self.value_loss_coef + action_loss - dist_entropy * self.entropy_coef
                # compute vae loss and backprop
                if rlloss_through_encoder:
                    loss += self.args.vae_loss_coeff * compute_vae_loss()
                    if self.args.use_rim_level2:
                        value_prediction_loss = compute_n_step_value_prediction_loss(self.actor_critic, activated_branch)
                        loss += self.args.n_step_value_prediction_coeff * value_prediction_loss
                    if self.args.use_memory:
                        loss += 0.0001 * torch.linalg.norm(encoder.brim.A)
                        loss += 0.0001 * torch.linalg.norm(encoder.brim.B)
                        if self.args.reconstruction_memory_loss:
                            loss += self.args.reconstruction_memory_loss_coef * compute_memory_loss(self.actor_critic, activated_branch)
                if self.args.bebold_intrinsic_reward:
                    loss += self.compute_rnd_loss(random_target_network(state_batch).detach(), predictor_network(state_batch))

                # compute gradients (will attach to all networks involved in this computation)
                loss.backward()
                nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.args.policy_max_grad_norm)
                if (encoder is not None) and rlloss_through_encoder:
                    nn.utils.clip_grad_norm_(encoder.parameters(), self.args.policy_max_grad_norm)

                # update
                self.optimiser.step()
                if rlloss_through_encoder:
                    self.optimiser_vae.step()
                if self.args.use_memory and self.args.use_hebb:
                    self.hebb_meta_params_optim.step()


                value_loss_epoch += value_loss.item()
                action_loss_epoch += action_loss.item()
                dist_entropy_epoch += dist_entropy.item()
                loss_epoch += loss.item()

                if rlloss_through_encoder:
                    # recompute embeddings (to build computation graph)
                    utl.recompute_embeddings(self.actor_critic, policy_storage, encoder, sample=False, update_idx=e + 1,
                                             detach_every=self.args.tbptt_stepsize if hasattr(self.args, 'tbptt_stepsize') else None,
                                             activated_branch=activated_branch,)

        if (not rlloss_through_encoder) and (self.optimiser_vae is not None):
            for _ in range(self.args.num_vae_updates):
                compute_vae_loss(update=True)

        if self.lr_scheduler_policy is not None:
            self.lr_scheduler_policy.step()
        if self.lr_scheduler_encoder is not None:
            self.lr_scheduler_encoder.step()
        if self.lr_scheduler_hebb_meta is not None:
            self.lr_scheduler_hebb_meta.step()

        num_updates = self.ppo_epoch * self.num_mini_batch

        value_loss_epoch /= num_updates
        action_loss_epoch /= num_updates
        dist_entropy_epoch /= num_updates
        loss_epoch /= num_updates

        return value_loss_epoch, action_loss_epoch, dist_entropy_epoch, loss_epoch

    def act(self, embedded_state, latent, brim_output_level1, belief, task, deterministic=False):
        return self.actor_critic.act(embedded_state=embedded_state, latent=latent, brim_output_level1=brim_output_level1, belief=belief, task=task, deterministic=deterministic)

    def compute_rnd_loss(self, pred_next_emb, next_emb):
        forward_dynamics_loss = torch.norm(pred_next_emb - next_emb, dim=-1, p=2)
        return torch.mean(forward_dynamics_loss)