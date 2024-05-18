from dataclasses import dataclass, field

import numpy as np
import scipy
import torch
from numpy.typing import NDArray
from PIL import Image
from torchvision import transforms
from torchvision.transforms.functional import to_pil_image


@dataclass
class ImageConfig:
    width: int
    height: int
    mask_dilates: int
    flow_mask_dilates: int
    input_size: tuple[int, int]
    video_length: int
    process_size: int = field(init=False)

    def __post_init__(self) -> None:
        """Initialize output size."""
        # self.output_size = (self.width, self.height)
        self.process_size = (
            self.width - self.width % 8,
            self.height - self.height % 8,
        )


class Stack:
    """Stack images based on number of channels."""

    def __init__(self, roll=False):
        self.roll = roll

    def __call__(self, img_group) -> NDArray:
        mode = img_group[0].mode
        if mode == "1":
            img_group = [img.convert("L") for img in img_group]
            mode = "L"
        if mode == "L":
            return np.stack([np.expand_dims(x, 2) for x in img_group], axis=2)
        if mode == "RGB":
            if self.roll:
                return np.stack([np.array(x)[:, :, ::-1] for x in img_group], axis=2)
            return np.stack(img_group, axis=2)
        raise NotImplementedError(f"Image mode {mode}")


class ToTorchFormatTensor:
    """Converts a PIL.Image (RGB) or numpy.ndarray (H x W x C) in the range [0, 255] to a torch.FloatTensor of shape (C x H x W) in the range [0.0, 1.0]."""

    # TODO: Check if this function is necessary with comfyUI workflow.
    def __init__(self, div=True):
        self.div = div

    def __call__(self, pic) -> torch.Tensor:
        if isinstance(pic, np.ndarray):
            # numpy img: [L, C, H, W]
            img = torch.from_numpy(pic).permute(2, 3, 0, 1).contiguous()
        else:
            # handle PIL Image
            img = torch.ByteTensor(torch.ByteStorage.from_buffer(pic.tobytes()))
            img = img.view(pic.size[1], pic.size[0], len(pic.mode))
            # put it from HWC to CHW format
            # yikes, this transpose takes 80% of the loading time/CPU
            img = img.transpose(0, 1).transpose(0, 2).contiguous()
        img = img.float().div(255) if self.div else img.float()
        return img


def resize_images(images: list[Image.Image], config: ImageConfig) -> list[Image.Image]:
    """Resizes each image in the list to a new size divisible by 8."""
    if config.process_size != config.input_size:
        images = [f.resize(config.process_size) for f in images]

    return images


def convert_image_to_frames(images: torch.Tensor) -> list[Image.Image]:
    """Convert a batch of PyTorch tensors into a list of PIL Image frames."""
    frames = []
    for image in images:
        torch_frame = image.detach().cpu()
        np_frame = torch_frame.numpy()
        np_frame = (np_frame * 255).clip(0, 255).astype(np.uint8)
        frame = Image.fromarray(np_frame)
        frames.append(frame)

    return frames


def binary_mask(mask: np.ndarray, th: float = 0.1) -> np.ndarray:
    mask[mask > th] = 1
    mask[mask <= th] = 0

    return mask


def extrapolation(frames: list[Image.Image], config: ImageConfig):
    """Prepares the data for video outpainting."""
    nFrame = config.video_length
    imgW, imgH = config.input_size

    # Defines new FOV.
    imgH_extr = int(config.height_scale * imgH)
    imgW_extr = int(config.width_scale * imgW)
    imgH_extr = imgH_extr - imgH_extr % 8
    imgW_extr = imgW_extr - imgW_extr % 8
    H_start = int((imgH_extr - imgH) / 2)
    W_start = int((imgW_extr - imgW) / 2)

    # Extrapolates the FOV for video.
    frames = []
    for v in frames:
        frame = np.zeros(((imgH_extr, imgW_extr, 3)), dtype=np.uint8)
        frame[H_start : H_start + imgH, W_start : W_start + imgW, :] = v
        frames.append(Image.fromarray(frame))

    # Generates the mask for missing region.
    masks_dilated = []
    flow_masks = []

    dilate_h = 4 if H_start > 10 else 0
    dilate_w = 4 if W_start > 10 else 0
    mask = np.ones(((imgH_extr, imgW_extr)), dtype=np.uint8)

    mask[
        H_start + dilate_h : H_start + imgH - dilate_h,
        W_start + dilate_w : W_start + imgW - dilate_w,
    ] = 0
    flow_masks.append(Image.fromarray(mask * 255))

    mask[H_start : H_start + imgH, W_start : W_start + imgW] = 0
    masks_dilated.append(Image.fromarray(mask * 255))

    flow_masks = flow_masks * nFrame
    masks_dilated = masks_dilated * nFrame
    config.output_size(imgW_extr, imgH_extr)
    return (
        frames,
        flow_masks,
        masks_dilated,
    )


