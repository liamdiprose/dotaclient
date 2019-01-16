from collections import Counter
from datetime import datetime
import argparse
import copy
import io
import logging
import os
import pickle
import re
import socket
import time

from google.cloud import storage
from tensorboardX import SummaryWriter
import numpy as np
import pika
import torch
import torch.distributed as dist

from distributed import DistributedDataParallelSparseParamCPU
from dotaservice.protos.DotaService_pb2 import TEAM_DIRE, TEAM_RADIANT
from policy import Policy


logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

torch.manual_seed(7)

eps = np.finfo(np.float32).eps.item()


def is_distributed():
    return torch.distributed.is_available() and torch.distributed.is_initialized()


def is_master():
    if is_distributed():
        return torch.distributed.get_rank() == 0
    else:
        return True


class MessageQueue:
    EXPERIENCE_QUEUE_NAME = 'experience'
    MODEL_EXCHANGE_NAME = 'model'
    MAX_RETRIES = 10

    def __init__(self, host, port, prefetch_count, use_model_exchange):
        """
        Args:
            prefetch_count (int): Amount of messages to prefetch. Settings this variable too
                high can result in blocked pipes that time out.
        """
        self._params = pika.ConnectionParameters(
            host=host,
            port=port,
            heartbeat=0,
        )
        self.prefetch_count = prefetch_count
        self.use_model_exchange = use_model_exchange

        self._conn = None
        self._xp_channel = None
        self._model_exchange = None

    def process_events(self):
        try:
            self._conn.process_data_events()
        except:
            pass

    def connect(self):
        if not self._conn or self._conn.is_closed:
            # RMQ.
            for i in range(10):
                try:
                    self._conn = pika.BlockingConnection(self._params)
                except pika.exceptions.ConnectionClosed:
                    logger.error('Connection to RMQ failed. retring. ({}/{})'.format(i, self.MAX_RETRIES))
                    time.sleep(5)
                    continue
                else:
                    logger.info('Connected to RMQ')
                    break

            # Experience channel.
            self._xp_channel = self._conn.channel()
            self._xp_channel.basic_qos(prefetch_count=self.prefetch_count)
            self._xp_channel.queue_declare(queue=self.EXPERIENCE_QUEUE_NAME)

            # Model Exchange.
            if self.use_model_exchange:
                self._model_exchange = self._conn.channel()
                self._model_exchange.exchange_declare(
                    exchange=self.MODEL_EXCHANGE_NAME,
                    exchange_type='x-recent-history',
                    arguments={'x-recent-history-length': 1},
                )

    @property
    def xp_queue_size(self):
        try:
            res = self._xp_channel.queue_declare(queue=self.EXPERIENCE_QUEUE_NAME, passive=True)
            return res.method.message_count
        except:
            return None

    def process_data_events(self):
        # Sends heartbeat, might keep conn healthier.
        try:
            self._conn.process_data_events()
        except:  # Gotta catch em' all!
            pass

    def _publish_model(self, msg, hdr):
        self._model_exchange.basic_publish(
            exchange=self.MODEL_EXCHANGE_NAME,
            routing_key='',
            body=msg,
            properties=pika.BasicProperties(headers=hdr),
        )

    def publish_model(self, *args, **kwargs):
        try:
            self._publish_model(*args, **kwargs)
        except (pika.exceptions.ConnectionClosed, pika.exceptions.ChannelClosed):
            logger.error('reconnecting to queue')
            self.connect()
            self._publish_model(*args, **kwargs)

    def _consume_xp(self):
        method, properties, body = next(self._xp_channel.consume(
            queue=self.EXPERIENCE_QUEUE_NAME,
            no_ack=False,
        ))
        self._xp_channel.basic_ack(delivery_tag=method.delivery_tag)
        return method, properties, body

    def consume_xp(self):
        try:
            return self._consume_xp()
        except (pika.exceptions.ConnectionClosed, pika.exceptions.ChannelClosed):
            logger.error('reconnecting to queue')
            self.connect()
            return self._consume_xp()

    def close(self):
        if self._conn and self._conn.is_open:
            logger.info('closing queue connection')
            self._conn.close()


class Experience:
    def __init__(self, game_id, states, actions, rewards, weight_version, team_id):
        self.game_id = game_id
        self.states = states
        self.actions = actions
        self.rewards = rewards
        self.weight_version = weight_version
        self.team_id = team_id


