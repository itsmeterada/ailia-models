import sys
import time

import numpy as np
import cv2
from skimage import transform as sk_trans

import ailia

# import original modules
sys.path.append("../../util")
from arg_utils import get_base_parser, update_parser, get_savepath  # noqa
from model_utils import check_and_download_models  # noqa
from detector_utils import load_image  # noqa
from nms_utils import nms_boxes
from math_utils import softmax
from webcamera_utils import get_capture, get_writer  # noqa

from utils_crop import crop_image

# logger
from logging import getLogger  # noqa


# from face_restoration import get_face_landmarks_5
# from face_restoration import align_warp_face, get_inverse_affine
# from face_restoration import paste_faces_to_image

PI = np.pi

logger = getLogger(__name__)

# ======================
# Parameters
# ======================

WEIGHT_DET_PATH = "retinaface_resnet50.onnx"
MODEL_DET_PATH = "retinaface_resnet50.onnx.prototxt"
REMOTE_PATH = "https://storage.googleapis.com/ailia-models/gfpgan/"

REALESRGAN_MODEL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth"

IMAGE_PATH = "s6.jpg"
SAVE_IMAGE_PATH = "output.png"

IMAGE_SIZE = 512

# ======================
# Arguemnt Parser Config
# ======================

parser = get_base_parser("LivePortrait", IMAGE_PATH, SAVE_IMAGE_PATH)
parser.add_argument("--onnx", action="store_true", help="execute onnxruntime version.")
args = update_parser(parser)


# ======================
# Model selection
# ======================

WEIGHT_PATH = ".onnx"
MODEL_PATH = ".onnx.prototxt"


# ======================
# Secondary Functions
# ======================


def distance2bbox(points, distance, max_shape=None):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    if max_shape is not None:
        x1 = x1.clamp(min=0, max=max_shape[1])
        y1 = y1.clamp(min=0, max=max_shape[0])
        x2 = x2.clamp(min=0, max=max_shape[1])
        y2 = y2.clamp(min=0, max=max_shape[0])
    return np.stack([x1, y1, x2, y2], axis=-1)


def distance2kps(points, distance, max_shape=None):
    """Decode distance prediction to bounding box.

    Args:
        points (Tensor): Shape (n, 2), [x, y].
        distance (Tensor): Distance from the given point to 4
            boundaries (left, top, right, bottom).
        max_shape (tuple): Shape of the image.

    Returns:
        Tensor: Decoded bboxes.
    """
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, i % 2] + distance[:, i]
        py = points[:, i % 2 + 1] + distance[:, i + 1]
        if max_shape is not None:
            px = px.clamp(min=0, max=max_shape[1])
            py = py.clamp(min=0, max=max_shape[0])
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1)


def face_align(data, center, output_size, scale, rotation):
    scale_ratio = scale
    rot = float(rotation) * np.pi / 180.0

    t1 = sk_trans.SimilarityTransform(scale=scale_ratio)
    cx = center[0] * scale_ratio
    cy = center[1] * scale_ratio
    t2 = sk_trans.SimilarityTransform(translation=(-1 * cx, -1 * cy))
    t3 = sk_trans.SimilarityTransform(rotation=rot)
    t4 = sk_trans.SimilarityTransform(translation=(output_size / 2, output_size / 2))
    t = t1 + t2 + t3 + t4
    M = t.params[0:2]
    cropped = cv2.warpAffine(data, M, (output_size, output_size), borderValue=0.0)

    return cropped, M


def trans_points2d(pts, M):
    new_pts = np.zeros(shape=pts.shape, dtype=np.float32)
    for i in range(pts.shape[0]):
        pt = pts[i]
        new_pt = np.array([pt[0], pt[1], 1.0], dtype=np.float32)
        new_pt = np.dot(M, new_pt)
        new_pts[i] = new_pt[0:2]

    return new_pts


def calculate_distance_ratio(
    lmk: np.ndarray, idx1: int, idx2: int, idx3: int, idx4: int, eps: float = 1e-6
) -> np.ndarray:
    return np.linalg.norm(lmk[:, idx1] - lmk[:, idx2], axis=1, keepdims=True) / (
        np.linalg.norm(lmk[:, idx3] - lmk[:, idx4], axis=1, keepdims=True) + eps
    )


