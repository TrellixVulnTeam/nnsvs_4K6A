import shutil
from glob import glob
from os.path import join
from pathlib import Path

import hydra
import joblib
import mlflow
import numpy as np
import torch
from hydra.utils import get_original_cwd, to_absolute_path
from nnmnkwii.datasets import FileDataSource, FileSourceDataset, MemoryCacheDataset
from nnsvs.logger import getLogger
from nnsvs.util import MinMaxScaler, StandardScaler, init_seed, pad_2d
from omegaconf import DictConfig, ListConfig, OmegaConf
from torch import nn, optim
from torch.utils import data as data_utils
from torch.utils.tensorboard import SummaryWriter


def log_params_from_omegaconf_dict(params):
    for param_name, element in params.items():
        _explore_recursive(param_name, element)


def _explore_recursive(parent_name, element):
    if isinstance(element, DictConfig):
        for k, v in element.items():
            if isinstance(v, DictConfig) or isinstance(v, ListConfig):
                _explore_recursive(f"{parent_name}.{k}", v)
            else:
                mlflow.log_param(f"{parent_name}.{k}", v)
    elif isinstance(element, ListConfig):
        for i, v in enumerate(element):
            mlflow.log_param(f"{parent_name}.{i}", v)


def num_trainable_params(model):
    """Count the number of trainable parameters in the model.

    Args:
        model (torch.nn.Module): Model to count the number of trainable parameters.

    Returns:
        int: Number of trainable parameters.
    """
    parameters = filter(lambda p: p.requires_grad, model.parameters())
    return sum([np.prod(p.size()) for p in parameters])


class NpyFileSource(FileDataSource):
    def __init__(self, data_root):
        self.data_root = data_root

    def collect_files(self):
        files = sorted(glob(join(self.data_root, "*-feats.npy")))
        return files

    def collect_features(self, path):
        return np.load(path).astype(np.float32)


class Dataset(torch.utils.data.Dataset):
    def __init__(self, X, Y):
        self.X = X
        self.Y = Y

    def __getitem__(self, idx):
        return self.X[idx], self.Y[idx]

    def __len__(self):
        return len(self.X)


def collate_fn(batch):
    """Create batch

    Args:
        batch(tuple): List of tuples
            - x[0] (ndarray,int) : list of (T, D_in)
            - x[1] (ndarray,int) : list of (T, D_out)
    Returns:
        tuple: Tuple of batch
            - x (FloatTensor) : Network inputs (B, max(T), D_in)
            - y (FloatTensor)  : Network targets (B, max(T), D_out)
            - lengths (LongTensor): Input lengths
    """
    lengths = [len(x[0]) for x in batch]
    max_len = max(lengths)
    x_batch = torch.stack([torch.from_numpy(pad_2d(x[0], max_len)) for x in batch])
    y_batch = torch.stack([torch.from_numpy(pad_2d(x[1], max_len)) for x in batch])
    l_batch = torch.tensor(lengths, dtype=torch.long)
    return x_batch, y_batch, l_batch


def get_data_loaders(data_config, collate_fn):
    data_loaders = {}
    for phase in ["train_no_dev", "dev"]:
        in_dir = to_absolute_path(data_config[phase].in_dir)
        out_dir = to_absolute_path(data_config[phase].out_dir)
        train = phase.startswith("train")
        in_feats = FileSourceDataset(NpyFileSource(in_dir))
        out_feats = FileSourceDataset(NpyFileSource(out_dir))

        in_feats = MemoryCacheDataset(in_feats, cache_size=10000)
        out_feats = MemoryCacheDataset(out_feats, cache_size=10000)

        dataset = Dataset(in_feats, out_feats)
        data_loaders[phase] = data_utils.DataLoader(
            dataset,
            batch_size=data_config.batch_size,
            collate_fn=collate_fn,
            pin_memory=data_config.pin_memory,
            num_workers=data_config.num_workers,
            shuffle=train,
        )

    return data_loaders


def save_checkpoint(
    logger,
    out_dir,
    model,
    optimizer,
    lr_scheduler,
    epoch,
    is_best=False,
    postfix="",
):
    if isinstance(model, nn.DataParallel):
        model = model.module

    out_dir.mkdir(parents=True, exist_ok=True)
    if is_best:
        path = out_dir / f"best_loss{postfix}.pth"
    else:
        path = out_dir / "epoch{:04d}{}.pth".format(epoch, postfix)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "lr_scheduler_state": lr_scheduler.state_dict(),
        },
        path,
    )

    logger.info(f"Saved checkpoint at {path}")
    if not is_best:
        shutil.copyfile(path, out_dir / f"latest{postfix}.pth")


