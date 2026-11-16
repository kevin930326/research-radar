"""最小的 xlsx 讀寫（Story 2.13、ADR-0045）：只用標準函式庫（`zipfile`＋`xml.etree`），bytes 進出、
不碰檔案系統。

xlsx 是一個 zip，裡面是幾份 XML。這裡只做標註試算表需要的部分：

- **寫**（`write_workbook`）：每張表的儲存格（字串用 `t="inlineStr"`、數字用 `<v>`）、凍結第一列、
  欄寬、粗體表頭、自動換行，以及三種資料驗證：清單（`"a,b,c"`，合起來最多 255 字元）、清單來源是
  儲存格範圍（`'論文'!$A$2:$A$N`）、整數／小數範圍；一律 `errorStyle="stop"`（不在範圍內的值
  打不進去）。
  zip 成員的順序與時間戳固定，同樣的輸入寫出同樣的位元組。XML 不允許的控制字元拿掉；超過
  `MAX_CELL_CHARS`（Excel 一格的上限）的字串截斷，回報截斷了幾格。
- **讀**（`read_workbook`）：依 `workbook.xml` 與它的 rels 找工作表（Target 可相對可絕對），
  讀 Excel、LibreOffice、Google 試算表常見的存法：`sharedStrings.xml`（含 rich text）、`inlineStr`、
  `str`、`n`、`b`（`e` 錯誤值拒收）；沒有 `r` 屬性的列與格依位置。回傳每張表的二維值
  （`str | Decimal | bool | None`）。

讀的時候不信任檔案：檔案大小、成員數、每個成員與合計的解壓大小、每張表的列數與欄數都有上限；
任何 XML 含 `<!DOCTYPE` 或 `<!ENTITY` 直接拒收（不展開實體）；不是 UTF-8 的 XML 也拒收。錯了丟
`XlsxError`，訊息不帶儲存格內容。
"""

import io
import posixpath
import re
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

# Excel 一格最多 32,767 個字元（以 UTF-16 單位算）。
MAX_CELL_CHARS: Final = 32_767
# 清單驗證寫成 `"a,b,c"` 時，引號裡最多 255 個字元（Excel 的限制）。
MAX_LIST_CHARS: Final = 255

# 讀取的上限。
MAX_FILE_BYTES: Final = 20 * 1024 * 1024
MAX_MEMBERS: Final = 200
MAX_MEMBER_BYTES: Final = 50 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 100 * 1024 * 1024
MAX_ROWS: Final = 20_000
MAX_COLUMNS: Final = 200

_MAIN_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_DOCUMENT: Final = _REL_NS + "/officeDocument"
_WORKSHEET: Final = _REL_NS + "/worksheet"
_STYLES: Final = _REL_NS + "/styles"
# zip 成員固定的時間戳（zip 能表示的最早時間）。
_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
# 樣式索引（`xl/styles.xml` 的 cellXfs）。
_STYLE_HEADER: Final = 1
_STYLE_WRAP: Final = 2
# XML 1.0 不允許的字元（Tab、換行、歸位以外的 C0 控制字元、代理字元、U+FFFE／U+FFFF）。
_XML_INVALID: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_CELL_REF: Final = re.compile(r"([A-Z]{1,3})([0-9]{1,7})")
_ENCODING_DECL: Final = re.compile(rb"""^\s*<\?xml[^>]*encoding\s*=\s*["']([^"']+)["']""")
_FORBIDDEN_XML: Final = ("<!DOCTYPE", "<!ENTITY")
# 沒有上下限的小數驗證用的邊界（寫成純數字，遠短於 Excel 公式 255 字元的限制）。
_UNBOUNDED_DECIMAL: Final = Decimal(1_000_000_000_000_000)
# 列號最多 7 位數（`MAX_ROWS` 遠小於這個）。
_MAX_ROW_DIGITS: Final = 7
_SHEET_NAME_FORBIDDEN: Final = re.compile(r"[\\/?*\[\]:]")

type CellValue = str | Decimal | bool | None
type WriteValue = str | int | Decimal | None
type SheetValues = tuple[tuple[CellValue, ...], ...]


