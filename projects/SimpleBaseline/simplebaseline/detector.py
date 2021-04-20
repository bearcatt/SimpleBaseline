# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import torch
from torch import nn

from detectron2.modeling import (
    META_ARCH_REGISTRY,
    build_backbone,
    detector_postprocess
)
from detectron2.structures import Boxes, ImageList, Instances

from .decoder import Decoder
from .loss import SetCriterion


@META_ARCH_REGISTRY.register()
class SimpleBaseline(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.device = torch.device(cfg.MODEL.DEVICE)
        self.in_features = cfg.MODEL.ROI_HEADS.IN_FEATURES
        self.num_classes = cfg.MODEL.SimpleBaseline.NUM_CLASSES
        self.num_queries = cfg.MODEL.SimpleBaseline.NUM_QUERIES
        self.hidden_dim = cfg.MODEL.SimpleBaseline.HIDDEN_DIM

        self.backbone = build_backbone(cfg)
        self.queries = nn.Embedding(self.num_queries, self.hidden_dim)
        self.decoder = Decoder(cfg, roi_input_shape=self.backbone.output_shape())

        self.criterion = SetCriterion(cfg=cfg)

        mu = torch.Tensor(cfg.MODEL.PIXEL_MEAN).to(self.device).view(3, 1, 1)
        sigma = torch.Tensor(cfg.MODEL.PIXEL_STD).to(self.device).view(3, 1, 1)
        self.normalizer = lambda x: (x - mu) / sigma

    def forward(self, batched_inputs):
        images, images_xywh = self.preprocess_image(batched_inputs)

        src = self.backbone(images.tensor)
        features = list()
        for f in self.in_features:
            feature = src[f]
            features.append(feature)

        init_boxes = images_xywh[:, None, :].repeat(1, self.num_queries, 1)
        output = self.decoder(features, init_boxes, self.queries.weight)

        if self.training:
            targets = [x["instances"].to(self.device) for x in batched_inputs]
            targets = self.prepare_targets(targets)
            loss_dict = self.criterion(output, targets)
            return loss_dict
        else:
            box_logits = output[-1]["pred_logits"]
            box_preds = output[-1]["pred_boxes"]
            results = self.inference(box_logits, box_preds, images.image_sizes)

            processed_results = []
            for results_per_image, input_per_image, image_size in zip(
                    results, batched_inputs, images.image_sizes):
                height = input_per_image.get("height", image_size[0])
                width = input_per_image.get("width", image_size[1])
                r = detector_postprocess(results_per_image, height, width)
                processed_results.append({"instances": r})

            return processed_results

    def prepare_targets(self, targets):
        new_targets = []
        for target in targets:
            h, w = target.image_size
            image_size = torch.as_tensor([w, h, w, h], dtype=torch.float)
            new_targets.append({
                "image_size": image_size.unsqueeze(0).to(self.device),
                "labels": target.gt_classes.to(self.device),
                "boxes": target.gt_boxes.tensor.to(self.device)
            })
        return new_targets

    def inference(self, box_logits, box_preds, image_sizes):
        assert len(box_logits) == len(image_sizes)
        results = []

        scores = torch.sigmoid(box_logits)
        labels = torch.arange(self.num_classes, device=self.device).\
                    unsqueeze(0).repeat(self.num_queries, 1).flatten(0, 1)

        for i, (score, box_pred, image_size) in enumerate(
                zip(scores, box_preds, image_sizes)):
            result = Instances(image_size)
            score, topk_indices = score.flatten(0, 1).topk(100, sorted=False)
            result.scores = score

            labels_per_image = labels[topk_indices]
            result.pred_classes = labels_per_image

            box_pred = box_pred.view(-1, 1, 4).repeat(1, self.num_classes, 1)
            box_pred = box_pred.view(-1, 4)[topk_indices]
            result.pred_boxes = Boxes(box_pred)
            results.append(result)

        return results

    def preprocess_image(self, inputs):
        """Normalize, pad and batch the input images.
        """
        images = [self.normalizer(x["image"].to(self.device)) for x in inputs]
        images = ImageList.from_tensors(images, self.backbone.size_divisibility)

        images_xywh = list()
        for item in inputs:
            h, w = item["image"].shape[-2:]
            images_xywh.append(torch.tensor([0, 0, w, h], dtype=torch.float32))
        images_xywh = torch.stack(images_xywh).to(self.device)

        return images, images_xywh