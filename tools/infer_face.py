import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

_ARGPARSE_CN_MAP = {
    "usage: ": "用法: ",
    "positional arguments": "位置参数",
    "options": "可选参数",
    "show this help message and exit": "显示帮助并退出",
}


def _argparse_cn(text: str) -> str:
    return _ARGPARSE_CN_MAP.get(text, text)


argparse._ = _argparse_cn


def _to_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    reference = (
        args.reference
        or args.ref
        or args.image1
        or args.source
        or args.reference_image_path
    )
    probe = (
        args.probe or args.verify or args.image2 or args.target or args.probe_image_path
    )

    if not reference and args.positional:
        reference = args.positional[0]
    if not probe and args.positional and len(args.positional) > 1:
        probe = args.positional[1]

    if not reference or not probe:
        raise RuntimeError("缺少图片路径，请提供参考图和待验证图")

    ref_path = Path(reference).resolve()
    probe_path = Path(probe).resolve()
    if not ref_path.exists() or not ref_path.is_file():
        raise RuntimeError(f"参考图不存在: {ref_path}")
    if not probe_path.exists() or not probe_path.is_file():
        raise RuntimeError(f"待验证图不存在: {probe_path}")
    return ref_path, probe_path


def _load_image(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图片: {path}")
    return image


def _detect_largest_face(image: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        return None
    face_cascade = cv2.CascadeClassifier(str(cascade_path))
    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=4, minSize=(48, 48)
    )
    if faces is None or len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda box: int(box[2]) * int(box[3]))
    return int(x), int(y), int(w), int(h)


def _crop_face_or_full(image: np.ndarray) -> np.ndarray:
    face_box = _detect_largest_face(image)
    if not face_box:
        return image
    x, y, w, h = face_box
    return image[y : y + h, x : x + w]


def _preprocess(gray_image: np.ndarray) -> np.ndarray:
    resized = cv2.resize(gray_image, (160, 160))
    return resized


def _orb_similarity(img1: np.ndarray, img2: np.ndarray) -> float:
    orb = cv2.ORB_create(nfeatures=800)
    kp1, des1 = orb.detectAndCompute(img1, None)
    kp2, des2 = orb.detectAndCompute(img2, None)
    if des1 is None or des2 is None or len(des1) == 0 or len(des2) == 0:
        return 0.0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    if not matches:
        return 0.0
    good = [m for m in matches if m.distance <= 48]
    base = max(len(des1), len(des2), 1)
    score = float(len(good)) / float(base)
    return float(max(0.0, min(1.0, score)))


def _hist_similarity(img1: np.ndarray, img2: np.ndarray) -> float:
    hist1 = cv2.calcHist([img1], [0], None, [64], [0, 256])
    hist2 = cv2.calcHist([img2], [0], None, [64], [0, 256])
    hist1 = cv2.normalize(hist1, hist1).flatten()
    hist2 = cv2.normalize(hist2, hist2).flatten()
    corr = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
    score = (float(corr) + 1.0) / 2.0
    return float(max(0.0, min(1.0, score)))


def _pixel_similarity(img1: np.ndarray, img2: np.ndarray) -> float:
    diff = np.abs(img1.astype(np.float32) - img2.astype(np.float32))
    mean_abs = float(np.mean(diff))
    score = 1.0 - (mean_abs / 255.0)
    return float(max(0.0, min(1.0, score)))


def _classic_face_verify(
    ref_image: np.ndarray, probe_image: np.ndarray
) -> Dict[str, float]:
    ref_face = _crop_face_or_full(ref_image)
    probe_face = _crop_face_or_full(probe_image)

    ref_gray = _preprocess(cv2.cvtColor(ref_face, cv2.COLOR_BGR2GRAY))
    probe_gray = _preprocess(cv2.cvtColor(probe_face, cv2.COLOR_BGR2GRAY))

    orb_score = _orb_similarity(ref_gray, probe_gray)
    hist_score = _hist_similarity(ref_gray, probe_gray)
    pixel_score = _pixel_similarity(ref_gray, probe_gray)

    final_score = 0.45 * orb_score + 0.25 * hist_score + 0.30 * pixel_score
    final_score = float(max(0.0, min(1.0, final_score)))
    return {
        "score": final_score,
        "orb_score": orb_score,
        "hist_score": hist_score,
        "pixel_score": pixel_score,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="人脸比对推理脚本（标准输出JSON）。",
        add_help=False,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "可选参数"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("positional", nargs="*", help="可选：参考图 待验证图")
    parser.add_argument("--reference", default=None, help="参考图路径")
    parser.add_argument("--probe", default=None, help="待验证图路径")
    parser.add_argument("--ref", default=None, help="参考图路径（别名）")
    parser.add_argument("--verify", default=None, help="待验证图路径（别名）")
    parser.add_argument("--image1", default=None, help="参考图路径（别名）")
    parser.add_argument("--image2", default=None, help="待验证图路径（别名）")
    parser.add_argument("--source", default=None, help="参考图路径（别名）")
    parser.add_argument("--target", default=None, help="待验证图路径（别名）")
    parser.add_argument(
        "--reference-image-path", default=None, help="参考图路径（别名）"
    )
    parser.add_argument("--probe-image-path", default=None, help="待验证图路径（别名）")
    parser.add_argument(
        "--threshold",
        type=float,
        default=_to_float_env("FACE_CLASSIC_THRESHOLD", 0.72),
        help="匹配阈值（默认读取 FACE_CLASSIC_THRESHOLD 或 0.72）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        reference_path, probe_path = _resolve_paths(args)
        ref_image = _load_image(reference_path)
        probe_image = _load_image(probe_path)

        scores = _classic_face_verify(ref_image, probe_image)
        score = float(scores["score"])
        matched = score >= float(args.threshold)

        result = {
            "matched": bool(matched),
            "score": score,
            "threshold": float(args.threshold),
            "backend": "opencv_classic",
            "detail": scores,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        err = re.sub(r"\s+", " ", str(exc)).strip()
        print(f"错误: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
