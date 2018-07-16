#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import time
from collections import defaultdict
from six.moves import xrange
from abc import abstractmethod, ABCMeta

import numpy as np
import torch
from torch.optim.optimizer import Optimizer
from torch.autograd import Variable

from nmtlab.models import EncoderDecoderModel
from nmtlab.utils import MTDataset, smoothed_bleu
from nmtlab.schedulers import Scheduler

ROOT_RANK = 0


class TrainerKit(object):
    """Training NMT models.
    """
    
    __metaclass__ = ABCMeta
    
    def __init__(self, model, dataset, optimizer, scheduler=None, multigpu=False):
        """Create a trainer.
        Args:
            model (EncoderDecoderModel): The model to train.
            dataset (MTDataset): Bilingual dataset.
            optimizer (Optimizer): Torch optimizer.
            scheduler (Scheduler): Training scheduler.
        """
        self._model = model
        self._dataset = dataset
        self._optimizer = optimizer
        self._scheduler = scheduler if scheduler is not None else Scheduler()
        self._multigpu = multigpu
        self._n_devices = 1
        # Setup horovod1i
        if multigpu:
            try:
                import horovod.torch as hvd
            except ImportError:
                raise SystemError("nmtlab requires horovod to run multigpu training.")
            # Initialize Horovod
            hvd.init()
            # Pin GPU to be used to process local rank (one GPU per process)
            torch.cuda.set_device(hvd.local_rank())
            self._model.cuda()
            self._optimizer = hvd.DistributedOptimizer(optimizer, named_parameters=self._model.named_parameters())
            hvd.broadcast_parameters(self._model.state_dict(), root_rank=ROOT_RANK)
            # Set the scope of training data
            self._dataset.set_gpu_scope(hvd.rank(), hvd.size())
            self._n_devices = hvd.size()
        elif torch.cuda.is_available():
            self._model.cuda()
            # Initialize common variables
        self._log_lines = []
        self._scheduler.bind(self)
        self._best_criteria = 65535
        self._n_train_batch = self._dataset.n_train_batch()
        self._batch_size = self._dataset.batch_size()
        self.configure()
        self._begin_time = 0
        self._current_epoch = 0
        self._current_step = 0
        # Print information
        self.log("nmtlab", "Training {} with {} parameters".format(
            self._model.__class__.__name__, len(list(self._model.named_parameters()))
        ))
        self.log("nmtlab", "with {} and {}".format(
            self._optimizer.__class__.__name__, self._scheduler.__class__.__name__
        ))
        self.log("nmtlab", "Training data has {} batches".format(self._dataset.n_train_batch()))
        self._report_valid_data_hash()
        device_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
        self.log("nmtlab", "Running with {} GPUs ({})".format(
            hvd.size() if multigpu else 1, device_name
        ))
    
    def configure(self, save_path=None, clip_norm=5, n_valid_per_epoch=10, criteria="bleu"):
        """Configure the hyperparameters of the trainer.
        """
        self._save_path = save_path
        self._clip_norm = clip_norm
        self._n_valid_per_epoch = n_valid_per_epoch
        self._criteria = criteria
        self._valid_freq = int(self._n_train_batch / self._n_valid_per_epoch)
        assert self._criteria in ("bleu", "loss")
    
    @abstractmethod
    def run(self):
        """Run the training from begining to end.
        """
    
    def train(self, batch):
        """Run one forward and backward step with given batch.
        """
        src_seq = Variable(batch.src.transpose(0, 1))
        tgt_seq = Variable(batch.tgt.transpose(0, 1))
        val_map = self._model(src_seq, tgt_seq)
        self._optimizer.zero_grad()
        val_map["loss"].backward()
        # self._clip_grad_norm()
        if self._clip_norm > 0:
            # print([p.grad.data.norm() for p in self._model.parameters()])
            # torch.nn.utils.clip_grad_norm_(self._model.parameters(), self._clip_norm)
            self._clip_grad_norm()
            # print([p.grad.data.norm() for p in self._model.parameters()])
        self._optimizer.step()
        self.print_progress(val_map)
        return val_map
    
    def valid(self):
        """Validate the model every few steps.
        """
        if (self._current_step + 1) % self._valid_freq == 0 and self._is_root_node():
            self._model.train(False)
            score_map = self.run_valid()
            is_improved = self.check_improvement(score_map)
            self._scheduler.after_valid(is_improved, score_map)
            self._model.train(True)
            self.log("valid", "{}{} (epoch {}, step {})".format(
                self._dict_str(score_map), " *" if is_improved else "",
                self._current_epoch + 1, self._current_step + 1
            ))
        # Check new trainer settings
        if (self._current_step + 1) % self._valid_freq == 0 and self._multigpu:
            import horovod.torch as hvd
            lr = torch.tensor(self.get_learning_rate())
            lr = hvd.broadcast(lr, ROOT_RANK)
            new_lr = float(lr.numpy())
            if new_lr != self.get_learning_rate():
                self.set_learning_rate(new_lr)
            # print(hvd.local_rank(), self._model.attention_key_nn.weight[0, -10:])
        if (self._current_step + 1) % 30 == 0 and self._multigpu:
            import horovod.torch as hvd
            hvd.broadcast_parameters(self._model.state_dict(), root_rank=ROOT_RANK)
    
    def run_valid(self):
        """Run the model on the validation set and report loss.
        """
        score_map = defaultdict(list)
        for batch in self._dataset.valid_set():
            src_seq = batch.src.transpose(0, 1)
            tgt_seq = batch.tgt.transpose(0, 1)
            with torch.no_grad():
                val_map = self._model(src_seq, tgt_seq, sampling=True)
                # Estimate BLEU
                if "sampled_tokens" in val_map and val_map["sampled_tokens"] is not None:
                    bleu = self._compute_bleu(val_map["sampled_tokens"], tgt_seq)
                    score_map["bleu"].append(- bleu)
                    del val_map["sampled_tokens"]
                for k, v in val_map.items():
                    if v is not None:
                        score_map[k].append(v)
        for key, vals in score_map.items():
            score_map[key] = np.mean(vals)
        return score_map
    
    def check_improvement(self, score_map):
        cri = score_map[self._criteria]
        if cri < self._best_criteria - abs(self._best_criteria) * 0.001:
            self._best_criteria = cri
            self.save(0, 0)
            return True
        else:
            return False
    
    def print_progress(self, val_map):
        progress = int(float(self._current_step) / self._n_train_batch * 100)
        speed = float(self._current_step * self._batch_size) / (time.time() - self._begin_time) * self._n_devices
        sys.stdout.write("[epoch {}|{}%] loss={:.2f} | {:.1f} sample/s   \r".format(
            self._current_epoch + 1, progress, val_map["loss"], speed
        ))
        sys.stdout.flush()
    
    def log(self, who, msg):
        line = "[{}] {}".format(who, msg)
        self._log_lines.append(line)
        if self._is_root_node():
            print(line)
    
    def save(self, epoch, step):
        state_dict = {
            "epoch": epoch,
            "step": step,
            "model_state": self._model.state_dict(),
            "optimizer_state": self._optimizer.state_dict()
        }
        if self._save_path is not None:
            torch.save(state_dict, self._save_path)
            open(self._save_path + ".log", "w").writelines([l + "\n" for l in self._log_lines])
    
    def load(self, model_path=None):
        if model_path is None:
            model_path = self._save_path
        state_dict = torch.load(model_path)
        self._model.load_state_dict(state_dict["model_state"])
        self._optimizer.load_state_dict(state_dict["optimizer_state"])
        self._current_step = state_dict["step"]
        self._current_epoch = state_dict["epoch"]
    
    def is_finished(self):
        is_finished = self._scheduler.is_finished()
        if self._multigpu:
            import horovod.torch as hvd
            flag_tensor = torch.tensor(1 if is_finished else 0)
            flag_tensor = hvd.broadcast(flag_tensor, ROOT_RANK)
            return flag_tensor > 0
        else:
            return is_finished
    
    def get_learning_rate(self):
        return self._optimizer.param_groups[0]["lr"]
    
    def set_learning_rate(self, lr):
        for g in self._optimizer.param_groups:
            g["lr"] = lr
        if self._is_root_node():
            self.log("nmtlab", "change learning rate to {:.6f}".format(lr))
    
    def begin_epoch(self, epoch):
        """Set current epoch.
        """
        self._current_epoch = epoch
        self._scheduler.before_epoch()
        self._begin_time = time.time()
    
    def end_epoch(self):
        """End one epoch.
        """
        self._scheduler.after_epoch()
        self.log("nmtlab", "Ending epoch {}, spent {} minutes  ".format(
            self._current_epoch + 1, int(self.epoch_time() / 60.)
        ))
    
    def begin_step(self, step):
        """Set current step.
        """
        self._current_step = step
    
    def epoch(self):
        """Get current epoch.
        """
        return self._current_epoch
    
    def step(self):
        """Get current step
        """
        return self._current_step
    
    def epoch_time(self):
        """Get the seconds consumed in current epoch.
        """
        return time.time() - self._begin_time
    
    def _report_valid_data_hash(self):
        """Report the hash number of the valid data.

        This is to ensure the valid scores are consistent in every runs.
        """
        import hashlib
        valid_list = [
            " ".join(example.tgt)
            for example in self._dataset.raw_valid_data().examples
        ]
        valid_hash = hashlib.sha1("\n".join(valid_list).encode("utf-8", "ignore")).hexdigest()[-8:]
        self.log("nmtlab", "Hash of validation data is {}".format(valid_hash))

    def _clip_grad_norm(self):
        """Clips gradient norm of parameters.
        """
        if self._clip_norm <= 0:
            return
        parameters = filter(lambda p: p.grad is not None, self._model.parameters())
        max_norm = float(self._clip_norm)
        for param in parameters:
            grad_norm = param.grad.data.norm()
            if grad_norm > max_norm:
                param.grad.data.mul_(max_norm / (grad_norm + 1e-6))
            
    @staticmethod
    def _compute_bleu(sampled_tokens, tgt_seq):
        """Compute smoothed BLEU of sampled tokens
        """
        bleus = []
        tgt_seq = tgt_seq.cpu().numpy()
        sampled_tokens = sampled_tokens.cpu().numpy()
        tgt_mask = np.greater(tgt_seq, 0)
        for i in xrange(tgt_seq.shape[0]):
            target_len = int(tgt_mask[i].sum())
            ref_tokens = tgt_seq[i, 1:target_len - 1]
            out_tokens = list(sampled_tokens[i])
            if 2 in out_tokens:
                out_tokens = out_tokens[:out_tokens.index(2)]
            else:
                out_tokens = out_tokens[:target_len - 2]
            if not out_tokens:
                bleus.append(0.)
            else:
                bleus.append(smoothed_bleu(out_tokens, ref_tokens))
        return np.mean(bleus)
    
    @staticmethod
    def _dict_str(rmap):
        return " ".join(
            ["{}={:.2f}".format(n, v) for n, v in rmap.items()]
        )
    
    def _is_root_node(self):
        if self._multigpu:
            import horovod.torch as hvd
            return hvd.local_rank() == ROOT_RANK
        else:
            return True