def get_rotation_matrix(pitch_, yaw_, roll_):
    """the input is in degree"""
    # transform to radian
    pitch = pitch_ / 180 * PI
    yaw = yaw_ / 180 * PI
    roll = roll_ / 180 * PI

    # calculate the euler matrix
    bs = pitch.shape[0]
    ones = np.ones([bs, 1], dtype=np.float32)
    zeros = np.zeros([bs, 1], dtype=np.float32)
    x, y, z = pitch, yaw, roll

    rot_x = np.concatenate(
        [ones, zeros, zeros, zeros, np.cos(x), -np.sin(x), zeros, np.sin(x), np.cos(x)],
        axis=1,
    ).reshape([bs, 3, 3])

    rot_y = np.concatenate(
        [np.cos(y), zeros, np.sin(y), zeros, ones, zeros, -np.sin(y), zeros, np.cos(y)],
        axis=1,
    ).reshape([bs, 3, 3])

    rot_z = np.concatenate(
        [np.cos(z), -np.sin(z), zeros, np.sin(z), np.cos(z), zeros, zeros, zeros, ones],
        axis=1,
    ).reshape([bs, 3, 3])

    rot = rot_z @ rot_y @ rot_x
    return rot.transpose(0, 2, 1)  # transpose


def transform_keypoint(kp_info: dict):
    """
    transform the implicit keypoints with the pose, shift, and expression deformation
    kp: BxNx3
    """
    kp = kp_info["kp"]  # (bs, k, 3)
    pitch, yaw, roll = kp_info["pitch"], kp_info["yaw"], kp_info["roll"]

    t, exp = kp_info["t"], kp_info["exp"]
    scale = kp_info["scale"]

    bs = kp.shape[0]
    num_kp = kp.shape[1]  # Bxnum_kpx3

    rot_mat = get_rotation_matrix(pitch, yaw, roll)  # (bs, 3, 3)

    # Eqn.2: s * (R * x_c,s + exp) + t
    kp_transformed = kp.reshape(bs, num_kp, 3) @ rot_mat + exp.reshape(bs, num_kp, 3)
    kp_transformed *= scale[..., None]  # (bs, k, 3) * (bs, 1, 1) = (bs, k, 3)
    kp_transformed[:, :, 0:2] += t[:, None, 0:2]  # remove z, only apply tx ty

    return kp_transformed


# ======================
# Main functions
# ======================


