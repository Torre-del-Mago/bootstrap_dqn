from __future__ import print_function
import os
import numpy as np
from collections import Counter
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import datetime
import time
from dqn_model import EnsembleNet, NetWithPrior
from dqn_utils import seed_everything, write_info_file, generate_gif, save_checkpoint
from env import Environment
from replay import ReplayMemory
import config
# from torch.utils.tensorboard import SummaryWriter
import mlflow
import mlflow.pytorch

torch.set_num_threads(2)

def rolling_average(a, n=5):
    if n == 0:
        return a
    ret = np.cumsum(a, dtype=float)
    ret[n:] = ret[n:] - ret[:-n]
    return ret[n - 1:] / n

def log_dict_losses(writer, plot_dict, step):
    for n in plot_dict.keys():
        print('logging', n)
        writer.add_scalar(n, plot_dict[n]['val'], step)

# def tensorboard_log_all(p, writer, step):
#     epoch_num = len(p['steps'])
#     epochs = np.arange(epoch_num)
#     steps = p['steps']

#     # Log data to TensorBoard
#     log_dict_losses(writer, {'episode steps': {'index': epochs, 'val': p['episode_step']}}, step)
#     log_dict_losses(writer, {'episode steps': {'index': epochs, 'val': p['episode_relative_times']}}, step)
#     log_dict_losses(writer, {'episode head': {'index': epochs, 'val': p['episode_head']}}, step)
#     log_dict_losses(writer, {'steps loss': {'index': steps, 'val': p['episode_loss']}}, step)
#     log_dict_losses(writer, {'steps eps': {'index': steps, 'val': p['eps_list']}}, step)
#     log_dict_losses(writer, {'steps reward': {'index': steps, 'val': p['episode_reward']}}, step)
#     log_dict_losses(writer, {'episode reward': {'index': epochs, 'val': p['episode_reward']}}, step)
#     log_dict_losses(writer, {'episode times': {'index': epochs, 'val': p['episode_times']}}, step)
#     log_dict_losses(writer, {'steps avg reward': {'index': steps, 'val': p['avg_rewards']}}, step)
#     log_dict_losses(writer, {'eval rewards': {'index': p['eval_steps'], 'val': p['eval_rewards']}}, step)

def mlflow_log_all(p, step):
    mlflow.log_metric("episode_step", p['episode_step'][-1], step)
    mlflow.log_metric("episode_head", p['episode_head'][-1], step)
    mlflow.log_metric("eps_list", p['eps_list'][-1], step)
    mlflow.log_metric("episode_loss", p['episode_loss'][-1], step)
    mlflow.log_metric("episode_reward", p['episode_reward'][-1], step)
    mlflow.log_metric("episode_times", p['episode_times'][-1], step)
    mlflow.log_metric("episode_relative_times", p['episode_relative_times'][-1], step)
    mlflow.log_metric("avg_rewards", p['avg_rewards'][-1], step)
    mlflow.log_metric("eval_rewards", p['eval_rewards'][-1], step)
    mlflow.log_metric("eval_steps", p['eval_steps'][-1], step)

def handle_checkpoint(last_save, cnt):
    if (cnt - last_save) >= info['CHECKPOINT_EVERY_STEPS']:
        st = time.time()
        print("beginning checkpoint", st)
        last_save = cnt
        state = {
            'info': info,
            'optimizer': opt.state_dict(),
            'cnt': cnt,
            'policy_net_state_dict': policy_net.state_dict(),
            'target_net_state_dict': target_net.state_dict(),
            'perf': perf,
        }
        filename = os.path.abspath(model_base_filepath + "_%010dq.pkl" % cnt)
        save_checkpoint(state, filename)
        # npz will be added
        buff_filename = os.path.abspath(model_base_filepath + "_%010dq_train_buffer" % cnt)
        replay_memory.save_buffer(buff_filename)
        print("finished checkpoint", time.time() - st)
        return last_save
    else:
        return last_save

