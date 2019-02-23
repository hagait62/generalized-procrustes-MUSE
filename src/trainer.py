# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import os
from logging import getLogger
import scipy
import scipy.linalg
import torch
from torch.autograd import Variable
from torch.nn import functional as F

from .utils import get_optimizer, load_embeddings, normalize_embeddings, export_embeddings
from .utils import clip_parameters
from .dico_builder import build_dictionary, cross_match_dictionary
from .evaluation.word_translation import DIC_EVAL_PATH, load_identical_char_dico, load_identical_num_dico, load_dictionary


logger = getLogger()


class Trainer(object):

    def __init__(self, src_emb, tgt_emb, mapping, discriminator, params):
        """
        Initialize trainer script.
        """
        self.src_emb = src_emb
        self.tgt_emb = tgt_emb
        self.src_dico = params.src_dico
        self.tgt_dico = getattr(params, 'tgt_dico', None)
        self.mapping = mapping
        self.discriminator = discriminator
        self.params = params

        # optimizers
        if hasattr(params, 'map_optimizer'):
            optim_fn, optim_params = get_optimizer(params.map_optimizer)
            self.map_optimizer = optim_fn(mapping[self.params.src_lang].parameters(), **optim_params)
        if hasattr(params, 'dis_optimizer'):
            optim_fn, optim_params = get_optimizer(params.dis_optimizer)
            self.dis_optimizer = optim_fn(discriminator.parameters(), **optim_params)
        else:
            assert discriminator is None

        # best validation score
        self.best_valid_metric = -1e12

        self.decrease_lr = False

    def get_dis_xy(self, volatile):
        """
        Get discriminator input batch / output target.
        """
        # select random word IDs
        bs = self.params.batch_size
        if not self.params.dis_most_frequent <= min(len(self.src_dico), len(self.tgt_dico[self.params.tgt_lang[-1]])):
            self.params.dis_most_frequent = min(len(self.src_dico), len(self.tgt_dico[self.params.tgt_lang[-1]]))
        mf = self.params.dis_most_frequent
        src_ids = torch.LongTensor(bs).random_(len(self.src_dico) if mf == 0 else mf)
        tgt_ids = torch.LongTensor(bs).random_(len(self.tgt_dico[self.params.tgt_lang[-1]]) if mf == 0 else mf)
        if self.params.cuda:
            src_ids = src_ids.cuda()
            tgt_ids = tgt_ids.cuda()

        # get word embeddings
        src_emb = self.src_emb(Variable(src_ids, volatile=True))
        tgt_emb = self.tgt_emb[self.params.tgt_lang[-1]](Variable(tgt_ids, volatile=True))
        src_emb = self.mapping[self.params.src_lang](Variable(src_emb.data, volatile=volatile))
        tgt_emb = Variable(tgt_emb.data, volatile=volatile)

        # input / target
        x = torch.cat([src_emb, tgt_emb], 0)
        y = torch.FloatTensor(2 * bs).zero_()
        y[:bs] = 1 - self.params.dis_smooth
        y[bs:] = self.params.dis_smooth
        y = Variable(y.cuda() if self.params.cuda else y)

        return x, y

    def dis_step(self, stats):
        """
        Train the discriminator.
        """
        self.discriminator.train()

        # loss
        x, y = self.get_dis_xy(volatile=True)
        preds = self.discriminator(Variable(x.data))
        loss = F.binary_cross_entropy(preds, y)
        stats['DIS_COSTS'].append(loss.data[0])

        # check NaN
        if (loss != loss).data.any():
            logger.error("NaN detected (discriminator)")
            exit()

        # optim
        self.dis_optimizer.zero_grad()
        loss.backward()
        self.dis_optimizer.step()
        clip_parameters(self.discriminator, self.params.dis_clip_weights)

    def mapping_step(self, stats):
        """
        Fooling discriminator training step.
        """
        if self.params.dis_lambda == 0:
            return 0

        self.discriminator.eval()

        # loss
        x, y = self.get_dis_xy(volatile=False)
        preds = self.discriminator(x)
        loss = F.binary_cross_entropy(preds, 1 - y)
        loss = self.params.dis_lambda * loss

        # check NaN
        if (loss != loss).data.any():
            logger.error("NaN detected (fool discriminator)")
            exit()

        # optim
        self.map_optimizer.zero_grad()
        loss.backward()
        self.map_optimizer.step()
        self.orthogonalize()

        return 2 * self.params.batch_size

    def load_training_dico(self, dico_train, support):
        """
        Load training dictionary.
        """
        dico = {}
        dico_inbn = {}

        word2id1 = self.src_dico.word2id

        for lang in self.params.tgt_lang:
            word2id2 = self.tgt_dico[lang].word2id

            # identical character strings
            if dico_train == "identical_char":
                dico[lang] = load_identical_char_dico(word2id1, word2id2, True)
            #identical numbers
            elif dico_train == 'identical_num':
                dico[lang] = load_identical_num_dico(word2id1, word2id2, True)
            # use one of the provided dictionary
            elif dico_train == "default":
                filename = '%s-%s.0-5000.txt' % (self.params.src_lang, lang)
                dico[lang] = load_dictionary(
                    os.path.join(DIC_EVAL_PATH, filename),
                    word2id1, word2id2, True
                )
            # dictionary provided by the user
            else:
                dico[lang] = load_dictionary(dico_train, word2id1, word2id2, True)
        if support and len(self.params.tgt_lang)>1:
            dico_inbn[self.params.tgt_lang[1]] = load_identical_char_dico(self.tgt_dico[self.params.tgt_lang[0]].word2id,self.tgt_dico[self.params.tgt_lang[1]].word2id, True)

        self.dico = cross_match_dictionary(self.params.tgt_lang, dico, dico_inbn, self.params)

    def build_dictionary(self,support):
        """
        Build a dictionary from aligned embeddings.
        """
        src_emb = self.mapping[self.params.src_lang](self.src_emb.weight).data
        tgt_emb = {lang: self.mapping[lang](self.tgt_emb[lang].weight).data for lang in self.params.tgt_lang}
        src_emb = src_emb / src_emb.norm(2, 1, keepdim=True).expand_as(src_emb)
        tgt_emb = {lang: tgt_emb[lang] / tgt_emb[lang].norm(2, 1, keepdim=True).expand_as(tgt_emb[lang]) for lang in self.params.tgt_lang}
        self.dico = build_dictionary(src_emb, tgt_emb, self.params, support)

    def simple_procrustes(self):
        """
        Find the best orthogonal matrix mapping using the Orthogonal Procrustes problem
        https://en.wikipedia.org/wiki/Orthogonal_Procrustes_problem
        """
        A = self.src_emb.weight.data[self.dico[:, 0]]###TODO: if same row repeats in dico, will have same rows in matrices
        B = self.tgt_emb[self.params.tgt_lang[-1]].weight.data[self.dico[:, 1]]
        W = self.mapping[self.params.src_lang].weight.data
        M = B.transpose(0, 1).mm(A).cpu().numpy()
        U, S, V_t = scipy.linalg.svd(M, full_matrices=True)
        W.copy_(torch.from_numpy(U.dot(V_t)).type_as(W))

    def get_group_average(self,X,T):
        if self.params.cuda:
            group_average=torch.mean(torch.stack([X[lang].cuda().mm(T[lang].cuda()) for lang in X.keys()]),0)
        else:
            group_average=torch.mean(torch.stack([X[lang].mm(T[lang]) for lang in X.keys()]),0)
        return group_average

    def generalized_procrustes(self, support, initial_run):
        """
        Find the best orthogonal matrix mapping using the Orthogonal Procrustes problem
        https://en.wikipedia.org/wiki/Orthogonal_Procrustes_problem
        """
        lang_list=[self.params.tgt_lang[-1]] if not support else self.params.tgt_lang
        X = {lang: self.tgt_emb[lang].weight.data[self.dico[:, i]] for i,lang in enumerate(lang_list,1)}
        X[self.params.src_lang] = self.src_emb.weight.data[self.dico[:,0]]
        T = {lang: self.mapping[lang].weight.data for lang in [self.params.src_lang]+lang_list}
        for _ in range(100):
            if initial_run:
                #initialize group average with a random Language
                G = X[self.params.tgt_lang[0]]
            else: G = self.get_group_average(X,T)
            #superimpose all instances to current reference shape
            for lang in X.keys():
                M = G.transpose(0, 1).mm(X[lang]).cpu().numpy()
                U, S, V_t = scipy.linalg.svd(M, full_matrices=True)
                T[lang].copy_(torch.from_numpy(U.dot(V_t)).type_as(T[lang]))
            initial_run=False


    def orthogonalize(self):
        """
        Orthogonalize the mapping.
        """
        if self.params.map_beta > 0:
            W = self.mapping[self.params.src_lang].weight.data
            beta = self.params.map_beta
            W.copy_((1 + beta) * W - beta * W.mm(W.transpose(0, 1).mm(W)))

    def update_lr(self, to_log, metric):
        """
        Update learning rate when using SGD.
        """
        if 'sgd' not in self.params.map_optimizer:
            return
        old_lr = self.map_optimizer.param_groups[0]['lr']
        new_lr = max(self.params.min_lr, old_lr * self.params.lr_decay)
        if new_lr < old_lr:
            logger.info("Decreasing learning rate: %.8f -> %.8f" % (old_lr, new_lr))
            self.map_optimizer.param_groups[0]['lr'] = new_lr

        if self.params.lr_shrink < 1 and to_log[metric] >= -1e7:
            if to_log[metric] < self.best_valid_metric:
                logger.info("Validation metric is smaller than the best: %.5f vs %.5f"
                            % (to_log[metric], self.best_valid_metric))
                # decrease the learning rate, only if this is the
                # second time the validation metric decreases
                if self.decrease_lr:
                    old_lr = self.map_optimizer.param_groups[0]['lr']
                    self.map_optimizer.param_groups[0]['lr'] *= self.params.lr_shrink
                    logger.info("Shrinking the learning rate: %.5f -> %.5f"
                                % (old_lr, self.map_optimizer.param_groups[0]['lr']))
                self.decrease_lr = True

    def save_best(self, to_log, metric):
        """
        Save the best model for the given validation metric.
        """
        # best mapping for the given validation criterion
        if to_log[metric] > self.best_valid_metric:
            # new best mapping
            self.best_valid_metric = to_log[metric]
            logger.info('* Best value for "%s": %.5f' % (metric, to_log[metric]))
            # save the mapping
            W = {lang: self.mapping[lang].weight.data.cpu().numpy() for lang in [self.params.src_lang] + self.params.tgt_lang}
            path = {lang: os.path.join(self.params.exp_path, 'best_mapping.{}.pth'.format(lang)) for lang in [self.params.src_lang] + self.params.tgt_lang}
            for lang in [self.params.src_lang]+ self.params.tgt_lang:
                logger.info('* Saving the mapping to %s ...' % path[lang])
                torch.save(W[lang], path[lang])


    def reload_best(self):
        """
        Reload the best mapping.
        """
        path = {lang: os.path.join(self.params.exp_path, 'best_mapping.{}.pth'.format(lang)) for lang in self.params.tgt_lang+[self.params.src_lang]}
        # reload the model
        for lang in self.params.tgt_lang+[self.params.src_lang]:
            to_reload = torch.from_numpy(torch.load(path[lang]))
            W = self.mapping[lang].weight.data
            logger.info('* Reloading the best model from %s ...' % path[lang])
            assert to_reload.size() == W.size()
            W.copy_(to_reload.type_as(W))

    def export(self):
        """
        Export embeddings.
        """
        #params = self.params
        # load all embeddings
        logger.info("Reloading all embeddings for mapping ...")

        src_emb = self.mapping[self.params.src_lang](self.src_emb.weight).data
        tgt_emb = {lang: self.mapping[lang](self.tgt_emb[lang].weight).data for lang in self.params.tgt_lang}
        src_emb = src_emb / src_emb.norm(2, 1, keepdim=True).expand_as(src_emb)
        tgt_emb = {lang: tgt_emb[lang] / tgt_emb[lang].norm(2, 1, keepdim=True).expand_as(tgt_emb[lang]) for lang in self.params.tgt_lang}
        export_embeddings(src_emb.cpu().numpy(), {lang: tgt_emb[lang].cpu().numpy() for lang in self.params.tgt_lang}, self.params)
