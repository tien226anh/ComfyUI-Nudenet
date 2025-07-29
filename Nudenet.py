import math
import os
import cv2
import numpy as np
import torch

import folder_paths
from .utils import ModelLoader, overlay, pixelate

if "Nudenet" not in folder_paths.folder_names_and_paths:
    current_paths = [os.path.join(folder_paths.models_dir, "Nudenet")]
else:
    current_paths, _ = folder_paths.folder_names_and_paths["Nudenet"]
folder_paths.supported_pt_extensions.update({".onnx"})
folder_paths.folder_names_and_paths["Nudenet"] = (
    current_paths,
    folder_paths.supported_pt_extensions,
)

CENSOR_METHODS = [
    "pixelate",
    "blur",
    "gaussian_blur",
    "image",
]

CLASSIDS_LABELS_MAPPING = {
    0: "FEMALE_FACE",
    1: "MALE_FACE",
    2: "FEMALE_GENITALIA_COVERED",
    3: "FEMALE_GENITALIA_EXPOSED",
    4: "BUTTOCKS_COVERED",
    5: "BUTTOCKS_EXPOSED",
    6: "FEMALE_BREAST_COVERED",
    7: "FEMALE_BREAST_EXPOSED",
    8: "MALE_BREAST_EXPOSED",
    9: "ARMPITS_EXPOSED",
    10: "BELLY_EXPOSED",
    11: "MALE_GENITALIA_EXPOSED",
    12: "ANUS_EXPOSED",
    13: "FEET_COVERED",
    14: "FEET_EXPOSED",
    15: "EYE",
}

LABELS_CLASSIDS_MAPPING = labels_classids_mapping = {
    label: class_id for class_id, label in CLASSIDS_LABELS_MAPPING.items()
}

BLOCK_COUNT_BEHAVIOURS = [
    "fixed",
    "fewer_when_small",
    "fewer_when_large",
]


def read_image(img, target_size=320):
    assert isinstance(img, np.ndarray)

    img_height, img_width = img.shape[:2]
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    aspect = img_width / img_height

    if img_height > img_width:
        new_height = target_size
        new_width = int(round(target_size * aspect))
    else:
        new_width = target_size
        new_height = int(round(target_size / aspect))

    resize_factor = math.sqrt(
        (img_width**2 + img_height**2) / (new_width**2 + new_height**2)
    )
    img = cv2.resize(img, (new_width, new_height))

    pad_x = target_size - new_width
    pad_y = target_size - new_height
    pad_top = pad_y // 2
    pad_bottom = pad_y - pad_top
    pad_left = pad_x // 2
    pad_right = pad_x - pad_left

    img = cv2.copyMakeBorder(
        img,
        pad_top,
        pad_bottom,
        pad_left,
        pad_right,
        cv2.BORDER_CONSTANT,
        value=[0, 0, 0],
    )
    img = cv2.resize(img, (target_size, target_size))

    image_data = img.astype("float32")
    image_data = np.transpose(img, (2, 0, 1))
    image_data = np.expand_dims(image_data, axis=0)
    return image_data, resize_factor, pad_left, pad_top


def postprocess(output, resize_factor, pad_left, pad_top, min_score):
    outputs = np.transpose(np.squeeze(output[0]))
    rows = outputs.shape[0]
    boxes = []
    scores = []
    class_ids = []
    for i in range(rows):
        classes_scores = outputs[i][4:]
        max_score = np.amax(classes_scores)
        if max_score >= min_score:
            class_id = np.argmax(classes_scores)
            x, y, w, h = outputs[i][0], outputs[i][1], outputs[i][2], outputs[i][3]
            left = int(round((x - w * 0.5 - pad_left) * resize_factor))
            top = int(round((y - h * 0.5 - pad_top) * resize_factor))
            width = int(round(w * resize_factor))
            height = int(round(h * resize_factor))
            class_ids.append(class_id)
            scores.append(max_score)
            boxes.append([left, top, width, height])
    indices = cv2.dnn.NMSBoxes(boxes, scores, 0.25, 0.45)
    return [
        {
            "id": class_ids[i],
            "score": round(float(scores[i]), 2),
            "box": boxes[i],
        }
        for i in indices
    ]


