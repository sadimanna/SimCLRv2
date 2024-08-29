import math, copy
from typing import List, Tuple

import torch
from pytorch_lightning import LightningModule
from torch import Tensor
from torch.nn import Identity
# from torchvision.models import resnet50, resnet101

from resnetsk import resnet50

from lightly.loss.ntx_ent_loss import NTXentLoss
from lightly.models.modules import SimCLRProjectionHead
from lightly.models import utils
from lightly.models.utils import get_weight_decay_parameters
from lightly.transforms import SimCLRTransform
from lightly.utils.benchmarking import OnlineLinearClassifier
from lightly.utils.lars import LARS
from lightly.utils.scheduler import CosineWarmupScheduler


class SimCLR_v2(LightningModule):
    def __init__(self, batch_size_per_device: int, num_classes: int) -> None:
        super().__init__()
        self.save_hyperparameters()
        self.batch_size_per_device = batch_size_per_device

        resnet = resnet50(skratio=0.0)
        resnet.fc = Identity()  # Ignore classification head
        self.backbone = resnet
        self.projection_head = SimCLRProjectionHead(num_layers=3, 
                                                    output_dim = 512)
        
        self.criterion = NTXentLoss(temperature=0.1, 
                                    gather_distributed=True) #, 
                                    # memory_bank_size = 65536) #min_temp=0.1, max_temp=0.2, memory_bank_size = 65536, gather_distributed=True)

        self.online_classifier = OnlineLinearClassifier(feature_dim = 512, num_classes=num_classes)

    def forward(self, x: Tensor) -> Tensor:
        # print(self.backbone(x).flatten(start_dim=1).shape)
        return self.projection_head(self.backbone(x).flatten(start_dim=1))

    def training_step(
        self, batch: Tuple[List[Tensor], Tensor, List[str]], batch_idx: int
    ) -> Tensor:
        views, targets = batch[0], batch[1]
        features = self.forward(torch.cat(views))#.flatten(start_dim=1)
        # z = self.projection_head(features)
        z0, z1 = features.chunk(len(views))
        loss = self.criterion(z0, z1)
        self.log(
            "train_loss", loss, prog_bar=True, sync_dist=True, batch_size=len(targets)
        )

        cls_loss, cls_log = self.online_classifier.training_step(
            (features.detach(), targets.repeat(len(views))), batch_idx
        )
        cls_loss = cls_loss
        self.log_dict(cls_log, sync_dist=True, batch_size=len(targets))
        return loss + cls_loss

    def validation_step(
        self, batch: Tuple[Tensor, Tensor, List[str]], batch_idx: int
    ) -> Tensor:
        images, targets = batch[0], batch[1]
        features = self.forward(images)#.flatten(start_dim=1)
        cls_loss, cls_log = self.online_classifier.validation_step(
            (features.detach(), targets), batch_idx
        )
        self.log_dict(cls_log, prog_bar=True, sync_dist=True, batch_size=len(targets))
        return cls_loss

    def configure_optimizers(self):
        # Don't use weight decay for batch norm, bias parameters, and classification
        # head to improve performance.
        params, params_no_weight_decay = get_weight_decay_parameters(
            [self.backbone, self.projection_head]
        )
        optimizer = LARS(
            [
                {"name": "simclr", "params": params},
                {
                    "name": "simclr_no_weight_decay",
                    "params": params_no_weight_decay,
                    "weight_decay": 0.0,
                },
                {
                    "name": "online_classifier",
                    "params": self.online_classifier.parameters(),
                    "weight_decay": 0.0,
                },
            ],
            # Square root learning rate scaling improves performance for small
            # batch sizes (<=2048) and few training epochs (<=200). Alternatively,
            # linear scaling can be used for larger batches and longer training:
            #   lr=0.3 * self.batch_size_per_device * self.trainer.world_size / 256
            # See Appendix B.1. in the SimCLR paper https://arxiv.org/abs/2002.05709
            lr=0.1 * math.sqrt(self.batch_size_per_device * self.trainer.world_size),
            momentum=0.9,
            # Note: Paper uses weight decay of 1e-6 but reference code 1e-4. See:
            # https://github.com/google-research/simclr/blob/2fc637bdd6a723130db91b377ac15151e01e4fc2/README.md?plain=1#L103
            weight_decay=1e-6,
        )
        # print(self.trainer.estimated_stepping_batches)
        scheduler = {
            "scheduler": CosineWarmupScheduler(
                optimizer=optimizer,
                warmup_epochs=(
                    self.trainer.estimated_stepping_batches
                    / self.trainer.max_epochs
                    * 10
                ),
                max_epochs=self.trainer.estimated_stepping_batches,
            ),
            "interval": "step",
        }
        return [optimizer], [scheduler]


transform = SimCLRTransform()
