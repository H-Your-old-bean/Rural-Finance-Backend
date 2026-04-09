import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_AUTO_DISCOVER_SCRIPT_CACHE: Optional[List[Path]] = None


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace(",", "")
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _normalize_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = str(raw).strip()
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            return None
    compact = re.search(r"(20\d{6})", text)
    if compact:
        token = compact.group(1)
        try:
            return datetime.strptime(token, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _split_names(raw: str) -> List[str]:
    text = str(raw or "").strip()
    text = re.sub(r"[，,、;；]", " ", text)
    parts = re.split(r"\s+", text)
    out: List[str] = []
    for part in parts:
        token = part.strip()
        if re.fullmatch(r"[A-Za-z\u4e00-\u9fa5]{2,16}", token) and token not in out:
            out.append(token)
    return out


def _extract_party(full_text: str, labels: List[str]) -> Optional[str]:
    pattern = "|".join(re.escape(x) for x in labels)
    m = re.search(rf"(?:{pattern})\s*[:：]?\s*([^\n:：]{{1,40}})", full_text)
    if not m:
        return None
    value = m.group(1).strip().strip(".。")
    return value or None


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


def _normalize_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = payload or {}
    if isinstance(source.get("fields"), dict):
        source = source["fields"]

    reason_raw = source.get("reason")
    payer_raw = source.get("payer")
    payee_raw = source.get("payee")
    amount_raw = source.get("amount")
    date_raw = source.get("date")
    slip_type_raw = str(source.get("slip_type") or "white_slip").strip().lower()

    raw_signers = source.get("signers") or source.get("signatories") or []
    signers: List[str] = []
    if isinstance(raw_signers, str):
        signers.extend(_split_names(raw_signers))
    elif isinstance(raw_signers, list):
        for item in raw_signers:
            signers.extend(_split_names(str(item)))
    signers = list(dict.fromkeys(signers))

    reason = str(reason_raw).strip() if reason_raw not in (None, "") else None
    payer = str(payer_raw).strip() if payer_raw not in (None, "") else None
    payee = str(payee_raw).strip() if payee_raw not in (None, "") else None
    amount = _to_float(str(amount_raw)) if amount_raw not in (None, "") else None
    date_value = _normalize_date(str(date_raw)) if date_raw not in (None, "") else None

    if slip_type_raw not in {"white_slip", "loan_note", "receipt_note", "other"}:
        slip_type_raw = "white_slip"

    return {
        "reason": reason,
        "signers": signers,
        "payer": payer,
        "payee": payee,
        "amount": amount,
        "date": date_value,
        "slip_type": slip_type_raw,
    }


def _call_external_predictor(
    *,
    file_bytes: Optional[bytes],
    filename: Optional[str],
) -> Dict[str, Any]:
    if not file_bytes:
        raise RuntimeError("图片字节为空")

    suffix = Path(filename or "white_slip.jpg").suffix or ".jpg"
    timeout_seconds = float(os.getenv("WHITE_SLIP_LOCAL_MODEL_CMD_TIMEOUT", "5"))
    max_commands = max(1, int(os.getenv("WHITE_SLIP_LOCAL_MODEL_MAX_COMMANDS", "3")))
    total_timeout = float(os.getenv("WHITE_SLIP_LOCAL_MODEL_TOTAL_TIMEOUT", "12"))

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        errors: List[str] = []
        commands = _iter_external_model_commands(
            image_path=tmp_path,
            filename=filename or Path(tmp_path).name,
        )
        deadline = time.monotonic() + max(1.0, total_timeout)
        attempted = 0
        for command in commands:
            if attempted >= max_commands:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(
                    f"杈惧埌鎬昏秴鏃堕檺鍒?{total_timeout:.0f}s锛屽凡鍋滄澶栭儴鍛戒护灏濊瘯"
                )
                break
            current_timeout = max(1.0, min(timeout_seconds, remaining))
            attempted += 1
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=current_timeout,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"鍛戒护瓒呮椂({current_timeout:.0f}s), 鍛戒护={command}")
                continue
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
                return _normalize_fields(payload)
            errors.append(
                f"无JSON输出, 命令={command}, 输出={(stdout or stderr)[:120]}"
            )

        raise RuntimeError(
            "外部模型命令执行失败: " + " | ".join(errors[:5])
            if errors
            else "外部模型命令执行失败: 没有可用命令"
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _discover_auto_script_paths() -> List[Path]:
    global _AUTO_DISCOVER_SCRIPT_CACHE
    if _AUTO_DISCOVER_SCRIPT_CACHE is not None:
        return _AUTO_DISCOVER_SCRIPT_CACHE

    root = Path(__file__).resolve().parent
    candidates: List[Path] = []
    seen: set[str] = set()

    seed_rel_paths = [
        "infer_white_slip.py",
        "predict_white_slip.py",
        "white_slip_infer.py",
        "white_slip_predict.py",
        "tools/infer_white_slip.py",
        "tools/predict_white_slip.py",
        "tools/white_slip_infer.py",
        "tools/white_slip_predict.py",
        "models/infer_white_slip.py",
        "models/predict_white_slip.py",
        "models/white_slip_infer.py",
        "models/white_slip_predict.py",
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
            if "white" not in name and "slip" not in name and "baitiao" not in name:
                continue
            if not any(token in name for token in ("infer", "predict", "parse")):
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(path.resolve())

    _AUTO_DISCOVER_SCRIPT_CACHE = candidates[:12]
    return _AUTO_DISCOVER_SCRIPT_CACHE


def _iter_external_model_commands(*, image_path: str, filename: str) -> List[str]:
    cmd_template = os.getenv("WHITE_SLIP_LOCAL_MODEL_CMD", "").strip()
    if cmd_template:
        return [cmd_template.format(image_path=image_path, filename=filename)]

    auto_discover = (
        os.getenv("WHITE_SLIP_LOCAL_MODEL_AUTO_DISCOVER", "true").strip().lower()
        == "true"
    )
    if not auto_discover:
        return []

    python_bin = (
        os.getenv("WHITE_SLIP_LOCAL_MODEL_PYTHON", "").strip() or sys.executable
    )
    quoted_py = f'"{python_bin}"'
    quoted_image = f'"{image_path}"'
    commands: List[str] = []
    for script in _discover_auto_script_paths():
        quoted_script = f'"{str(script)}"'
        commands.append(f"{quoted_py} {quoted_script} --image {quoted_image}")
        commands.append(f"{quoted_py} {quoted_script} {quoted_image}")
    return commands[:16]


def predict_white_slip(
    *,
    text_lines: List[str],
    full_text: str,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """
    本地白条预测器（默认正则规则版）。
    可按需替换为你自己的训练模型推理逻辑。
    """
    strict_external = (
        os.getenv("WHITE_SLIP_LOCAL_MODEL_CMD_STRICT", "false").strip().lower()
        == "true"
    )
    cmd_template = os.getenv("WHITE_SLIP_LOCAL_MODEL_CMD", "").strip()
    auto_discover = (
        os.getenv("WHITE_SLIP_LOCAL_MODEL_AUTO_DISCOVER", "true").strip().lower()
        == "true"
    )
    if cmd_template or auto_discover:
        try:
            return _call_external_predictor(file_bytes=file_bytes, filename=filename)
        except Exception:
            if strict_external:
                raise

    # 该开关用于快速关闭规则分支并返回空结构。
    if os.getenv("WHITE_SLIP_LOCAL_MODEL_RULE_ENABLED", "true").lower() != "true":
        return {
            "reason": None,
            "signers": [],
            "payer": None,
            "payee": None,
            "amount": None,
            "date": None,
            "slip_type": "white_slip",
        }

    text = full_text or "\n".join(text_lines or [])
    reason = None
    for pattern in (
        r"(?:事由|用途|内容)\s*[:：]?\s*([^\n]{2,80})",
        r"(?:报销)?事由\s*[:：]?\s*([^\n]{2,80})",
    ):
        m = re.search(pattern, text)
        if m:
            reason = m.group(1).strip().strip(".。")
            break

    signers: List[str] = []
    for raw in re.findall(
        r"(?:签字|签名|经手人|报销人)\s*[:：]?\s*([^\n]{2,40})", text
    ):
        for name in _split_names(raw):
            if name not in signers:
                signers.append(name)

    amount = None
    for pattern in (
        r"(?:金额|合计|人民币)\s*[:：]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        r"(?:¥|￥)\s*([0-9]+(?:\.[0-9]{1,2})?)",
    ):
        m = re.search(pattern, text)
        if m:
            amount = _to_float(m.group(1))
            break

    date_value = None
    for line in text_lines or []:
        date_value = _normalize_date(line)
        if date_value:
            break
    if not date_value:
        date_value = _normalize_date(text)

    payer = _extract_party(text, ["付款人", "交款人", "借款人", "出款人"])
    payee = _extract_party(text, ["收款人", "收款方", "收款单位"])

    slip_type = "white_slip"
    if "借条" in text:
        slip_type = "loan_note"
    elif "收条" in text or "领条" in text:
        slip_type = "receipt_note"

    return {
        "reason": reason,
        "signers": signers,
        "payer": payer,
        "payee": payee,
        "amount": amount,
        "date": date_value,
        "slip_type": slip_type,
    }