class ActionGetter:
    """Determines an action according to an epsilon greedy strategy with annealing epsilon"""
    """This class is from fg91's dqn. TODO put my function back in"""
    def __init__(self, n_actions, eps_initial=1, eps_final=0.1, eps_final_frame=0.01,
                 eps_evaluation=0.0, eps_annealing_frames=100000,
                 replay_memory_start_size=50000, max_steps=25000000, random_seed=122):
        """
        Args:
            n_actions: Integer, number of possible actions
            eps_initial: Float, Exploration probability for the first
                replay_memory_start_size frames
            eps_final: Float, Exploration probability after
                replay_memory_start_size + eps_annealing_frames frames
            eps_final_frame: Float, Exploration probability after max_frames frames
            eps_evaluation: Float, Exploration probability during evaluation
            eps_annealing_frames: Int, Number of frames over which the
                exploration probability is annealed from eps_initial to eps_final
            replay_memory_start_size: Integer, Number of frames during
                which the agent only explores
            max_frames: Integer, Total number of frames shown to the agent
        """
        self.n_actions = n_actions
        self.eps_initial = eps_initial
        self.eps_final = eps_final
        self.eps_final_frame = eps_final_frame
        self.eps_evaluation = eps_evaluation
        self.eps_annealing_frames = eps_annealing_frames
        self.replay_memory_start_size = replay_memory_start_size
        self.max_steps = max_steps
        self.random_state = np.random.RandomState(random_seed)

        # Slopes and intercepts for exploration decrease
        if self.eps_annealing_frames > 0:
            self.slope = -(self.eps_initial - self.eps_final) / self.eps_annealing_frames
            self.intercept = self.eps_initial - self.slope * self.replay_memory_start_size
            self.slope_2 = -(self.eps_final - self.eps_final_frame) / (self.max_steps - self.eps_annealing_frames - self.replay_memory_start_size)
            self.intercept_2 = self.eps_final_frame - self.slope_2 * self.max_steps

    def pt_get_action(self, step_number, state, active_heads, evaluation=False):
        """
        Args:
            step_number: int number of the current step
            state: A (4, 84, 84) sequence of frames of an atari game in grayscale
            active_heads: list of heads to use for voting
            evaluation: A boolean saying whether the agent is being evaluated
        Returns:
            An integer between 0 and n_actions
        """
        if evaluation:
            eps = self.eps_evaluation
        elif step_number < self.replay_memory_start_size:
            eps = self.eps_initial
        elif self.eps_annealing_frames > 0:
            # TODO check this
            if step_number >= self.replay_memory_start_size and step_number < self.replay_memory_start_size + self.eps_annealing_frames:
                eps = self.slope * step_number + self.intercept
            elif step_number >= self.replay_memory_start_size + self.eps_annealing_frames:
                eps = self.slope_2 * step_number + self.intercept_2
        else:
            eps = 0
        if self.random_state.rand() < eps:
            return eps, self.random_state.randint(0, self.n_actions)
        else:
            state = torch.Tensor(state.astype(float) / info['NORM_BY'])[None, :].to(info['DEVICE'])
            vals = policy_net(state, None)
            # vote on action
            if active_heads is not None:
                acts = [torch.argmax(vals[h], dim=1).item() for h in active_heads]
                data = Counter(acts)
                action = data.most_common(1)[0][0]
                return eps, action
            else:
                acts = [torch.argmax(vals[h], dim=1).item() for h in range(info['N_ENSEMBLE'])]
                data = Counter(acts)
                action = data.most_common(1)[0][0]
                return eps, action

