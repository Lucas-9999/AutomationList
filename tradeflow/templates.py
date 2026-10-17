"""Read and patch existing XLSX cells without rebuilding the workbook.

This is a low-level, local template primitive, not approval or document delivery.
The caller owns template/signature acceptance, business mapping and recalculation.
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO
import hashlib
import posixpath
import re
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape
from zipfile import BadZipFile, ZipFile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CELL_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
MAX_UNCOMPRESSED = 100 * 1024 * 1024


def _xml(content: bytes) -> ET.Element:
    # UTF-16/32 interleave ASCII markup with NUL bytes. Detect declarations
    # before ElementTree can expand an internal entity in those encodings.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", content.replace(b"\x00", b""), re.I):
        raise ValueError("不支持含 DTD/实体声明的模板 XML")
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise ValueError("模板包含无效 XML") from None


def _package(content: bytes) -> tuple[list, dict[str, bytes], bytes]:
    if not isinstance(content, bytes) or not content:
        raise ValueError("模板必须是非空 XLSX bytes")
    try:
        with ZipFile(BytesIO(content)) as package:
            infos = package.infolist()
            if len(infos) > 10000 or sum(info.file_size for info in infos) > MAX_UNCOMPRESSED:
                raise ValueError("模板 OOXML 解压大小或部件数量超限")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("模板 ZIP 部件名重复")
            if any(info.flag_bits & 1 for info in infos):
                raise ValueError("不支持加密 ZIP 模板")
            if any(name.startswith("/") or ".." in name.split("/") or "\\" in name for name in names):
                raise ValueError("模板包含非法部件路径")
            parts = {info.filename: package.read(info) for info in infos}
            return infos, parts, package.comment
    except (BadZipFile, KeyError, RuntimeError, NotImplementedError):
        raise ValueError("模板不是可读取的 XLSX ZIP 文件") from None


def _sheet_parts(parts: dict[str, bytes]) -> list[dict]:
    if "xl/workbook.xml" not in parts or "xl/_rels/workbook.xml.rels" not in parts:
        raise ValueError("模板缺少 workbook 或 workbook relationships")
    root = _xml(parts["xl/workbook.xml"])
    if root.tag != f"{{{MAIN}}}workbook":
        raise ValueError("当前仅支持标准 transitional XLSX 命名空间")
    relationships = {}
    for relation in _xml(parts["xl/_rels/workbook.xml.rels"]):
        identity = relation.get("Id")
        if identity in relationships:
            raise ValueError("工作簿关系 ID 重复")
        relationships[identity] = relation
    sheets = []
    names = set()
    for sheet in root.findall(f"{{{MAIN}}}sheets/{{{MAIN}}}sheet"):
        name, identity = sheet.get("name"), sheet.get(f"{{{DOC_REL}}}id")
        relation = relationships.get(identity)
        if not name or name in names or relation is None:
            raise ValueError("工作表名称或关系无效/重复")
        if relation.get("TargetMode") == "External" or relation.get("Type") != DOC_REL + "/worksheet":
            raise ValueError("不支持外部或非 worksheet 工作表")
        target = relation.get("Target", "")
        part = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if not part.startswith("xl/") or part not in parts:
            raise ValueError("工作表目标部件缺失或越界")
        names.add(name)
        sheets.append({"name": name, "part": part, "state": sheet.get("state", "visible")})
    if not sheets:
        raise ValueError("模板没有工作表")
    return sheets


def _coordinate(reference: str) -> tuple[int, int]:
    match = CELL_REF.fullmatch(reference) if isinstance(reference, str) else None
    if match is None:
        raise ValueError("单元格坐标必须为 A1 形式")
    column = 0
    for character in match[1]:
        column = column * 26 + ord(character) - ord("A") + 1
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ValueError("单元格坐标超出 XLSX 范围")
    return column, row


def _cells(root: ET.Element) -> dict[str, ET.Element]:
    if root.tag != f"{{{MAIN}}}worksheet":
        raise ValueError("工作表 XML 命名空间无效")
    cells = {}
    for cell in root.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row/{{{MAIN}}}c"):
        reference = cell.get("r")
        _coordinate(reference)
        if reference in cells:
            raise ValueError("工作表单元格坐标重复")
        formulae = cell.findall(f"{{{MAIN}}}f")
        values = cell.findall(f"{{{MAIN}}}v")
        inline = cell.findall(f"{{{MAIN}}}is")
        if len(formulae) > 1 or len(values) > 1 or len(inline) > 1 or (inline and (formulae or values)):
            raise ValueError("单元格含重复或互斥的公式/值节点：" + reference)
        cells[reference] = cell
    return cells


def _merges(root: ET.Element) -> list[str]:
    return [merge.get("ref", "") for merge in root.findall(f"{{{MAIN}}}mergeCells/{{{MAIN}}}mergeCell")]


def _is_merge_child(reference: str, ranges: list[str]) -> bool:
    col, row = _coordinate(reference)
    for merged in ranges:
        endpoints = merged.split(":")
        if len(endpoints) != 2:
            raise ValueError("合并单元格区域无效")
        first_col, first_row = _coordinate(endpoints[0])
        last_col, last_row = _coordinate(endpoints[1])
        if first_col > last_col or first_row > last_row:
            raise ValueError("合并单元格区域顺序无效")
        if first_col <= col <= last_col and first_row <= row <= last_row and reference != endpoints[0]:
            return True
    return False


def inspect_template(content: bytes) -> dict:
    """Inspect sparse existing cells only; never expand worksheet dimensions.

    Formula caches are reported as stored, not recalculated or verified. Media
    counts do not establish that a template is free of old customer signatures.
    """
    _, parts, _ = _package(content)
    sheets = _sheet_parts(parts)
    external_parts = [name for name in parts if name.startswith("xl/externalLinks/")]
    external_relationships = []
    for name, value in parts.items():
        if name.endswith(".rels"):
            for relation in _xml(value):
                if relation.get("TargetMode") == "External":
                    external_relationships.append({"part": name, "type": relation.get("Type"), "target": relation.get("Target")})
    warnings = []
    for sheet in sheets:
        root = _xml(parts[sheet["part"]])
        cells = _cells(root)
        formulas = []
        cell_metadata = {}
        for reference, cell in cells.items():
            formula, cached = cell.find(f"{{{MAIN}}}f"), cell.find(f"{{{MAIN}}}v")
            cell_metadata[reference] = {"style": cell.get("s"), "type": cell.get("t"), "formula": formula is not None}
            if formula is not None:
                text = formula.text or ""
                is_external = bool(re.search(r"\[[0-9]+\]", text)) or "http://" in text or "https://" in text
                formulas.append({"cell": reference, "formula": text, "attributes": dict(formula.attrib),
                                 "cached_value": cached.text if cached is not None else None,
                                 "cache_verified": False, "external_reference": is_external})
                if is_external:
                    warnings.append(f"{sheet['name']}!{reference}: 外部公式可能依赖旧工作簿，必须映射或独立核验")
        dimension = root.find(f"{{{MAIN}}}dimension")
        sheet.update(dimension=dimension.get("ref") if dimension is not None else None,
                     cells=cell_metadata, cell_count=len(cells), formulas=formulas, merged_ranges=_merges(root),
                     drawings=len(root.findall(f"{{{MAIN}}}drawing")),
                     legacy_drawings=len(root.findall(f"{{{MAIN}}}legacyDrawing")))
    if external_parts or external_relationships:
        warnings.append("存在外部关系或外部链接部件；读取缓存不能证明当前业务数据准确")
    if any(sheet["formulas"] for sheet in sheets):
        warnings.append("本引擎不重算公式；原缓存是未验证历史值，业务输出前需重算并核验")
    return {"sha256": hashlib.sha256(content).hexdigest(), "sheets": sheets,
            "parts": list(parts), "media_parts": [name for name in parts if name.startswith("xl/media/") and not name.endswith("/")],
            "external_link_parts": external_parts, "external_relationships": external_relationships,
            "signature_review_required": True, "formula_recalculation_available": False, "warnings": warnings}


def _tag_end(content: bytes, start: int) -> int:
    quote = None
    for index in range(start, len(content)):
        byte = content[index]
        if quote is not None:
            if byte == quote:
                quote = None
        elif byte in (34, 39):
            quote = byte
        elif byte == 62:
            return index + 1
    raise ValueError("XML 标签未关闭")


def _cell_spans(content: bytes) -> dict[str, dict]:
    """Expat byte offsets avoid rewriting unrelated XML or namespace prefixes."""
    parser = expat.ParserCreate(namespace_separator="}")
    stack = []
    spans = {}
    cell_path = ("worksheet", "sheetData", "row", "c")

    def start(name, attributes):
        local = name.split("}")[-1]
        offset = parser.CurrentByteIndex
        end = _tag_end(content, offset)
        path = tuple(frame["name"] for frame in stack) + (local,)
        frame = {"name": local, "start": offset, "open_end": end, "attributes": attributes,
                 "path": path, "self_closing": content[offset:end].rstrip().endswith(b"/>"), "children": []}
        stack.append(frame)

    def finish(name):
        frame = stack.pop()
        frame["end"] = frame["open_end"] if frame["self_closing"] else _tag_end(content, parser.CurrentByteIndex)
        if frame["path"] == cell_path:
            reference = frame["attributes"].get("r")
            if reference in spans:
                raise ValueError("单元格坐标重复")
            spans[reference] = frame
        elif len(frame["path"]) == 5 and frame["path"][:4] == cell_path:
            stack[-1]["children"].append(frame)

    parser.StartElementHandler = start
    parser.EndElementHandler = finish
    try:
        parser.Parse(content, True)
    except expat.ExpatError:
        raise ValueError("模板工作表 XML 解析失败") from None
    return spans


def _value_xml(value: object, prefix: bytes) -> tuple[bytes | None, bytes]:
    if value is None:
        return None, b""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("单元格数值仅允许 Decimal/int；拒绝 bool/float")
    if isinstance(value, (Decimal, int)):
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("单元格数值必须有限，禁止 NaN/Infinity")
        text = str(number).encode("ascii")
        if len(text) > 1024 or abs(number.as_tuple().exponent) > 1000:
            raise ValueError("单元格数值精度超出支持范围")
        return None, b"<" + prefix + b"v>" + text + b"</" + prefix + b"v>"
    if not isinstance(value, str):
        raise ValueError("单元格只接受字符串、Decimal、int 或 None 清空")
    if len(value) > 32767:
        raise ValueError("单元格文本超过 Excel 32767 字符限制")
    for character in value:
        number = ord(character)
        if not (number in (9, 10, 13) or 32 <= number <= 0xD7FF or 0xE000 <= number <= 0xFFFD or 0x10000 <= number <= 0x10FFFF):
            raise ValueError("单元格文本包含 XML 非法字符")
    # Inline strings leave sharedStrings.xml and its old indexes untouched.
    text = escape(value).replace("\r", "&#13;").encode("utf-8")
    return b"inlineStr", (b"<" + prefix + b"is><" + prefix + b't xml:space="preserve">' + text
                           + b"</" + prefix + b"t></" + prefix + b"is>")


def _replace_cell(content: bytes, span: dict, value: object) -> bytes:
    opening = content[span["start"]:span["open_end"]]
    qualified = re.match(br"<([A-Za-z_][A-Za-z0-9_.:-]*)", opening)
    if qualified is None:
        raise ValueError("单元格标签格式不支持")
    name = qualified[1]
    prefix = name[:-1]  # c -> b''; x:c -> b'x:'
    cell_type, new_value = _value_xml(value, prefix)
    opening = re.sub(br"\s+t\s*=\s*([\"']).*?\1", b"", opening, flags=re.S)
    opening = re.sub(br"/\s*>\Z", b">", opening)
    if cell_type is not None:
        opening = opening[:-1] + b' t="' + cell_type + b'">'
    body_end = span["open_end"] if span["self_closing"] else content.rfind(b"</", span["open_end"], span["end"])
    if body_end < span["open_end"]:
        raise ValueError("单元格结束标签定位失败")
    remove = sorted((child["start"], child["end"]) for child in span["children"] if child["name"] in ("v", "is", "f"))
    preserved = []
    cursor = span["open_end"]
    for start, end in remove:
        preserved.append(content[cursor:start])
        cursor = end
    preserved.append(content[cursor:body_end])
    closing = b"</" + name + b">" if span["self_closing"] else content[body_end:span["end"]]
    return opening + new_value + b"".join(preserved) + closing


def patch_xlsx_cells(content: bytes, updates: dict, *, allow_formula_overwrite: bool = False) -> bytes:
    """Patch only explicitly mapped, existing cells in an in-memory XLSX copy.

    updates is {sheet_name: {A1_reference: str | Decimal | int | None}}.
    None clears prior values, including shared-string references. Strings remain
    text even when starting with '='. Formula overwrite is opt-in and grouped
    array/shared formulas are unsupported. No formula is recalculated; any
    remaining caches must be independently invalidated/recalculated and checked
    by the caller before business acceptance. ZIP entry payloads not modified
    here remain byte-identical (the ZIP container itself is repackaged).
    """
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates 必须包含明确的工作表与单元格映射")
    if not isinstance(allow_formula_overwrite, bool):
        raise ValueError("allow_formula_overwrite 必须是布尔值")
    infos, parts, comment = _package(content)
    sheets = {sheet["name"]: sheet for sheet in _sheet_parts(parts)}
    replacements = {}
    for sheet_name, mapped in updates.items():
        if sheet_name not in sheets:
            raise ValueError(f"工作表不存在: {sheet_name}")
        if not isinstance(mapped, dict) or not mapped:
            raise ValueError(f"{sheet_name}: 需要非空单元格映射")
        part = sheets[sheet_name]["part"]
        original = parts[part]
        root = _xml(original)
        cells = _cells(root)
        merges = _merges(root)
        spans = _cell_spans(original)
        patches = []
        for reference, value in mapped.items():
            _coordinate(reference)
            if reference not in cells or reference not in spans:
                raise ValueError(f"{sheet_name}!{reference}: 原模板没有此单元格，禁止插入新格")
            if _is_merge_child(reference, merges):
                raise ValueError(f"{sheet_name}!{reference}: 合并区域非主格，禁止覆盖")
            formula = cells[reference].find(f"{{{MAIN}}}f")
            if formula is not None:
                if not allow_formula_overwrite:
                    raise ValueError(f"{sheet_name}!{reference}: 公式覆盖未明确授权")
                if formula.get("t") in ("array", "shared", "dataTable") or formula.get("ref"):
                    raise ValueError(f"{sheet_name}!{reference}: 不支持覆盖数组/共享/数据表公式")
            span = spans[reference]
            patches.append((span["start"], span["end"], _replace_cell(original, span, value)))
        changed = original
        for start, end, replacement in sorted(patches, reverse=True):
            changed = changed[:start] + replacement + changed[end:]
        # Re-parse the modified part before repackaging, without serializing it.
        _cells(_xml(changed))
        replacements[part] = changed
    output = BytesIO()
    with ZipFile(output, "w") as package:
        package.comment = comment
        for info in infos:
            package.writestr(info, replacements.get(info.filename, parts[info.filename]))
    return output.getvalue()
