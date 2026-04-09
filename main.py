import asyncio
import base64
import email
import hashlib
import imaplib
import importlib
import json
import mimetypes
import os
import re
import secrets
import socket
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Fernet：对称加密工具（适合文件内容加密存储）
from cryptography.fernet import Fernet

# FastAPI：用于构建 REST API、声明请求参数、依赖注入和抛出 HTTP 异常
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

# Pydantic：用于请求体结构定义与输入校验
from pydantic import BaseModel, Field

# SQLAlchemy：用于数据库连接、ORM 模型定义与查询
from sqlalchemy import DateTime, Float, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

# ============================================================
# 1) 核心配置区
#    - 数据库连接配置
#    - 密钥加载与加密器初始化
#    - 令牌有效期配置
# ============================================================

# 数据库 URL：优先读取环境变量，未配置时默认使用本地 SQLite
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rural_finance.db")

# 创建数据库引擎
# 注意：SQLite 需要 check_same_thread=False 以适配常见 Web 场景
is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")
is_sqlite_memory = SQLALCHEMY_DATABASE_URL in {"sqlite:///:memory:", "sqlite://"}
engine_kwargs: Dict[str, Any] = {}
if is_sqlite:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
if is_sqlite_memory:
    # 内存 SQLite 必须复用同一连接，避免建表与业务请求不在同一库实例。
    engine_kwargs["poolclass"] = StaticPool
engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_kwargs)

# 创建数据库会话工厂：每个请求获取独立 Session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 本地密钥文件路径：用于 Fernet 加密/解密
KEY_FILE = "secret.key"

# 首次运行若无密钥文件，则自动生成
if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, "wb") as f:
        f.write(Fernet.generate_key())

# 读取密钥并初始化加密器
with open(KEY_FILE, "rb") as f:
    cipher = Fernet(f.read())

# 令牌过期时间（分钟）
TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_EXPIRE_MINUTES", "120"))

# OCR 相关配置：
# - OCR_PROVIDER: auto / paddle / baidu / plain_text / off
# - OCR_REQUIRED: true 时，OCR失败会直接导致上传失败
OCR_PROVIDER = os.getenv("OCR_PROVIDER", "auto").strip().lower()
OCR_REQUIRED = os.getenv("OCR_REQUIRED", "false").strip().lower() == "true"
OCR_FAIL_OPEN = os.getenv("OCR_FAIL_OPEN", "true").strip().lower() == "true"
OCR_TIMEOUT_SECONDS = float(os.getenv("OCR_TIMEOUT_SECONDS", "15"))
OCR_PIPELINE_TIMEOUT_SECONDS = float(os.getenv("OCR_PIPELINE_TIMEOUT_SECONDS", "60"))
OCR_TIMEOUT_COOLDOWN_SECONDS = float(os.getenv("OCR_TIMEOUT_COOLDOWN_SECONDS", "5"))
OCR_WHITE_SLIP_FALLBACK = (
    os.getenv("OCR_WHITE_SLIP_FALLBACK", "false").strip().lower() == "true"
)
WHITE_SLIP_IMAGE_OCR_TIMEOUT_SECONDS = float(
    os.getenv(
        "WHITE_SLIP_IMAGE_OCR_TIMEOUT_SECONDS",
        str(max(30, int(OCR_PIPELINE_TIMEOUT_SECONDS))),
    )
)
OCR_STARTUP_WARMUP_ENABLED = (
    os.getenv("OCR_STARTUP_WARMUP_ENABLED", "true").strip().lower() == "true"
)
OCR_STARTUP_WARMUP_TIMEOUT_SECONDS = float(
    os.getenv("OCR_STARTUP_WARMUP_TIMEOUT_SECONDS", "180")
)
BAIDU_OCR_API_KEY = os.getenv("BAIDU_OCR_API_KEY", "").strip()
BAIDU_OCR_SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "").strip()
BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_GENERAL_OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"