def ptlearn(states, actions, rewards, next_states, terminal_flags, masks):
    states = torch.Tensor(states.astype(float) / info['NORM_BY']).to(info['DEVICE'])
    next_states = torch.Tensor(next_states.astype(float) / info['NORM_BY']).to(info['DEVICE'])
    rewards = torch.Tensor(rewards).to(info['DEVICE'])
    actions = torch.LongTensor(actions).to(info['DEVICE'])
    terminal_flags = torch.Tensor(terminal_flags.astype(int)).to(info['DEVICE'])
    masks = torch.FloatTensor(masks.astype(int)).to(info['DEVICE'])
    
    # Min history to learn is 200,000 frames in DQN - 50000 steps
    losses = [0.0 for _ in range(info['N_ENSEMBLE'])]
    opt.zero_grad()
    
    q_policy_vals = policy_net(states, None)
    next_q_target_vals = target_net(next_states, None)
    next_q_policy_vals = policy_net(next_states, None)
    
    cnt_losses = []
    for k in range(info['N_ENSEMBLE']):
        #TODO finish masking
        total_used = torch.sum(masks[:, k])
        if total_used > 0.0:
            next_q_vals = next_q_target_vals[k].data
            if info['DOUBLE_DQN']:
                next_actions = next_q_policy_vals[k].data.max(1, True)[1]
                next_qs = next_q_vals.gather(1, next_actions).squeeze(1)
            else:
                next_qs = next_q_vals.max(1)[0]  # max returns a pair

            preds = q_policy_vals[k].gather(1, actions[:, None]).squeeze(1)
            targets = rewards + info['GAMMA'] * next_qs * (1 - terminal_flags)
            l1loss = F.smooth_l1_loss(preds, targets, reduction='mean')
            full_loss = masks[:, k] * l1loss
            loss = torch.sum(full_loss / total_used)
            cnt_losses.append(loss)
            losses[k] = loss.cpu().detach().item()

    loss = sum(cnt_losses) / info['N_ENSEMBLE']
    loss.backward()
    for param in policy_net.core_net.parameters():
        if param.grad is not None:
            # Divide grads in core
            param.grad.data *= 1.0 / float(info['N_ENSEMBLE'])
    nn.utils.clip_grad_norm_(policy_net.parameters(), info['CLIP_GRAD'])
    opt.step()
    return np.mean(losses)

def train(step_number, last_save):
    """Contains the training and evaluation loops"""
    epoch_num = len(perf['steps'])
    writer = SummaryWriter(log_dir=model_base_filedir)

    while step_number < info['MAX_STEPS']:
        ########################
        ####### Training #######
        ########################
        epoch_frame = 0
        while epoch_frame < info['EVAL_FREQUENCY']:
            terminal = False
            life_lost = True
            state = env.reset()
            start_steps = step_number
            st = time.time()
            episode_reward_sum = 0
            random_state.shuffle(heads)
            active_heads = heads[:info['VOTING_HEADS']]  # Use a subset of heads for voting
            epoch_num += 1
            ep_eps_list = []
            ptloss_list = []
            while not terminal:
                if life_lost:
                    action = 1
                    eps = 0
                else:
                    eps, action = action_getter.pt_get_action(step_number, state=state, active_heads=active_heads)
                ep_eps_list.append(eps)
                next_state, reward, life_lost, terminal = env.step(action)
                # Store transition in the replay memory
                replay_memory.add_experience(
                    action=action,
                    frame=next_state[-1],
                    reward=np.sign(reward),  # TODO - maybe there should be +1 here
                    terminal=life_lost
                )

                step_number += 1
                epoch_frame += 1
                episode_reward_sum += reward
                state = next_state

                if step_number % info['LEARN_EVERY_STEPS'] == 0 and step_number > info['MIN_HISTORY_TO_LEARN']:
                    _states, _actions, _rewards, _next_states, _terminal_flags, _masks = replay_memory.get_minibatch(info['BATCH_SIZE'])
                    ptloss = ptlearn(_states, _actions, _rewards, _next_states, _terminal_flags, _masks)
                    ptloss_list.append(ptloss)
                if step_number % info['TARGET_UPDATE'] == 0 and step_number > info['MIN_HISTORY_TO_LEARN']:
                    print("++++++++++++++++++++++++++++++++++++++++++++++++")
                    print('updating target network at %s' % step_number)
                    target_net.load_state_dict(policy_net.state_dict())

            et = time.time()
            ep_time = et - st
            perf['steps'].append(step_number)
            perf['episode_step'].append(step_number - start_steps)
            perf['episode_head'].append(active_heads)
            perf['eps_list'].append(np.mean(ep_eps_list))
            perf['episode_loss'].append(np.mean(ptloss_list))
            perf['episode_reward'].append(episode_reward_sum)
            perf['episode_times'].append(ep_time)
            perf['episode_relative_times'].append(time.time() - info['START_TIME'])
            perf['avg_rewards'].append(np.mean(perf['episode_reward'][-100:]))
            last_save = handle_checkpoint(last_save, step_number)

            if not epoch_num % info['PLOT_EVERY_EPISODES'] and step_number > info['MIN_HISTORY_TO_LEARN']:
                # TODO plot title
                print('avg reward', perf['avg_rewards'][-1])
                print('last rewards', perf['episode_reward'][-info['PLOT_EVERY_EPISODES']:])

                mlflow_log_all(perf, step_number)
                # tensorboard_log_all(perf, writer, step_number)
                with open('rewards.txt', 'a') as reward_file:
                    print(len(perf['episode_reward']), step_number, perf['avg_rewards'][-1], file=reward_file)
        
        avg_eval_reward = evaluate(step_number)
        perf['eval_rewards'].append(avg_eval_reward)
        perf['eval_steps'].append(step_number)
        mlflow_log_all(perf, step_number)
        # tensorboard_log_all(perf, writer, step_number)

    writer.close()

