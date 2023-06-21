import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from trailmet.algorithms.distill.distill import Distillation
from trailmet.algorithms.distill.losses import KDTransferLoss

import logging
from datetime import datetime
from tqdm import tqdm
import wandb
import pandas as pd
import numpy as np
import os
import time

from trailmet.utils import AverageMeter, accuracy, save_checkpoint, seed_everything

logger = logging.getLogger(__name__)

# Hinton's Knowledge Distillation
seed_everything(43)


class KDTransfer(Distillation):
    """class to compress model using distillation via kd transfer"""

    def __init__(self, teacher_model, student_model, dataloaders, **kwargs):
        super(KDTransfer, self).__init__(**kwargs)
        self.teacher_model = teacher_model
        self.student_model = student_model
        self.dataloaders = dataloaders
        self.kwargs = kwargs
        self.lambda_ = self.kwargs["DISTILL_ARGS"].get("LAMBDA", 0.5)
        self.temperature = self.kwargs["DISTILL_ARGS"].get("TEMPERATURE", 5)
        self.ce_loss = nn.CrossEntropyLoss()
        self.kd_loss = KDTransferLoss(self.temperature, "batchmean")

        self.epochs = kwargs["DISTILL_ARGS"].get("EPOCHS", 200)
        self.lr = kwargs["DISTILL_ARGS"].get("LR", 0.1)

        self.wandb_monitor = self.kwargs.get("wandb", "False")
        self.dataset_name = dataloaders["train"].dataset.__class__.__name__
        self.save = "./checkpoints/"

        self.name = "_".join(
            [
                self.dataset_name,
                f"{self.epochs}",
                f"{self.lr}",
                datetime.now().strftime("%b-%d_%H:%M:%S"),
            ]
        )

        os.makedirs(f"{os.getcwd()}/logs/Response_KD", exist_ok=True)
        os.makedirs(self.save, exist_ok=True)
        self.logger_file = f"{os.getcwd()}/logs/Response_KD/{self.name}.log"

        logging.basicConfig(
            filename=self.logger_file,
            format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
            datefmt="%m/%d/%Y %H:%M:%S",
            level=logging.INFO,
        )

        logger.info(f"Experiment Arguments: {self.kwargs}")

        if self.wandb_monitor:
            wandb.init(project="Trailmet Response_KD", name=self.name)
            wandb.config.update(self.kwargs)

    def compress_model(self):
        """function to transfer knowledge from teacher to student"""
        # include teacher training options
        self.distill(
            self.teacher_model,
            self.student_model,
            self.dataloaders,
            **self.kwargs["DISTILL_ARGS"],
        )

    def distill(self, teacher_model, student_model, dataloaders, **kwargs):
        print("=====> TRAINING STUDENT NETWORK <=====")
        logger.info("=====> TRAINING STUDENT NETWORK <=====")
        test_only = kwargs.get("TEST_ONLY", False)
        lr = kwargs.get("LR", 0.1)
        weight_decay = kwargs.get("WEIGHT_DECAY", 0.0005)

        optimizer = torch.optim.SGD(
            student_model.parameters(),
            lr=lr,
            weight_decay=weight_decay,
            momentum=0.9,
            nesterov=True,
        )

        milestones = kwargs.get("MILESTONES", [60, 120, 160])
        gamma = kwargs.get("GAMMA", 0.2)

        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, milestones=milestones, gamma=gamma, verbose=False
        )

        criterion = self.criterion
        best_top1_acc = 0

        if test_only == False:
            epochs_list = []
            val_top1_acc_list = []
            val_top5_acc_list = []
            for epoch in range(self.epochs):
                t_loss = self.train_one_epoch(
                    teacher_model,
                    student_model,
                    dataloaders["train"],
                    criterion,
                    optimizer,
                    epoch,
                )

                valid_loss, valid_top1_acc, valid_top5_acc = self.test(
                    teacher_model, student_model, dataloaders["val"], criterion, epoch
                )

                # use conditions for different schedulers e.g. ReduceLROnPlateau needs scheduler.step(v_loss)
                scheduler.step()

                is_best = False
                if valid_top1_acc > best_top1_acc:
                    best_top1_acc = valid_top1_acc
                    is_best = True

                save_checkpoint(
                    {
                        "epoch": epoch,
                        "state_dict": student_model.state_dict(),
                        "best_top1_acc": best_top1_acc,
                        "optimizer": optimizer.state_dict(),
                        "scheduler": scheduler.state_dict(),
                    },
                    is_best,
                    self.save,
                )

                if self.wandb_monitor:
                    wandb.log({"best_top1_acc": best_top1_acc})

                epochs_list.append(epoch)
                val_top1_acc_list.append(valid_top1_acc.cpu().numpy())
                val_top5_acc_list.append(valid_top5_acc.cpu().numpy())

                df_data = np.array(
                    [
                        epochs_list,
                        val_top1_acc_list,
                        val_top5_acc_list,
                    ]
                ).T
                df = pd.DataFrame(
                    df_data,
                    columns=[
                        "Epochs",
                        "Validation Top1",
                        "Validation Top5",
                    ],
                )
                df.to_csv(
                    f"{os.getcwd()}/logs/Response_KD/{self.name}.csv", index=False
                )

    def train_one_epoch(
        self, teacher_model, student_model, dataloader, loss_fn, optimizer, epoch
    ):
        teacher_model.eval()
        student_model.train()

        batch_time = AverageMeter("Time", ":6.3f")
        data_time = AverageMeter("Data", ":6.3f")
        losses = AverageMeter("Loss", ":.4e")

        end = time.time()

        epoch_iterator = tqdm(
            dataloader,
            desc="Training student network Epoch [X] (X / X Steps) (batch time=X.Xs) (data time=X.Xs) (loss=X.X)",
            bar_format="{l_bar}{r_bar}",
            dynamic_ncols=True,
            disable=False,
        )

        for i, (images, labels) in enumerate(epoch_iterator):
            data_time.update(time.time() - end)
            images = images.to(self.device, dtype=torch.float)
            labels = labels.to(self.device)

            teacher_preds = teacher_model(images)
            student_preds = student_model(images)
            loss = loss_fn(teacher_preds, student_preds, labels)
            n = images.size(0)
            losses.update(loss.item(), n)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            batch_time.update(time.time() - end)
            end = time.time()

            epoch_iterator.set_description(
                "Training student network Epoch [%d] (%d / %d Steps) (batch time=%2.5fs) (data time=%2.5fs) (loss=%2.5f)"
                % (
                    epoch,
                    (i + 1),
                    len(dataloader),
                    batch_time.val,
                    data_time.val,
                    losses.val,
                )
            )

            logger.info(
                "Training student network Epoch [%d] (%d / %d Steps) (batch time=%2.5fs) (data time=%2.5fs) (loss=%2.5f)"
                % (
                    epoch,
                    (i + 1),
                    len(dataloader),
                    batch_time.val,
                    data_time.val,
                    losses.val,
                )
            )

            if self.wandb_monitor:
                wandb.log(
                    {
                        "train_loss": losses.val,
                    }
                )

        return losses.avg

    def test(self, teacher_model, student_model, dataloader, loss_fn, epoch):
        batch_time = AverageMeter("Time", ":6.3f")
        losses = AverageMeter("Loss", ":.4e")
        top1 = AverageMeter("Acc@1", ":6.2f")
        top5 = AverageMeter("Acc@5", ":6.2f")

        epoch_iterator = tqdm(
            dataloader,
            desc="Validating student network Epoch [X] (X / X Steps) (batch time=X.Xs) (loss=X.X) (top1=X.X) (top5=X.X)",
            bar_format="{l_bar}{r_bar}",
            dynamic_ncols=True,
            disable=False,
        )

        teacher_model.eval()
        student_model.eval()

        with torch.no_grad():
            end = time.time()

            for i, (images, labels) in enumerate(epoch_iterator):
                images = images.to(self.device, dtype=torch.float)
                labels = labels.to(self.device)

                teacher_preds = teacher_model(images)
                student_preds = student_model(images)
                loss = loss_fn(teacher_preds, student_preds, labels)

                pred1, pred5 = accuracy(student_preds, labels, topk=(1, 5))

                n = images.size(0)
                losses.update(loss.item(), n)
                top1.update(pred1[0], n)
                top5.update(pred5[0], n)

                # measure elapsed time
                batch_time.update(time.time() - end)
                end = time.time()

                epoch_iterator.set_description(
                    "Validating student network Epoch [%d] (%d / %d Steps) (batch time=%2.5fs) (loss=%2.5f) (top1=%2.5f) (top5=%2.5f)"
                    % (
                        epoch,
                        (i + 1),
                        len(dataloader),
                        batch_time.val,
                        losses.val,
                        top1.val,
                        top5.val,
                    )
                )

                logger.info(
                    "Validating student network Epoch [%d] (%d / %d Steps) (batch time=%2.5fs) (loss=%2.5f) (top1=%2.5f) (top5=%2.5f)"
                    % (
                        epoch,
                        (i + 1),
                        len(dataloader),
                        batch_time.val,
                        losses.val,
                        top1.val,
                        top5.val,
                    )
                )

                if self.wandb_monitor:
                    wandb.log(
                        {
                            "val_loss": losses.val,
                            "val_top1_acc": top1.val,
                            "val_top5_acc": top5.val,
                        }
                    )

            print(
                " * acc@1 {top1.avg:.3f} acc@5 {top5.avg:.3f}".format(
                    top1=top1, top5=top5
                )
            )
        return losses.avg, top1.avg, top5.avg

    def criterion(self, out_t, out_s, labels):
        ce_loss = self.ce_loss(out_s, labels)
        kd_loss = self.kd_loss(out_t, out_s)
        return (
            self.lambda_ * ce_loss
            + (1 - self.lambda_) * (self.temperature**2) * kd_loss
        )
