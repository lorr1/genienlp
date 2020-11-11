import torch
import math
import os
import glob
import re
import logging
import shutil
import numpy as np
import matplotlib.pyplot as plt

from operator import mul

from .transformers_utils import SPIECE_UNDERLINE

from genienlp.metrics import computeBLEU

logger = logging.getLogger(__name__)

def sort_checkpoints(output_dir):
    return list(sorted(glob.glob(os.path.join(output_dir, "checkpointepoch=*.ckpt"), recursive=True)))


def get_transformer_schedule_with_warmup(optimizer, num_warmup_steps, num_training_steps, dimension):
    num_warmup_steps = max(1, num_warmup_steps)

    def lr_lambda(current_step):
        current_step += 1
        return 1. / math.sqrt(dimension) * min(1 / math.sqrt(current_step), current_step / (num_warmup_steps * math.sqrt(num_warmup_steps)))

    return torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)



def _rotate_checkpoints(args, checkpoint_prefix, use_mtime=False):
    if not args.save_total_limit:
        return
    if args.save_total_limit <= 0:
        return

    # Check if we should delete older checkpoint(s)
    glob_checkpoints = glob.glob(os.path.join(args.output_dir, '{}-*'.format(checkpoint_prefix)))
    if len(glob_checkpoints) <= args.save_total_limit:
        return

    ordering_and_checkpoint_path = []
    for path in glob_checkpoints:
        if use_mtime:
            ordering_and_checkpoint_path.append((os.path.getmtime(path), path))
        else:
            regex_match = re.match('.*{}-([0-9]+)'.format(checkpoint_prefix), path)
            if regex_match and regex_match.groups():
                ordering_and_checkpoint_path.append((int(regex_match.groups()[0]), path))

    checkpoints_sorted = sorted(ordering_and_checkpoint_path)
    checkpoints_sorted = [checkpoint[1] for checkpoint in checkpoints_sorted]
    number_of_checkpoints_to_delete = max(0, len(checkpoints_sorted) - args.save_total_limit)
    checkpoints_to_be_deleted = checkpoints_sorted[:number_of_checkpoints_to_delete]
    for checkpoint in checkpoints_to_be_deleted:
        logger.info("Deleting older checkpoint [{}] due to args.save_total_limit".format(checkpoint))
        shutil.rmtree(checkpoint)


def compute_ece(exact_match, confidences, num_bins = 10, binning = 'uniform', output_reliability_diagrams = None):
    '''
    Compute expected calibration error.
    binning = 'uniform' corresponds to standard ECE, binning = 'adaptive' corresponds to AdaECE as defined in https://arxiv.org/pdf/2002.09437.pdf
    output_reliability_diagrams: a path to output reliability diagram plots for ECE, AdaECE
    '''
    if binning == 'uniform':
        bin_intervals = np.arange(0, 1, 1.0 / num_bins)
    elif binning == 'adaptive':
        bin_intervals = np.quantile(confidences, np.arange(0, 1, 1.0/num_bins), interpolation='higher')

    bin_assignment = np.digitize(confidences, bin_intervals)
    bin_accuracies = [0.0 for _ in range(num_bins)]
    bin_confidences = [0.0 for _ in range(num_bins)]
    bin_sizes = [0 for _ in range(num_bins)]
    for i, bin in enumerate(bin_assignment):
        bin_confidences[bin - 1] += confidences[i]
        bin_accuracies[bin - 1] += exact_match[i]
        bin_sizes[bin - 1] += 1
    total_count = sum(bin_sizes)
    ece = 0.0
    for bin, conf in enumerate(bin_confidences):
        ece += abs(conf - bin_accuracies[bin]) if bin_sizes[bin] > 0 else 0
    ece /= total_count

    if output_reliability_diagrams:
        bin_accuracies = [acc / bin_sizes[i] if bin_sizes[i] > 0 else 0 for (i, acc) in enumerate(bin_accuracies)]
        total_count = sum(bin_sizes)
        bin_fractions = [size / total_count for size in bin_sizes]
        fig, axs = plt.subplots(2, figsize=(5,7), sharex=True, sharey=True)
        axs[0].bar(np.arange(num_bins), bin_fractions)
        axs[0].set_ylabel("Percent of Samples")
        axs[1].bar(np.arange(num_bins), bin_accuracies)
        axs[1].set_ylabel("Accuracy")
        axs[1].set_xlabel("Confidence")
        if binning == 'uniform':
            axs[1].plot([0,num_bins],[0,1], '--', c='black')
        fig.suptitle(f"Reliability diagram, {binning} bins")
        plt.ylim(0,1)
        plt.xticks(np.arange(num_bins), [round(x, 2) for x in bin_intervals])
        plt.savefig(f"{output_reliability_diagrams}_{binning}")

    return ece


