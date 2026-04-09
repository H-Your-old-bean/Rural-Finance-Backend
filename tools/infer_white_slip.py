import argparse
import importlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

_ARGPARSE_CN_MAP = {
    "usage: ": "用法: ",
    "positional arguments": "位置参数",
    "options": "可选参数",
    "show this help message and exit": "显示帮助并退出",
}


def _argparse_cn(text: str) -> str:
    return _ARGPARSE_CN_MAP.get(text, text)


argparse._ = _argparse_cn


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
    text = text.replace("\u5e74", "-").replace("\u6708", "-").replace("\u65e5", "")
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
    if isinstance(source.get("white_slip"), dict):
        source = source["white_slip"]

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


def _collect_paddle_text(node: Any, output: List[str]) -> None:
    if isinstance(node, dict):
        rec_texts = node.get("rec_texts")
        if isinstance(rec_texts, list):
            for text in rec_texts:
                if isinstance(text, str):
                    stripped = text.strip()
                    if stripped:
                        output.append(stripped)
        for value in node.values():
            _collect_paddle_text(value, output)
        return

    if isinstance(node, (list, tuple)):
        if (
            len(node) >= 2
            and isinstance(node[1], (list, tuple))
            and len(node[1]) >= 1
            and isinstance(node[1][0], str)
        ):
            text = node[1][0].strip()
            if text:
                output.append(text)
            return
        for child in node:
            _collect_paddle_text(child, output)


def _run_paddle_ocr(image_path: Path, lang: str, use_gpu: bool) -> List[str]:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(
        lang=lang,
        use_gpu=use_gpu,
        show_log=False,
        enable_hpi=False,
        enable_mkldnn=False,
        enable_cinn=False,
    )
    try:
        raw_result = ocr.ocr(str(image_path), cls=False)
    except TypeError:
        raw_result = ocr.predict(str(image_path))
    lines: List[str] = []
    _collect_paddle_text(raw_result, lines)
    return [line for line in lines if line][:120]


def _rule_extract_fields(text_lines: List[str], full_text: str) -> Dict[str, Any]:
    reason = None
    for pattern in (
        r"(?:\u4e8b\u7531|\u7528\u9014|\u5185\u5bb9)\s*[:：]?\s*([^\n]{2,80})",
        r"(?:\u62a5\u9500)?\u4e8b\u7531\s*[:：]?\s*([^\n]{2,80})",
    ):
        m = re.search(pattern, full_text)
        if m:
            reason = m.group(1).strip().strip(".。")
            break

    signers: List[str] = []
    for raw in re.findall(
        r"(?:\u7b7e\u5b57|\u7b7e\u540d|\u7ecf\u624b\u4eba|\u62a5\u9500\u4eba)\s*[:：]?\s*([^\n]{2,40})",
        full_text,
    ):
        for name in _split_names(raw):
            if name not in signers:
                signers.append(name)

    amount = None
    for pattern in (
        r"(?:\u91d1\u989d|\u5408\u8ba1|\u4eba\u6c11\u5e01)\s*[:：]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        r"(?:¥|￥)\s*([0-9]+(?:\.[0-9]{1,2})?)",
    ):
        m = re.search(pattern, full_text)
        if m:
            amount = _to_float(m.group(1))
            break

    date_value = None
    for line in text_lines:
        date_value = _normalize_date(line)
        if date_value:
            break
    if not date_value:
        date_value = _normalize_date(full_text)

    payer = _extract_party(
        full_text,
        [
            "\u4ed8\u6b3e\u4eba",
            "\u4ea4\u6b3e\u4eba",
            "\u501f\u6b3e\u4eba",
            "\u51fa\u6b3e\u4eba",
        ],
    )
    payee = _extract_party(
        full_text,
        ["\u6536\u6b3e\u4eba", "\u6536\u6b3e\u65b9", "\u6536\u6b3e\u5355\u4f4d"],
    )

    slip_type = "white_slip"
    if "\u501f\u6761" in full_text:
        slip_type = "loan_note"
    elif "\u6536\u6761" in full_text or "\u9886\u6761" in full_text:
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


