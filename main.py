from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

import os
# os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:1024"
import barlowtwins
import byol
import dcl
import dclw
import dino
import finetune_eval
import knn_eval
import linear_eval
import transfer_tasks
import simclr, simclrv2
import directclr
import simclr_vanilla
import simclr_encoder
import swav
import moco
import torch
torch.cuda.empty_cache()
import vicreg
import tssimclr, tsdcl
import miov3, miov2, miov1
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import (
    DeviceStatsMonitor,
    EarlyStopping,
    LearningRateMonitor,
)
from pytorch_lightning.loggers import TensorBoardLogger
from torch.utils.data import DataLoader
from torchvision import transforms as T

from lightly.data import LightlyDataset
from lightly.transforms.utils import IMAGENET_NORMALIZE
from lightly.utils.benchmarking import MetricCallback
from lightly.utils.dist import print_rank_zero

parser = ArgumentParser("ImageNet ResNet50 Benchmarks")
parser.add_argument("--train-dir", type=Path, default="C:/Users/ISI_UTS/Siladittya/iclr2023/inexpts/datasets/ImageNet100/train")
parser.add_argument("--val-dir", type=Path, default="C:/Users/ISI_UTS/Siladittya/iclr2023/inexpts/datasets/ImageNet100/val")
parser.add_argument("--log-dir", type=Path, default="C:/Users/ISI_UTS/Siladittya/iclr2023/inexpts/benchmark_logs_im100_r50")
parser.add_argument("--transfer-dir", type=Path, default="/datasets/")
parser.add_argument("--batch-size-per-device", type=int, default=256)
parser.add_argument("--epochs", type=int, default=100)
parser.add_argument("--num-workers", type=int, default=1)
parser.add_argument("--accelerator", type=str, default="gpu")
parser.add_argument("--devices", type=int, default=1)
parser.add_argument("--precision", type=str, default="bf16")
parser.add_argument("--ckpt-path", type=Path, default=None)
parser.add_argument("--compile-model", action="store_true")
parser.add_argument("--methods", type=str, nargs="+")
parser.add_argument("--num-classes", type=int, default=100)
parser.add_argument("--skip-knn-eval", action="store_true")
parser.add_argument("--skip-linear-eval", action="store_true")
parser.add_argument("--skip-finetune-eval", action="store_false")
parser.add_argument("--skip-transfer-tasks", action="store_false")

METHODS = {
    "barlowtwins": {
        "model": barlowtwins.BarlowTwins,
        "transform": barlowtwins.transform,
    },
    "byol": {"model": byol.BYOL, "transform": byol.transform},
    "dcl": {"model": dcl.DCL, "transform": dcl.transform},
    "dclw": {"model": dclw.DCLW, "transform": dclw.transform},
    "dino": {"model": dino.DINO, "transform": dino.transform},
    "simclr": {"model": simclr_vanilla.SimCLR, "transform": simclr.transform},
    "simclrv2": {"model": simclrv2.SimCLR_v2, "transform": simclr.transform},
    "directclr": {"model": directclr.DirectCLR, "transform": simclr.transform},
    "swav": {"model": swav.SwAV, "transform": swav.transform},
    "tssimclr": {"model": tssimclr.TSSimCLR, "transform": simclr.transform},
    "tsdcl": {"model": tsdcl.TSDCL, "transform": tsdcl.transform},
    "miov3": {"model": miov3.MIOv3, "transform":miov3.transform},
    "miov2": {"model": miov2.MIOv2, "transform":miov2.transform},
    "miov1": {"model": miov1.MIOv1, "transform":miov1.transform},
    "vicreg": {"model": vicreg.VICReg, "transform": vicreg.transform},
    "moco": {"model":moco.MoCo, "transform":moco.transform}
}