def compute_metrics(
    generations,
    golds,
    reduction='average',
    output_reliability_diagrams = None,
    beam_search = False,
    calibrator_path = None
):
    """
    Inputs:
        generations: a list of list of strings; generations[i] is a list of all generated outputs of the model for example i
        golds: a list of strings; golds[i] is the gold answer for example i
        reduction: how we should compute an example's metrics from its multiple generations
        output_reliability_diagrams: path prefix for reliability diagram output plots
        beam_search: whether beam search was used (affect format of confidence scores)
        calibrator_path: if specified, file path of random forest calibrator to use instead of direct probabilities
    """
    calibrator = None
    if calibrator_path is not None:
        import xgboost as xgb
        calibrator = xgb.Booster()
        calibrator.load_model(calibrator_path)
    
    # Store prediction metrics for thresholds at percent-increments to plot later
    thresholds = np.arange(0, 1, 0.01)
    prediction_metrics = {'thresholds': thresholds}
    for metric in ["tp","tn","fp","fn"]:
        prediction_metrics[metric] = np.zeros_like(thresholds)

    all_bleu = []
    all_exact_matches = []
    sentence_confidences = []
    for idx, output in enumerate(generations):
        bleu_score = 0.0
        exact_match = 0.0
        sentence_confidence = 0.0
        for sample in output:
            if isinstance(sample, tuple):
                sample, confidences = sample
                confidence = np.max(confidences) if beam_search else np.prod(confidences)
                if calibrator is not None:
                    calibrator_input = np.atleast_2d(confidences if beam_search else confidence)
                    confidence = calibrator.predict(xgb.DMatrix(calibrator_input))[0]
                    # TODO: if we do other than using classifier/logisitc loss,
                    # apply logistic function so the result is in [0,1]
                    # confidence = 1 / (1 + np.exp(-confidence))
                if reduction == 'average':
                    sentence_confidence += confidence
                else:
                    sentence_confidence = max(sentence_confidence, confidence)
            if reduction == 'average':
                bleu_score += computeBLEU([sample], [[golds[idx]]])
            else:
                bleu_score = max(bleu_score, computeBLEU([sample], [[golds[idx]]]))
            if re.sub('\s+', '', sample).lower() == re.sub('\s+', '', golds[idx]).lower():
                if reduction == 'average':
                    exact_match += 1
                else:
                    exact_match = max(exact_match, 1)
        if reduction == 'average':
            bleu_score /= len(output)
            exact_match /= len(output)
            sentence_confidence /= len(output)
        for i, threshold in enumerate(thresholds):
            if sentence_confidence >= threshold:
                if exact_match >= 0.5:
                    prediction_metrics['tp'][i] += 1
                else:
                    prediction_metrics['fp'][i] += 1
            else:
                if exact_match < 0.5:
                    prediction_metrics['tn'][i] += 1
                else:
                    prediction_metrics['fn'][i] += 1
        all_bleu.append(bleu_score)
        all_exact_matches.append(exact_match)
        sentence_confidences.append(sentence_confidence)

    # compute_uncertainty_metrics()
    total_bleu = sum(all_bleu)
    total_exact_match = sum(all_exact_matches)
    ece = compute_ece(all_exact_matches, sentence_confidences, binning='uniform', output_reliability_diagrams=output_reliability_diagrams)
    ada_ece = compute_ece(all_exact_matches, sentence_confidences, binning='adaptive')

    prediction_metrics['included'] = (
        prediction_metrics['tp'] + prediction_metrics['fp']) / (
        prediction_metrics['tp'] + prediction_metrics['fp'] + prediction_metrics['tn'] + prediction_metrics['fn'])
    prediction_metrics['prediction_acc'] = (
        prediction_metrics['tp'] + prediction_metrics['tn']) / (
        prediction_metrics['tp'] + prediction_metrics['fp'] + prediction_metrics['tn'] + prediction_metrics['fn'])
    prediction_metrics['precision'] = prediction_metrics['tp'] / (
        prediction_metrics['tp'] + prediction_metrics['fp'])
    prediction_metrics['F1'] = 2 * prediction_metrics['tp'] / (
        2 * prediction_metrics['tp'] + prediction_metrics['fp'] +  + prediction_metrics['fn'])

    if output_reliability_diagrams is not None:
        fig, ax = plt.subplots(1, figsize=(5,4))
        ax.plot(thresholds, prediction_metrics['included'], label='Fraction included')
        ax.plot(thresholds, prediction_metrics['precision'], label='Precision')
        ax.plot(thresholds, prediction_metrics['F1'], label='F1 score')
        ax.set_xlabel("Confidence Threshold")
        fig.suptitle("Model Performance vs. Confidence Threshold")
        plt.ylim(top=1.02)
        plt.legend()
        plt.savefig(f"{output_reliability_diagrams}_performance")

    return {
        'bleu': total_bleu / len(all_bleu),
        'em': 100.0 * total_exact_match / len(all_exact_matches),
        'ece': ece,
        'ada_ece': ada_ece,
        'prediction_metrics': prediction_metrics
    }