def get_face_analysis(det_face, landmark):

    def get_landmark(img, face):
        input_size = 192

        bbox = face["bbox"]
        w, h = (bbox[2] - bbox[0]), (bbox[3] - bbox[1])
        center = (bbox[2] + bbox[0]) / 2, (bbox[3] + bbox[1]) / 2
        rotate = 0
        _scale = input_size / (max(w, h) * 1.5)
        aimg, M = face_align(img, center, input_size, _scale, rotate)
        input_size = tuple(aimg.shape[0:2][::-1])

        aimg = aimg.transpose(2, 0, 1)  # HWC -> CHW
        aimg = np.expand_dims(aimg, axis=0)
        aimg = aimg.astype(np.float32)

        # feedforward
        if not args.onnx:
            output = landmark.predict([aimg])
        else:
            output = landmark.run(None, {"data": aimg})
        pred = output[0][0]

        pred = pred.reshape((-1, 2))
        pred[:, 0:2] += 1
        pred[:, 0:2] *= input_size[0] // 2

        IM = cv2.invertAffineTransform(M)
        pred = trans_points2d(pred, IM)

        return pred

    def face_analysis(img):
        input_size = 512

        im_ratio = float(img.shape[0]) / img.shape[1]
        if im_ratio > 1:
            new_height = input_size
            new_width = int(new_height / im_ratio)
        else:
            new_width = input_size
            new_height = int(new_width * im_ratio)
        det_scale = float(new_height) / img.shape[0]
        resized_img = cv2.resize(img, (new_width, new_height))
        det_img = np.zeros((input_size, input_size, 3), dtype=np.uint8)
        det_img[:new_height, :new_width, :] = resized_img

        det_img = (det_img - 127.5) / 128
        det_img = det_img.transpose(2, 0, 1)  # HWC -> CHW
        det_img = np.expand_dims(det_img, axis=0)
        det_img = det_img.astype(np.float32)

        # feedforward
        if not args.onnx:
            output = det_face.predict([det_img])
        else:
            output = det_face.run(None, {"input.1": det_img})

        scores_list = []
        bboxes_list = []
        kpss_list = []

        det_thresh = 0.5
        fmc = 3
        feat_stride_fpn = [8, 16, 32]
        center_cache = {}
        for idx, stride in enumerate(feat_stride_fpn):
            scores = output[idx]
            bbox_preds = output[idx + fmc]
            bbox_preds = bbox_preds * stride
            kps_preds = output[idx + fmc * 2] * stride
            height = input_size // stride
            width = input_size // stride
            K = height * width
            key = (height, width, stride)
            if key in center_cache:
                anchor_centers = center_cache[key]
            else:
                anchor_centers = np.stack(
                    np.mgrid[:height, :width][::-1], axis=-1
                ).astype(np.float32)

                anchor_centers = (anchor_centers * stride).reshape((-1, 2))
                num_anchors = 2
                anchor_centers = np.stack(
                    [anchor_centers] * num_anchors, axis=1
                ).reshape((-1, 2))
                if len(center_cache) < 100:
                    center_cache[key] = anchor_centers

            pos_inds = np.where(scores >= det_thresh)[0]
            bboxes = distance2bbox(anchor_centers, bbox_preds)
            pos_scores = scores[pos_inds]
            pos_bboxes = bboxes[pos_inds]
            scores_list.append(pos_scores)
            bboxes_list.append(pos_bboxes)

            kpss = distance2kps(anchor_centers, kps_preds)
            kpss = kpss.reshape((kpss.shape[0], -1, 2))
            pos_kpss = kpss[pos_inds]
            kpss_list.append(pos_kpss)

        scores = np.vstack(scores_list)
        scores_ravel = scores.ravel()
        order = scores_ravel.argsort()[::-1]
        bboxes = np.vstack(bboxes_list) / det_scale
        kpss = np.vstack(kpss_list) / det_scale
        pre_det = np.hstack((bboxes, scores)).astype(np.float32, copy=False)
        pre_det = pre_det[order, :]

        nms_thresh = 0.4
        keep = nms_boxes(pre_det, [1 for s in pre_det], nms_thresh)
        bboxes = pre_det[keep, :]
        kpss = kpss[order, :, :]
        kpss = kpss[keep, :, :]

        if bboxes.shape[0] == 0:
            return []

        ret = []
        for i in range(bboxes.shape[0]):
            bbox = bboxes[i, 0:4]
            det_score = bboxes[i, 4]
            kps = None
            if kpss is not None:
                kps = kpss[i]
            face = dict(bbox=bbox, kps=kps, det_score=det_score)
            lmk = get_landmark(img, face)
            face["landmark_2d_106"] = lmk

            ret.append(face)

        src_face = sorted(
            ret,
            key=lambda face: (face["bbox"][2] - face["bbox"][0])
            * (face["bbox"][3] - face["bbox"][1]),
            reverse=True,
        )

        return src_face

    return face_analysis


def preprocess(img):
    img = img / 255.0
    img = np.clip(img, 0, 1)  # clip to 0~1
    img = img.transpose(2, 0, 1)  # HxWx3x1 -> 1x3xHxW
    img = np.expand_dims(img, axis=0)
    img = img.astype(np.float32)

    return img


def src_preprocess(img):
    h, w = img.shape[:2]

    # ajust the size of the image according to the maximum dimension
    max_dim = 1280
    if max(h, w) > max_dim:
        if h > w:
            new_h = max_dim
            new_w = int(w * (max_dim / h))
        else:
            new_w = max_dim
            new_h = int(h * (max_dim / w))
        img = cv2.resize(img, (new_w, new_h))

    # ensure that the image dimensions are multiples of n
    division = 2
    new_h = img.shape[0] - (img.shape[0] % division)
    new_w = img.shape[1] - (img.shape[1] % division)

    if new_h == 0 or new_w == 0:
        # when the width or height is less than n, no need to process
        return img

    if new_h != img.shape[0] or new_w != img.shape[1]:
        img = img[:new_h, :new_w]

    return img


