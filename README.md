# FinanceRobot（农村财务机器人安全网关）

基于 `FastAPI + SQLAlchemy + SQLite + Fernet` 的农村财务报账后端示例。  
当前版本已包含：OCR 识别、报账业务落库、查重验真、会计审核、通知中心、政策咨询原型、扫描仪自动上传脚本。

## 1. 已实现能力

- 登录鉴权与令牌校验（含角色区分：会计/报账员）
- 凭证上传 `upload-voucher`（支持 `X-Source-Device`）
- OCR 识别金额/日期/发票代码，白条提取事由与签字人
- 风控关卡：发票查重 + 电子发票验真
- 规则引擎：
  - 事由含“招待”且金额 `> 5000` -> `需村民代表大会决议`
  - 白条签字人数 `< 2` -> `附件不全：缺少签字`
- 报账单业务表 `Reimbursement` 持久化
- 审核后台接口：会计可修改报账状态（如待审核 -> 已打款）
- 汇总统计接口：按月/类别/状态统计
- 通知中心：会计推送通知、报账员拉取通知
- 政策咨询原型：本地政策文档检索 + Gemini/Qwen（可配置）
- 扫描仪客户端脚本：监控目录并自动上传
- 人脸识别模块：人脸注册、人脸核验、登录可选人脸二次校验

## 2. 目录结构

```text
FINANCEROBOT/
├─ main.py
├─ scanner_auto_upload.py
├─ policy_docs/
│  └─ village_finance_policy.md
├─ encrypted_storage/
│  ├─ pending/
│  └─ synced/
├─ rural_finance.db
└─ secret.key
```

## 3. 快速启动

```bash
pip install fastapi uvicorn sqlalchemy cryptography python-multipart pydantic httpx
python main.py
```

启动后访问：
- `http://127.0.0.1:8000/docs`

## 4. 默认测试账号

- 报账员：`reporter01 / 130102199003031234`
- 会计：`accountant01 / 130102199002021234`
- 管理账号：`admin01 / 130102199001011234`

## 5. 请求约定

- 受保护接口都需要请求头：`access-token: <token>`
- 上传接口支持来源标识头：`X-Source-Device: Fujitsu-fi-7140`

## 6. 核心接口

- `POST /login`
- `POST /logout`
- `POST /upload-voucher`
- `POST /sync-pending`
- `GET /sync-status`
- `GET /reimbursements`
- `PATCH /reimbursements/{reimbursement_id}/audit`
- `GET /reimbursements/summary`
- `POST /notifications/push`
- `GET /notifications`
- `PATCH /notifications/{notification_id}`
- `POST /policy/consult`
- `GET /logs`
- `POST /face/register`
- `POST /face/verify-image`
- `GET /face/profile`

## 7. 扫描仪自动上传（fi-7140）

```bash
python scanner_auto_upload.py --watch-dir "C:\Scanned" --base-url "http://127.0.0.1:8000"
```

说明：
- 默认会带 `X-Source-Device: Fujitsu-fi-7140`
- 检测到新扫描文件后自动上传
- 上传成功会弹窗显示识别结果和审核状态

## 8. 环境变量

基础：
- `DATABASE_URL`（默认 `sqlite:///./rural_finance.db`）
- `TOKEN_EXPIRE_MINUTES`（默认 `120`）

OCR：
- `OCR_PROVIDER`：`auto/paddle/baidu/plain_text/off`
- `OCR_REQUIRED`：`true/false`
- `BAIDU_OCR_API_KEY`
- `BAIDU_OCR_SECRET_KEY`

验真：
- `TAX_VERIFY_URL`
- `TAX_VERIFY_STRICT`

权限：
- `ACCOUNTANT_USERS`（逗号分隔）

政策咨询：
- `POLICY_AI_PROVIDER`：`off/gemini/qwen`
- `POLICY_AI_MODEL`
- `POLICY_DOCS_DIR`
- `POLICY_TOP_K`
- `GEMINI_API_KEY`
- `QWEN_OPENAI_BASE_URL`
- `QWEN_OPENAI_API_KEY`

