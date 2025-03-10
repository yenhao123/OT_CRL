# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Learner for the PPO agent."""

from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple

import acme
from acme import core
from acme import types
from acme.agents.jax.ppo import networks
from acme.agents.jax.ppo import regularization, bc
from acme.jax import networks as networks_lib
from acme.jax.utils import get_from_first_device
from acme.utils import counting
from acme.utils import loggers
import jax
import jax.nn as jnn
import jax.numpy as jnp
import optax
import reverb
import rlax

import tensorflow_probability
import numpy as np
tfp = tensorflow_probability.substrates.jax
tfd = tfp.distributions

IS_DEBUG = False

class Batch(NamedTuple):
  """A batch of data; all shapes are expected to be [B, ...]."""
  observations: types.NestedArray
  actions: jnp.ndarray
  advantages: jnp.ndarray

  # Target value estimate used to bootstrap the value function.
  target_values: jnp.ndarray

  # Value estimate and action log-prob at behavior time.
  behavior_values: jnp.ndarray
  behavior_log_probs: jnp.ndarray

class BC_Batch(NamedTuple):
    observations: jnp.ndarray
    labels: jnp.ndarray

class TrainingState(NamedTuple):
  """Training state for the PPO learner."""
  params: networks_lib.Params
  opt_state: optax.OptState
  random_key: networks_lib.PRNGKey

  # Optional counter used for exponential moving average zero debiasing
  ema_counter: Optional[jnp.int32] = None

  # Optional parameter for maintaining a running estimate of the scale of
  # advantage estimates
  biased_advantage_scale: Optional[networks_lib.Params] = None
  advantage_scale: Optional[networks_lib.Params] = None

  # Optional parameter for maintaining a running estimate of the mean and
  # standard deviation of value estimates
  biased_value_first_moment: Optional[networks_lib.Params] = None
  biased_value_second_moment: Optional[networks_lib.Params] = None
  value_mean: Optional[networks_lib.Params] = None
  value_std: Optional[networks_lib.Params] = None