def crop_src_image(models, img):
    img = img[:, :, ::-1]  # BGR -> RGB

    face_analysis = models["face_analysis"]
    src_face = face_analysis(img)

    if len(src_face) == 0:
        logger.info("No face detected in the source image.")
        return None
    elif len(src_face) > 1:
        logger.info(f"More than one face detected in the image, only pick one face.")

    src_face = src_face[0]
    lmk = src_face["landmark_2d_106"]  # this is the 106 landmarks from insightface

    # crop the face
    crop_info = crop_image(img, lmk, dsize=512, scale=2.3, vy_ratio=-0.125)

    lmk = landmark_runner(models, img, lmk)

    crop_info["lmk_crop"] = lmk
    crop_info["img_crop_256x256"] = cv2.resize(
        crop_info["img_crop"], (256, 256), interpolation=cv2.INTER_AREA
    )
    crop_info["lmk_crop_256x256"] = crop_info["lmk_crop"] * 256 / 512

    return crop_info


def landmark_runner(models, img, lmk):
    crop_dct = crop_image(img, lmk, dsize=224, scale=1.5, vy_ratio=-0.1)
    img_crop = crop_dct["img_crop"]

    img_crop = img_crop / 255
    img_crop = img_crop.transpose(2, 0, 1)  # HWC -> CHW
    img_crop = np.expand_dims(img_crop, axis=0)
    img_crop = img_crop.astype(np.float32)

    # feedforward
    net = models["landmark_runner"]
    if not args.onnx:
        output = net.predict([img_crop])
    else:
        output = net.run(None, {"input": img_crop})
    out_pts = output[2]

    # 2d landmarks 203 points
    lmk = out_pts[0].reshape(-1, 2) * 224  # scale to 0-224
    # _transform_pts
    M = crop_dct["M_c2o"]
    lmk = lmk @ M[:2, :2].T + M[:2, 2]

    return lmk


def extract_feature_3d(models, x):
    net = models["appearance_feature_extractor"]

    # feedforward
    if not args.onnx:
        output = net.predict([x])
    else:
        output = net.run(None, {"x": x})
    f_s = output[0]
    f_s = f_s.astype(np.float32)

    return f_s


def get_kp_info(models, x):
    net = models["motion_extractor"]

    # feedforward
    if not args.onnx:
        output = net.predict([x])
    else:
        output = net.run(None, {"x": x})
    pitch, yaw, roll, t, exp, scale, kp = output

    kp_info = dict(pitch=pitch, yaw=yaw, roll=roll, t=t, exp=exp, scale=scale, kp=kp)

    pred = softmax(kp_info["pitch"], axis=1)
    degree = np.sum(pred * np.arange(66), axis=1) * 3 - 97.5
    kp_info["pitch"] = degree[:, None]  # Bx1
    pred = softmax(kp_info["yaw"], axis=1)
    degree = np.sum(pred * np.arange(66), axis=1) * 3 - 97.5
    kp_info["yaw"] = degree[:, None]  # Bx1
    pred = softmax(kp_info["roll"], axis=1)
    degree = np.sum(pred * np.arange(66), axis=1) * 3 - 97.5
    kp_info["roll"] = degree[:, None]  # Bx1

    kp_info = {k: v.astype(np.float32) for k, v in kp_info.items()}

    bs = kp_info["kp"].shape[0]
    kp_info["kp"] = kp_info["kp"].reshape(bs, -1, 3)  # BxNx3
    kp_info["exp"] = kp_info["exp"].reshape(bs, -1, 3)  # BxNx3

    return kp_info


def stitching(models, kp_source, kp_driving):
    """conduct the stitching
    kp_source: Bxnum_kpx3
    kp_driving: Bxnum_kpx3
    """

    bs, num_kp = kp_source.shape[:2]

    kp_driving_new = kp_driving

    bs_src = kp_source.shape[0]
    bs_dri = kp_driving.shape[0]
    feat = np.concatenate(
        [kp_source.reshape(bs_src, -1), kp_driving.reshape(bs_dri, -1)], axis=1
    )

    # feedforward
    net = models["stitching"]
    if not args.onnx:
        output = net.predict([feat])
    else:
        output = net.run(None, {"x": feat})
    delta = output[0]

    delta_exp = delta[..., : 3 * num_kp].reshape(bs, num_kp, 3)  # 1x20x3
    delta_tx_ty = delta[..., 3 * num_kp : 3 * num_kp + 2].reshape(bs, 1, 2)  # 1x1x2

    kp_driving_new += delta_exp
    kp_driving_new[..., :2] += delta_tx_ty

    return kp_driving_new