class XlsxError(ValueError):
    """寫不出或讀不懂的 xlsx（訊息不帶儲存格內容）。"""


# --- 資料驗證 -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListRule:
    """下拉清單：只能選 `items` 其中一個。"""

    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RangeListRule:
    """下拉清單，選項是另一張表某一欄的儲存格（`'表'!$欄$first:$欄$last`）。`column` 從 0 起算，
    `first_row`／`last_row` 是 Excel 的列號（從 1 起算）。"""

    sheet: str
    column: int
    first_row: int
    last_row: int


@dataclass(frozen=True, slots=True)
class NumberRule:
    """整數（`whole`）或小數（`decimal`）；`minimum`／`maximum` 是 None 表示那一邊沒有限制。"""

    kind: Literal["whole", "decimal"]
    minimum: int | Decimal | None = None
    maximum: int | Decimal | None = None


type ValidationRule = ListRule | RangeListRule | NumberRule


@dataclass(frozen=True, slots=True)
class ColumnValidation:
    """一欄的資料驗證：`column` 從 0 起算，套用在 Excel 第 `first_row`～`last_row` 列。"""

    column: int
    first_row: int
    last_row: int
    rule: ValidationRule


@dataclass(frozen=True)
class Sheet:
    """要寫的一張表：`rows` 的第一列是表頭（粗體、凍結）；`widths` 是每欄的欄寬（沒給的用預設），
    `wrap_columns` 是要自動換行的欄（從 0 起算）。"""

    name: str
    rows: Sequence[Sequence[WriteValue]]
    widths: Sequence[float] = ()
    wrap_columns: frozenset[int] = field(default_factory=frozenset)
    validations: Sequence[ColumnValidation] = ()


@dataclass(frozen=True)
class XlsxOutput:
    """`write_workbook` 的結果：檔案位元組與截斷了幾格。"""

    data: bytes
    truncated_cells: int


def column_letter(index: int) -> str:
    """欄的字母（0 → `A`、25 → `Z`、26 → `AA`）。

    Raises:
        ValueError: `index` 是負數。
    """
    if type(index) is not int or index < 0:
        raise ValueError("欄號要是 0 以上的整數")
    letters = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clean_cell_text(text: str) -> tuple[str, bool]:
    """寫進儲存格前的字串：拿掉 XML 不允許的控制字元；超過 `MAX_CELL_CHARS` 時截斷。回傳
    （字串, 有沒有截斷）。"""
    cleaned = _XML_INVALID.sub("", text)
    if _utf16_units(cleaned) <= MAX_CELL_CHARS:
        return cleaned, False
    units = 0
    for index, char in enumerate(cleaned):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > MAX_CELL_CHARS:
            return cleaned[:index], True
    return cleaned, False  # pragma: no cover - 上面一定會在某個字元超過


# --- 寫 -----------------------------------------------------------------------------------


def _check_sheet_name(name: str) -> None:
    if not name or len(name) > 31 or _SHEET_NAME_FORBIDDEN.search(name) or name != name.strip("'"):
        raise XlsxError("工作表名稱要是 1～31 個字元，不能有 \\ / ? * [ ] : 也不能以 ' 開頭或結尾")