def convert_mask_to_frames(images: torch.Tensor) -> list[Image.Image]:
    """Convert a batch of PyTorch tensors into a list of PIL Image frames."""
    frames = []
    for image in images:
        image = image.detach().cpu()

        # Adjust the scaling based on the data type
        if image.dtype == torch.float32:
            image = (image * 255).clamp(0, 255).byte()

        frame: Image.Image = to_pil_image(image)
        frames.append(frame)

    return frames


def read_masks(
    masks: torch.Tensor, config: ImageConfig
) -> tuple[list[Image.Image], list[Image.Image]]:
    """TODO: Docstring."""
    mask_images = convert_mask_to_frames(masks)
    mask_images = resize_images(mask_images, config)
    masks_dilated: list[Image.Image] = []
    flow_masks: list[Image.Image] = []

    for mask_image in mask_images:
        mask_array = np.array(mask_image.convert("L"))

        # Dilate 8 pixel so that all known pixel is trustworthy
        if config.flow_mask_dilates > 0:
            flow_mask_img = scipy.ndimage.binary_dilation(
                mask_array, iterations=config.flow_mask_dilates
            ).astype(np.uint8)
        else:
            flow_mask_img = binary_mask(mask_array).astype(np.uint8)
        flow_masks.append(Image.fromarray(flow_mask_img * 255))

        if config.mask_dilates > 0:
            mask_array = scipy.ndimage.binary_dilation(
                mask_array, iterations=config.mask_dilates
            ).astype(np.uint8)
        else:
            mask_array = binary_mask(mask_array).astype(np.uint8)
        masks_dilated.append(Image.fromarray(mask_array * 255))

    if len(mask_images) == 1:
        flow_masks = flow_masks * config.video_length
        masks_dilated = masks_dilated * config.video_length

    return flow_masks, masks_dilated


def to_tensors():
    return transforms.Compose([Stack(), ToTorchFormatTensor()])


def prepare_frames_and_masks(
    frames: list[Image.Image],
    mask: torch.Tensor,
    config: ImageConfig,
    device: torch.device,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, list[NDArray]]:
    frames = resize_images(frames, config)

    flow_masks, masks_dilated = read_masks(mask, config)

    # original_frames = [np.array(f).astype(np.uint8) for f in frames]
    original_frames = [np.array(f) for f in frames]
    frames_tensor: torch.Tensor = to_tensors()(frames).unsqueeze(0) * 2 - 1
    flow_masks_tensor: torch.Tensor = to_tensors()(flow_masks).unsqueeze(0)
    masks_dilated_tensor: torch.Tensor = to_tensors()(masks_dilated).unsqueeze(0)
    frames_tensor, flow_masks_tensor, masks_dilated_tensor = (
        frames_tensor.to(device),
        flow_masks_tensor.to(device),
        masks_dilated_tensor.to(device),
    )
    return frames_tensor, flow_masks_tensor, masks_dilated_tensor, original_frames


def handle_output(
    composed_frames: list[NDArray],
    flow_masks: torch.Tensor,
    masks_dilated: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    output_frames = [
        torch.from_numpy(frame.astype(np.float32) / 255.0) for frame in composed_frames
    ]

    output_images = torch.stack(output_frames)

    output_flow_masks = flow_masks.squeeze()
    output_masks_dilated = masks_dilated.squeeze()

    return output_images, output_flow_masks, output_masks_dilated
