# Mimic 3 datasets. in hospital mortality
import os
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
# The GPU id to use, usually either &quot;0&quot; or &quot;1&quot;
gpu_index = 1
os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
import mimic3_utils.common_utils as common_utils
import numpy as np
import torch
import math
import get_model
import pandas as pd
import train_model
from sklearn import metrics
import sklearn.utils as sk_utils
import pathlib

class CustomDataset(torch.utils.data.BatchSampler):
    def __init__(self, data_dir, batch_size, input_size_list_raw, device, batch_first = False, shuffle = True):
        data = np.load(data_dir)
        X_array_raw = data['arr_0']
        Y_array = data['arr_1']
        X_array = np.zeros(X_array_raw.shape) # shape: (Sample size, T, n_features)
        # change the sequence of the features
        beg_index = 0
        for sub_list in input_size_list_raw:
            this_size = len(sub_list)
            X_array[:, :, beg_index:(beg_index + this_size)] = X_array_raw[:, :, sub_list]
            beg_index = beg_index + this_size
        self.n_examples = X_array.shape[0]
        self.steps = math.ceil(self.n_examples/batch_size)
        self.X_array = torch.tensor(X_array.copy()).to(device).float()
        self.Y_array = torch.tensor(Y_array.copy()).to(device).long()
        if not batch_first:
            self.X_array = self.X_array.permute(1, 0, 2)
        self.shuffle = shuffle
        self.on_epoch_end()
        self.batch_size = batch_size

    def __iter__(self):
        # return permuted tensors.
        for i in range(0, self.n_examples, self.batch_size):
            X = self.X_array[:, i:i + self.batch_size]
            Y = self.Y_array[i:i + self.batch_size]
            yield (X, Y)

    def __len__(self):
        return self.steps

    def on_epoch_end(self):
        if self.shuffle:
            shuffle_index = torch.randperm(self.X_array.size(1))
            self.X_array = self.X_array[:, shuffle_index, :]
            self.Y_array = self.Y_array[shuffle_index]
        # self.index = 0

class CheckAccuracy:
    def __init__(self, criterion, device, is_print = True):
        self.criterion = criterion
        self.is_print = is_print
        self.device = device

    def get_metrics(self, Y_array, prediction_probability):
        auc = metrics.roc_auc_score(Y_array, prediction_probability)
        (precisions, recalls, thresholds) = metrics.precision_recall_curve(Y_array, prediction_probability)
        auprc = metrics.auc(recalls, precisions)
        return auc, auprc

    def check_accuracy(self, model, test_data, n_resample = None):
        # test_data: data loadersa
        model.eval()
        Y_test_list = []
        output_list = []
        with torch.no_grad():
            for i, this_batch in enumerate(test_data):
                minibatch_X = this_batch[0]
                minibatch_Y = this_batch[1]
                minibatch_Y_cpu = minibatch_Y.cpu().numpy()
                outputs = model(minibatch_X)
                Y_test_list.append(minibatch_Y_cpu)
                output_list.append(outputs.cpu())
        test_data.on_epoch_end()
        validation_outputs = torch.cat(output_list, dim=0).to(self.device)
        Y_test = np.concatenate(Y_test_list, axis=0)
        Y_test = torch.tensor(Y_test).to(self.device).long()
        if validation_outputs.shape[1] == 1:
            validation_outputs = validation_outputs.view(validation_outputs.shape[0])
        validation_loss = self.criterion(validation_outputs, Y_test).item()
        Y_array_cpu = Y_test.cpu().numpy()
        predictions_probability = torch.nn.functional.softmax(validation_outputs, dim=1).cpu().numpy()
        predictions = predictions_probability.argmax(axis=1)
        overall_acc = ((predictions == Y_array_cpu).sum() / Y_array_cpu.shape[0]).item()

        auc, auprc = self.get_metrics(Y_array_cpu, predictions_probability[:, -1])

        result_dict = {'loss': validation_loss, 'accuracy': overall_acc, 'auc': auc, 'auprc': auprc}

        if n_resample is None:
            if self.is_print:
                print("validation loss: {:.4f}".format(validation_loss))
                print("validation acc: {:.4f}".format(overall_acc))
                print("validation auc roc: {:.4f}".format(auc))
                print("validation auc auprc: {:.4f}".format(auprc))
        else:
            # resample to calculate confidence intervals
            print("resampling results")
            resample_result_list = []
            data = np.zeros((Y_array_cpu.shape[0], 2))
            data[:, 0] = np.array(Y_array_cpu)
            data[:, 1] = np.array(predictions_probability[:, -1])
            for i in range(n_resample):
                resample_data = sk_utils.resample(data, n_samples=len(data))
                auc, auprc = self.get_metrics(resample_data[:, 0], resample_data[:, 1])
                resample_result_list.append({'auc': auc, 'auprc': auprc})
            resample_result = pd.DataFrame(resample_result_list)
            for metric in ['auc', 'auprc']:
                # update the point value by mean
                result_dict[metric] = resample_result[metric].mean()
                result_dict[metric + '_lower'] = resample_result[metric].quantile(0.025)
                result_dict[metric + '_upper'] = resample_result[metric].quantile(0.975)
            if self.is_print:
                print(result_dict)
        return result_dict