def all_gather(t):
    _t = [torch.empty_like(t) for _ in range(dist.get_world_size())]
    dist.all_gather(_t, t)
    return torch.cat(_t)


class DotaOptimizer:

    MODEL_FILENAME_FMT = "model_%09d.pt"
    BUCKET_NAME = 'dotaservice'
    RUNNING_NORM_FACTOR = 0.95
    MODEL_HISTOGRAM_FREQ = 128
    MAX_GRAD_NORM = 0.5

    def __init__(self, rmq_host, rmq_port, batch_size, learning_rate, checkpoint, pretrained_model,
                 mq_prefetch_count, exp_dir, job_dir):
        super().__init__()
        self.rmq_host = rmq_host
        self.rmq_port = rmq_port
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.checkpoint = checkpoint
        self.mq_prefetch_count = mq_prefetch_count
        self.episode = 0
        self.policy_base = Policy()
        self.exp_dir = exp_dir
        self.job_dir = job_dir
        self.log_dir = os.path.join(exp_dir, job_dir)

        if self.checkpoint:
            # TODO(tzaman): Set logdir ourselves?
            self.writer = SummaryWriter(log_dir=self.log_dir)
            logger.info('Checkpointing to: {}'.format(self.log_dir))
            client = storage.Client()
            self.bucket = client.get_bucket(self.BUCKET_NAME)

            # First, check if logdir exists.
            latest_model = self.get_latest_model(prefix=self.log_dir)
            # If there's a model in here, we resume from there
            if latest_model is not None:
                logger.info('Found a latest model in pretrained dir: {}'.format(latest_model))
                self.episode = self.episode_from_model_filename(filename=latest_model)
                if pretrained_model is not None:
                    logger.warning('Overriding pretrained model by latest model.')
                pretrained_model = latest_model

            if pretrained_model is not None:
                logger.info('Downloading: {}'.format(pretrained_model))
                model_blob = self.bucket.get_blob(pretrained_model)
                # TODO(tzaman): Download to BytesIO and supply to torch in that way.
                pretrained_model = '/tmp/model.pt'
                model_blob.download_to_filename(pretrained_model)

        if pretrained_model is not None:
            self.policy_base.load_state_dict(torch.load(pretrained_model), strict=False)

        if torch.distributed.is_available() and torch.distributed.is_initialized():
            self.policy = DistributedDataParallelSparseParamCPU(self.policy_base)
        else:
            self.policy = self.policy_base

        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=self.learning_rate)
        self.time_last_step = time.time()

        self.mq = MessageQueue(host=self.rmq_host, port=self.rmq_port,
                               prefetch_count=mq_prefetch_count,
                               use_model_exchange=self.checkpoint)
        self.mq.connect()

    @staticmethod
    def episode_from_model_filename(filename):
        x = re.search('(\d+)(?=.pt)', filename)
        return int(x.group(0))

    def get_latest_model(self, prefix):
        blobs = list(self.bucket.list_blobs(prefix=prefix))
        if not blobs:
            # Directory does not exist, or no files in directory.
            return None
        else:
            fns = [x.name for x in blobs if x.name[-3:] == '.pt']
            if not fns:
                # No relevant files in directory.
                return None
            fns.sort()
            latest_model = fns[-1]
            return latest_model

    @property
    def events_filename(self):
        return self.writer.file_writer.event_writer._ev_writer._file_name

    @staticmethod
    def discount_rewards(rewards, gamma=0.98):
        """
        0.99^70 = 0.5
        0.98^35 = 0.5
        """
        R = 0
        discounted_rewards = []
        for r in rewards[::-1]:
            R = r + gamma * R
            discounted_rewards.insert(0, R)
        return torch.tensor(discounted_rewards)

    def normalize(self, t):
        t -= t.mean()
        t /= (t.std() + eps)
        return t

    def finish_episode(self, probs, rewards):
        log_probs = torch.log(probs)
        loss = torch.mul(-log_probs, rewards)
        self.optimizer.zero_grad()
        loss = loss.mean()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.policy.parameters(), self.MAX_GRAD_NORM)
        self.optimizer.step()

        return loss

    def get_multihead_rewards(self, actions, rewards):
        mh_rewards = []
        for action, reward in zip(actions, rewards):
            mh_rewards.extend([reward] * len(action))
        return torch.stack(mh_rewards)

    def process_rollout(self, states, actions):
        """Processes a single experience consisting out of multiple steps.
        
        returns a flat list of propbalities, multi-heads are also flattened in.
        """
        # Insert the full sequence in one step.
        head_prob_dict, _ = self.policy(**states, hidden=None)  # -> {heads: tensors}

        probs = []
        seq_len = len(actions)
        for i in range(seq_len):
            action = actions[i]
            for k, v in action.items():
                head_probs = head_prob_dict[k][i]
                a_prob = head_probs[0, v[0][0]]
                probs.append(a_prob)

        # Probs is a flat list of probabilities, even for multi-head.
        return torch.stack(probs)

    def run(self):
        while True:
            experiences = []
            for _ in range(self.batch_size):
                method, properties, body = self.mq.consume_xp()
                data = pickle.loads(body)
                experience = Experience(
                    game_id=data['game_id'],
                    states=data['states'],
                    actions=data['actions'],
                    rewards=data['rewards'],
                    weight_version=data['weight_version'],
                    team_id=data['team_id'],
                    )
                experiences.append(experience)
            self.step(experiences=experiences)

    def step(self, experiences):
        logger.info('::step episode={}'.format(self.episode))
        
        # Get item form queue
        all_reward_sums = []
        # all_discounted_rewards = {TEAM_RADIANT: [], TEAM_DIRE: []}
        # all_logprobs = {TEAM_RADIANT: [], TEAM_DIRE: []}
        # all_rewards = []
        reward_counter = Counter()
        weight_ages = []
        teams = []
        n_steps = 0

        all_states = []
        all_actions = []

        all_mh_rewards = []
        all_probs = []

        # Loop over each experience
        for experience in experiences:
            self.mq.process_data_events()

            for reward in experience.rewards:  # Steps in a batch.
                reward_counter.update(reward)

            reward_sums = [sum(r.values()) for r in experience.rewards]
            discounted_rewards = self.discount_rewards(reward_sums)
            mh_rewards = self.get_multihead_rewards(experience.actions, discounted_rewards)
            all_mh_rewards.append(mh_rewards)

            probs = self.process_rollout(
                states=experience.states,
                actions=experience.actions,
            )

            all_probs.append(probs)
 
            # Non functional (metrics):
            all_reward_sums.append(sum(reward_sums))
            teams.append(experience.team_id)
            weight_ages.append(self.episode - experience.weight_version)
            n_steps += len(reward_sums)


        # policy_old = copy.deepcopy(self.policy)
        # policy_old.load_state_dict(self.policy.state_dict())

        # n_epochs = 4
        # for epoch in range(n_epochs):
        #     mb_pis, mb_vs = self.policy(mb_obs)
        #     mb_pi_olds, mb_v_olds = self.policy_old(mb_obs)

        all_probs = torch.cat(all_probs)
        all_mh_rewards = torch.cat(all_mh_rewards)

        all_norm_mh_rewards = self.normalize(all_mh_rewards)

        loss = self.finish_episode(probs=all_probs, rewards=all_norm_mh_rewards)

        self.episode += 1

        steps_per_s = n_steps / (time.time() - self.time_last_step)
        self.time_last_step = time.time()

        avg_weight_age = sum(weight_ages) / self.batch_size

        reward_counter = dict(reward_counter)

        reward_sum = sum(reward_counter.values())
        mean_reward = reward_sum / self.batch_size

        logger.info('steps_per_s={:.2f}, avg_weight_age={:.2f}, mean_reward={:.2f}, loss={:.4f}'.format(
            steps_per_s, avg_weight_age, mean_reward, loss))

        speed_key = 'steps per s'
        metrics = {
            speed_key: steps_per_s,
            'mean_reward': mean_reward,
            'loss': loss,
        }
        for k, v in reward_counter.items():
            metrics['reward_{}'.format(k)] = v / self.batch_size

        # Reduce all the metrics
        metrics_t = torch.tensor(list(metrics.values()), dtype=torch.float32)

        weight_ages = torch.tensor(weight_ages)
        teams = torch.tensor(teams)
        all_reward_sums = torch.tensor(all_reward_sums)
        if is_distributed():
            dist.all_reduce(metrics_t, op=dist.ReduceOp.SUM)
            metrics_t /= dist.get_world_size()

            weight_ages = all_gather(weight_ages)
            teams = all_gather(teams)
            all_reward_sums = all_gather(all_reward_sums)

        metrics_d = dict(zip(metrics.keys(), metrics_t))

        if is_distributed():
            # Speed is always the sum.
            metrics_d[speed_key] *= dist.get_world_size()

        if self.checkpoint:
            # Write metrics to events file.
            for name, metric in metrics_d.items():
                self.writer.add_scalar(name, metric, self.episode)
            
            # Age histogram
            self.writer.add_histogram('weight_age', weight_ages, self.episode)

            # Rewards histogram
            self.writer.add_histogram('rewards_radiant', all_reward_sums[teams==TEAM_RADIANT], self.episode)
            self.writer.add_histogram('rewards_dire', all_reward_sums[teams==TEAM_DIRE], self.episode)

            # Model
            if self.episode % self.MODEL_HISTOGRAM_FREQ == 1:
                for name, param in self.policy_base.named_parameters():
                    self.writer.add_histogram(name, param.clone().cpu().data.numpy(), self.episode)

            # RMQ Queue size.
            queue_size = self.mq.xp_queue_size
            if queue_size is not None:
                self.writer.add_scalar('mq_size', queue_size, self.episode)

            # Upload events to GCS
            self.writer.file_writer.flush()  # Flush before uploading
            blob = self.bucket.blob(self.events_filename)
            blob.upload_from_filename(filename=self.events_filename)

            self.upload_model()

    def upload_model(self):
        if not is_master():
            # Only rank 0 uploads the model.
            return

        filename = self.MODEL_FILENAME_FMT % self.episode
        rel_path = os.path.join(self.log_dir, filename)

        # Serialize the model.
        buffer = io.BytesIO()
        state_dict = self.policy_base.state_dict()
        torch.save(obj=state_dict, f=buffer)
        state_dict_b = buffer.getvalue()

        # Write model to file.
        with open(rel_path, 'wb') as f:
            f.write(state_dict_b)

        # Send to exchange.
        self.mq.publish_model(msg=state_dict_b, hdr={'version': self.episode})

        # Upload to GCP.
        blob = self.bucket.blob(rel_path)
        blob.upload_from_string(data=state_dict_b)  # Model


