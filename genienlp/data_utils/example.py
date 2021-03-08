#
# Copyright (c) 2019-2020 The Board of Trustees of the Leland Stanford Junior University
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from typing import NamedTuple, List, Union, Iterable
import unicodedata
import torch
from dataclasses import dataclass



def identity(x, **kw):
    return x, [], x


class SequentialField(NamedTuple):
    value: Union[torch.tensor, List[int]]
    length: Union[torch.tensor, int]
    limited: Union[torch.tensor, List[int]]
    feature: Union[torch.tensor, List[List[int]], None]


# Feature is defined per token
# Each field contains a list of possible values for that feature
@dataclass
class Feature:
    type_id: List[int] = None
    type_prob: List[float] = None

    def __mul__(self, n):
        return [self for _ in range(n)]

    def flatten(self):
        result = []
        for field in VALID_FEATURE_FIELDS:
            field_val = getattr(self, field)
            if field_val:
                result += field_val
        return result

VALID_FEATURE_FIELDS = tuple(Feature.__annotations__.keys())

def get_pad_feature(feature_fields, ned_features_default_val, ned_features_size):
    # return None if not using NED
    pad_feature = None
    if len(feature_fields):
        pad_feature = Feature()
        for i, field in enumerate(feature_fields):
            assert field in VALID_FEATURE_FIELDS
            setattr(pad_feature, field, [ned_features_default_val[i]] * ned_features_size[i])
    return pad_feature


class Example(NamedTuple):
    example_id: str
    context: str
    context_feature: List[Feature]
    question: str
    question_feature: List[Feature]
    answer: str
    answer_feature: List[Feature]
    context_plus_question: str
    context_plus_question_feature: List[Feature]
    context_plus_question_with_types: str

    @staticmethod
    def from_raw(example_id: str, context: str, question: str, answer: str, preprocess=identity, lower=False):
        args = [example_id]
        answer = unicodedata.normalize('NFD', answer)
        
        question_plus_types = ''
        context_plus_types = ''
        
        for argname, arg in (('context', context), ('question', question), ('answer', answer)):
            arg = unicodedata.normalize('NFD', arg)
            sentence, features, sentence_plus_types = preprocess(arg.rstrip('\n'), field_name=argname, answer=answer)
            
            if argname == 'context':
                context_plus_types = sentence_plus_types
            elif argname == 'question':
                question_plus_types = sentence_plus_types
            
            if lower:
                sentence = sentence.lower()
            args.append(sentence)
            args.append(features)

        # create context_plus_question fields by concatenating context and question fields
        # if either question or context is empty, don't use space
        if not args[1]:
            args.append(args[3])
        elif not args[3]:
            args.append(args[1])
        else:
            args.append(args[1] + ' ' + args[3])
        args.append(args[2] + args[4])
        args.append(context_plus_types + ' ' + question_plus_types)
        
        return Example(*args)


def tokenize_and_align_labels(all_sequences, all_labels, tokenizer, label_all_tokens=True):
    tokenized_inputs = tokenizer.batch_encode_plus(
        all_sequences,
        padding=False,
        truncation=True,
        # We use this argument because the texts in our dataset are lists of words (with a label for each word).
        is_split_into_words=True,
    )
    
    all_processed_labels = []
    for i, label in enumerate(all_labels):
        word_ids = tokenized_inputs.word_ids(batch_index=i)
        previous_word_idx = None
        label_ids = []
        for word_idx in word_ids:
            # Special tokens have a word id that is None. We set the label to -100 so they are automatically
            # ignored in the loss function.
            if word_idx is None:
                label_ids.append(-100)
            # We set the label for the first token of each word.
            elif word_idx != previous_word_idx:
                label_ids.append(label[word_idx])
            # For the other tokens in a word, we set the label to either the current label or -100, depending on
            # the label_all_tokens flag.
            else:
                label_ids.append(label[word_idx] if label_all_tokens else -100)
            previous_word_idx = word_idx

        all_processed_labels.append(label_ids)

        
    return all_processed_labels


