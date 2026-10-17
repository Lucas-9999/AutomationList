"""A bounded Decimal calculator for the project's XLSX template formulas.

Not an Excel engine. No eval, network, file writes, or use of historical formula
caches as inputs. Division is explicitly rounded to 50 significant digits.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from io import BytesIO
import re
from xml.sax.saxutils import escape
from zipfile import ZipFile

from .templates import MAIN, _cell_spans, _cells, _coordinate, _package, _sheet_parts, _xml

DIVISION_PRECISION = 50
MAX_RANGE_CELLS = 100000
_TOKEN = re.compile(r"\s*(?:(\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)|((?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)|([A-Za-z_][A-Za-z0-9_.]*)|([+*/():,-]))")


def _number(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except DecimalException:
        raise ValueError("公式输入不是合法 Decimal") from None
    if not value.is_finite() or abs(value.as_tuple().exponent) > 1000 or len(value.as_tuple().digits) > 10000:
        raise ValueError("公式输入非有限或超出数值范围")
    return value


def _numeric(value) -> Decimal:
    if value is None:
        return Decimal(0)
    if not isinstance(value, Decimal):
        raise ValueError("算术表达式引用了文本或非数值单元格")
    return value


def _operate(left: Decimal, operator: str, right: Decimal) -> Decimal:
    if operator == "/" and right == 0:
        raise ValueError("公式除数为零")
    with localcontext() as context:
        context.rounding = ROUND_HALF_EVEN
        context.prec = (DIVISION_PRECISION if operator == "/" else
                        max(50, len(left.as_tuple().digits) + len(right.as_tuple().digits)
                            + abs(left.as_tuple().exponent - right.as_tuple().exponent) + 16))
        if context.prec > 20000:
            raise ValueError("公式运算精度超出支持范围")
        try:
            if operator == "+":
                value = left + right
            elif operator == "-":
                value = left - right
            elif operator == "*":
                value = left * right
            else:
                value = left / right
        except DecimalException:
            raise ValueError("Decimal 公式计算失败") from None
    return _number(str(value))


def _range(first: str, last: str) -> tuple[int, int, int, int]:
    first_col, first_row = _coordinate(first.replace("$", "").upper())
    last_col, last_row = _coordinate(last.replace("$", "").upper())
    if first_col > last_col or first_row > last_row:
        raise ValueError("SUM 或共享公式区域顺序无效")
    if (last_col - first_col + 1) * (last_row - first_row + 1) > MAX_RANGE_CELLS:
        raise ValueError("公式区域超出稀疏计算范围上限")
    return first_col, first_row, last_col, last_row


def _col_name(column: int) -> str:
    letters = ""
    while column:
        column, digit = divmod(column - 1, 26)
        letters = chr(65 + digit) + letters
    return letters


class _Expression:
    def __init__(self, formula: str, lookup):
        if not formula or len(formula) > 8192:
            raise ValueError("公式为空或过长")
        formula = formula.strip()
        if formula.startswith("="):
            formula = formula[1:]
        if any(symbol in formula for symbol in ("[", "]", "!")):
            raise ValueError("禁止外部工作簿或跨工作表公式引用")
        self.tokens = []
        offset = 0
        while offset < len(formula):
            if not formula[offset:].strip():
                break
            match = _TOKEN.match(formula, offset)
            if not match:
                raise ValueError("公式含不支持的语法")
            kind = next(index for index, value in enumerate(match.groups()) if value is not None)
            self.tokens.append((kind, match.groups()[kind].upper()))
            offset = match.end()
        if len(self.tokens) > 1024:
            raise ValueError("公式 token 数量超限")
        self.index = 0
        self.lookup = lookup
        self.depth = 0

    def peek(self, value=None):
        token = self.tokens[self.index] if self.index < len(self.tokens) else None
        return token if value is None else token is not None and token[1] == value

    def take(self, value=None):
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise ValueError("公式表达式不完整或不受支持")
        self.index += 1
        return token

    def evaluate(self):
        value = self.expression()
        if self.peek() is not None:
            raise ValueError("公式包含未支持的后续语法或函数")
        return Decimal(0) if value is None else value

    def expression(self):
        left = self.term()
        while self.peek("+") or self.peek("-"):
            operator = self.take()[1]
            left = _operate(_numeric(left), operator, _numeric(self.term()))
        return left

    def term(self):
        left = self.atom()
        while self.peek("*") or self.peek("/"):
            operator = self.take()[1]
            left = _operate(_numeric(left), operator, _numeric(self.atom()))
        return left

    def atom(self):
        self.depth += 1
        if self.depth > 100:
            raise ValueError("公式嵌套层级超限")
        try:
            if self.peek("+") or self.peek("-"):
                sign = self.take()[1]
                value = _numeric(self.atom())
                return value if sign == "+" else _operate(Decimal(0), "-", value)
            if self.peek("("):
                self.take("(")
                value = self.expression()
                self.take(")")
                return value
            kind, token = self.take()
            if kind == 0:
                reference = token.replace("$", "")
                _coordinate(reference)
                return self.lookup(reference)
            if kind == 1:
                return _number(token)
            if kind == 2 and token == "ROUND":
                self.take("(")
                value = _numeric(self.expression())
                self.take(",")
                places = _numeric(self.expression())
                self.take(")")
                if places != places.to_integral_value() or abs(places) > 100:
                    raise ValueError("ROUND 精度必须为范围内整数")
                with localcontext() as context:
                    context.prec = max(500, len(value.as_tuple().digits) + abs(int(places)) + 10)
                    return value.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)
            if kind == 2 and token == "SUM":
                self.take("(")
                first_kind, first = self.take()
                if first_kind != 0:
                    raise ValueError("SUM 仅支持同工作表单格或矩形引用")
                last = first
                if self.peek(":"):
                    self.take(":")
                    last_kind, last = self.take()
                    if last_kind != 0:
                        raise ValueError("SUM 区域结束坐标无效")
                self.take(")")
                first_col, first_row, last_col, last_row = _range(first, last)
                total = Decimal(0)
                for row in range(first_row, last_row + 1):
                    for col in range(first_col, last_col + 1):
                        value = self.lookup(f"{_col_name(col)}{row}")
                        if isinstance(value, Decimal):
                            total = _operate(total, "+", value)
                return total
            raise ValueError(f"不支持公式函数或 token: {token}")
        finally:
            self.depth -= 1


def _string_text(element) -> str:
    if element is None:
        return ""
    pieces = []
    for child in element:
        if child.tag == f"{{{MAIN}}}t":
            pieces.append(child.text or "")
        elif child.tag == f"{{{MAIN}}}r":
            pieces.extend(node.text or "" for node in child.findall(f"{{{MAIN}}}t"))
    return "".join(pieces)


def _raw_value(cell, shared_strings: list[str]):
    kind = cell.get("t")
    value = cell.find(f"{{{MAIN}}}v")
    raw = value.text if value is not None else None
    if kind == "e":
        raise ValueError(f"输入单元格包含 Excel 错误值 {raw}")
    if kind == "s":
        if raw is None or not re.fullmatch(r"[0-9]+", raw) or int(raw) >= len(shared_strings):
            raise ValueError("共享字符串索引无效")
        return shared_strings[int(raw)]
    if kind == "inlineStr":
        return _string_text(cell.find(f"{{{MAIN}}}is"))
    if kind == "str":
        return raw or ""
    if kind not in (None, "n"):
        raise ValueError(f"不支持输入单元格类型 {kind}")
    return None if raw is None or raw == "" else _number(raw)


def _translate_shared_formula(expression: str, anchor: str, target: str) -> str:
    """Shift tokenized A1 references only; absolute axes keep their position."""
    if not expression or len(expression) > 8192 or any(symbol in expression for symbol in ("!", "[", "]")):
        raise ValueError("共享公式为空、过长或包含跨表/外部引用")
    anchor_col, anchor_row = _coordinate(anchor)
    target_col, target_row = _coordinate(target)
    col_offset, row_offset = target_col - anchor_col, target_row - anchor_row
    start = 1 if expression.startswith("=") else 0
    cursor = start
    pieces = [expression[:start]]
    while cursor < len(expression):
        if not expression[cursor:].strip():
            pieces.append(expression[cursor:])
            break
        token = _TOKEN.match(expression, cursor)
        if token is None:
            raise ValueError("共享公式含不支持的语法")
        reference = token[1]
        if reference is None:
            pieces.append(expression[cursor:token.end()])
        else:
            axes = re.fullmatch(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)", reference)
            col, row = _coordinate(reference.replace("$", "").upper())
            if not axes[1]:
                col += col_offset
            if not axes[3]:
                row += row_offset
            if col < 1 or col > 16384 or row < 1 or row > 1048576:
                raise ValueError("共享公式平移后坐标越界")
            replacement = axes[1] + _col_name(col) + axes[3] + str(row)
            pieces.append(expression[cursor:token.start(1)] + replacement)
        cursor = token.end()
    return "".join(pieces)


def _prepare_formulas(cells: dict, sheet_name: str) -> tuple[dict[str, str], set[str]]:
    normal = {}
    shared = {}
    shared_cells = set()
    for reference, cell in cells.items():
        formula = cell.find(f"{{{MAIN}}}f")
        if formula is None:
            continue
        kind = formula.get("t")
        if kind in (None, "normal") and not formula.get("ref"):
            normal[reference] = formula.text or ""
        elif kind == "shared":
            identity = formula.get("si", "")
            if not re.fullmatch(r"[0-9]+", identity):
                raise ValueError(f"{sheet_name}!{reference}: shared 缺少合法 si")
            shared.setdefault(str(int(identity)), []).append((reference, formula))
            shared_cells.add(reference)
        else:
            raise ValueError(f"{sheet_name}!{reference}: 不支持数组/数据表公式")
    ranges = []
    for identity, group in shared.items():
        anchors = [(reference, formula) for reference, formula in group if (formula.text or "").strip()]
        if len(anchors) != 1:
            raise ValueError(f"{sheet_name}: shared {identity} 锚缺失或不唯一")
        anchor_ref, anchor = anchors[0]
        endpoints = anchor.get("ref", "").split(":")
        if len(endpoints) == 1 and endpoints[0]:
            endpoints = endpoints * 2
        if len(endpoints) != 2:
            raise ValueError(f"{sheet_name}!{anchor_ref}: shared 锚缺少矩形 ref")
        bounds = _range(*endpoints)
        first_col, first_row, last_col, last_row = bounds
        for old_bounds in ranges:
            old_col, old_row, old_last_col, old_last_row = old_bounds
            if max(first_col, old_col) <= min(last_col, old_last_col) and max(first_row, old_row) <= min(last_row, old_last_row):
                raise ValueError(f"{sheet_name}: shared 公式区域重叠")
        ranges.append(bounds)
        expected = {f"{_col_name(col)}{row}" for row in range(first_row, last_row + 1) for col in range(first_col, last_col + 1)}
        group_references = {reference for reference, _ in group}
        if group_references != expected:
            raise ValueError(f"{sheet_name}: shared 成员缺失或超出锚区域")
        for reference, formula in group:
            if reference != anchor_ref and formula.get("ref"):
                raise ValueError(f"{sheet_name}!{reference}: shared 从属格不能声明新区域")
            normal[reference] = _translate_shared_formula(anchor.text or "", anchor_ref, reference)
    return normal, shared_cells


def _cache_patch(content: bytes, span: dict, value) -> bytes:
    """Replace the cache only; retain the original f element byte for byte."""
    opening = content[span["start"]:span["open_end"]]
    qualified = re.match(br"<([A-Za-z_][A-Za-z0-9_.:-]*)", opening)
    if qualified is None:
        raise ValueError("公式单元格标签无效")
    prefix = qualified[1][:-1]
    opening = re.sub(br"\s+t\s*=\s*([\"']).*?\1", b"", opening, flags=re.S)
    if isinstance(value, str):
        opening = opening[:-1] + b' t="str">'
        encoded = escape(value).replace("\r", "&#13;").encode("utf-8")
    else:
        encoded = str(value).encode("ascii")
    new_cache = b"<" + prefix + b"v>" + encoded + b"</" + prefix + b"v>"
    formula = next((child for child in span["children"] if child["name"] == "f"), None)
    if formula is None:
        raise ValueError("公式 XML 定位失败")
    body_end = content.rfind(b"</", span["open_end"], span["end"])
    events = [(child["start"], child["end"], b"") for child in span["children"] if child["name"] in ("v", "is")]
    events.append((formula["end"], formula["end"], new_cache))
    cursor = span["open_end"]
    body = []
    for start, end, replacement in sorted(events):
        body.append(content[cursor:start])
        body.append(replacement)
        cursor = end
    body.append(content[cursor:body_end])
    return opening + b"".join(body) + content[body_end:span["end"]]


def recalculate_supported_formulas(content: bytes) -> tuple[bytes, dict]:
    """Recalculate supported formulas from current inputs and update caches.

    Supports same-sheet A1/$A$1 references (including direct text references),
    + - * / and parentheses, SUM of one rectangular range or a single cell,
    and ROUND(expression, integer_places) with Decimal ROUND_HALF_UP.
    SUM ignores referenced text/blank cells; arithmetic rejects text operands.
    Division uses 50 significant digits, ROUND_HALF_EVEN. Other arithmetic is
    exact within the explicit resource limits. Unknown functions, external or
    cross-sheet references, arrays, invalid shared groups, cycles and input errors
    fail atomically. A shared group with one supported anchor is calculated by
    shifting relative axes; its original shared XML is retained unchanged.
    This primitive provides no business/template acceptance.
    """
    infos, parts, comment = _package(content)
    shared_strings = []
    if "xl/sharedStrings.xml" in parts:
        shared_root = _xml(parts["xl/sharedStrings.xml"])
        shared_strings = [_string_text(node) for node in shared_root.findall(f"{{{MAIN}}}si")]
    replacements = {}
    values_report = {}
    records = []
    for sheet in _sheet_parts(parts):
        part, name = sheet["part"], sheet["name"]
        original = parts[part]
        cells = _cells(_xml(original))
        formulas, shared_cells = _prepare_formulas(cells, name)
        inputs = {}
        for reference, cell in cells.items():
            formula = cell.find(f"{{{MAIN}}}f")
            if formula is None:
                try:
                    inputs[reference] = _raw_value(cell, shared_strings)
                except ValueError as error:
                    raise ValueError(f"{name}!{reference}: {error}") from None
        computed = {}
        visiting = []

        def lookup(reference):
            if reference in computed:
                return computed[reference]
            if reference not in formulas:
                return inputs.get(reference)
            if reference in visiting:
                raise ValueError("循环公式依赖: " + " -> ".join(visiting + [reference]))
            if len(visiting) >= 100:
                raise ValueError("公式依赖层级超限")
            visiting.append(reference)
            try:
                value = _Expression(formulas[reference], lookup).evaluate()
            except (ValueError, RecursionError) as error:
                raise ValueError(f"{name}!{reference}: {error}") from None
            finally:
                visiting.pop()
            computed[reference] = value
            return value

        for reference in formulas:
            lookup(reference)
        if not computed:
            continue
        spans = _cell_spans(original)
        patches = []
        values_report[name] = {}
        for reference in formulas:
            value = computed[reference]
            span = spans[reference]
            patches.append((span["start"], span["end"], _cache_patch(original, span, value)))
            values_report[name][reference] = str(value)
            records.append({"sheet": name, "cell": reference, "formula": formulas[reference],
                            "shared_formula": reference in shared_cells,
                            "value": str(value), "type": "text" if isinstance(value, str) else "number"})
        changed = original
        for start, end, replacement in sorted(patches, reverse=True):
            changed = changed[:start] + replacement + changed[end:]
        _cells(_xml(changed))
        replacements[part] = changed
    report = {
        "status": "recalculated_within_supported_scope", "formula_count": len(records),
        "calculated_values": values_report, "calculated_cells": records,
        "scope": {"engine": "limited_decimal_template_calculator", "complete_excel_engine": False,
                  "operators": ["+", "-", "*", "/", "parentheses"],
                  "functions": ["SUM(single_cell_or_rectangle)", "ROUND(expression,integer_places)"], "same_sheet_references_only": True,
                  "shared_formulas": "unique_anchor_supported_expression_relative_axes_only",
                  "sum_ignores_text_and_blank": True, "division_precision": DIVISION_PRECISION,
                  "division_rounding": "ROUND_HALF_EVEN", "max_range_cells": MAX_RANGE_CELLS},
        "business_validation_passed": False,
    }
    if not replacements:
        return content, report
    output = BytesIO()
    with ZipFile(output, "w") as package:
        package.comment = comment
        for info in infos:
            package.writestr(info, replacements.get(info.filename, parts[info.filename]))
    return output.getvalue(), report
