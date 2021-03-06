# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from logging import getLogger
import torch
import numpy as np
from .utils import get_nn_avg_dist


logger = getLogger()


def get_candidates(emb1, emb2, params):
    """
    Get best translation pairs candidates.
    """
    bs = 128

    all_scores = []
    all_targets = []

    # number of source words to consider
    n_src = emb1.size(0)
    if params.dico_max_rank > 0 and not params.dico_method.startswith('invsm_beta_'):
        n_src = params.dico_max_rank

    # nearest neighbors
    if params.dico_method == 'nn':

        # for every source word
        for i in range(0, n_src, bs):

            # compute target words scores
            scores = emb2.mm(emb1[i:min(n_src, i + bs)].transpose(0, 1)).transpose(0, 1)
            best_scores, best_targets = scores.topk(2, dim=1, largest=True, sorted=True)

            # update scores / potential targets
            all_scores.append(best_scores.cpu())
            all_targets.append(best_targets.cpu())

        all_scores = torch.cat(all_scores, 0)
        all_targets = torch.cat(all_targets, 0)

    # inverted softmax
    elif params.dico_method.startswith('invsm_beta_'):

        beta = float(params.dico_method[len('invsm_beta_'):])

        # for every target word
        for i in range(0, emb2.size(0), bs):

            # compute source words scores
            scores = emb1.mm(emb2[i:i + bs].transpose(0, 1))
            scores.mul_(beta).exp_()
            scores.div_(scores.sum(0, keepdim=True).expand_as(scores))

            best_scores, best_targets = scores.topk(2, dim=1, largest=True, sorted=True)

            # update scores / potential targets
            all_scores.append(best_scores.cpu())
            all_targets.append((best_targets + i).cpu())

        all_scores = torch.cat(all_scores, 1)
        all_targets = torch.cat(all_targets, 1)

        all_scores, best_targets = all_scores.topk(2, dim=1, largest=True, sorted=True)
        all_targets = all_targets.gather(1, best_targets)

    # contextual dissimilarity measure
    elif params.dico_method.startswith('csls_knn_'):

        knn = params.dico_method[len('csls_knn_'):]
        assert knn.isdigit()
        knn = int(knn)

        # average distances to k nearest neighbors
        average_dist1 = torch.from_numpy(get_nn_avg_dist(emb2, emb1, knn))
        average_dist2 = torch.from_numpy(get_nn_avg_dist(emb1, emb2, knn))
        average_dist1 = average_dist1.type_as(emb1)
        average_dist2 = average_dist2.type_as(emb2)

        # for every source word
        for i in range(0, n_src, bs):

            # compute target words scores
            scores = emb2.mm(emb1[i:min(n_src, i + bs)].transpose(0, 1)).transpose(0, 1)
            scores.mul_(2)
            scores.sub_(average_dist1[i:min(n_src, i + bs)][:, None] + average_dist2[None, :])
            best_scores, best_targets = scores.topk(2, dim=1, largest=True, sorted=True)

            # update scores / potential targets
            all_scores.append(best_scores.cpu())
            all_targets.append(best_targets.cpu())

        all_scores = torch.cat(all_scores, 0)
        all_targets = torch.cat(all_targets, 0)

    all_pairs = torch.cat([
        torch.arange(0, all_targets.size(0)).long().unsqueeze(1),
        all_targets[:, 0].unsqueeze(1)
    ], 1)

    # sanity check
    assert all_scores.size() == all_pairs.size() == (n_src, 2)

    # sort pairs by score confidence
    diff = all_scores[:, 0] - all_scores[:, 1]
    reordered = diff.sort(0, descending=True)[1]
    all_scores = all_scores[reordered]
    all_pairs = all_pairs[reordered]

    # max dico words rank
    if params.dico_max_rank > 0:
        selected = all_pairs.max(1)[0] <= params.dico_max_rank
        mask = selected.unsqueeze(1).expand_as(all_scores).clone()
        all_scores = all_scores.masked_select(mask).view(-1, 2)
        all_pairs = all_pairs.masked_select(mask).view(-1, 2)

    # max dico size
    if params.dico_max_size > 0:
        all_scores = all_scores[:params.dico_max_size]
        all_pairs = all_pairs[:params.dico_max_size]

    # min dico size
    diff = all_scores[:, 0] - all_scores[:, 1]
    if params.dico_min_size > 0:
        diff[:params.dico_min_size] = 1e9

    # confidence threshold
    if params.dico_threshold > 0:
        mask = diff > params.dico_threshold
        logger.info("Selected %i / %i pairs above the confidence threshold." % (mask.sum(), diff.size(0)))
        mask = mask.unsqueeze(1).expand_as(all_pairs).clone()
        all_pairs = all_pairs.masked_select(mask).view(-1, 2)

    return all_pairs


