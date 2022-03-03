import copy
import os
from itertools import chain
from typing import Union, Tuple, Dict, Callable

import numpy as np
import torch
from sklearn.decomposition import PCA
from torch.utils.data import DataLoader

from commons.utils import move_to_device
from trainer.self_supervised_trainer import SelfSupervisedTrainer
from trainer.trainer import Trainer


class CLASSTrainer(Trainer):
    def __init__(self, model, model2, critic, critic2, args, metrics: Dict[str, Callable], main_metric: str,
                 device: torch.device, tensorboard_functions: Dict[str, Callable], decoder=None, decoder2=None,
                 optim=None, main_metric_goal: str = 'min', loss_func=torch.nn.MSELoss,
                 scheduler_step_per_batch: bool = True, **kwargs):
        # move to device before loading optim params in super class
        # no need to move model because it will be moved in super class call
        self.model2 = model2.to(device)
        self.critic = critic.to(device)
        self.critic2 = critic2.to(device)
        self.decoder = decoder.to(device) if decoder else None
        self.decoder2 = decoder2.to(device) if decoder2 else None
        super(CLASSTrainer, self).__init__(model, args, metrics, main_metric, device, tensorboard_functions,
                                           optim, main_metric_goal, loss_func, scheduler_step_per_batch)

        if args.checkpoint:
            checkpoint = torch.load(args.checkpoint, map_location=self.device)
            self.model2.load_state_dict(checkpoint['model2_state_dict'])

    def forward_pass(self, batch):
        graph = tuple(batch)[0]
        graph_copy = copy.deepcopy(graph)
        modelA_out = self.model(graph)  # foward the rest of the batch to the model
        modelB_out = self.model2(graph_copy)  # foward the rest of the batch to the model
        criticA_out = self.critic(modelA_out)
        criticB_out = self.critic2(modelB_out)
        decoderA_out = self.decoder(graph) if self.decoder else None
        decoderB_out = self.decoder2(graph) if self.decoder2 else None
        modelA_loss, modelB_loss, criticA_loss, criticB_loss, decoderA_loss, decoderB_loss, loss_components = self.loss_func(
            modelA_out, modelB_out, criticA_out, criticB_out, decoderA_out, decoderB_out, graph, graph_copy,
            self.args.output_regularisation, self.args.loss_coeff1, self.args.loss_coeff2)

        return modelA_loss, modelB_loss, criticA_loss, criticB_loss, decoderA_loss, decoderB_loss, \
               (loss_components if loss_components != [] else None), modelA_out, modelB_out

    def process_batch(self, batch, optim):
        modelA_loss, modelB_loss, criticA_loss, criticB_loss, decoderA_loss, decoderB_loss, loss_components, predictions, targets = self.forward_pass(batch)

        if optim != None:  # run backpropagation if an optimizer is provided
            if self.args.iterations_per_model == 0:
                modelA_loss.backward(inputs=list(self.model.parameters()), retain_graph=True)
                self.optim.step()
                modelB_loss.backward(inputs=list(self.model2.parameters()), retain_graph=True)
                self.optim2.step()
                criticA_loss.backward(inputs=list(self.critic.parameters()))
                self.optim_critic.step()
                criticB_loss.backward(inputs=list(self.critic2.parameters()))
                self.optim_critic2.step()
                if decoderA_loss:
                    decoderA_loss.backward(inputs=list(self.decoder.parameters()))
                    self.optim_decoder.step()
                    self.optim_decoder.zero_grad()
                if decoderB_loss:
                    decoderB_loss.backward(inputs=list(self.decoder2.parameters()))
                    self.optim_decoder2.step()
                    self.optim_decoder2.zero_grad()

                self.optim.zero_grad()
                self.optim2.zero_grad()
                self.optim_critic.zero_grad()
                self.optim_critic2.zero_grad()

                self.optim_steps += 1

            else:
                if (self.optim_steps // self.args.iterations_per_model) % 2 == 0:
                    modelA_loss.backward(inputs=list(self.model.parameters()), retain_graph=True)
                    self.optim.step()
                    criticA_loss.backward(inputs=list(self.critic.parameters()))
                    self.optim_critic.step()
                    if decoderA_loss:
                        decoderA_loss.backward(inputs=list(self.decoder.parameters()))
                        self.optim_decoder.step()
                        self.optim_decoder.zero_grad()

                    self.optim.zero_grad()
                    self.optim_critic.zero_grad()

                    self.optim_steps += 1

                else:
                    modelB_loss.backward(inputs=list(self.model2.parameters()), retain_graph=True)
                    self.optim2.step()
                    criticB_loss.backward(inputs=list(self.critic2.parameters()))
                    self.optim_critic2.step()
                    if decoderB_loss:
                        decoderB_loss.backward(inputs=list(self.decoder2.parameters()))
                        self.optim_decoder2.step()
                        self.optim_decoder2.zero_grad()

                    self.optim2.zero_grad()
                    self.optim_critic2.zero_grad()

                    self.optim_steps += 1

        return modelA_loss, loss_components, (predictions.detach()), (targets.detach())

    def run_per_epoch_evaluations(self, data_loader):
        print('computing PCA explained variance')
        representations = []
        targets = []
        for batch in data_loader:
            batch = [element.to(self.device) for element in batch]
            _, _, modelA_out, modelB_out = self.process_batch(batch, optim=None)
            representations.append(modelA_out)
            targets.append(modelB_out)
        representations = torch.cat(representations, dim=0)
        targets = torch.cat(targets, dim=0)
        for n_components in [8, 16]:
            for name, X in [('pred', representations), ('targets', targets)]:
                pca = PCA(n_components=n_components)
                pca.fit_transform(X.cpu())
                total_explained_var_ratio = np.sum(pca.explained_variance_ratio_)
                self.writer.add_scalar(f'PCA{n_components}_explained_variance_{name}', total_explained_var_ratio, self.optim_steps)
        print('finish computing PCA explained variance')


    def initialize_optimizer(self, optim):
        self.optim = optim(self.model.parameters(), **self.args.optimizer_params)
        self.optim2 = optim(self.model2.parameters(), **self.args.optimizer2_params)
        self.optim_critic = optim(self.critic.parameters(), **self.args.optimizer_critic_params)
        self.optim_critic2 = optim(self.critic2.parameters(), **self.args.optimizer_critic2_params)
        self.optim_decoder = optim(self.decoder.parameters(), **self.args.optimizer_decoder_params) if self.decoder else None
        self.optim_decoder2 = optim(self.decoder2.parameters(), **self.args.optimizer_decoder2_params) if self.decoder2 else None

    def save_model_state(self, epoch: int, checkpoint_name: str):
        torch.save({
            'epoch': epoch,
            'best_val_score': self.best_val_score,
            'optim_steps': self.optim_steps,
            'model_state_dict': self.model.state_dict(),
            'model2_state_dict': self.model2.state_dict(),
            'optimizer_state_dict': self.optim.state_dict(),
            'scheduler_state_dict': None if self.lr_scheduler == None else self.lr_scheduler.state_dict()
        }, os.path.join(self.writer.log_dir, checkpoint_name))
