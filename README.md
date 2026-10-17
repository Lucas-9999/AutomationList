# AutomationList · 外贸工作台

面向业务员的本地外贸辅助工具，提供询价技术翻译，以及成交后从 ERP 文件生成外贸单据草稿的流程。后端使用 Python / FastAPI，页面使用原生 JavaScript，业务记录存于本机 SQLite。

## 业务流程

1. **询价翻译**：导入 EML、TXT、DOCX 或 XLSX，保留客户原文；通过可替换的 Agent API 分块翻译、整理技术要求，并保留原文引用。图片不做 OCR，缺失信息需要人工澄清。
2. **成交后制单**：业务员在系统外完成报价、客户确认和 ERP 开单，再上传 ERP Excel。每次手动填写汇率，预览后选择生成 CI、PI、PL、报关单或发货签收单草稿。

汇率填写为 `1 USD = X CNY`，人民币单价除以汇率，美元单价保留 4 位，金额保留 2 位。总额汇总各行金额。报关支持按已确认的 HS、品名、材质、等级和杆径范围分类，每类填写一次；表色差异不单独拆组。分类汇总金额保留原明细金额之和，不用四位显示单价反算覆盖。其他单据保留原始产品明细。

系统不代替报价、客户下单确认或 ERP 开单，也不发送客户邮件、签约或提交报关。

## 公开版范围

本仓库不含真实客户文件、业务数据库、原始模板、历史报告或业务截图。测试示例使用 `SYNTHETIC` 合成数据。不要把本机数据和密钥提交到仓库。

**空数据库可以启动和查看界面，但缺少已适配模板时会阻断导出。** 模板引擎仅支持已检查布局并绑定 SHA-256 的模板。上传任意 Word / Excel 不代表可以直接生成；新模板需要字段、布局、签章安全审查和专用适配，不能通过取消白名单绕过检查。参见[模板适配说明](docs/TEMPLATES.md)。

当前为本地审阅原型：原生 Office 打印版式尚未正式验收；默认仅监听回环地址，未提供多用户认证，不能直接暴露到公网。

## Windows 启动

当前本地验证环境为 Python 3.14.6、Node.js 24。其他 Python 版本尚未验证。CI 配置 Node.js 22 执行前端测试，具体结果以运行记录为准；启动网站不依赖 Node.js。

在仓库根目录打开 PowerShell：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints-tested.txt
Copy-Item .env.example .env
```

编辑本机 `.env` 后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

也可双击 `启动后端服务.cmd`。打开[本机工作台](http://127.0.0.1:8765/)或[接口文档](http://127.0.0.1:8765/docs)。保留启动窗口，按 `Ctrl+C` 停止服务。`run.ps1` 优先使用 `.venv`，读取 `.env`，兼容旧名称 `.evn`；已有进程环境变量优先。修改配置后需重启后端。

## 其他平台启动

下面是跨平台的手动启动方式；各平台的原生 Office 打印尚未验收。

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt -c constraints-tested.txt
cp .env.example .env
python -m uvicorn tradeflow.api:create_app --factory --host 127.0.0.1 --port 8765 --env-file .env
```

`constraints-tested.txt` 记录已测试的直接依赖版本，不是完整的传递依赖锁文件。

## 本机配置

从 `.env.example` 复制配置，不要在网页或 GitHub 中粘贴密钥。

| 配置 | 用途 |
| --- | --- |
| `TRADEFLOW_AGENT_BASE_URL` / `TRADEFLOW_AGENT_API_KEY` / `TRADEFLOW_AGENT_MODEL` | 配置翻译与语义分析服务；未配置时不能调用翻译 |
| `TRADEFLOW_AGENT_RESPONSE_FORMAT` | 默认 `json_schema`；服务不支持时可显式设为 `json_object`，返回结果仍需本地校验 |
| `TRADEFLOW_HS_PROVIDER` / `TRADEFLOW_HSCIQ_PROTOCOL` / `TRADEFLOW_HSCIQ_API_KEY` | 独立配置 HS 目录服务；协议按服务商实际权限选择 `mcp` 或 `tax_rest` |
| `TRADEFLOW_DATA_DIR` | 本机数据目录，默认 `data`；包含原文件、业务记录和生成结果，需自行备份 |
| `TRADEFLOW_SELLER_NAME` | 本机业务卖方名称 |
| `TRADEFLOW_TEMPLATE_SELLER_NAMES` | 本机已审核模板的卖方名称 JSON 数组，须与模板一致；不能代替版式和哈希适配 |

卖方配置示例仅为合成数据：

```dotenv
TRADEFLOW_SELLER_NAME=SYNTHETIC SELLER
TRADEFLOW_TEMPLATE_SELLER_NAMES='["SYNTHETIC SELLER"]'
```

公开版没有默认模板卖方名单；未显式配置时阻断相关模板检查与制单。示例中的 `SYNTHETIC` 不代表真实卖方。名单仅用于核对，不会替换模板中的固定抬头或银行信息。启用外部 API 后，请求所需的询价内容或 HS 查询参数会发送给所配置的服务商；本机保存不等于这些请求离线执行。

## 测试

安装上述开发依赖，并使用 Node.js 22：

```sh
python -m pytest tests --basetemp tmp/ci -q
node --test tests/test_document_jobs_frontend.cjs
```

CI 在 Ubuntu 配置 Python 3.14 与 Node.js 22 执行上述检查。具体结果以对应运行记录为准；自动化测试不等于原生 Office 打印验收。架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 许可证

尚未指定许可证。