def compute_attention(sample_layer_attention, att_pooling):
    sample_layer_attention_pooled = None
    if att_pooling == 'mean':
        sample_layer_attention_pooled = torch.mean(sample_layer_attention, dim=0, keepdim=False)
    elif att_pooling == 'max':
        sample_layer_attention_pooled = torch.max(sample_layer_attention, dim=0, keepdim=False)[0]

    return sample_layer_attention_pooled


def replace_quoted_params(src_tokens, tgt_tokens, tokenizer, sample_layer_attention_pooled, model_type, tgt_lang):
    # find positions of quotation marks in src and tgt
    src2tgt_mapping = {}
    src2tgt_mapping_index = {}

    ## FIXED: quotation marks are exclusively used to wrap parameters so just check if they are present in target token
    # quote_wordpiece = tokenizer.tokenize('"')[0]
    # quote_token = '"'
    src_quotation_symbols = ['"']
    tgt_quotation_symbols = ['"']
    if tgt_lang == 'ru':
        tgt_quotation_symbols.extend(['«', '»'])

    src_spans_ind = [index for index, token in enumerate(src_tokens) if
                     any([symbol in token for symbol in src_quotation_symbols])]
    tgt_spans_ind = [index for index, token in enumerate(tgt_tokens) if
                     any([symbol in token for symbol in tgt_quotation_symbols])]

    if model_type == 'marian':
        src_strings = tokenizer.spm_source.DecodePieces(src_tokens)
        tgt_strings = tokenizer.spm_target.DecodePieces(tgt_tokens)
    else:
        src_strings = tokenizer.convert_tokens_to_string(src_tokens)
        tgt_strings = tokenizer.convert_tokens_to_string(tgt_tokens)

    if len(src_spans_ind) % 2 != 0:
        logging.error('corrupted span in src string: [{}]'.format(src_strings))
        return tgt_strings, False
    if len(tgt_spans_ind) % 2 != 0:
        logging.error('corrupted span in tgt string: [{}] with src string: [{}]\n'
                      'outputting example without reverting the parameter'.format(tgt_strings, src_strings))
        return tgt_strings, False

    # arrange spans and exclude quotation mark indices
    src_spans = [(src_spans_ind[i] + 1, src_spans_ind[i + 1] - 1) for i in range(0, len(src_spans_ind), 2)]
    tgt_spans = [(tgt_spans_ind[i] + 1, tgt_spans_ind[i + 1] - 1) for i in range(0, len(tgt_spans_ind), 2)]

    if len(src_spans) != len(tgt_spans):
        logging.error('numbers of spans in src and tgt strings do not match: [{}], [{}]\n'
                      'outputting example without reverting the parameter'.format(src_strings, tgt_strings))

        return tgt_strings, False

    tgt_span_success = set()
    for src_idx, (beg, end) in enumerate(src_spans):
        i = beg
        tgt_span_idx = None
        while i <= end:
            max_tgt_att_idx = torch.argmax(sample_layer_attention_pooled[:, i]).item()

            # find span in tgt that contains this index
            for tgt_idx, (s1, s2) in enumerate(tgt_spans):
                if s1 <= max_tgt_att_idx <= s2 and (s1, s2) not in tgt_span_success:
                    tgt_span_idx = tgt_idx
                    src2tgt_mapping[(beg, end)] = (s1, s2)
                    src2tgt_mapping_index[src_idx] = tgt_span_idx
                    tgt_span_success.add((s1, s2))
                    break
            if tgt_span_idx is not None:
                break
            else:
                # span could not be found; check the next wordpiece
                i += 1

        if tgt_span_idx is None:
            logger.error(
                'Could not find a corresponding span in tgt for ({}, {}) src span in src string: [{}]'.format(beg, end,
                                                                                                              src_strings))
            return tgt_strings, False
    ####
    # replacing in word-piece space is not clean since Marian uses different spm models for src and tgt
    ####
    # # replace property values (wrapped in quotation marks) in target text with source values
    # tgt2src_mapping = {v: k for k, v in src2tgt_mapping.items()}
    # tgt_begin2span = {k[0]: k for k, v in tgt2src_mapping.items()}
    # all_tgt_begins = set(tgt_begin2span.keys())
    #
    # new_tgt_tokens = []
    # i = 0
    # while i < len(tgt_tokens):
    #     if i in all_tgt_begins:
    #         tgt_span = tgt_begin2span[i]
    #         src_span = tgt2src_mapping[tgt_span]
    #         new_tgt_tokens.extend(src_tokens[src_span[0]: src_span[1]+1])
    #         i += tgt_span[1] - tgt_span[0] + 1
    #     else:
    #         new_tgt_tokens.append(tgt_tokens[i])
    #         i += 1
    # final_output = tokenizer.convert_tokens_to_ids(new_tgt_tokens)

    src_quoted_pattern_maybe_space = re.compile(r'[{0}]\s?([^{0}]*?)\s?[{0}]'.format(''.join(src_quotation_symbols)))
    tgt_quoted_pattern_maybe_space = re.compile(r'[{0}]\s?([^{0}]*?)\s?[{0}]'.format(''.join(tgt_quotation_symbols)))

    src_matches = list(re.finditer(src_quoted_pattern_maybe_space, src_strings))
    tgt_matches = list(re.finditer(tgt_quoted_pattern_maybe_space, tgt_strings))

    tgt2src_mapping_index = {v: k for k, v in src2tgt_mapping_index.items()}

    # move through characters
    tokens = []
    curr = 0
    for pos, match in enumerate(tgt_matches):
        start, end = match.span()
        if start > curr:
            tokens.append(tgt_strings[curr:start])
        replace_match = src_matches[tgt2src_mapping_index[pos]]
        tokens.append(replace_match.group(0))
        curr = end
    if curr < len(tgt_strings):
        tokens.append(tgt_strings[curr:])

    text = ' '.join(tokens)

    return text, True