def warp_decode(models, feature_3d, kp_source, kp_driving):
    """get the image after the warping of the implicit keypoints
    feature_3d: Bx32x16x64x64, feature volume
    kp_source: BxNx3
    kp_driving: BxNx3
    """

    # feedforward
    net = models["warping_module"]
    if not args.onnx:
        output = net.predict([feature_3d, kp_source, kp_driving])
    else:
        output = net.run(
            None,
            {
                "feature_3d": feature_3d,
                "kp_source": kp_source,
                "kp_driving": kp_driving,
            },
        )
    out, occlusion_map, deformation = output
    out = out.astype(np.float32)

    # decode
    net = models["spade_generator"]
    if not args.onnx:
        output = net.predict([out])
    else:
        output = net.run(
            None,
            {
                "feature": out,
            },
        )
    out = output[0]

    ret_dct = {
        "out": out.astype(np.float32),
        "occlusion_map": occlusion_map.astype(np.float32),
        "deformation": deformation.astype(np.float32),
    }

    return ret_dct


def predict(models, crop_info, img):
    source_lmk = crop_info["lmk_crop"]
    img_crop, img_crop_256x256 = crop_info["img_crop"], crop_info["img_crop_256x256"]

    return models


def recognize_from_video(models):
    # prepare input data
    img = load_image(IMAGE_PATH)
    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    src_img = src_preprocess(img)
    crop_info = crop_src_image(models, src_img)

    if crop_info is None:
        raise Exception("No face detected in the source image!")

    # prepare_source
    I_s = preprocess(crop_info["img_crop_256x256"])

    x_s_info = get_kp_info(models, I_s)
    x_c_s = x_s_info["kp"]
    R_s = get_rotation_matrix(x_s_info["pitch"], x_s_info["yaw"], x_s_info["roll"])
    f_s = extract_feature_3d(models, I_s)
    x_s = transform_keypoint(x_s_info)

    # video_file = args.video if args.video else args.input[0]
    video_file = "d0.mp4"
    capture = get_capture(video_file)
    assert capture.isOpened(), "Cannot capture source"
    # capture = imageio.get_reader(file_path, "ffmpeg")

    # # create video writer if savepath is specified as video format
    # if args.savepath != SAVE_IMAGE_PATH:
    #     f_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) * args.upscale
    #     f_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) * args.upscale
    #     writer = get_writer(args.savepath, f_h, f_w)
    # else:
    #     writer = None

    face_analysis = models["face_analysis"]
    trajectory_lmk = []

    frame_shown = False
    frame_no = 0
    while True:
        ret, frame = capture.read()
        if (cv2.waitKey(1) & 0xFF == ord("q")) or not ret:
            break
        if frame_shown and cv2.getWindowProperty("frame", cv2.WND_PROP_VISIBLE) == 0:
            break

        img_rgb = frame[:, :, ::-1]  # BGR -> RGB

        # calc_lmks_from_cropped_video
        if frame_no == 0:
            src_face = face_analysis(img_rgb)
            if len(src_face) == 0:
                logger.info(f"No face detected in the frame #{frame_no}")
                raise Exception(f"No face detected in the frame #{frame_no}")
            elif len(src_face) > 1:
                logger.info(
                    f"More than one face detected in the driving frame_{frame_no}, only pick one face."
                )
            src_face = src_face[0]
            lmk = src_face["landmark_2d_106"]
            lmk = landmark_runner(models, img_rgb, lmk)
        else:
            lmk = landmark_runner(models, img_rgb, trajectory_lmk[-1])
        trajectory_lmk.append(lmk)
        driving_rgb_crop_256x256 = cv2.resize(img_rgb, (256, 256))

        # calc_driving_ratio
        lmk = lmk[None]
        c_d_eyes = np.concatenate(
            [
                calculate_distance_ratio(lmk, 6, 18, 0, 12),
                calculate_distance_ratio(lmk, 30, 42, 24, 36),
            ],
            axis=1,
        )
        c_d_lip = calculate_distance_ratio(lmk, 90, 102, 48, 66)

        # prepare_driving_videos
        I_d_i = preprocess(driving_rgb_crop_256x256)

        # collect s_d, R_d, δ_d and t_d for inference
        x_d_i_info = get_kp_info(models, I_d_i)
        R_d_i = get_rotation_matrix(
            x_d_i_info["pitch"], x_d_i_info["yaw"], x_d_i_info["roll"]
        )

        output_fps = 25
        x_d_i_info = motion = {
            "scale": x_d_i_info["scale"].astype(np.float32),
            "R_d": R_d_i.astype(np.float32),
            "exp": x_d_i_info["exp"].astype(np.float32),
            "t": x_d_i_info["t"].astype(np.float32),
        }
        c_d_eyes = c_d_eyes.astype(np.float32)
        c_d_lip = c_d_lip.astype(np.float32)

        if frame_no == 0:
            R_d_0 = R_d_i
            x_d_0_info = x_d_i_info

        R_new = (R_d_i @ R_d_0.transpose(0, 2, 1)) @ R_s
        delta_new = x_s_info["exp"] + (x_d_i_info["exp"] - x_d_0_info["exp"])
        scale_new = x_s_info["scale"] * (x_d_i_info["scale"] / x_d_0_info["scale"])
        t_new = x_s_info["t"] + (x_d_i_info["t"] - x_d_0_info["t"])

        t_new[..., 2] = 0  # zero tz
        x_d_i_new = scale_new * (x_c_s @ R_new + delta_new) + t_new

        x_d_i_new = stitching(models, x_s, x_d_i_new)

        out = warp_decode(models, f_s, x_s, x_d_i_new)

        out = out["out"]
        out = out.transpose(0, 2, 3, 1)  # 1x3xHxW -> 1xHxWx3
        out = np.clip(out, 0, 1)  # clip to 0~1
        out = np.clip(out * 255, 0, 255).astype(np.uint8)  # 0~1 -> 0~255
        I_p_i = out[0]

        # # inference
        # img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # restored_img = predict(models, img)
        # restored_img = cv2.cvtColor(restored_img, cv2.COLOR_RGB2BGR)

        # # show
        # cv2.imshow("frame", restored_img)
        # frame_shown = True

        # # save results
        # if writer is not None:
        #     writer.write(restored_img)

        frame_no += 1

    # capture.release()
    # cv2.destroyAllWindows()
    # if writer is not None:
    #     writer.release()

    logger.info("Script finished successfully.")


