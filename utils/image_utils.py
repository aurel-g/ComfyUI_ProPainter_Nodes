import torch
import scipy
import numpy as np
from PIL import Image
from torchvision import transforms
from torchvision.transforms.functional import to_pil_image

from numpy.typing import NDArray

# For Debugging
import cv2
import os
from icecream import ic



class Stack(object):
    def __init__(self, roll=False):
        self.roll = roll

    def __call__(self, img_group) -> NDArray:
        mode = img_group[0].mode
        if mode == '1':
            img_group = [img.convert('L') for img in img_group]
            mode = 'L'
        if mode == 'L':
            return np.stack([np.expand_dims(x, 2) for x in img_group], axis=2)
        elif mode == 'RGB':
            if self.roll:
                return np.stack([np.array(x)[:, :, ::-1] for x in img_group],
                                axis=2)
            else:
                return np.stack(img_group, axis=2)
        else:
            raise NotImplementedError(f"Image mode {mode}")


class ToTorchFormatTensor(object):
    """ Converts a PIL.Image (RGB) or numpy.ndarray (H x W x C) in the range [0, 255]
    to a torch.FloatTensor of shape (C x H x W) in the range [0.0, 1.0] """
    # TODO: Check how this function is working with the comfy workflow.
    def __init__(self, div=True):
        self.div = div

    def __call__(self, pic) -> torch.Tensor:
        if isinstance(pic, np.ndarray):
            # numpy img: [L, C, H, W]
            img = torch.from_numpy(pic).permute(2, 3, 0, 1).contiguous()
        else:
            # handle PIL Image
            img = torch.ByteTensor(torch.ByteStorage.from_buffer(
                pic.tobytes()))
            img = img.view(pic.size[1], pic.size[0], len(pic.mode))
            # put it from HWC to CHW format
            # yikes, this transpose takes 80% of the loading time/CPU
            img = img.transpose(0, 1).transpose(0, 2).contiguous()
        img = img.float().div(255) if self.div else img.float()
        return img


def resize_images(images: list[Image.Image], 
                  input_size: tuple[int, int], 
                  output_size: tuple[int, int]) -> tuple[list[Image.Image], tuple[int, int]]:
    """
    Resizes each image in the list to a new size divisible by 8.

    Returns:
        A list of resized images with dimensions divisible by 8 and process size.
    """    
    process_size = (output_size[0]-output_size[0]%8, output_size[1]-output_size[1]%8)
    ic(process_size)
    
    if process_size != input_size:
        images = [f.resize(process_size) for f in images]

    return images, process_size


def convert_image_to_frames(images: torch.Tensor) -> list[Image.Image]:
    """
    Convert a batch of PyTorch tensors into a list of PIL Image frames 
    
    Args:
    images (torch.Tensor): A batch of images represented as tensors.

    Returns:
    List[Image]: A list of images converted to PIL 
    """
    frames = []
    for image in images:
        torch_frame = image.detach().cpu()
        np_frame = torch_frame.numpy()
        np_frame = (np_frame * 255).clip(0, 255).astype(np.uint8)
        frame = Image.fromarray(np_frame)
        frames.append(frame)
    
    # For Debbuging
    save_root = "custom_nodes/ComfyUI-ProPainter-Nodes/results"
    for i, mask in enumerate(frames):
        mask.save(os.path.join(save_root, 'test_pil_frames', f"pil_frame_{i}.png"))
    
    return frames


def binary_mask(mask: np.ndarray, 
                th: float = 0.1) -> np.ndarray:
    mask[mask>th] = 1
    mask[mask<=th] = 0
    
    return mask


def convert_mask_to_frames(images: torch.Tensor) -> list[Image.Image]:
    """
    Convert a batch of PyTorch tensors into a list of PIL Image frames 
    
    Args:
    images (torch.Tensor): A batch of images represented as tensors.

    Returns:
    List[Image.Image]: A list of images converted to PIL 
    """
    frames = []
    for image in images:        
        image = image.detach().cpu()

        # Adjust the scaling based on the data type
        if image.dtype == torch.float32:
            image = (image * 255).clamp(0, 255).byte()

        frame: Image.Image = to_pil_image(image)
        frames.append(frame)
    
    # For Debugging
    save_root = "custom_nodes/ComfyUI-ProPainter-Nodes/results"
    for i, mask in enumerate(frames):
        mask.save(os.path.join(save_root, 'test_pil_masks', f"pil_mask_{i}.png"))
    
    return frames


def read_masks(masks: torch.Tensor, 
               input_size: tuple[int, int], 
               output_size: tuple[int, int], 
               length, 
               flow_mask_dilates=8, 
               mask_dilates=5) -> tuple[list[Image.Image], list[Image.Image]]:
    """
    TODO: Docstring.
    """
    mask_imgs = convert_mask_to_frames(masks)
    mask_imgs, _ = resize_images(mask_imgs, input_size, output_size)
    masks_dilated = []
    flow_masks = []

    for mask_img in mask_imgs:
        mask_img = np.array(mask_img.convert('L'))

        # Dilate 8 pixel so that all known pixel is trustworthy
        if flow_mask_dilates > 0:
            flow_mask_img = scipy.ndimage.binary_dilation(mask_img, iterations=flow_mask_dilates).astype(np.uint8)
        else:
            flow_mask_img = binary_mask(mask_img).astype(np.uint8)
        flow_masks.append(Image.fromarray(flow_mask_img * 255))
        
        if mask_dilates > 0:
            mask_img = scipy.ndimage.binary_dilation(mask_img, iterations=mask_dilates).astype(np.uint8)
        else:
            mask_img = binary_mask(mask_img).astype(np.uint8)
        masks_dilated.append(Image.fromarray(mask_img * 255))
    
    if len(mask_imgs) == 1:
        flow_masks = flow_masks * length
        masks_dilated = masks_dilated * length

    # For Debugging
    save_root = "custom_nodes/ComfyUI-ProPainter-Nodes/results"
    for i, mask in enumerate(flow_masks):
        mask.save(os.path.join(save_root, 'mask_frames', f"flow_mask_{i}.png"))

    return flow_masks, masks_dilated


def to_tensors():
    return transforms.Compose([Stack(), ToTorchFormatTensor()])


# For debugging only
def imwrite(img, file_path, params=None, auto_mkdir=True):
    """Write image to file.

    Args:
        img (ndarray): Image array to be written.
        file_path (str): Image file path.
        params (None or list): Same as opencv's :func:`imwrite` interface.
        auto_mkdir (bool): If the parent folder of `file_path` does not exist,
            whether to create it automatically.

    Returns:
        bool: Successful or not.
    """
    if auto_mkdir:
        dir_name = os.path.abspath(os.path.dirname(file_path))
        os.makedirs(dir_name, exist_ok=True)
    return cv2.imwrite(file_path, img, params)