def _quoted_sheet(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _number_text(value: int | Decimal) -> str:
    if isinstance(value, bool):
        raise XlsxError("數字欄不收 bool")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise XlsxError("數字要是有限的值")
        return format(value, "f")
    return str(value)


def _formula_list(items: tuple[str, ...]) -> str:
    if not items:
        raise XlsxError("下拉清單至少要有一個選項")
    for item in items:
        if not item or "," in item or '"' in item or _XML_INVALID.search(item):
            raise XlsxError("下拉清單的選項不能是空的，也不能有逗號、雙引號或控制字元")
    joined = ",".join(items)
    if len(joined) > MAX_LIST_CHARS:
        raise XlsxError(f"下拉清單的選項合起來超過 {MAX_LIST_CHARS} 個字元")
    return f'"{joined}"'


def _validation_xml(validation: ColumnValidation, sheet_names: frozenset[str]) -> str:
    if validation.first_row < 1 or validation.last_row < validation.first_row:
        raise XlsxError("資料驗證的列範圍不對")
    if validation.last_row > MAX_ROWS + 1:
        raise XlsxError("資料驗證的列範圍太大")
    letter = column_letter(validation.column)
    sqref = f"{letter}{validation.first_row}:{letter}{validation.last_row}"
    common = f'allowBlank="1" showErrorMessage="1" errorStyle="stop" sqref="{sqref}"'
    rule = validation.rule
    match rule:
        case ListRule(items=items):
            formula = escape(_formula_list(items))
            return (
                f'<dataValidation type="list" {common}><formula1>{formula}</formula1>'
                "</dataValidation>"
            )
        case RangeListRule(sheet=sheet, column=column, first_row=first, last_row=last):
            if sheet not in sheet_names:
                raise XlsxError("清單來源指到不存在的工作表")
            if first < 1 or last < first:
                raise XlsxError("清單來源的列範圍不對")
            source_letter = column_letter(column)
            reference = f"{_quoted_sheet(sheet)}!${source_letter}${first}:${source_letter}${last}"
            return (
                f'<dataValidation type="list" {common}>'
                f"<formula1>{escape(reference)}</formula1></dataValidation>"
            )
        case NumberRule(kind=kind, minimum=minimum, maximum=maximum):
            if kind not in ("whole", "decimal"):
                raise XlsxError("數字驗證只有 whole 與 decimal")
            formulas: tuple[int | Decimal, ...]
            if minimum is not None and maximum is not None:
                operator = "between"
                formulas = (minimum, maximum)
            elif minimum is not None:
                operator = "greaterThanOrEqual"
                formulas = (minimum,)
            elif maximum is not None:
                operator = "lessThanOrEqual"
                formulas = (maximum,)
            else:
                # 沒有上下限：只要求是數字（Excel 的 decimal 驗證一定要有範圍；公式最多 255 字元，
                # 所以用短的邊界）。
                operator = "between"
                formulas = (-_UNBOUNDED_DECIMAL, _UNBOUNDED_DECIMAL)
            body = "".join(
                f"<formula{index}>{_number_text(value)}</formula{index}>"
                for index, value in enumerate(formulas, start=1)
            )
            return (
                f'<dataValidation type="{kind}" operator="{operator}" {common}>{body}'
                "</dataValidation>"
            )


def _cell_xml(reference: str, value: WriteValue, style: int) -> tuple[str, bool]:
    style_attr = f' s="{style}"' if style else ""
    if value is None:
        return (f'<c r="{reference}"{style_attr}/>' if style else ""), False
    if isinstance(value, str):
        text, truncated = clean_cell_text(value)
        return (
            f'<c r="{reference}"{style_attr} t="inlineStr"><is>'
            f'<t xml:space="preserve">{escape(text)}</t></is></c>'
        ), truncated
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style_attr}><v>{_number_text(value)}</v></c>', False
    raise XlsxError("儲存格只收 str、int、Decimal 或 None")


def _sheet_xml(sheet: Sheet, sheet_names: frozenset[str]) -> tuple[str, int]:
    if len(sheet.rows) > MAX_ROWS:
        raise XlsxError(f"一張表最多 {MAX_ROWS} 列")
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        f'<worksheet xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">',
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        "</sheetView></sheetViews>",
    ]
    width_count = max(len(sheet.widths), max(sheet.wrap_columns, default=-1) + 1)
    if width_count:
        parts.append("<cols>")
        for index in range(width_count):
            width = sheet.widths[index] if index < len(sheet.widths) else 12.0
            style = f' style="{_STYLE_WRAP}"' if index in sheet.wrap_columns else ""
            parts.append(
                f'<col min="{index + 1}" max="{index + 1}" width="{width:g}"'
                f' customWidth="1"{style}/>'
            )
        parts.append("</cols>")
    parts.append("<sheetData>")
    truncated = 0
    for row_index, row in enumerate(sheet.rows, start=1):
        if len(row) > MAX_COLUMNS:
            raise XlsxError(f"一張表最多 {MAX_COLUMNS} 欄")
        cells: list[str] = []
        for column_index, value in enumerate(row):
            if row_index == 1:
                cell_style = _STYLE_HEADER
            elif column_index in sheet.wrap_columns:
                cell_style = _STYLE_WRAP
            else:
                cell_style = 0
            cell, cut = _cell_xml(f"{column_letter(column_index)}{row_index}", value, cell_style)
            truncated += cut
            cells.append(cell)
        parts.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    parts.append("</sheetData>")
    if sheet.validations:
        parts.append(f'<dataValidations count="{len(sheet.validations)}">')
        parts.extend(_validation_xml(item, sheet_names) for item in sheet.validations)
        parts.append("</dataValidations>")
    parts.append("</worksheet>")
    return "".join(parts), truncated


