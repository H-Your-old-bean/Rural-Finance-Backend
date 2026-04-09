import argparse
import ctypes
import json
import mimetypes
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".pdf"}
_ARGPARSE_CN_MAP = {
    "usage: ": "用法: ",
    "positional arguments": "位置参数",
    "options": "可选参数",
    "show this help message and exit": "显示帮助并退出",
}


def _argparse_cn(text: str) -> str:
    return _ARGPARSE_CN_MAP.get(text, text)


argparse._ = _argparse_cn


def popup(
    title: str, message: str, enabled: bool = True, is_error: bool = False
) -> None:
    if not enabled:
        print(f"[{title}] {message}")
        return
    if hasattr(ctypes, "windll"):
        style = 0x10 if is_error else 0x40
        try:
            # MessageBoxW 为模态窗口；放到后台线程避免阻塞扫描循环。
            threading.Thread(
                target=lambda: ctypes.windll.user32.MessageBoxW(
                    0, message, title, style
                ),
                daemon=True,
            ).start()
            return
        except Exception:
            pass
    print(f"[{title}] {message}")


class ScannerUploader:
    LAST_NOTIFICATION_KEY = "__last_notification_id__"

    def __init__(
        self,
        base_url: str,
        username: str,
        id_card: str,
        watch_dir: Path,
        source_device: str,
        poll_interval: float,
        settle_seconds: float,
        archive_dir: Optional[Path],
        popup_enabled: bool,
        notification_interval: float,
        request_timeout: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.id_card = id_card
        self.watch_dir = watch_dir
        self.source_device = source_device
        self.poll_interval = poll_interval
        self.settle_seconds = settle_seconds
        self.archive_dir = archive_dir
        self.popup_enabled = popup_enabled
        self.notification_interval = max(5.0, float(notification_interval))
        self.request_timeout = max(5.0, float(request_timeout))
        self.state_path = watch_dir / ".scan_upload_state.json"
        self.token = None
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.request_timeout, connect=10.0)
        )
        self.state: Dict[str, float] = self._load_state()

    def _load_state(self) -> Dict[str, float]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_last_notification_id(self) -> int:
        return int(self.state.get(self.LAST_NOTIFICATION_KEY, 0))

    def _set_last_notification_id(self, notification_id: int) -> None:
        self.state[self.LAST_NOTIFICATION_KEY] = float(max(0, notification_id))
        self._save_state()

    def login(self) -> None:
        resp = self.client.post(
            f"{self.base_url}/login",
            json={"username": self.username, "id_card": self.id_card},
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"登录失败: {payload}")
        self.token = token
        print("[INFO] 登录成功")

    def _file_key(self, path: Path) -> str:
        stat = path.stat()
        return f"{path.resolve()}::{stat.st_mtime_ns}::{stat.st_size}"

    def _is_file_ready(self, path: Path) -> bool:
        stat = path.stat()
        if stat.st_size <= 0:
            return False
        age = time.time() - stat.st_mtime
        return age >= self.settle_seconds

    def _archive(self, path: Path) -> Path:
        if not self.archive_dir:
            return path
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        target = self.archive_dir / path.name
        if target.exists():
            stamp = time.strftime("%Y%m%d%H%M%S")
            target = self.archive_dir / f"{path.stem}_{stamp}{path.suffix}"
        shutil.move(str(path), str(target))
        return target

    def upload_file(self, path: Path) -> Dict:
        if not self.token:
            self.login()

        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as f:
            files = {"file": (path.name, f, mime_type)}
            headers = {
                "access-token": self.token,
                "X-Source-Device": self.source_device,
            }
            resp = self.client.post(
                f"{self.base_url}/upload-voucher",
                params={"username": self.username},
                files=files,
                headers=headers,
            )

        if resp.status_code in (401, 403):
            self.login()
            return self.upload_file(path)

        resp.raise_for_status()
        return resp.json()

    def poll_notifications(self) -> None:
        if not self.token:
            self.login()

        since_id = self._get_last_notification_id()
        headers = {"access-token": self.token}
        resp = self.client.get(
            f"{self.base_url}/notifications",
            params={
                "username": self.username,
                "since_id": since_id,
                "active_only": "true",
                "limit": 20,
            },
            headers=headers,
        )

        if resp.status_code in (401, 403):
            self.login()
            self.poll_notifications()
            return

        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", []) or []
        if not items:
            return

        max_id = since_id
        for item in sorted(items, key=lambda x: x.get("id", 0)):
            notice_id = int(item.get("id", 0))
            max_id = max(max_id, notice_id)
            title = item.get("title", "新通知")
            content = item.get("content", "")
            popup("乡镇通知", f"{title}\n{content}", enabled=self.popup_enabled)
            print(f"[NOTICE] #{notice_id} {title}")
        self._set_last_notification_id(max_id)

    def run(self) -> None:
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        if self.archive_dir:
            self.archive_dir.mkdir(parents=True, exist_ok=True)

        if not self.token:
            self.login()

        print(f"[INFO] 开始监控目录: {self.watch_dir}")
        print(f"[INFO] HTTP超时时间: {self.request_timeout:.0f}s")
        next_notification_ts = 0.0
        while True:
            try:
                now = time.time()
                if now >= next_notification_ts:
                    try:
                        self.poll_notifications()
                    except httpx.TimeoutException:
                        print(
                            f"[WARN] 通知轮询超时（{self.request_timeout:.0f}s）；服务端可能仍在进行OCR处理。"
                        )
                    except Exception as exc:
                        print(f"[WARN] 閫氱煡杞澶辫触: {exc}")
                    next_notification_ts = now + self.notification_interval

                for path in sorted(self.watch_dir.iterdir()):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    if not self._is_file_ready(path):
                        continue

                    file_key = self._file_key(path)
                    if file_key in self.state:
                        continue

                    try:
                        result = self.upload_file(path)
                        reimbursement_id = result.get("reimbursement_id")
                        reimbursement_status = result.get("reimbursement_status")
                        ocr = result.get("ocr", {})
                        fields = ocr.get("fields", {}) or {}
                        ocr_status = ocr.get("status")
                        ocr_provider = ocr.get("provider")
                        ocr_doc_type = ocr.get("document_type")
                        ocr_error = ocr.get("error")
                        ocr_warning = ocr.get("warning")
                        id_name = fields.get("id_name")
                        id_number = fields.get("id_number")
                        message_lines = [
                            f"文件: {path.name}",
                            f"金额: {fields.get('amount')}",
                            f"日期: {fields.get('date')}",
                            f"发票代码: {fields.get('invoice_code')}",
                            f"报账ID: {reimbursement_id}",
                            f"审核状态: {reimbursement_status}",
                        ]
                        if ocr_doc_type:
                            message_lines.append(f"单据类型: {ocr_doc_type}")
                        if ocr_doc_type == "id_card":
                            message_lines.append(f"姓名: {id_name}")
                            message_lines.append(f"身份证号: {id_number}")
                        if ocr_doc_type == "white_slip":
                            white_slip_standard = (
                                ocr.get("white_slip_standard", {}) or {}
                            )
                            signers = ", ".join(
                                white_slip_standard.get("signers", []) or []
                            )
                            missing_fields = ", ".join(
                                white_slip_standard.get("missing_fields", []) or []
                            )
                            message_lines.append(
                                f"事由: {white_slip_standard.get('reason')}"
                            )
                            message_lines.append(
                                f"白条状态: {white_slip_standard.get('status')}"
                            )
                            message_lines.append(f"签字人: {signers}")
                            if missing_fields:
                                message_lines.append(f"缺失项: {missing_fields}")
                        if ocr_provider:
                            message_lines.append(f"OCR提供方: {ocr_provider}")
                        if ocr_error:
                            message_lines.append(f"OCR错误: {ocr_error}")
                        if ocr_warning:
                            message_lines.append(f"OCR警告: {ocr_warning}")
                        message = "\n".join(message_lines)
                        popup("上传识别成功", message, enabled=self.popup_enabled)

                        self.state[file_key] = time.time()
                        self._save_state()
                        archived = self._archive(path)
                        print(f"[OK] {path.name} -> {archived}")
                    except Exception as exc:
                        if isinstance(exc, httpx.TimeoutException):
                            timeout_msg = (
                                f"请求超时（{self.request_timeout:.0f}s）。"
                                "请提高 --request-timeout 或先预热服务端OCR。"
                            )
                            popup(
                                "上传失败",
                                f"{path.name}\n错误: {timeout_msg}",
                                enabled=self.popup_enabled,
                                is_error=True,
                            )
                            print(f"[ERROR] {path.name}: {timeout_msg}")
                            continue
                        popup(
                            "上传识别失败",
                            f"{path.name}\n错误: {exc}",
                            enabled=self.popup_enabled,
                            is_error=True,
                        )
                        print(f"[ERROR] {path.name}: {exc}")
                time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                print("[INFO] 监控已停止")
                break


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="扫描文件自动上传工具（fi-7140）",
        add_help=False,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "可选参数"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="admin01")
    parser.add_argument("--id-card", default="130102199001011234")
    parser.add_argument("--watch-dir", required=True, help="扫描输出目录")
    parser.add_argument("--source-device", default="Fujitsu-fi-7140")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--notification-interval", type=float, default=30.0)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=120.0,
        help="HTTP请求超时时间（秒）",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="上传完成后移动到归档目录（默认: watch-dir/uploaded）",
    )
    parser.add_argument("--no-popup", action="store_true", help="关闭弹窗")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    watch_dir = Path(args.watch_dir).resolve()
    archive_dir = (
        Path(args.archive_dir).resolve()
        if args.archive_dir
        else (watch_dir / "uploaded")
    )

    uploader = ScannerUploader(
        base_url=args.base_url,
        username=args.username,
        id_card=args.id_card,
        watch_dir=watch_dir,
        source_device=args.source_device,
        poll_interval=args.poll_interval,
        settle_seconds=args.settle_seconds,
        archive_dir=archive_dir,
        popup_enabled=not args.no_popup,
        notification_interval=args.notification_interval,
        request_timeout=args.request_timeout,
    )
    uploader.run()


if __name__ == "__main__":
    main()
