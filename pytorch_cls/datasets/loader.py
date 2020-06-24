#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Data loader."""

import os

import torch
from pytorch_cls.core.config import cfg
from pytorch_cls.datasets.cifar10 import Cifar10
from pytorch_cls.datasets.imagenet import ImageNet
from pytorch_cls.datasets.imagenet import ImageNet_Dataset
from pytorch_cls.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler


# Supported datasets
_DATASETS = {"cifar10": Cifar10, "imagenet": ImageNet,
             "imagenet_dataset": ImageNet_Dataset}


# Default data directory (/path/pycls/pycls/datasets/data)
# _DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_DATA_DIR = "/gdata"

# Relative data paths to default data directory
_PATHS = {"cifar10": "cifar10", "imagenet": "ImageNet2012",
          "imagenet_dataset": "ImageNet2012"}


def _construct_loader(dataset_name, split, batch_size, shuffle, drop_last):
    """Constructs the data loader for the given dataset."""
    err_str = "Dataset '{}' not supported".format(dataset_name)
    assert dataset_name in _DATASETS and dataset_name in _PATHS, err_str
    # Retrieve the data path for the dataset
    data_path = os.path.join(_DATA_DIR, _PATHS[dataset_name])
    # construct torch or dali dataset
    # we only support ImageNet now, TODO: other datasets
    if dataset_name == 'imagenet_dataset':
        dataset = ImageNet_Dataset(data_path,
                                   batch_size=int(cfg.TRAIN.BATCH_SIZE / cfg.NUM_GPUS),
                                   size=cfg.TRAIN.IM_SIZE,
                                   val_batch_size=int(cfg.TEST.BATCH_SIZE / cfg.NUM_GPUS),
                                   val_size=cfg.TEST.IM_SIZE,
                                   min_crop_size=0.08,
                                   workers=cfg.DATA_LOADER.NUM_WORKERS,
                                   world_size=cfg.NUM_GPUS,
                                   cuda=True,
                                   use_dali=cfg.DATA_LOADER.USE_DALI,
                                   dali_cpu=cfg.DATA_LOADER.DALI_CPU,
                                   pca_jitter=cfg.DATA_LOADER.PCA_JITTER)
        return dataset

    # Construct the dataset
    dataset = _DATASETS[dataset_name](data_path, split)
    # Create a sampler for multi-process training
    sampler = DistributedSampler(dataset) if cfg.NUM_GPUS > 1 else None
    # Create a loader
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=(False if sampler else shuffle),
        sampler=sampler,
        num_workers=cfg.DATA_LOADER.NUM_WORKERS,
        pin_memory=cfg.DATA_LOADER.PIN_MEMORY,
        drop_last=drop_last,
    )
    return loader


def construct_train_loader():
    """Train loader wrapper."""
    return _construct_loader(
        dataset_name=cfg.TRAIN.DATASET,
        split=cfg.TRAIN.SPLIT,
        batch_size=int(cfg.TRAIN.BATCH_SIZE / cfg.NUM_GPUS),
        shuffle=True,
        drop_last=True,
    )


def construct_test_loader():
    """Test loader wrapper."""
    return _construct_loader(
        dataset_name=cfg.TEST.DATASET,
        split=cfg.TEST.SPLIT,
        batch_size=int(cfg.TEST.BATCH_SIZE / cfg.NUM_GPUS),
        shuffle=False,
        drop_last=False,
    )


def shuffle(loader, cur_epoch):
    if cfg.TEST.DATASET == 'imagenet_dataset' or cfg.TRAIN.DATASET == 'imagenet_dataset':
        return
    """"Shuffles the data."""
    err_str = "Sampler type '{}' not supported".format(type(loader.sampler))
    assert isinstance(loader.sampler, (RandomSampler,
                                       DistributedSampler)), err_str
    # RandomSampler handles shuffling automatically
    if isinstance(loader.sampler, DistributedSampler):
        # DistributedSampler shuffles data based on epoch
        loader.sampler.set_epoch(cur_epoch)
