import warnings
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from ._functional import soft_jaccard_score, to_tensor
from .reductions import LossReduction


class JaccardLoss(nn.Module):
    def __init__(
            self,
            classes: Optional[list[int]] = None,
            log_loss: bool = False,
            from_logits: bool = True,
            mask_to_one_hot: bool = False,
            reduction: str = 'mean',
            smooth: float = 0.0,
            ignore_index: Optional[int] = None,
            eps: float = 1e-7,
    ):
        """Jaccard loss for image segmentation task.
        It supports binary, multiclass and multilabel cases.
        Ground truth masks should have shape (B, C, H, W) for multiclass and multilabel cases
        or (B, 1, H, W) for binary case. For the multiclass case, the ground truth mask can also
        have shape (B, 1, H, W) but you should set mask_to_one_hot = True.

        Args:
            - classes:  List of classes that contribute in loss computation.
                By default, all channels are included.
            - log_loss: If True, loss computed as `- log(jaccard_coeff)`,
                otherwise `1 - jaccard_coeff`
            - from_logits: If True, assumes input is raw logits.
            - mask_to_one_hot: if set to True, the mask is converted into one-hot format.
            - reduction: select the reduction to be applied to the loss.
            - smooth: Smoothness constant for dice coefficient
            - ignore_index: Label that indicates ignored pixels
                (does not contribute to loss)
            - eps: A small epsilon for numerical stability to avoid zero division error
                (denominator will be always greater or equal to eps)

        Shape
             - **y_pred** - torch.Tensor of shape (B, C, H, W),
             - **y_true** - torch.Tensor of shape (B, C, H, W) or (B, 1, H, W),
        where C is the number of classes.

        Reference
            https://docs.monai.io/en/stable/_modules/monai/losses/dice.html#DiceLoss
        """
        if reduction not in LossReduction.available_reductions():
            raise ValueError(f'Unsupported reduction: {reduction}, '
                             f'available options are {LossReduction.available_reductions()}.')
        super().__init__()

        self.classes = classes
        self.from_logits = from_logits
        self.mask_to_one_hot = mask_to_one_hot
        self.reduction = reduction
        self.smooth = smooth
        self.eps = eps
        self.log_loss = log_loss
        self.ignore_index = ignore_index

    def forward(self, y_pred: torch.Tensor, y_true: torch.Tensor) -> torch.Tensor:

        batch_size = y_pred.shape[0]
        num_classes = y_pred.shape[1]
        spatial_dims: list[int] = torch.arange(2, len(y_pred.shape)).tolist()

        if self.classes is not None:
            if num_classes == 1:
                warnings.warn("Single channel prediction, masking classes is not supported for Binary Segmentation")
            else:
                self.classes = to_tensor(self.classes, dtype = torch.long)

        if self.from_logits:
            # Convert logits to class probabilities using Sigmoid for Binary Case
            # and Softmax for multiclass/multilabels cases.
            # Using log-exp formulation as it is more numerically stable
            # and does not cause vanishing gradient.
            if num_classes == 1:
                y_pred = F.logsigmoid(y_pred).exp()
            else:
                y_pred = F.log_softmax(y_pred, dim = 1).exp()

        if self.mask_to_one_hot:
            # Convert y_true to one_hot representation to compute DiceLoss
            if num_classes == 1:
                warnings.warn("Single channel prediction, 'mask_to_one_hot = True' ignored.")
            else:
                # maybe there is a better way to handle this?
                permute_dims = tuple(dim - 1 for dim in spatial_dims)
                y_true = F.one_hot(y_true, num_classes).squeeze(dim = 1) # N, 1, H, W, ... ---> N, H, W, ..., C
                y_true = y_true.permute(0, -1, *permute_dims) # N, 1, H, W, ..., C ---> N, C, H, W, ...

        if y_true.shape != y_pred.shape:
            raise AssertionError(f"Ground truth has different shape ({y_true.shape})"
                                 f" from predicted mask ({y_pred.shape})")

        # Only reduce spatial dimensions
        scores = soft_jaccard_score(y_pred,
                                    y_true.type_as(y_pred),
                                    smooth = self.smooth,
                                    eps = self.eps,
                                    dims = spatial_dims)

        if self.log_loss:
            loss = -torch.log(scores.clamp_min(self.eps))
        else:
            loss = 1.0 - scores

        # IoU loss is defined for non-empty classes
        # So we zero contribution of channel that does not have true pixels
        # NOTE: A better workaround would be to use loss term `mean(y_pred)`
        # for this case, however it will be a modified jaccard loss
        ### same as dice loss, should we remove this?
        # to delete?
        # dims = tuple(d for d in range(len(y_true.shape)) if d != 1)
        # mask = y_true.sum(dims) > 0
        # loss *= mask.to(loss.dtype)

        if self.classes is not None:
            loss = loss[:, self.classes, :]

        if self.reduction == LossReduction.MEAN:
            loss = torch.mean(loss)
        elif self.reduction == LossReduction.SUM:
            loss = torch.sum(loss)
        elif self.reduction == LossReduction.NONE:
            broadcast_shape = list(loss.shape[0:2]) + [1] * (len(y_true.shape) - 2)
            loss = loss.view(broadcast_shape)

        return loss