人脸识别：
- `FACE_RECOGNITION_ENABLED`：`true/false`
- `FACE_LOGIN_REQUIRED`：`true/false`
- `FACE_PROVIDER`：`auto/local/api/mock`
- `FACE_MATCH_THRESHOLD`（默认 `0.82`）
- `FACE_ALLOW_MOCK_FALLBACK`：`true/false`（默认 `false`）
- `FACE_MAX_IMAGE_BYTES`
- `FACE_LOCAL_MODEL_ENABLED`
- `FACE_LOCAL_MODEL_MODULE`（默认 `face_local_model`）
- `FACE_LOCAL_MODEL_FUNCTION`（默认 `verify_face_pair`）
- `FACE_LOCAL_MODEL_CMD`（外部模型命令模板，支持 `{reference_image_path}` 和 `{probe_image_path}`）
- `FACE_LOCAL_MODEL_CMD_TIMEOUT`
- `FACE_LOCAL_MODEL_CMD_STRICT`
- `FACE_LOCAL_MODEL_AUTO_DISCOVER`
- `FACE_LOCAL_MODEL_PYTHON`
- `FACE_LOCAL_MODEL_USE_HASH_FALLBACK`（默认 `false`，建议保持关闭）
- `FACE_CLASSIC_THRESHOLD`（OpenCV 经典模型阈值，默认 `0.72`）
- `FACE_API_URL`
- `FACE_API_KEY`
- `FACE_API_VERIFY_TLS`

白条模型（真实模型建议）：
- `WHITE_SLIP_LOCAL_MODEL_CMD_STRICT`（默认 `false`，建议真实模型模式设为 `true`）
- `WHITE_SLIP_LOCAL_MODEL_RULE_ENABLED`（默认 `true`，建议真实模型模式设为 `false`）
- `WHITE_SLIP_REAL_MODEL_REQUIRED`（默认 `true`，OCR无文本则报错）

## 9. 政策文档库

默认目录为 `policy_docs/`。  
你可以放入 `.md/.txt` 政策文件，`/policy/consult` 会先检索文档再回答。

## 10. 说明

- 上传失败（查重/验真）会写入 `OperationLog`，便于报账员追溯原因。
- 当前为演示项目，生产环境建议补充：密码哈希、HTTPS、限流、审计归档、消息队列。

## 11. 人脸模块接入示例

使用本地模型（推荐）：

```powershell
$env:FACE_RECOGNITION_ENABLED="true"
$env:FACE_PROVIDER="local"
$env:FACE_LOCAL_MODEL_ENABLED="true"
$env:FACE_LOCAL_MODEL_MODULE="face_local_model"
$env:FACE_LOCAL_MODEL_FUNCTION="verify_face_pair"
$env:FACE_LOCAL_MODEL_CMD_STRICT="true"
$env:FACE_LOCAL_MODEL_USE_HASH_FALLBACK="false"

# 如果你有独立推理脚本：
$env:FACE_LOCAL_MODEL_CMD=".venv\\Scripts\\python.exe tools\\infer_face.py --reference {reference_image_path} --probe {probe_image_path}"
```

登录时增加可选字段 `face_image_base64` 即可触发二次校验；也可以先调 `/face/register` 和 `/face/verify-image` 做单独核验。

白条真实模型建议配置：

```powershell
$env:WHITE_SLIP_LOCAL_MODEL_ENABLED="true"
$env:WHITE_SLIP_LOCAL_MODEL_MODULE="white_slip_local_model"
$env:WHITE_SLIP_LOCAL_MODEL_FUNCTION="predict_white_slip"
$env:WHITE_SLIP_LOCAL_MODEL_CMD=".venv\\Scripts\\python.exe tools\\infer_white_slip.py --image {image_path}"
$env:WHITE_SLIP_LOCAL_MODEL_CMD_STRICT="true"
$env:WHITE_SLIP_LOCAL_MODEL_RULE_ENABLED="false"
$env:WHITE_SLIP_REAL_MODEL_REQUIRED="true"
```