def evaluate(step_number):
    print("""
         #########################
         ####### Evaluation ######
         #########################
         """)
    eval_rewards = []
    evaluate_step_number = 0
    frames_for_gif = []
    results_for_eval = []

    # Only run one
    for i in range(info['NUM_EVAL_EPISODES']):
        state = env.reset()
        episode_reward_sum = 0
        terminal = False
        life_lost = True
        episode_steps = 0
        while not terminal:
            if life_lost:
                action = 1
            else:
                eps, action = action_getter.pt_get_action(step_number, state, active_heads=None, evaluation=True)
            next_state, reward, life_lost, terminal = env.step(action)
            evaluate_step_number += 1
            episode_steps += 1
            episode_reward_sum += reward
            if not i:
                # Only save first episode
                frames_for_gif.append(env.ale.getScreenRGB())
                results_for_eval.append(f"{action}, {reward}, {life_lost}, {terminal}")
            if not episode_steps % 100:
                print('eval', episode_steps, episode_reward_sum)
            state = next_state
        eval_rewards.append(episode_reward_sum)

    print("Evaluation score:\n", np.mean(eval_rewards))
    generate_gif(model_base_filedir, step_number, frames_for_gif, eval_rewards[0], name='test', results=results_for_eval)

    # Show the evaluation score in MLflow
    efile = os.path.join(model_base_filedir, 'eval_rewards.txt')
    with open(efile, 'a') as eval_reward_file:
        print(step_number, np.mean(eval_rewards), file=eval_reward_file)
    return np.mean(eval_rewards)