def main(
    train_dir: Path,
    val_dir: Path,
    log_dir: Path,
    transfer_dir: Path,
    batch_size_per_device: int,
    epochs: int,
    num_workers: int,
    accelerator: str,
    devices: int,
    precision: str,
    compile_model: bool,
    methods: Union[Sequence[str], None],
    num_classes: int,
    skip_knn_eval: bool,
    skip_linear_eval: bool,
    skip_finetune_eval: bool,
    skip_transfer_tasks: bool,
    ckpt_path: str = None,
) -> None:
    torch.set_float32_matmul_precision("high")

    method_names = methods or METHODS.keys()

    for method in method_names:
        method_dir = (
            log_dir / method / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ).resolve()
        model = METHODS[method]["model"](
            batch_size_per_device=batch_size_per_device, num_classes=num_classes
        )

        if compile_model and hasattr(torch, "compile"):
            # Compile model if PyTorch supports it.
            print_rank_zero("Compiling model...")
            model = torch.compile(model)

        if epochs <= 0:
            print_rank_zero("Epochs <= 0, skipping pretraining.")
            if ckpt_path is not None:
                model.load_state_dict(torch.load(ckpt_path)['state_dict'])
        else:
            pretrain(
                model=model,
                method=method,
                train_dir=train_dir,
                val_dir=val_dir,
                log_dir=method_dir,
                batch_size_per_device=batch_size_per_device,
                epochs=epochs, # - end_epoch, #state_dict['epochs'],
                num_workers=num_workers,
                accelerator=accelerator,
                devices=devices,
                precision=precision,
                ckpt_path=ckpt_path,
            )

        if skip_knn_eval:
            print_rank_zero("Skipping KNN eval.")
        else:
            knn_eval.knn_eval(
                model=model,
                num_classes=num_classes,
                train_dir=train_dir,
                val_dir=val_dir,
                log_dir=method_dir,
                batch_size_per_device=batch_size_per_device,
                num_workers=num_workers,
                accelerator=accelerator,
                devices=devices,
            )

        if skip_linear_eval:
            print_rank_zero("Skipping linear eval.")
        else:
            linear_eval.linear_eval(
                model=model,
                num_classes=num_classes,
                train_dir=train_dir,
                val_dir=val_dir,
                log_dir=method_dir,
                batch_size_per_device=batch_size_per_device,
                num_workers=num_workers,
                accelerator=accelerator,
                devices=devices,
                precision=precision,
            )

        if skip_finetune_eval:
            print_rank_zero("Skipping fine-tune eval.")
        else:
            finetune_eval.finetune_eval(
                model=model,
                num_classes=num_classes,
                train_dir=train_dir,
                val_dir=val_dir,
                log_dir=method_dir,
                batch_size_per_device=batch_size_per_device,
                num_workers=num_workers,
                accelerator=accelerator,
                devices=devices,
                precision=precision,
            )
        
        if skip_transfer_tasks:
            print_rank_zero("Skipping transfer tasks.")
        else:
            transfer_tasks.evaluate(
                model = model,
                transfer_dir = transfer_dir,
                log_dir=method_dir,
                batch_size_per_device=batch_size_per_device,
                num_workers=num_workers,
                accelerator=accelerator,
                devices=devices,
                precision=precision,
            )


def pretrain(
    model: LightningModule,
    method: str,
    train_dir: Path,
    val_dir: Path,
    log_dir: Path,
    batch_size_per_device: int,
    epochs: int,
    num_workers: int,
    accelerator: str,
    devices: int,
    precision: str,
    ckpt_path: str = None,
    # callbacks = None,
) -> None:
    print_rank_zero(f"Running pretraining for {method}...")

    # Setup training data.
    train_transform = METHODS[method]["transform"]
    train_dataset = LightlyDataset(input_dir=str(train_dir), transform=train_transform)
    # print(train_dataset.__len__())
    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size_per_device,
        shuffle=True,
        num_workers=num_workers,
        drop_last=True,
        persistent_workers=True,
    )

    # Setup validation data.
    val_transform = T.Compose(
        [
            T.Resize(256),
            T.CenterCrop(224),
            T.ToTensor(),
            T.Normalize(mean=IMAGENET_NORMALIZE["mean"], std=IMAGENET_NORMALIZE["std"]),
        ]
    )
    val_dataset = LightlyDataset(input_dir=str(val_dir), transform=val_transform)
    val_dataloader = DataLoader(
        val_dataset,
        batch_size=batch_size_per_device,
        shuffle=False,
        num_workers=num_workers,
        persistent_workers=True,
    )

    # Train model.
    metric_callback = MetricCallback()
    trainer = Trainer(
        max_epochs=epochs,
        accelerator=accelerator,
        devices=devices,
        callbacks=[
            LearningRateMonitor(),
            # Stop if training loss diverges.
            EarlyStopping(monitor="train_loss", patience=int(1e12), check_finite=True),
            DeviceStatsMonitor(),
            metric_callback,
        ],
        logger=TensorBoardLogger(save_dir=str(log_dir), name="pretrain_simclr"),
        precision=precision,
        strategy="auto", #ddp", #_find_unused_parameters_true", #"auto", #ddp", #_find_unused_parameters_true",
        sync_batchnorm=True,
        # find_unused_parameters = False,
        # resume_from_checkpoint = ckpt_path,
    )

    trainer.fit(
        model=model,
        train_dataloaders=train_dataloader,
        val_dataloaders=val_dataloader,
        ckpt_path=ckpt_path,
    )
    for metric in ["val_online_cls_top1", "val_online_cls_top5"]:
        print_rank_zero(f"max {metric}: {max(metric_callback.val_metrics[metric])}")


if __name__ == "__main__":
    args = parser.parse_args()
    main(**vars(args))