def force_replace_quoted_params(src_tokens, tgt_tokens, tokenizer, sample_layer_attention_pooled, model_type):
    # find positions of quotation marks in src
    src2tgt_mapping = {}

    src_spans_ind = [index for index, token in enumerate(src_tokens) if '"' in token]
    tgt_is_piece = [1 if token[0] == SPIECE_UNDERLINE else 0 for token in tgt_tokens]
    tgt_piece2word_mapping = list(np.cumsum(tgt_is_piece) - 1)

    if len(src_spans_ind) % 2 != 0:
        logging.error('corrupted span in src string: [{}]'.format(tokenizer.spm_source.DecodePieces(src_tokens)))
        # this almost never happens but if it does it is usually because quotation is missing from the end of src_tokens
        # we temporary fix this by adding '"' to the end of src_tokens
        src_tokens += tokenizer.tokenize('"')
        src_spans_ind = [index for index, token in enumerate(src_tokens) if '"' in token]

    if model_type == 'marian':
        src_strings = tokenizer.spm_source.DecodePieces(src_tokens)
        tgt_strings = tokenizer.spm_target.DecodePieces(tgt_tokens)
    else:
        src_strings = tokenizer.convert_tokens_to_string(src_tokens)
        tgt_strings = tokenizer.convert_tokens_to_string(tgt_tokens)

    # arrange spans and exclude quotation mark indices
    src_spans = [(src_spans_ind[i] + 1, src_spans_ind[i + 1] - 1) for i in range(0, len(src_spans_ind), 2)]

    for src_idx, (beg, end) in enumerate(src_spans):
        s1 = torch.argmax(sample_layer_attention_pooled[:, beg]).item()
        s2 = torch.argmax(sample_layer_attention_pooled[:, end]).item()

        # clamp values to max tgt_tokens length
        s1 = min(s1, len(tgt_tokens) - 1)
        s2 = min(s2, len(tgt_tokens) - 1)

        src2tgt_mapping[(beg, end)] = (s1, s2)

    quoted_pattern_maybe_space = re.compile(r'\"\s?([^"]*?)\s?\"')

    src_matches = list(re.finditer(quoted_pattern_maybe_space, src_strings))

    # update src2tgt_mapping to map to word indices in response
    for key, value in src2tgt_mapping.items():
        s1, s2 = value
        try:
            src2tgt_mapping[key] = (
            max(0, tgt_piece2word_mapping[s1] - 1), min(tgt_piece2word_mapping[s2] + 1, len(tgt_tokens)))
        except:
            raise ValueError('corrupted span in tgt string: [{}] with src string: [{}]\n'
                             'outputting example without reverting the parameter'.format(tgt_strings, src_strings))

    # move through words
    tgt_strings_words = tgt_strings.split(' ')
    tokens = []
    curr = 0
    for i, (key, value) in enumerate(src2tgt_mapping.items()):
        start, end = value
        if start > curr:
            tokens.extend(tgt_strings_words[curr:start])
        replace_match = src_matches[i]
        tokens.append(replace_match.group(0))
        curr = end
    if curr < len(tgt_strings_words):
        tokens.extend(tgt_strings_words[curr:])

    text = ' '.join(tokens)

    return text