if __name__ == '__main__':
    from argparse import ArgumentParser
    parser = ArgumentParser()
    # parser.add_argument('-c', '--cuda', action='store_true', default=False)
    parser.add_argument('-c', '--cuda', default=0, help='cuda device number')
    parser.add_argument('-v', '--voting_nr', default=1)
    parser.add_argument('-l', '--model_loadpath', default='', help='.pkl model file full path')
    parser.add_argument('-b', '--buffer_loadpath', default='', help='.npz replay buffer file full path')
    args = parser.parse_args()

    # device = 'cuda:1' if args.cuda else 'cpu'
    device = f'cuda:{args.cuda}'
    print(f"running on {device}")

    info = {
        #"GAME":'roms/breakout.bin', # gym prefix
        "GAME": 'roms/pong.bin',  # gym prefix
        "DEVICE": device,  # CPU vs GPU set by argument
        "VOTING_HEADS": args.voting_nr,  # how many heads to use for voting
        "NAME": 'FRANKbootstrap_fasteranneal_pong',  # start files with name
        "DUELING": True,  # use dueling DQN
        "DOUBLE_DQN": True,  # use double DQN
        "PRIOR": True,  # turn on to use randomized prior
        "PRIOR_SCALE": 10,  # what to scale prior by
        "N_ENSEMBLE": 2,  # number of bootstrap heads to use. when 1, this is a normal DQN
        "LEARN_EVERY_STEPS": 4,  # updates every 4 steps in Osband
        "BERNOULLI_PROBABILITY": 0.9,  # Probability of experience to go to each head - if 1, every experience goes to every head
        "TARGET_UPDATE": 10000,  # how often to update target network
        "MIN_HISTORY_TO_LEARN": 50000,  # in environment frames
        "NORM_BY": 255.,  # divide the float(of uint) by this number to normalize - max val of data is 255
        "EPS_INITIAL": 1.0,  # should be 1
        "EPS_FINAL": 0.01,  # 0.01 in Osband
        "EPS_EVAL": 0.0,  # 0 in Osband, 0.05 in others....
        "EPS_ANNEALING_FRAMES": int(1e6),  # this may have been 1e6 in Osband
        "EPS_FINAL_FRAME": 0.01,
        #"EPS_ANNEALING_FRAMES":0, # if it annealing is zero, then it will only use the bootstrap after the first MIN_EXAMPLES_TO_LEARN steps which are random
        "NUM_EVAL_EPISODES": 1,  # num examples to average in eval
        "BUFFER_SIZE": int(1e6),  # Buffer size for experience replay
        "CHECKPOINT_EVERY_STEPS": 500000,  # how often to write pkl of model and npz of data buffer
        "EVAL_FREQUENCY": 250000,  # how often to run evaluation episodes
        "ADAM_LEARNING_RATE": 6.25e-5,
        "RMS_LEARNING_RATE": 0.00025,  # according to paper = 0.00025
        "RMS_DECAY": 0.95,
        "RMS_MOMENTUM": 0.0,
        "RMS_EPSILON": 0.00001,
        "RMS_CENTERED": True,
        "HISTORY_SIZE": 4,  # how many past frames to use for state input
        "N_EPOCHS": 90000,  # Number of episodes to run
        "BATCH_SIZE": 32,  # Batch size to use for learning
        "GAMMA": .99,  # Gamma weight in Q update
        "PLOT_EVERY_EPISODES": 50,
        "CLIP_GRAD": 5,  # Gradient clipping setting
        "SEED": 101,
        "RANDOM_HEAD": -1,  # just used in plotting as demarcation
        "NETWORK_INPUT_SIZE": (84, 84),
        "START_TIME": time.time(),
        "MAX_STEPS": int(50e6),  # 50e6 steps is 200e6 frames
        "MAX_EPISODE_STEPS": 27000,  # Orig DQN give 18k steps, Rainbow seems to give 27k steps
        "FRAME_SKIP": 4,  # deterministic frame skips to match DeepMind
        "MAX_NO_OP_FRAMES": 30,  # random number of noops applied to beginning of each episode
        "DEAD_AS_END": True,  # do you send finished=true to agent while training when it loses a life
    }

    info['FAKE_ACTS'] = [info['RANDOM_HEAD'] for _ in range(info['N_ENSEMBLE'])]
    info['args'] = args
    info['load_time'] = datetime.date.today().ctime()
    info['NORM_BY'] = float(info['NORM_BY'])

    # Create environment
    env = Environment(rom_file=info['GAME'], frame_skip=info['FRAME_SKIP'],
                      num_frames=info['HISTORY_SIZE'], no_op_start=info['MAX_NO_OP_FRAMES'], rand_seed=info['SEED'],
                      dead_as_end=info['DEAD_AS_END'], max_episode_steps=info['MAX_EPISODE_STEPS'])

    # Create replay buffer
    replay_memory = ReplayMemory(size=info['BUFFER_SIZE'],
                                 frame_height=info['NETWORK_INPUT_SIZE'][0],
                                 frame_width=info['NETWORK_INPUT_SIZE'][1],
                                 agent_history_length=info['HISTORY_SIZE'],
                                 batch_size=info['BATCH_SIZE'],
                                 num_heads=info['N_ENSEMBLE'],
                                 bernoulli_probability=info['BERNOULLI_PROBABILITY'])

    random_state = np.random.RandomState(info["SEED"])
    action_getter = ActionGetter(n_actions=env.num_actions,
                                 eps_initial=info['EPS_INITIAL'],
                                 eps_final=info['EPS_FINAL'],
                                 eps_final_frame=info['EPS_FINAL_FRAME'],
                                 eps_annealing_frames=info['EPS_ANNEALING_FRAMES'],
                                 eps_evaluation=info['EPS_EVAL'],
                                 replay_memory_start_size=info['MIN_HISTORY_TO_LEARN'],
                                 max_steps=info['MAX_STEPS'])

    if args.model_loadpath:
        # Load data from loadpath - save model load for later. We need some of
        # these parameters to setup other things
        print(f'loading model from: {args.model_loadpath}')
        model_dict = torch.load(args.model_loadpath)
        info = model_dict['info']
        info['DEVICE'] = device
        # Set a new random seed
        info["SEED"] = model_dict['cnt']
        model_base_filedir = os.path.split(args.model_loadpath)[0]
        start_step_number = start_last_save = model_dict['cnt']
        info['loaded_from'] = args.model_loadpath
        perf = model_dict['perf']
        start_step_number = perf['steps'][-1]
    else:
        # Create new project
        perf = {'steps': [],
                'avg_rewards': [],
                'episode_step': [],
                'episode_head': [],
                'eps_list': [],
                'episode_loss': [],
                'episode_reward': [],
                'episode_times': [],
                'episode_relative_times': [],
                'eval_rewards': [],
                'eval_steps': []}

        start_step_number = 0
        start_last_save = 0
        # Make new directory for this run in the case that there is already a
        # project with this name
        run_num = 0
        model_base_filedir = os.path.join(config.model_savedir, info['NAME'] + '%02d' % run_num)
        while os.path.exists(model_base_filedir):
            run_num += 1
            model_base_filedir = os.path.join(config.model_savedir, info['NAME'] + '%02d' % run_num)
        os.makedirs(model_base_filedir)
        print("----------------------------------------------")
        print(f"starting NEW project: {model_base_filedir}")

    model_base_filepath = os.path.join(model_base_filedir, info['NAME'])
    write_info_file(info, model_base_filepath, start_step_number)
    heads = list(range(info['N_ENSEMBLE']))
    seed_everything(info["SEED"])

    policy_net = EnsembleNet(n_ensemble=info['N_ENSEMBLE'],
                             n_actions=env.num_actions,
                             network_output_size=info['NETWORK_INPUT_SIZE'][0],
                             num_channels=info['HISTORY_SIZE'], dueling=info['DUELING']).to(info['DEVICE'])
    target_net = EnsembleNet(n_ensemble=info['N_ENSEMBLE'],
                             n_actions=env.num_actions,
                             network_output_size=info['NETWORK_INPUT_SIZE'][0],
                             num_channels=info['HISTORY_SIZE'], dueling=info['DUELING']).to(info['DEVICE'])
    if info['PRIOR']:
        prior_net = EnsembleNet(n_ensemble=info['N_ENSEMBLE'],
                                n_actions=env.num_actions,
                                network_output_size=info['NETWORK_INPUT_SIZE'][0],
                                num_channels=info['HISTORY_SIZE'], dueling=info['DUELING']).to(info['DEVICE'])

        print("using randomized prior")
        policy_net = NetWithPrior(policy_net, prior_net, info['PRIOR_SCALE'])
        target_net = NetWithPrior(target_net, prior_net, info['PRIOR_SCALE'])

    target_net.load_state_dict(policy_net.state_dict())
    # Create optimizer
    #opt = optim.RMSprop(policy_net.parameters(),
    #                    lr=info["RMS_LEARNING_RATE"],
    #                    momentum=info["RMS_MOMENTUM"],
    #                    eps=info["RMS_EPSILON"],
    #                    centered=info["RMS_CENTERED"],
    #                    alpha=info["RMS_DECAY"])
    opt = optim.Adam(policy_net.parameters(), lr=info['ADAM_LEARNING_RATE'])

    if args.model_loadpath:
        # what about random states - they will be wrong now???
        # TODO - what about target net update cnt
        target_net.load_state_dict(model_dict['target_net_state_dict'])
        policy_net.load_state_dict(model_dict['policy_net_state_dict'])
        opt.load_state_dict(model_dict['optimizer'])
        print("loaded model state_dicts")
        if not args.buffer_loadpath:
            args.buffer_loadpath = args.model_loadpath.replace('.pkl', '_train_buffer.npz')
            print(f"auto loading buffer from: {args.buffer_loadpath}")
            try:
                replay_memory.load_buffer(args.buffer_loadpath)
            except Exception as e:
                print(e)
                print(f'not able to load from buffer: {args.buffer_loadpath}. exit() to continue with empty buffer')


    ml_config = {
        'ADAM_LEARNING_RATE': info['ADAM_LEARNING_RATE'],
        'EPS_INITIAL': info['EPS_INITIAL'],
        'EPS_FINAL': info['EPS_FINAL'],
        'EPS_EVAL': info['EPS_EVAL'],
        'EPS_ANNEALING_FRAMES': info['EPS_ANNEALING_FRAMES'],
        'EPS_FINAL_FRAME': info['EPS_FINAL_FRAME'],
        'BUFFER_SIZE': info['BUFFER_SIZE'],
        'CHECKPOINT_EVERY_STEPS': info['CHECKPOINT_EVERY_STEPS'],
        'EVAL_FREQUENCY': info['EVAL_FREQUENCY'],
        'NORM_BY': info['NORM_BY'],
        'N_ENSEMBLE': info['N_ENSEMBLE'],
        'LEARN_EVERY_STEPS': info['LEARN_EVERY_STEPS'],
        'TARGET_UPDATE': info['TARGET_UPDATE'],
        'MIN_HISTORY_TO_LEARN': info['MIN_HISTORY_TO_LEARN'],
        'NETWORK_INPUT_SIZE': info['NETWORK_INPUT_SIZE'],
        'SEED': info['SEED'],
        'MAX_STEPS': info['MAX_STEPS'],
        'MAX_EPISODE_STEPS': info['MAX_EPISODE_STEPS'],
        'FRAME_SKIP': info['FRAME_SKIP'],
        'MAX_NO_OP_FRAMES': info['MAX_NO_OP_FRAMES'],
        'DEAD_AS_END': info['DEAD_AS_END'],
        'NAME': info['NAME'],
        'DUELING': info['DUELING'],
        'DOUBLE_DQN': info['DOUBLE_DQN'],
        'PRIOR': info['PRIOR'],
        'PRIOR_SCALE': info['PRIOR_SCALE'],
        'GAME': info['GAME']
    }

    # Log hyperparameters with MLflow
    run_name = f"{info['VOTING_HEADS']}_{info['N_ENSEMBLE']}"
    mlflow.start_run(run_name=run_name)
    mlflow.log_params(ml_config)

    mlflow.pytorch.log_model(policy_net, "models")
    mlflow.pytorch.log_model(target_net, "models")

    train(start_step_number, start_last_save)

    
    mlflow.end_run()