# 税务验真配置（用于电子发票）
# - TAX_VERIFY_URL: 税务验真接口地址
# - TAX_VERIFY_STRICT: true 时，电子发票验真失败会阻断入库
TAX_VERIFY_URL = os.getenv("TAX_VERIFY_URL", "").strip()
TAX_VERIFY_STRICT = os.getenv("TAX_VERIFY_STRICT", "false").strip().lower() == "true"
TAX_VERIFY_TIMEOUT_SECONDS = float(os.getenv("TAX_VERIFY_TIMEOUT_SECONDS", "10"))
FINANCE_SYSTEM_QUERY_URL = os.getenv("FINANCE_SYSTEM_QUERY_URL", "").strip()
FINANCE_SYSTEM_TIMEOUT_SECONDS = float(
    os.getenv("FINANCE_SYSTEM_TIMEOUT_SECONDS", "10")
)
WHITE_SLIP_AI_ENABLED = (
    os.getenv("WHITE_SLIP_AI_ENABLED", "false").strip().lower() == "true"
)
WHITE_SLIP_AI_PROVIDER = os.getenv("WHITE_SLIP_AI_PROVIDER", "zhipu").strip().lower()
WHITE_SLIP_AI_MODEL = os.getenv("WHITE_SLIP_AI_MODEL", "glm-4v").strip()
WHITE_SLIP_AI_API_URL = os.getenv(
    "WHITE_SLIP_AI_API_URL",
    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
).strip()
WHITE_SLIP_AI_API_KEY = os.getenv("WHITE_SLIP_AI_API_KEY", "").strip()
WHITE_SLIP_AI_TIMEOUT_SECONDS = float(os.getenv("WHITE_SLIP_AI_TIMEOUT_SECONDS", "20"))
WHITE_SLIP_AI_VERIFY_TLS = (
    os.getenv("WHITE_SLIP_AI_VERIFY_TLS", "true").strip().lower() == "true"
)
WHITE_SLIP_AI_MAX_IMAGE_BYTES = int(
    os.getenv("WHITE_SLIP_AI_MAX_IMAGE_BYTES", str(5 * 1024 * 1024))
)
WHITE_SLIP_LOCAL_MODEL_ENABLED = (
    os.getenv("WHITE_SLIP_LOCAL_MODEL_ENABLED", "true").strip().lower() == "true"
)
WHITE_SLIP_LOCAL_MODEL_MODULE = os.getenv(
    "WHITE_SLIP_LOCAL_MODEL_MODULE", "white_slip_local_model"
).strip()
WHITE_SLIP_LOCAL_MODEL_FUNCTION = os.getenv(
    "WHITE_SLIP_LOCAL_MODEL_FUNCTION", "predict_white_slip"
).strip()
WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS = float(
    os.getenv("WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS", "25")
)
WHITE_SLIP_FALLBACK_ALWAYS = (
    os.getenv("WHITE_SLIP_FALLBACK_ALWAYS", "false").strip().lower() == "true"
)
WHITE_SLIP_STARTUP_WARMUP_ENABLED = (
    os.getenv("WHITE_SLIP_STARTUP_WARMUP_ENABLED", "true").strip().lower() == "true"
)
FACE_RECOGNITION_ENABLED = (
    os.getenv("FACE_RECOGNITION_ENABLED", "false").strip().lower() == "true"
)
FACE_LOGIN_REQUIRED = (
    os.getenv("FACE_LOGIN_REQUIRED", "false").strip().lower() == "true"
)
FACE_PROVIDER = os.getenv("FACE_PROVIDER", "auto").strip().lower()
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.82"))
FACE_VERIFY_TIMEOUT_SECONDS = float(os.getenv("FACE_VERIFY_TIMEOUT_SECONDS", "10"))
FACE_API_URL = os.getenv("FACE_API_URL", "").strip()
FACE_API_KEY = os.getenv("FACE_API_KEY", "").strip()
FACE_API_VERIFY_TLS = os.getenv("FACE_API_VERIFY_TLS", "true").strip().lower() == "true"
FACE_MAX_IMAGE_BYTES = int(os.getenv("FACE_MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
FACE_LOCAL_MODEL_ENABLED = (
    os.getenv("FACE_LOCAL_MODEL_ENABLED", "false").strip().lower() == "true"
)
FACE_LOCAL_MODEL_MODULE = os.getenv(
    "FACE_LOCAL_MODEL_MODULE", "face_local_model"
).strip()
FACE_LOCAL_MODEL_FUNCTION = os.getenv(
    "FACE_LOCAL_MODEL_FUNCTION", "verify_face_pair"
).strip()
FACE_ALLOW_MOCK_FALLBACK = (
    os.getenv("FACE_ALLOW_MOCK_FALLBACK", "false").strip().lower() == "true"
)

# 审核与权限配置
# ACCOUNTANT_USERS 例： "admin01,accountant01"
ACCOUNTANT_USERS = {
    item.strip()
    for item in os.getenv("ACCOUNTANT_USERS", "admin01,accountant01").split(",")
    if item.strip()
}
VALID_USERS = {
    "admin01": "130102199001011234",
    "accountant01": "130102199002021234",
    "reporter01": "130102199003031234",
}

# 政策咨询配置
# POLICY_AI_PROVIDER: off / gemini / qwen
POLICY_AI_PROVIDER = os.getenv("POLICY_AI_PROVIDER", "off").strip().lower()
POLICY_AI_MODEL = os.getenv("POLICY_AI_MODEL", "qwen-plus")
POLICY_DOCS_DIR = Path(os.getenv("POLICY_DOCS_DIR", "policy_docs")).resolve()
POLICY_TOP_K = int(os.getenv("POLICY_TOP_K", "3"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta/models"
).strip()
QWEN_OPENAI_BASE_URL = os.getenv(
    "QWEN_OPENAI_BASE_URL", "http://127.0.0.1:8001/v1"
).strip()
QWEN_OPENAI_API_KEY = os.getenv("QWEN_OPENAI_API_KEY", "").strip()

# 用户村别映射（用于统计占比）
# USER_VILLAGE_MAP 例: "admin01:第一村,reporter01:第二村"
_raw_village_map = os.getenv(
    "USER_VILLAGE_MAP", "admin01:第一村,accountant01:第一村,reporter01:第二村"
)
USER_VILLAGE_MAP: Dict[str, str] = {}
for pair in _raw_village_map.split(","):
    item = pair.strip()
    if not item or ":" not in item:
        continue
    user, village = item.split(":", 1)
    USER_VILLAGE_MAP[user.strip()] = village.strip() or "未配置村别"


# ============================================================
# 2) ORM 模型定义
#    - Base: ORM 基类
#    - OperationLog: 审计日志
#    - AccessToken: 访问令牌表
#    - PendingUpload: 离线补传队列表
# ============================================================


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类。所有数据表模型均继承自该类。"""

    pass


class OperationLog(Base):
    """
    审计日志表：记录系统关键行为，用于留痕与追溯。
    字段说明：
    - operator: 操作人（用户名）
    - action: 操作描述
    - timestamp: 操作发生时间
    """

    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )


class AccessToken(Base):
    """
    访问令牌表：保存登录后签发的 token，实现可校验、可过期、可撤销。
    字段说明：
    - token: 令牌字符串（唯一）
    - username: 令牌所属用户
    - created_at: 创建时间
    - expires_at: 过期时间
    - revoked_at: 撤销时间（为空表示未撤销）
    """

    __tablename__ = "access_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PendingUpload(Base):
    """
    离线补传队列表：网络中断时把加密后的凭证文件入队，网络恢复后再补传。
    字段说明：
    - original_filename: 原始文件名（已清洗）
    - encrypted_path: 本地加密文件路径
    - status: pending/synced/failed
    - created_at: 入队时间
    - synced_at: 同步完成时间
    - error_message: 失败原因（如果有）
    """

    __tablename__ = "pending_uploads"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending / synced / failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Reimbursement(Base):
    """
    报账单业务表：用于持久化识别结果与审核状态，支撑账务查询。
    核心字段：
    - amount: 金额
    - category: 费用类别
    - reason: 事由
    - status: 待审核/已入账/需村民代表大会决议
    - invoice_code: 发票代码（用于查重）
    - image_path: 凭证文件路径
    - physical_storage_location: 纸质凭证存放位置
    - box_id: 纸质凭证箱号
    """

    __tablename__ = "reimbursements"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="其他")
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="待审核")
    invoice_code: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    image_path: Mapped[str] = mapped_column(String(512), nullable=False)
    voucher_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    source_device: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    verify_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    verify_message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    physical_storage_location: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    box_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    physical_stored_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    physical_stored_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )


class Notification(Base):
    """
    通知公告表：会计发布，报账员可拉取。
    target_role: all / reporter / accountant
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_role: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    is_active: Mapped[int] = mapped_column(default=1, nullable=False)


class FaceProfile(Base):
    """
    人脸档案表：
    - 每个用户保存一份加密后的人脸基准图
    - 登录或独立核验时用作比对参考
    """

    __tablename__ = "face_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    encrypted_face_path: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    is_active: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


# 启动时自动建表（若表已存在则跳过）
Base.metadata.create_all(bind=engine)


def _ensure_sqlite_columns() -> None:
    """
    轻量补列逻辑：仅用于本地 SQLite 演示环境。
    当模型新增字段时，自动执行 ALTER TABLE，避免历史库缺列报错。
    """
    if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        return

    required_columns = {
        "reimbursements": {
            "physical_storage_location": "TEXT",
            "box_id": "TEXT",
            "physical_stored_by": "TEXT",
            "physical_stored_at": "DATETIME",
        }
    }

    try:
        with engine.begin() as conn:
            for table_name, column_map in required_columns.items():
                rows = conn.execute(
                    text(f"PRAGMA table_info('{table_name}')")
                ).fetchall()
                existing_columns = {str(row[1]) for row in rows}
                for column_name, column_type in column_map.items():
                    if column_name in existing_columns:
                        continue
                    conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
                    )
    except Exception:
        # 补列失败不阻断启动，避免影响已有线上流程。
        pass


_ensure_sqlite_columns()

# 准备加密文件目录：
# - pending: 离线待补传
# - synced: 在线上传成功（或补传成功）后存档
os.makedirs("encrypted_storage", exist_ok=True)
os.makedirs("encrypted_storage/pending", exist_ok=True)
os.makedirs("encrypted_storage/synced", exist_ok=True)
os.makedirs("encrypted_storage/faces", exist_ok=True)


# ============================================================
# 3) 工具函数区
#    - 网络探测
#    - 文件名清洗
#    - token 创建与校验
#    - 加密文件写盘
# ============================================================


def check_real_network(host: str = "8.8.8.8", port: int = 53, timeout: int = 2) -> bool:
    """
    检测网络连通性：
    通过 TCP 连接指定 host/port 判断当前网络是否可用。
    返回 True 表示可用，False 表示不可用。
    """
    try:
        socket.setdefaulttimeout(timeout)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def sanitize_filename(name: Optional[str]) -> str:
    """
    文件名安全清洗：
    - 去除路径信息（防路径穿越）
    - 替换斜杠字符
    - 兜底 unknown_file
    """
    raw = name or "unknown_file"
    base = os.path.basename(raw).strip().replace("\\", "_").replace("/", "_")
    return base or "unknown_file"


def create_access_token(db: Session, username: str) -> str:
    """
    创建并持久化访问令牌：
    - 生成高熵随机 token
    - 写入 token 表
    - 返回 token 字符串
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(minutes=TOKEN_EXPIRE_MINUTES)

    row = AccessToken(
        token=token,
        username=username,
        created_at=now,
        expires_at=expires,
        revoked_at=None,
    )
    db.add(row)
    db.commit()
    return token


def verify_access_token(
    db: Session, username: str, access_token: Optional[str]
) -> AccessToken:
    """
    校验访问令牌：
    1) 必须提供 token
    2) token 必须存在且与 username 匹配
    3) token 必须未撤销
    4) token 必须未过期
    校验通过返回 token 对应数据库记录。
    """
    if not access_token:
        raise HTTPException(status_code=403, detail="拒绝访问：缺少令牌")

    stmt = select(AccessToken).where(
        AccessToken.token == access_token,
        AccessToken.username == username,
        AccessToken.revoked_at.is_(None),
    )
    token_row = db.execute(stmt).scalar_one_or_none()

    if token_row is None:
        raise HTTPException(status_code=401, detail="令牌无效或用户不匹配")

    if token_row.expires_at < datetime.now():
        raise HTTPException(status_code=401, detail="令牌已过期，请重新登录")

    return token_row


def write_encrypted_file(
    target_dir: str, original_filename: Optional[str], encrypted_content: bytes
) -> str:
    """
    把加密内容写入本地文件：
    - 文件名添加时间戳，避免覆盖
    - 返回最终保存路径
    """
    safe_name = sanitize_filename(original_filename)
    unique = datetime.now().strftime("%Y%m%d%H%M%S%f")
    file_path = os.path.join(target_dir, f"{unique}_{safe_name}.enc")
    with open(file_path, "wb") as f:
        f.write(encrypted_content)
    return file_path


def _decode_image_base64(image_base64: Optional[str]) -> bytes:
    raw = str(image_base64 or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="缺少人脸图片 base64 数据")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = re.sub(r"\s+", "", raw)
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="人脸图片 base64 格式不正确")
    if not image_bytes:
        raise HTTPException(status_code=422, detail="人脸图片数据为空")
    if len(image_bytes) > FACE_MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"人脸图片过大，超过限制 {FACE_MAX_IMAGE_BYTES} 字节",
        )
    return image_bytes


def _load_face_profile(db: Session, username: str) -> Optional[FaceProfile]:
    return (
        db.execute(select(FaceProfile).where(FaceProfile.username == username))
        .scalars()
        .first()
    )


def _load_face_reference_bytes(profile: FaceProfile) -> bytes:
    try:
        with open(profile.encrypted_face_path, "rb") as f:
            encrypted = f.read()
    except FileNotFoundError:
        raise RuntimeError("人脸基准图文件不存在，请重新注册")
    except Exception as exc:
        raise RuntimeError(f"读取人脸基准图失败: {exc}")
    try:
        return cipher.decrypt(encrypted)
    except Exception as exc:
        raise RuntimeError(f"解密人脸基准图失败: {exc}")


def _normalize_face_verify_result(
    payload: Dict[str, Any], provider: str
) -> Dict[str, Any]:
    source = payload or {}
    if isinstance(source.get("result"), dict):
        source = source["result"]
    if isinstance(source.get("data"), dict):
        source = source["data"]

    score = None
    score_raw = source.get("score")
    if isinstance(score_raw, (int, float)):
        score = float(score_raw)
    elif isinstance(score_raw, str):
        try:
            score = float(score_raw.strip())
        except ValueError:
            score = None

    matched: Optional[bool] = None
    matched_raw = source.get("matched")
    if isinstance(matched_raw, bool):
        matched = matched_raw
    elif isinstance(matched_raw, str):
        low = matched_raw.strip().lower()
        if low in {"true", "1", "yes", "y"}:
            matched = True
        elif low in {"false", "0", "no", "n"}:
            matched = False

    if matched is None and score is not None:
        matched = score >= FACE_MATCH_THRESHOLD
    if matched is None:
        raise RuntimeError("人脸识别结果缺少 matched/score 字段")
    if score is None:
        score = 1.0 if matched else 0.0

    message = str(source.get("message") or "").strip() or None
    return {
        "status": "ok",
        "provider": provider,
        "matched": bool(matched),
        "score": float(score),
        "threshold": FACE_MATCH_THRESHOLD,
        "message": message,
    }


def _coerce_face_raw_result(raw_result: Any) -> Dict[str, Any]:
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, bool):
        return {"matched": raw_result}
    if isinstance(raw_result, (int, float)):
        return {"score": float(raw_result)}
    if isinstance(raw_result, (list, tuple)) and raw_result:
        first = raw_result[0]
        second = raw_result[1] if len(raw_result) > 1 else None
        payload: Dict[str, Any] = {}
        if isinstance(first, bool):
            payload["matched"] = first
            if isinstance(second, (int, float)):
                payload["score"] = float(second)
            elif isinstance(second, str):
                payload["message"] = second
            return payload
        if isinstance(first, (int, float)):
            payload["score"] = float(first)
            if isinstance(second, bool):
                payload["matched"] = second
            elif isinstance(second, str):
                payload["message"] = second
            return payload
    raise RuntimeError(
        "本地人脸模型返回值不支持，请返回 dict/bool/float/tuple(list) 之一"
    )


def _invoke_face_predictor(
    predictor: Any, reference_bytes: bytes, probe_bytes: bytes, username: str
) -> Dict[str, Any]:
    reference_path = ""
    probe_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_ref:
            tmp_ref.write(reference_bytes)
            reference_path = tmp_ref.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_probe:
            tmp_probe.write(probe_bytes)
            probe_path = tmp_probe.name

        call_attempts = [
            lambda: predictor(
                reference_image_bytes=reference_bytes,
                probe_image_bytes=probe_bytes,
                username=username,
            ),
            lambda: predictor(
                reference_bytes=reference_bytes,
                probe_bytes=probe_bytes,
                username=username,
            ),
            lambda: predictor(
                reference_image_path=reference_path,
                probe_image_path=probe_path,
                username=username,
            ),
            lambda: predictor(
                reference_path=reference_path,
                probe_path=probe_path,
                username=username,
            ),
            lambda: predictor(image1_path=reference_path, image2_path=probe_path),
            lambda: predictor(image1=reference_path, image2=probe_path),
            lambda: predictor(reference_bytes, probe_bytes, username),
            lambda: predictor(reference_bytes, probe_bytes),
            lambda: predictor(reference_path, probe_path, username),
            lambda: predictor(reference_path, probe_path),
        ]
        last_error: Optional[Exception] = None
        for call in call_attempts:
            try:
                raw = call()
                return _coerce_face_raw_result(raw)
            except TypeError as exc:
                last_error = exc
                continue
        if last_error:
            raise RuntimeError(f"本地人脸模型函数签名不匹配: {last_error}")
        raise RuntimeError("本地人脸模型调用失败")
    finally:
        for path in (reference_path, probe_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def _call_face_local_model(
    reference_bytes: bytes, probe_bytes: bytes, username: str
) -> Dict[str, Any]:
    if not FACE_LOCAL_MODEL_ENABLED:
        return {
            "status": "skipped",
            "provider": "local_model",
            "reason": "未启用本地人脸模型（FACE_LOCAL_MODEL_ENABLED=false）",
        }
    try:
        module = importlib.import_module(FACE_LOCAL_MODEL_MODULE)
        predictor = getattr(module, FACE_LOCAL_MODEL_FUNCTION, None)
        if predictor is None:
            return {
                "status": "failed",
                "provider": "local_model",
                "error": (
                    "未找到本地人脸函数: "
                    f"{FACE_LOCAL_MODEL_MODULE}.{FACE_LOCAL_MODEL_FUNCTION}"
                ),
            }
        raw_result = _invoke_face_predictor(
            predictor,
            reference_bytes=reference_bytes,
            probe_bytes=probe_bytes,
            username=username,
        )
        return _normalize_face_verify_result(raw_result, provider="local_model")
    except Exception as exc:
        return {"status": "failed", "provider": "local_model", "error": str(exc)}


def _call_face_api(
    reference_bytes: bytes, probe_bytes: bytes, username: str
) -> Dict[str, Any]:
    if not FACE_API_URL:
        return {"status": "skipped", "provider": "api", "reason": "FACE_API_URL 未配置"}
    headers = {"Content-Type": "application/json"}
    if FACE_API_KEY:
        headers["Authorization"] = f"Bearer {FACE_API_KEY}"
    payload = {
        "username": username,
        "reference_image_base64": base64.b64encode(reference_bytes).decode("utf-8"),
        "probe_image_base64": base64.b64encode(probe_bytes).decode("utf-8"),
    }
    try:
        response = httpx.post(
            FACE_API_URL,
            json=payload,
            headers=headers,
            timeout=FACE_VERIFY_TIMEOUT_SECONDS,
            verify=FACE_API_VERIFY_TLS,
        )
        response.raise_for_status()
        json_payload = response.json()
        if not isinstance(json_payload, dict):
            raise RuntimeError("人脸 API 返回内容不是 JSON 对象")
        return _normalize_face_verify_result(json_payload, provider="api")
    except Exception as exc:
        return {"status": "failed", "provider": "api", "error": str(exc)}


def _verify_face_pair(
    reference_bytes: bytes, probe_bytes: bytes, username: str
) -> Dict[str, Any]:
    if not FACE_RECOGNITION_ENABLED:
        return {
            "status": "failed",
            "provider": "off",
            "error": "人脸识别未启用（FACE_RECOGNITION_ENABLED=false）",
        }
    if not reference_bytes or not probe_bytes:
        return {"status": "failed", "provider": "off", "error": "人脸图片数据为空"}

    provider = FACE_PROVIDER
    errors: List[str] = []

    if provider in {"auto", "local"}:
        local_result = _call_face_local_model(reference_bytes, probe_bytes, username)
        if local_result.get("status") == "ok":
            return local_result
        errors.append(
            str(
                local_result.get("error")
                or local_result.get("reason")
                or "local_failed"
            )
        )
        if provider == "local":
            return {
                "status": "failed",
                "provider": "local_model",
                "error": errors[-1],
            }

    if provider in {"auto", "api"}:
        api_result = _call_face_api(reference_bytes, probe_bytes, username)
        if api_result.get("status") == "ok":
            return api_result
        errors.append(
            str(api_result.get("error") or api_result.get("reason") or "api_failed")
        )
        if provider == "api":
            return {"status": "failed", "provider": "api", "error": errors[-1]}

    allow_mock = provider == "mock" or (provider == "auto" and FACE_ALLOW_MOCK_FALLBACK)
    if allow_mock:
        ref_hash = hashlib.sha256(reference_bytes).hexdigest()
        probe_hash = hashlib.sha256(probe_bytes).hexdigest()
        matched = ref_hash == probe_hash
        return {
            "status": "ok",
            "provider": "mock",
            "matched": matched,
            "score": 1.0 if matched else 0.0,
            "threshold": FACE_MATCH_THRESHOLD,
            "message": "mock 模式使用图像哈希一致性校验",
        }

    if provider == "auto":
        return {
            "status": "failed",
            "provider": "auto",
            "error": "本地模型与API都未完成可用识别，且未启用 mock 回退",
            "details": errors,
        }

    return {
        "status": "failed",
        "provider": provider or "unknown",
        "error": "不支持的 FACE_PROVIDER 配置",
        "details": errors,
    }


# OCR 引擎缓存
_paddle_ocr_instance = None
_baidu_token_cache: Dict[str, Any] = {
    "access_token": None,
    "expires_at": datetime.min,
}
_ocr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-worker-0")
_white_slip_model_executor = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="white-slip-worker-0"
)
_ocr_executor_lock = threading.Lock()
_white_slip_model_executor_lock = threading.Lock()
_ocr_executor_generation = 0
_white_slip_executor_generation = 0
_ocr_timeout_until = datetime.min


def _reset_ocr_executor() -> None:
    """
    OCR超时后，旧线程可能长期阻塞（例如模型加载卡死）。
    这里重建执行器，避免后续请求排队到失活线程上。
    """
    global _ocr_executor, _ocr_executor_generation
    with _ocr_executor_lock:
        old_executor = _ocr_executor
        _ocr_executor_generation += 1
        _ocr_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"ocr-worker-{_ocr_executor_generation}",
        )
    try:
        old_executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def _reset_white_slip_executor() -> None:
    """
    白条本地模型超时后重建执行器，防止执行线程被长期任务占满。
    """
    global _white_slip_model_executor, _white_slip_executor_generation
    with _white_slip_model_executor_lock:
        old_executor = _white_slip_model_executor
        _white_slip_executor_generation += 1
        _white_slip_model_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"white-slip-worker-{_white_slip_executor_generation}",
        )
    try:
        old_executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def _to_float(text: str) -> Optional[float]:
    cleaned = text.replace(",", "").replace("¥", "").replace("￥", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip().replace("年", "-").replace("月", "-").replace("日", "")
    value = value.replace("/", "-").replace(".", "-")
    value = re.sub(r"-{2,}", "-", value)
    parts = [p for p in value.split("-") if p]
    if len(parts) != 3:
        return None
    if not all(p.isdigit() for p in parts):
        return None
    year, month, day = parts
    if len(year) != 4:
        return None
    try:
        return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def extract_voucher_core_fields(full_text: str) -> Dict[str, Any]:
    invoice_code = None
    amount = None
    date_value = None

    invoice_match = re.search(
        r"(?:发票代码|票据代码)\s*[:：]?\s*([A-Za-z0-9]{6,30})", full_text
    )
    if invoice_match:
        invoice_code = invoice_match.group(1).strip()

    date_patterns = [
        r"(20\d{2}[年/\-.]\d{1,2}[月/\-.]\d{1,2}日?)",
        r"(19\d{2}[年/\-.]\d{1,2}[月/\-.]\d{1,2}日?)",
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, full_text)
        if date_match:
            date_value = _normalize_date(date_match.group(1))
            if date_value:
                break

    labeled_amount = re.search(
        r"(?:金额|价税合计|合计金额|小写|实付金额|人民币)\D{0,6}(?:¥|￥)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})|[0-9]+(?:\.\d{1,2}))",
        full_text,
    )
    if labeled_amount:
        amount = _to_float(labeled_amount.group(1))

    if amount is None:
        candidates = re.findall(
            r"(?:¥|￥)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})|[0-9]+\.\d{1,2})",
            full_text,
        )
        values = [v for v in (_to_float(item) for item in candidates) if v is not None]
        if values:
            amount = max(values)

    return {
        "amount": amount,
        "date": date_value,
        "invoice_code": invoice_code,
    }


_ID_CARD_LABEL_NUMBER_PATTERN = re.compile(
    r"(?:\u516c\u6c11\u8eab\u4efd\u53f7\u7801|\u8eab\u4efd\u8bc1\u53f7\u7801|\u8eab\u4efd\u8bc1\u53f7|\u8eab\u4efd\u53f7\u7801)\s*[:\uff1a]?\s*([0-9Xx\s]{15,24})"
)
_ID_CARD_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d{17}[0-9Xx]|\d{15})(?!\d)")
_ID_CARD_LABEL_NAME_PATTERN = re.compile(
    r"(?:\u59d3\u540d)\s*[:\uff1a]?\s*([^\n:\uff1a]{1,20})"
)
_ID_CARD_KEYWORDS = (
    "\u5c45\u6c11\u8eab\u4efd\u8bc1",
    "\u4e2d\u534e\u4eba\u6c11\u5171\u548c\u56fd",
    "\u516c\u6c11\u8eab\u4efd\u53f7\u7801",
    "\u7b7e\u53d1\u673a\u5173",
    "\u6709\u6548\u671f\u9650",
)
_ID_CARD_NAME_BLOCKLIST = (
    "\u6027\u522b",
    "\u6c11\u65cf",
    "\u51fa\u751f",
    "\u4f4f\u5740",
    "\u516c\u6c11",
    "\u8eab\u4efd",
    "\u53f7\u7801",
    "\u5e74",
    "\u6708",
    "\u65e5",
)


def _normalize_id_number(raw: str) -> Optional[str]:
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9Xx]", "", raw).upper()
    if re.fullmatch(r"\d{17}[0-9X]", cleaned):
        return cleaned
    if re.fullmatch(r"\d{15}", cleaned):
        return cleaned
    return None


def _normalize_id_name(raw: str) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()
    value = re.split(r"[\n,，;；]", value)[0].strip()
    value = re.sub(r"\s+", "", value)
    value = value.strip(".:：")
    if not value:
        return None
    if any(token in value for token in _ID_CARD_NAME_BLOCKLIST):
        return None
    if re.fullmatch(r"[\u4e00-\u9fa5\u00b7]{2,16}", value):
        return value
    if re.fullmatch(r"[A-Za-z][A-Za-z ]{1,30}", value):
        return value.strip()
    return None


def _mask_id_number(id_number: Optional[str]) -> Optional[str]:
    normalized = _normalize_id_number(id_number or "")
    if not normalized:
        return None
    if len(normalized) == 18:
        return f"{normalized[:6]}********{normalized[-4:]}"
    return f"{normalized[:4]}*******{normalized[-4:]}"


def extract_id_card_fields(text_lines: List[str], full_text: str) -> Dict[str, Any]:
    id_number = None
    name = None

    labeled_number = _ID_CARD_LABEL_NUMBER_PATTERN.search(full_text)
    if labeled_number:
        id_number = _normalize_id_number(labeled_number.group(1))

    if not id_number:
        compact_text = re.sub(r"\s+", "", full_text)
        number_match = _ID_CARD_NUMBER_PATTERN.search(compact_text)
        if number_match:
            id_number = _normalize_id_number(number_match.group(1))

    labeled_name = _ID_CARD_LABEL_NAME_PATTERN.search(full_text)
    if labeled_name:
        name = _normalize_id_name(labeled_name.group(1))

    if not name:
        for index, line in enumerate(text_lines):
            match = re.search(r"(?:\u59d3\u540d)\s*[:\uff1a]?\s*(.*)", line)
            if not match:
                continue
            candidate = match.group(1).strip()
            if not candidate and index + 1 < len(text_lines):
                candidate = text_lines[index + 1].strip()
            parsed_name = _normalize_id_name(candidate)
            if parsed_name:
                name = parsed_name
                break

    has_keyword = any(keyword in full_text for keyword in _ID_CARD_KEYWORDS)
    is_id_card = bool((id_number and (has_keyword or name)) or (has_keyword and name))

    return {
        "name": name,
        "id_number": id_number,
        "is_id_card": is_id_card,
    }


_RECEIPT_LABEL_NUMBER_PATTERN = re.compile(
    r"(?:\u6536\u636e(?:\u7f16?\u53f7)?|\u7968\u636e(?:\u7f16?\u53f7)?|\u51ed\u8bc1\u53f7)\s*[:\uff1a]?\s*([A-Za-z0-9\-]{4,40})"
)
_RECEIPT_PAYER_PATTERN = re.compile(
    r"(?:\u4ea4\u6b3e\u4eba|\u4ed8\u6b3e\u4eba|\u7f34\u6b3e\u4eba)\s*[:\uff1a]?\s*([^\n:\uff1a]{1,40})"
)
_RECEIPT_PAYEE_PATTERN = re.compile(
    r"(?:\u6536\u6b3e\u4eba|\u6536\u6b3e\u5355\u4f4d|\u6536\u6b3e\u65b9)\s*[:\uff1a]?\s*([^\n:\uff1a]{1,60})"
)
_RECEIPT_KEYWORDS = (
    "\u6536\u6b3e\u6536\u636e",
    "\u6536\u636e",
    "\u6536\u6b3e\u51ed\u8bc1",
    "\u975e\u7a0e\u52a1\u7968\u636e",
    "\u8d22\u653f\u7968\u636e",
)


def _normalize_receipt_party(raw: str) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()
    value = re.split(r"[\n,，;；]", value)[0].strip()
    value = value.strip(".:：")
    if not value:
        return None
    return value[:60]


def extract_receipt_fields(text_lines: List[str], full_text: str) -> Dict[str, Any]:
    receipt_number = None
    payer = None
    payee = None

    number_match = _RECEIPT_LABEL_NUMBER_PATTERN.search(full_text)
    if number_match:
        receipt_number = number_match.group(1).strip()

    payer_match = _RECEIPT_PAYER_PATTERN.search(full_text)
    if payer_match:
        payer = _normalize_receipt_party(payer_match.group(1))

    payee_match = _RECEIPT_PAYEE_PATTERN.search(full_text)
    if payee_match:
        payee = _normalize_receipt_party(payee_match.group(1))

    has_keyword = any(keyword in full_text for keyword in _RECEIPT_KEYWORDS)
    has_amount_marker = bool(
        re.search(
            r"(?:\u91d1\u989d|\u5408\u8ba1|\u4eba\u6c11\u5e01|[¥￥]\s*[0-9])", full_text
        )
    )
    is_receipt = has_keyword and bool(receipt_number or has_amount_marker)

    return {
        "receipt_number": receipt_number,
        "payer": payer,
        "payee": payee,
        "is_receipt": is_receipt,
    }


def _empty_ocr_fields() -> Dict[str, Any]:
    return {
        "amount": None,
        "date": None,
        "invoice_code": None,
        "id_name": None,
        "id_number": None,
        "receipt_number": None,
    }


def _split_possible_names(raw_value: str) -> List[str]:
    cleaned = raw_value.strip()
    cleaned = re.sub(r"[，,;；。]", " ", cleaned)
    parts = re.split(r"[、/\s]+", cleaned)
    names: List[str] = []
    for part in parts:
        token = part.strip()
        if re.fullmatch(r"[A-Za-z\u4e00-\u9fa5·]{2,16}", token):
            names.append(token)
    return names


def extract_white_slip_fields(text_lines: List[str], full_text: str) -> Dict[str, Any]:
    reason = None
    signer_candidates: List[str] = []

    reason_patterns = [
        r"(?:报销)?事由\s*[:：]?\s*([^\n]{2,80})",
        r"(?:报销)?用途\s*[:：]?\s*([^\n]{2,80})",
        r"(?:报销)?内容\s*[:：]?\s*([^\n]{2,80})",
    ]
    for pattern in reason_patterns:
        match = re.search(pattern, full_text)
        if match:
            reason = match.group(1).strip(" ：:;；。")
            break

    if not reason:
        for line in text_lines:
            compact = line.strip()
            if any(key in compact for key in ("事由", "用途", "报销", "说明")):
                if "：" in compact:
                    reason = compact.split("：", 1)[1].strip()
                elif ":" in compact:
                    reason = compact.split(":", 1)[1].strip()
                else:
                    reason = compact
                if reason:
                    break

    signer_pattern = re.compile(
        r"(?:签字|签名|经手人|报销人|审批人|领款人|收款人|负责人)\s*[:：]?\s*([A-Za-z\u4e00-\u9fa5·、/ ]{2,40})"
    )
    for raw in signer_pattern.findall(full_text):
        signer_candidates.extend(_split_possible_names(raw))

    if not signer_candidates:
        for line in text_lines:
            if "签字" in line or "签名" in line:
                signer_candidates.extend(re.findall(r"[\u4e00-\u9fa5·]{2,4}", line))

    deduplicated_signers: List[str] = []
    signer_labels = {
        "签字",
        "签名",
        "经手人",
        "报销人",
        "审批人",
        "领款人",
        "收款人",
        "负责人",
    }
    for name in signer_candidates:
        if name not in signer_labels and name not in deduplicated_signers:
            deduplicated_signers.append(name)

    return {
        "reason": reason,
        "signers": deduplicated_signers,
    }


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


def _build_white_slip_ai_prompt(text_lines: List[str], full_text: str) -> str:
    joined = "\n".join(text_lines[:80]).strip()
    return (
        "请从中文白条报销凭证中提取结构化字段。\n"
        "只返回JSON对象，不要返回Markdown。\n"
        "字段: reason, signers, payer, payee, amount, date, slip_type。\n"
        "规则:\n"
        "- signers 必须是字符串数组。\n"
        "- amount 必须是数字或 null。\n"
        "- date 格式必须是 YYYY-MM-DD 或 null。\n"
        "- slip_type 必须是 white_slip、loan_note、receipt_note、other 之一。\n"
        f"OCR文本行:\n{joined}\n\n"
        f"OCR全文:\n{full_text}"
    )


def _normalize_white_slip_ai_fields(ai_payload: Dict[str, Any]) -> Dict[str, Any]:
    def _first_non_empty(*values: Any) -> Optional[str]:
        for item in values:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                return text
        return None

    reason = _first_non_empty(
        ai_payload.get("reason"),
        ai_payload.get("expense_reason"),
        ai_payload.get("event_reason"),
        ai_payload.get("\u4e8b\u7531"),
    )

    raw_signers = (
        ai_payload.get("signers")
        or ai_payload.get("signatories")
        or ai_payload.get("\u7b7e\u5b57\u4eba")
        or []
    )
    signer_candidates: List[str] = []
    if isinstance(raw_signers, str):
        signer_candidates.extend(_split_possible_names(raw_signers))
    elif isinstance(raw_signers, list):
        for item in raw_signers:
            signer_candidates.extend(_split_possible_names(str(item)))

    signers: List[str] = []
    for signer in signer_candidates:
        if signer not in signers:
            signers.append(signer)

    payer = _first_non_empty(
        ai_payload.get("payer"),
        ai_payload.get("payment_party"),
        ai_payload.get("\u4ed8\u6b3e\u4eba"),
        ai_payload.get("\u4ea4\u6b3e\u4eba"),
    )
    payee = _first_non_empty(
        ai_payload.get("payee"),
        ai_payload.get("receipt_party"),
        ai_payload.get("\u6536\u6b3e\u4eba"),
        ai_payload.get("\u6536\u6b3e\u65b9"),
    )

    amount_raw = _first_non_empty(
        ai_payload.get("amount"),
        ai_payload.get("total_amount"),
        ai_payload.get("\u91d1\u989d"),
    )
    amount = _to_float(amount_raw) if amount_raw is not None else None

    date_raw = _first_non_empty(
        ai_payload.get("date"),
        ai_payload.get("expense_date"),
        ai_payload.get("\u65e5\u671f"),
    )
    date_value = None
    if date_raw:
        date_value = _normalize_date(date_raw) or _normalize_compact_date(date_raw)

    slip_type = _first_non_empty(
        ai_payload.get("slip_type"),
        ai_payload.get("document_type"),
        ai_payload.get("\u5355\u636e\u7c7b\u578b"),
    )
    normalized_slip_type = (slip_type or "white_slip").strip().lower()
    if normalized_slip_type not in {"white_slip", "loan_note", "receipt_note", "other"}:
        normalized_slip_type = "white_slip"

    return {
        "reason": reason,
        "signers": signers,
        "payer": payer,
        "payee": payee,
        "amount": amount,
        "date": date_value,
        "slip_type": normalized_slip_type,
    }


def _merge_white_slip_fields(
    rule_fields: Dict[str, Any], ai_fields: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    merged = dict(rule_fields or {})
    ai_payload = ai_fields or {}

    if ai_payload.get("reason"):
        merged["reason"] = ai_payload.get("reason")

    merged_signers: List[str] = []
    for source in (ai_payload.get("signers", []), merged.get("signers", [])):
        for item in source:
            name = str(item).strip()
            if name and name not in merged_signers:
                merged_signers.append(name)
    merged["signers"] = merged_signers

    for key in ("payer", "payee", "amount", "date", "slip_type"):
        value = ai_payload.get(key)
        if value not in (None, "", []):
            merged[key] = value
    return merged


def _call_white_slip_local_model(
    *,
    text_lines: List[str],
    full_text: str,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    if not WHITE_SLIP_LOCAL_MODEL_ENABLED:
        return {
            "status": "skipped",
            "provider": "local_model",
            "reason": "未启用本地白条模型（WHITE_SLIP_LOCAL_MODEL_ENABLED=false）",
        }
    if not WHITE_SLIP_LOCAL_MODEL_MODULE:
        return {
            "status": "failed",
            "provider": "local_model",
            "error": "本地白条模型模块未配置（WHITE_SLIP_LOCAL_MODEL_MODULE）",
        }
    if not WHITE_SLIP_LOCAL_MODEL_FUNCTION:
        return {
            "status": "failed",
            "provider": "local_model",
            "error": "本地白条模型函数未配置（WHITE_SLIP_LOCAL_MODEL_FUNCTION）",
        }

    try:
        module = importlib.import_module(WHITE_SLIP_LOCAL_MODEL_MODULE)
        predictor = getattr(module, WHITE_SLIP_LOCAL_MODEL_FUNCTION, None)
        if not callable(predictor):
            raise RuntimeError(
                "未找到本地预测函数: "
                f"{WHITE_SLIP_LOCAL_MODEL_MODULE}.{WHITE_SLIP_LOCAL_MODEL_FUNCTION}"
            )

        timeout_seconds = max(1.0, float(WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS))
        try:
            future = _white_slip_model_executor.submit(
                predictor,
                text_lines=text_lines,
                full_text=full_text,
                file_bytes=file_bytes,
                filename=filename,
            )
        except RuntimeError:
            # 执行器可能因超时重建/关闭处于不可用状态，重建后重试一次。
            _reset_white_slip_executor()
            future = _white_slip_model_executor.submit(
                predictor,
                text_lines=text_lines,
                full_text=full_text,
                file_bytes=file_bytes,
                filename=filename,
            )
        try:
            raw_result = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            _reset_white_slip_executor()
            return {
                "status": "failed",
                "provider": "local_model",
                "model": (
                    f"{WHITE_SLIP_LOCAL_MODEL_MODULE}.{WHITE_SLIP_LOCAL_MODEL_FUNCTION}"
                ),
                "error": (
                    f"鏈湴鐧芥潯妯″瀷鎵ц瓒呮椂锛宼imeout={timeout_seconds:.0f}s"
                ),
            }
        if isinstance(raw_result, dict):
            payload = raw_result
        else:
            payload = _extract_first_json_object(str(raw_result)) or {}
        if not isinstance(payload, dict) or not payload:
            raise RuntimeError("本地预测函数返回为空")

        fields = _normalize_white_slip_ai_fields(payload)
        return {
            "status": "ok",
            "provider": "local_model",
            "model": (
                f"{WHITE_SLIP_LOCAL_MODEL_MODULE}.{WHITE_SLIP_LOCAL_MODEL_FUNCTION}"
            ),
            "fields": fields,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "provider": "local_model",
            "model": (
                f"{WHITE_SLIP_LOCAL_MODEL_MODULE}.{WHITE_SLIP_LOCAL_MODEL_FUNCTION}"
            ),
            "error": str(exc)[:500],
        }


def _call_white_slip_ai(
    *,
    text_lines: List[str],
    full_text: str,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    if not WHITE_SLIP_AI_ENABLED:
        return {
            "status": "skipped",
            "provider": WHITE_SLIP_AI_PROVIDER,
            "reason": "未启用白条AI模型（WHITE_SLIP_AI_ENABLED=false）",
        }
    if not WHITE_SLIP_AI_API_KEY:
        return {
            "status": "failed",
            "provider": WHITE_SLIP_AI_PROVIDER,
            "error": "白条AI密钥未配置（WHITE_SLIP_AI_API_KEY）",
        }
    if not WHITE_SLIP_AI_API_URL:
        return {
            "status": "failed",
            "provider": WHITE_SLIP_AI_PROVIDER,
            "error": "白条AI接口地址未配置（WHITE_SLIP_AI_API_URL）",
        }

    try:
        prompt = _build_white_slip_ai_prompt(text_lines, full_text)
        content_items: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]

        if file_bytes:
            if len(file_bytes) > WHITE_SLIP_AI_MAX_IMAGE_BYTES:
                raise RuntimeError(
                    "白条图片大小超过 WHITE_SLIP_AI_MAX_IMAGE_BYTES 限制"
                )
            safe_name = sanitize_filename(filename or "white_slip.jpg")
            mime_type = mimetypes.guess_type(safe_name)[0] or "image/jpeg"
            if not str(mime_type).startswith("image/"):
                raise RuntimeError("白条AI仅支持图片文件输入")
            image_b64 = base64.b64encode(file_bytes).decode("utf-8")
            content_items.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                }
            )

        payload = {
            "model": WHITE_SLIP_AI_MODEL,
            "messages": [{"role": "user", "content": content_items}],
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {WHITE_SLIP_AI_API_KEY}",
            "Content-Type": "application/json",
        }
        response = httpx.post(
            WHITE_SLIP_AI_API_URL,
            json=payload,
            headers=headers,
            timeout=WHITE_SLIP_AI_TIMEOUT_SECONDS,
            verify=WHITE_SLIP_AI_VERIFY_TLS,
        )
        response.raise_for_status()
        result = response.json()

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"白条AI返回内容为空: {result}")
        content = (((choices[0] or {}).get("message")) or {}).get("content", "")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("text"):
                    text_parts.append(str(block.get("text")))
            content = "\n".join(text_parts)

        json_payload = _extract_first_json_object(str(content))
        if not json_payload:
            raise RuntimeError("白条AI响应不是有效JSON")

        fields = _normalize_white_slip_ai_fields(json_payload)
        return {
            "status": "ok",
            "provider": WHITE_SLIP_AI_PROVIDER,
            "model": WHITE_SLIP_AI_MODEL,
            "fields": fields,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "provider": WHITE_SLIP_AI_PROVIDER,
            "model": WHITE_SLIP_AI_MODEL,
            "error": str(exc)[:500],
        }


def build_white_slip_structured_payload(
    text_lines: List[str],
    full_text: str,
    core_fields: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    rule_fields = extract_white_slip_fields(text_lines, full_text)
    local_model_result = _call_white_slip_local_model(
        text_lines=text_lines,
        full_text=full_text,
        file_bytes=file_bytes,
        filename=filename,
    )
    ai_result = _call_white_slip_ai(
        text_lines=text_lines,
        full_text=full_text,
        file_bytes=file_bytes,
        filename=filename,
    )

    local_fields = (
        local_model_result.get("fields", {})
        if local_model_result.get("status") == "ok"
        else {}
    )
    ai_fields = ai_result.get("fields", {}) if ai_result.get("status") == "ok" else {}
    merged_fields = _merge_white_slip_fields(rule_fields, ai_fields)
    merged_fields = _merge_white_slip_fields(merged_fields, local_fields)

    ai_amount = merged_fields.get("amount")
    if core_fields.get("amount") is None and isinstance(ai_amount, (int, float)):
        core_fields["amount"] = float(ai_amount)
    if not core_fields.get("date") and merged_fields.get("date"):
        core_fields["date"] = merged_fields.get("date")

    standardized = standardize_white_slip_document(
        text_lines, full_text, core_fields, merged_fields
    )
    if not standardized.get("payer") and merged_fields.get("payer"):
        standardized["payer"] = merged_fields.get("payer")
    if not standardized.get("payee") and merged_fields.get("payee"):
        standardized["payee"] = merged_fields.get("payee")
    if merged_fields.get("slip_type"):
        standardized["slip_type"] = merged_fields.get("slip_type")

    return {
        "white_slip": merged_fields,
        "white_slip_standard": standardized,
        "white_slip_ai": ai_result,
        "white_slip_local_model": local_model_result,
    }


_WHITE_SLIP_TYPE_KEYWORDS = (
    "\u767d\u6761",
    "\u501f\u6761",
    "\u6536\u6761",
    "\u9886\u6761",
    "\u6b20\u6761",
)


def _extract_party_value(
    full_text: str, text_lines: List[str], labels: List[str]
) -> Optional[str]:
    label_pattern = "|".join(re.escape(item) for item in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:\uff1a]?\s*([^\n:\uff1a]{{1,60}})", full_text
    )
    if match:
        value = match.group(1).strip().strip(".:：")
        if value:
            return value[:60]

    for line in text_lines:
        compact = line.strip()
        for label in labels:
            if label not in compact:
                continue
            parts = re.split(r"[:\uff1a]", compact, maxsplit=1)
            if len(parts) == 2:
                value = parts[1].strip().strip(".:：")
                if value:
                    return value[:60]
    return None


def standardize_white_slip_document(
    text_lines: List[str],
    full_text: str,
    core_fields: Dict[str, Any],
    white_slip_fields: Dict[str, Any],
) -> Dict[str, Any]:
    slip_type = "white_slip"
    for keyword in _WHITE_SLIP_TYPE_KEYWORDS:
        if keyword in full_text:
            slip_type = keyword
            break

    amount_value = core_fields.get("amount")
    amount = float(amount_value) if isinstance(amount_value, (int, float)) else None
    date_value = core_fields.get("date")
    reason = (white_slip_fields.get("reason") or "").strip() or None
    signers = white_slip_fields.get("signers", []) or []
    signer_count = len(signers)

    payer = _extract_party_value(
        full_text,
        text_lines,
        [
            "\u4ea4\u6b3e\u4eba",
            "\u4ed8\u6b3e\u4eba",
            "\u51fa\u6b3e\u4eba",
            "\u501f\u6b3e\u4eba",
        ],
    )
    payee = _extract_party_value(
        full_text,
        text_lines,
        [
            "\u6536\u6b3e\u4eba",
            "\u6536\u6b3e\u5355\u4f4d",
            "\u6536\u6b3e\u65b9",
            "\u6536\u6b3e\u4eba\u5458",
        ],
    )

    missing_fields: List[str] = []
    if not reason:
        missing_fields.append("reason")
    if amount is None:
        missing_fields.append("amount")
    if not date_value:
        missing_fields.append("date")
    if signer_count < 2:
        missing_fields.append("signers>=2")

    status = "ready" if not missing_fields else "incomplete"
    return {
        "slip_type": slip_type,
        "reason": reason,
        "amount": amount,
        "date": date_value,
        "payer": payer,
        "payee": payee,
        "signers": signers,
        "signer_count": signer_count,
        "required_signer_count": 2,
        "missing_fields": missing_fields,
        "status": status,
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


def _get_paddle_ocr_instance():
    global _paddle_ocr_instance
    if _paddle_ocr_instance is None:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError("未安装 PaddleOCR，请先 pip install paddleocr") from exc
        _paddle_ocr_instance = PaddleOCR(
            lang="ch",
            enable_hpi=False,
            enable_mkldnn=False,
            enable_cinn=False,
        )
    return _paddle_ocr_instance


def _run_paddle_ocr(file_bytes: bytes, filename: Optional[str]) -> List[str]:
    suffix = os.path.splitext(sanitize_filename(filename))[1] or ".jpg"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        ocr = _get_paddle_ocr_instance()
        try:
            # PaddleOCR 2.x 常见调用方式
            raw_result = ocr.ocr(tmp_path, cls=False)
        except TypeError:
            # PaddleOCR 3.x 接口已调整为 predict
            raw_result = ocr.predict(tmp_path)
        lines: List[str] = []
        _collect_paddle_text(raw_result, lines)
        return [line for line in lines if line][:120]
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _is_paddle_provider_enabled() -> bool:
    return OCR_PROVIDER in {"auto", "paddle"}


def _run_startup_warmup_task(
    *,
    task_name: str,
    timeout_seconds: float,
    task_fn,
) -> None:
    timeout = max(1.0, float(timeout_seconds))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="startup-warmup") as pool:
        future = pool.submit(task_fn)
        try:
            future.result(timeout=timeout)
            print(f"[Warmup] {task_name} completed in startup phase.")
        except FutureTimeoutError:
            print(f"[Warmup] {task_name} timeout after {timeout:.0f}s; service continues.")
        except Exception as exc:
            print(f"[Warmup] {task_name} failed: {exc}")


def _warmup_ocr_runtime() -> None:
    if OCR_PROVIDER == "off":
        return

    if _is_paddle_provider_enabled():
        _get_paddle_ocr_instance()


def _warmup_white_slip_runtime() -> None:
    if not WHITE_SLIP_LOCAL_MODEL_ENABLED:
        return
    _call_white_slip_local_model(
        text_lines=["白条 预热"],
        full_text="白条 事由: 预热 金额: 1",
        file_bytes=None,
        filename="white_slip_warmup.txt",
    )


def _get_baidu_access_token() -> str:
    global _baidu_token_cache

    if not BAIDU_OCR_API_KEY or not BAIDU_OCR_SECRET_KEY:
        raise RuntimeError(
            "未配置百度OCR密钥：请设置 BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY"
        )

    now = datetime.now()
    cached_token = _baidu_token_cache.get("access_token")
    expires_at = _baidu_token_cache.get("expires_at", datetime.min)
    if (
        cached_token
        and isinstance(expires_at, datetime)
        and now < (expires_at - timedelta(minutes=1))
    ):
        return cached_token

    response = httpx.post(
        BAIDU_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": BAIDU_OCR_API_KEY,
            "client_secret": BAIDU_OCR_SECRET_KEY,
        },
        timeout=OCR_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 0))
    if not token:
        raise RuntimeError(f"百度OCR鉴权失败: {payload}")

    _baidu_token_cache = {
        "access_token": token,
        "expires_at": now + timedelta(seconds=expires_in),
    }
    return token


def _run_baidu_ocr(file_bytes: bytes, filename: Optional[str]) -> List[str]:
    safe_name = sanitize_filename(filename).lower()
    if safe_name.endswith(".pdf"):
        raise RuntimeError("百度通用OCR接口不支持直接识别PDF，请先上传扫描图片")

    access_token = _get_baidu_access_token()
    image_base64 = base64.b64encode(file_bytes).decode("utf-8")
    response = httpx.post(
        f"{BAIDU_GENERAL_OCR_URL}?access_token={access_token}",
        data={
            "image": image_base64,
            "language_type": "CHN_ENG",
            "detect_direction": "true",
        },
        timeout=OCR_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("error_code"):
        raise RuntimeError(
            f"百度OCR识别失败: {payload.get('error_msg', payload.get('error_code'))}"
        )

    words = payload.get("words_result", [])
    return [item.get("words", "").strip() for item in words if item.get("words")]


def _run_plain_text_ocr(file_bytes: bytes, filename: Optional[str]) -> List[str]:
    text_extensions = (".txt", ".md", ".csv", ".json", ".xml", ".html", ".log")
    safe_name = sanitize_filename(filename).lower()
    if not safe_name.endswith(text_extensions):
        raise RuntimeError("plain_text 仅支持文本文件扩展名")

    for encoding in ("utf-8", "gbk", "utf-16"):
        try:
            decoded = file_bytes.decode(encoding)
            return [line.strip() for line in decoded.splitlines() if line.strip()]
        except UnicodeDecodeError:
            continue
    raise RuntimeError("plain_text 无法解码当前文本文件")


def analyze_voucher_text(
    text_lines: List[str],
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    lines = [line.strip() for line in text_lines if line and line.strip()]
    full_text = "\n".join(lines)
    fields = _empty_ocr_fields()
    fields.update(extract_voucher_core_fields(full_text))
    id_card = extract_id_card_fields(lines, full_text)
    receipt = extract_receipt_fields(lines, full_text)
    fields["id_name"] = id_card.get("name")
    fields["id_number"] = id_card.get("id_number")
    fields["receipt_number"] = receipt.get("receipt_number")

    white_slip_keywords = ("白条", "借条", "收条", "领条", "欠条")
    rule_white_slip_fields = extract_white_slip_fields(lines, full_text)
    is_id_card = bool(id_card.get("is_id_card"))
    is_receipt = (not is_id_card) and bool(receipt.get("is_receipt"))
    is_white_slip = (
        (not is_id_card)
        and (not is_receipt)
        and (
            any(keyword in full_text for keyword in white_slip_keywords)
            or _has_meaningful_white_slip_fields(rule_white_slip_fields)
        )
    )
    if is_id_card:
        document_type = "id_card"
    elif is_receipt:
        document_type = "receipt"
    elif is_white_slip:
        document_type = "white_slip"
    else:
        document_type = "voucher"

    result: Dict[str, Any] = {
        "fields": fields,
        "document_type": document_type,
        "text_lines": lines,
    }
    if is_id_card:
        result["id_card"] = {
            "name": id_card.get("name"),
            "id_number": id_card.get("id_number"),
        }
    if is_receipt:
        result["receipt"] = {
            "receipt_number": receipt.get("receipt_number"),
            "payer": receipt.get("payer"),
            "payee": receipt.get("payee"),
        }
    if is_white_slip:
        white_slip_payload = build_white_slip_structured_payload(
            lines,
            full_text,
            fields,
            file_bytes=file_bytes,
            filename=filename,
        )
        result["white_slip"] = white_slip_payload["white_slip"]
        result["white_slip_standard"] = white_slip_payload["white_slip_standard"]
        result["white_slip_ai"] = white_slip_payload["white_slip_ai"]
        result["white_slip_local_model"] = white_slip_payload["white_slip_local_model"]
    return result


_IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
_WHITE_SLIP_FILE_HINTS = (
    "white",
    "slip",
    "baitiao",
    "白条",
    "借条",
    "收条",
    "领条",
    "欠条",
)


def _is_image_filename(filename: Optional[str]) -> bool:
    safe_name = sanitize_filename(filename).lower()
    suffix = os.path.splitext(safe_name)[1]
    if suffix in _IMAGE_FILE_EXTENSIONS:
        return True
    mime_type = mimetypes.guess_type(safe_name)[0] or ""
    return str(mime_type).startswith("image/")


def _should_try_white_slip_fallback(filename: Optional[str]) -> bool:
    if WHITE_SLIP_FALLBACK_ALWAYS:
        return True
    safe_name = sanitize_filename(filename).lower()
    return any(token in safe_name for token in _WHITE_SLIP_FILE_HINTS)


def _has_meaningful_white_slip_fields(fields: Dict[str, Any]) -> bool:
    if not isinstance(fields, dict):
        return False
    has_semantic_text = any(
        fields.get(key) not in (None, "", []) for key in ("reason", "payer", "payee")
    )
    signers = fields.get("signers")
    has_signers = isinstance(signers, list) and bool(signers)
    has_amount_or_date = fields.get("amount") not in (None, "") or fields.get(
        "date"
    ) not in (None, "")
    slip_type = str(fields.get("slip_type") or "").strip().lower()
    if has_semantic_text or has_signers:
        return True
    if has_amount_or_date and slip_type in {"loan_note", "receipt_note"}:
        return True
    return False


def _try_white_slip_image_fallback(
    file_bytes: bytes,
    filename: Optional[str],
    *,
    trigger: str,
    ocr_error: str,
) -> Optional[Dict[str, Any]]:
    if not file_bytes:
        return None
    if not _is_image_filename(filename):
        return None
    if not _should_try_white_slip_fallback(filename):
        return None
    if not WHITE_SLIP_LOCAL_MODEL_ENABLED and not WHITE_SLIP_AI_ENABLED:
        return None

    local_model_result = _call_white_slip_local_model(
        text_lines=[],
        full_text="",
        file_bytes=file_bytes,
        filename=filename,
    )
    ai_result = _call_white_slip_ai(
        text_lines=[],
        full_text="",
        file_bytes=file_bytes,
        filename=filename,
    )

    local_fields = (
        local_model_result.get("fields", {})
        if local_model_result.get("status") == "ok"
        else {}
    )
    ai_fields = ai_result.get("fields", {}) if ai_result.get("status") == "ok" else {}

    merged_fields = _merge_white_slip_fields({"reason": None, "signers": []}, ai_fields)
    merged_fields = _merge_white_slip_fields(merged_fields, local_fields)
    if not _has_meaningful_white_slip_fields(merged_fields):
        return None

    core_fields = _empty_ocr_fields()
    if isinstance(merged_fields.get("amount"), (int, float)):
        core_fields["amount"] = float(merged_fields["amount"])
    if merged_fields.get("date"):
        core_fields["date"] = merged_fields["date"]

    standardized = standardize_white_slip_document(
        [],
        "",
        core_fields,
        merged_fields,
    )
    if not standardized.get("payer") and merged_fields.get("payer"):
        standardized["payer"] = merged_fields["payer"]
    if not standardized.get("payee") and merged_fields.get("payee"):
        standardized["payee"] = merged_fields["payee"]
    if merged_fields.get("slip_type"):
        standardized["slip_type"] = merged_fields["slip_type"]

    trigger_text = {
        "provider_chain_failed": "OCR多引擎识别失败",
        "cooldown": "OCR超时冷却期",
        "timeout": "OCR执行超时",
        "worker_error": "OCR工作线程异常",
    }.get(trigger, trigger)

    return {
        "status": "ok",
        "provider": "white_slip_fallback",
        "document_type": "white_slip",
        "fields": core_fields,
        "text_lines": [],
        "white_slip": merged_fields,
        "white_slip_standard": standardized,
        "white_slip_ai": ai_result,
        "white_slip_local_model": local_model_result,
        "warning": f"{trigger_text}，已切换到白条图片模型兜底解析。原因: {ocr_error}",
    }


def _perform_ocr_inner(file_bytes: bytes, filename: Optional[str]) -> Dict[str, Any]:
    provider = OCR_PROVIDER
    if provider == "off":
        return {
            "status": "skipped",
            "provider": "off",
            "fields": _empty_ocr_fields(),
            "document_type": "voucher",
            "text_lines": [],
        }

    provider_chain = {
        "auto": ["paddle", "baidu", "plain_text"],
        "paddle": ["paddle"],
        "baidu": ["baidu"],
        "plain_text": ["plain_text"],
    }.get(provider)
    if not provider_chain:
        raise RuntimeError(f"不支持的 OCR_PROVIDER: {provider}")

    errors: List[str] = []
    for current in provider_chain:
        try:
            lines = (
                _run_paddle_ocr(file_bytes, filename)
                if current == "paddle"
                else _run_baidu_ocr(file_bytes, filename)
                if current == "baidu"
                else _run_plain_text_ocr(file_bytes, filename)
            )
            analyzed = analyze_voucher_text(
                lines,
                file_bytes=file_bytes,
                filename=filename,
            )
            analyzed["status"] = "ok"
            analyzed["provider"] = current
            if not lines:
                analyzed["status"] = "empty"
            return analyzed
        except Exception as exc:
            errors.append(f"{current}: {exc}")

    error_message = " | ".join(errors) if errors else "OCR识别失败"
    if OCR_REQUIRED:
        raise RuntimeError(error_message)

    fallback_result = None
    if OCR_WHITE_SLIP_FALLBACK:
        fallback_result = _try_white_slip_image_fallback(
            file_bytes,
            filename,
            trigger="provider_chain_failed",
            ocr_error=error_message,
        )
        if fallback_result:
            return fallback_result

    return {
        "status": "failed",
        "provider": provider,
        "error": error_message,
        "fields": _empty_ocr_fields(),
        "document_type": "voucher",
        "text_lines": [],
    }


def perform_ocr(file_bytes: bytes, filename: Optional[str]) -> Dict[str, Any]:
    global _ocr_timeout_until

    now = datetime.now()
    if now < _ocr_timeout_until:
        cooldown_seconds = max(1, int((_ocr_timeout_until - now).total_seconds()))
        cooldown_error = f"OCR在超时后进入冷却，约 {cooldown_seconds}s 后可重试"
        fallback_result = None
        if OCR_WHITE_SLIP_FALLBACK:
            fallback_result = _try_white_slip_image_fallback(
                file_bytes,
                filename,
                trigger="cooldown",
                ocr_error=cooldown_error,
            )
            if fallback_result:
                return fallback_result
        return {
            "status": "failed",
            "provider": OCR_PROVIDER,
            "error": cooldown_error,
            "fields": _empty_ocr_fields(),
            "document_type": "voucher",
            "text_lines": [],
        }

    # OCR可能因模型加载/下载耗时较长，需要限制接口时延。
    timeout_seconds = max(1.0, float(OCR_PIPELINE_TIMEOUT_SECONDS))
    try:
        future = _ocr_executor.submit(_perform_ocr_inner, file_bytes, filename)
    except RuntimeError:
        # 线程池可能已被重建关闭，重试一次。
        _reset_ocr_executor()
        future = _ocr_executor.submit(_perform_ocr_inner, file_bytes, filename)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        _reset_ocr_executor()
        cooldown_seconds = max(0.0, float(OCR_TIMEOUT_COOLDOWN_SECONDS))
        if cooldown_seconds > 0:
            _ocr_timeout_until = datetime.now() + timedelta(seconds=cooldown_seconds)
        else:
            _ocr_timeout_until = datetime.min
        timeout_error = (
            "OCR执行超时，已回退为空字段。"
            f"provider={OCR_PROVIDER}, timeout={timeout_seconds:.0f}s"
        )
        fallback_result = None
        if OCR_WHITE_SLIP_FALLBACK:
            fallback_result = _try_white_slip_image_fallback(
                file_bytes,
                filename,
                trigger="timeout",
                ocr_error=timeout_error,
            )
            if fallback_result:
                return fallback_result
        return {
            "status": "failed",
            "provider": OCR_PROVIDER,
            "error": timeout_error,
            "fields": _empty_ocr_fields(),
            "document_type": "voucher",
            "text_lines": [],
        }
    except Exception as exc:
        if OCR_REQUIRED:
            raise
        worker_error = f"OCR工作线程异常: {exc}"
        fallback_result = None
        if OCR_WHITE_SLIP_FALLBACK:
            fallback_result = _try_white_slip_image_fallback(
                file_bytes,
                filename,
                trigger="worker_error",
                ocr_error=worker_error,
            )
            if fallback_result:
                return fallback_result
        return {
            "status": "failed",
            "provider": OCR_PROVIDER,
            "error": worker_error,
            "fields": _empty_ocr_fields(),
            "document_type": "voucher",
            "text_lines": [],
        }


def summarize_ocr_for_log(ocr_result: Dict[str, Any]) -> str:
    fields = ocr_result.get("fields", {}) or {}
    amount = fields.get("amount")
    date_value = fields.get("date")
    invoice_code = fields.get("invoice_code")
    summary = f"OCR[{ocr_result.get('provider')}]: 金额={amount}, 日期={date_value}, 发票代码={invoice_code}"

    if ocr_result.get("document_type") == "id_card":
        id_name = fields.get("id_name")
        masked_id = _mask_id_number(fields.get("id_number"))
        summary += f", 韬唤璇佸鍚?={id_name}, 韬唤璇佸彿={masked_id}"
        summary += f", id_name={id_name}, id_number={masked_id}"

    if ocr_result.get("document_type") == "receipt":
        receipt_number = fields.get("receipt_number")
        summary += f", receipt_number={receipt_number}"

    if ocr_result.get("document_type") == "white_slip":
        white_slip = ocr_result.get("white_slip", {}) or {}
        reason = white_slip.get("reason")
        signers = ",".join(white_slip.get("signers", []))
        summary += f", 白条事由={reason}, 签字={signers}"
    if ocr_result.get("warning"):
        summary += f", 警告={ocr_result.get('warning')}"
    return summary


def normalize_invoice_code(invoice_code: Optional[str]) -> Optional[str]:
    if not invoice_code:
        return None
    cleaned = re.sub(r"\s+", "", invoice_code).strip().upper()
    return cleaned or None


def normalize_invoice_number(invoice_number: Optional[str]) -> Optional[str]:
    if not invoice_number:
        return None
    cleaned = re.sub(r"\s+", "", invoice_number).strip().upper()
    cleaned = re.sub(r"[^0-9A-Z]", "", cleaned)
    if not cleaned:
        return None
    return cleaned


def _normalize_compact_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    token = re.sub(r"[^0-9]", "", raw)
    if len(token) != 8:
        return None
    try:
        return datetime.strptime(token, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def build_invoice_unique_code(
    invoice_code: Optional[str], invoice_number: Optional[str]
) -> Optional[str]:
    normalized_code = normalize_invoice_code(invoice_code)
    normalized_number = normalize_invoice_number(invoice_number)
    if normalized_code and normalized_number:
        return f"{normalized_code}-{normalized_number}"
    return normalized_code


def build_invoice_lookup_candidates(
    invoice_code: Optional[str], invoice_number: Optional[str] = None
) -> List[str]:
    normalized_code = normalize_invoice_code(invoice_code)
    normalized_number = normalize_invoice_number(invoice_number)
    if normalized_code and "-" in normalized_code and not normalized_number:
        code_part, number_part = normalized_code.split("-", 1)
        normalized_code = normalize_invoice_code(code_part)
        normalized_number = normalize_invoice_number(number_part)
    invoice_unique_code = build_invoice_unique_code(normalized_code, normalized_number)

    candidates: List[str] = []
    for value in (invoice_unique_code, normalized_code):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _decode_email_header(value: str) -> str:
    if not value:
        return ""
    decoded_parts = decode_header(value)
    text_parts: List[str] = []
    for chunk, encoding in decoded_parts:
        if isinstance(chunk, bytes):
            enc = encoding or "utf-8"
            try:
                text_parts.append(chunk.decode(enc, errors="ignore"))
            except Exception:
                text_parts.append(chunk.decode("utf-8", errors="ignore"))
        else:
            text_parts.append(str(chunk))
    return "".join(text_parts).strip()


def _parse_qr_invoice_payload_legacy(qr_content: str) -> Dict[str, Any]:
    content = qr_content.strip()
    parsed: Dict[str, str] = {}

    if "=" in content:
        for item in re.split(r"[;&\n]", content):
            part = item.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            parsed[key.strip().lower()] = value.strip()

    csv_tokens = [token.strip() for token in content.split(",") if token.strip()]
    looks_like_csv_qr = len(csv_tokens) >= 6 and "=" not in content
    if looks_like_csv_qr:
        # Common VAT QR layout example:
        # 01,10,fpdm,fphm,amount,date,check_code,...
        parsed.setdefault("fpdm", csv_tokens[2] if len(csv_tokens) > 2 else "")
        parsed.setdefault("fphm", csv_tokens[3] if len(csv_tokens) > 3 else "")
        parsed.setdefault("je", csv_tokens[4] if len(csv_tokens) > 4 else "")
        parsed.setdefault("kprq", csv_tokens[5] if len(csv_tokens) > 5 else "")
        parsed.setdefault("jym", csv_tokens[6] if len(csv_tokens) > 6 else "")

    invoice_code = normalize_invoice_code(
        parsed.get("invoice_code")
        or parsed.get("fpdm")
        or parsed.get("code")
        or parsed.get("发票代码")
    )
    if not invoice_code:
        match = re.search(
            r"(?:发票代码|票据代码)\s*[:：]?\s*([A-Za-z0-9]{6,30})", content
        )
        if match:
            invoice_code = normalize_invoice_code(match.group(1))

    invoice_number = normalize_invoice_number(
        parsed.get("invoice_number")
        or parsed.get("fphm")
        or parsed.get("number")
        or parsed.get("发票号码")
    )
    if not invoice_number:
        number_match = re.search(
            r"(?:发票号码|号码)\s*[:：]?\s*([A-Za-z0-9]{6,30})", content
        )
        if number_match:
            invoice_number = normalize_invoice_number(number_match.group(1))

    check_code = normalize_invoice_number(
        parsed.get("check_code") or parsed.get("jym") or parsed.get("校验码")
    )
    if not check_code:
        check_match = re.search(r"(?:校验码)\s*[:：]?\s*([A-Za-z0-9]{6,30})", content)
        if check_match:
            check_code = normalize_invoice_number(check_match.group(1))

    amount_text = (
        parsed.get("amount")
        or parsed.get("je")
        or parsed.get("价税合计")
        or parsed.get("金额")
        or ""
    )
    amount_candidate = _to_float(amount_text) if amount_text else None
    if amount_candidate is None:
        fallback_values = re.findall(
            r"(?:¥|￥)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})|[0-9]+\.\d{1,2})",
            content,
        )
        valid_values = [
            v for v in (_to_float(vv) for vv in fallback_values) if v is not None
        ]
        amount_candidate = max(valid_values) if valid_values else 0.0
    amount = float(amount_candidate or 0.0)

    amount_without_tax = _to_float(
        parsed.get("bhsje")
        or parsed.get("amount_without_tax")
        or parsed.get("不含税金额")
        or ""
    )
    tax_amount = _to_float(
        parsed.get("se") or parsed.get("tax") or parsed.get("税额") or ""
    )

    date_text = (
        parsed.get("date")
        or parsed.get("kprq")
        or parsed.get("开票日期")
        or parsed.get("rq")
        or ""
    )
    voucher_date = _normalize_date(date_text) or _normalize_compact_date(date_text)
    if not voucher_date:
        date_match = re.search(r"(20\d{2}[年/\-.]\d{1,2}[月/\-.]\d{1,2}日?)", content)
        voucher_date = _normalize_date(date_match.group(1)) if date_match else None
    if not voucher_date:
        compact_match = re.search(r"(20\d{6})", content)
        voucher_date = (
            _normalize_compact_date(compact_match.group(1)) if compact_match else None
        )

    invoice_unique_code = build_invoice_unique_code(invoice_code, invoice_number)

    return {
        "invoice_code": invoice_code,
        "invoice_number": invoice_number,
        "invoice_unique_code": invoice_unique_code,
        "check_code": check_code,
        "amount": amount,
        "amount_without_tax": amount_without_tax,
        "tax_amount": tax_amount,
        "voucher_date": voucher_date,
        "raw_format": "csv" if looks_like_csv_qr else "kv",
    }


def parse_qr_invoice_payload(qr_content: str) -> Dict[str, Any]:
    content = qr_content.strip()
    parsed: Dict[str, str] = {}

    if "=" in content:
        for item in re.split(r"[;&\n]", content):
            part = item.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            parsed[key.strip().lower()] = value.strip()

    csv_tokens = [token.strip() for token in content.split(",") if token.strip()]
    looks_like_csv_qr = len(csv_tokens) >= 6 and "=" not in content
    if looks_like_csv_qr:
        parsed.setdefault("fpdm", csv_tokens[2] if len(csv_tokens) > 2 else "")
        parsed.setdefault("fphm", csv_tokens[3] if len(csv_tokens) > 3 else "")
        parsed.setdefault("je", csv_tokens[4] if len(csv_tokens) > 4 else "")
        parsed.setdefault("kprq", csv_tokens[5] if len(csv_tokens) > 5 else "")
        parsed.setdefault("jym", csv_tokens[6] if len(csv_tokens) > 6 else "")

    invoice_code = normalize_invoice_code(
        parsed.get("invoice_code")
        or parsed.get("fpdm")
        or parsed.get("code")
        or parsed.get("\u53d1\u7968\u4ee3\u7801")
    )
    if not invoice_code:
        match = re.search(
            r"(?:\u53d1\u7968\u4ee3\u7801|\u7968\u636e\u4ee3\u7801|invoice[_\s-]*code)\s*[:\uff1a]?\s*([A-Za-z0-9]{6,30})",
            content,
            flags=re.IGNORECASE,
        )
        if match:
            invoice_code = normalize_invoice_code(match.group(1))

    invoice_number = normalize_invoice_number(
        parsed.get("invoice_number")
        or parsed.get("fphm")
        or parsed.get("number")
        or parsed.get("\u53d1\u7968\u53f7\u7801")
    )
    if not invoice_number:
        number_match = re.search(
            r"(?:\u53d1\u7968\u53f7\u7801|\u53f7\u7801|invoice[_\s-]*number)\s*[:\uff1a]?\s*([A-Za-z0-9]{6,30})",
            content,
            flags=re.IGNORECASE,
        )
        if number_match:
            invoice_number = normalize_invoice_number(number_match.group(1))

    check_code = normalize_invoice_number(
        parsed.get("check_code")
        or parsed.get("jym")
        or parsed.get("\u6821\u9a8c\u7801")
    )
    if not check_code:
        check_match = re.search(
            r"(?:\u6821\u9a8c\u7801|check[_\s-]*code)\s*[:\uff1a]?\s*([A-Za-z0-9]{6,30})",
            content,
            flags=re.IGNORECASE,
        )
        if check_match:
            check_code = normalize_invoice_number(check_match.group(1))

    amount_text = (
        parsed.get("amount")
        or parsed.get("je")
        or parsed.get("\u4ef7\u7a0e\u5408\u8ba1")
        or parsed.get("\u91d1\u989d")
        or ""
    )
    amount_candidate = _to_float(amount_text) if amount_text else None
    if amount_candidate is None:
        fallback_values = re.findall(
            r"(?:¥|￥)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})|[0-9]+\.\d{1,2})",
            content,
        )
        valid_values = [
            v for v in (_to_float(vv) for vv in fallback_values) if v is not None
        ]
        amount_candidate = max(valid_values) if valid_values else 0.0
    amount = float(amount_candidate or 0.0)

    amount_without_tax = _to_float(
        parsed.get("bhsje")
        or parsed.get("amount_without_tax")
        or parsed.get("\u4e0d\u542b\u7a0e\u91d1\u989d")
        or ""
    )
    tax_amount = _to_float(
        parsed.get("se") or parsed.get("tax") or parsed.get("\u7a0e\u989d") or ""
    )

    date_text = (
        parsed.get("date")
        or parsed.get("kprq")
        or parsed.get("\u5f00\u7968\u65e5\u671f")
        or parsed.get("rq")
        or ""
    )
    voucher_date = _normalize_date(date_text) or _normalize_compact_date(date_text)
    if not voucher_date:
        date_match = re.search(
            r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", content
        )
        if date_match:
            voucher_date = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
    if not voucher_date:
        compact_match = re.search(r"(20\d{6})", content)
        voucher_date = (
            _normalize_compact_date(compact_match.group(1)) if compact_match else None
        )

    invoice_unique_code = build_invoice_unique_code(invoice_code, invoice_number)
    if looks_like_csv_qr:
        raw_format = "csv"
    elif "=" in content:
        raw_format = "kv"
    else:
        raw_format = "text"

    return {
        "invoice_code": invoice_code,
        "invoice_number": invoice_number,
        "invoice_unique_code": invoice_unique_code,
        "check_code": check_code,
        "amount": amount,
        "amount_without_tax": amount_without_tax,
        "tax_amount": tax_amount,
        "voucher_date": voucher_date,
        "raw_format": raw_format,
    }


def _extract_email_text(msg: email.message.Message) -> str:
    texts: List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disposition:
                continue
            if part.get_content_type() != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                texts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                texts.append(payload.decode("utf-8", errors="ignore"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                texts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                texts.append(payload.decode("utf-8", errors="ignore"))
    return "\n".join(texts).strip()


def _extract_email_attachments(msg: email.message.Message) -> List[Dict[str, Any]]:
    attachments: List[Dict[str, Any]] = []
    for part in msg.walk():
        filename_raw = part.get_filename()
        disposition = str(part.get("Content-Disposition", "")).lower()
        if not filename_raw and "attachment" not in disposition:
            continue

        filename = _decode_email_header(filename_raw or "attachment.bin")
        payload = part.get_payload(decode=True)
        if not payload:
            continue

        attachments.append(
            {
                "filename": filename,
                "content": payload,
                "content_type": part.get_content_type() or "application/octet-stream",
            }
        )
    return attachments


def _create_reimbursement_from_invoice_data(
    db: Session,
    *,
    username: str,
    amount: float,
    category: str,
    reason: Optional[str],
    invoice_code: Optional[str],
    voucher_date: Optional[str],
    image_path: str,
    source_device: str,
    is_e_invoice: bool,
    storage_location: Optional[str] = None,
    box_id: Optional[str] = None,
    stored_by: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_code = normalize_invoice_code(invoice_code)
    duplicate = find_duplicate_invoice(db, normalized_code)
    if duplicate:
        duplicated_at = duplicate.created_at.strftime("%m月%d日")
        db.add(
            OperationLog(
                operator=username,
                action=f"尝试报销发票 {normalized_code} 失败：已于 {duplicated_at} 报销过",
            )
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail=f"发票代码重复，疑似重复报账：{normalized_code}（已存在id={duplicate.id}）",
        )

    tax_verify_result = {
        "checked": False,
        "passed": True,
        "message": "非电子发票，无需验真",
    }
    if is_e_invoice:
        tax_verify_result = verify_e_invoice_with_tax_api(
            invoice_code=normalized_code,
            amount=amount,
            voucher_date=voucher_date,
        )
        should_block = (not tax_verify_result.get("passed")) and (
            tax_verify_result.get("checked") or TAX_VERIFY_STRICT
        )
        if should_block:
            db.add(
                OperationLog(
                    operator=username,
                    action=(
                        f"尝试报销发票 {normalized_code or '未知代码'} 失败："
                        f"电子发票验真未通过({tax_verify_result.get('message')})"
                    ),
                )
            )
            db.commit()
            raise HTTPException(
                status_code=422,
                detail=f"电子发票验真未通过：{tax_verify_result.get('message')}",
            )

    if not is_e_invoice:
        verify_status = "not_required"
    elif tax_verify_result.get("checked") and tax_verify_result.get("passed"):
        verify_status = "verified"
    elif tax_verify_result.get("checked") and not tax_verify_result.get("passed"):
        verify_status = "failed"
    else:
        verify_status = "unverified"

    policy_result = apply_policy_rules(reason, amount)
    stored_at = (
        datetime.now()
        if (storage_location and storage_location.strip())
        or (box_id and box_id.strip())
        else None
    )

    row = Reimbursement(
        username=username,
        amount=amount,
        category=category,
        reason=reason,
        status=policy_result["status"],
        invoice_code=normalized_code,
        image_path=image_path,
        voucher_date=voucher_date,
        source_device=source_device,
        verify_status=verify_status,
        verify_message=(tax_verify_result.get("message") or "")[:250],
        physical_storage_location=(storage_location or "").strip() or None,
        box_id=(box_id or "").strip() or None,
        physical_stored_by=(stored_by or "").strip() or None,
        physical_stored_at=stored_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "reimbursement": row,
        "policy": policy_result,
        "tax_verification": tax_verify_result,
    }


def extract_reason_from_ocr(ocr_result: Dict[str, Any]) -> Optional[str]:
    if ocr_result.get("document_type") == "white_slip":
        white_slip = ocr_result.get("white_slip", {}) or {}
        reason = (white_slip.get("reason") or "").strip()
        if reason:
            return reason

    text_lines = ocr_result.get("text_lines", []) or []
    for line in text_lines:
        compact = line.strip()
        if any(key in compact for key in ("事由", "用途", "报销", "说明")):
            if "：" in compact:
                reason = compact.split("：", 1)[1].strip()
            elif ":" in compact:
                reason = compact.split(":", 1)[1].strip()
            else:
                reason = compact
            if reason:
                return reason
    return None


def infer_category(reason: Optional[str], ocr_result: Dict[str, Any]) -> str:
    if ocr_result.get("document_type") == "white_slip":
        return "白条"

    if ocr_result.get("document_type") == "receipt":
        return "\u6536\u636e"
    reason_text = (reason or "").lower()
    if "招待" in reason_text:
        return "招待费"
    if "差旅" in reason_text or "交通" in reason_text:
        return "差旅费"
    if "办公" in reason_text or "耗材" in reason_text:
        return "办公费"
    if "维修" in reason_text:
        return "维修费"
    return "其他"


def apply_policy_rules(
    reason: Optional[str], amount: float, ocr_result: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    status = "待审核"
    rule_notes: List[str] = []

    ocr_payload = ocr_result or {}
    if ocr_payload.get("document_type") == "white_slip":
        white_slip = ocr_payload.get("white_slip", {}) or {}
        signers = white_slip.get("signers", []) or []
        if len(signers) < 2:
            status = "附件不全：缺少签字"
            rule_notes.append("命中政策：白条签字人数不足2人")

    if status == "待审核" and reason and "招待" in reason and amount > 5000:
        status = "需村民代表大会决议"
        rule_notes.append("命中政策：招待类且金额>5000")

    return {"status": status, "rule_notes": rule_notes}


def is_electronic_invoice(ocr_result: Dict[str, Any], filename: Optional[str]) -> bool:
    joined = "\n".join(ocr_result.get("text_lines", []) or [])
    keywords = ("电子发票", "增值税电子", "全电发票", "数电发票")
    if any(keyword in joined for keyword in keywords):
        return True
    lower_name = (filename or "").lower()
    return lower_name.endswith(".pdf") and "发票" in joined


def find_duplicate_invoice(
    db: Session,
    invoice_code: Optional[str],
    invoice_number: Optional[str] = None,
) -> Optional[Reimbursement]:
    candidates = build_invoice_lookup_candidates(invoice_code, invoice_number)
    if not candidates:
        return None
    return (
        db.execute(
            select(Reimbursement)
            .where(Reimbursement.invoice_code.in_(candidates))
            .order_by(Reimbursement.created_at.desc())
        )
        .scalars()
        .first()
    )


def find_duplicate_invoice_by_candidates(
    db: Session, candidates: List[str]
) -> Optional[Reimbursement]:
    normalized_candidates: List[str] = []
    for item in candidates:
        normalized = normalize_invoice_code(item)
        if normalized and normalized not in normalized_candidates:
            normalized_candidates.append(normalized)
    if not normalized_candidates:
        return None
    return (
        db.execute(
            select(Reimbursement)
            .where(Reimbursement.invoice_code.in_(normalized_candidates))
            .order_by(Reimbursement.created_at.desc())
        )
        .scalars()
        .first()
    )


def verify_e_invoice_with_tax_api(
    invoice_code: Optional[str],
    amount: Optional[float],
    voucher_date: Optional[str],
) -> Dict[str, Any]:
    normalized_code = normalize_invoice_code(invoice_code)
    if not normalized_code:
        return {
            "checked": False,
            "passed": False,
            "message": "电子发票缺少发票代码，无法验真",
        }

    if not TAX_VERIFY_URL:
        return {
            "checked": False,
            "passed": False,
            "message": "未配置 TAX_VERIFY_URL，无法调用税务验真接口",
        }

    payload = {
        "invoice_code": normalized_code,
        "amount": amount,
        "date": voucher_date,
    }

    try:
        response = httpx.post(
            TAX_VERIFY_URL,
            json=payload,
            timeout=TAX_VERIFY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        verdict = None
        for key in ("is_valid", "valid", "verified", "pass"):
            if key in data:
                verdict = data.get(key)
                break
        if verdict is None and isinstance(data.get("data"), dict):
            nested = data["data"]
            for key in ("is_valid", "valid", "verified", "pass"):
                if key in nested:
                    verdict = nested.get(key)
                    break

        passed: Optional[bool] = None
        if isinstance(verdict, bool):
            passed = verdict
        elif isinstance(verdict, int):
            passed = verdict == 1
        elif isinstance(verdict, str):
            passed = verdict.strip().lower() in {
                "true",
                "1",
                "ok",
                "pass",
                "passed",
                "valid",
                "verified",
                "success",
            }

        if passed is None:
            passed = bool(data.get("success", False))

        message = str(data.get("message") or data.get("msg") or "").strip()
        if not message:
            message = "验真通过" if passed else "验真失败"

        return {
            "checked": True,
            "passed": passed,
            "message": message,
            "raw": data,
        }
    except Exception as exc:
        return {
            "checked": False,
            "passed": False,
            "message": f"调用税务验真接口异常: {exc}",
        }


AUDIT_ALLOWED_STATUS = {
    "待审核",
    "已入账",
    "已打款",
    "已驳回",
    "需村民代表大会决议",
}


def get_user_role(username: str) -> str:
    return "accountant" if username in ACCOUNTANT_USERS else "reporter"


def ensure_accountant_permission(username: str) -> None:
    if username not in ACCOUNTANT_USERS:
        raise HTTPException(status_code=403, detail="无会计审核权限")


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "gbk", "utf-16"):
        try:
            return path.read_text(encoding=encoding)
        except Exception:
            continue
    return ""


def load_policy_documents() -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    if POLICY_DOCS_DIR.exists():
        for path in sorted(POLICY_DOCS_DIR.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".txt", ".md"}:
                continue
            content = _read_text_file(path).strip()
            if content:
                docs.append({"name": path.name, "content": content})

    if not docs:
        docs.append(
            {
                "name": "default_policy",
                "content": (
                    "村级财务规则：普通报账状态为待审核。"
                    "若事由涉及招待且金额超过5000元，需村民代表大会决议后执行。"
                    "电子发票应进行验真，验真失败不得入账。"
                ),
            }
        )
    return docs


def _extract_query_tokens(question: str) -> List[str]:
    tokens = [t for t in re.split(r"[\s,，。；;、:：]+", question) if t]
    return tokens[:20]


def retrieve_policy_context(question: str, top_k: int) -> List[Dict[str, str]]:
    docs = load_policy_documents()
    tokens = _extract_query_tokens(question)
    scored: List[Dict[str, Any]] = []

    for doc in docs:
        content = doc["content"]
        score = 0
        for token in tokens:
            if token in content:
                score += 1
        if "5000" in question and "5000" in content:
            score += 2
        if "招待" in question and "招待" in content:
            score += 2
        scored.append({"name": doc["name"], "content": content, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    selected = [item for item in scored[: max(1, top_k)] if item["score"] > 0]
    if not selected:
        selected = scored[:1]
    return [{"name": item["name"], "content": item["content"]} for item in selected]


def _build_policy_prompt(question: str, contexts: List[Dict[str, str]]) -> str:
    context_block = "\n\n".join(
        [f"[文档:{c['name']}]\n{c['content'][:2000]}" for c in contexts]
    )
    return (
        "你是村级财务政策助手。请严格依据提供的政策文档回答，"
        "先给结论，再给办理建议，不确定时明确说“需人工确认”。\n\n"
        f"政策文档:\n{context_block}\n\n"
        f"用户问题: {question}"
    )


def _call_gemini_policy(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("未配置 GEMINI_API_KEY")
    url = f"{GEMINI_API_BASE}/{POLICY_AI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    response = httpx.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=OCR_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini无返回: {payload}")
    parts = (((candidates[0] or {}).get("content") or {}).get("parts")) or []
    text = "".join([p.get("text", "") for p in parts if isinstance(p, dict)]).strip()
    if not text:
        raise RuntimeError("Gemini返回为空")
    return text


def _call_qwen_policy(prompt: str) -> str:
    headers = {"Content-Type": "application/json"}
    if QWEN_OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {QWEN_OPENAI_API_KEY}"

    response = httpx.post(
        f"{QWEN_OPENAI_BASE_URL.rstrip('/')}/chat/completions",
        headers=headers,
        json={
            "model": POLICY_AI_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "你是村级财务政策助手，只能基于给定政策文档回答。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=OCR_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices", [])
    if not choices:
        raise RuntimeError(f"Qwen无返回: {payload}")
    content = (((choices[0] or {}).get("message")) or {}).get("content", "")
    text = str(content).strip()
    if not text:
        raise RuntimeError("Qwen返回为空")
    return text


def answer_policy_question(question: str) -> Dict[str, Any]:
    contexts = retrieve_policy_context(question, POLICY_TOP_K)
    prompt = _build_policy_prompt(question, contexts)
    provider = POLICY_AI_PROVIDER

    if provider == "off":
        return {
            "provider": "off",
            "answer": (
                "当前未启用大模型。根据现行规则：若事由含“招待”且金额超过5000元，"
                "需村民代表大会决议后再走审核入账。"
            ),
            "sources": [c["name"] for c in contexts],
        }

    try:
        if provider == "gemini":
            text = _call_gemini_policy(prompt)
        elif provider == "qwen":
            text = _call_qwen_policy(prompt)
        else:
            raise RuntimeError(f"不支持的 POLICY_AI_PROVIDER: {provider}")
        return {
            "provider": provider,
            "answer": text,
            "sources": [c["name"] for c in contexts],
        }
    except Exception as exc:
        return {
            "provider": provider,
            "answer": f"模型调用失败，降级为规则回答：{exc}",
            "sources": [c["name"] for c in contexts],
            "error": str(exc),
        }


def _parse_date_safe(date_text: Optional[str]) -> Optional[datetime]:
    if not date_text:
        return None
    try:
        return datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return None


def resolve_village(username: str) -> str:
    return USER_VILLAGE_MAP.get(username, "未配置村别")


def get_summary_group_key(row: Reimbursement, group_by: str) -> str:
    if group_by == "village":
        return resolve_village(row.username)
    if group_by == "category":
        return row.category or "未分类"
    if group_by == "status":
        return row.status or "未知状态"

    voucher_dt = _parse_date_safe(row.voucher_date)
    dt = voucher_dt or row.created_at
    return dt.strftime("%Y-%m")


REIMBURSEMENT_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "village_procurement": {
        "name": "\u6751\u96c6\u4f53\u91c7\u8d2d",
        "description": "\u7528\u4e8e\u6751\u96c6\u4f53\u7269\u8d44\u4e0e\u670d\u52a1\u91c7\u8d2d\u62a5\u8d26",
        "category": "\u91c7\u8d2d\u652f\u51fa",
        "default_reason": "\u6751\u96c6\u4f53\u91c7\u8d2d",
        "required_fields": [
            "vendor",
            "item_name",
            "quantity",
            "amount",
            "invoice_code",
        ],
        "policy_hint": "\u91c7\u8d2d\u7c7b\u5efa\u8bae\u9644\u660e\u7ec6\u6e05\u5355\u4e0e\u5408\u540c",
        "default_amount": 0.0,
    },
    "labor_subsidy": {
        "name": "\u52a1\u5de5\u8865\u8d34",
        "description": "\u7528\u4e8e\u6751\u96c6\u4f53\u4e34\u65f6\u7528\u5de5\u3001\u516c\u76ca\u5c97\u4f4d\u8865\u8d34",
        "category": "\u52a1\u5de5\u8865\u8d34",
        "default_reason": "\u52a1\u5de5\u8865\u8d34\u53d1\u653e",
        "required_fields": [
            "worker_name",
            "work_days",
            "unit_price",
            "amount",
            "signers",
        ],
        "policy_hint": "\u5efa\u8bae\u9644\u5de5\u4f5c\u91cf\u8868\u4e0e\u7b7e\u5b57\u78ba\u8ba4",
        "default_amount": 0.0,
    },
    "infrastructure_repair": {
        "name": "\u57fa\u7840\u8bbe\u65bd\u7ef4\u4fee",
        "description": "\u7528\u4e8e\u6751\u5185\u9053\u8def\u3001\u6c34\u5229\u3001\u7167\u660e\u7b49\u8bbe\u65bd\u7ef4\u4fee",
        "category": "\u7ef4\u4fee\u8d39",
        "default_reason": "\u57fa\u7840\u8bbe\u65bd\u7ef4\u4fee",
        "required_fields": [
            "project_name",
            "supplier",
            "amount",
            "acceptance_signers",
        ],
        "policy_hint": "\u5efa\u8bae\u9644\u7ef4\u4fee\u524d\u540e\u8bf4\u660e\u4e0e\u9a8c\u6536\u8bb0\u5f55",
        "default_amount": 0.0,
    },
}


def _get_template_or_404(template_key: str) -> Dict[str, Any]:
    template = REIMBURSEMENT_TEMPLATES.get((template_key or "").strip())
    if not template:
        raise HTTPException(
            status_code=404, detail=f"template not found: {template_key}"
        )
    return template


def build_template_prefill(
    template_key: str,
    *,
    amount: Optional[float] = None,
    reason: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    template = _get_template_or_404(template_key)
    amount_value = (
        float(amount)
        if isinstance(amount, (int, float))
        else float(template.get("default_amount") or 0.0)
    )
    reason_value = (reason or template.get("default_reason") or "").strip() or None
    category_value = str(template.get("category") or "\u5176\u4ed6")
    policy_preview = apply_policy_rules(reason_value, amount_value)
    required_fields = [str(x) for x in (template.get("required_fields") or [])]
    extra_payload = dict(extra or {})
    for field_name in required_fields:
        extra_payload.setdefault(field_name, None)
    missing_required_fields = [
        field_name
        for field_name in required_fields
        if extra_payload.get(field_name) in (None, "")
    ]

    return {
        "template_key": template_key,
        "template_name": template.get("name"),
        "template_description": template.get("description"),
        "category": category_value,
        "reason": reason_value,
        "amount": amount_value,
        "extra": extra_payload,
        "policy_preview": policy_preview,
        "required_fields": required_fields,
        "missing_required_fields": missing_required_fields,
        "policy_hint": template.get("policy_hint"),
    }


# ============================================================
# 4) FastAPI 应用初始化与中间件
# ============================================================

app = FastAPI(title="农村财务机器人安全网关", version="3.4.0-AdminPolicy")

# CORS 配置：当前为全开放，方便联调。
# 生产环境建议限制 allow_origins 到可信域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 5) 依赖与请求模型
# ============================================================


@app.on_event("startup")
def warmup_runtime_models() -> None:
    if OCR_STARTUP_WARMUP_ENABLED:
        _run_startup_warmup_task(
            task_name="OCR runtime",
            timeout_seconds=OCR_STARTUP_WARMUP_TIMEOUT_SECONDS,
            task_fn=_warmup_ocr_runtime,
        )
    if WHITE_SLIP_STARTUP_WARMUP_ENABLED:
        _run_startup_warmup_task(
            task_name="white-slip local model",
            timeout_seconds=max(
                10.0,
                float(WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS) + 5.0,
            ),
            task_fn=_warmup_white_slip_runtime,
        )

def get_db():
    """
    数据库会话依赖：
    - 请求进入时创建 Session
    - 请求结束后关闭 Session
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class LoginRequest(BaseModel):
    """
    登录请求体模型：
    - username: 账号
    - id_card: 身份证号（示例字段）
    """

    username: str = Field(..., examples=["admin01"])
    id_card: str = Field(..., examples=["130102199001011234"])
    face_image_base64: Optional[str] = Field(
        default=None,
        examples=["data:image/jpeg;base64,/9j/4AAQSk..."],
        description="可选：用于人脸二次校验的图片 base64",
    )


