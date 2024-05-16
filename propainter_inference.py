import numpy as np
import torch
from tqdm import tqdm

from .model.modules.flow_comp_raft import RAFT_bi
from .model.recurrent_flow_completion import RecurrentFlowCompleteNet
from .model.propainter import InpaintGenerator

from numpy.typing import NDArray


def get_ref_index(
    mid_neighbor_id: int,
    neighbor_ids: list[int],
    length: int,
    ref_stride: int = 10,
    ref_num: int = -1,
) -> list[int]:
    """Calculate reference indices for frames based on the provided parameters."""
    ref_index = []
    if ref_num == -1:
        for i in range(0, length, ref_stride):
            if i not in neighbor_ids:
                ref_index.append(i)
    else:
        start_idx = max(0, mid_neighbor_id - ref_stride * (ref_num // 2))
        end_idx = min(length, mid_neighbor_id + ref_stride * (ref_num // 2))
        for i in range(start_idx, end_idx, ref_stride):
            if i not in neighbor_ids:
                if len(ref_index) > ref_num:
                    break
                ref_index.append(i)
    return ref_index


def compute_flow(
    raft_model: RAFT_bi, frames: torch.Tensor, raft_iter: int, video_length: int
) -> tuple[torch.Tensor, torch.Tensor]:
    """Compute forward and backward optical flows using the RAFT model."""
    if frames.size(dim=-1) <= 640:
        short_clip_len = 12
    elif frames.size(dim=-1) <= 720:
        short_clip_len = 8
    elif frames.size(dim=-1) <= 1280:
        short_clip_len = 4
    else:
        short_clip_len = 2

    # use fp32 for RAFT
    if frames.size(dim=1) > short_clip_len:
        gt_flows_f_list, gt_flows_b_list = [], []
        for chunck in range(0, video_length, short_clip_len):
            end_f = min(video_length, chunck + short_clip_len)
            if chunck == 0:
                flows_f, flows_b = raft_model(frames[:, chunck:end_f], iters=raft_iter)
            else:
                flows_f, flows_b = raft_model(
                    frames[:, chunck - 1 : end_f], iters=raft_iter
                )

            gt_flows_f_list.append(flows_f)
            gt_flows_b_list.append(flows_b)
            torch.cuda.empty_cache()

        gt_flows_f = torch.cat(gt_flows_f_list, dim=1)
        gt_flows_b = torch.cat(gt_flows_b_list, dim=1)
        gt_flows_bi = (gt_flows_f, gt_flows_b)
    else:
        gt_flows_bi = raft_model(frames, iters=raft_iter)
        torch.cuda.empty_cache()

    return gt_flows_bi


def complete_flow(
    recurrent_flow_model: RecurrentFlowCompleteNet,
    flows_tuple: tuple[torch.Tensor, torch.Tensor],
    flow_masks: torch.Tensor,
    subvideo_length: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Complete and refine optical flows using a recurrent flow completion model.

    This function processes optical flows in chunks if the total length exceeds the specified
    subvideo length. It uses a recurrent model to complete and refine the flows, combining
    forward and backward flows into bidirectional flows.
    """
    flow_length = flows_tuple[0].size(dim=1)
    if flow_length > subvideo_length:
        pred_flows_f, pred_flows_b = [], []
        pad_len = 5
        for f in range(0, flow_length, subvideo_length):
            s_f = max(0, f - pad_len)
            e_f = min(flow_length, f + subvideo_length + pad_len)
            pad_len_s = max(0, f) - s_f
            pad_len_e = e_f - min(flow_length, f + subvideo_length)
            pred_flows_bi_sub, _ = recurrent_flow_model.forward_bidirect_flow(
                (flows_tuple[0][:, s_f:e_f], flows_tuple[1][:, s_f:e_f]),
                flow_masks[:, s_f : e_f + 1],
            )
            pred_flows_bi_sub = recurrent_flow_model.combine_flow(
                (flows_tuple[0][:, s_f:e_f], flows_tuple[1][:, s_f:e_f]),
                pred_flows_bi_sub,
                flow_masks[:, s_f : e_f + 1],
            )

            pred_flows_f.append(
                pred_flows_bi_sub[0][:, pad_len_s : e_f - s_f - pad_len_e]
            )
            pred_flows_b.append(
                pred_flows_bi_sub[1][:, pad_len_s : e_f - s_f - pad_len_e]
            )
            torch.cuda.empty_cache()

        pred_flows_f = torch.cat(pred_flows_f, dim=1)
        pred_flows_b = torch.cat(pred_flows_b, dim=1)

        pred_flows_bi = (pred_flows_f, pred_flows_b)

    else:
        pred_flows_bi, _ = recurrent_flow_model.forward_bidirect_flow(
            flows_tuple, flow_masks
        )
        pred_flows_bi = recurrent_flow_model.combine_flow(
            flows_tuple, pred_flows_bi, flow_masks
        )

        torch.cuda.empty_cache()

    return pred_flows_bi


def image_propagation(
    inpaint_model: InpaintGenerator,
    frames: torch.Tensor,
    masks_dilated: torch.Tensor,
    prediction_flows: tuple[torch.Tensor, torch.Tensor],
    video_length: int,
    subvideo_length: int,
    process_size: tuple[int, int],
) -> tuple[torch.Tensor, torch.Tensor]:
    """Propagate inpainted images across video frames.

    If the video length exceeds a defined threshold, the process is segmented and handled in chunks.
    """
    process_width, process_height = process_size
    masked_frames = frames * (1 - masks_dilated)
    subvideo_length_img_prop = min(
        100, subvideo_length
    )  # ensure a minimum of 100 frames for image propagation
    if video_length > subvideo_length_img_prop:
        updated_frames, updated_masks = [], []
        pad_len = 10
        for f in range(0, video_length, subvideo_length_img_prop):
            s_f = max(0, f - pad_len)
            e_f = min(video_length, f + subvideo_length_img_prop + pad_len)
            pad_len_s = max(0, f) - s_f
            pad_len_e = e_f - min(video_length, f + subvideo_length_img_prop)
            b, t, _, _, _ = masks_dilated[:, s_f:e_f].size()
            pred_flows_bi_sub = (
                prediction_flows[0][:, s_f : e_f - 1],
                prediction_flows[1][:, s_f : e_f - 1],
            )
            prop_imgs_sub, updated_local_masks_sub = inpaint_model.img_propagation(
                masked_frames[:, s_f:e_f],
                pred_flows_bi_sub,
                masks_dilated[:, s_f:e_f],
                "nearest",
            )
            updated_frames_sub = (
                frames[:, s_f:e_f] * (1 - masks_dilated[:, s_f:e_f])
                + prop_imgs_sub.view(b, t, 3, process_height, process_width)
                * masks_dilated[:, s_f:e_f]
            )
            updated_masks_sub = updated_local_masks_sub.view(
                b, t, 1, process_height, process_width
            )

            updated_frames.append(
                updated_frames_sub[:, pad_len_s : e_f - s_f - pad_len_e]
            )
            updated_masks.append(
                updated_masks_sub[:, pad_len_s : e_f - s_f - pad_len_e]
            )
            torch.cuda.empty_cache()

        updated_frames = torch.cat(updated_frames, dim=1)
        updated_masks = torch.cat(updated_masks, dim=1)
    else:
        b, t, _, _, _ = masks_dilated.size()
        prop_imgs, updated_local_masks = inpaint_model.img_propagation(
            masked_frames, prediction_flows, masks_dilated, "nearest"
        )
        updated_frames = (
            frames * (1 - masks_dilated)
            + prop_imgs.view(b, t, 3, process_height, process_width) * masks_dilated
        )
        updated_masks = updated_local_masks.view(b, t, 1, process_height, process_width)
        torch.cuda.empty_cache()

    return updated_frames, updated_masks


def feature_propagation(
    inpaint_model: InpaintGenerator,
    updated_frames: torch.Tensor,
    updated_masks: torch.Tensor,
    masks_dilated: torch.Tensor,
    prediction_flows: tuple[torch.Tensor, torch.Tensor],
    original_frames: NDArray,
    video_length: int,
    subvideo_length: int,
    neighbor_length: int,
    ref_stride: int,
    process_size: tuple[int, int],
) -> list[NDArray]:
    """Propagate inpainted features across video frames.

    The process is segmented and handled in chunks if the video length exceeds a defined threshold.
    """
    process_width, process_height = process_size

    comp_frames = [None] * video_length

    neighbor_stride = neighbor_length // 2
    ref_num = subvideo_length // ref_stride if video_length > subvideo_length else -1

    for f in tqdm(range(0, video_length, neighbor_stride)):
        neighbor_ids = list(
            range(
                max(0, f - neighbor_stride), min(video_length, f + neighbor_stride + 1)
            )
        )
        ref_ids = get_ref_index(f, neighbor_ids, video_length, ref_stride, ref_num)
        selected_imgs = updated_frames[:, neighbor_ids + ref_ids, :, :, :]
        selected_masks = masks_dilated[:, neighbor_ids + ref_ids, :, :, :]
        selected_update_masks = updated_masks[:, neighbor_ids + ref_ids, :, :, :]
        selected_pred_flows_bi = (
            prediction_flows[0][:, neighbor_ids[:-1], :, :, :],
            prediction_flows[1][:, neighbor_ids[:-1], :, :, :],
        )

        with torch.no_grad():
            # 1.0 indicates mask
            l_t = len(neighbor_ids)

            pred_img = inpaint_model(
                selected_imgs,
                selected_pred_flows_bi,
                selected_masks,
                selected_update_masks,
                l_t,
            )

            pred_img = pred_img.view(-1, 3, process_height, process_width)

            pred_img = (pred_img + 1) / 2
            pred_img = pred_img.cpu().permute(0, 2, 3, 1).numpy() * 255
            binary_masks = (
                masks_dilated[0, neighbor_ids, :, :, :]
                .cpu()
                .permute(0, 2, 3, 1)
                .numpy()
                .astype(np.uint8)
            )
            for i in range(len(neighbor_ids)):
                idx = neighbor_ids[i]
                img = np.array(pred_img[i]).astype(np.uint8) * binary_masks[
                    i
                ] + original_frames[idx] * (1 - binary_masks[i])
                if comp_frames[idx] is None:
                    comp_frames[idx] = img
                else:
                    comp_frames[idx] = (
                        comp_frames[idx].astype(np.float32) * 0.5
                        + img.astype(np.float32) * 0.5
                    )

                comp_frames[idx] = comp_frames[idx].astype(np.uint8)

        torch.cuda.empty_cache()

    return comp_frames
