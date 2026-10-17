"""Read-only DOCX inquiry transcription; preserves text without interpreting it."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import PurePosixPath
import stat
from zipfile import BadZipFile, ZipFile

from .templates import _xml

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
DOCUMENT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
MAX_INPUT_BYTES = 15 * 1024 * 1024
MAX_PARTS = 2000
MAX_PART_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_BLOCKS = 10000
MAX_BODY_CHARACTERS = 180000
MAX_XML_DEPTH = 128
MAX_XML_NODES = 200000


def _package(content: bytes) -> dict[str, bytes]:
    if not isinstance(content, bytes) or not content:
        raise ValueError("询价文档必须为非空 DOCX bytes")
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("询价文档超过 15 MiB 输入上限")
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PARTS:
                raise ValueError("询价文档 ZIP 部件数量超限")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("询价文档 ZIP 解压总大小超限")
            seen = set()
            for info in infos:
                # ZipInfo normalizes backslashes on Windows even while reading.
                # Inspect its original wire name before allowing that rewrite.
                name = info.orig_filename
                if (not name or "\x00" in name or "\\" in name or name.startswith("/") or ":" in name
                        or any(part in {".", "..", ""} for part in name.rstrip("/").split("/"))):
                    raise ValueError("询价文档 ZIP 非法部件路径")
                if name.casefold() in seen:
                    raise ValueError("询价文档 ZIP 重复部件路径")
                seen.add(name.casefold())
                if info.flag_bits & 1:
                    raise ValueError("询价文档不支持加密 ZIP 部件")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("询价文档不支持 ZIP 符号链接")
                if info.file_size > MAX_PART_BYTES:
                    raise ValueError("询价文档 ZIP 单部件大小超限")
                if info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 500:
                    raise ValueError("询价文档 ZIP 压缩比超限")
            return {info.filename: archive.read(info) for info in infos if not info.is_dir()}
    except (BadZipFile, RuntimeError, NotImplementedError, OSError):
        raise ValueError("询价文件不是可读取的 DOCX ZIP") from None


def parse_inquiry_document(content: bytes, filename: str) -> dict:
    """Transcribe paragraphs and table cells in document order, without OCR.

    Ambiguous fields, revision text, unsupported text containers and omitted
    supplementary text disable model analysis instead of claiming completeness.
    Neither external relationships nor embedded objects are opened.
    """
    if not isinstance(filename, str) or not filename.lower().endswith(".docx"):
        raise ValueError("询价文档解析仅支持 .docx")
    filename = PurePosixPath(filename.replace("\\", "/")).name
    parts = _package(content)
    if "[Content_Types].xml" not in parts or "word/document.xml" not in parts:
        raise ValueError("询价文件缺少标准 DOCX 正文或内容类型")
    roots = {name: _xml(value) for name, value in parts.items()
             if name.endswith((".xml", ".rels"))}
    node_count = 0
    for root in roots.values():
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            node_count += 1
            if depth > MAX_XML_DEPTH or node_count > MAX_XML_NODES:
                raise ValueError("询价文档 XML 层级或节点数量超限；不会截断正文")
            stack.extend((child, depth + 1) for child in node)
    types = roots["[Content_Types].xml"]
    if types.tag != f"{{{CONTENT_TYPES}}}Types":
        raise ValueError("询价文件 DOCX 内容类型无效")
    overrides = [node.get("ContentType") for node in types
                 if node.get("PartName") == "/word/document.xml"]
    if overrides != [DOCUMENT_CONTENT_TYPE] or any("macroenabled" in str(node.attrib).lower() for node in types):
        raise ValueError("询价文件必须为标准无宏 DOCX 文档")
    if any("vbaproject" in name.lower() for name in parts):
        raise ValueError("询价文件不支持含宏部件的 DOCX")
    document = roots["word/document.xml"]
    if document.tag != f"{{{WORD}}}document":
        raise ValueError("询价文件 Word 正文命名空间无效")
    bodies = document.findall(f"{{{WORD}}}body")
    if len(bodies) != 1:
        raise ValueError("询价文件必须有且只有一个 Word 正文")

    warnings, blockers, features, external = [], [], [], []
    blocks, tables = [], []
    body_lines = ["DOCX 询价原文转录。保留段落和表格顺序，未翻译、未推断技术字段。"]
    body_length = len(body_lines[0])
    paragraph_index = 0

    def block(reason: str, warning: str):
        if reason not in blockers:
            blockers.append(reason)
        warnings.append(warning)

    risky = {
        "fldSimple": ("unverified_fields", "Word 域及缓存结果未经核验，不能作为已确认技术要求"),
        "fldChar": ("unverified_fields", "Word 域及缓存结果未经核验，不能作为已确认技术要求"),
        "instrText": ("unverified_fields", "Word 域代码未经核验，不能作为正文事实"),
        "ins": ("tracked_revisions", "跟踪修订尚未确定接受或拒绝，保留可见原文但暂停文本分析"),
        "del": ("tracked_revisions", "跟踪修订尚未确定接受或拒绝，保留删除原文但暂停文本分析"),
        "moveFrom": ("tracked_revisions", "移动修订尚未确定接受或拒绝"),
        "moveTo": ("tracked_revisions", "移动修订尚未确定接受或拒绝"),
        "txbxContent": ("unparsed_textbox", "文本框只登记元信息，尚未完整解析其顺序与文字"),
        "altChunk": ("unparsed_embedded_content", "外部导入正文只登记元信息，未读取或执行"),
        "object": ("unparsed_embedded_content", "嵌入对象只登记元信息，未读取或执行"),
        "vanish": ("hidden_text", "文档含隐藏文字设置，显示语义未经核验"),
        "webHidden": ("hidden_text", "文档含隐藏文字设置，显示语义未经核验"),
        "sym": ("unparsed_symbol", "字体符号未转换，不能推断符号含义"),
        "sdt": ("unverified_content_control", "内容控件显示语义未经核验，已保留其中原文"),
        "numPr": ("unparsed_numbering", "自动编号未展开，不能将其当作已读取的型号或数量"),
        "footnoteReference": ("unparsed_supplementary_text", "脚注引用未合入正文，需核验脚注内容"),
        "endnoteReference": ("unparsed_supplementary_text", "尾注引用未合入正文，需核验尾注内容"),
        "customXml": ("unparsed_body_container", "自定义 XML 正文容器显示语义未经核验"),
        "subDoc": ("unparsed_embedded_content", "子文档引用未读取，不能推断其内容"),
    }
    # Word ships large style/numbering libraries containing unused list and
    # hidden-text definitions. Only effective content and its style ancestry
    # affect whether this inquiry's text is safe to analyze.
    content_roots = {"word/document.xml": document}
    for name, root in roots.items():
        if name.startswith(("word/header", "word/footer", "word/comments", "word/footnotes", "word/endnotes")):
            if any(n.tag in {f"{{{WORD}}}t", f"{{{WORD}}}delText"} and (n.text or "").strip()
                   for n in root.iter()):
                content_roots[name] = root

    def scan_features(part_name, root, style_id=None):
        for node in root.iter():
            if node.tag.startswith("{" + WORD + "}"):
                local = node.tag.split("}", 1)[1]
                if local in risky:
                    reason, warning = risky[local]
                    features.append({"part": part_name, "feature": local,
                                     "style_id": style_id,
                                     "attributes": dict(node.attrib),
                                     "raw_instruction": node.text if local == "instrText" else None,
                                     "analysis_status": "not_verified"})
                    block(reason, warning)
                elif local.endswith("PrChange") or local in {"moveFromRangeStart", "moveToRangeStart", "delText"}:
                    features.append({"part": part_name, "feature": local, "analysis_status": "not_verified"})
                    block("tracked_revisions", "文档含修订文字或属性，尚未确定最终版本")
            if node.tag.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/math}"):
                block("unparsed_math", "Word 数学公式未转换，不能推断其中规格或计算结果")
            if node.tag == "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent":
                block("unparsed_alternative_content", "Word 替代内容分支未经显示验证，不能将多个分支合并为事实")
            if len(features) > MAX_BLOCKS:
                raise ValueError("询价文档未核验特征数量超限；不会截断风险")

    for part_name, root in content_roots.items():
        scan_features(part_name, root)

    referenced_styles = set()
    default_needed = set()
    for root in content_roots.values():
        for node in root.iter():
            if node.tag in {f"{{{WORD}}}pStyle", f"{{{WORD}}}rStyle", f"{{{WORD}}}tblStyle"}:
                identity = node.get(f"{{{WORD}}}val")
                if identity:
                    referenced_styles.add(identity)
            for local, properties, reference, kind in (
                ("p", "pPr", "pStyle", "paragraph"),
                ("r", "rPr", "rStyle", "character"),
                ("tbl", "tblPr", "tblStyle", "table"),
            ):
                if node.tag == f"{{{WORD}}}{local}" and node.find(f"{{{WORD}}}{properties}/{{{WORD}}}{reference}") is None:
                    default_needed.add(kind)

    style_root = roots.get("word/styles.xml")
    style_definitions, default_styles, resolved_styles = {}, {}, set()
    if style_root is not None:
        for style in style_root.findall(f"{{{WORD}}}style"):
            identity = style.get(f"{{{WORD}}}styleId")
            if not identity or identity in style_definitions:
                raise ValueError("询价文档样式 ID 缺失或重复")
            style_definitions[identity] = style
            if style.get(f"{{{WORD}}}default", "").lower() in {"1", "true", "on"}:
                kind = style.get(f"{{{WORD}}}type")
                if kind in default_styles:
                    raise ValueError("询价文档同类型默认样式重复")
                default_styles[kind] = identity
        defaults = style_root.find(f"{{{WORD}}}docDefaults")
        if defaults is not None:
            scan_features("word/styles.xml", defaults, "docDefaults")
    style_seeds = referenced_styles | {identity for kind, identity in default_styles.items()
                                     if kind in default_needed}
    for seed in sorted(style_seeds):
        identity, chain = seed, set()
        while identity and identity not in resolved_styles:
            if identity in chain:
                block("unverified_style", "已使用的 Word 样式继承存在循环，显示语义未经核验")
                break
            chain.add(identity)
            style = style_definitions.get(identity)
            if style is None:
                block("unverified_style", "已使用的 Word 样式定义缺失，显示语义未经核验：" + identity)
                break
            scan_features("word/styles.xml", style, identity)
            based_on = style.find(f"{{{WORD}}}basedOn")
            identity = based_on.get(f"{{{WORD}}}val") if based_on is not None else None
        resolved_styles.update(chain)

    for part_name, root in roots.items():
        for node in root.iter(f"{{{REL}}}Relationship"):
            if node.get("TargetMode", "").lower() == "external":
                external.append({"part": part_name, "id": node.get("Id"),
                                 "type": node.get("Type"), "target": node.get("Target")})
    if external:
        warnings.append("外部关系仅登记，未访问网络、链接文件或自动更新内容")

    supplementary = []
    for name, root in roots.items():
        if name.startswith(("word/header", "word/footer", "word/comments", "word/footnotes", "word/endnotes")):
            text = "".join(n.text or "" for n in root.iter() if n.tag in {f"{{{WORD}}}t", f"{{{WORD}}}delText"})
            supplementary.append({"part": name, "text_present": bool(text.strip()),
                                  "text_character_count": len(text), "analysis_status": "not_analyzed"})
            warnings.append(name + "：页眉页脚、批注或脚注仅登记元信息，未合入正文")
            if text.strip():
                block("unparsed_supplementary_text", "辅助部件含未解析文字，不能声称已完整读懂询价")

    def paragraph_text(element):
        values = []
        def collect(node):
            if node.tag == f"{{{WORD}}}txbxContent":
                return
            if node.tag in {f"{{{WORD}}}t", f"{{{WORD}}}delText"}:
                values.append(node.text or "")
            elif node.tag == f"{{{WORD}}}tab":
                values.append("\t")
            elif node.tag in {f"{{{WORD}}}br", f"{{{WORD}}}cr"}:
                values.append("\n")
            elif node.tag == f"{{{WORD}}}noBreakHyphen":
                values.append("\u2011")
            elif node.tag == f"{{{WORD}}}softHyphen":
                values.append("\u00ad")
            for child in node:
                collect(child)
        collect(element)
        return "".join(values)

    def walk(container, path, context=None):
        nonlocal paragraph_index, body_length
        for index, node in enumerate(container, 1):
            local = node.tag.rsplit("}", 1)[-1]
            location = path + f"/{local}[{index}]"
            if node.tag == f"{{{WORD}}}p":
                paragraph_index += 1
                if len(blocks) >= MAX_BLOCKS:
                    raise ValueError("询价文档段落块数超过上限；不会截断正文")
                raw = paragraph_text(node)
                item = {"part": "word/document.xml", "path": location,
                        "paragraph_index": paragraph_index, "raw": raw, **(context or {})}
                blocks.append(item)
                line = f"[word/document.xml:{location}] 原文：\n{raw}"
                body_length += len(line) + 1
                if body_length > MAX_BODY_CHARACTERS:
                    raise ValueError("询价正文字符超过上限；不会截断规格")
                body_lines.append(line)
            elif node.tag == f"{{{WORD}}}tbl":
                identity = len(tables) + 1
                rows = node.findall(f"{{{WORD}}}tr")
                if any(n.tag == f"{{{WORD}}}tr" for child in node
                       if child.tag != f"{{{WORD}}}tr" for n in child.iter()):
                    block("unparsed_body_container", "表格含被控件或修订包裹的行，未擅自合并这些明细")
                tables.append({"part": "word/document.xml", "path": location,
                               "table_index": identity, "row_count": len(rows),
                               "cell_counts": [len(row.findall(f"{{{WORD}}}tc")) for row in rows]})
                for ri, row in enumerate(rows, 1):
                    if any(n.tag == f"{{{WORD}}}tc" for child in row
                           if child.tag != f"{{{WORD}}}tc" for n in child.iter()):
                        block("unparsed_body_container", "表格含被控件或修订包裹的单元格，未擅自合并这些明细")
                    for ci, cell in enumerate(row.findall(f"{{{WORD}}}tc"), 1):
                        walk(cell, location + f"/tr[{ri}]/tc[{ci}]",
                             {"table_index": identity, "row_index": ri, "cell_index": ci})
            elif node.tag in {f"{{{WORD}}}sdt", f"{{{WORD}}}sdtContent", f"{{{WORD}}}ins", f"{{{WORD}}}del",
                              f"{{{WORD}}}moveFrom", f"{{{WORD}}}moveTo"}:
                walk(node, location, context)
            elif local not in {"sectPr", "pPr", "tcPr", "sdtPr", "sdtEndPr"}:
                # Unknown containers may carry text or branches that this
                # transcription cannot order safely (e.g. AlternateContent).
                if any(n.tag in {f"{{{WORD}}}t", f"{{{WORD}}}p", f"{{{WORD}}}tbl"} for n in node.iter()):
                    block("unparsed_body_container", "未支持的正文容器仅登记元信息，未擅自合并其文字")
                    features.append({"part": "word/document.xml", "path": location,
                                     "feature": local, "analysis_status": "not_analyzed"})
    walk(bodies[0], "/body")

    attachments = [{"filename": name.rsplit("/", 1)[-1], "part": name,
                    "sha256": hashlib.sha256(value).hexdigest(), "size_bytes": len(value),
                    "source_type": "embedded_media" if name.startswith("word/media/") else "embedded_object",
                    "analysis_status": "not_analyzed"}
                   for name, value in parts.items() if name.startswith(("word/media/", "word/embeddings/"))]
    if attachments:
        warnings.append("not-analyzed：图片、技术图纸及嵌入对象仅登记元信息，尚未进行 OCR 或视觉理解")
    if any(item["source_type"] == "embedded_object" for item in attachments):
        block("unparsed_embedded_content", "嵌入文件未解析，不能声称已完整读懂询价")
    has_text = any(item["raw"].strip() for item in blocks)
    if not has_text:
        block("no_analyzable_text", "没有可分析的段落或表格正文；不能用图片或文件名推断技术要求")
    warnings.append("只读原文转录；未应用页面排版、自动编号、OCR 或技术字段推断")
    body_lines.extend("警示：" + warning for warning in dict.fromkeys(warnings))
    body = "\n".join(body_lines)
    if len(body) > MAX_BODY_CHARACTERS:
        raise ValueError("询价正文字符超过上限；不会截断规格")
    return {"subject": filename, "sender": "", "message_id": "", "body": body,
            "sha256": hashlib.sha256(content).hexdigest(), "source_type": "inquiry_document",
            "source": {"filename": filename, "source_type": "inquiry_document",
                       "text_analysis_available": has_text and not blockers,
                       "analysis_blockers": blockers, "tables": tables, "block_count": len(blocks),
                       "features": features, "supplementary_parts": supplementary,
                       "style_resolution": {"referenced_style_ids": sorted(referenced_styles),
                                            "resolved_style_ids": sorted(resolved_styles),
                                            "default_style_types_used": sorted(default_needed)},
                       "external_relationships": external},
            "blocks": blocks, "attachments": attachments, "warnings": list(dict.fromkeys(warnings))}
