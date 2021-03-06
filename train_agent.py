import os
import numpy as np
from text_localization_environment import TextLocEnv
from chainerrl.links.mlp import MLP
from chainerrl.links import Sequence
from chainerrl.experiments.train_agent import train_agent_with_evaluation
import chainer
import chainerrl
import logging
import sys
from tb_chainer import SummaryWriter
import time
import re

from custom_model import CustomModel
from config import CONFIG, write_config, print_config
from tensorboard_gradient_histogram import TensorboardGradientPlotter


"""
Set arguments w/ config file (--config) or cli
:gpu_id :imagefile_path :boxfile_path :resultdir_path :start_epsilon :end_epsilon :decay_steps \
:replay_buffer_capacity :gamma :replay_start_size :update_interval :target_update_interval :steps \
:steps :eval_n_episodes :train_max_episode_len :eval_interval
"""
def main():
    print_config()

    relative_paths = np.loadtxt(CONFIG['imagefile_path'], dtype=str)
    images_base_path = os.path.dirname(CONFIG['imagefile_path'])
    absolute_paths = [images_base_path + i.strip('.') for i in relative_paths]
    bboxes = np.load(CONFIG['boxfile_path'], allow_pickle=True)

    env = TextLocEnv(absolute_paths, bboxes, CONFIG['gpu_id'])

    n_actions = env.action_space.n
    q_func = chainerrl.q_functions.SingleModelStateQFunctionWithDiscreteAction(CustomModel(n_actions))
    if CONFIG['gpu_id'] != -1:
        q_func = q_func.to_gpu(CONFIG['gpu_id'])

    # Use Adam to optimize q_func. eps=1e-2 is for stability.
    optimizer = chainer.optimizers.Adam(eps=CONFIG['epsilon'], amsgrad=True, alpha=CONFIG['learning_rate'])
    optimizer.setup(q_func)

    # Use epsilon-greedy for exploration
    explorer = chainerrl.explorers.LinearDecayEpsilonGreedy(
        start_epsilon=CONFIG['start_epsilon'],
        end_epsilon=CONFIG['end_epsilon'],
        decay_steps=CONFIG['decay_steps'],
        random_action_func=env.action_space.sample)

    # DQN uses Experience Replay.
    # Specify a replay buffer and its capacity.
    replay_buffer = chainerrl.replay_buffer.EpisodicReplayBuffer(capacity=CONFIG['replay_buffer_capacity'])

    # Now create an agent that will interact with the environment.
    agent = chainerrl.agents.DQN(
        q_func,
        optimizer,
        replay_buffer,
        CONFIG['gamma'],
        explorer,
        gpu=CONFIG['gpu_id'],
        replay_start_size=CONFIG['replay_start_size'],
        update_interval=CONFIG['update_interval'],
        target_update_interval=CONFIG['target_update_interval'])

    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='')

    eval_run_count = 10

    timestr = time.strftime("%Y%m%d-%H%M%S")
    agentClassName = agent.__class__.__name__[:10]

    step_hooks = []
    logger = None
    if CONFIG['use_tensorboard']:
        writer = SummaryWriter("tensorboard/tensorBoard_exp_" + timestr + "_" + agentClassName)
        step_hooks = [TensorBoardLoggingStepHook(writer)]
        handler = TensorBoardEvaluationLoggingHandler(writer, agent, eval_run_count)
        logger = logging.getLogger()
        logger.addHandler(handler)

        gradients_weights_log_interval = 100
        optimizer.add_hook(
            TensorboardGradientPlotter(summary_writer=writer, log_interval=gradients_weights_log_interval)
        )

    # save config file to results dir after initializing agent
    write_config()

    # Overwrite the normal evaluation method
    # chainerrl.experiments.evaluator.run_evaluation_episodes = run_localization_evaluation_episodes

    train_agent_with_evaluation(
        agent,
        env,
        steps=CONFIG['steps'],  # Train the agent for no of steps
        eval_n_episodes=CONFIG['eval_n_episodes'],  # episodes are sampled for each evaluation
        eval_n_steps=None,
        train_max_episode_len=CONFIG['train_max_episode_len'],  # Maximum length of each episodes
        eval_interval=CONFIG['eval_interval'],  # Evaluate the agent after every no of steps
        outdir=CONFIG['resultdir_path'],  # Save everything to directory
        step_hooks=step_hooks,
        logger=logger)

    agent.save('agent_' + timestr + "_" + agentClassName)


