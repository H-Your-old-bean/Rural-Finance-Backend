import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_AUTO_DISCOVER_SCRIPT_CACHE: Optional[List[Path]] = None


def _extract_first_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    text = str(raw_text or "").strip()
    if not text:
        return None

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```$", "", text).strip()

    candidates = [text]
    left = text.find("{")
    right = text.rfind("}")
    if left >= 0 and right > left:
        candidates.append(text[left : right + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            continue
    return None


def _normalize_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = payload or {}
    if isinstance(source.get("result"), dict):
        source = source["result"]
    if isinstance(source.get("data"), dict):
        source = source["data"]

    score = source.get("score")
    matched = source.get("matched")
    message = source.get("message")

    out: Dict[str, Any] = {}
    if isinstance(score, (int, float)):
        out["score"] = float(score)
    elif isinstance(score, str):
        try:
            out["score"] = float(score.strip())
        except ValueError:
            pass

    if isinstance(matched, bool):
        out["matched"] = matched
    elif isinstance(matched, str):
        low = matched.strip().lower()
        if low in {"1", "true", "yes", "y"}:
            out["matched"] = True
        elif low in {"0", "false", "no", "n"}:
            out["matched"] = False

    if isinstance(message, str) and message.strip():
        out["message"] = message.strip()

    if "matched" not in out and "score" not in out:
        raise RuntimeError("模型输出缺少 matched 或 score 字段")
    return out


def _discover_auto_script_paths() -> List[Path]:
    global _AUTO_DISCOVER_SCRIPT_CACHE
    if _AUTO_DISCOVER_SCRIPT_CACHE is not None:
        return _AUTO_DISCOVER_SCRIPT_CACHE

    root = Path(__file__).resolve().parent
    candidates: List[Path] = []
    seen: set[str] = set()

    seed_rel_paths = [
        "infer_face.py",
        "face_infer.py",
        "face_predict.py",
        "face_verify.py",
        "tools/infer_face.py",
        "tools/face_infer.py",
        "tools/face_predict.py",
        "tools/face_verify.py",
        "models/infer_face.py",
        "models/face_infer.py",
        "models/face_predict.py",
        "models/face_verify.py",
    ]
    for rel in seed_rel_paths:
        path = (root / rel).resolve()
        if path.exists() and path.is_file():
            key = str(path).lower()
            if key not in seen:
                seen.add(key)
                candidates.append(path)

    for folder_name in ("tools", "models", "model"):
        folder = (root / folder_name).resolve()
        if not folder.exists() or not folder.is_dir():
            continue
        for path in folder.rglob("*.py"):
            name = path.name.lower()
            if "face" not in name and "renlian" not in name:
                continue
            if not any(
                token in name
                for token in ("infer", "predict", "verify", "match", "parse")
            ):
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(path.resolve())

    candidates.sort(
        key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True
    )
    _AUTO_DISCOVER_SCRIPT_CACHE = candidates[:12]
    return _AUTO_DISCOVER_SCRIPT_CACHE


def _iter_external_model_commands(
    *,
    reference_image_path: str,
    probe_image_path: str,
) -> List[str]:
    cmd_template = os.getenv("FACE_LOCAL_MODEL_CMD", "").strip()
    if cmd_template:
        return [
            cmd_template.format(
                reference_image_path=reference_image_path,
                probe_image_path=probe_image_path,
                ref_image_path=reference_image_path,
                verify_image_path=probe_image_path,
                image1_path=reference_image_path,
                image2_path=probe_image_path,
            )
        ]

    auto_discover = (
        os.getenv("FACE_LOCAL_MODEL_AUTO_DISCOVER", "true").strip().lower() == "true"
    )
    if not auto_discover:
        return []

    python_bin = os.getenv("FACE_LOCAL_MODEL_PYTHON", "").strip() or sys.executable
    quoted_py = f'"{python_bin}"'
    quoted_ref = f'"{reference_image_path}"'
    quoted_probe = f'"{probe_image_path}"'
    commands: List[str] = []

    for script in _discover_auto_script_paths():
        quoted_script = f'"{str(script)}"'
        commands.append(f"{quoted_py} {quoted_script} {quoted_ref} {quoted_probe}")
        commands.append(
            f"{quoted_py} {quoted_script} --reference {quoted_ref} --probe {quoted_probe}"
        )
        commands.append(
            f"{quoted_py} {quoted_script} --ref {quoted_ref} --verify {quoted_probe}"
        )
        commands.append(
            f"{quoted_py} {quoted_script} --image1 {quoted_ref} --image2 {quoted_probe}"
        )
        commands.append(
            f"{quoted_py} {quoted_script} --source {quoted_ref} --target {quoted_probe}"
        )
    return commands[:40]


def _call_external_predictor(
    *,
    reference_image_bytes: bytes,
    probe_image_bytes: bytes,
) -> Dict[str, Any]:
    if not reference_image_bytes or not probe_image_bytes:
        raise RuntimeError("图片字节为空")

    timeout_seconds = float(os.getenv("FACE_LOCAL_MODEL_CMD_TIMEOUT", "30"))
    ref_path = ""
    probe_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as ref_tmp:
            ref_tmp.write(reference_image_bytes)
            ref_path = ref_tmp.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as probe_tmp:
            probe_tmp.write(probe_image_bytes)
            probe_path = probe_tmp.name

        errors: List[str] = []
        for command in _iter_external_model_commands(
            reference_image_path=ref_path,
            probe_image_path=probe_path,
        ):
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout_seconds),
            )
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            if proc.returncode != 0:
                errors.append(
                    f"返回码={proc.returncode}, 命令={command}, 错误={stderr[:120]}"
                )
                continue

            payload = _extract_first_json_object(stdout) or _extract_first_json_object(
                stderr
            )
            if payload:
                return _normalize_result(payload)
            errors.append(
                f"无JSON输出, 命令={command}, 输出={(stdout or stderr)[:120]}"
            )

        if errors:
            raise RuntimeError("外部人脸模型执行失败: " + " | ".join(errors[:5]))
        raise RuntimeError("外部人脸模型执行失败: 没有可用命令")
    finally:
        for path in (ref_path, probe_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def verify_face_pair(
    *,
    reference_image_bytes: bytes,
    probe_image_bytes: bytes,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """
    本地人脸核验默认适配器：
    1) 优先尝试外部推理命令（FACE_LOCAL_MODEL_CMD 或自动发现）
    2) 外部模型不可用时，按配置决定是否启用哈希回退
    """
    strict_external = (
        os.getenv("FACE_LOCAL_MODEL_CMD_STRICT", "false").strip().lower() == "true"
    )
    cmd_template = os.getenv("FACE_LOCAL_MODEL_CMD", "").strip()
    auto_discover = (
        os.getenv("FACE_LOCAL_MODEL_AUTO_DISCOVER", "true").strip().lower() == "true"
    )
    if cmd_template or auto_discover:
        try:
            return _call_external_predictor(
                reference_image_bytes=reference_image_bytes,
                probe_image_bytes=probe_image_bytes,
            )
        except Exception:
            if strict_external:
                raise

    allow_hash_fallback = (
        os.getenv("FACE_LOCAL_MODEL_USE_HASH_FALLBACK", "false").strip().lower()
        == "true"
    )
    if not allow_hash_fallback:
        raise RuntimeError("未找到可用本地模型，且已关闭哈希回退")

    ref_hash = hashlib.sha256(reference_image_bytes).hexdigest()
    probe_hash = hashlib.sha256(probe_image_bytes).hexdigest()
    matched = ref_hash == probe_hash
    return {
        "matched": matched,
        "score": 1.0 if matched else 0.0,
        "message": (
            "使用哈希回退进行一致性校验；建议配置 FACE_LOCAL_MODEL_CMD 接入真实模型"
        ),
        "username": username,
    }