class ReimbursementAuditRequest(BaseModel):
    status: str = Field(..., examples=["已打款"])
    comment: Optional[str] = Field(default=None, examples=["银行转账完成"])


class PolicyConsultRequest(BaseModel):
    question: str = Field(..., examples=["5000元招待费怎么报？"])


class NotificationPushRequest(BaseModel):
    title: str = Field(..., examples=["本月报账截止提醒"])
    content: str = Field(..., examples=["请在25号前完成本月报账单提交。"])
    target_role: str = Field(default="reporter", examples=["reporter"])


class NotificationDisableRequest(BaseModel):
    is_active: int = Field(default=0, examples=[0])


class QRInvoiceImportRequest(BaseModel):
    qr_content: str = Field(
        ...,
        examples=[
            "invoice_code=4400123450;invoice_number=12345678;amount=123.45;date=2026-03-26",
            "01,10,4400123450,12345678,123.45,20260326,ABCDEFGH",
        ],
    )
    reason: Optional[str] = Field(default="二维码导入电子发票")


class QRInvoiceParseRequest(BaseModel):
    qr_content: str = Field(
        ...,
        examples=[
            "invoice_code=4400123450;invoice_number=12345678;amount=123.45;date=2026-03-26",
            "01,10,4400123450,12345678,123.45,20260326,ABCDEFGH",
        ],
    )