_STYLES_XML: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<styleSheet xmlns="{_MAIN_NS}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment vertical="top" wrapText="1"/></xf>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def write_workbook(sheets: Sequence[Sheet]) -> XlsxOutput:
    """把 `sheets` 寫成 xlsx 的位元組（同樣的輸入 → 同樣的位元組）。

    Raises:
        XlsxError: 沒有工作表、名稱不合法或重複、列或欄太多、值的型別不對、資料驗證寫不出來
            （例如清單選項合起來超過 255 字元、範圍指到不存在的表）。
    """
    if not sheets:
        raise XlsxError("至少要有一張工作表")
    names = [sheet.name for sheet in sheets]
    for name in names:
        _check_sheet_name(name)
    if len({name.casefold() for name in names}) != len(names):
        raise XlsxError("工作表名稱重複")
    sheet_names = frozenset(names)
    count = len(sheets)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, count + 1)
        )
        + '<Override PartName="/xl/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_DOCUMENT}" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets>'
        + "".join(
            f'<sheet name={quoteattr(name)} sheetId="{index}" r:id="rId{index}"/>'
            for index, name in enumerate(names, start=1)
        )
        + "</sheets></workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        + "".join(
            f'<Relationship Id="rId{index}" Type="{_WORKSHEET}"'
            f' Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, count + 1)
        )
        + f'<Relationship Id="rId{count + 1}" Type="{_STYLES}" Target="styles.xml"/>'
        "</Relationships>"
    )
    members: list[tuple[str, str]] = [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", root_rels),
        ("xl/workbook.xml", workbook),
        ("xl/_rels/workbook.xml.rels", workbook_rels),
        ("xl/styles.xml", _STYLES_XML),
    ]
    truncated = 0
    for index, sheet in enumerate(sheets, start=1):
        xml, cut = _sheet_xml(sheet, sheet_names)
        truncated += cut
        members.append((f"xl/worksheets/sheet{index}.xml", xml))
    return XlsxOutput(zip_members(members, compress=True), truncated)


def zip_members(members: Sequence[tuple[str, str]], *, compress: bool) -> bytes:
    """依給的順序把（名稱, 內容）寫成 zip；時間戳、權限、建立系統都固定（同輸入 → 同位元組）。"""
    buffer = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = method
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()


# --- 讀 -----------------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(element: ElementTree.Element, name: str) -> str | None:
    """不管命名空間的屬性（`r:id` 在 transitional 與 strict 的命名空間不同）。"""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


