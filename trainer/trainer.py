import copy
import inspect
import os
import shutil
from typing import Tuple, Dict, Callable, Union

import pyaml
import torch
import numpy as np
from torch.utils.tensorboard.summary import hparams
from models import *  # do not remove
from trainer.byol_wrapper import BYOLwrapper
from trainer.lr_schedulers import WarmUpWrapper  # do not remove

from torch.optim.lr_scheduler import *  # For loading optimizer specified in config

from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime

from commons.utils import flatten_dict, tensorboard_gradient_magnitude


class Trainer():
    def __init__(self, model, args, metrics: Dict[str, Callable], main_metric: str,
                 device: torch.device, tensorboard_functions: Dict[str, Callable],
                 optim=None, main_metric_goal: str = 'min', loss_func=torch.nn.MSELoss(),
                 scheduler_step_per_batch: bool = True):

        self.args = args
        self.device = device
        self.model = model.to(self.device)
        self.loss_func = loss_func
        self.tensorboard_functions = tensorboard_functions
        self.metrics = metrics
        self.main_metric = type(self.loss_func).__name__ if main_metric == 'loss' else main_metric
        self.main_metric_goal = main_metric_goal
        self.scheduler_step_per_batch = scheduler_step_per_batch
        self.initialize_optimizer(optim)
        self.initialize_scheduler()

        if args.checkpoint:
            checkpoint = torch.load(args.checkpoint, map_location=self.device)
            self.writer = SummaryWriter(os.path.dirname(args.checkpoint))
            self.model.load_state_dict(checkpoint['model_state_dict'])
            self.optim.load_state_dict(checkpoint['optimizer_state_dict'])
            if self.lr_scheduler != None and checkpoint['scheduler_state_dict'] != None:
                self.lr_scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
            self.start_epoch = checkpoint['epoch']
            self.best_val_score = checkpoint['best_val_score']
            self.optim_steps = checkpoint['optim_steps']
        else:
            self.start_epoch = 1
            self.optim_steps = 0
            self.best_val_score = -np.inf if self.main_metric_goal == 'max' else np.inf  # running score to decide whether or not a new model should be saved
            self.writer = SummaryWriter(
                '{}/{}_{}_{}'.format(args.logdir, args.model_type, args.experiment_name,
                                     datetime.now().strftime('%d-%m_%H-%M-%S')))
            shutil.copyfile(self.args.config.name,
                            os.path.join(self.writer.log_dir, os.path.basename(self.args.config.name)))
        print('Log directory: ', self.writer.log_dir)
        self.hparams = copy.copy(args).__dict__
        for key, value in flatten_dict(self.hparams).items():
            print(f'{key}: {value}')

    def run_per_epoch_evaluations(self, loader):
        pass

    def train(self, train_loader: DataLoader, val_loader: DataLoader):
        """
        Train and simultaneously evaluate on the val_loader and then estimate the stderr on eval_data if it is provided
        Args:
            train_loader: For training
            val_loader: For validation during training

        Returns:

        """
        args = self.args
        epochs_no_improve = 0  # counts every epoch that the validation accuracy did not improve for early stopping
        for epoch in range(self.start_epoch, args.num_epochs + 1):  # loop over the dataset multiple times
            self.model.train()
            self.predict(train_loader, epoch, optim=self.optim)

            self.model.eval()
            with torch.no_grad():
                val_loss, val_predictions, val_targets = self.predict(val_loader, epoch)
                metrics = self.evaluate_metrics(val_predictions, val_targets.float(), val=True)
                metrics[type(self.loss_func).__name__] = val_loss
                self.run_tensorboard_functions(val_predictions, val_targets, step=self.optim_steps, data_split='val')

                val_score = metrics[self.main_metric]
                if self.lr_scheduler != None and not self.scheduler_step_per_batch:
                    self.step_schedulers(metrics=val_score)

                if self.args.eval_per_epochs > 0 and epoch % self.args.eval_per_epochs == 0:
                    self.run_per_epoch_evaluations(val_loader)

                self.tensorboard_log(metrics, data_split='val', epoch=epoch, log_hparam=True, step=self.optim_steps)
                print('[Epoch %d] %s: %.6f val loss: %.6f' % (epoch, self.main_metric, val_score, val_loss))

                # save the model with the best main_metric depending on wether we want to maximize or minimize the main metric
                if val_score >= self.best_val_score and self.main_metric_goal == 'max' or val_score <= self.best_val_score and self.main_metric_goal == 'min':
                    epochs_no_improve = 0
                    self.best_val_score = val_score
                    self.save_checkpoint(epoch, checkpoint_name='best_checkpoint.pt')
                else:
                    epochs_no_improve += 1
                self.save_checkpoint(epoch, checkpoint_name='last_checkpoint.pt')

                if epochs_no_improve >= args.patience:  # stopping criterion
                    print(
                        f'Early stopping criterion based on -{self.main_metric}- that should be {self.main_metric_goal} reached after {epoch} epochs. Best model checkpoint was in epoch {epoch - epochs_no_improve}.')
                    break

        # evaluate on best checkpoint
        checkpoint = torch.load(os.path.join(self.writer.log_dir, 'best_checkpoint.pt'), map_location=self.device)
        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.evaluation(val_loader, data_split='val_best_checkpoint')

    def forward_pass(self, batch):
        targets = batch[-1]  # the last entry of the batch tuple is always the targets
        predictions = self.model(*tuple(batch[:-1]))  # foward the rest of the batch to the model
        return self.loss_func(predictions, targets.float()), predictions, targets

    def process_batch(self, batch, optim):
        loss, predictions, targets = self.forward_pass(batch)
        if optim != None:  # run backpropagation if an optimizer is provided
            loss.backward()
            self.optim.step()
            self.after_optim_step()  # overwrite to do stuff before zeroing out grads
            self.optim.zero_grad()
            self.optim_steps += 1
        return loss, predictions.detach(), targets.detach()

    def predict(self, data_loader: DataLoader, epoch: int = 0, optim: torch.optim.Optimizer = None,
                return_predictions: bool = False) -> Tuple[float, Union[torch.Tensor, None], Union[torch.Tensor, None]]:
        """
        get predictions for data in dataloader and do backpropagation if an optimizer is provided
        Args:
            data_loader: pytorch dataloader from which the batches will be taken
            epoch: optional parameter for logging
            optim: pytorch optimizer. If this is none, no backpropagation is done
            return_predictions: return the prdictions if true, else returns None

        Returns:
            metrics: a dictionary with all the metrics and the loss
            predictions: all predictions in the epoch
            targets: all targets of the epoch
        """
        args = self.args
        epoch_targets = torch.tensor([]).to(self.device)
        epoch_predictions = torch.tensor([]).to(self.device)
        epoch_loss = 0
        for i, batch in enumerate(data_loader):
            batch = [element.to(self.device) for element in batch]
            loss, predictions, targets = self.process_batch(batch, optim)
            with torch.no_grad():
                if self.optim_steps % args.log_iterations == 0 and optim != None:  # log every log_iterations during train
                    metrics = self.evaluate_metrics(predictions, targets.float())
                    metrics[type(self.loss_func).__name__] = loss.item()
                    self.run_tensorboard_functions(predictions, targets, step=self.optim_steps, data_split='train')
                    self.tensorboard_log(metrics, data_split='train', step=self.optim_steps, epoch=epoch)
                    print('[Epoch %d; Iter %5d/%5d] %s: loss: %.7f' % (epoch,
                                                                       i + 1, len(data_loader), 'train', loss.item()))
                if optim == None:  # during validation or testing when we want to average metrics over all the data in that dataloader
                    epoch_loss += loss.item()
                    epoch_targets = torch.cat((targets, epoch_targets), 0)
                    epoch_predictions = torch.cat((predictions, epoch_predictions), 0)

        if optim == None:
            return epoch_loss / len(data_loader), epoch_predictions, epoch_targets

    def after_optim_step(self):
        if self.optim_steps % self.args.log_iterations == 0:
            tensorboard_gradient_magnitude(self.optim, self.writer, self.optim_steps)
        if self.lr_scheduler != None and (self.scheduler_step_per_batch or (isinstance(self.lr_scheduler,
                                                                                       WarmUpWrapper) and self.lr_scheduler.total_warmup_steps > self.lr_scheduler._step)):  # step per batch if that is what we want to do or if we are using a warmup schedule and are still in the warmup period
            self.step_schedulers()

    def evaluate_metrics(self, predictions, targets, batch=None, val=False) -> Dict[str, float]:
        metric_results = {}
        metric_results[f'mean_pred'] = torch.mean(predictions).item()
        metric_results[f'std_pred'] = torch.std(predictions).item()
        metric_results[f'mean_targets'] = torch.mean(targets).item()
        metric_results[f'std_targets'] = torch.std(targets).item()
        for key, metric in self.metrics.items():
            if not hasattr(metric, 'val_only') or val:
                metric_results[key] = metric(predictions, targets).item()
        return metric_results

    def tensorboard_log(self, metrics, data_split: str, epoch: int, step: int, log_hparam: bool = False):
        metrics['epoch'] = epoch
        for i, param_group in enumerate(self.optim.param_groups):
            metrics[f'lr_param_group_{i}'] = param_group['lr']
        logs = {}
        for key, metric in metrics.items():
            metric_name = f'{key}/{data_split}'
            logs[metric_name] = metric
            self.writer.add_scalar(metric_name, metric, step)

        if log_hparam:  # write hyperparameters
            exp, ssi, sei = hparams(flatten_dict(self.hparams), flatten_dict(logs))
            self.writer.file_writer.add_summary(exp)
            self.writer.file_writer.add_summary(ssi)
            self.writer.file_writer.add_summary(sei)

    def run_tensorboard_functions(self, predictions, targets, step, data_split):
        for key, tensorboard_function in self.tensorboard_functions.items():
            tensorboard_function(predictions, targets, self.writer, step, data_split=data_split)

    def evaluation(self, data_loader: DataLoader, data_split: str = ''):
        self.model.eval()
        loss, predictions, targets = self.predict(data_loader)

        metrics = self.evaluate_metrics(predictions, targets.float(), val=True)
        metrics[type(self.loss_func).__name__] = loss
        with open(os.path.join(self.writer.log_dir, 'evaluation_' + data_split + '.txt'), 'w') as file:
            print('Statistics on ', data_split)
            for key, value in metrics.items():
                file.write(f'{key}: {value}\n')
                print(f'{key}: {value}')

    def initialize_optimizer(self, optim):
        transferred_keys = [k for k in self.model.state_dict().keys() if
                            any(transfer_layer in k for transfer_layer in self.args.transfer_layers) and not any(
                                to_exclude in k for to_exclude in self.args.exclude_from_transfer)]
        transferred_params = [v for k, v in self.model.named_parameters() if k in transferred_keys]
        new_params = [v for k, v in self.model.named_parameters() if
                      k not in transferred_keys and 'batch_norm' not in k]
        batch_norm_params = [v for k, v in self.model.named_parameters() if
                             'batch_norm' in k and k not in transferred_keys]

        transfer_lr = self.args.optimizer_params['lr'] if self.args.transferred_lr == None else self.args.transferred_lr
        # the order of the params here determines in which order they will start being updated during warmup when using ordered warmup in the warmupwrapper
        self.optim = optim([{'params': batch_norm_params, 'weight_decay': 0},
                            {'params': new_params},
                            {'params': transferred_params, 'lr': transfer_lr}], **self.args.optimizer_params)

    def step_schedulers(self, metrics=None):
        try:
            self.lr_scheduler.step(metrics=metrics)
        except:
            self.lr_scheduler.step()

    def initialize_scheduler(self):
        if self.args.lr_scheduler:  # Needs "from torch.optim.lr_scheduler import *" to work
            self.lr_scheduler = globals()[self.args.lr_scheduler](self.optim, **self.args.lr_scheduler_params)
        else:
            self.lr_scheduler = None

    def save_checkpoint(self, epoch: int, checkpoint_name: str):
        """
        Saves checkpoint of model in the logdir of the summarywriter/ in the used rundir
        Args:
            epoch: current epoch from which the run will be continued if it is loaded

        Returns:

        """
        run_dir = self.writer.log_dir
        self.save_model_state(epoch, checkpoint_name)
        train_args = copy.copy(self.args)
        # when loading from a checkpoint the config entry is a string. Otherwise it is a file object
        config_path = self.args.config if isinstance(self.args.config, str) else self.args.config.name
        train_args.config = os.path.join(run_dir, os.path.basename(config_path))
        with open(os.path.join(run_dir, 'train_arguments.yaml'), 'w') as yaml_path:
            pyaml.dump(train_args.__dict__, yaml_path)

        # Get the class of the used model (works because of the "from models import *" calling the init.py in the models dir)
        model_class = globals()[type(self.model).__name__]
        source_code = inspect.getsource(model_class)  # Get the sourcecode of the class of the model.
        file_name = os.path.basename(inspect.getfile(model_class))
        with open(os.path.join(run_dir, file_name), "w") as f:
            f.write(source_code)

    def save_model_state(self, epoch: int, checkpoint_name: str):
        torch.save({
            'epoch': epoch,
            'best_val_score': self.best_val_score,
            'optim_steps': self.optim_steps,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optim.state_dict(),
            'scheduler_state_dict': None if self.lr_scheduler == None else self.lr_scheduler.state_dict()
        }, os.path.join(self.writer.log_dir, checkpoint_name))