class WhiteSlipNormalizeRequest(BaseModel):
    text_lines: List[str] = Field(
        default_factory=list,
        examples=[["白条", "事由: 修路人工费", "金额: 2600", "签字: 张三 李四"]],
    )
    raw_text: Optional[str] = Field(
        default=None, examples=["白条\n事由: 修路人工费\n金额: 2600"]
    )
    amount: Optional[float] = Field(default=None, examples=[2600.0])
    date: Optional[str] = Field(default=None, examples=["2026-03-27"])


class EmailSyncRequest(BaseModel):
    imap_host: str = Field(..., examples=["imap.example.com"])
    imap_port: int = Field(default=993, examples=[993])
    email_username: str = Field(..., examples=["village_finance@example.com"])
    email_password: str = Field(..., examples=["your-app-password"])
    folder: str = Field(default="INBOX", examples=["INBOX"])
    limit: int = Field(default=10, examples=[10])
    only_unseen: bool = Field(default=True, examples=[True])


class TemplateApplyRequest(BaseModel):
    template_key: str = Field(..., examples=["village_procurement"])
    amount: Optional[float] = Field(default=None, examples=[3200.0])
    reason: Optional[str] = Field(default=None, examples=["春耕物资采购报账"])
    extra: Optional[Dict[str, Any]] = Field(
        default=None,
        examples=[{"vendor": "某农资门市", "item_name": "复合肥"}],
    )


