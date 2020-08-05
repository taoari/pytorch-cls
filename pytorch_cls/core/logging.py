#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Logging."""

import builtins
import decimal
import logging
import os
import sys

import pytorch_cls.core.distributed as dist
import simplejson
from pytorch_cls.core.config import cfg


# Show filename and line number in logs
_FORMAT = "[%(filename)s: %(lineno)3d]: %(message)s"

# Log file name (for cfg.LOG_DEST = 'file')
_LOG_FILE = "stdout.log"

# Data output with dump_log_data(data, data_type) will be tagged w/ this
_TAG = "json_stats: "

# Data output with dump_log_data(data, data_type) will have data[_TYPE]=data_type
_TYPE = "_type"


def _suppress_print():
    """Suppresses printing from the current process."""

    def ignore(*_objects, _sep=" ", _end="\n", _file=sys.stdout, _flush=False):
        pass

    builtins.print = ignore

def init_logger(log_file, head='%(asctime)-15s] %(message)s'):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # create console handler and set level to info
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(head)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # auto time stamp the log file name
    if log_file == 'auto':
        from datetime import datetime
        log_file = str(datetime.now()).replace(' ', 'T').replace(':', '-') + '.log.txt'

    # create debug file handler and set level to debug
    handler = logging.FileHandler(log_file, "w")
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(head)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def setup_logging():
    """Sets up the logging."""
    # Enable logging only for the master process
    if dist.is_master_proc():
        init_logger(os.path.join(cfg.OUT_DIR, _LOG_FILE), _FORMAT)
#        # Clear the root logger to prevent any existing logging config
#        # (e.g. set by another module) from messing with our setup
#        logging.root.handlers = []
#        # Construct logging configuration
#        logging_config = {"level": logging.INFO, "format": _FORMAT}
#        # Log either to stdout or to a file
#        if cfg.LOG_DEST == "stdout":
#            logging_config["stream"] = sys.stdout
#        else:
#            logging_config["filename"] = os.path.join(cfg.OUT_DIR, _LOG_FILE)
#        # Configure logging
#        logging.basicConfig(**logging_config)
    else:
        _suppress_print()


def get_logger(name):
    """Retrieves the logger."""
    return logging.getLogger(name)


def dump_log_data(data, data_type, prec=4):
    """Covert data (a dictionary) into tagged json string for logging."""
    data[_TYPE] = data_type
    data = float_to_decimal(data, prec)
    data_json = simplejson.dumps(data, sort_keys=True, use_decimal=True)
    return "{:s}{:s}".format(_TAG, data_json)


def float_to_decimal(data, prec=4):
    """Convert floats to decimals which allows for fixed width json."""
    if isinstance(data, dict):
        return {k: float_to_decimal(v, prec) for k, v in data.items()}
    if isinstance(data, float):
        return decimal.Decimal(("{:." + str(prec) + "f}").format(data))
    else:
        return data


def get_log_files(log_dir, name_filter=""):
    """Get all log files in directory containing subdirs of trained models."""
    names = [n for n in sorted(os.listdir(log_dir)) if name_filter in n]
    files = [os.path.join(log_dir, n, _LOG_FILE) for n in names]
    f_n_ps = [(f, n) for (f, n) in zip(files, names) if os.path.exists(f)]
    files, names = zip(*f_n_ps) if f_n_ps else [], []
    return files, names


def load_log_data(log_file, data_types_to_skip=()):
    """Loads log data into a dictionary of the form data[data_type][metric][index]."""
    # Load log_file
    assert os.path.exists(log_file), "Log file not found: {}".format(log_file)
    with open(log_file, "r") as f:
        lines = f.readlines()
    # Extract and parse lines that start with _TAG and have a type specified
    lines = [_l[_l.find(_TAG) + len(_TAG):] for _l in lines if _TAG in _l]
    lines = [simplejson.loads(_l) for _l in lines]
    lines = [_l for _l in lines if _TYPE in _l and not _l[_TYPE]
             in data_types_to_skip]
    # Generate data structure accessed by data[data_type][index][metric]
    data_types = [_l[_TYPE] for _l in lines]
    data = {t: [] for t in data_types}
    for t, line in zip(data_types, lines):
        del line[_TYPE]
        data[t].append(line)
    # Generate data structure accessed by data[data_type][metric][index]
    for t in data:
        metrics = sorted(data[t][0].keys())
        err_str = "Inconsistent metrics in log for _type={}: {}".format(
            t, metrics)
        assert all(sorted(d.keys()) == metrics for d in data[t]), err_str
        data[t] = {m: [d[m] for d in data[t]] for m in metrics}
    return data


def sort_log_data(data):
    """Sort each data[data_type][metric] by epoch or keep only first instance."""
    for t in data:
        if "epoch" in data[t]:
            epoch = [float(e.split("/")[0]) for e in data[t]["epoch"]]
            if "iter" in data[t]:
                i_cur = [float(i.split("/")[0]) for i in data[t]["iter"]]
                i_max = [float(i.split("/")[1]) for i in data[t]["iter"]]
                epoch = [e + (ic - 1.0) / im for e, ic,
                         im in zip(epoch, i_cur, i_max)]
            for m in data[t]:
                data[t][m] = [v for _, v in sorted(zip(epoch, data[t][m]))]
        else:
            data[t] = {m: d[0] for m, d in data[t].items()}
    return data