def get_stream_weight(stream_weights, stream_sizes):
    if stream_weights is not None:
        assert len(stream_weights) == len(stream_sizes)
        return torch.tensor(stream_weights)

    S = sum(stream_sizes)
    w = torch.tensor(stream_sizes).float() / S
    return w


def _instantiate_optim(optim_config, model):
    # Optimizer
    optimizer_class = getattr(optim, optim_config.optimizer.name)
    optimizer = optimizer_class(model.parameters(), **optim_config.optimizer.params)

    # Scheduler
    lr_scheduler_class = getattr(optim.lr_scheduler, optim_config.lr_scheduler.name)
    lr_scheduler = lr_scheduler_class(optimizer, **optim_config.lr_scheduler.params)

    return optimizer, lr_scheduler


def _resume(logger, resume_config, model, optimizer, lr_scheduler):
    if resume_config.checkpoint is not None and len(resume_config.checkpoint) > 0:
        logger.info("Load weights from %s", resume_config.checkpoint)
        checkpoint = torch.load(to_absolute_path(resume_config.checkpoint))
        model.load_state_dict(checkpoint["state_dict"])
        if resume_config.load_optimizer:
            logger.info("Load optimizer state")
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            lr_scheduler.load_state_dict(checkpoint["lr_scheduler_state"])


def setup(config, device):
    """Setup for training

    Args:
        config (dict): configuration for training
        device (torch.device): device to use for training

    Returns:
        (tuple): tuple containing model, optimizer, learning rate scheduler,
            data loaders, tensorboard writer, and logger.
    """
    logger = getLogger(config.verbose)
    logger.info(OmegaConf.to_yaml(config))

    logger.info(f"PyTorch version: {torch.__version__}")

    if torch.cuda.is_available():
        from torch.backends import cudnn

        cudnn.benchmark = config.train.cudnn.benchmark
        cudnn.deterministic = config.train.cudnn.deterministic
        logger.info(f"cudnn.deterministic: {cudnn.deterministic}")
        logger.info(f"cudnn.benchmark: {cudnn.benchmark}")
        if torch.backends.cudnn.version() is not None:
            logger.info(f"cuDNN version: {torch.backends.cudnn.version()}")

    logger.info(f"Random seed: {config.seed}")
    init_seed(config.seed)

    if config.train.use_detect_anomaly:
        torch.autograd.set_detect_anomaly(True)
        logger.info("Set to use torch.autograd.detect_anomaly")

    # Model
    model = hydra.utils.instantiate(config.model.netG).to(device)
    logger.info(
        "Number of trainable params: {:.3f} million".format(
            num_trainable_params(model) / 1000000.0
        )
    )
    logger.info(model)

    # Optimizer
    optimizer_class = getattr(optim, config.train.optim.optimizer.name)
    optimizer = optimizer_class(
        model.parameters(), **config.train.optim.optimizer.params
    )

    # Scheduler
    lr_scheduler_class = getattr(
        optim.lr_scheduler, config.train.optim.lr_scheduler.name
    )
    lr_scheduler = lr_scheduler_class(
        optimizer, **config.train.optim.lr_scheduler.params
    )

    # DataLoader
    data_loaders = get_data_loaders(config.data, collate_fn)

    # Resume
    if (
        config.train.resume.checkpoint is not None
        and len(config.train.resume.checkpoint) > 0
    ):
        logger.info("Load weights from %s", config.train.resume.checkpoint)
        checkpoint = torch.load(to_absolute_path(config.train.resume.checkpoint))
        model.load_state_dict(checkpoint["state_dict"])
        if config.train.resume.load_optimizer:
            logger.info("Load optimizer state")
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            lr_scheduler.load_state_dict(checkpoint["lr_scheduler_state"])

    if config.data_parallel:
        model = nn.DataParallel(model)

    # Mlflow
    if config.mlflow.enabled:
        mlflow.set_tracking_uri("file://" + get_original_cwd() + "/mlruns")
        mlflow.set_experiment(config.mlflow.experiment)
        # NOTE: disable tensorboard if mlflow is enabled
        writer = None
        logger.info("Using mlflow instead of tensorboard")
    else:
        # Tensorboard
        writer = SummaryWriter(to_absolute_path(config.train.log_dir))

    # Scalers
    if "in_scaler_path" in config.data and config.data.in_scaler_path is not None:
        in_scaler = joblib.load(to_absolute_path(config.data.in_scaler_path))
        in_scaler = MinMaxScaler(
            in_scaler.min_, in_scaler.scale_, in_scaler.data_min_, in_scaler.data_max_
        )
    else:
        in_scaler = None
    if "out_scaler_path" in config.data and config.data.out_scaler_path is not None:
        out_scaler = joblib.load(to_absolute_path(config.data.out_scaler_path))
        out_scaler = StandardScaler(
            out_scaler.mean_, out_scaler.var_, out_scaler.scale_
        )
    else:
        out_scaler = None

    return (
        model,
        optimizer,
        lr_scheduler,
        data_loaders,
        writer,
        logger,
        in_scaler,
        out_scaler,
    )