class InvoiceDuplicateCheckRequest(BaseModel):
    invoice_unique_code: Optional[str] = Field(
        default=None, examples=["4400123450-12345678"]
    )
    invoice_code: Optional[str] = Field(default=None, examples=["4400123450"])
    invoice_number: Optional[str] = Field(default=None, examples=["12345678"])


class FinanceSystemQueryRequest(BaseModel):
    start_date: Optional[str] = Field(default=None, examples=["2026-03-01"])
    end_date: Optional[str] = Field(default=None, examples=["2026-03-31"])
    status: Optional[str] = Field(default=None, examples=["待审核"])
    category: Optional[str] = Field(default=None, examples=["采购支出"])
    target_username: Optional[str] = Field(default=None, examples=["reporter01"])
    min_amount: Optional[float] = Field(default=0.0, examples=[0.0])
    max_amount: Optional[float] = Field(default=None, examples=[10000.0])
    query_all: bool = Field(default=False, examples=[False])
    limit: int = Field(default=50, examples=[50])
    offset: int = Field(default=0, examples=[0])


# ============================================================
# 6) 接口定义（按业务分组）
#    - 权限管控：/login /logout
#    - 链路保障：/upload-voucher /sync-pending /sync-status
#    - 业务查询：/account/balance /records/filter
#    - 审计系统：/logs
# ============================================================