class NumericalizedExamples(NamedTuple):
    example_id: List[str]
    context: SequentialField
    answer: SequentialField
    
    @staticmethod
    def from_examples(examples, numericalizer, add_types_to_text):
        assert all(isinstance(ex.example_id, str) for ex in examples)
        numericalized_examples = []
        

        # numericalizer.encode_batch([ex.context_plus_question_with_types for ex in examples], [[Feature(type_id=[int(val)], type_prob=[1.0]) for val in ex.answer.split(" ")] for ex in examples], 'context')
        
        if add_types_to_text != 'no':
            tokenized_contexts = numericalizer.encode_batch(
                [ex.context_plus_question_with_types for ex in examples],
                [],
                'context'
            )
        else:
            tokenized_contexts = numericalizer.encode_batch(
                                            [ex.context_plus_question for ex in examples],
                                            [ex.context_plus_question_feature for ex in examples if ex.context_plus_question_feature],
                                            'context'
            )

        # align labels
        for ex in examples:
            assert len(ex.answer.split(" ")) == len(ex.context_plus_question.split(" ")), print(ex.context_plus_question)
        
        tokenized_answers = tokenize_and_align_labels(
            [ex.context_plus_question.split(" ") for ex in examples],
            [list(map(lambda token: int(token), ex.answer.split(" "))) for ex in examples],
            numericalizer._tokenizer
        )

        batch_decoder_numerical = []
        if numericalizer.decoder_vocab:
            for i in range(len(tokenized_answers)):
                batch_decoder_numerical.append(numericalizer.decoder_vocab.encode(tokenized_answers[i]))
        else:
            batch_decoder_numerical = [[]] * len(tokenized_answers)
        
        answer_sequential_fields = []
        for i in range(len(tokenized_answers)):
            answer_sequential_fields.append(
                SequentialField(value=tokenized_answers[i], length=len(tokenized_answers[i]), limited=batch_decoder_numerical[i], feature=None))
        
        
        # tokenized_answers = numericalizer.encode_batch([ex.answer for ex in examples], [], 'answer')
        
        for i in range(len(examples)):
            numericalized_examples.append(NumericalizedExamples([examples[i].example_id],
                                        tokenized_contexts[i],
                                        answer_sequential_fields[i]))
        return numericalized_examples

    @staticmethod
    def collate_batches(batches : Iterable['NumericalizedExamples'], numericalizer, device, db_unk_id):
        example_id = []

        context_values, context_lengths, context_limiteds, context_features = [], [], [], []
        answer_values, answer_lengths, answer_limiteds, answer_features = [], [], [], []

        for batch in batches:
            example_id.append(batch.example_id[0])
            context_values.append(torch.tensor(batch.context.value, device=device))
            context_lengths.append(torch.tensor(batch.context.length, device=device))
            context_limiteds.append(torch.tensor(batch.context.limited, device=device))
            if batch.context.feature:
                context_features.append(torch.tensor(batch.context.feature, device=device))

            answer_values.append(torch.tensor(batch.answer.value, device=device))
            answer_lengths.append(torch.tensor(batch.answer.length, device=device))
            answer_limiteds.append(torch.tensor(batch.answer.limited, device=device))

        context_values = numericalizer.pad(context_values, pad_id=numericalizer.pad_id)
        context_limiteds = numericalizer.pad(context_limiteds, pad_id=numericalizer.decoder_pad_id)
        context_lengths = torch.stack(context_lengths, dim=0)
        
        if context_features:
            context_features = numericalizer.pad(context_features, pad_id=db_unk_id)

        answer_values = numericalizer.pad(answer_values, pad_id=-100)
        # answer_values = numericalizer.pad(answer_values, pad_id=numericalizer.pad_id)
        answer_limiteds = numericalizer.pad(answer_limiteds, pad_id=numericalizer.decoder_pad_id)
        answer_lengths = torch.stack(answer_lengths, dim=0)

        context = SequentialField(value=context_values,
                                  length=context_lengths,
                                  limited=context_limiteds,
                                  feature=context_features)


        answer = SequentialField(value=answer_values,
                                 length=answer_lengths,
                                 limited=answer_limiteds,
                                 feature=None)


        return NumericalizedExamples(example_id=example_id,
                                     context=context,
                                     answer=answer)