def setup_gan(config, device):
    """Setup for training GAN

    Args:
        config (dict): configuration for training
        device (torch.device): device to use for training

    Returns:
        (tuple): tuple containing model, optimizer, learning rate scheduler,
            data loaders, tensorboard writer, and logger.
    """
    logger = getLogger(config.verbose)
    logger.info(OmegaConf.to_yaml(config))

    logger.info(f"PyTorch version: {torch.__version__}")

    if torch.cuda.is_available():
        from torch.backends import cudnn

        cudnn.benchmark = config.train.cudnn.benchmark
        cudnn.deterministic = config.train.cudnn.deterministic
        logger.info(f"cudnn.deterministic: {cudnn.deterministic}")
        logger.info(f"cudnn.benchmark: {cudnn.benchmark}")
        if torch.backends.cudnn.version() is not None:
            logger.info(f"cuDNN version: {torch.backends.cudnn.version()}")

    logger.info(f"Random seed: {config.seed}")
    init_seed(config.seed)

    if config.train.use_detect_anomaly:
        torch.autograd.set_detect_anomaly(True)
        logger.info("Set to use torch.autograd.detect_anomaly")

    # Model G
    netG = hydra.utils.instantiate(config.model.netG).to(device)
    logger.info(
        "[Generator] Number of trainable params: {:.3f} million".format(
            num_trainable_params(netG) / 1000000.0
        )
    )
    logger.info(netG)
    # Optimizer and LR scheduler for G
    optG, schedulerG = _instantiate_optim(config.train.optim.netG, netG)

    # Model D
    netD = hydra.utils.instantiate(config.model.netD).to(device)
    logger.info(
        "[Discriminator] Number of trainable params: {:.3f} million".format(
            num_trainable_params(netD) / 1000000.0
        )
    )
    logger.info(netD)
    # Optimizer and LR scheduler for D
    optD, schedulerD = _instantiate_optim(config.train.optim.netD, netD)

    # DataLoader
    data_loaders = get_data_loaders(config.data, collate_fn)

    # Resume
    _resume(logger, config.train.resume.netG, netG, optG, schedulerG)
    _resume(logger, config.train.resume.netD, netD, optD, schedulerD)

    if config.data_parallel:
        netG = nn.DataParallel(netG)
        netD = nn.DataParallel(netD)

    # Mlflow
    if config.mlflow.enabled:
        mlflow.set_tracking_uri("file://" + get_original_cwd() + "/mlruns")
        mlflow.set_experiment(config.mlflow.experiment)
        # NOTE: disable tensorboard if mlflow is enabled
        writer = None
        logger.info("Using mlflow instead of tensorboard")
    else:
        # Tensorboard
        writer = SummaryWriter(to_absolute_path(config.train.log_dir))

    # Scalers
    if "in_scaler_path" in config.data and config.data.in_scaler_path is not None:
        in_scaler = joblib.load(to_absolute_path(config.data.in_scaler_path))
        in_scaler = MinMaxScaler(
            in_scaler.min_, in_scaler.scale_, in_scaler.data_min_, in_scaler.data_max_
        )
    else:
        in_scaler = None
    if "out_scaler_path" in config.data and config.data.out_scaler_path is not None:
        out_scaler = joblib.load(to_absolute_path(config.data.out_scaler_path))
        out_scaler = StandardScaler(
            out_scaler.mean_, out_scaler.var_, out_scaler.scale_
        )
    else:
        out_scaler = None

    return (
        (netG, optG, schedulerG),
        (netD, optD, schedulerD),
        data_loaders,
        writer,
        logger,
        in_scaler,
        out_scaler,
    )


def save_configs(config):
    out_dir = Path(to_absolute_path(config.train.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "model.yaml", "w") as f:
        OmegaConf.save(config.model, f)
    with open(out_dir / "config.yaml", "w") as f:
        OmegaConf.save(config, f)