def build_pairwise_dictionary(src_emb, tgt_emb, params, s2t_candidates=None, t2s_candidates=None, return_tensor=False):
    """
    Build a training dictionary given current embeddings / mapping.
    """
    logger.info("Building the train dictionary ...")
    s2t = 'S2T' in params.dico_build
    t2s = 'T2S' in params.dico_build
    assert s2t or t2s

    if s2t:
        if s2t_candidates is None:
            s2t_candidates = get_candidates(src_emb, tgt_emb, params)
    if t2s:
        if t2s_candidates is None:
            t2s_candidates = get_candidates(tgt_emb, src_emb, params)
        t2s_candidates = torch.cat([t2s_candidates[:, 1:], t2s_candidates[:, :1]], 1)

    if params.dico_build == 'S2T':
        dico = s2t_candidates
    elif params.dico_build == 'T2S':
        dico = t2s_candidates
    else:
        s2t_candidates = set([(a, b) for a, b in s2t_candidates.numpy()])
        t2s_candidates = set([(a, b) for a, b in t2s_candidates.numpy()])
        if params.dico_build == 'S2T|T2S':
            final_pairs = s2t_candidates | t2s_candidates
        else:
            assert params.dico_build == 'S2T&T2S'
            final_pairs = s2t_candidates & t2s_candidates
            if len(final_pairs) == 0:
                logger.warning("Empty intersection ...")
                return None
        dico = list([[a, b] for (a, b) in final_pairs])

    logger.info('New train dictionary of %i pairs.' % len(dico))
    if return_tensor:
        dico = torch.LongTensor(dico)
        return dico.cuda() if params.cuda else dico
    #pdb.set_trace()
    else: return np.array(dico)

def cross_match_dictionary(lang_list, dico, dico_inbn, params):
    final_dico = []

    for row in dico[lang_list[0]]:#iterate over candidate pairs in one dico and check if they exist in others
        src_word=row[0]
        if all([src_word in dico[lang][:,0] for lang in lang_list]):
            new_row = [src_word]
            new_row+=[dico[lang][np.where(dico[lang][:,0]==src_word)][0][1] for lang in lang_list]###TODO: why we take only the first element?
            final_dico.append(new_row)
        elif dico_inbn is not None:
            if row[1] in dico_inbn[params.tgt_lang[1]][:,0]:
                new_row = [src_word,row[1]]
                new_row+=[dico_inbn[params.tgt_lang[1]][np.where(dico_inbn[params.tgt_lang[1]][:,0]==row[1])][0][1]]
                final_dico.append(new_row)
    if dico_inbn is not None:
        for row in dico[params.tgt_lang[-1]]:
            src_word=row[0]
            if not src_word in dico[params.tgt_lang[0]][:,0]:
                if row[1] in dico_inbn[params.tgt_lang[1]][:,1]:
                    new_row = [src_word]
                    new_row+=[dico_inbn[params.tgt_lang[1]][np.where(dico_inbn[params.tgt_lang[1]][:,1]==row[1])][0][0]]
                    new_row+=[row[1]]
                    final_dico.append(new_row)

    final_dico = np.array(final_dico,)##TODO: some pairs may repeat
    dico = torch.from_numpy(final_dico)

    logger.info('New FINAL train dictionary of %i pairs.' % len(dico))

    return dico.cuda() if params.cuda else dico

def build_dictionary(src_emb, tgt_emb, params, support, s2t_candidates=None, t2s_candidates=None):
    dico, dico_inbn = {}, {} #dico --> source to target languages dico; dico_inbn --> between target languages dico (only works with two for now)
    lang_list = [params.tgt_lang[-1]] if not support else params.tgt_lang #only consider the last tagret language if no support

    for lang in lang_list:
        dico[lang] = build_pairwise_dictionary(src_emb,tgt_emb[lang],params, s2t_candidates, t2s_candidates)

    if support and len(lang_list)>1:
        dico_inbn[params.tgt_lang[1]] = build_pairwise_dictionary(tgt_emb[params.tgt_lang[0]],tgt_emb[params.tgt_lang[1]],params, s2t_candidates, t2s_candidates)
    else: dico_inbn = None

    return cross_match_dictionary(lang_list, dico, dico_inbn, params)##TODO: why remove supervied pairs??