def _load_module_from_path(module_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "custom_white_slip_model", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法从路径加载模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_custom_predictor(
    image_path: Path,
    *,
    module_name: Optional[str],
    module_path: Optional[Path],
    function_name: str,
) -> Dict[str, Any]:
    if module_path is not None:
        module = _load_module_from_path(module_path)
    elif module_name:
        module = importlib.import_module(module_name)
    else:
        raise RuntimeError("自定义预测器需要提供 module_name 或 module_path")

    predictor = getattr(module, function_name, None)
    if not callable(predictor):
        raise RuntimeError(f"未找到函数: {function_name}")

    image_bytes = image_path.read_bytes()
    call_errors: List[str] = []
    for call in (
        lambda: predictor(
            image_path=str(image_path),
            image_bytes=image_bytes,
            filename=image_path.name,
        ),
        lambda: predictor(image_path=str(image_path)),
        lambda: predictor(str(image_path)),
    ):
        try:
            raw_result = call()
            if isinstance(raw_result, dict):
                return _normalize_fields(raw_result)
            payload = _extract_first_json_object(str(raw_result))
            if payload:
                return _normalize_fields(payload)
            call_errors.append("返回内容不是有效JSON")
        except Exception as exc:
            call_errors.append(str(exc))
    raise RuntimeError("自定义预测器调用失败: " + " | ".join(call_errors[:3]))


def _resolve_image_path(args: argparse.Namespace) -> Path:
    image_arg = args.image or args.image_path
    if not image_arg:
        raise RuntimeError("缺少图片路径，请使用 --image <路径>")
    path = Path(image_arg).resolve()
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"图片不存在: {path}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="白条图片推理脚本（标准输出JSON）。",
        add_help=False,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "可选参数"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("image_path", nargs="?", help="图片路径")
    parser.add_argument("--image", default=None, help="图片路径")
    parser.add_argument("--lang", default="ch", help="PaddleOCR语言（默认: ch）")
    parser.add_argument("--use-gpu", action="store_true", help="启用GPU进行OCR")
    parser.add_argument(
        "--custom-module",
        default=None,
        help="可选：自定义模型推理模块名",
    )
    parser.add_argument(
        "--custom-module-path",
        default=None,
        help="可选：自定义模型推理脚本路径",
    )
    parser.add_argument(
        "--custom-function",
        default="predict_from_image",
        help="自定义模块中的函数名（默认: predict_from_image）",
    )
    parser.add_argument(
        "--with-meta",
        action="store_true",
        help="在输出JSON中包含元信息字段",
    )
    parser.add_argument(
        "--allow-empty-ocr",
        action="store_true",
        help="允许OCR未识别文本时输出空结构（默认关闭）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        image_path = _resolve_image_path(args)
        module_path = (
            Path(args.custom_module_path).resolve() if args.custom_module_path else None
        )

        if args.custom_module or module_path:
            fields = _call_custom_predictor(
                image_path,
                module_name=args.custom_module,
                module_path=module_path,
                function_name=args.custom_function,
            )
            lines: List[str] = []
        else:
            try:
                lines = _run_paddle_ocr(
                    image_path, lang=args.lang, use_gpu=args.use_gpu
                )
            except Exception:
                lines = []
            real_required = (
                os.getenv("WHITE_SLIP_REAL_MODEL_REQUIRED", "true").strip().lower()
                == "true"
            )
            if real_required and not args.allow_empty_ocr and not lines:
                raise RuntimeError("OCR模型未识别到文本，白条真实模型未产出有效结果")
            full_text = "\n".join(lines)
            fields = _rule_extract_fields(lines, full_text)

        if args.with_meta:
            fields = {
                **fields,
                "meta": {
                    "image_path": str(image_path),
                    "ocr_line_count": len(lines),
                },
            }

        print(json.dumps(fields, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