@app.post("/login", tags=["权限管控"], summary="双重验证并生成令牌")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    """
    登录接口：
    - 校验账号+身份证
    - 通过后签发 token
    - 记录登录日志
    """
    if data.username not in VALID_USERS or VALID_USERS[data.username] != data.id_card:
        db.add(OperationLog(operator=data.username, action="尝试登录失败：身份不符"))
        db.commit()
        raise HTTPException(status_code=401, detail="身份验证失败")

    face_verified = False
    face_provider = None
    face_score = None
    has_face_image = bool((data.face_image_base64 or "").strip())
    if FACE_LOGIN_REQUIRED or has_face_image:
        if not FACE_RECOGNITION_ENABLED:
            db.add(
                OperationLog(
                    operator=data.username,
                    action="登录失败：人脸校验已请求但模块未启用",
                )
            )
            db.commit()
            raise HTTPException(
                status_code=503,
                detail="人脸识别模块未启用（FACE_RECOGNITION_ENABLED=false）",
            )
        if FACE_LOGIN_REQUIRED and not has_face_image:
            raise HTTPException(
                status_code=422,
                detail="登录已启用人脸校验，请提供 face_image_base64",
            )

        profile = _load_face_profile(db, data.username)
        if not profile or not profile.is_active:
            db.add(OperationLog(operator=data.username, action="登录失败：未注册人脸"))
            db.commit()
            raise HTTPException(
                status_code=422, detail="未注册人脸基准图，请先调用 /face/register"
            )
        try:
            reference_bytes = _load_face_reference_bytes(profile)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        probe_bytes = _decode_image_base64(data.face_image_base64)
        verify_result = _verify_face_pair(reference_bytes, probe_bytes, data.username)
        if verify_result.get("status") != "ok":
            db.add(
                OperationLog(
                    operator=data.username,
                    action=(
                        "登录失败：人脸校验异常 "
                        f"{verify_result.get('error') or verify_result.get('reason') or '未知错误'}"
                    ),
                )
            )
            db.commit()
            raise HTTPException(
                status_code=422,
                detail=(
                    "人脸校验失败: "
                    f"{verify_result.get('error') or verify_result.get('reason') or '未知错误'}"
                ),
            )
        if not verify_result.get("matched"):
            db.add(
                OperationLog(
                    operator=data.username,
                    action=(
                        "登录失败：人脸不匹配 "
                        f"provider={verify_result.get('provider')}, "
                        f"score={verify_result.get('score')}"
                    ),
                )
            )
            db.commit()
            raise HTTPException(status_code=401, detail="人脸验证失败")

        face_verified = True
        face_provider = verify_result.get("provider")
        face_score = verify_result.get("score")
        profile.last_verified_at = datetime.now()
        if isinstance(face_score, (int, float)):
            profile.last_score = float(face_score)
        db.add(profile)

    token = create_access_token(db, data.username)
    role = "会计" if get_user_role(data.username) == "accountant" else "报账员"
    db.add(
        OperationLog(
            operator=data.username,
            action=(
                "登录成功：发放访问令牌"
                + (
                    f"，人脸已校验 provider={face_provider}, score={face_score}"
                    if face_verified
                    else ""
                )
            ),
        )
    )
    db.commit()
    return {
        "status": "success",
        "access_token": token,
        "role": role,
        "expires_in_minutes": TOKEN_EXPIRE_MINUTES,
        "face_verified": face_verified,
        "face_provider": face_provider,
        "face_score": face_score,
    }


