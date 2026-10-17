"""Local API with source-linked records and unsigned template review drafts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import AgentClient, validate_inquiry_source
from .analysis_errors import safe_analysis_failure
from .domain import check_document_readiness, normalize_order, transition
from .document_data import prepare_document, decimal_items
from .drafts import generate_review_draft
from .ingestion import parse_email, parse_erp_xlsx
from .profiles import candidate_profile
from .receipts import inspect_receipt_template, generate_receipt_draft
from .layout_review import inspect_text_fit
from .customs import inspect_customs_template, generate_customs_draft
from .customs_data import prepare_customs_document, customs_engine_inputs
from .store import ConflictError, Store

MAX_UPLOAD = 15 * 1024 * 1024
STATIC_ROOT = Path(__file__).parent / "static"
TEMPLATE_TYPES = {"contract", "ci", "pi", "pl", "customs", "receipt"}


class WorkflowCreate(BaseModel):
    inquiry_id: str


class ChunkAnalyze(BaseModel):
    plan_id: str = Field(min_length=1, max_length=128)


class Advance(BaseModel):
    target: str
    expected_version: int = Field(ge=1)
    evidence: dict


class Readiness(BaseModel):
    order_id: str
    shipment_id: str | None = None
    allocation_id: str | None = None


class AllocationCreate(BaseModel):
    shipment_id: str
    lines: list[dict]
    confirmation_ref: str


class DraftCreate(BaseModel):
    template_id: str
    order_id: str
    allocation_id: str | None = None
    fields: dict = Field(default_factory=dict)
    item_extras: dict = Field(default_factory=dict)


class RevisionCreate(BaseModel):
    payload: dict
    reason: str


class DocumentJobRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=160)
    exchange_rate: str = Field(min_length=1, max_length=64)
    document_types: list[str] = Field(min_length=1, max_length=5)
    fields: dict[str, str] = Field(default_factory=dict)
    item_extras: dict[str, dict[str, str]] = Field(default_factory=dict)
    document_fields: dict[str, dict[str, str]] = Field(default_factory=dict)
    preview_key: str | None = Field(default=None, max_length=64)
    customs_confirmations: dict[str, str] = Field(default_factory=dict)
    hs_catalog_bindings: dict[str, str] = Field(default_factory=dict)
    customs_groups: list[dict] | None = Field(default=None, max_length=1000)


class CustomsCategoryRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=120)
    hs_code: str = Field(min_length=1, max_length=32)
    product_name: str = Field(min_length=1, max_length=200)
    material: str = Field(min_length=1, max_length=200)
    grade: str = Field(min_length=1, max_length=120)
    diameter_min: str | None = None
    diameter_max: str | None = None
    min_inclusive: bool = True
    max_inclusive: bool = False
    description: str = Field(default="", max_length=4000)
    declaration_elements_text: str = Field(default="", max_length=4000)
    origin_country: str = Field(default="", max_length=200)
    destination_country: str = Field(default="", max_length=200)
    domestic_origin: str = Field(default="", max_length=200)
    levy_method: str = Field(default="", max_length=200)
    legal_unit: str = Field(default="KG", max_length=100)
    catalog_snapshot_id: str | None = Field(default=None, max_length=160)
    change_reason: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(default=0, ge=0, strict=True)


class HSFormatRequest(BaseModel):
    hs_code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    description_template: str = Field(min_length=1, max_length=4000)
    declaration_elements_template: str = Field(default="", max_length=4000)
    change_reason: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(default=0, ge=0, strict=True)


def create_app(data_root: Path | None = None) -> FastAPI:
    store = Store(data_root or Path(os.environ.get("TRADEFLOW_DATA_DIR", "data")))
    app = FastAPI(title="外贸自动化工作台 · 本地原型", version="0.7.0", description="询价翻译与ERP外贸制单。报关红字和核心字段每票确认，支持共享HS目录查询、申报要素参考及用户申报格式。人民币价格按手动汇率换算美元，单价四位、金额两位。")
    app.state.store = store

    def require_current_order(identity: str):
        from .revisions import order_revision_status
        status = order_revision_status(store, identity)
        if not status["is_current"]:
            raise ConflictError("所选订单已有修订版本，请选择当前版本：" + status["latest_order_id"])
        return status

    def with_revision_status(record: dict):
        from .revisions import order_revision_status
        if record["kind"] == "order":
            return {**record, "revision_status": order_revision_status(store, record["id"])}
        if record["kind"] == "draft":
            status = order_revision_status(store, record["payload"]["request"]["order_id"])
            return {**record, "source_order_current": status["is_current"], "effective_status": "review_draft" if status["is_current"] else "superseded"}
        return record

    @app.get("/", include_in_schema=False)
    def workspace():
        # Each UI revision receives a new URL, including in browsers retaining an
        # older unversioned script with heuristic cache freshness.
        version = hashlib.sha256((STATIC_ROOT / "document_jobs.js").read_bytes()).hexdigest()[:16]
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        html = html.replace('src="/static/document_jobs.js"', 'src="/static/document_jobs.js?v=' + version + '"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/project-status")
    def project_status():
        configured = all(bool(os.environ.get(key, "").strip()) for key in ("TRADEFLOW_AGENT_BASE_URL", "TRADEFLOW_AGENT_API_KEY", "TRADEFLOW_AGENT_MODEL"))
        return {
            "stage": "本地原型与真实样本适配",
            "scope": "询价翻译与ERP外贸制单；报价和确认下单由业务员在系统外完成",
            "document_generation": False,
            "review_draft_generation": True,
            "agent_configured": configured,
            "down_payment_percent": "30",
            "modules": [
                {"name": "邮件、Word/Excel 询价与 ERP 解析", "status": "支持DOCX/XLSX询价；由使用者本机导入资料，保留原文、定位及哈希"},
                {"name": "Agent 翻译", "status": "已读取本机配置；真实询价输出仍需逐项校验" if configured else "接口与模拟测试通过，真实服务待配置"},
                {"name": "外贸制单", "status": "上传ERP、每次手动汇率、美元单价4位/金额2位，默认模板导出CI/PI/PL/报关/签收；报关支持完整续明细"},
                {"name": "历史管理", "status": "旧订单/分批/回放保留在管理中；日常制单无需重复建单和分配；原生打印与业务复核待完成"},
                {"name": "修订与备份", "status": "订单修订保留历史版本；本地备份校验与新目录恢复可用"},
            ],
            "missing_inputs": ["真实询价翻译业务验收" if configured else "Agent 服务配置", "历史回放后的批次包装归属及原生打印验收"],
        }

    @app.get("/history-replay")
    def history_replay():
        path = store.root / "history_replay.json"
        if not path.exists():
            return {"available": False, "note": "尚未生成本机历史回放报告；不会创建业务记录。"}
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("历史回放报告路径或大小无效")
        content = path.read_bytes()
        try:
            report = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("历史回放报告不是有效 JSON") from None
        if not isinstance(report, dict) or report.get("format") != "tradeflow-history-replay-v1":
            raise ValueError("历史回放报告版本不受支持")
        return {"available": True, "report_sha256": hashlib.sha256(content).hexdigest(), "report": report}

    @app.get("/records")
    def list_records(kind: str):
        if kind not in {"inquiry", "shipment", "order", "analysis", "analysis_chunk", "allocation", "draft"}:
            raise ValueError("不支持的记录类型")
        return [with_revision_status(record) for record in store.list_records(kind)]

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc):
        if request.url.path.startswith("/workflows/") and request.url.path.endswith("/advance"):
            identity = request.url.path.split("/")[2]
            try:
                store.workflow(identity)
                store.audit(identity, "transition_rejected", {"reason": "validation_or_version_conflict"})
            except KeyError:
                pass
        return JSONResponse(status_code=409 if isinstance(exc, ConflictError) else 422, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse(status_code=404, content={"detail": "记录不存在"})

    async def read_upload(file: UploadFile) -> tuple[bytes, str]:
        content = await file.read(MAX_UPLOAD + 1)
        await file.close()
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "文件超过 15 MiB 上限")
        if not content:
            raise ValueError("空文件")
        return content, (file.filename or "unnamed").replace("\\", "/").split("/")[-1]

    @app.get("/health")
    def health():
        return {"status": "ok", "stage": "local_review_prototype", "real_samples_accepted": False, "document_generation": False, "review_draft_generation": True}

    @app.get("/document-jobs/sources")
    def document_sources():
        from .document_jobs import list_sources
        return list_sources(store)

    @app.get("/customs/hs-formats")
    def list_hs_formats():
        from .hs_formats import list_formats
        return list_formats(store)

    @app.get("/customs/categories")
    def list_customs_categories():
        from .customs_groups import list_categories
        return list_categories(store)

    @app.post("/customs/categories")
    def save_customs_category(body: CustomsCategoryRequest):
        from .customs_groups import save_category
        try:
            return save_category(store, body.model_dump())
        except ValueError:
            store.audit("customs_categories", "customs_category_save_failed", {"reason": "invalid_or_version_conflict"})
            raise

    def catalog_call(operation, *args, **kwargs):
        from .hs_catalog import CatalogError
        try:
            return operation(*args, **kwargs)
        except CatalogError as exc:
            store.audit("hs_catalog", "hs_catalog_request_failed", {"code": exc.code})
            status = {"not_configured": 503, "rate_limited": 429, "invalid_input": 422, "unsupported_operation": 400}.get(exc.code, 502)
            return JSONResponse(status_code=status, content={"detail": exc.message, "code": exc.code})

    @app.get("/customs/hs-catalog/status")
    def hs_catalog_status():
        from .hs_catalog import status
        return status()

    @app.get("/customs/hs-catalog/search")
    def hs_catalog_search(keywords: str, page: int = 1):
        from .hs_catalog import search
        return catalog_call(search, store, keywords, page=page)

    @app.get("/customs/hs-catalog/codes/{code}")
    def hs_catalog_code(code: str, refresh: bool = False):
        from .hs_catalog import lookup
        return catalog_call(lookup, store, code, refresh=refresh)

    @app.get("/customs/hs-catalog/updates")
    def hs_catalog_updates(range: str = "30d"):
        from .hs_catalog import updates
        return catalog_call(updates, store, range)

    @app.post("/customs/hs-formats")
    def save_hs_format(body: HSFormatRequest):
        from .hs_formats import save_format
        try:
            return save_format(store, body.model_dump())
        except ValueError:
            store.audit("hs_formats", "hs_format_save_failed", {"reason": "invalid_or_version_conflict"})
            raise

    @app.post("/document-jobs/import")
    async def import_document_source(file: UploadFile = File(...)):
        from .document_jobs import source_projection
        from .fx import project_erp_source
        content, filename = await read_upload(file)
        if Path(filename).suffix.lower() != ".xlsx":
            raise ValueError("ERP销售合同来源请上传 XLSX Excel文件")
        parsed = parse_erp_xlsx(content, filename)
        digest = hashlib.sha256(content).hexdigest()
        parsed["sha256"] = digest
        project_erp_source({"id": "validation", "payload": parsed})
        store.save_original(content)
        key = digest + ":" + parsed["source"]["sheet"]
        previous = store.find("erp_source", key)
        record = previous or store.create_record("erp_source", key, parsed)
        return source_projection(store, record["id"])

    @app.post("/document-jobs/preview")
    def preview_document_job(body: DocumentJobRequest):
        from .document_jobs import prepare_job
        return prepare_job(store, body.model_dump())[0]

    @app.post("/document-jobs")
    def generate_document_job(body: DocumentJobRequest):
        from .document_jobs import create_job, public_job
        try:
            return public_job(create_job(store, body.model_dump()))
        except ValueError:
            store.audit(body.source_id, "document_job_failed", {"reason": "missing_invalid_or_stale_preview"})
            raise

    @app.get("/document-jobs")
    def list_document_jobs():
        from .document_jobs import public_job
        return [public_job(record) for record in store.list_records("document_job", limit=1000)]

    @app.get("/document-jobs/{identity}/files/{file_id}/download")
    def download_job_file(identity: str, file_id: str):
        from .document_jobs import _original
        job = store.get(identity, "document_job")
        item = next((item for item in job["payload"]["files"] if item["id"] == file_id), None)
        if item is None:
            raise KeyError("文件不存在")
        _original(store, {"payload": {"sha256": item["sha256"]}})
        return FileResponse(store.root / "raw" / item["sha256"], media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=item["filename"])

    @app.get("/document-jobs/{identity}/download")
    def download_job_zip(identity: str):
        from .document_jobs import _original
        payload = store.get(identity, "document_job")["payload"]
        _original(store, {"payload": {"sha256": payload["zip_sha256"]}})
        return FileResponse(store.root / "raw" / payload["zip_sha256"], media_type="application/zip", filename=payload["zip_filename"])

    @app.get("/static/document_jobs.js", include_in_schema=False)
    def document_jobs_script():
        return FileResponse(STATIC_ROOT / "document_jobs.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.post("/inquiries/upload")
    async def upload_inquiry(file: UploadFile = File(...)):
        content, filename = await read_upload(file)
        if filename.lower().endswith(".xlsx"):
            from .inquiry_workbook import parse_inquiry_workbook
            parsed = parse_inquiry_workbook(content, filename)
        elif filename.lower().endswith(".docx"):
            from .inquiry_document import parse_inquiry_document
            parsed = parse_inquiry_document(content, filename)
        else:
            if not filename.lower().endswith((".eml", ".txt")):
                raise ValueError("询价文件支持 .docx、.xlsx、.eml、.txt；旧版 .doc/.xls 请先另存为 .docx/.xlsx")
            parsed = parse_email(content, filename)
        digest = store.save_original(content)
        # Identical bytes have identical semantics, regardless of client filename.
        parsed["sha256"] = digest
        existing = store.find("inquiry", digest)
        if existing:
            return existing
        return store.create_record("inquiry", digest, parsed)

    def analysis_source(identity: str):
        inquiry = store.get(identity, "inquiry")
        try:
            # Reject unverified formulas/revisions before touching originals or
            # provider configuration; this gate predates structure enrichment.
            validate_inquiry_source(inquiry["payload"])
            from .inquiry_structure import enrich_inquiry_structure
            try:
                content = (store.root / "raw" / inquiry["payload"]["sha256"]).read_bytes()
            except OSError:
                raise ValueError("询价原件不可读取；禁止分析") from None
            if hashlib.sha256(content).hexdigest() != inquiry["payload"]["sha256"]:
                raise ValueError("询价原件哈希校验失败；禁止分析")
            inquiry = dict(inquiry, payload=enrich_inquiry_structure(inquiry["payload"], content))
            validate_inquiry_source(inquiry["payload"])
        except ValueError:
            store.audit(identity, "analysis_failed", {"reason": "unverified_inquiry_source"})
            raise
        return inquiry

    @app.get("/inquiries/{identity}/analysis-plan")
    def inquiry_analysis_plan(identity: str):
        from .inquiry_chunks import build_inquiry_plan
        from .inquiry_analysis import get_cached_chunk_ids, structure_summary
        inquiry = analysis_source(identity)
        plan = build_inquiry_plan(inquiry["payload"])
        configured, cached_ids = False, set()
        try:
            agent = AgentClient()
            configured = True
            cached_ids = set(get_cached_chunk_ids(store, inquiry, agent, plan))
        except ValueError:
            pass
        chunks = [{key: chunk[key] for key in ("id", "index", "start", "end", "characters")} | {"cached": chunk["id"] in cached_ids, "row_count": len(chunk.get("structured_rows", []))} for chunk in plan["chunks"]]
        return {key: value for key, value in plan.items() if key != "chunks"} | {"chunks": chunks, "agent_configured": configured, "structure_summary": structure_summary(inquiry["payload"])}

    def execute_analysis(identity: str, *, chunk_id: str | None = None, plan_id: str | None = None):
        from .inquiry_chunks import build_inquiry_plan
        from .inquiry_analysis import analyze_inquiry_all, analyze_inquiry_chunk
        inquiry = analysis_source(identity)
        if chunk_id is not None:
            plan = build_inquiry_plan(inquiry["payload"])
            if plan_id != plan["plan_id"]:
                raise HTTPException(409, "询价分块计划已变化，请刷新计划后重试。")
            if not any(chunk["id"] == chunk_id for chunk in plan["chunks"]):
                raise HTTPException(404, "询价分析分块不存在。")
        try:
            agent = AgentClient()
            if chunk_id is not None:
                return analyze_inquiry_chunk(store, inquiry, agent, chunk_id, plan_id=plan_id)
            return analyze_inquiry_all(store, inquiry, agent)
        except ValueError as exc:
            failure = safe_analysis_failure(exc)
            detail = {"reason": failure["code"], "retryable": failure["retryable"]}
            if chunk_id is not None:
                detail.update(chunk_id=chunk_id, plan_id=plan_id)
            store.audit(identity, "analysis_failed", detail)
            raise HTTPException(503, failure["message"]) from None

    @app.post("/inquiries/{identity}/analysis-chunks/{chunk_id}")
    def inquiry_analysis_chunk(identity: str, chunk_id: str, request: ChunkAnalyze):
        return execute_analysis(identity, chunk_id=chunk_id, plan_id=request.plan_id)

    @app.post("/inquiries/{identity}/analyze")
    def analyze(identity: str):
        return execute_analysis(identity)

    @app.post("/shipments/upload")
    async def upload_shipment(file: UploadFile = File(...), mapping_json: str | None = Form(None)):
        content, filename = await read_upload(file)
        try:
            mapping = json.loads(mapping_json) if mapping_json else None
        except json.JSONDecodeError:
            raise ValueError("mapping_json 不是合法 JSON") from None
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError("mapping_json 必须是对象")
        parsed = parse_erp_xlsx(content, filename, mapping)
        digest = store.save_original(content)
        parsed["sha256"] = digest
        key = digest + ":" + hashlib.sha256(json.dumps(mapping, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        existing = store.find("shipment", key)
        if existing:
            return existing
        return store.create_record("shipment", key, parsed)

    @app.post("/orders")
    def create_order(payload: dict):
        if {"order_root_id", "revision_no", "supersedes_order_id", "revision_reason", "root_order_id", "latest_order_id", "is_current"} & payload.keys():
            raise ValueError("版本信息由系统生成，不能在新建订单中提供")
        normalized = normalize_order(payload)
        if "inquiry_id" in normalized:
            if not isinstance(normalized["inquiry_id"], str) or not normalized["inquiry_id"].strip():
                raise ValueError("inquiry_id 必须是已导入询价的记录标识")
            store.get(normalized["inquiry_id"], "inquiry")
        return store.create_record("order", normalized["order_no"], normalized)

    @app.get("/orders/{identity}/revision-status")
    def revision_status(identity: str):
        from .revisions import order_revision_status
        return order_revision_status(store, identity)

    @app.post("/orders/{identity}/revisions")
    def revise_order(identity: str, body: RevisionCreate):
        from .revisions import create_order_revision
        try:
            return with_revision_status(create_order_revision(store, identity, body.payload, body.reason))
        except ValueError:
            store.audit(identity, "revision_rejected", {"reason": "validation_or_version_conflict"})
            raise

    @app.post("/orders/{identity}/allocations")
    def allocate(identity: str, body: AllocationCreate):
        try:
            return store.allocate(identity, body.shipment_id, body.lines, body.confirmation_ref)
        except ValueError:
            store.audit(identity, "allocation_rejected", {"shipment_id": body.shipment_id, "reason": "validation_or_conflict"})
            raise

    @app.get("/orders/{identity}/allocations")
    def allocations(identity: str):
        order = store.get(identity, "order")["payload"]
        records = [x for x in store.list_records("allocation", limit=100000) if x["payload"]["order_id"] == identity]
        from decimal import Decimal, localcontext
        with localcontext() as context:
            context.prec = 1024
            used = {x["line_id"]: Decimal(0) for x in order["items"]}
            for record in records:
                for row in record["payload"]["quantities"]:
                    used[row["order_line_id"]] += Decimal(row["quantity"])
            balance = [{"order_line_id": x["line_id"], "ordered": x["quantity"], "allocated": str(used[x["line_id"]]), "remaining": str(Decimal(x["quantity"]) - used[x["line_id"]])} for x in order["items"]]
        return {"allocations": records, "balance": balance, "fully_allocated": all(Decimal(x["remaining"]) == 0 for x in balance)}

    @app.post("/templates/{document_type}/upload")
    async def upload_template(document_type: str, file: UploadFile = File(...)):
        if document_type not in TEMPLATE_TYPES:
            raise ValueError("未知单据类型")
        content, filename = await read_upload(file)
        if Path(filename).suffix.lower() not in {".xlsx", ".xls", ".docx", ".doc", ".pdf"}:
            raise ValueError("模板应为 Excel、Word 或 PDF；收到后仍须单独检查格式")
        digest = store.save_original(content)
        payload = {"document_type": document_type, "filename": filename, "sha256": digest, "status": "received_unverified", "format_accepted": False}
        key = f"{document_type}:{digest}"
        existing = store.find("template", key)
        return existing or store.create_record("template", key, payload)

    @app.get("/templates")
    def templates():
        return store.list_records("template")

    @app.get("/templates/{identity}/inspection")
    def template_inspection(identity: str):
        template = store.get(identity, "template")["payload"]
        if not template["filename"].lower().endswith(".xlsx"):
            raise ValueError("当前字段映射检查支持可编辑 XLSX 原稿")
        content = (store.root / "raw" / template["sha256"]).read_bytes()
        profile = document_profile(content, template["document_type"])
        profile["layout_preflight"] = inspect_text_fit(content)
        return profile

    def document_profile(content: bytes, document_type: str):
        if document_type == "receipt":
            return inspect_receipt_template(content)
        if document_type == "customs":
            return inspect_customs_template(content)
        return candidate_profile(content, document_type)

    def draft_context(body: DraftCreate):
        require_current_order(body.order_id)
        template = store.get(body.template_id, "template")["payload"]
        if not template["filename"].lower().endswith(".xlsx"):
            raise ValueError("草稿生成需要已适配的 XLSX 模板")
        order = store.get(body.order_id, "order")["payload"]
        packing_warnings = []
        if body.allocation_id:
            allocation = store.get(body.allocation_id, "allocation")["payload"]
            if allocation["order_id"] != body.order_id:
                raise ValueError("分配不属于所选订单")
            from .allocations import bind_batch_packaging
            order, packing_warnings = bind_batch_packaging(allocation["batch_order"], order)
            packing_warnings = sorted(set(packing_warnings + allocation.get("unbound_order_packing_fields", [])))
        elif template["document_type"] in {"ci", "pl", "receipt", "customs"}:
            raise ValueError("CI、PL、报关单和签收单需选择已确认的实际出货分批分配")
        content = (store.root / "raw" / template["sha256"]).read_bytes()
        profile = document_profile(content, template["document_type"])
        preparer = prepare_customs_document if template["document_type"] == "customs" else prepare_document
        plan = preparer(profile, order, body.fields, body.item_extras)
        plan["unbound_order_packing_fields"] = packing_warnings
        return content, template, plan

    @app.post("/documents/prepare")
    def prepare(body: DraftCreate):
        return draft_context(body)[2]

    @app.post("/documents/drafts")
    def create_draft(body: DraftCreate):
        require_current_order(body.order_id)
        request = body.model_dump()
        # Bind idempotency to all deterministic generator code, so a repaired
        # engine never silently reuses an artifact from an earlier engine.
        engine = hashlib.sha256()
        for module in ("drafts.py", "receipts.py", "customs.py", "customs_data.py", "document_data.py", "allocations.py", "templates.py", "profiles.py", "calculations.py", "layout_review.py"):
            engine.update((Path(__file__).parent / module).read_bytes())
        engine_version = engine.hexdigest()
        key = hashlib.sha256(json.dumps({"request": request, "engine": engine_version}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        existing = store.find("draft", key)
        if existing:
            return existing
        try:
            content, template, plan = draft_context(body)
            if not plan["ready_for_review_draft"]:
                raise ValueError("草稿数据尚未齐备：" + ", ".join(plan["missing"]))
            if template["document_type"] == "receipt":
                output, report = generate_receipt_draft(content, plan["fields"], decimal_items(plan))
            elif template["document_type"] == "customs":
                headers, items = customs_engine_inputs(plan)
                output, report = generate_customs_draft(content, headers, items)
            else:
                output, report = generate_review_draft(content, template["document_type"], plan["fields"], decimal_items(plan))
            report["layout_preflight"] = inspect_text_fit(output)
        except ValueError:
            store.audit(body.order_id, "draft_generation_failed", {"template_id": body.template_id, "reason": "missing_or_invalid_data_or_unsupported_template"})
            raise
        digest = store.save_original(output)
        return store.create_record("draft", key, {"request": request, "engine_version": engine_version,
             "template_sha256": template["sha256"], "sha256": digest, "document_type": template["document_type"],
             "status": "review_draft", "format_accepted": False, "report": report,
             "download_note": "待审阅草稿，不能作为签署或正式报关依据"})

    @app.get("/documents/drafts/{identity}/download")
    def download_draft(identity: str):
        draft = store.get(identity, "draft")["payload"]
        require_current_order(draft["request"]["order_id"])
        return FileResponse(store.root / "raw" / draft["sha256"],
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="REVIEW-" + draft["document_type"] + "-" + identity + ".xlsx")

    @app.post("/documents/readiness")
    def readiness(body: Readiness):
        require_current_order(body.order_id)
        order = store.get(body.order_id, "order")["payload"]
        shipment = store.get(body.shipment_id, "shipment")["payload"] if body.shipment_id else None
        if body.allocation_id:
            allocation = store.get(body.allocation_id, "allocation")["payload"]
            if allocation["order_id"] != body.order_id or (body.shipment_id and allocation["shipment_id"] != body.shipment_id):
                raise ValueError("分批分配不属于所选订单或 ERP")
            order, shipment = allocation["batch_order"], allocation["shipment_view"]
        accepted = [r["payload"]["document_type"] for r in store.list_records("template") if r["payload"].get("format_accepted") is True]
        result = check_document_readiness(order, shipment, accepted)
        return {**result, "generation_available": False, "template_note": "模板接收不代表格式验收；须完成字段映射、内容与版式对比。"}

    @app.post("/workflows")
    def workflow_create(body: WorkflowCreate):
        return store.create_workflow(body.inquiry_id)

    @app.get("/workflows/{identity}")
    def workflow_get(identity: str):
        return store.workflow(identity)

    @app.post("/workflows/{identity}/advance")
    def advance(identity: str, body: Advance):
        workflow = store.workflow(identity)
        if workflow["version"] != body.expected_version:
            raise ConflictError("流程已发生变化，请刷新后重试")
        evidence = body.evidence.copy()
        links = workflow["payload"].copy()
        if body.target == "technical_ready":
            analysis = store.get(evidence.get("analysis_id", ""), "analysis")
            if analysis["payload"]["inquiry_id"] != workflow["inquiry_id"]:
                raise ValueError("分析不属于本询价")
            links["analysis_id"] = analysis["id"]
        elif body.target == "order_confirmed":
            require_current_order(evidence.get("order_id", ""))
            order = store.get(evidence.get("order_id", ""), "order")
            if order["payload"].get("inquiry_id") and order["payload"]["inquiry_id"] != workflow["inquiry_id"]:
                raise ValueError("成交订单已明确关联其他询价，不能接入本流程")
            links["order_id"] = order["id"]
        elif body.target == "shipment_verified":
            if not links.get("order_id"):
                raise ValueError("流程未关联成交订单")
            require_current_order(links["order_id"])
            shipment = store.get(evidence.get("shipment_id", ""), "shipment")
            order_payload = store.get(links["order_id"], "order")["payload"]
            shipment_payload = shipment["payload"]
            if evidence.get("allocation_id"):
                allocation = store.get(evidence["allocation_id"], "allocation")
                facts = allocation["payload"]
                if facts["order_id"] != links["order_id"] or facts["shipment_id"] != shipment["id"]:
                    raise ValueError("分批分配不属于本订单和出货记录")
                order_payload, shipment_payload = facts["batch_order"], facts["shipment_view"]
                evidence.update(shipment_facts_confirmed=True, shipment_confirmation_ref=facts["confirmation_ref"])
                links["allocation_id"] = allocation["id"]
            if shipment["payload"].get("source", {}).get("document_type") == "sales_order":
                ref = evidence.get("shipment_confirmation_ref")
                if evidence.get("shipment_facts_confirmed") is not True or not isinstance(ref, str) or not ref.strip():
                    raise ValueError("ERP 来源是销售订单，需确认实际出货事实并提供 shipment_confirmation_ref；不能自动视为出货单")
            # Domain reconciliation checks factual fields independently of caller flags.
            check = check_document_readiness(order_payload, shipment_payload, list(TEMPLATE_TYPES))
            reconciliation = check.get("reconciliation")
            if reconciliation is not None:
                if not reconciliation.get("passed", False):
                    raise ValueError("ERP 与订单对账未通过：" + json.dumps(reconciliation, ensure_ascii=False))
            else:
                raise ValueError("对账结果尚未实现；禁止只凭调用方标志通过")
            evidence["reconciliation_passed"] = True
            links["shipment_id"] = shipment["id"]
        elif body.target in {"documents_ready", "dispatched", "delivered", "closed"}:
            raise ValueError("此阶段依赖真实单据生成与交付集成，当前底座尚未验收")
        new_status = transition(workflow["status"], body.target, evidence)
        if new_status == workflow["status"]:
            if any(workflow["payload"].get(k) != v for k, v in links.items()):
                raise ConflictError("同状态重试不能替换关联业务记录")
            return workflow
        return store.update_workflow(identity, body.expected_version, new_status, links, evidence)

    @app.get("/records/{identity}")
    def record_get(identity: str):
        return with_revision_status(store.get(identity))

    @app.get("/audit/{identity}")
    def audit(identity: str):
        return store.history(identity)

    return app


# Use uvicorn --factory tradeflow.api:create_app to avoid import-time data creation.