class TensorBoardLoggingStepHook(chainerrl.experiments.StepHook):
    def __init__(self, summary_writer):
        self.summary_writer = summary_writer
        return

    def __call__(self, env, agent, step):
        step_count = agent.t
        self.summary_writer.add_scalar('average_q', agent.average_q, step_count)
        self.summary_writer.add_scalar('average_loss', agent.average_loss, step_count)

        return


class TensorBoardEvaluationLoggingHandler(logging.Handler):
    def __init__(self, summary_writer, agent, eval_run_count, level=logging.NOTSET):
        logging.Handler.__init__(self, level)
        self.summary_writer = summary_writer
        self.agent = agent
        self.eval_run_count = eval_run_count
        self.episode_rewards = np.empty(eval_run_count)
        self.episode_lengths = np.empty(eval_run_count)
        self.episode_ious = np.empty(eval_run_count)
        self.episode_max_ious = np.empty(eval_run_count)
        return

    def emit(self, record):
        match_new_best = re.search(r'The best score is updated ([^ ]*) -> ([^ ]*)', record.getMessage())
        if match_new_best:
            new_best_score = match_new_best.group(2)
            step_count = self.agent.t
            self.summary_writer.add_scalar('evaluation_new_best_score', new_best_score, step_count)

        match_reward = re.search(r'evaluation episode ([^ ]*) length:([^ ]*) R:([^ ]*) IoU:([^ ]*) Max_IoU:([^ ]*)', record.getMessage())
        if match_reward:
            episode_number = int(match_reward.group(1))
            episode_length = int(match_reward.group(2))
            episode_reward = float(match_reward.group(3))
            episode_iou = float(match_reward.group(4))
            episode_max_iou = float(match_reward.group(5))

            self.episode_lengths[episode_number] = episode_length
            self.episode_rewards[episode_number] = episode_reward
            self.episode_ious[episode_number] = episode_iou
            self.episode_max_ious[episode_number] = episode_max_iou

            if episode_number == self.eval_run_count - 1:
                step_count = self.agent.t
                self.summary_writer.add_scalar('evaluation_length_mean', np.mean(self.episode_lengths), step_count)
                self.summary_writer.add_scalar('evaluation_reward_mean', np.mean(self.episode_rewards), step_count)
                self.summary_writer.add_scalar('evaluation_reward_median', np.median(self.episode_rewards), step_count)
                self.summary_writer.add_scalar('evaluation_reward_variance', np.var(self.episode_rewards), step_count)
                self.summary_writer.add_scalar('evaluation_iou_mean', np.mean(self.episode_ious), step_count)
                self.summary_writer.add_scalar('evaluation_iou_median', np.median(self.episode_ious), step_count)
                self.summary_writer.add_scalar('evaluation_max_iou_mean', np.mean(self.episode_max_ious), step_count)
        return


def run_localization_evaluation_episodes(env, agent, n_steps, n_episodes, max_episode_len=None,
                            logger=None):
    """Run multiple evaluation episodes and return returns.
    Args:
        env (Environment): Environment used for evaluation
        agent (Agent): Agent to evaluate.
        n_episodes (int): Number of evaluation runs.
        max_episode_len (int or None): If specified, episodes longer than this
            value will be truncated.
        logger (Logger or None): If specified, the given Logger object will be
            used for logging results. If not specified, the default logger of
            this module will be used.
    Returns:
        List of returns of evaluation runs.
    """
    logger = logger or logging.getLogger(__name__)
    scores = []
    for i in range(n_episodes):
        obs = env.reset()
        done = False
        test_r = 0
        t = 0
        while not (done or t == max_episode_len):
            a = agent.act(obs)
            obs, r, done, info = env.step(a)
            test_r += r
            t += 1
        agent.stop_episode()
        # As mixing float and numpy float causes errors in statistics
        # functions, here every score is cast to float.
        iou = float(env.iou) if done else float(0)
        max_iou = float(env.max_iou)
        scores.append(float(test_r))
        logger.info('evaluation episode %s length:%s R:%s IoU:%s Max_IoU:%s', i, t, test_r, iou, max_iou)
    return scores


if __name__ == '__main__':
    main()