@app.post("/logout", tags=["权限管控"], summary="撤销当前令牌")
def logout(
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    退出接口：
    - 校验 token
    - 将当前 token 标记为已撤销
    - 记录退出日志
    """
    token_row = verify_access_token(db, username, access_token)
    token_row.revoked_at = datetime.now()
    db.add(OperationLog(operator=username, action="主动退出：令牌已撤销"))
    db.commit()
    return {"status": "success", "detail": "已退出登录"}


@app.post("/face/register", tags=["人脸识别"], summary="注册人脸基准图")
async def register_face_profile(
    username: str = Form(...),
    id_card: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not FACE_RECOGNITION_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="人脸识别模块未启用（FACE_RECOGNITION_ENABLED=false）",
        )
    if username not in VALID_USERS or VALID_USERS[username] != id_card:
        db.add(OperationLog(operator=username, action="人脸注册失败：身份不符"))
        db.commit()
        raise HTTPException(status_code=401, detail="身份验证失败")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="上传图片为空")
    if len(content) > FACE_MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"图片过大，超过限制 {FACE_MAX_IMAGE_BYTES} 字节",
        )

    encrypted_content = cipher.encrypt(content)
    saved_path = write_encrypted_file(
        "encrypted_storage/faces",
        file.filename or f"{username}_face.jpg",
        encrypted_content,
    )

    profile = _load_face_profile(db, username)
    old_path = None
    now = datetime.now()
    if profile:
        old_path = profile.encrypted_face_path
        profile.encrypted_face_path = saved_path
        profile.provider = FACE_PROVIDER or "auto"
        profile.is_active = 1
        profile.updated_at = now
    else:
        profile = FaceProfile(
            username=username,
            encrypted_face_path=saved_path,
            provider=FACE_PROVIDER or "auto",
            is_active=1,
            created_at=now,
            updated_at=now,
        )
    db.add(profile)
    db.add(
        OperationLog(
            operator=username,
            action=f"人脸注册成功：provider={FACE_PROVIDER or 'auto'}",
        )
    )
    db.commit()
    db.refresh(profile)

    if old_path and old_path != saved_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except Exception:
            pass

    return {
        "status": "success",
        "username": username,
        "provider": profile.provider,
        "updated_at": profile.updated_at.isoformat(),
    }


@app.post("/face/verify-image", tags=["人脸识别"], summary="核验上传人脸图片")
async def verify_face_image(
    username: str,
    file: UploadFile = File(...),
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    if not FACE_RECOGNITION_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="人脸识别模块未启用（FACE_RECOGNITION_ENABLED=false）",
        )

    profile = _load_face_profile(db, username)
    if not profile or not profile.is_active:
        raise HTTPException(
            status_code=422, detail="未注册人脸基准图，请先调用 /face/register"
        )
    try:
        reference_bytes = _load_face_reference_bytes(profile)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    probe_bytes = await file.read()
    if not probe_bytes:
        raise HTTPException(status_code=422, detail="上传图片为空")
    if len(probe_bytes) > FACE_MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"图片过大，超过限制 {FACE_MAX_IMAGE_BYTES} 字节",
        )

    verify_result = _verify_face_pair(reference_bytes, probe_bytes, username)
    if verify_result.get("status") != "ok":
        db.add(
            OperationLog(
                operator=username,
                action=(
                    "人脸核验失败："
                    f"{verify_result.get('error') or verify_result.get('reason') or '未知错误'}"
                ),
            )
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail=(
                "人脸核验失败: "
                f"{verify_result.get('error') or verify_result.get('reason') or '未知错误'}"
            ),
        )

    profile.last_verified_at = datetime.now()
    if isinstance(verify_result.get("score"), (int, float)):
        profile.last_score = float(verify_result["score"])
    db.add(profile)

    if not verify_result.get("matched"):
        db.add(
            OperationLog(
                operator=username,
                action=(
                    "人脸核验不通过："
                    f"provider={verify_result.get('provider')}, "
                    f"score={verify_result.get('score')}"
                ),
            )
        )
        db.commit()
        raise HTTPException(
            status_code=401,
            detail=(
                "人脸不匹配，验证未通过。"
                f"score={verify_result.get('score')}, threshold={verify_result.get('threshold')}"
            ),
        )

    db.add(
        OperationLog(
            operator=username,
            action=(
                "人脸核验通过："
                f"provider={verify_result.get('provider')}, "
                f"score={verify_result.get('score')}"
            ),
        )
    )
    db.commit()
    return {
        "status": "success",
        "matched": True,
        "provider": verify_result.get("provider"),
        "score": verify_result.get("score"),
        "threshold": verify_result.get("threshold"),
    }


@app.get("/face/profile", tags=["人脸识别"], summary="查询人脸注册状态")
def get_face_profile(
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    profile = _load_face_profile(db, username)
    if not profile:
        return {"status": "success", "enrolled": False}
    return {
        "status": "success",
        "enrolled": bool(profile.is_active),
        "provider": profile.provider,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
        "last_verified_at": (
            profile.last_verified_at.isoformat() if profile.last_verified_at else None
        ),
        "last_score": profile.last_score,
    }


@app.post("/upload-voucher", tags=["链路保障"], summary="加密上传与断网处理")
async def upload_voucher(
    username: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    access_token: str = Header(default=None),
    source_device: str = Header(default="manual", alias="X-Source-Device"),
    storage_location: Optional[str] = Header(default=None, alias="X-Storage-Location"),
    storage_box: Optional[str] = Header(default=None, alias="X-Storage-Box"),
    storage_operator: Optional[str] = Header(default=None, alias="X-Storage-Operator"),
):
    """
    上传凭证接口：
    - 校验 token
    - 读取文件后调用 OCR（自动选择 PaddleOCR / 百度OCR）
    - 识别金额、日期、发票代码
    - 若判定为“白条”，额外提取事由和签字人
    - 对原文件进行加密存储
    - 网络可用：写入 synced 目录
    - 网络不可用：写入 pending 目录 + 入库离线队列
    - 返回结构化 OCR 结果并记录审计日志
    """
    verify_access_token(db, username, access_token)

    try:
        content = await file.read()
        # OCR/模型调用为阻塞操作，放入线程池避免卡住事件循环。
        ocr_result = await run_in_threadpool(perform_ocr, content, file.filename)
        ocr_status = (ocr_result.get("status") or "").strip().lower()
        if ocr_status in {"failed", "empty"}:
            ocr_error = (
                ocr_result.get("error") or "OCR did not return usable text"
            ).strip()
            fail_open = OCR_FAIL_OPEN and (not OCR_REQUIRED)
            db.add(
                OperationLog(
                    operator=username,
                    action=(
                        f"OCR识别失败：来源={source_device}，文件={sanitize_filename(file.filename)}，"
                        f"status={ocr_status}，error={ocr_error}"
                        + (
                            "；已启用 fail-open 继续入库"
                            if fail_open
                            else "；已阻断上传"
                        )
                    ),
                )
            )
            if not fail_open:
                db.commit()
                raise HTTPException(status_code=422, detail=f"OCR识别失败: {ocr_error}")
            ocr_result = dict(ocr_result)
            ocr_result["warning"] = f"OCR识别失败已降级处理并继续入库: {ocr_error}"
        ocr_summary = summarize_ocr_for_log(ocr_result)
        ocr_fields = ocr_result.get("fields", {}) or {}
        amount_raw = ocr_fields.get("amount")
        amount = float(amount_raw) if isinstance(amount_raw, (int, float)) else 0.0
        voucher_date = ocr_fields.get("date")
        invoice_code = normalize_invoice_code(ocr_fields.get("invoice_code"))
        invoice_number = normalize_invoice_number(ocr_fields.get("invoice_number"))
        reason = extract_reason_from_ocr(ocr_result)
        category = infer_category(reason, ocr_result)
        policy_result = apply_policy_rules(reason, amount, ocr_result=ocr_result)
        reimbursement_status = policy_result["status"]
        is_e_invoice = is_electronic_invoice(ocr_result, file.filename)
        stored_at = (
            datetime.now()
            if (storage_location and storage_location.strip())
            or (storage_box and storage_box.strip())
            else None
        )

        # 关卡1：查重（仅当识别到发票代码时）
        duplicated = find_duplicate_invoice(db, invoice_code, invoice_number)
        if duplicated:
            duplicated_at = duplicated.created_at.strftime("%m月%d日")
            db.add(
                OperationLog(
                    operator=username,
                    action=(
                        f"尝试报销发票 {invoice_code} 失败：已于 {duplicated_at} 报销过"
                    ),
                )
            )
            db.commit()
            raise HTTPException(
                status_code=409,
                detail=f"发票代码重复，疑似重复报账：{invoice_code}（已存在id={duplicated.id}）",
            )

        # 关卡2：电子发票验真（按配置决定是否严格阻断）
        tax_verify_result = {
            "checked": False,
            "passed": True,
            "message": "非电子发票，无需验真",
        }
        if is_e_invoice:
            tax_verify_result = verify_e_invoice_with_tax_api(
                invoice_code=invoice_code,
                amount=amount,
                voucher_date=voucher_date,
            )
            should_block = (not tax_verify_result.get("passed")) and (
                tax_verify_result.get("checked") or TAX_VERIFY_STRICT
            )
            if should_block:
                db.add(
                    OperationLog(
                        operator=username,
                        action=(
                            f"尝试报销发票 {invoice_code or '未知代码'} 失败："
                            f"电子发票验真未通过({tax_verify_result.get('message')})"
                        ),
                    )
                )
                db.commit()
                raise HTTPException(
                    status_code=422,
                    detail=f"电子发票验真未通过：{tax_verify_result.get('message')}",
                )

        if not is_e_invoice:
            verify_status = "not_required"
        elif tax_verify_result.get("checked") and tax_verify_result.get("passed"):
            verify_status = "verified"
        elif tax_verify_result.get("checked") and not tax_verify_result.get("passed"):
            verify_status = "failed"
        else:
            verify_status = "unverified"

        encrypted_content = cipher.encrypt(content)

        # 网络不可用：进入离线补传流程
        if not check_real_network():
            pending_path = write_encrypted_file(
                "encrypted_storage/pending", file.filename, encrypted_content
            )
            task = PendingUpload(
                username=username,
                original_filename=sanitize_filename(file.filename),
                encrypted_path=pending_path,
                status="pending",
                created_at=datetime.now(),
            )
            db.add(task)
            db.add(
                OperationLog(
                    operator=username,
                    action=f"网络断开：来源={source_device}，凭证 {sanitize_filename(file.filename)} 已持久化到离线队列；{ocr_summary}",
                )
            )
            reimbursement = Reimbursement(
                username=username,
                amount=amount,
                category=category,
                reason=reason,
                status=reimbursement_status,
                invoice_code=invoice_code,
                image_path=pending_path,
                voucher_date=voucher_date,
                source_device=source_device,
                verify_status=verify_status,
                verify_message=(tax_verify_result.get("message") or "")[:250],
                physical_storage_location=(storage_location or "").strip() or None,
                box_id=(storage_box or "").strip() or None,
                physical_stored_by=(storage_operator or "").strip() or None,
                physical_stored_at=stored_at,
            )
            db.add(reimbursement)
            db.commit()
            db.refresh(task)
            db.refresh(reimbursement)
            return {
                "status": "cached",
                "message": "已自动转入断网补传模式",
                "pending_id": task.id,
                "reimbursement_id": reimbursement.id,
                "reimbursement_status": reimbursement.status,
                "source_device": source_device,
                "policy": policy_result,
                "tax_verification": tax_verify_result,
                "ocr": ocr_result,
            }

        # 网络可用：直接入在线存储目录
        synced_path = write_encrypted_file(
            "encrypted_storage/synced", file.filename, encrypted_content
        )
        reimbursement = Reimbursement(
            username=username,
            amount=amount,
            category=category,
            reason=reason,
            status=reimbursement_status,
            invoice_code=invoice_code,
            image_path=synced_path,
            voucher_date=voucher_date,
            source_device=source_device,
            verify_status=verify_status,
            verify_message=(tax_verify_result.get("message") or "")[:250],
            physical_storage_location=(storage_location or "").strip() or None,
            box_id=(storage_box or "").strip() or None,
            physical_stored_by=(storage_operator or "").strip() or None,
            physical_stored_at=stored_at,
        )
        db.add(reimbursement)
        db.add(
            OperationLog(
                operator=username,
                action=f"凭证上报成功：来源={source_device}，加密存储 {synced_path}；状态={reimbursement_status}；{ocr_summary}",
            )
        )
        db.commit()
        db.refresh(reimbursement)
        return {
            "status": "success",
            "detail": "数据已上报云端",
            "reimbursement_id": reimbursement.id,
            "reimbursement_status": reimbursement.status,
            "source_device": source_device,
            "policy": policy_result,
            "tax_verification": tax_verify_result,
            "ocr": ocr_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"错误: {str(e)}")


@app.post("/sync-pending", tags=["链路保障"], summary="手动同步离线队列")
def sync_pending_uploads(
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    手动补传接口：
    - 校验 token
    - 检查网络可用性
    - 扫描 pending 状态任务并尝试标记为 synced
    - 记录补传日志
    - 返回补传统计
    """
    verify_access_token(db, username, access_token)

    if not check_real_network():
        raise HTTPException(status_code=503, detail="当前网络不可用，无法执行补传")

    pending_items = (
        db.execute(
            select(PendingUpload)
            .where(PendingUpload.status == "pending")
            .order_by(PendingUpload.created_at.asc())
        )
        .scalars()
        .all()
    )

    synced_count = 0
    failed_count = 0

    for item in pending_items:
        try:
            # 这里为演示逻辑：仅更新状态，实际项目可在此调用远端上传 API
            item.status = "synced"
            item.synced_at = datetime.now()
            item.error_message = None
            db.add(
                OperationLog(
                    operator=username,
                    action=f"离线补传成功：{item.original_filename} (id={item.id})",
                )
            )
            synced_count += 1
        except Exception as e:
            item.status = "failed"
            item.error_message = str(e)[:250]
            failed_count += 1

    db.commit()

    pending_count = (
        db.execute(select(PendingUpload).where(PendingUpload.status == "pending"))
        .scalars()
        .all()
    )

    return {
        "status": "success",
        "synced_count": synced_count,
        "failed_count": failed_count,
        "pending_count": len(pending_count),
    }


@app.get("/account/balance", tags=["业务查询"], summary="查询村集体账户余额")
def get_balance(
    username: str,
    db: Session = Depends(get_db),
    access_token: str = Header(default=None),
):
    """
    余额查询接口（演示数据）：
    - 校验 token
    - 返回模拟余额
    - 记录日志
    """
    verify_access_token(db, username, access_token)

    mock_balance = 125800.50
    db.add(OperationLog(operator=username, action="查询村集体账户余额"))
    db.commit()
    return {"village": "石家庄某村", "balance": mock_balance, "currency": "CNY"}


@app.get("/records/filter", tags=["业务查询"], summary="报账记录筛选查询")
def filter_records(
    username: str,
    record_type: Optional[str] = None,
    min_amount: float = 0.0,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    记录筛选接口（演示数据）：
    - 校验 token
    - 根据类型与最小金额过滤记录
    - 记录日志
    """
    verify_access_token(db, username, access_token)

    mock_records = [
        {"id": 1, "type": "务工补贴", "amount": 500.0, "status": "已到账"},
        {"id": 2, "type": "基础设施维修", "amount": 12000.0, "status": "审核中"},
        {"id": 3, "type": "办公用品采购", "amount": 2300.5, "status": "已完成"},
    ]
    filtered = [
        r
        for r in mock_records
        if (not record_type or r["type"] == record_type) and r["amount"] >= min_amount
    ]

    db.add(
        OperationLog(operator=username, action=f"执行记录筛选: {record_type or 'ALL'}")
    )
    db.commit()
    return filtered


@app.get("/reimbursements", tags=["业务查询"], summary="查询报账单（业务表）")
def list_reimbursements(
    username: str,
    access_token: str = Header(default=None),
    status: Optional[str] = None,
    category: Optional[str] = None,
    min_amount: float = 0.0,
    query_all: bool = False,
    db: Session = Depends(get_db),
):
    """
    报账单查询接口：
    - 从 Reimbursement 业务表读取
    - 支持按状态/类别/最小金额过滤
    """
    verify_access_token(db, username, access_token)

    stmt = select(Reimbursement)
    if not query_all:
        stmt = stmt.where(Reimbursement.username == username)
    else:
        ensure_accountant_permission(username)
    if status:
        stmt = stmt.where(Reimbursement.status == status)
    if category:
        stmt = stmt.where(Reimbursement.category == category)
    stmt = stmt.where(Reimbursement.amount >= min_amount).order_by(
        Reimbursement.created_at.desc()
    )

    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": r.id,
            "amount": r.amount,
            "category": r.category,
            "reason": r.reason,
            "status": r.status,
            "invoice_code": r.invoice_code,
            "image_path": r.image_path,
            "voucher_date": r.voucher_date,
            "source_device": r.source_device,
            "verify_status": r.verify_status,
            "verify_message": r.verify_message,
            "physical_storage_location": r.physical_storage_location,
            "box_id": r.box_id,
            "physical_stored_by": r.physical_stored_by,
            "physical_stored_at": (
                r.physical_stored_at.isoformat() if r.physical_stored_at else None
            ),
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.post(
    "/finance-system/reimbursements/query",
    tags=["业务查询扩展"],
    summary="对接财务系统查询报账单",
)
def query_finance_system_reimbursements(
    payload: FinanceSystemQueryRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    target_username = (payload.target_username or "").strip() or None
    min_amount = float(payload.min_amount or 0.0)
    max_amount = (
        float(payload.max_amount)
        if isinstance(payload.max_amount, (int, float))
        else None
    )
    if max_amount is not None and min_amount > max_amount:
        raise HTTPException(
            status_code=422, detail="min_amount cannot be greater than max_amount"
        )

    start_date = None
    if payload.start_date:
        start_date = _normalize_date(payload.start_date) or _normalize_compact_date(
            payload.start_date
        )
        if not start_date:
            raise HTTPException(
                status_code=422,
                detail="invalid start_date, expected YYYY-MM-DD or YYYYMMDD",
            )
    end_date = None
    if payload.end_date:
        end_date = _normalize_date(payload.end_date) or _normalize_compact_date(
            payload.end_date
        )
        if not end_date:
            raise HTTPException(
                status_code=422,
                detail="invalid end_date, expected YYYY-MM-DD or YYYYMMDD",
            )

    if payload.query_all or (target_username and target_username != username):
        ensure_accountant_permission(username)

    if start_date and end_date:
        start_dt_check = _parse_date_safe(start_date)
        end_dt_check = _parse_date_safe(end_date)
        if start_dt_check and end_dt_check and start_dt_check > end_dt_check:
            raise HTTPException(
                status_code=422, detail="start_date must be <= end_date"
            )

    request_payload = payload.dict()
    request_payload["request_username"] = username
    external_error: Optional[str] = None

    if FINANCE_SYSTEM_QUERY_URL:
        try:
            response = httpx.post(
                FINANCE_SYSTEM_QUERY_URL,
                json=request_payload,
                timeout=FINANCE_SYSTEM_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            db.add(
                OperationLog(
                    operator=username,
                    action="财务系统查询成功（external）",
                )
            )
            db.commit()
            return {"status": "success", "source": "finance_system", "data": data}
        except Exception as exc:
            external_error = str(exc)

    stmt = select(Reimbursement)
    if payload.query_all:
        stmt = stmt
    elif target_username:
        stmt = stmt.where(Reimbursement.username == target_username)
    else:
        stmt = stmt.where(Reimbursement.username == username)

    if payload.status:
        stmt = stmt.where(Reimbursement.status == payload.status)
    if payload.category:
        stmt = stmt.where(Reimbursement.category == payload.category)
    stmt = stmt.where(Reimbursement.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(Reimbursement.amount <= max_amount)

    rows = db.execute(stmt.order_by(Reimbursement.created_at.desc())).scalars().all()

    start_dt = _parse_date_safe(start_date) if start_date else None
    end_dt = _parse_date_safe(end_date) if end_date else None

    if start_dt or end_dt:
        filtered_rows = []
        for row in rows:
            row_dt = _parse_date_safe(row.voucher_date) or row.created_at
            row_day = row_dt.date()
            if start_dt and row_day < start_dt.date():
                continue
            if end_dt and row_day > end_dt.date():
                continue
            filtered_rows.append(row)
        rows = filtered_rows

    cap_limit = max(1, min(int(payload.limit or 50), 200))
    cap_offset = max(0, int(payload.offset or 0))
    paged_rows = rows[cap_offset : cap_offset + cap_limit]

    db.add(
        OperationLog(
            operator=username,
            action="财务系统查询完成（local_fallback）",
        )
    )
    db.commit()

    return {
        "status": "success",
        "source": "local_fallback",
        "external_error": external_error,
        "query_scope": (
            "all"
            if payload.query_all
            else (target_username if target_username else username)
        ),
        "total": len(rows),
        "offset": cap_offset,
        "limit": cap_limit,
        "has_more": (cap_offset + cap_limit) < len(rows),
        "items": [
            {
                "id": r.id,
                "username": r.username,
                "amount": r.amount,
                "category": r.category,
                "reason": r.reason,
                "status": r.status,
                "invoice_code": r.invoice_code,
                "voucher_date": r.voucher_date,
                "created_at": r.created_at.isoformat(),
            }
            for r in paged_rows
        ],
    }


@app.patch(
    "/reimbursements/{reimbursement_id}/audit",
    tags=["审核后台"],
    summary="会计审核报账单状态",
)
def audit_reimbursement(
    reimbursement_id: int,
    payload: ReimbursementAuditRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    审核接口：
    - 仅会计权限用户可调用
    - 修改报账单状态（如：待审核 -> 已打款）
    """
    verify_access_token(db, username, access_token)
    ensure_accountant_permission(username)

    if payload.status not in AUDIT_ALLOWED_STATUS:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的审核状态: {payload.status}，可选: {sorted(AUDIT_ALLOWED_STATUS)}",
        )

    reimbursement = db.get(Reimbursement, reimbursement_id)
    if not reimbursement:
        raise HTTPException(status_code=404, detail="报账单不存在")

    old_status = reimbursement.status
    reimbursement.status = payload.status
    comment = (payload.comment or "").strip()
    if comment:
        reimbursement.verify_message = comment[:250]

    db.add(
        OperationLog(
            operator=username,
            action=(
                f"审核报账单 id={reimbursement_id}: "
                f"{old_status} -> {payload.status}"
                + (f"，备注={comment}" if comment else "")
            ),
        )
    )
    db.commit()
    db.refresh(reimbursement)

    return {
        "status": "success",
        "reimbursement_id": reimbursement.id,
        "old_status": old_status,
        "new_status": reimbursement.status,
        "audit_by": username,
        "reason": reimbursement.reason,
        "amount": reimbursement.amount,
    }


@app.get(
    "/reimbursements/summary", tags=["业务查询"], summary="报账汇总统计（周报/月报）"
)
def reimbursements_summary(
    username: str,
    access_token: str = Header(default=None),
    group_by: str = "month",
    status: Optional[str] = None,
    category: Optional[str] = None,
    query_all: bool = False,
    db: Session = Depends(get_db),
):
    """
    报账汇总接口：
    - group_by: month/category/status
    - 支持按状态和类别过滤
    """
    verify_access_token(db, username, access_token)
    if group_by not in {"month", "category", "status"}:
        raise HTTPException(
            status_code=422, detail="group_by 仅支持 month/category/status"
        )

    stmt = select(Reimbursement)
    if not query_all:
        stmt = stmt.where(Reimbursement.username == username)
    else:
        ensure_accountant_permission(username)
    if status:
        stmt = stmt.where(Reimbursement.status == status)
    if category:
        stmt = stmt.where(Reimbursement.category == category)

    rows = db.execute(stmt).scalars().all()
    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"amount": 0.0, "count": 0}
    )
    for row in rows:
        key = get_summary_group_key(row, group_by)
        buckets[key]["amount"] += float(row.amount or 0.0)
        buckets[key]["count"] += 1

    items = [
        {
            "key": key,
            "amount": round(value["amount"], 2),
            "count": value["count"],
        }
        for key, value in sorted(buckets.items(), key=lambda kv: kv[0])
    ]
    total_amount = round(sum(item["amount"] for item in items), 2)
    total_count = sum(item["count"] for item in items)

    return {
        "group_by": group_by,
        "total_amount": total_amount,
        "total_count": total_count,
        "items": items,
    }