def main():
    # # model files check and download
    # check_and_download_models(WEIGHT_PATH, MODEL_PATH, REMOTE_PATH)

    env_id = args.env_id

    # initialize
    if not args.onnx:
        net = ailia.Net(MODEL_PATH, WEIGHT_PATH, env_id=env_id)
    else:
        import onnxruntime

        # init F
        appearance_feature_extractor = onnxruntime.InferenceSession(
            "appearance_feature_extractor.onnx"
        )
        # init M
        motion_extractor = onnxruntime.InferenceSession("motion_extractor.onnx")
        # init W
        warping_module = onnxruntime.InferenceSession("warping_module.onnx")
        # init G
        spade_generator = onnxruntime.InferenceSession("spade_generator.onnx")
        # init S
        stitching = onnxruntime.InferenceSession("stitching.onnx")

        landmark_runner = onnxruntime.InferenceSession("landmark.onnx")

        landmark = onnxruntime.InferenceSession("2d106det.onnx")
        det_face = onnxruntime.InferenceSession("det_10g.onnx")

    face_analysis = get_face_analysis(det_face, landmark)

    models = {
        "appearance_feature_extractor": appearance_feature_extractor,
        "motion_extractor": motion_extractor,
        "warping_module": warping_module,
        "spade_generator": spade_generator,
        "stitching": stitching,
        "landmark_runner": landmark_runner,
        "face_analysis": face_analysis,
    }

    recognize_from_video(models)


if __name__ == "__main__":
    main()
