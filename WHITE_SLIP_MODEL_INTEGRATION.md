# 白条本地模型接入说明

当前项目支持 3 种白条本地模型接入方式：

1. Python函数模式（默认）：`white_slip_local_model.predict_white_slip`
2. 外部命令模式：设置 `WHITE_SLIP_LOCAL_MODEL_CMD`
3. 自动发现模式（无需命令）：自动扫描 `tools/`、`models/` 下白条推理脚本

## 必要环境变量

```powershell
$env:WHITE_SLIP_LOCAL_MODEL_ENABLED="true"
```

## 外部命令模式（推荐）

使用带 `{image_path}` 占位符的命令模板：

```powershell
$env:WHITE_SLIP_LOCAL_MODEL_CMD=".venv\Scripts\python.exe tools\infer_white_slip.py --image {image_path}"
$env:WHITE_SLIP_LOCAL_MODEL_CMD_TIMEOUT="30"
$env:WHITE_SLIP_LOCAL_MODEL_CMD_STRICT="true"
```

约定：
- 输入：服务端会把上传图片写入临时文件，并将路径注入 `{image_path}`。
- 输出：命令必须在标准输出中打印一个 JSON 对象。
- JSON 键：
  - `reason`（字符串或 null）
  - `signers`（字符串数组）
  - `payer`（字符串或 null）
  - `payee`（字符串或 null）
  - `amount`（数字或 null）
  - `date`（`YYYY-MM-DD` 或 null）
  - `slip_type`（`white_slip|loan_note|receipt_note|other`）

## 接口测试

启动服务后测试：

```powershell
curl -X POST "http://127.0.0.1:8000/white-slips/local-model-parse-image?username=admin01" `
  -H "access-token: <token>" `
  -F "file=@D:\ScanInput\white_slip.jpg;type=image/jpeg"
```

## OCR超时兜底说明

当 OCR 超时时，上传流程会自动尝试白条图片模型兜底。
若兜底提取到有效字段，上传仍会成功，响应中可见：
- `ocr.status = ok`
- `ocr.provider = white_slip_fallback`

## 自动发现模式

如果未设置 `WHITE_SLIP_LOCAL_MODEL_CMD`，系统会自动尝试：
- `tools/`、`models/` 下文件名包含 `white/slip` 且包含 `infer/predict/parse` 的脚本
- 常见调用参数：
  - `python script.py {image_path}`
  - `python script.py --image {image_path}`
  - `python script.py --img {image_path}`
  - `python script.py --input {image_path}`
  - `python script.py --file {image_path}`

可选环境变量：

```powershell
$env:WHITE_SLIP_LOCAL_MODEL_AUTO_DISCOVER="true"
$env:WHITE_SLIP_LOCAL_MODEL_PYTHON=".venv\Scripts\python.exe"
```