def init_distribution(backend='gloo'):
    logger.info('init_distribution')
    assert 'WORLD_SIZE' in os.environ
    if int(os.environ['WORLD_SIZE']) < 2:
        return
    torch.distributed.init_process_group(backend=backend)
    logger.info("Distribution initialized.")


def main(rmq_host, rmq_port, batch_size, learning_rate, pretrained_model, mq_prefetch_count,
         exp_dir, job_dir):
    logger.info('main(rmq_host={}, rmq_port={}, batch_size={} exp_dir={}, job_dir={})'.format(
        rmq_host, rmq_port, batch_size, exp_dir, job_dir))
 
    # If applicable, initialize distributed training.
    if torch.distributed.is_available():
        init_distribution()
    else:
        logger.info('distribution unavailable')

    # Only the master should checkpoint.
    checkpoint = is_master()

    dota_optimizer = DotaOptimizer(
        rmq_host=rmq_host,
        rmq_port=rmq_port,
        batch_size=batch_size,
        learning_rate=learning_rate,
        checkpoint=checkpoint,
        pretrained_model=pretrained_model,
        mq_prefetch_count=mq_prefetch_count,
        exp_dir=exp_dir,
        job_dir=job_dir,
    )

    # Upload initial model.
    dota_optimizer.upload_model()

    dota_optimizer.run()


def default_job_dir():
    return '{}_{}'.format(datetime.now().strftime('%b%d_%H-%M-%S'), socket.gethostname())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--exp-dir", type=str, help="experiment dir name", default='runs')
    parser.add_argument("--job-dir", type=str, help="job dir name", default=default_job_dir())
    parser.add_argument("--ip", type=str, help="mq ip", default='127.0.0.1')
    parser.add_argument("--port", type=int, help="mq port", default=5672)
    parser.add_argument("--batch-size", type=int, help="batch size", default=8)
    parser.add_argument("--learning-rate", type=float, help="learning rate", default=1e-4)
    parser.add_argument("--pretrained-model", type=str, help="pretrained model file within gcs bucket", default=None)
    parser.add_argument("--mq-prefetch-count", type=int,
                        help="amount of experience messages to prefetch from mq", default=2)
    args = parser.parse_args()

    try:
        main(
            rmq_host=args.ip,
            rmq_port=args.port,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            pretrained_model=args.pretrained_model,
            mq_prefetch_count=args.mq_prefetch_count,
            exp_dir=args.exp_dir,
            job_dir=args.job_dir,
        )
    except KeyboardInterrupt:
        pass
