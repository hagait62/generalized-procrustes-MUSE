# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import os
from logging import getLogger
from copy import deepcopy
import numpy as np
from torch.autograd import Variable

from . import get_wordsim_scores, get_crosslingual_wordsim_scores
from .word_translation import get_word_translation_accuracy
from . import load_europarl_data, get_sent_translation_accuracy
from ..dico_builder import get_candidates, build_dictionary, build_pairwise_dictionary
from src.utils import get_idf
import pdb
logger = getLogger()
import torch


class Evaluator(object):

    def __init__(self, trainer):
        """
        Initialize evaluator.
        """
        self.src_emb = trainer.src_emb
        self.tgt_emb = trainer.tgt_emb
        self.src_dico = trainer.src_dico
        self.tgt_dico = trainer.tgt_dico
        self.mapping = trainer.mapping
        self.discriminator = trainer.discriminator
        self.params = trainer.params

    def monolingual_wordsim(self, to_log):
        """
        Evaluation on monolingual word similarity.
        """
        src_ws_scores = get_wordsim_scores(
            self.src_dico.lang, self.src_dico.word2id,
            self.mapping[self.params.src_lang](self.src_emb.weight).data.cpu().numpy()
        )
        if self.params.tgt_lang:
            tgt_ws_scores = {}
            for lang in self.params.tgt_lang:
                tgt_ws_scores[lang] = get_wordsim_scores(
                    self.tgt_dico[lang].lang, self.tgt_dico[lang].word2id,
                    self.mapping[lang](self.tgt_emb[lang].weight).data.cpu().numpy()
                )
        else: tgt_ws_scores = None
        if src_ws_scores is not None:
            src_ws_monolingual_scores = np.mean(list(src_ws_scores.values()))
            logger.info("Monolingual source word similarity score average: %.5f" % src_ws_monolingual_scores)
            to_log['src_ws_monolingual_scores'] = src_ws_monolingual_scores
            to_log.update({'src_' + k: v for k, v in src_ws_scores.items()})
        if tgt_ws_scores is not None:
            tgt_ws_monolingual_scores = {}
            for lang in self.params.tgt_lang:
                if tgt_ws_scores[lang] is not None:
                    tgt_ws_monolingual_scores[lang] = np.mean(list(tgt_ws_scores[lang].values()))
                    logger.info("Monolingual target word similarity score average %s : %.5f" % (lang,tgt_ws_monolingual_scores[lang]))
                    to_log['tgt_ws_monolingual_scores_{}'.format(lang)] = tgt_ws_monolingual_scores[lang]
                    to_log.update({'tgt_' + k: v for k, v in tgt_ws_scores[lang].items()})
        if src_ws_scores is not None and tgt_ws_scores is not None:
            for lang in self.params.tgt_lang:
                if tgt_ws_scores[lang] is not None:
                    ws_monolingual_scores = (src_ws_monolingual_scores + tgt_ws_monolingual_scores[lang]) / 2
                    logger.info("Monolingual word similarity score average %s : %.5f" % (lang, ws_monolingual_scores))
                    to_log['ws_monolingual_scores_{}'.format(lang)] = ws_monolingual_scores

    def crosslingual_wordsim(self, to_log):
        """
        Evaluation on cross-lingual word similarity.
        """
        src_emb = self.mapping[self.params.src_lang](self.src_emb.weight).data.cpu().numpy()

        for lang in self.params.tgt_lang:
            tgt_emb = self.mapping[lang](self.tgt_emb[lang].weight).data.cpu().numpy()
            # cross-lingual wordsim evaluation
            src_tgt_ws_scores = get_crosslingual_wordsim_scores(
                self.src_dico.lang, self.src_dico.word2id, src_emb,
                self.tgt_dico[lang].lang, self.tgt_dico[lang].word2id, tgt_emb,
            )
            if src_tgt_ws_scores is None:
                return
            ws_crosslingual_scores = np.mean(list(src_tgt_ws_scores.values()))
            logger.info("Cross-lingual word similarity score average %s: %.5f" % (lang, ws_crosslingual_scores))
            to_log['ws_crosslingual_scores_{}'.format(lang)] = ws_crosslingual_scores
            to_log.update({'src_tgt_{}'.format(lang) + k: v for k, v in src_tgt_ws_scores.items()})

    def word_translation(self, to_log):
        """
        Evaluation on word translation.
        """
        # mapped word embeddings
        src_emb = self.mapping[self.params.src_lang](self.src_emb.weight).data
        for i,lang in enumerate(self.params.tgt_lang):
            torch.cuda.empty_cache()
            #if not os.path.isfile('data/crosslingual/dictionaries/%s-%s.5000-6500.txt' % (self.params.src_lang,lang)): continue
            tgt_emb = self.mapping[lang](self.tgt_emb[lang].weight).data

            for method in ['nn', 'csls_knn_10']:
                results = get_word_translation_accuracy(
                    self.src_dico.lang, self.src_dico.word2id, src_emb,
                    self.tgt_dico[lang].lang, self.tgt_dico[lang].word2id, tgt_emb,
                    method=method,
                    id2word_src=self.src_dico.id2word,
                    id2word_tgt=self.tgt_dico[lang].id2word,
                    dico_eval=self.params.dico_eval
                )
                to_log.update([('%s-%s_%s' % (k, method,lang), v) for k, v in results])
                #results = get_word_translation_accuracy(
                #    self.tgt_dico[lang].lang, self.tgt_dico[lang].word2id, tgt_emb,
                #    self.src_dico.lang, self.src_dico.word2id, src_emb,
                #    method=method,
                #    id2word_src=self.tgt_dico[lang].id2word,
                #    id2word_tgt=self.src_dico.id2word,
                #    dico_eval=self.params.dico_eval_bwd
                #)
                #to_log.update([('%s-%s_%s' % (k, method,lang), v) for k, v in results])
