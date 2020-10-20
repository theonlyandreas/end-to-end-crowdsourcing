import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

from datasets.tripadvisor import TripAdvisorDataset
from datasets import collate_wrapper
from models.ipa2lt_head import Ipa2ltHead
from models.basic import BasicNetwork
from utils import get_model_path


class Solver(object):

    def __init__(self, dataset, learning_rate, batch_size, momentum=0.9, model_weights_path='',
                 writer=None, device=torch.device('cpu'), verbose=True,
                 embedding_dim=50, label_dim=2, annotator_dim=2, averaging_method='macro',
                 save_path_head=None, save_at=None, save_params=None, use_softmax=False,
                 pseudo_annotators=None, pseudo_model_path_func=None, pseudo_func_args={},
                 ):
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.dataset = dataset
        self.embedding_dim = embedding_dim
        self.label_dim = label_dim
        self.annotator_dim = annotator_dim
        self.momentum = momentum
        self.model_weights_path = model_weights_path
        self.save_path_head = save_path_head
        self.save_at = save_at
        self.save_params = save_params
        self.device = device
        self.writer = writer
        self.verbose = verbose
        self.averaging_method = averaging_method
        self.use_softmax = use_softmax

        # List with pseudo annotators and separate function for getting a model path
        self.pseudo_annotators = pseudo_annotators
        self.pseudo_model_path_func = pseudo_model_path_func
        self.pseudo_func_args = pseudo_func_args

        if pseudo_annotators is not None:
            self._create_pseudo_labels()

    def _get_model(self, basic_only=False, pretrained_basic=False):
        if not basic_only:
            model = Ipa2ltHead(self.embedding_dim, self.label_dim, self.annotator_dim, use_softmax=self.use_softmax)
        else:
            model = BasicNetwork(self.embedding_dim, self.label_dim, use_softmax=self.use_softmax)
        if self.model_weights_path is not '':
            print(
                f'Training model with weights of file {self.model_weights_path}')
            if pretrained_basic and not basic_only:
                model.basic_network.load_state_dict(torch.load(self.model_weights_path))
            else:
                model.load_state_dict(torch.load(self.model_weights_path))
        model.to(self.device)

        return model

    def _create_pseudo_labels(self):
        model = BasicNetwork(self.embedding_dim, self.label_dim, use_softmax=self.use_softmax)
        for pseudo_ann in self.pseudo_annotators:
            model.load_state_dict(torch.load(self.pseudo_model_path_func(**self.pseudo_func_args, annotator=pseudo_ann)))
            model.to(self.device)
            annotator_list = self.dataset.annotators.copy()
            annotator_list.remove(pseudo_ann)
            for annotator in annotator_list:
                self.dataset.create_pseudo_labels(annotator, pseudo_ann, model)

    def _print(self, *args, **kwargs):

        print(*args, **kwargs)

    def fit(self, epochs, return_f1=False, single_annotator=None, basic_only=False, fix_base=False, pretrained_basic=False):
        model = self._get_model(basic_only=basic_only, pretrained_basic=pretrained_basic)
        if single_annotator is not None or basic_only:
            self.annotator_dim = 1
            optimizer = optim.AdamW([
                {'params': model.parameters()},
            ], lr=self.learning_rate, betas=(0.9, 0.999), eps=1e-08, weight_decay=0.01, amsgrad=False)

        # if self.label_dim is 2:
        #    criterion = nn.BCELoss()
        # elif self.label_dim > 2:
        criterion = nn.CrossEntropyLoss()
        if single_annotator is None and not basic_only:
            if not fix_base:
                optimizers = [optim.AdamW([
                    {'params': model.basic_network.parameters()},
                    {'params': model.bias_matrices[i].parameters()},
                ],
                    lr=self.learning_rate, betas=(0.9, 0.999), eps=1e-08, weight_decay=0.01, amsgrad=False)
                    for i in range(self.annotator_dim)]
            else:
                optimizers = [optim.AdamW([
                    {'params': model.bias_matrices[i].parameters()},
                ],
                    lr=self.learning_rate, betas=(0.9, 0.999), eps=1e-08, weight_decay=0.01, amsgrad=False)
                    for i in range(self.annotator_dim)]

        loss_history = []
        inputs = 0
        labels = 0
        f1 = 0.0

        # self._print('START TRAINING')
        self._print(
            f'learning rate: {self.learning_rate} - batch size: {self.batch_size}')
        for epoch in range(epochs):
            # loop over all annotators
            for i in range(self.annotator_dim):
                # switch to current annotator
                if single_annotator is not None:
                    annotator = single_annotator
                    self.dataset.set_annotator_filter(annotator)
                    no_annotator_head = True
                elif single_annotator is None and basic_only:
                    annotator = 'all'
                    self.dataset.no_annotator_filter()
                    no_annotator_head = True
                else:
                    annotator = self.dataset.annotators[i]
                    optimizer = optimizers[i]
                    self.dataset.set_annotator_filter(annotator)
                    no_annotator_head = False

                # training
                self.dataset.set_mode('train')
                train_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=self.batch_size, collate_fn=collate_wrapper)
                self.fit_epoch(model, optimizer, criterion, train_loader, annotator, i,
                               epoch, loss_history, no_annotator_head=no_annotator_head)

                # validation
                self.dataset.set_mode('validation')
                val_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=self.batch_size, collate_fn=collate_wrapper)
                if return_f1:
                    if len(val_loader) is 0:
                        self.dataset.set_mode('train')
                        val_loader = torch.utils.data.DataLoader(
                            self.dataset, batch_size=self.batch_size, collate_fn=collate_wrapper)
                    _, _, f1 = self.fit_epoch(model, optimizer, criterion, val_loader, annotator, i,
                                              epoch, loss_history, mode='validation', return_metrics=True, no_annotator_head=no_annotator_head)
                else:
                    self.fit_epoch(model, optimizer, criterion, val_loader, annotator, i,
                                   epoch, loss_history, mode='validation', no_annotator_head=no_annotator_head)

            if self.save_at is not None and self.save_path_head is not None and self.save_params is not None:
                if epoch in self.save_at:
                    params = self.save_params
                    if return_f1:
                        path = get_model_path(self.save_path_head, params['stem'], params['current_time'], params['hyperparams'], f1)
                    else:
                        path = get_model_path(self.save_path_head, params['stem'], params['current_time'], params['hyperparams'])
                    path += f'_epoch{epoch}.pt'

                    print(f'Saving model at: {path}')
                    torch.save(model.state_dict(), path)

        self._print('Finished Training' + 20 * ' ')
        self._print('sum of first 10 losses: ', sum(loss_history[0:10]))
        self._print('sum of last  10 losses: ', sum(loss_history[-10:]))

        if return_f1:
            return model, f1

        return model

    def fit_epoch(self, model, opt, criterion, data_loader, annotator, annotator_idx, epoch, loss_history, mode='train',
                  return_metrics=False, no_annotator_head=False):
        if no_annotator_head:
            annotator_idx = None
        mean_loss = 0.0
        mean_accuracy = 0.0
        mean_precision = 0.0
        mean_recall = 0.0
        mean_f1 = 0.0
        len_data_loader = len(data_loader)
        for i, data in enumerate(data_loader, 1):
            # Prepare inputs to be passed to the model
            # Prepare labels for the Loss computation
            self._print(
                f'Annotator {annotator} - Epoch {epoch}: Step {i} / {len_data_loader}' + 10 * ' ', end='\r')
            inputs, labels, pseudo_labels = data.input, data.target, data.pseudo_targets
            opt.zero_grad()

            # Generate predictions
            if annotator_idx is not None:
                outputs = model(inputs)[annotator_idx]
                if len(pseudo_labels) is not 0:
                    outputs_pseudo_labels = model(inputs)
                    losses = [criterion(outputs_pseudo_labels[self.dataset.annotators.index(ann)].float(), pseudo_labels[ann])
                              for ann in pseudo_labels.keys()]
            else:
                outputs = model(inputs)

            # Compute Loss:
            loss = criterion(outputs.float(), labels)

            # performance measures of the batch
            predictions = outputs.argmax(dim=1)
            accuracy, precision, recall, f1 = self.performance_measures(predictions, labels, self.averaging_method)

            # statistics for logging
            current_batch_size = inputs.shape[0]
            divisor = (i - 1) * self.batch_size + current_batch_size
            mean_loss = ((i - 1) * self.batch_size * mean_loss +
                         loss.item() * current_batch_size) / divisor
            mean_accuracy = (mean_accuracy * self.batch_size * (i - 1) + accuracy.item() * current_batch_size) / divisor
            mean_precision = (mean_precision * self.batch_size * (i - 1) + precision.item() * current_batch_size) / divisor
            mean_recall = (mean_recall * self.batch_size * (i - 1) + recall.item() * current_batch_size) / divisor
            mean_f1 = (mean_f1 * self.batch_size * (i - 1) + f1.item() * current_batch_size) / divisor
            loss_history.append(loss.item())

            if mode is 'train':
                # Update gradients
                if annotator_idx is not None and len(pseudo_labels) is not 0:
                    final_loss = loss
                    for pseudo_loss in losses:
                        final_loss += pseudo_loss
                    final_loss.backward()
                else:
                    loss.backward()

                # Optimization step
                opt.step()

            if self.writer is not None:
                self.writer.add_scalar(f'Loss/Annotator {annotator}/{mode}', mean_loss, epoch)
                self.writer.add_scalar(
                    f'Accuracy/Annotator {annotator}/{mode}', mean_accuracy, epoch)
                self.writer.add_scalar(
                    f'Precision/Annotator {annotator}/{mode}', mean_precision, epoch)
                self.writer.add_scalar(f'Recall/Annotator {annotator}/{mode}', mean_recall, epoch)
                self.writer.add_scalar(f'F1 score/Annotator {annotator}/{mode}', mean_f1, epoch)

        if return_metrics:
            return mean_loss, mean_accuracy, mean_f1

    def evaluate_model(self, output_file_path, labels=None):
        model = self._get_model()

        # write annotation bias matrices into log file
        import sys
        original_stdout = sys.stdout
        with open(output_file_path, 'w') as f:
            sys.stdout = f
            self.dataset.set_mode('train')
            out_text = ''
            acc_out = ''
            bias_out = 'Annotation bias matrices\n\n'
            for i, annotator in enumerate(self.dataset.annotators):
                bias_out += f'Annotator {annotator}\n'
                bias_out += f'Output\\LatentTruth'
                if labels is not None:
                    for label in labels:
                        bias_out += '\t' * 3 + f'{label}'
                    bias_out += '\n'
                    for j, label in enumerate(labels):
                        bias_out += f'{label}' + ' ' * (15 - len(label))
                        for k, label_2 in enumerate(labels):
                            bias_out += '\t' * 3 + f'{model.bias_matrices[i].weight[j][k].cpu().detach().numpy(): .4f}'
                        bias_out += '\n'
                    bias_out += '\n'
                else:
                    bias_out += f'{model.bias_matrices[i].weight.cpu().detach().numpy()}\n\n'

                correct = {ann: 0 for ann in self.dataset.annotators}

                different_answers = 0
                different_answers_idx = []

                self.dataset.set_annotator_filter(annotator)
                # batch_size needs to be 1
                val_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=1, collate_fn=collate_wrapper)

                for i, data in enumerate(val_loader, 1):
                    # Prepare inputs to be passed to the model
                    inp, label = data.input, data.target

                    # Generate predictions
                    latent_truth = model.basic_network(inp)
                    output = model(inp)

                    # print input/output
                    out_text += f'Point {i} - Label by {annotator}: {label.cpu().numpy()} - Latent truth {latent_truth.cpu().detach().numpy()}'
                    predictions = {}
                    for idx, ann in enumerate(self.dataset.annotators):
                        out_text += f' - Annotator {ann} {output[idx].cpu().detach().numpy()}'
                        predictions[ann] = [output[idx].argmax(dim=1), output[idx].max(dim=1)]

                    # filtered_preds = {key: predictions[key][1] for key in predictions.keys() if predictions[key][0] == label}

                    for idx, ann in enumerate(self.dataset.annotators):
                        if predictions[ann][0] == label:
                            # annotator_highest_pred = max(filtered_preds, key=filtered_preds.get)
                            correct[ann] += 1
                    predictions_set = set([predictions[ann][0].cpu().detach().numpy().item(0) for ann in self.dataset.annotators])
                    if len(predictions_set) is not 1:
                        different_answers += 1
                        different_answers_idx.append(i)

                    out_text += '\n'

                # Document correct predictions
                all_same_accuracies = set([correct[ann] for ann in self.dataset.annotators])
                acc_out += '-' * 25 + f'   Annotator {annotator}   ' + '-' * 25 + '\n'
                acc_out += f'Different answers given by bias matrices {different_answers} / {len(self.dataset)} times\n'
                acc_out += f'Different answers at points: {different_answers_idx[:min(5, len(different_answers_idx))]}\n'
                acc_out += f'Accuracies of samples labeled by {annotator}:'
                # ' has {len(all_same_accuracies)} '
                # if len(all_same_accuracies) is 1:
                #     acc_out += 'answer\n'
                # else:
                #     acc_out += 'different answers\n'
                acc_out += '\n'
                for ann in self.dataset.annotators:
                    acc_out += f'Annotator {ann}: {correct[ann]} / {len(self.dataset)}     '
                acc_out += '\n\n'

            print(bias_out)
            print(acc_out)
            print('\n' * 30)
            print(out_text)
            sys.stdout = original_stdout

    @staticmethod
    def performance_measures(predictions, labels, averaging_method='macro'):
        if predictions.device.type == 'cuda' or labels.device.type == 'cuda':
            predictions, labels = predictions.cpu(), labels.cpu()

        # averaging for multiclass targets, can be one of [‘micro’, ‘macro’, ‘samples’, ‘weighted’]
        accuracy = accuracy_score(labels, predictions)
        zero_division = 0
        precision = precision_score(labels, predictions, average=averaging_method, zero_division=zero_division)
        recall = recall_score(labels, predictions, average=averaging_method, zero_division=zero_division)
        f1 = f1_score(labels, predictions, average=averaging_method, zero_division=zero_division)

        return accuracy, precision, recall, f1
