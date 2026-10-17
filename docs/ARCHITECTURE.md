# 架构与边界

## 数据流

```text
询价文件 → 原文解析 → 分块计划 → Agent API → 引用校验 → 技术中文与待澄清项

ERP Excel → 原始行与单元格定位 → 手动汇率换算 → 补充字段与预览
                                              ↓
                      已适配模板 ← 人工核对确认 → 单据草稿与审计记录
                                              ↓
                               报关分类汇总与每类申报信息
```

报价、客户确认、ERP 开单在工作台之外完成。报关分类只改变报关单的商品行，CI、PI、PL 和签收单继续使用原始明细。

## 模块

| 模块 | 职责 |
| --- | --- |
| `tradeflow/api.py`、`tradeflow/static/` | 本机 HTTP 接口与业务页面 |
| `ingestion.py`、`inquiry_document.py`、`inquiry_workbook.py` | 文件解析、原文及来源定位 |
| `agent.py`、`inquiry_chunks.py`、`inquiry_analysis.py` | 可替换的分析服务、分块请求与结果校验 |
| `fx.py`、`calculations.py`、`document_data.py` | Decimal 换算、金额和制单数据准备 |
| `document_jobs.py`、`customs_review.py` | 制单预览、核对、确认与生成协调 |
| `customs_groups.py`、`customs_data.py` | 分类规则、原始成员关系与报关汇总 |
| `hs_catalog.py`、`hs_catalog_mcp.py`、`hs_catalog_bindings.py` | HS 服务适配、查询快照及版本关联 |
| `hs_formats.py` | 按 HS 保存可编辑申报格式 |
| `templates.py`、`drafts.py`、`customs.py`、`receipts.py` | 模板检查与对应格式的草稿生成 |
| `store.py`、`revisions.py`、`backup.py` | 本机原文件、记录、修订审计与备份 |

## 一致性规则

- 原文件按 SHA-256 保存，保留原文和 ERP 单元格位置；业务字段修改需要留痕。
- 数量和金额使用 Decimal。每票手动确认汇率；美元单价四位、金额两位，按 `ROUND_HALF_UP` 舍入，总额汇总行金额。
- 同一报关类别保留全部源行成员，汇总数量及金额。显示单价为本类金额除以数量的四位结果，汇总金额不从显示单价反算。
- 已保存的分类按版本引用。分类、成员或业务事实改变后，需要重新预览和确认。
- HS 服务返回目录信息和申报要素，不能替代业务员确认实际归类、材质、用途和申报事实。
- 关键创建操作使用内容或业务键去重；修订和失败记录可追溯。具体接口约束以代码及测试为准。

## 存储与运行

`TRADEFLOW_DATA_DIR` 下保存 SQLite 数据库及原始文件等运行数据。密钥只通过环境配置提供，不写入业务记录。`.env`、运行数据、客户文件、导出文件、备份和日志均不属于公开仓库内容。

空库可启动；公开仓库不携带真实模板和客户资料，缺少已适配模板时导出会被阻断。模板适配要求见 [TEMPLATES.md](TEMPLATES.md)。

默认监听 `127.0.0.1:8765`。当前没有多用户身份认证和面向公网的运行保障，仅用于可信本机环境。原生 Office 打印版式尚未正式验收，生成结果需人工审阅。仓库中的 CI 仅验证自动化测试，不执行生产部署。