#

    def sent_translation(self, to_log):
        """
        Evaluation on sentence translation.
        Only available on Europarl, for en - {de, es, fr, it} language pairs.
        """
        lg1 = self.src_dico.lang
        lg2 = self.tgt_dico.lang

        # parameters
        n_keys = 200000
        n_queries = 2000
        n_idf = 300000

        # load europarl data
        if not hasattr(self, 'europarl_data'):
            self.europarl_data = load_europarl_data(
                lg1, lg2, n_max=(n_keys + 2 * n_idf)
            )

        # if no Europarl data for this language pair
        if not self.europarl_data:
            return

        # mapped word embeddings
        src_emb = self.mapping[self.params.src_lang](self.src_emb.weight).data
        tgt_emb = self.mapping['tgt'](self.tgt_emb['tgt'].weight).data

        # get idf weights
        idf = get_idf(self.europarl_data, lg1, lg2, n_idf=n_idf)

        for method in ['nn', 'csls_knn_10']:

            # source <- target sentence translation
            results = get_sent_translation_accuracy(
                self.europarl_data,
                self.src_dico.lang, self.src_dico.word2id, src_emb,
                self.tgt_dico.lang, self.tgt_dico.word2id, tgt_emb,
                n_keys=n_keys, n_queries=n_queries,
                method=method, idf=idf
            )
            to_log.update([('tgt_to_src_%s-%s' % (k, method), v) for k, v in results])

            # target <- source sentence translation
            results = get_sent_translation_accuracy(
                self.europarl_data,
                self.tgt_dico.lang, self.tgt_dico.word2id, tgt_emb,
                self.src_dico.lang, self.src_dico.word2id, src_emb,
                n_keys=n_keys, n_queries=n_queries,
                method=method, idf=idf
            )
            to_log.update([('src_to_tgt_%s-%s' % (k, method), v) for k, v in results])

    def dist_mean_cosine(self, to_log):
        """
        Mean-cosine model selection criterion.
        """
        overall_mean_cosine = {x:[] for x in ['nn', 'csls_knn_10']}
        # get normalized embeddings
        src_emb =self.mapping[self.params.src_lang](self.src_emb.weight).data
        src_emb = src_emb / src_emb.norm(2, 1, keepdim=True).expand_as(src_emb)
        for lang in self.params.tgt_lang:
            tgt_emb =  self.mapping[lang](self.tgt_emb[lang].weight).data
            tgt_emb = tgt_emb / tgt_emb.norm(2, 1, keepdim=True).expand_as(tgt_emb)

            # build dictionary
            for dico_method in ['nn', 'csls_knn_10']:
                dico_build = 'S2T'
                dico_max_size = 10000
                # temp params / dictionary generation
                _params = deepcopy(self.params)
                _params.dico_method = dico_method
                _params.dico_build = dico_build
                _params.dico_threshold = 0
                _params.dico_max_rank = 10000
                _params.dico_min_size = 0
                _params.dico_max_size = dico_max_size
                s2t_candidates = get_candidates(src_emb, tgt_emb, _params)
                t2s_candidates = get_candidates(tgt_emb, src_emb, _params)
                dico = build_pairwise_dictionary(src_emb, tgt_emb, _params, s2t_candidates, t2s_candidates, True)
                # mean cosine
                if dico is None:
                    mean_cosine = -1e9
                else:
                    mean_cosine = (src_emb[dico[:dico_max_size, 0]] * tgt_emb[dico[:dico_max_size, 1]]).sum(1).mean()
                mean=mean_cosine.item()
                logger.info("Mean cosine (%s method, %s build, %i max size): %.5f"
                            % (dico_method, _params.dico_build, dico_max_size, mean))
                to_log['mean_cosine-%s-%s-%i_%s' % (dico_method, _params.dico_build, dico_max_size,lang)] = mean

    def all_eval(self, to_log, biling_dict):
        """
        Run all evaluations.
        """
        self.monolingual_wordsim(to_log)
        self.crosslingual_wordsim(to_log)
        if biling_dict: self.word_translation(to_log)
        #self.sent_translation(to_log)
        self.dist_mean_cosine(to_log)

    def eval_dis(self, to_log):
        """
        Evaluate discriminator predictions and accuracy.
        """
        bs = 128
        src_preds = []
        tgt_preds = []

        for lang in self.params.tgt_lang:
            self.discriminator.eval()
            for i in range(0, self.src_emb.num_embeddings, bs):
                emb = Variable(self.src_emb.weight[i:i + bs].data, volatile=True)
                preds = self.discriminator(self.mapping[self.params.src_lang](emb))
                src_preds.extend(preds.data.cpu().tolist())


            for i in range(0, self.tgt_emb[lang].num_embeddings, bs):
                emb = Variable(self.tgt_emb[lang].weight[i:i + bs].data, volatile=True)
                preds = self.discriminator(self.mapping[lang](emb))
                tgt_preds.extend(preds.data.cpu().tolist())

            src_pred = np.mean(src_preds)
            tgt_pred = np.mean(tgt_preds)
            logger.info("Discriminator source / target %s predictions: %.5f / %.5f"
                        % (lang, src_pred, tgt_pred))

            src_accu = np.mean([x >= 0.5 for x in src_preds])
            tgt_accu = np.mean([x < 0.5 for x in tgt_preds])
            dis_accu = ((src_accu * self.src_emb.num_embeddings + tgt_accu * self.tgt_emb[lang].num_embeddings) /
                        (self.src_emb.num_embeddings + self.tgt_emb[lang].num_embeddings))
            logger.info("Discriminator source / target %s / global accuracy: %.5f / %.5f / %.5f"
                        % (lang, src_accu, tgt_accu, dis_accu))

            to_log['dis_accu_{}'.format(lang)] = dis_accu
            to_log['dis_src_pred_{}'.format(lang)] = src_pred
            to_log['dis_tgt_pred_{}'.format(lang)] = tgt_pred
