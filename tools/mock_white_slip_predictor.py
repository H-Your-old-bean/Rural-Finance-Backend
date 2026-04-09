import json
import os
import sys
from pathlib import Path


def main() -> int:
    image_path = sys.argv[1] if len(sys.argv) > 1 else ""
    filename = Path(image_path).name if image_path else ""

    # Mock payload; replace with your own model inference output.
    payload = {
        "reason": "修路人工费",
        "signers": ["张三", "李四"],
        "payer": "村委会",
        "payee": "施工队",
        "amount": 2600.0,
        "date": "2026-03-30",
        "slip_type": "white_slip",
        "meta": {
            "image_path_exists": bool(image_path and os.path.exists(image_path)),
            "filename": filename,
        },
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
