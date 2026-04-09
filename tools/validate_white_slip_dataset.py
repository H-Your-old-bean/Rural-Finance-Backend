import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLIP_TYPES = {"white_slip", "loan_note", "receipt_note", "other"}
SPLITS = {"train", "val", "test"}


def _load_records(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                yield i, json.loads(line)
        return

    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("JSON input must be a list of records")
        for i, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"record #{i} must be an object")
            yield i, item
        return

    raise ValueError("input file must be .jsonl or .json")


def _is_str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _validate_record(record: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    if not isinstance(record, dict):
        return ["record is not an object"]

    rid = record.get("id")
    if not isinstance(rid, str) or not rid.strip():
        errors.append("id must be non-empty string")

    image_path = record.get("image_path")
    if not isinstance(image_path, str) or not image_path.strip():
        errors.append("image_path must be non-empty string")

    split = record.get("split")
    if split not in SPLITS:
        errors.append("split must be one of: train, val, test")

    ocr_lines = record.get("ocr_text_lines")
    if not isinstance(ocr_lines, list):
        errors.append("ocr_text_lines must be an array")
    else:
        if any(not isinstance(x, str) for x in ocr_lines):
            errors.append("ocr_text_lines items must be strings")

    ann = record.get("annotation")
    if not isinstance(ann, dict):
        errors.append("annotation must be an object")
        return errors

    for key in ("reason", "payer", "payee", "date"):
        if key not in ann:
            errors.append(f"annotation.{key} is required")
        elif not _is_str_or_none(ann.get(key)):
            errors.append(f"annotation.{key} must be string or null")

    if "signers" not in ann:
        errors.append("annotation.signers is required")
    elif not isinstance(ann.get("signers"), list):
        errors.append("annotation.signers must be an array")
    elif any(not isinstance(x, str) for x in ann.get("signers", [])):
        errors.append("annotation.signers items must be strings")

    if "amount" not in ann:
        errors.append("annotation.amount is required")
    else:
        amount = ann.get("amount")
        if amount is not None and not isinstance(amount, (int, float)):
            errors.append("annotation.amount must be number or null")
        if isinstance(amount, (int, float)) and amount < 0:
            errors.append("annotation.amount must be >= 0")

    slip_type = ann.get("slip_type")
    if slip_type not in SLIP_TYPES:
        errors.append(
            "annotation.slip_type must be one of: white_slip, loan_note, receipt_note, other"
        )

    date_value = ann.get("date")
    if isinstance(date_value, str) and date_value and not DATE_RE.match(date_value):
        errors.append("annotation.date must match YYYY-MM-DD or be null")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate white-slip training dataset (.jsonl/.json)"
    )
    parser.add_argument("--input", required=True, help="dataset file path")
    args = parser.parse_args()

    dataset_path = Path(args.input).resolve()
    if not dataset_path.exists():
        print(f"[ERROR] file not found: {dataset_path}")
        return 1

    total = 0
    invalid = 0
    split_stats = {"train": 0, "val": 0, "test": 0}

    try:
        for line_no, record in _load_records(dataset_path):
            total += 1
            split = record.get("split")
            if split in split_stats:
                split_stats[split] += 1

            errors = _validate_record(record)
            if errors:
                invalid += 1
                print(f"[INVALID] line={line_no}, id={record.get('id')}")
                for err in errors:
                    print(f"  - {err}")
    except Exception as exc:
        print(f"[ERROR] failed to validate dataset: {exc}")
        return 1

    print(
        "[SUMMARY] "
        f"total={total}, invalid={invalid}, "
        f"train={split_stats['train']}, val={split_stats['val']}, test={split_stats['test']}"
    )
    if total == 0:
        print("[ERROR] dataset is empty")
        return 1
    return 1 if invalid > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
