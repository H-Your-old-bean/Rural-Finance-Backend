import os
import socket
import secrets
from datetime import datetime, timedelta
from typing import Optional

# FastAPI：用于构建 REST API、声明请求参数、依赖注入和抛出 HTTP 异常
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Header
from fastapi.middleware.cors import CORSMiddleware

# Pydantic：用于请求体结构定义与输入校验
from pydantic import BaseModel, Field

# SQLAlchemy：用于数据库连接、ORM 模型定义与查询
from sqlalchemy import String, DateTime, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session

# Fernet：对称加密工具（适合文件内容加密存储）
from cryptography.fernet import Fernet


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
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {},
)

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
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


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
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
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
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)  # pending / synced / failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


# 启动时自动建表（若表已存在则跳过）
Base.metadata.create_all(bind=engine)

# 准备加密文件目录：
# - pending: 离线待补传
# - synced: 在线上传成功（或补传成功）后存档
os.makedirs("encrypted_storage", exist_ok=True)
os.makedirs("encrypted_storage/pending", exist_ok=True)
os.makedirs("encrypted_storage/synced", exist_ok=True)


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


def verify_access_token(db: Session, username: str, access_token: Optional[str]) -> AccessToken:
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


def write_encrypted_file(target_dir: str, original_filename: Optional[str], encrypted_content: bytes) -> str:
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


# ============================================================
# 4) FastAPI 应用初始化与中间件
# ============================================================

app = FastAPI(title="农村财务机器人安全网关", version="3.1.1-Bugfix")

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
    valid_users = {"admin01": "130102199001011234"}

    if data.username not in valid_users or valid_users[data.username] != data.id_card:
        db.add(OperationLog(operator=data.username, action="尝试登录失败：身份不符"))
        db.commit()
        raise HTTPException(status_code=401, detail="身份验证失败")

    token = create_access_token(db, data.username)
    db.add(OperationLog(operator=data.username, action="登录成功：发放访问令牌"))
    db.commit()
    return {
        "status": "success",
        "access_token": token,
        "expires_in_minutes": TOKEN_EXPIRE_MINUTES,
    }


@app.post("/logout", tags=["权限管控"], summary="撤销当前令牌")
def logout(username: str, access_token: str = Header(default=None), db: Session = Depends(get_db)):
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


@app.post("/upload-voucher", tags=["链路保障"], summary="加密上传与断网处理")
async def upload_voucher(
    username: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    access_token: str = Header(default=None),
):
    """
    上传凭证接口：
    - 校验 token
    - 读取并加密文件内容
    - 网络可用：写入 synced 目录
    - 网络不可用：写入 pending 目录 + 入库离线队列
    - 记录审计日志
    """
    verify_access_token(db, username, access_token)

    try:
        content = await file.read()
        encrypted_content = cipher.encrypt(content)

        # 网络不可用：进入离线补传流程
        if not check_real_network():
            pending_path = write_encrypted_file("encrypted_storage/pending", file.filename, encrypted_content)
            task = PendingUpload(
                username=username,
                original_filename=sanitize_filename(file.filename),
                encrypted_path=pending_path,
                status="pending",
                created_at=datetime.now(),
            )
            db.add(task)
            db.add(OperationLog(operator=username, action=f"网络断开：凭证 {sanitize_filename(file.filename)} 已持久化到离线队列"))
            db.commit()
            db.refresh(task)
            return {
                "status": "cached",
                "message": "已自动转入断网补传模式",
                "pending_id": task.id,
            }

        # 网络可用：直接入在线存储目录
        synced_path = write_encrypted_file("encrypted_storage/synced", file.filename, encrypted_content)
        db.add(OperationLog(operator=username, action=f"凭证上报成功：加密存储 {synced_path}"))
        db.commit()
        return {"status": "success", "detail": "数据已上报云端"}

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

    pending_items = db.execute(
        select(PendingUpload).where(PendingUpload.status == "pending").order_by(PendingUpload.created_at.asc())
    ).scalars().all()

    synced_count = 0
    failed_count = 0

    for item in pending_items:
        try:
            # 这里为演示逻辑：仅更新状态，实际项目可在此调用远端上传 API
            item.status = "synced"
            item.synced_at = datetime.now()
            item.error_message = None
            db.add(OperationLog(operator=username, action=f"离线补传成功：{item.original_filename} (id={item.id})"))
            synced_count += 1
        except Exception as e:
            item.status = "failed"
            item.error_message = str(e)[:250]
            failed_count += 1

    db.commit()

    pending_count = db.execute(
        select(PendingUpload).where(PendingUpload.status == "pending")
    ).scalars().all()

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
        r for r in mock_records
        if (not record_type or r["type"] == record_type) and r["amount"] >= min_amount
    ]

    db.add(OperationLog(operator=username, action=f"执行记录筛选: {record_type or 'ALL'}"))
    db.commit()
    return filtered


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

    pending_items = db.execute(
        select(PendingUpload).where(PendingUpload.status == "pending").order_by(PendingUpload.created_at.asc())
    ).scalars().all()

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


@app.get("/logs", tags=["审计系统"])
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
    logs = db.execute(select(OperationLog).order_by(OperationLog.timestamp.desc())).scalars().all()
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
    print("农村财务机器人安全网关 - Bugfix Ready")
    print(f"对接基准 URL: http://{target_ip}:8000")
    print(f"API交互文档: http://{target_ip}:8000/docs")
    print("★" * 55 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)