def single_model(result_dir_root, model_param_dict, train_data_dir, val_data_dir, training_param_dict,
                 input_size_list_raw):
    print(result_dir_root)
    ########################### model training
    print("training...")
    model_saving_dir = os.path.join(result_dir_root, 'model')
    if not os.path.exists(model_saving_dir):
        os.makedirs(model_saving_dir)
    # data preparation
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    batch_size = training_param_dict['batch_size']
    del training_param_dict['batch_size']
    train_data = CustomDataset(train_data_dir, batch_size, input_size_list_raw, device)
    val_data = CustomDataset(val_data_dir, 1024, input_size_list_raw, device, shuffle = False)
    model_param_dict['device'] = device
    print('print(model_param_dict)', model_param_dict)
    print('print(training_param_dict)', training_param_dict)
    np.savez(os.path.join(model_saving_dir, 'param_dict'), model_param_dict, training_param_dict)

    model = get_model.get_model(**model_param_dict)
    criterion = torch.nn.CrossEntropyLoss()
    check_accuracy_obj = CheckAccuracy(criterion, device)
    print('The number of trainable parameters is', model.param_num)
    val_result = train_model.train_mimic3(model, train_data, val_data, model_saving_dir, criterion,
                                          check_accuracy_obj,
                                          **training_param_dict)
    print('Validation result', val_result)
    accuracy_result = pd.DataFrame([val_result])
    accuracy_result.to_excel(os.path.join(result_dir_root, "accuracy_validation.xlsx"))


if __name__ == '__main__':
    #### parameters
    task = 'in_hospital_mortality'
    data_root_dir = os.path.join(pathlib.Path(__file__).parent.absolute(), 'mimic3_utils', task)  # data folder
    result_dir = os.path.join(pathlib.Path(__file__).parent.absolute(), 'mimic3', task)  # save results in this folder
    # The experiments in Harutyunyan et al. (2019) are coded with Keras.
    # We enable Karas initialization so that results are comparable.
    model_param_dict = {"model_name": 'mGRN', "n_feature": 76, "n_rnn_units": 8,
                        "num_classes": 2, "batch_first": False,
                        "size_of": 8, "dropouti": 0.1, "dropoutw": 0, "dropouto": 0.5,
                        "keras_initialization": True}
    training_param_dict = {'batch_size': 8, 'learning_rate': 5e-4, 'weight_decay': 0,
                           'num_epochs': 100, 'lr_decay_loss': 0.275, 'lr_decay_factor': 5,
                           'save_metric': 'loss', 'save_model_starting_epoch': 10, }
    ####
    train_data_dir = os.path.join(data_root_dir, 'train.npz')
    val_data_dir = os.path.join(data_root_dir, 'val.npz')
    header_dir = os.path.join(data_root_dir, 'header_list.npz')
    # get the names of the columns
    header_data = np.load(header_dir)
    header = header_data['arr_0']
    header_data.close()
    # grouping of features
    input_size_list_raw = common_utils.get_input_size_raw(header)
    input_size_list = [len(x) for x in input_size_list_raw]
    model_param_dict['input_size_list'] = input_size_list
    single_model(result_dir, model_param_dict, train_data_dir, val_data_dir, training_param_dict,
                 input_size_list_raw)



