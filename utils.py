# Copyright (c) 2019-present, Thomas Wolf.
# All rights reserved. This source code is licensed under the BSD-style license found in the LICENSE file in the root directory of this source tree.
import logging
import os
from tqdm import tqdm
from pprint import pformat

import torch

from ignite.engine import Engine, Events
from ignite.handlers import ModelCheckpoint
from ignite.metrics import RunningAverage
from ignite.contrib.handlers import ProgressBar
from ignite.contrib.handlers.tensorboard_logger import OptimizerParamsHandler, OutputHandler, TensorboardLogger

from pytorch_pretrained_bert import cached_path

DATASETS_URL = {
    'wikitext-2':   {'train': "https://s3.amazonaws.com/datasets.huggingface.co/wikitext-2/train.txt",
                     'valid': "https://s3.amazonaws.com/datasets.huggingface.co/wikitext-2/valid.txt"},
    'wikitext-103': {'train': "https://s3.amazonaws.com/datasets.huggingface.co/wikitext-103/wiki.train.tokens",
                     'valid': "https://s3.amazonaws.com/datasets.huggingface.co/wikitext-103/wiki.valid.tokens"},
    'simplebooks-2-raw': {'train': "https://s3.amazonaws.com/datasets.huggingface.co/simplebooks-2-raw/train.txt",
                          'valid': "https://s3.amazonaws.com/datasets.huggingface.co/simplebooks-2-raw/valid.txt"},
    'simplebooks-92-raw': {'train': "https://s3.amazonaws.com/datasets.huggingface.co/simplebooks-92-raw/train.txt",
                           'valid': "https://s3.amazonaws.com/datasets.huggingface.co/simplebooks-92-raw/valid.txt"},
    'imdb':         {'train': "https://s3.amazonaws.com/datasets.huggingface.co/aclImdb/train.txt",
                     'valid': "https://s3.amazonaws.com/datasets.huggingface.co/aclImdb/valid.txt",
                     'labels': {'train': "https://s3.amazonaws.com/datasets.huggingface.co/aclImdb/train.labels.txt",
                                'valid': "https://s3.amazonaws.com/datasets.huggingface.co/aclImdb/valid.labels.txt",
                                'convert': {'pos': 0, 'neg': 1}}},
    }

PRETRAINED_MODEL_URL = "https://s3.amazonaws.com/models.huggingface.co/naacl-2019-tutorial/"

WEIGHTS_NAME = 'model_checkpoint.pth'
CONFIG_NAME = 'model_training_args.bin'

logger = logging.getLogger(__file__)


def average_distributed_scalar(scalar, args):
    """ Average a scalar over the nodes if we are in distributed training. We use this for distributed evaluation. """
    if args.local_rank == -1:
        return scalar
    scalar_t = torch.tensor(scalar, dtype=torch.float, device=args.device) / torch.distributed.get_world_size()
    torch.distributed.all_reduce(scalar_t, op=torch.distributed.ReduceOp.SUM)
    return scalar_t.item()


def pad_dataset(dataset, padding=0):
    """ Pad a dataset (list of list).
        This could be optimized by defining a Dataset class and dynamically pad batches but this is easier to write.
    """
    max_l = max(len(x) for x in dataset)
    dataset = [x + [padding] * (max_l - len(x)) for x in dataset]
    return dataset


def add_logging_and_checkpoint_saving(trainer, evaluator, metrics, model, optimizer, args):
    """ Add tensorboard logging, progress bar and checkpoint saving to a training engine and save training config. """
    RunningAverage(output_transform=lambda x: x).attach(trainer, "loss")
    pbar = ProgressBar(persist=True)
    pbar.attach(trainer, metric_names=["loss"])
    evaluator.add_event_handler(Events.COMPLETED, lambda _: pbar.log_message("Validation: %s" % pformat(evaluator.state.metrics)))

    tb_logger = TensorboardLogger(log_dir=None)
    tb_logger.attach(trainer, log_handler=OutputHandler(tag="training", metric_names=["loss"]), event_name=Events.ITERATION_COMPLETED)
    tb_logger.attach(trainer, log_handler=OptimizerParamsHandler(optimizer), event_name=Events.ITERATION_STARTED)

    @evaluator.on(Events.COMPLETED)  # Log evaluator metrics on tensorboard
    def tb_log_metrics(engine):
        for name in metrics.keys():
            tb_logger.writer.add_scalar(name, engine.state.metrics[name], trainer.state.iteration)

    checkpoint_handler = ModelCheckpoint(tb_logger.writer.log_dir, 'checkpoint', save_interval=1, n_saved=3)
    trainer.add_event_handler(Events.EPOCH_COMPLETED, checkpoint_handler, {'mymodel': getattr(model, 'module', model)})  # "getattr" take care of distributed encapsulation

    torch.save(args, os.path.join(tb_logger.writer.log_dir, CONFIG_NAME))


def get_and_tokenize_dataset(tokenizer, dataset_dir='wikitext-103', dataset_cache=None, with_labels=False):
    """ Retrieve, tokenize, encode and cache a dataset with optional labels """
    if dataset_cache and os.path.isfile(dataset_cache):
        logger.info("Load encoded dataset from cache at %s", dataset_cache)
        encoded_dataset = torch.load(dataset_cache)
    else:
        if dataset_dir in DATASETS_URL:
            dataset_dir = DATASETS_URL[dataset_dir]
        else:
            dataset_dir = {'train': os.path.join(dataset_dir, 'train.txt'),
                           'valid': os.path.join(dataset_dir, 'valid.txt')}
        logger.info("Get dataset from %s", dataset_dir)
        dataset = {}
        for split_name in ['train', 'valid']:
            dataset_file = cached_path(dataset_dir[split_name])
            with open(dataset_file, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
                dataset[split_name] = [
                    line.strip(' ').replace('\n', '[SEP]').replace('<unk>', '[UNK]') for line in tqdm(all_lines)]
        labels = {}
        if with_labels:
            for split_name in ['train', 'valid']:
                dataset_file = cached_path(dataset_dir['labels'][split_name])
                with open(dataset_file, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                    labels[split_name] = [dataset_dir['labels']['convert'][line.strip()] for line in tqdm(all_lines)]

        logger.info("Tokenize and encode the dataset")
        logging.getLogger("pytorch_pretrained_bert.tokenization").setLevel(logging.ERROR)  # No warning on sample size
        def encode(obj):
            if isinstance(obj, str):
                return tokenizer.convert_tokens_to_ids(tokenizer.tokenize(obj))
            if isinstance(obj, dict):
                return dict((n, encode(o)) for n, o in obj.items())
            return list(encode(o) for o in tqdm(obj))
        encoded_dataset = encode(dataset)

        # Add labels if classification, or for language modeling, add number of words and gather in one list
        for split_name in ['train', 'valid']:
            if with_labels:
                encoded_dataset[split_name + '_labels'] = labels[split_name]
            else:
                encoded_dataset[split_name] = [ind for line in encoded_dataset[split_name] for ind in line]
                encoded_dataset[split_name + '_num_words'] = sum(len(line.split(' ')) for line in dataset[split_name])

        if dataset_cache:
            logger.info("Save encoded dataset to cache at %s", dataset_cache)
            torch.save(encoded_dataset, dataset_cache)

    return encoded_dataset