def nudenet_execute(
    nudenet_model: ModelLoader,
    input_images: torch.Tensor,
    censor_method: str,
    filtered_labels: list,
    min_score: float,
    blocks: float,
    block_count_scaling: str,
    overlay_image: torch.Tensor,
    overlay_strength: float,
    alpha_mask: torch.Tensor,
):
    input_images = input_images.clone()

    assert isinstance(
        input_images, torch.Tensor
    ), "input_images must be a torch.Tensor but got %s" % type(input_images)
    assert input_images.dim() == 4, (
        "input_images must be a 4D tensor but got %s" % input_images.dim()
    )

    batch_size = input_images.shape[0]
    output_images = []

    if censor_method == "image":
        assert overlay_image is not None, "overlay_image must be provided"
        assert alpha_mask is not None, "alpha_mask must be provided"
        assert isinstance(
            overlay_image, torch.Tensor
        ), "overlay_image must be a torch.Tensor"
        assert isinstance(alpha_mask, torch.Tensor), "alpha_mask must be a torch.Tensor"
        assert overlay_image.dim() == 4, "overlay_image must be a 4D tensor"

        overlay_image = overlay_image[0].cpu().numpy()
        alpha_mask = (
            alpha_mask.reshape((alpha_mask.shape[-2], alpha_mask.shape[-1]))
            .cpu()
            .numpy()
        )

    for i in range(batch_size):
        image = input_images[i].cpu().numpy()
        preprocessed_image, resize_factor, pad_left, pad_top = read_image(
            image, nudenet_model["input_width"]
        )
        outputs = nudenet_model["session"].run(
            None, {nudenet_model["input_name"]: preprocessed_image}
        )
        detections = postprocess(outputs, resize_factor, pad_left, pad_top, min_score)
        censored = [d for d in detections if d.get("id") not in filtered_labels]

        if block_count_scaling == "fixed":
            scaled_blocks = blocks

        for d in censored:
            box = d["box"]
            x, y, w, h = box[0], box[1], box[2], box[3]
            area = image[y : y + h, x : x + w]

            if block_count_scaling != "fixed":
                d_pct = max(h / image.shape[:2][0], w / image.shape[:2][1])
                if block_count_scaling == "fewer_when_large":
                    scaled_blocks = int(blocks + d_pct * (1 - blocks))
                else:  # elif block_count_scaling == "fewer_when_small"
                    scaled_blocks = int(1 + d_pct * (blocks - 1))

            if censor_method == "pixelate":
                image[y : y + h, x : x + w] = pixelate(area, blocks=scaled_blocks)
            elif censor_method == "blur":
                image[y : y + h, x : x + w] = cv2.blur(
                    area, (scaled_blocks, scaled_blocks)
                )
            elif censor_method == "gaussian_blur":
                image[y : y + h, x : x + w] = cv2.GaussianBlur(area, (h, h), 0)
            elif censor_method == "image":
                pasty = cv2.resize(overlay_image, (w, h))
                alpha_mask = cv2.resize(alpha_mask, (w, h))
                image = overlay(image, pasty, alpha_mask, x, y, overlay_strength)

        output_images.append(torch.from_numpy(image).unsqueeze(0))

    return torch.cat(output_images, dim=0)


class ApplyNudenet:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "nudenet_model": ("NUDENET_MODEL",),
                "image": ("IMAGE",),
                "censor_method": (CENSOR_METHODS,),
                "filtered_labels": ("FILTERED_LABELS",),
                "min_score": (
                    "FLOAT",
                    {"default": 0.2, "min": 0.0, "max": 1.0, "step": 0.01},
                ),
                "blocks": ("INT", {"default": 3, "min": 1, "max": 100, "step": 1}),
                "block_count_scaling": (
                    BLOCK_COUNT_BEHAVIOURS,
                    {
                        "tooltip": "Scale block count by censor area. Only affects pixelate censor."
                    },
                ),
            },
            "optional": {
                "overlay_image": ("IMAGE",),
                "overlay_strength": (
                    "FLOAT",
                    {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.1},
                ),
                "alpha_mask": ("MASK",),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply_nudenet"
    CATEGORY = "Nudenet"

    def apply_nudenet(
        self,
        nudenet_model,
        image,
        filtered_labels,
        censor_method,
        min_score,
        blocks,
        block_count_scaling,
        overlay_image: torch.Tensor = None,
        overlay_strength: float = 1.0,
        alpha_mask: torch.Tensor = None,
    ):
        output_image = nudenet_execute(
            nudenet_model=nudenet_model,
            input_images=image,
            censor_method=censor_method,
            filtered_labels=filtered_labels,
            min_score=min_score,
            blocks=blocks,
            block_count_scaling=block_count_scaling,
            overlay_image=overlay_image,
            overlay_strength=overlay_strength,
            alpha_mask=alpha_mask,
        )
        return (output_image,)


class FilteredLabel:

    @classmethod
    def INPUT_TYPES(cls):
        """
        Define the input types dynamically based on LABELS_MAPPING.

        Returns:
            dict: A dictionary of required input types with default values.
        """
        return {
            "required": {
                key: ("BOOLEAN", {"default": True})
                for key in LABELS_CLASSIDS_MAPPING.keys()
            }
        }

    RETURN_TYPES = ("FILTERED_LABELS",)
    FUNCTION = "filter_labels"
    CATEGORY = "Nudenet"

    def filter_labels(self, *args, **kwags):
        white_list_class_ids = []
        for label, is_filter in kwags.items():
            if not is_filter:
                white_list_class_ids.append(LABELS_CLASSIDS_MAPPING[label])
        return (white_list_class_ids,)


class NudenetModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (folder_paths.get_filename_list("Nudenet"),),
            }
        }

    RETURN_TYPES = ("NUDENET_MODEL",)
    FUNCTION = "load_model"
    CATEGORY = "Nudenet"

    def load_model(self, model, providers="CPU"):
        import onnxruntime
        from onnxruntime.capi import _pybind_state as C

        self.session = onnxruntime.InferenceSession(
            os.path.join(current_paths[0], model), providers=C.get_available_providers()
        )
        model_inputs = self.session.get_inputs()
        self.input_width = model_inputs[0].shape[2]
        self.input_height = model_inputs[0].shape[3]
        self.input_name = model_inputs[0].name

        return (
            ModelLoader(
                session=self.session,
                input_width=self.input_width,
                input_height=self.input_height,
                input_name=self.input_name,
            ),
        )


"""
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 Register
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""
NODE_CLASS_MAPPINGS = {
    # Main Apply Nodes
    "ApplyNudenet": ApplyNudenet,
    # Loaders
    "NudenetModelLoader": NudenetModelLoader,
    # Helpers
    "FilterdLabel": FilteredLabel,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    # Main Apply Nodes
    "ApplyNudenet": "Apply Nudenet",
    # Loaders
    "NudenetModelLoader": "Nudenet Model Loader",
    # Helpers
    "FilterdLabel": "Filtered Labels",
}
