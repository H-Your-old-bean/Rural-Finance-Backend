# 农村财务机器人安全网关（FinanceRobot）

一个基于 **FastAPI + SQLAlchemy + SQLite + Fernet** 的后端网关示例项目，提供登录鉴权、凭证加密上传、断网补传队列、审计日志与基础业务查询能力。

---

## 1. 项目功能

- 双重身份登录（账号 + 身份证）
- 令牌签发、校验、过期与退出撤销
- 凭证文件加密存储（Fernet）
- 断网时离线队列持久化（数据库 + 本地加密文件）
- 手动补传离线队列
- 审计日志记录与查询
- 余额查询与记录筛选（演示数据）

---

## 2. 目录结构

```text
FINANCEROBOT/
├─ main.py                 # 主服务入口（推荐使用）
├─ 实战.py                  # 与 main.py 同步版本
├─ smoke_test.ps1          # PowerShell 冒烟测试脚本
├─ rural_finance.db        # SQLite 数据库文件（运行后自动创建/更新）
├─ secret.key              # Fernet 密钥文件（首次运行自动生成）
└─ encrypted_storage/
   ├─ pending/             # 断网待补传加密文件
   └─ synced/              # 在线上传后的加密文件
```

---

## 3. 环境要求

- Python 3.10+
- Windows PowerShell（用于运行 `smoke_test.ps1`）
- 推荐安装依赖：
  - `fastapi`
  - `uvicorn`
  - `sqlalchemy`
  - `cryptography`
  - `python-multipart`
  - `pydantic`

---

## 4. 安装依赖（示例）

```bash
pip install fastapi uvicorn sqlalchemy cryptography python-multipart pydantic
```

如果你使用虚拟环境，请先激活后再安装依赖。

---

## 5. 启动服务

在 `FINANCEROBOT` 目录执行：

```bash
python main.py
```

启动后可访问：

- 服务地址：`http://127.0.0.1:8000`
- API 文档：`http://127.0.0.1:8000/docs`

> 说明：`main.py` 与 `实战.py` 当前为同步版本，二者任选其一启动即可。

---

## 6. 鉴权与请求约定

### 登录
- `POST /login`
- Body:
```json
{
  "username": "admin01",
  "id_card": "130102199001011234"
}
```

登录成功后返回 `access_token`。  
后续受保护接口需在请求头携带：

```http
access_token: <你的token>
```

并传入 `username` 参数（query/form）。

---

## 7. 冒烟测试（推荐）

项目提供一键脚本：`smoke_test.ps1`，覆盖完整流程：

1. 登录
2. 上传凭证
3. 查询余额
4. 记录筛选
5. 查询补传队列
6. 手动补传
7. 查询日志
8. 退出登录
9. 验证 token 失效

### 执行方式

在 `FINANCEROBOT` 目录下：

```powershell
.\smoke_test.ps1
```

可选参数：

```powershell
.\smoke_test.ps1 -BaseUrl "http://127.0.0.1:8000" -Username "admin01" -IdCard "130102199001011234"
```

---

## 8. 常用接口清单

- `POST /login`：登录并签发 token
- `POST /logout`：退出并撤销 token
- `POST /upload-voucher`：上传并加密凭证（断网时写入离线队列）
- `POST /sync-pending`：手动补传离线队列
- `GET /sync-status`：查看离线待补传队列
- `GET /account/balance`：查询账户余额（演示数据）
- `GET /records/filter`：筛选报账记录（演示数据）
- `GET /logs`：查询审计日志

---

## 9. 配置项

- `DATABASE_URL`：数据库连接字符串（默认 `sqlite:///./rural_finance.db`）
- `TOKEN_EXPIRE_MINUTES`：令牌有效期（分钟，默认 `120`）

示例：

```bash
set DATABASE_URL=sqlite:///./rural_finance.db
set TOKEN_EXPIRE_MINUTES=180
python main.py
```

---

## 10. 注意事项

- `secret.key` 非常敏感，请勿泄露或提交到公共仓库。
- 生产环境建议：
  - 使用更严格的用户体系（数据库用户 + 密码哈希）
  - 使用 HTTPS
  - 限制 CORS 来源
  - 配置反向代理与日志归档
  - 增加接口限流与告警

---

## 11. 快速排错

- 启动失败：检查依赖是否完整安装。
- 上传失败：确认请求为 `multipart/form-data`，字段名为 `file` 与 `username`，并携带 `access_token` 头。
- 补传失败：`/sync-pending` 会先做网络探测，网络不可用时返回 503。
- token 无效：请重新调用 `/login` 获取新 token。

---

## 12. 许可

当前仓库未显式声明 License。若你用于团队或发布，建议补充 `LICENSE` 文件。