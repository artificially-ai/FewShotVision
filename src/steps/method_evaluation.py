import os
import random
import time

import numpy as np
from pipeline.steps import AbstractStep
import torch
import torch.optim
import torch.utils.data.sampler

from src import backbone
import src.loaders.feature_loader as feat_loader  # TODO : ambiguous
from src.loaders.datamgr import SetDataManager
from src.methods import BaselineFinetune
from src.methods import ProtoNet
from src.methods import MatchingNet
from src.methods import RelationNet
from src.methods.maml import MAML
from src.utils import configs
from src.utils.io_utils import model_dict, parse_args, get_best_file, get_assigned_file


class MethodEvaluation(AbstractStep):
    def apply(self, args):
        params = parse_args('test', args)

        acc_all = []

        few_shot_params = dict(n_way=params.test_n_way, n_support=params.n_shot)

        if params.dataset in ['omniglot', 'cross_char']:
            assert params.model == 'Conv4' and not params.train_aug, 'omniglot only support Conv4 without augmentation'
            params.model = 'Conv4S'

        # Define model
        if params.method == 'baseline':
            model = BaselineFinetune(model_dict[params.model], **few_shot_params)
        elif params.method == 'baseline++':
            model = BaselineFinetune(model_dict[params.model], loss_type='dist', **few_shot_params)
        elif params.method == 'protonet':
            model = ProtoNet(model_dict[params.model], **few_shot_params)
        elif params.method == 'matchingnet':
            model = MatchingNet(model_dict[params.model], **few_shot_params)
        elif params.method in ['relationnet', 'relationnet_softmax']:
            if params.model == 'Conv4':
                feature_model = backbone.Conv4NP
            elif params.model == 'Conv6':
                feature_model = backbone.Conv6NP
            elif params.model == 'Conv4S':
                feature_model = backbone.Conv4SNP
            else:
                feature_model = lambda: model_dict[params.model](flatten=False)
            loss_type = 'mse' if params.method == 'relationnet' else 'softmax'
            model = RelationNet(feature_model, loss_type=loss_type, **few_shot_params)
        elif params.method in ['maml', 'maml_approx']:
            backbone.ConvBlock.maml = True
            backbone.SimpleBlock.maml = True
            backbone.BottleneckBlock.maml = True
            backbone.ResNet.maml = True
            model = MAML(model_dict[params.model], approx=(params.method == 'maml_approx'), **few_shot_params)
            if params.dataset in ['omniglot', 'cross_char']:  # maml use different parameter in omniglot
                model.n_task = 32
                model.task_update_num = 1
                model.train_lr = 0.1
        else:
            raise ValueError('Unknown method')

        model = model.cuda()

        # Define checkpoint directory
        checkpoint_dir = '%s/checkpoints/%s/%s_%s' % (configs.save_dir, params.dataset, params.model, params.method)
        if params.train_aug:
            checkpoint_dir += '_aug'
        if not params.method in ['baseline', 'baseline++']:
            checkpoint_dir += '_%dway_%dshot' % (params.train_n_way, params.n_shot)

        # modelfile   = get_resume_file(checkpoint_dir)

        # Fetch model parameters
        if not params.method in ['baseline', 'baseline++']:
            if params.save_iter != -1:
                modelfile = get_assigned_file(checkpoint_dir, params.save_iter)
            else:
                modelfile = get_best_file(checkpoint_dir)
            if modelfile is not None:
                tmp = torch.load(modelfile)
                model.load_state_dict(tmp['state'])

        split = params.split
        if params.save_iter != -1:
            split_str = split + "_" + str(params.save_iter)
        else:
            split_str = split
        if params.method in ['maml', 'maml_approx']:  # maml do not support testing with feature
            if 'Conv' in params.model:
                if params.dataset in ['omniglot', 'cross_char']:
                    image_size = 28
                else:
                    image_size = 84
            else:
                image_size = 224

            datamgr = SetDataManager(image_size, n_episode=params.n_iter, n_query=15, **few_shot_params)

            if params.dataset == 'cross':
                if split == 'base':
                    loadfile = configs.data_dir['miniImagenet'] + 'all.json'
                else:
                    loadfile = configs.data_dir['CUB'] + split + '.json'
            elif params.dataset == 'cross_char':
                if split == 'base':
                    loadfile = configs.data_dir['omniglot'] + 'noLatin.json'
                else:
                    loadfile = configs.data_dir['emnist'] + split + '.json'
            else:
                loadfile = configs.data_dir[params.dataset] + split + '.json'

            novel_loader = datamgr.get_data_loader(loadfile, aug=False)
            if params.adaptation:
                model.task_update_num = 100  # We perform adaptation on MAML simply by updating more times.
            model.eval()
            acc_mean, acc_std = model.test_loop(novel_loader, return_std=True)

        else:
            # Fetch feature vectors
            # cl_data_file is a dictionnary where each key is a label and each value is a list of feature vectors
            novel_file = os.path.join(checkpoint_dir.replace("checkpoints", "features"),
                                      split_str + ".hdf5")  # defaut split = novel, but you can also test base or val classes
            cl_data_file = feat_loader.init_loader(novel_file)

            for i in range(params.n_iter):
                acc = self._feature_evaluation(cl_data_file, model, n_query=15, adaptation=params.adaptation,
                                         **few_shot_params)
                acc_all.append(acc)
                if i % 10 == 0:
                    print('{}/{}'.format(i, params.n_iter))

            acc_all = np.asarray(acc_all)
            acc_mean = np.mean(acc_all)
            acc_std = np.std(acc_all)
            print('%d Test Acc = %4.2f%% +- %4.2f%%' % (params.n_iter, acc_mean, 1.96 * acc_std / np.sqrt(
                params.n_iter)))  # 1.96 is the approximation for 95% confidence interval
        with open('./record/results.txt', 'a') as f:
            timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            aug_str = '-aug' if params.train_aug else ''
            aug_str += '-adapted' if params.adaptation else ''
            if params.method in ['baseline', 'baseline++']:
                exp_setting = '%s-%s-%s-%s%s %sshot %sway_test' % (
                    params.dataset, split_str, params.model, params.method, aug_str, params.n_shot, params.test_n_way)
            else:
                exp_setting = '%s-%s-%s-%s%s %sshot %sway_train %sway_test' % (
                    params.dataset, split_str, params.model, params.method, aug_str, params.n_shot, params.train_n_way,
                    params.test_n_way)
            acc_str = '%d Test Acc = %4.2f%% +- %4.2f%%' % (
                params.n_iter, acc_mean, 1.96 * acc_std / np.sqrt(params.n_iter))  # TODO : redite
            f.write('Time: %s, Setting: %s, Acc: %s \n' % (timestamp, exp_setting, acc_str))

    def dump_output(self, _, output_folder, output_name, **__):
        pass

    def _feature_evaluation(self, cl_data_file, model, n_way=5, n_support=5, n_query=15, adaptation=False):
        class_list = cl_data_file.keys()

        select_class = random.sample(class_list, n_way)
        z_all = []
        for cl in select_class:
            img_feat = cl_data_file[cl]
            perm_ids = np.random.permutation(len(img_feat)).tolist()
            z_all.append([np.squeeze(img_feat[perm_ids[i]]) for i in range(n_support + n_query)])  # stack each batch

        z_all = torch.from_numpy(np.array(z_all))

        model.n_query = n_query
        if adaptation:
            scores = model.set_forward_adaptation(z_all, is_feature=True)
        else:
            scores = model.set_forward(z_all, is_feature=True)
        pred = scores.data.cpu().numpy().argmax(axis=1)
        y = np.repeat(range(n_way), n_query)
        acc = np.mean(pred == y) * 100
        return acc