class _Archive:
    """讀 zip 成員，守住解壓大小的上限。"""

    def __init__(self, data: bytes) -> None:
        if len(data) > MAX_FILE_BYTES:
            raise XlsxError(f"檔案超過 {MAX_FILE_BYTES // (1024 * 1024)} MB")
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self._zip.infolist()
        except zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError:
            raise XlsxError("不是 xlsx（讀不懂的 zip）") from None
        if len(infos) > MAX_MEMBERS:
            raise XlsxError(f"zip 裡的檔案超過 {MAX_MEMBERS} 個")
        self._infos = {info.filename: info for info in infos}
        if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
            raise XlsxError("zip 解壓後太大")
        self._read_total = 0

    def has(self, name: str) -> bool:
        return name in self._infos

    def read_xml(self, name: str) -> ElementTree.Element:
        info = self._infos.get(name)
        if info is None:
            raise XlsxError(f"xlsx 裡缺少 {name}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise XlsxError("zip 裡有檔案解壓後太大")
        try:
            with self._zip.open(info) as member:
                content = member.read(MAX_MEMBER_BYTES + 1)
        except (
            zipfile.BadZipFile,
            zlib.error,
            OSError,
            ValueError,
            EOFError,
            NotImplementedError,
        ):
            raise XlsxError(f"讀不出 {name}") from None
        except RuntimeError:  # 加密的成員
            raise XlsxError(f"讀不出 {name}（加密）") from None
        if len(content) > MAX_MEMBER_BYTES:
            raise XlsxError("zip 裡有檔案解壓後太大")
        self._read_total += len(content)
        if self._read_total > MAX_TOTAL_BYTES:
            raise XlsxError("zip 解壓後太大")
        return _parse_xml(content, name)


def _parse_xml(content: bytes, name: str) -> ElementTree.Element:
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    declared = _ENCODING_DECL.match(content)
    if declared is not None and declared.group(1).decode("ascii", "replace").lower() not in (
        "utf-8",
        "utf8",
    ):
        raise XlsxError(f"{name} 不是 UTF-8")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise XlsxError(f"{name} 不是 UTF-8") from None
    if any(marker in text for marker in _FORBIDDEN_XML):
        raise XlsxError(f"{name} 含 DOCTYPE 或 ENTITY（不接受）")
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise XlsxError(f"{name} 不是合法的 XML") from None


def _relationships(archive: _Archive, path: str) -> dict[str, tuple[str, str]]:
    """rels 檔：Id → (Type, 解析後的成員路徑)。`path` 是被描述的那份成員。"""
    directory, base = posixpath.split(path)
    rels_path = posixpath.join(directory, "_rels", base + ".rels")
    if not archive.has(rels_path):
        return {}
    found: dict[str, tuple[str, str]] = {}
    for element in archive.read_xml(rels_path):
        if _local(element.tag) != "Relationship":
            continue
        rel_id = element.get("Id")
        target = element.get("Target")
        if rel_id is None or target is None or element.get("TargetMode") == "External":
            continue
        if target.startswith("/"):
            resolved = posixpath.normpath(target.lstrip("/"))
        else:
            resolved = posixpath.normpath(posixpath.join(directory, target))
        found[rel_id] = (element.get("Type", ""), resolved)
    return found


def _text_of(element: ElementTree.Element) -> str:
    """`<si>`／`<is>` 的文字：直接的 `<t>` 與 rich text 的 `<r><t>`（不含注音 `<rPh>`）。"""
    parts: list[str] = []
    for child in element:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _shared_strings(archive: _Archive, path: str | None) -> list[str]:
    if path is None or not archive.has(path):
        return []
    return [_text_of(item) for item in archive.read_xml(path) if _local(item.tag) == "si"]


def _decimal(text: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise XlsxError("數字儲存格的值看不懂") from None
    if not value.is_finite():
        raise XlsxError("數字儲存格的值不是有限的數")
    return value


def _cell_value(cell: ElementTree.Element, shared: list[str], where: str) -> CellValue:
    """一格的值；`where` 是儲存格位置（例如 `B5`），只用在錯誤訊息（不帶內容）。"""
    kind = cell.get("t", "n")
    value_element = next((child for child in cell if _local(child.tag) == "v"), None)
    if kind == "inlineStr":
        inline = next((child for child in cell if _local(child.tag) == "is"), None)
        return None if inline is None else _text_of(inline)
    if value_element is None or value_element.text is None:
        return None
    raw = value_element.text
    match kind:
        case "s":
            try:
                index = int(raw)
                if index < 0:
                    raise IndexError(index)
                return shared[index]
            except ValueError, IndexError:
                raise XlsxError(f"{where} 共用字串的索引不對") from None
        case "e":
            raise XlsxError(f"{where} 是錯誤值（例如 #N/A、#REF!），請改成實際的值")
        case "str" | "d":
            return raw
        case "b":
            if raw.strip() not in ("0", "1"):
                raise XlsxError("布林儲存格的值看不懂")
            return raw.strip() == "1"
        case "n":
            return _decimal(raw)
        case _:
            raise XlsxError("不認得的儲存格型別")


def _column_index(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number - 1


def _sheet_values(root: ElementTree.Element, shared: list[str]) -> SheetValues:
    data = next((child for child in root if _local(child.tag) == "sheetData"), None)
    if data is None:
        return ()
    cells: dict[int, dict[int, CellValue]] = {}
    next_row = 1
    width = 0
    for row in data:
        if _local(row.tag) != "row":
            continue
        row_ref = row.get("r")
        if row_ref is None:
            row_number = next_row
        else:
            if not (row_ref.isascii() and row_ref.isdigit() and len(row_ref) <= _MAX_ROW_DIGITS):
                raise XlsxError("列號看不懂")
            row_number = int(row_ref)
        if row_number < 1 or row_number > MAX_ROWS:
            raise XlsxError(f"一張表最多 {MAX_ROWS} 列")
        next_row = row_number + 1
        values = cells.setdefault(row_number, {})
        next_column = 0
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            reference = cell.get("r")
            if reference is None:
                column = next_column
            else:
                match = _CELL_REF.fullmatch(reference)
                if match is None:
                    raise XlsxError("儲存格位置看不懂")
                if int(match.group(2)) != row_number:
                    raise XlsxError("儲存格位置和列號對不上")
                column = _column_index(match.group(1))
            if column >= MAX_COLUMNS:
                raise XlsxError(f"一張表最多 {MAX_COLUMNS} 欄")
            next_column = column + 1
            value = _cell_value(cell, shared, f"{column_letter(column)}{row_number}")
            if value is not None:
                values[column] = value
                width = max(width, column + 1)
    height = max((number for number, values in cells.items() if values), default=0)
    return tuple(
        tuple(cells.get(number, {}).get(column) for column in range(width))
        for number in range(1, height + 1)
    )


def read_workbook(data: bytes) -> Mapping[str, SheetValues]:
    """讀 xlsx：工作表名稱 → 每列的值（第 1 列在索引 0；每列一樣長，空的格是 None），照活頁簿裡
    的順序。

    Raises:
        XlsxError: 不是 xlsx、超過上限、XML 含 DOCTYPE／ENTITY 或不是 UTF-8、結構看不懂。
    """
    if not isinstance(data, bytes):
        raise XlsxError("要傳 bytes")
    archive = _Archive(data)
    workbook_path = "xl/workbook.xml"
    for rel_type, target in _relationships(archive, "").values():
        if rel_type.endswith("/officeDocument"):
            workbook_path = target
            break
    workbook = archive.read_xml(workbook_path)
    rels = _relationships(archive, workbook_path)
    shared_path = next(
        (target for rel_type, target in rels.values() if rel_type.endswith("/sharedStrings")),
        None,
    )
    if shared_path is None:
        default = posixpath.join(posixpath.dirname(workbook_path), "sharedStrings.xml")
        shared_path = default if archive.has(default) else None
    shared = _shared_strings(archive, shared_path)
    sheets_element = next((child for child in workbook if _local(child.tag) == "sheets"), None)
    if sheets_element is None:
        raise XlsxError("活頁簿裡沒有工作表")
    result: dict[str, SheetValues] = {}
    for sheet in sheets_element:
        if _local(sheet.tag) != "sheet":
            continue
        name = sheet.get("name")
        rel_id = _attr(sheet, "id")
        if name is None or rel_id is None or rel_id not in rels:
            raise XlsxError("活頁簿的工作表清單看不懂")
        rel_type, target = rels[rel_id]
        if not rel_type.endswith("/worksheet"):
            continue  # 圖表頁等不是工作表的
        if name in result:
            raise XlsxError("工作表名稱重複")
        result[name] = _sheet_values(archive.read_xml(target), shared)
    return result