@app.get(
    "/reimbursement-templates",
    tags=["业务查询扩展"],
    summary="获取报账模板列表",
)
def list_reimbursement_templates(
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    items = []
    for key, item in REIMBURSEMENT_TEMPLATES.items():
        items.append(
            {
                "template_key": key,
                "name": item.get("name"),
                "description": item.get("description"),
                "category": item.get("category"),
                "default_reason": item.get("default_reason"),
                "required_fields": item.get("required_fields", []),
                "policy_hint": item.get("policy_hint"),
                "default_amount": float(item.get("default_amount") or 0.0),
            }
        )
    return {"count": len(items), "items": items}


@app.post(
    "/reimbursement-templates/apply",
    tags=["业务查询扩展"],
    summary="应用报账模板生成预填数据",
)
def apply_reimbursement_template(
    payload: TemplateApplyRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    prefill = build_template_prefill(
        payload.template_key,
        amount=payload.amount,
        reason=payload.reason,
        extra=payload.extra,
    )
    return {"status": "success", "prefill": prefill}


@app.post("/policy/consult", tags=["政策咨询"], summary="政策咨询（文档检索 + 大模型）")
def policy_consult(
    payload: PolicyConsultRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    政策咨询原型接口：
    - 从本地政策文档检索上下文
    - 调用 Gemini / Qwen / 规则降级回答
    """
    verify_access_token(db, username, access_token)
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question 不能为空")

    answer = answer_policy_question(question)
    db.add(
        OperationLog(
            operator=username,
            action=(
                f"政策咨询: provider={answer.get('provider')} question={question[:60]}"
            ),
        )
    )
    db.commit()
    return answer


@app.post("/invoices/parse-qr", tags=["电子发票"], summary="扫码解析与查重预检")
def parse_invoice_qr(
    payload: QRInvoiceParseRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    扫码预检接口：
    - 解析二维码内容并结构化返回
    - 基于发票唯一标识执行查重预检（导入前）
    """
    verify_access_token(db, username, access_token)
    parsed = parse_qr_invoice_payload(payload.qr_content)
    unique_code = parsed.get("invoice_unique_code") or parsed.get("invoice_code")

    duplicate = find_duplicate_invoice(
        db,
        parsed.get("invoice_code"),
        parsed.get("invoice_number"),
    )
    duplicate_payload = None
    if duplicate:
        duplicate_payload = {
            "reimbursement_id": duplicate.id,
            "username": duplicate.username,
            "amount": duplicate.amount,
            "created_at": duplicate.created_at.isoformat(),
        }

    return {
        "status": "success",
        "parsed": parsed,
        "invoice_unique_code": unique_code,
        "is_duplicate": bool(duplicate),
        "duplicate": duplicate_payload,
        "print_advice": "skip_print" if duplicate else "ok_to_print",
    }


@app.post("/invoices/check-duplicate", tags=["发票查重"], summary="检查发票是否重复")
def check_invoice_duplicate(
    payload: InvoiceDuplicateCheckRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    normalized_unique = normalize_invoice_code(payload.invoice_unique_code)
    invoice_code = payload.invoice_code
    invoice_number = payload.invoice_number
    if normalized_unique and not invoice_code:
        if "-" in normalized_unique and not invoice_number:
            invoice_code, invoice_number = normalized_unique.split("-", 1)
        else:
            invoice_code = normalized_unique

    candidates = build_invoice_lookup_candidates(invoice_code, invoice_number)
    if normalized_unique and normalized_unique not in candidates:
        candidates.insert(0, normalized_unique)
    if not candidates:
        raise HTTPException(
            status_code=422,
            detail="invoice_unique_code or invoice_code/invoice_number is required",
        )

    duplicate = find_duplicate_invoice_by_candidates(db, candidates)
    duplicate_payload = None
    if duplicate:
        duplicate_payload = {
            "reimbursement_id": duplicate.id,
            "username": duplicate.username,
            "amount": duplicate.amount,
            "created_at": duplicate.created_at.isoformat(),
            "invoice_code": duplicate.invoice_code,
        }

    return {
        "status": "success",
        "lookup_candidates": candidates,
        "is_duplicate": bool(duplicate),
        "duplicate": duplicate_payload,
    }


@app.post(
    "/white-slips/ai-parse-image",
    tags=["白条识别"],
    summary="智能解析白条图片",
)
async def ai_parse_white_slip_image(
    username: str,
    file: UploadFile = File(...),
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=422, detail="上传图片为空")

    local_timeout = max(2.0, float(WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS) + 3.0)
    local_prefetch_task = asyncio.create_task(
        asyncio.wait_for(
            run_in_threadpool(
                _call_white_slip_local_model,
                text_lines=[],
                full_text="",
                file_bytes=file_bytes,
                filename=file.filename,
            ),
            timeout=local_timeout,
        )
    )

    ocr_timeout = max(3.0, float(WHITE_SLIP_IMAGE_OCR_TIMEOUT_SECONDS))
    try:
        ocr_result = await asyncio.wait_for(
            run_in_threadpool(perform_ocr, file_bytes, file.filename),
            timeout=ocr_timeout,
        )
    except asyncio.TimeoutError:
        ocr_result = {
            "status": "failed",
            "provider": OCR_PROVIDER,
            "error": f"白条OCR超时（{ocr_timeout:.0f}s）",
            "fields": _empty_ocr_fields(),
            "document_type": "white_slip",
            "text_lines": [],
        }

    text_lines = [
        line.strip()
        for line in (ocr_result.get("text_lines") or [])
        if isinstance(line, str) and line.strip()
    ]
    full_text = "\n".join(text_lines)
    rule_fields = extract_white_slip_fields(text_lines, full_text)
    core_fields = _empty_ocr_fields()
    ocr_fields = ocr_result.get("fields", {}) or {}
    if isinstance(ocr_fields, dict):
        for key in core_fields.keys():
            value = ocr_fields.get(key)
            if value not in ("", []):
                core_fields[key] = value

    try:
        ai_result = await asyncio.wait_for(
            run_in_threadpool(
                _call_white_slip_ai,
                text_lines=text_lines,
                full_text=full_text,
                file_bytes=file_bytes,
                filename=file.filename,
            ),
            timeout=max(2.0, float(WHITE_SLIP_AI_TIMEOUT_SECONDS) + 3.0),
        )
    except asyncio.TimeoutError:
        ai_result = {
            "status": "failed",
            "provider": WHITE_SLIP_AI_PROVIDER,
            "model": WHITE_SLIP_AI_MODEL,
            "error": f"白条AI超时（{WHITE_SLIP_AI_TIMEOUT_SECONDS:.0f}s）",
        }

    local_fallback_result = {
        "status": "skipped",
        "provider": "local_model",
        "reason": "AI链路成功，未启用本地回退",
    }
    if ai_result.get("status") == "ok":
        local_prefetch_task.cancel()
    else:
        try:
            local_fallback_result = await local_prefetch_task
        except asyncio.TimeoutError:
            local_fallback_result = {
                "status": "failed",
                "provider": "local_model",
                "model": (
                    f"{WHITE_SLIP_LOCAL_MODEL_MODULE}.{WHITE_SLIP_LOCAL_MODEL_FUNCTION}"
                ),
                "error": f"白条本地模型超时（{WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS:.0f}s）",
            }
        except asyncio.CancelledError:
            local_fallback_result = {
                "status": "failed",
                "provider": "local_model",
                "error": "白条本地模型任务被取消",
            }

        if local_fallback_result.get("status") != "ok" and text_lines:
            try:
                local_fallback_result = await asyncio.wait_for(
                    run_in_threadpool(
                        _call_white_slip_local_model,
                        text_lines=text_lines,
                        full_text=full_text,
                        file_bytes=file_bytes,
                        filename=file.filename,
                    ),
                    timeout=local_timeout,
                )
            except asyncio.TimeoutError:
                pass

    ai_fields = ai_result.get("fields", {}) if ai_result.get("status") == "ok" else {}
    local_fields = (
        local_fallback_result.get("fields", {})
        if local_fallback_result.get("status") == "ok"
        else {}
    )
    white_slip_fields = _merge_white_slip_fields(rule_fields, ai_fields)
    white_slip_fields = _merge_white_slip_fields(white_slip_fields, local_fields)
    if core_fields.get("amount") is None and isinstance(
        white_slip_fields.get("amount"), (int, float)
    ):
        core_fields["amount"] = float(white_slip_fields["amount"])
    if not core_fields.get("date") and white_slip_fields.get("date"):
        core_fields["date"] = white_slip_fields["date"]

    standardized = standardize_white_slip_document(
        text_lines,
        full_text,
        core_fields,
        white_slip_fields,
    )
    if not standardized.get("payer") and white_slip_fields.get("payer"):
        standardized["payer"] = white_slip_fields["payer"]
    if not standardized.get("payee") and white_slip_fields.get("payee"):
        standardized["payee"] = white_slip_fields["payee"]
    if white_slip_fields.get("slip_type"):
        standardized["slip_type"] = white_slip_fields["slip_type"]

    if (ai_result.get("status") != "ok") and (
        not _has_meaningful_white_slip_fields(white_slip_fields)
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "白条AI解析失败，且OCR未能提取到有效白条字段: "
                f"{ai_result.get('error') or ai_result.get('reason') or '未知错误'}"
            ),
        )

    warning = None
    if ai_result.get("status") != "ok":
        warning = (
            "白条AI模型失败，已使用OCR+规则提取结果: "
            f"{ai_result.get('error') or ai_result.get('reason') or '未知错误'}"
        )

    return {
        "status": "success",
        "document_type": "white_slip",
        "fields": core_fields,
        "white_slip": white_slip_fields,
        "white_slip_standard": standardized,
        "white_slip_ai": ai_result,
        "white_slip_local_model": local_fallback_result,
        "ocr": ocr_result,
        "warning": warning,
    }


@app.post("/white-slips/normalize", tags=["白条处理"], summary="白条结构化转换")
def normalize_white_slip(
    payload: WhiteSlipNormalizeRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    白条标准化接口：
    - 输入 OCR 文本行或原始文本
    - 输出标准结构字段与缺失项提示
    """
    verify_access_token(db, username, access_token)

    lines = [
        line.strip() for line in (payload.text_lines or []) if line and line.strip()
    ]
    if payload.raw_text and payload.raw_text.strip():
        raw_lines = [
            line.strip() for line in payload.raw_text.splitlines() if line.strip()
        ]
        lines.extend(raw_lines)
    lines = list(dict.fromkeys(lines))

    if not lines:
        raise HTTPException(status_code=422, detail="text_lines/raw_text 至少提供一项")

    full_text = "\n".join(lines)
    core_fields = extract_voucher_core_fields(full_text)
    if payload.amount is not None:
        core_fields["amount"] = float(payload.amount)
    if payload.date:
        normalized_date = _normalize_date(payload.date) or _normalize_compact_date(
            payload.date
        )
        core_fields["date"] = normalized_date or payload.date

    white_slip_payload = build_white_slip_structured_payload(
        lines,
        full_text,
        core_fields,
    )
    return {
        "status": "success",
        "document_type": "white_slip",
        "fields": core_fields,
        "white_slip": white_slip_payload["white_slip"],
        "white_slip_standard": white_slip_payload["white_slip_standard"],
        "white_slip_ai": white_slip_payload["white_slip_ai"],
        "white_slip_local_model": white_slip_payload["white_slip_local_model"],
    }


@app.post(
    "/white-slips/local-model-parse-image",
    tags=["白条识别"],
    summary="本地模型解析白条图片",
)
async def local_model_parse_white_slip_image(
    username: str,
    file: UploadFile = File(...),
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    verify_access_token(db, username, access_token)
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=422, detail="上传图片为空")

    local_timeout = max(2.0, float(WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS) + 3.0)
    local_prefetch_task = asyncio.create_task(
        asyncio.wait_for(
            run_in_threadpool(
                _call_white_slip_local_model,
                text_lines=[],
                full_text="",
                file_bytes=file_bytes,
                filename=file.filename,
            ),
            timeout=local_timeout,
        )
    )

    ocr_timeout = max(3.0, float(WHITE_SLIP_IMAGE_OCR_TIMEOUT_SECONDS))
    try:
        ocr_result = await asyncio.wait_for(
            run_in_threadpool(perform_ocr, file_bytes, file.filename),
            timeout=ocr_timeout,
        )
    except asyncio.TimeoutError:
        ocr_result = {
            "status": "failed",
            "provider": OCR_PROVIDER,
            "error": f"白条OCR超时（{ocr_timeout:.0f}s）",
            "fields": _empty_ocr_fields(),
            "document_type": "white_slip",
            "text_lines": [],
        }

    text_lines = [
        line.strip()
        for line in (ocr_result.get("text_lines") or [])
        if isinstance(line, str) and line.strip()
    ]
    full_text = "\n".join(text_lines)
    rule_fields = extract_white_slip_fields(text_lines, full_text)
    core_fields = _empty_ocr_fields()
    ocr_fields = ocr_result.get("fields", {}) or {}
    if isinstance(ocr_fields, dict):
        for key in core_fields.keys():
            value = ocr_fields.get(key)
            if value not in ("", []):
                core_fields[key] = value

    try:
        local_result = await local_prefetch_task
    except asyncio.TimeoutError:
        local_result = {
            "status": "failed",
            "provider": "local_model",
            "model": (
                f"{WHITE_SLIP_LOCAL_MODEL_MODULE}.{WHITE_SLIP_LOCAL_MODEL_FUNCTION}"
            ),
            "error": f"白条本地模型超时（{WHITE_SLIP_LOCAL_MODEL_TIMEOUT_SECONDS:.0f}s）",
        }

    if local_result.get("status") != "ok" and text_lines:
        try:
            local_result = await asyncio.wait_for(
                run_in_threadpool(
                    _call_white_slip_local_model,
                    text_lines=text_lines,
                    full_text=full_text,
                    file_bytes=file_bytes,
                    filename=file.filename,
                ),
                timeout=local_timeout,
            )
        except asyncio.TimeoutError:
            pass

    local_fields = (
        local_result.get("fields", {}) if local_result.get("status") == "ok" else {}
    )
    white_slip_fields = _merge_white_slip_fields(rule_fields, local_fields)
    if core_fields.get("amount") is None and isinstance(
        white_slip_fields.get("amount"), (int, float)
    ):
        core_fields["amount"] = float(white_slip_fields["amount"])
    if not core_fields.get("date") and white_slip_fields.get("date"):
        core_fields["date"] = white_slip_fields["date"]

    standardized = standardize_white_slip_document(
        text_lines,
        full_text,
        core_fields,
        white_slip_fields,
    )
    if not standardized.get("payer") and white_slip_fields.get("payer"):
        standardized["payer"] = white_slip_fields["payer"]
    if not standardized.get("payee") and white_slip_fields.get("payee"):
        standardized["payee"] = white_slip_fields["payee"]
    if white_slip_fields.get("slip_type"):
        standardized["slip_type"] = white_slip_fields["slip_type"]

    if (local_result.get("status") != "ok") and (
        not _has_meaningful_white_slip_fields(white_slip_fields)
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "白条本地模型解析失败: "
                f"{local_result.get('error') or local_result.get('reason') or '未知错误'}"
            ),
        )

    warning = None
    if local_result.get("status") != "ok":
        warning = (
            "白条本地模型失败，已使用OCR+规则提取结果: "
            f"{local_result.get('error') or local_result.get('reason') or '未知错误'}"
        )

    return {
        "status": "success",
        "document_type": "white_slip",
        "fields": core_fields,
        "white_slip": white_slip_fields,
        "white_slip_standard": standardized,
        "white_slip_local_model": local_result,
        "ocr": ocr_result,
        "warning": warning,
    }


@app.post("/invoices/import-by-qr", tags=["电子发票"], summary="扫码导入电子发票")
def import_invoice_by_qr(
    payload: QRInvoiceImportRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    扫码导入接口：
    - 解析发票二维码内容
    - 执行查重/验真/规则引擎
    - 落库 Reimbursement
    """
    verify_access_token(db, username, access_token)
    parsed = parse_qr_invoice_payload(payload.qr_content)

    invoice_code = parsed.get("invoice_code")
    invoice_number = parsed.get("invoice_number")
    invoice_unique_code = parsed.get("invoice_unique_code") or invoice_code
    if not invoice_unique_code:
        raise HTTPException(status_code=422, detail="二维码中未识别到有效发票标识")

    amount = float(parsed.get("amount") or 0.0)
    voucher_date = parsed.get("voucher_date")
    reason = (payload.reason or "二维码导入电子发票").strip() or "二维码导入电子发票"

    result = _create_reimbursement_from_invoice_data(
        db,
        username=username,
        amount=amount,
        category="电子发票",
        reason=reason,
        invoice_code=invoice_unique_code,
        voucher_date=voucher_date,
        image_path=f"qr://{invoice_unique_code}",
        source_device="qr-import",
        is_e_invoice=True,
    )
    row = result["reimbursement"]

    db.add(
        OperationLog(
            operator=username,
            action=(
                f"扫码导入电子发票成功：invoice={invoice_unique_code}, "
                f"reimbursement_id={row.id}"
            ),
        )
    )
    db.commit()

    return {
        "status": "success",
        "reimbursement_id": row.id,
        "reimbursement_status": row.status,
        "policy": result["policy"],
        "tax_verification": result["tax_verification"],
        "invoice": {
            "invoice_code": invoice_code,
            "invoice_number": invoice_number,
            "invoice_unique_code": row.invoice_code,
            "check_code": parsed.get("check_code"),
            "amount": row.amount,
            "amount_without_tax": parsed.get("amount_without_tax"),
            "tax_amount": parsed.get("tax_amount"),
            "date": row.voucher_date,
            "raw_format": parsed.get("raw_format"),
        },
    }


@app.post("/invoices/sync-email", tags=["电子发票"], summary="邮箱同步电子发票")
def sync_invoices_from_email(
    payload: EmailSyncRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    邮箱同步接口：
    - 连接 IMAP 邮箱抓取邮件附件
    - 对附件执行 OCR + 查重 + 验真
    - 自动写入 Reimbursement
    """
    verify_access_token(db, username, access_token)

    cap_limit = max(1, min(payload.limit, 50))
    imported_items: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    error_items: List[Dict[str, Any]] = []
    mailbox = None

    try:
        mailbox = imaplib.IMAP4_SSL(payload.imap_host, payload.imap_port)
        mailbox.login(payload.email_username, payload.email_password)
        mailbox.select(payload.folder)
        criteria = "UNSEEN" if payload.only_unseen else "ALL"
        status, data = mailbox.search(None, criteria)
        if status != "OK":
            raise HTTPException(status_code=502, detail="邮箱检索失败")

        message_ids = data[0].split() if data and data[0] else []
        message_ids = message_ids[-cap_limit:]

        supported_suffixes = {
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".bmp",
            ".tif",
            ".tiff",
            ".txt",
        }

        for msg_id in message_ids:
            fetch_status, msg_data = mailbox.fetch(msg_id, "(RFC822)")
            if fetch_status != "OK" or not msg_data:
                error_items.append(
                    {"mail_id": msg_id.decode(errors="ignore"), "error": "邮件拉取失败"}
                )
                continue

            raw_message = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
            if not raw_message:
                continue
            msg_obj = email.message_from_bytes(raw_message)
            subject = _decode_email_header(msg_obj.get("Subject", ""))
            body_text = _extract_email_text(msg_obj)
            body_parsed = parse_qr_invoice_payload(body_text) if body_text else {}
            attachments = _extract_email_attachments(msg_obj)

            for attachment in attachments:
                filename = attachment["filename"]
                suffix = Path(filename).suffix.lower()
                if suffix not in supported_suffixes:
                    skipped_items.append(
                        {
                            "filename": filename,
                            "reason": "附件格式不支持",
                            "subject": subject,
                        }
                    )
                    continue

                try:
                    content = attachment["content"]
                    ocr_result = perform_ocr(content, filename)
                    ocr_status = (ocr_result.get("status") or "").strip().lower()
                    if ocr_status in {"failed", "empty"}:
                        ocr_error = (
                            ocr_result.get("error") or "OCR did not return usable text"
                        )
                        raise HTTPException(
                            status_code=422,
                            detail=f"OCR识别失败: {ocr_error}",
                        )
                    ocr_fields = ocr_result.get("fields", {}) or {}
                    amount_raw = ocr_fields.get("amount")
                    amount = (
                        float(amount_raw)
                        if isinstance(amount_raw, (int, float))
                        else 0.0
                    )
                    voucher_date = ocr_fields.get("date") or body_parsed.get(
                        "voucher_date"
                    )
                    invoice_code = (
                        normalize_invoice_code(ocr_fields.get("invoice_code"))
                        or body_parsed.get("invoice_unique_code")
                        or body_parsed.get("invoice_code")
                    )
                    reason = (
                        extract_reason_from_ocr(ocr_result)
                        or f"邮箱同步: {subject or '电子发票'}"
                    )
                    category = infer_category(reason, ocr_result)
                    is_e_invoice = (
                        is_electronic_invoice(ocr_result, filename) or suffix == ".pdf"
                    )

                    result = _create_reimbursement_from_invoice_data(
                        db,
                        username=username,
                        amount=amount,
                        category=category,
                        reason=reason,
                        invoice_code=invoice_code,
                        voucher_date=voucher_date,
                        image_path=(
                            f"email://{payload.email_username}/{msg_id.decode(errors='ignore')}/{filename}"
                        ),
                        source_device="email-sync",
                        is_e_invoice=is_e_invoice,
                    )
                    row = result["reimbursement"]
                    imported_items.append(
                        {
                            "mail_id": msg_id.decode(errors="ignore"),
                            "subject": subject,
                            "filename": filename,
                            "reimbursement_id": row.id,
                            "status": row.status,
                            "invoice_code": row.invoice_code,
                        }
                    )
                except HTTPException as http_exc:
                    skipped_items.append(
                        {
                            "mail_id": msg_id.decode(errors="ignore"),
                            "subject": subject,
                            "filename": filename,
                            "reason": str(http_exc.detail),
                        }
                    )
                except Exception as exc:
                    error_items.append(
                        {
                            "mail_id": msg_id.decode(errors="ignore"),
                            "subject": subject,
                            "filename": filename,
                            "error": str(exc),
                        }
                    )

        db.add(
            OperationLog(
                operator=username,
                action=(
                    f"邮箱同步完成：imported={len(imported_items)}, "
                    f"skipped={len(skipped_items)}, errors={len(error_items)}"
                ),
            )
        )
        db.commit()

        return {
            "status": "success",
            "imported_count": len(imported_items),
            "skipped_count": len(skipped_items),
            "error_count": len(error_items),
            "imported": imported_items,
            "skipped": skipped_items,
            "errors": error_items,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"邮箱同步失败: {exc}")
    finally:
        try:
            if mailbox is not None:
                mailbox.logout()
        except Exception:
            pass


@app.post("/notifications/push", tags=["通知中心"], summary="会计发布通知")
def push_notification(
    payload: NotificationPushRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    发布通知接口：
    - 仅会计可发布
    - target_role 支持 all/reporter/accountant
    """
    verify_access_token(db, username, access_token)
    ensure_accountant_permission(username)

    target_role = payload.target_role.strip().lower()
    if target_role not in {"all", "reporter", "accountant"}:
        raise HTTPException(
            status_code=422, detail="target_role 仅支持 all/reporter/accountant"
        )

    title = payload.title.strip()
    content = payload.content.strip()
    if not title or not content:
        raise HTTPException(status_code=422, detail="title/content 不能为空")

    row = Notification(
        title=title[:128],
        content=content[:1000],
        target_role=target_role,
        created_by=username,
        is_active=1,
    )
    db.add(row)
    db.add(
        OperationLog(
            operator=username,
            action=f"发布通知: target_role={target_role}, title={title[:40]}",
        )
    )
    db.commit()
    db.refresh(row)

    return {
        "status": "success",
        "notification": {
            "id": row.id,
            "title": row.title,
            "content": row.content,
            "target_role": row.target_role,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
            "is_active": row.is_active,
        },
    }


@app.get("/notifications", tags=["通知中心"], summary="拉取通知列表")
def list_notifications(
    username: str,
    access_token: str = Header(default=None),
    active_only: bool = True,
    query_all: bool = False,
    since_id: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """
    通知拉取接口：
    - 报账员默认拉取 all + reporter
    - 会计默认拉取 all + accountant
    - 会计可 query_all=true 拉取全部
    """
    verify_access_token(db, username, access_token)
    role = get_user_role(username)
    cap_limit = max(1, min(limit, 100))

    stmt = select(Notification)
    if active_only:
        stmt = stmt.where(Notification.is_active == 1)

    if not query_all:
        stmt = stmt.where(Notification.target_role.in_(["all", role]))
    else:
        ensure_accountant_permission(username)
    if since_id > 0:
        stmt = stmt.where(Notification.id > since_id)

    rows = (
        db.execute(stmt.order_by(Notification.created_at.desc()).limit(cap_limit))
        .scalars()
        .all()
    )
    return {
        "role": role,
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "content": r.content,
                "target_role": r.target_role,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat(),
                "is_active": r.is_active,
            }
            for r in rows
        ],
    }


@app.patch(
    "/notifications/{notification_id}", tags=["通知中心"], summary="会计更新通知状态"
)
def update_notification_status(
    notification_id: int,
    payload: NotificationDisableRequest,
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    通知状态更新接口：
    - 仅会计可调用
    - is_active: 1 启用 / 0 关闭
    """
    verify_access_token(db, username, access_token)
    ensure_accountant_permission(username)

    if payload.is_active not in {0, 1}:
        raise HTTPException(status_code=422, detail="is_active 仅支持 0 或 1")

    row = db.get(Notification, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="通知不存在")

    row.is_active = payload.is_active
    db.add(
        OperationLog(
            operator=username,
            action=f"更新通知 id={notification_id} 状态为 is_active={payload.is_active}",
        )
    )
    db.commit()
    db.refresh(row)
    return {
        "status": "success",
        "notification_id": row.id,
        "is_active": row.is_active,
    }


@app.get("/sync-status", tags=["链路保障"], summary="查看离线补传队列状态")
def get_sync_status(
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    队列状态接口：
    - 校验 token
    - 返回当前 pending 队列明细
    """
    verify_access_token(db, username, access_token)

    pending_items = (
        db.execute(
            select(PendingUpload)
            .where(PendingUpload.status == "pending")
            .order_by(PendingUpload.created_at.asc())
        )
        .scalars()
        .all()
    )

    return {
        "pending_count": len(pending_items),
        "queue": [
            {
                "id": i.id,
                "username": i.username,
                "filename": i.original_filename,
                "encrypted_path": i.encrypted_path,
                "status": i.status,
                "created_at": i.created_at.isoformat(),
            }
            for i in pending_items
        ],
    }


@app.get("/logs", tags=["审计系统"], summary="获取操作日志")
def get_logs(
    username: str,
    access_token: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    审计日志接口：
    - 校验 token
    - 按时间倒序返回日志
    """
    verify_access_token(db, username, access_token)
    logs = (
        db.execute(select(OperationLog).order_by(OperationLog.timestamp.desc()))
        .scalars()
        .all()
    )
    return logs


# ============================================================
# 7) 启动入口
#    - 打印服务地址与文档地址
#    - 启动 Uvicorn
# ============================================================

if __name__ == "__main__":
    import uvicorn

    def get_host_ip() -> str:
        """
        获取本机局域网 IP，用于启动提示。
        若获取失败，回退到 127.0.0.1。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    target_ip = get_host_ip()
    print("\n" + "★" * 55)
    print("农村财务机器人安全网关 - AdminPolicy Ready")
    print(f"对接基准 URL: http://{target_ip}:8000")
    print(f"API交互文档: http://{target_ip}:8000/docs")
    print("★" * 55 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)