class PPOLearner(core.Learner):
  """Learner for PPO."""

  def __init__(
      self,
      ppo_networks: networks.PPONetworks,
      iterator: Iterator[reverb.ReplaySample],
      optimizer: optax.GradientTransformation,
      random_key: networks_lib.PRNGKey,
      ppo_clipping_epsilon: float = 0.2,
      normalize_advantage: bool = True,
      normalize_value: bool = False,
      normalization_ema_tau: float = 0.995,
      clip_value: bool = False,
      value_clipping_epsilon: float = 0.2,
      max_abs_reward: Optional[float] = None,
      gae_lambda: float = 0.95,
      discount: float = 0.99,
      entropy_cost: float = 0.,
      value_cost: float = 1.,
      num_epochs: int = 4,
      num_minibatches: int = 1,
      counter: Optional[counting.Counter] = None,
      logger: Optional[loggers.Logger] = None,
      log_global_norm_metrics: bool = False,
      metrics_logging_period: int = 100,
      olddataset: Iterator[reverb.ReplaySample] = None,
      reg_method: str = "None",
      is_bc: bool = False,
      only_bc: str = "False",
      bc_cost: float = 1.0,
      expertdata_path: str = None,
      end_explore_updateidx: int = None,
  ):
    self.local_learner_devices = jax.local_devices()
    self.num_local_learner_devices = jax.local_device_count()
    self.learner_devices = jax.devices()
    self.num_epochs = num_epochs
    self.num_minibatches = num_minibatches
    self.metrics_logging_period = metrics_logging_period
    self._num_full_update_steps = 0
    self._iterator = iterator
    self.learner_idx = None

    self.olddataset = olddataset
    self.reg_method = reg_method
    self.reg_object = None
    self.is_bc = is_bc
    self.only_bc = only_bc
    if self.is_bc:
      self.bc_object = bc.BC(expertdata_path, bc_cost, end_explore_updateidx)
      if self.bc_object.first_round and not IS_DEBUG:
        self.is_bc = False
    else:
      self.bc_object = None

    self.regularization = 0.0
    
    # Set up logging/counting.
    self._counter = counter or counting.Counter()
    self._logger = logger or loggers.make_default_logger('learner')
    

    def ppo_loss(
        params: networks_lib.Params,
        observations: networks_lib.Observation,
        actions: networks_lib.Action,
        advantages: jnp.ndarray,
        target_values: networks_lib.Value,
        behavior_values: networks_lib.Value,
        behavior_log_probs: networks_lib.LogProb,
        value_mean: jnp.ndarray,
        value_std: jnp.ndarray,
        key: networks_lib.PRNGKey,
    ) -> Tuple[jnp.ndarray, Dict[str, jnp.ndarray]]:
      '''
      if IS_DEBUG:
        jax.debug.print("[Key: {} observe input, observation: {}]", key, observations.shape)
        jax.debug.print("[Key: {} observe input, actions: {}]", key, actions.shape)
        jax.debug.print("[Key: {} observe input, advantage: {}]", key, advantages)
        jax.debug.print("[Key: {} observe input, target value: {}]", key, target_values)
      
      if IS_DEBUG:
        print("Model architecture...")
        for layer_name, value in params.items():
          print("[Layer_name: {}...]".format(layer_name))
          for k, v in value.items():
            print("[Key: {} ,shape: {}]".format(k, v.shape))
      '''
      
      """PPO loss for the policy and the critic."""
      distribution_params, values = ppo_networks.network.apply(
          params, observations)
    
      '''
      if IS_DEBUG:
        jax.debug.print("Obs: {}", observations)
        jax.debug.print("[Key: {} Distribution_params: {}, Values: {}]", key, distribution_params, values)
      '''
      if normalize_value:
        # values = values * jnp.fmax(value_std, 1e-6) + value_mean
        target_values = (target_values - value_mean) / jnp.fmax(value_std, 1e-6)
      
      # 選到這個 action 的可能性
      policy_log_probs = ppo_networks.log_prob(distribution_params, actions)
      key, sub_key = jax.random.split(key)  # pylint: disable=unused-variable
      policy_entropies = ppo_networks.entropy(distribution_params)
      
      # Compute the policy losses
      rhos = jnp.exp(policy_log_probs - behavior_log_probs)
      clipped_ppo_policy_loss = rlax.clipped_surrogate_pg_loss(
          rhos, advantages, ppo_clipping_epsilon)
      policy_entropy_loss = -jnp.mean(policy_entropies)
      total_policy_loss = (
          clipped_ppo_policy_loss + entropy_cost * policy_entropy_loss)

      # Compute the critic losses
      unclipped_value_loss = (values - target_values)**2
      if IS_DEBUG:
        jax.debug.print("[Mean imporance rate: {}]", jnp.mean(rhos))
        
      # default is False
      if clip_value:
        # Clip values to reduce variablility during critic training.
        clipped_values = behavior_values + jnp.clip(values - behavior_values,
                                                    -value_clipping_epsilon,
                                                    value_clipping_epsilon)
        clipped_value_error = target_values - clipped_values
        clipped_value_loss = clipped_value_error ** 2
        value_loss = jnp.mean(jnp.fmax(unclipped_value_loss,
                                       clipped_value_loss))
      else:
        # For Mujoco envs clipping hurts a lot. Evidenced by Figure 43 in
        # https://arxiv.org/pdf/2006.05990.pdf
        value_loss = jnp.mean(unclipped_value_loss)
      
      if self.is_bc:
        total_ppo_loss_wo_bc = total_policy_loss + value_cost * value_loss
        state_bc, ideal_action_bc_all = self.bc_object.sample()
        #v10
        #state_bc = state_bc[:, self.learner_idx]
        distribution_params_bc, _ = ppo_networks.network.apply(params, state_bc)
        ideal_action_bc = jnp.array(ideal_action_bc_all[:, self.learner_idx].tolist())
        pred_logits = distribution_params_bc.logits
        #pred_probs = jnn.softmax(pred_logits, axis=-1)
        #bc_loss = jnp.mean(jnp.square(ideal_action_bc - pred_probs))
        bc_loss = np.mean(tfd.Categorical(probs=ideal_action_bc).cross_entropy(tfd.Categorical(logits=distribution_params_bc)))
        if self.only_bc == "True":
          total_ppo_loss_wo_bc = 0

        total_ppo_loss = total_ppo_loss_wo_bc + self.bc_object.lr_rate * bc_loss
        '''
        if IS_DEBUG:
          jax.debug.print("[BC State: {}]", state_bc)

          jax.debug.print("[Logits: {}]", pred_logits)
          jax.debug.print("[Pred prob: {}]", pred_probs)

          jax.debug.print("[Ideal action: {}]", ideal_action_bc)
          jax.debug.print("[BC loss: {}]", bc_loss)
          jax.debug.print("[PPO loss: {}]", total_ppo_loss_wo_bc)
          jax.debug.print("[Total loss: {}]", total_ppo_loss)
        '''

        return total_ppo_loss, {
            'loss_total': total_ppo_loss,
            'loss_total_wo_bc': total_ppo_loss_wo_bc,
            'bc_loss': bc_loss,
            'loss_policy_total': total_policy_loss,
            'loss_policy_pg': clipped_ppo_policy_loss,
            'loss_policy_entropy': policy_entropy_loss,
            'loss_critic': value_loss
        }
      if self.reg_object is None:
        total_ppo_loss = total_policy_loss + value_cost * value_loss

        return total_ppo_loss, {
            'loss_total': total_ppo_loss,
            'loss_policy_total': total_policy_loss,
            'loss_policy_pg': clipped_ppo_policy_loss,
            'loss_policy_entropy': policy_entropy_loss,
            'loss_critic': value_loss,
        }
      else:
        total_ppo_loss_wo_reg = total_policy_loss + value_cost * value_loss
        regularization =  self.reg_object.regularize(params)
        total_ppo_loss = total_ppo_loss_wo_reg + regularization
        if IS_DEBUG:
          jax.debug.print("{}",regularization)
          jax.debug.print("{}",total_ppo_loss)
          jax.debug.print("{}", total_ppo_loss_wo_reg)
        return total_ppo_loss, {
            'loss_total': total_ppo_loss,
            'loss_total_wo_reg': total_ppo_loss_wo_reg,
            'loss_policy_total': total_policy_loss,
            'loss_policy_pg': clipped_ppo_policy_loss,
            'loss_policy_entropy': policy_entropy_loss,
            'loss_critic': value_loss,
        }

    ppo_loss_grad = jax.grad(ppo_loss, has_aux=True)

    def sgd_step(state: TrainingState, minibatch: Batch):

      observations = minibatch.observations
      actions = minibatch.actions
      advantages = minibatch.advantages
      target_values = minibatch.target_values
      behavior_values = minibatch.behavior_values
      behavior_log_probs = minibatch.behavior_log_probs
      key, sub_key = jax.random.split(state.random_key)

      loss_grad, metrics = ppo_loss_grad(
          state.params,
          observations,
          actions,
          advantages,
          target_values,
          behavior_values,
          behavior_log_probs,
          state.value_mean,
          state.value_std,
          sub_key,
      )


      # Apply updates
      loss_grad = jax.lax.pmean(loss_grad, axis_name='devices')

      updates, opt_state = optimizer.update(loss_grad, state.opt_state)
      params = optax.apply_updates(state.params, updates)

      if log_global_norm_metrics:
        metrics['norm_grad'] = optax.global_norm(loss_grad)
        metrics['norm_updates'] = optax.global_norm(updates)

      new_state = state._replace(
          params=params, opt_state=opt_state, random_key=key)

      return new_state, metrics


    def epoch_update(
        carry: Tuple[TrainingState, Batch],
        unused_t: Tuple[()],
    ):
      state, carry_batch = carry

      # Shuffling into minibatches
      batch_size = carry_batch.advantages.shape[0]
      key, sub_key = jax.random.split(state.random_key)
      # TODO(kamyar) For effiency could use same permutation for all epochs
      permuted_batch = jax.tree_util.tree_map(
          lambda x: jax.random.permutation(  # pylint: disable=g-long-lambda
              sub_key,
              x,
              axis=0,
              independent=False),
          carry_batch)
      state = state._replace(random_key=key)
      minibatches = jax.tree_util.tree_map(
          lambda x: jnp.reshape(  # pylint: disable=g-long-lambda
              x,
              [  # pylint: disable=g-long-lambda
                  num_minibatches, batch_size // num_minibatches
              ] + list(x.shape[1:])),
          permuted_batch)

      # Scan over the minibatches, arg[0]: 每次執行的 function、arg[1]: model architecture、arg[2]: 輸入序列、arg[3]: 執行幾次
      state, metrics = jax.lax.scan(
          sgd_step, state, minibatches, length=num_minibatches)
      metrics = jax.tree_util.tree_map(jnp.mean, metrics)

      return (state, carry_batch), metrics

    vmapped_network_apply = jax.vmap(
        ppo_networks.network.apply, in_axes=(None, 0), out_axes=0)

    def single_device_update(
        state: TrainingState,
        trajectories: types.NestedArray,
    ):
      # Update the EMA counter and obtain the zero debiasing multiplier
      if normalize_advantage or normalize_value:
        ema_counter = state.ema_counter + 1
        state = state._replace(ema_counter=ema_counter)
        zero_debias = 1. / (1. - jnp.power(normalization_ema_tau, ema_counter))

      # Extract the data.
      data = trajectories.data
      observations, actions, rewards, termination, extra = (data.observation,
                                                            data.action,
                                                            data.reward,
                                                            data.discount,
                                                            data.extras)

      if max_abs_reward is not None:
        # Apply reward clipping.
        rewards = jnp.clip(rewards, -1. * max_abs_reward, max_abs_reward)
      discounts = termination * discount
      behavior_log_probs = extra['log_prob']
      # Get loss
      _, behavior_values = vmapped_network_apply(state.params, observations)

      if normalize_value:
        batch_value_first_moment = jnp.mean(behavior_values)
        batch_value_second_moment = jnp.mean(behavior_values**2)
        batch_value_first_moment, batch_value_second_moment = jax.lax.pmean(
            (batch_value_first_moment, batch_value_second_moment),
            axis_name='devices')

        biased_value_first_moment = (
            normalization_ema_tau * state.biased_value_first_moment +
            (1. - normalization_ema_tau) * batch_value_first_moment)
        biased_value_second_moment = (
            normalization_ema_tau * state.biased_value_second_moment +
            (1. - normalization_ema_tau) * batch_value_second_moment)

        value_mean = biased_value_first_moment * zero_debias
        value_second_moment = biased_value_second_moment * zero_debias
        value_std = jnp.sqrt(jax.nn.relu(value_second_moment - value_mean**2))

        state = state._replace(
            biased_value_first_moment=biased_value_first_moment,
            biased_value_second_moment=biased_value_second_moment,
            value_mean=value_mean,
            value_std=value_std,
        )

        behavior_values = behavior_values * jnp.fmax(state.value_std,
                                                     1e-6) + state.value_mean

      behavior_values = jax.lax.stop_gradient(behavior_values)
      # Compute GAE using rlax
      vmapped_rlax_truncated_generalized_advantage_estimation = jax.vmap(
          rlax.truncated_generalized_advantage_estimation,
          in_axes=(0, 0, None, 0))
      advantages = vmapped_rlax_truncated_generalized_advantage_estimation(
          rewards[:, :-1], discounts[:, :-1], gae_lambda, behavior_values)
      
      advantages = jax.lax.stop_gradient(advantages)
      target_values = behavior_values[:, :-1] + advantages
      target_values = jax.lax.stop_gradient(target_values)
      
      # Exclude the last step - it was only used for bootstrapping.
      # The shape is [num_sequences, num_steps, ..]
      observations, actions, behavior_log_probs, behavior_values = jax.tree_util.tree_map(
          lambda x: x[:, :-1],
          (observations, actions, behavior_log_probs, behavior_values))

      # Shuffle the data and break into minibatches
      batch_size = advantages.shape[0] * advantages.shape[1]
      batch = Batch(
          observations=observations,
          actions=actions,
          advantages=advantages,
          target_values=target_values,
          behavior_values=behavior_values,
          behavior_log_probs=behavior_log_probs)
      batch = jax.tree_util.tree_map(
          lambda x: jnp.reshape(x, [batch_size] + list(x.shape[2:])), batch)
      
      if IS_DEBUG:
        jax.debug.print("[Advantage Before scaling : {}]", batch.advantages)
        
      
      if normalize_advantage:
        batch_advantage_scale = jnp.mean(jnp.abs(batch.advantages))
        batch_advantage_scale = jax.lax.pmean(batch_advantage_scale, 'devices')

        # update the running statistics
        biased_advantage_scale = (
            normalization_ema_tau * state.biased_advantage_scale +
            (1. - normalization_ema_tau) * batch_advantage_scale)
        advantage_scale = biased_advantage_scale * zero_debias
        state = state._replace(
            biased_advantage_scale=biased_advantage_scale,
            advantage_scale=advantage_scale)

        # scale the advantages
        scaled_advantages = batch.advantages / jnp.fmax(state.advantage_scale,
                                                        1e-6)
        batch = batch._replace(advantages=scaled_advantages)

      if IS_DEBUG:
        jax.debug.print("[Advantage After scaling : {}]", batch.advantages)

      # Scan desired number of epoch updates
      (state, _), metrics = jax.lax.scan(
          epoch_update, (state, batch), (), length=num_epochs)
      metrics = jax.tree_util.tree_map(jnp.mean, metrics)

      if normalize_advantage:
        metrics['advantage_scale'] = state.advantage_scale

      if normalize_value:
        metrics['value_mean'] = value_mean
        metrics['value_std'] = value_std

      return state, metrics

    pmapped_update_step = jax.pmap(
        single_device_update, axis_name='devices', devices=self.learner_devices)
    
    def full_update_step(
        state: TrainingState,
        trajectories: types.NestedArray,
    ):
      state, metrics = pmapped_update_step(state, trajectories)

      return state, metrics

    self._full_update_step = full_update_step

    def init_ppo_loss(
        params: networks_lib.Params,
        observations: networks_lib.Observation,
        actions: networks_lib.Action,
        advantages: jnp.ndarray,
        target_values: networks_lib.Value,
        behavior_values: networks_lib.Value,
        behavior_log_probs: networks_lib.LogProb,
        value_mean: jnp.ndarray,
        value_std: jnp.ndarray,
        key: networks_lib.PRNGKey,
    ) -> Tuple[jnp.ndarray, Dict[str, jnp.ndarray]]:
      
      """PPO loss for the policy and the critic."""
      distribution_params, values = ppo_networks.network.apply(
          params, observations)
    
      if normalize_value:
        # values = values * jnp.fmax(value_std, 1e-6) + value_mean
        target_values = (target_values - value_mean) / jnp.fmax(value_std, 1e-6)
      
      # 選到這個 action 的可能性
      policy_log_probs = ppo_networks.log_prob(distribution_params, actions)
      key, sub_key = jax.random.split(key)  # pylint: disable=unused-variable
      policy_entropies = ppo_networks.entropy(distribution_params)
      
      # Compute the policy losses
      rhos = jnp.exp(policy_log_probs - behavior_log_probs)
      clipped_ppo_policy_loss = rlax.clipped_surrogate_pg_loss(
          rhos, advantages, ppo_clipping_epsilon)
      policy_entropy_loss = -jnp.mean(policy_entropies)
      total_policy_loss = (
          clipped_ppo_policy_loss + entropy_cost * policy_entropy_loss)

      # Compute the critic losses
      unclipped_value_loss = (values - target_values)**2

      # default is False
      if clip_value:
        # Clip values to reduce variablility during critic training.
        clipped_values = behavior_values + jnp.clip(values - behavior_values,
                                                    -value_clipping_epsilon,
                                                    value_clipping_epsilon)
        clipped_value_error = target_values - clipped_values
        clipped_value_loss = clipped_value_error ** 2
        value_loss = jnp.mean(jnp.fmax(unclipped_value_loss,
                                       clipped_value_loss))
      else:
        # For Mujoco envs clipping hurts a lot. Evidenced by Figure 43 in
        # https://arxiv.org/pdf/2006.05990.pdf
        value_loss = jnp.mean(unclipped_value_loss)
      
      if self.is_bc:
        total_ppo_loss_wo_bc = total_policy_loss + value_cost * value_loss
        total_ppo_loss = total_ppo_loss_wo_bc
        bc_loss = -1
        
        return total_ppo_loss, {
            'loss_total': total_ppo_loss,
            'loss_total_wo_bc': total_ppo_loss_wo_bc,
            'bc_loss': bc_loss,
            'loss_policy_total': total_policy_loss,
            'loss_policy_pg': clipped_ppo_policy_loss,
            'loss_policy_entropy': policy_entropy_loss,
            'loss_critic': value_loss
        }
      if self.reg_object is None:
        total_ppo_loss = total_policy_loss + value_cost * value_loss

        return total_ppo_loss, {
            'loss_total': total_ppo_loss,
            'loss_policy_total': total_policy_loss,
            'loss_policy_pg': clipped_ppo_policy_loss,
            'loss_policy_entropy': policy_entropy_loss,
            'loss_critic': value_loss,
        }
      else:
        total_ppo_loss_wo_reg = total_policy_loss + value_cost * value_loss
        regularization =  self.reg_object.regularize(params)
        total_ppo_loss = total_ppo_loss_wo_reg + regularization

        return total_ppo_loss, {
            'loss_total': total_ppo_loss,
            'loss_total_wo_reg': total_ppo_loss_wo_reg,
            'loss_policy_total': total_policy_loss,
            'loss_policy_pg': clipped_ppo_policy_loss,
            'loss_policy_entropy': policy_entropy_loss,
            'loss_critic': value_loss,
        }

    init_ppo_loss_grad = jax.grad(init_ppo_loss, has_aux=True)
    
    def init_sgd_step(state: TrainingState, minibatch: Batch):

      observations = minibatch.observations
      actions = minibatch.actions
      advantages = minibatch.advantages
      target_values = minibatch.target_values
      behavior_values = minibatch.behavior_values
      behavior_log_probs = minibatch.behavior_log_probs
      key, sub_key = jax.random.split(state.random_key)

      loss_grad, metrics = init_ppo_loss_grad(
          state.params,
          observations,
          actions,
          advantages,
          target_values,
          behavior_values,
          behavior_log_probs,
          state.value_mean,
          state.value_std,
          sub_key,
      )


      # Apply updates
      loss_grad = jax.lax.pmean(loss_grad, axis_name='devices')
      updates, opt_state = optimizer.update(loss_grad, state.opt_state)
      params = optax.apply_updates(state.params, updates)

      if log_global_norm_metrics:
        metrics['norm_grad'] = optax.global_norm(loss_grad)
        metrics['norm_updates'] = optax.global_norm(updates)

      new_state = state._replace(
          params=params, opt_state=opt_state, random_key=key)

      return new_state, metrics

    def init_epoch_update(
        carry: Tuple[TrainingState, Batch],
        unused_t: Tuple[()],
    ):
      state, carry_batch = carry

      # Shuffling into minibatches
      batch_size = carry_batch.advantages.shape[0]
      key, sub_key = jax.random.split(state.random_key)
      # TODO(kamyar) For effiency could use same permutation for all epochs
      permuted_batch = jax.tree_util.tree_map(
          lambda x: jax.random.permutation(  # pylint: disable=g-long-lambda
              sub_key,
              x,
              axis=0,
              independent=False),
          carry_batch)
      state = state._replace(random_key=key)
      minibatches = jax.tree_util.tree_map(
          lambda x: jnp.reshape(  # pylint: disable=g-long-lambda
              x,
              [  # pylint: disable=g-long-lambda
                  num_minibatches, batch_size // num_minibatches
              ] + list(x.shape[1:])),
          permuted_batch)

      # Scan over the minibatches, arg[0]: 每次執行的 function、arg[1]: model architecture、arg[2]: 輸入序列、arg[3]: 執行幾次
      state, metrics = jax.lax.scan(
          init_sgd_step, state, minibatches, length=num_minibatches)
      metrics = jax.tree_util.tree_map(jnp.mean, metrics)

      return (state, carry_batch), metrics

    def init_single_device_update(
        state: TrainingState,
        trajectories: types.NestedArray,
    ):
      # Update the EMA counter and obtain the zero debiasing multiplier
      if normalize_advantage or normalize_value:
        ema_counter = state.ema_counter + 1
        state = state._replace(ema_counter=ema_counter)
        zero_debias = 1. / (1. - jnp.power(normalization_ema_tau, ema_counter))

      # Extract the data.
      data = trajectories.data
      observations, actions, rewards, termination, extra = (data.observation,
                                                            data.action,
                                                            data.reward,
                                                            data.discount,
                                                            data.extras)
      if max_abs_reward is not None:
        # Apply reward clipping.
        rewards = jnp.clip(rewards, -1. * max_abs_reward, max_abs_reward)
      discounts = termination * discount
      behavior_log_probs = extra['log_prob']
      # Get loss
      _, behavior_values = vmapped_network_apply(state.params, observations)

      if normalize_value:
        batch_value_first_moment = jnp.mean(behavior_values)
        batch_value_second_moment = jnp.mean(behavior_values**2)
        batch_value_first_moment, batch_value_second_moment = jax.lax.pmean(
            (batch_value_first_moment, batch_value_second_moment),
            axis_name='devices')

        biased_value_first_moment = (
            normalization_ema_tau * state.biased_value_first_moment +
            (1. - normalization_ema_tau) * batch_value_first_moment)
        biased_value_second_moment = (
            normalization_ema_tau * state.biased_value_second_moment +
            (1. - normalization_ema_tau) * batch_value_second_moment)

        value_mean = biased_value_first_moment * zero_debias
        value_second_moment = biased_value_second_moment * zero_debias
        value_std = jnp.sqrt(jax.nn.relu(value_second_moment - value_mean**2))

        state = state._replace(
            biased_value_first_moment=biased_value_first_moment,
            biased_value_second_moment=biased_value_second_moment,
            value_mean=value_mean,
            value_std=value_std,
        )

        behavior_values = behavior_values * jnp.fmax(state.value_std,
                                                     1e-6) + state.value_mean

      behavior_values = jax.lax.stop_gradient(behavior_values)
      # Compute GAE using rlax
      vmapped_rlax_truncated_generalized_advantage_estimation = jax.vmap(
          rlax.truncated_generalized_advantage_estimation,
          in_axes=(0, 0, None, 0))
      advantages = vmapped_rlax_truncated_generalized_advantage_estimation(
          rewards[:, :-1], discounts[:, :-1], gae_lambda, behavior_values)
      
      advantages = jax.lax.stop_gradient(advantages)
      target_values = behavior_values[:, :-1] + advantages
      target_values = jax.lax.stop_gradient(target_values)
      
      # Exclude the last step - it was only used for bootstrapping.
      # The shape is [num_sequences, num_steps, ..]
      observations, actions, behavior_log_probs, behavior_values = jax.tree_util.tree_map(
          lambda x: x[:, :-1],
          (observations, actions, behavior_log_probs, behavior_values))

      # Shuffle the data and break into minibatches
      batch_size = advantages.shape[0] * advantages.shape[1]
      batch = Batch(
          observations=observations,
          actions=actions,
          advantages=advantages,
          target_values=target_values,
          behavior_values=behavior_values,
          behavior_log_probs=behavior_log_probs)
      batch = jax.tree_util.tree_map(
          lambda x: jnp.reshape(x, [batch_size] + list(x.shape[2:])), batch)
      
      if IS_DEBUG:
        jax.debug.print("[Advantage Before scaling : {}]", batch.advantages)
        
      
      if normalize_advantage:
        batch_advantage_scale = jnp.mean(jnp.abs(batch.advantages))
        batch_advantage_scale = jax.lax.pmean(batch_advantage_scale, 'devices')

        # update the running statistics
        biased_advantage_scale = (
            normalization_ema_tau * state.biased_advantage_scale +
            (1. - normalization_ema_tau) * batch_advantage_scale)
        advantage_scale = biased_advantage_scale * zero_debias
        state = state._replace(
            biased_advantage_scale=biased_advantage_scale,
            advantage_scale=advantage_scale)

        # scale the advantages
        scaled_advantages = batch.advantages / jnp.fmax(state.advantage_scale, 1e-6)
        batch = batch._replace(advantages=scaled_advantages)

      if IS_DEBUG:
        jax.debug.print("[Advantage After scaling : {}]", batch.advantages)

      # Scan desired number of epoch updates
      (state, _), metrics = jax.lax.scan(
          init_epoch_update, (state, batch), (), length=num_epochs)
      metrics = jax.tree_util.tree_map(jnp.mean, metrics)

      if normalize_advantage:
        metrics['advantage_scale'] = state.advantage_scale

      if normalize_value:
        metrics['value_mean'] = value_mean
        metrics['value_std'] = value_std

      return state, metrics

    init_pmapped_update_step = jax.pmap(
        init_single_device_update, axis_name='devices', devices=self.learner_devices)

    def init_update_step(
            state: TrainingState,
            trajectories: types.NestedArray,
        ):
          state, metrics = init_pmapped_update_step(state, trajectories)

          return state, metrics

    self._init_update_step = init_update_step
    
    def bc_loss(
        params,
        observations,
        labels,
    ) -> Tuple[jnp.ndarray, Dict[str, jnp.ndarray]]:
      
      if self.is_bc:
        distribution_params_bc, _ = ppo_networks.network.apply(params, observations)
        bc_loss = np.mean(tfd.Categorical(probs=labels).cross_entropy(tfd.Categorical(logits=distribution_params_bc)))
        total_bc_loss = self.bc_object.lr_rate * bc_loss
        if IS_DEBUG:
          jax.debug.print("[BC State: {}]", observations)
          jax.debug.print("[Pred logits: {}]", distribution_params_bc.logits)
          jax.debug.print("[Pred action: {}]", jnn.softmax(distribution_params_bc.logits, axis=-1))
          jax.debug.print("[Ideal action: {}]", labels)
          jax.debug.print("[BC loss: {}]", bc_loss)
          jax.debug.print("[Total loss: {}]", total_bc_loss)

        return total_bc_loss, {
            'loss_total': total_bc_loss,
            'bc_loss': bc_loss,
        }
        
    bc_loss_grad = jax.grad(bc_loss, has_aux=True)

    def bc_sgd_step(state: TrainingState, batch):

      observations = batch.observations
      labels = batch.labels
      loss_grad, metrics = bc_loss_grad(
          state.params,
          observations,
          labels,
      )

      # Apply updates
      loss_grad = jax.lax.pmean(loss_grad, axis_name='devices')

      updates, opt_state = optimizer.update(loss_grad, state.opt_state)
      params = optax.apply_updates(state.params, updates)

      new_state = state._replace(params=params, opt_state=opt_state)

      return new_state, metrics

    def bc_epoch_update(
      state: TrainingState,
      batch
    ):
      # 定義 scan 的 step 函數
      def scan_step(carry, _):
          state, sum_metrics = carry
          state, metrics = bc_sgd_step(state, batch)
          sum_metrics = jax.tree_map(lambda s, m: s + m, sum_metrics, metrics)
          return (state, sum_metrics), None

      # 初始化 sum_metrics 為全零的 metrics 結構
      initial_metrics = bc_loss_grad(state.params, batch.observations, batch.labels)[1]
      sum_metrics = jax.tree_util.tree_map(lambda x: jnp.zeros_like(x), initial_metrics)

      # 使用 jax.lax.scan 進行迭代
      (state, sum_metrics), _ = jax.lax.scan(scan_step, (state, sum_metrics), None, length=num_epochs)

      # 計算平均 metrics
      avg_metrics = jax.tree_map(lambda s: s / num_epochs, sum_metrics)

      return state, avg_metrics
    
    def bc_single_device_update(
        state: TrainingState,
        batch,
    ):
      state, metrics = bc_epoch_update(state, batch)
      metrics = jax.tree_util.tree_map(jnp.mean, metrics)

      return state, metrics
    
    bc_pmapped_update_step = jax.pmap(
        bc_single_device_update, axis_name='devices', devices=self.learner_devices)

    def bc_update_step(
        state: TrainingState,
        batch: BC_Batch,
    ):

      state, metrics = bc_pmapped_update_step(state, batch)
      if IS_DEBUG:
        raise "1"
      return state, metrics

    self._bc_update_step = bc_update_step


    def make_initial_state(key: networks_lib.PRNGKey) -> TrainingState:

      """Initialises the training state (parameters and optimiser state)."""
      all_keys = jax.random.split(key, num=self.num_local_learner_devices + 1)
      key_init, key_state = all_keys[0], all_keys[1:]
      key_state = [key_state[i] for i in range(self.num_local_learner_devices)]
      key_state = jax.device_put_sharded(key_state, self.local_learner_devices)

      initial_params = ppo_networks.network.init(key_init)
      initial_opt_state = optimizer.init(initial_params)

      initial_params = jax.device_put_replicated(initial_params,
                                                 self.local_learner_devices)
      initial_opt_state = jax.device_put_replicated(initial_opt_state,
                                                    self.local_learner_devices)

      ema_counter = jnp.int32(0)
      ema_counter = jax.device_put_replicated(ema_counter,
                                              self.local_learner_devices)

      init_state = TrainingState(
          params=initial_params,
          opt_state=initial_opt_state,
          random_key=key_state,
          ema_counter=ema_counter,
      )

      if normalize_advantage:
        biased_advantage_scale = jax.device_put_replicated(
            jnp.zeros([]), self.local_learner_devices)
        advantage_scale = jax.device_put_replicated(
            jnp.zeros([]), self.local_learner_devices)

        init_state = init_state._replace(
            biased_advantage_scale=biased_advantage_scale,
            advantage_scale=advantage_scale)

      if normalize_value:
        biased_value_first_moment = jax.device_put_replicated(
            jnp.zeros([]), self.local_learner_devices)
        value_mean = biased_value_first_moment

        biased_value_second_moment = jax.device_put_replicated(
            jnp.zeros([]), self.local_learner_devices)
        value_second_moment = biased_value_second_moment
        value_std = jnp.sqrt(jax.nn.relu(value_second_moment - value_mean**2))

        init_state = init_state._replace(
            biased_value_first_moment=biased_value_first_moment,
            biased_value_second_moment=biased_value_second_moment,
            value_mean=value_mean,
            value_std=value_std)

      return init_state

    # Initialise training state (parameters and optimizer state).
    self._state = make_initial_state(random_key)

  def step(self):
    """Does a learner step and logs the results.

    One learner step consists of (possibly multiple) epochs of PPO updates on
    a batch of NxT steps collected by the actors.
    """
    sample = next(self._iterator)
    # CL
    if self.is_bc:
      if self._num_full_update_steps >= self.bc_object.end_explore_updateidx:
        state_bc, ideal_action_bc_all = self.bc_object.sample()
        batch = BC_Batch(
          observations=jnp.expand_dims(jnp.array(state_bc), axis=0),
          labels=jnp.expand_dims(jnp.array(ideal_action_bc_all[:, self.learner_idx].tolist()), axis=0),
        )
        self._state, results = self._bc_update_step(self._state, batch)
      else:
        self._state, results = self._init_update_step(self._state, sample)
    else:
      # RL
      self._state, results = self._init_update_step(self._state, sample)


    # Update our counts and record it.
    counts = self._counter.increment(steps=self.num_epochs * self.num_minibatches)

    # Snapshot and attempt to write logs. 
    if self._num_full_update_steps % self.metrics_logging_period == 0:
      results = jax.tree_util.tree_map(jnp.mean, results)
      self._logger.write({**results, **counts})

    self._num_full_update_steps += 1

  def get_variables(self, names: List[str]) -> List[networks_lib.Params]:
    params = get_from_first_device(self._state.params, as_numpy=False)
    return [params]

  def save(self) -> TrainingState:
    return get_from_first_device(self._state, as_numpy=False)

  def restore(self, state: TrainingState):

    # TODO(kamyar) Should the random_key come from self._state instead?
    random_key = state.random_key
    random_key = jax.random.split(
        random_key, num=self.num_local_learner_devices)
    random_key = jax.device_put_sharded(
        [random_key[i] for i in range(self.num_local_learner_devices)],
        self.local_learner_devices)

    state = jax.device_put_replicated(state, self.local_learner_devices)
    state = state._replace(random_key=random_key)
    self.old_state = state
    self._state = state

    if self.reg_method == "l2":
      self.reg_object = regularization.L2(state.params)
    elif self.reg_method == "ewc":
      self.reg_object = regularization.EWC(self.ppo_networks, state.params, [1])
    elif self.reg_method == "None":
      self.reg_object = None
    else:
      raise "No this regularization approach"

