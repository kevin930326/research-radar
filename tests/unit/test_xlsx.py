"""`radar/domain/xlsx.py`：最小的 xlsx 讀寫（Story 2.13、ADR-0045）。

寫→讀來回、讀 Excel 的存法、資料驗證的元素與元素順序、同輸入同位元組、截斷與控制字元，以及
各種不信任的檔（不是 zip、太大、zip 炸彈、DOCTYPE／ENTITY、超過列數或欄數）。
"""

import io
import re
import tracemalloc
import zipfile
from decimal import Decimal
from xml.etree import ElementTree

import pytest

from radar.domain import xlsx
from radar.domain.xlsx import (
    MAX_CELL_CHARS,
    ColumnValidation,
    ListRule,
    NumberRule,
    RangeListRule,
    Sheet,
    XlsxError,
    clean_cell_text,
    column_letter,
    read_workbook,
    write_workbook,
    zip_members,
)
from tests.annotation_support import RICH_TITLE, build_excel_style_workbook

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _sample() -> list[Sheet]:
    return [
        Sheet(
            name="論文",
            rows=[["id", "n", "note"], ["p1", 3, "a & <b>"], ["p2", Decimal("0.25"), None]],
            widths=[20.0, 8.0],
            wrap_columns=frozenset({2}),
            validations=[
                ColumnValidation(1, 2, 3, NumberRule("whole", 1, 10)),
                ColumnValidation(2, 2, 3, ListRule(("yes", "no"))),
            ],
        ),
        Sheet(
            name="構面",
            rows=[["paper_id", "x"]],
            validations=[
                ColumnValidation(0, 2, 201, RangeListRule("論文", 0, 2, 3)),
                ColumnValidation(1, 2, 201, NumberRule("decimal", Decimal(0), Decimal(1))),
            ],
        ),
    ]


def test_round_trip_keeps_values_and_sheet_order() -> None:
    output = write_workbook(_sample())

    sheets = read_workbook(output.data)

    assert list(sheets) == ["論文", "構面"]
    assert sheets["論文"] == (
        ("id", "n", "note"),
        ("p1", Decimal(3), "a & <b>"),
        ("p2", Decimal("0.25"), None),
    )
    assert sheets["構面"] == (("paper_id", "x"),)
    assert output.truncated_cells == 0


def test_same_input_gives_same_bytes() -> None:
    assert write_workbook(_sample()).data == write_workbook(_sample()).data


def test_zip_members_have_fixed_order_and_timestamp() -> None:
    data = write_workbook(_sample()).data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
    assert [info.filename for info in infos] == [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
    ]
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}


def test_sheet_xml_element_order_and_validations() -> None:
    members = _members(write_workbook(_sample()).data)
    root = ElementTree.fromstring(members["xl/worksheets/sheet1.xml"])

    assert [child.tag.removeprefix(MAIN) for child in root] == [
        "sheetViews",
        "cols",
        "sheetData",
        "dataValidations",
    ]
    pane = root.find(f"{MAIN}sheetViews/{MAIN}sheetView/{MAIN}pane")
    assert pane is not None
    assert pane.get("state") == "frozen"
    assert pane.get("ySplit") == "1"
    validations = root.findall(f"{MAIN}dataValidations/{MAIN}dataValidation")
    assert [
        (item.get("type"), item.get("sqref"), item.get("errorStyle")) for item in validations
    ] == [
        ("whole", "B2:B3", "stop"),
        ("list", "C2:C3", "stop"),
    ]
    assert validations[0].get("operator") == "between"
    assert [item.text for item in validations[0]] == ["1", "10"]
    assert validations[1].findtext(f"{MAIN}formula1") == '"yes,no"'
    header_cell = root.find(f"{MAIN}sheetData/{MAIN}row/{MAIN}c")
    assert header_cell is not None
    assert header_cell.get("t") == "inlineStr"
    assert header_cell.get("s") == "1"  # 粗體表頭

    second = ElementTree.fromstring(members["xl/worksheets/sheet2.xml"])
    formulas = [item.findtext(f"{MAIN}formula1") for item in second.iter(f"{MAIN}dataValidation")]
    assert formulas == ["'論文'!$A$2:$A$3", "0"]


def test_strings_are_inline_with_preserved_space() -> None:
    members = _members(write_workbook([Sheet(name="s", rows=[["  two  spaces "]])]).data)
    text = members["xl/worksheets/sheet1.xml"].decode()
    assert '<t xml:space="preserve">  two  spaces </t>' in text
    assert read_workbook(write_workbook([Sheet(name="s", rows=[[" x "]])]).data)["s"] == ((" x ",),)


def test_number_rule_without_bounds_and_with_one_bound() -> None:
    sheet = Sheet(
        name="s",
        rows=[["a", "b", "c"]],
        validations=[
            ColumnValidation(0, 2, 5, NumberRule("decimal")),
            ColumnValidation(1, 2, 5, NumberRule("whole", minimum=1)),
            ColumnValidation(2, 2, 5, NumberRule("decimal", maximum=Decimal(1))),
        ],
    )
    root = ElementTree.fromstring(
        _members(write_workbook([sheet]).data)["xl/worksheets/sheet1.xml"]
    )
    operators = [item.get("operator") for item in root.iter(f"{MAIN}dataValidation")]
    assert operators == ["between", "greaterThanOrEqual", "lessThanOrEqual"]
    formulas = [
        element.text or ""
        for item in root.iter(f"{MAIN}dataValidation")
        for element in item
        if element.tag.startswith(f"{MAIN}formula")
    ]
    assert formulas[:2] == ["-1000000000000000", "1000000000000000"]
    assert all(len(text) <= 255 for text in formulas)


def test_list_rule_over_255_characters_is_an_error() -> None:
    items = tuple(f"key_{index:03d}" for index in range(40))  # 40 × 8 + 39 個逗號 > 255
    sheet = Sheet(name="s", rows=[["a"]], validations=[ColumnValidation(0, 2, 3, ListRule(items))])
    with pytest.raises(XlsxError, match="255"):
        write_workbook([sheet])


@pytest.mark.parametrize("item", ["a,b", 'a"b', ""])
def test_list_rule_rejects_bad_items(item: str) -> None:
    sheet = Sheet(
        name="s", rows=[["a"]], validations=[ColumnValidation(0, 2, 3, ListRule((item,)))]
    )
    with pytest.raises(XlsxError):
        write_workbook([sheet])


def test_range_rule_must_point_to_existing_sheet() -> None:
    sheet = Sheet(
        name="s",
        rows=[["a"]],
        validations=[ColumnValidation(0, 2, 3, RangeListRule("nope", 0, 2, 3))],
    )
    with pytest.raises(XlsxError):
        write_workbook([sheet])


def test_control_characters_are_removed() -> None:
    output = write_workbook([Sheet(name="s", rows=[["a\x00b\x07c\td\ne"]])])
    assert read_workbook(output.data)["s"] == (("abc\td\ne",),)


def test_long_cell_is_truncated_and_counted() -> None:
    long_text = "字" * (MAX_CELL_CHARS + 10)
    output = write_workbook([Sheet(name="s", rows=[["ok", long_text, long_text]])])

    assert output.truncated_cells == 2
    [[first, second, third]] = read_workbook(output.data)["s"]
    assert first == "ok"
    assert isinstance(second, str)
    assert len(second) == MAX_CELL_CHARS
    assert third == second


def test_truncation_counts_utf16_units() -> None:
    emoji = "\U0001f600"  # UTF-16 兩個單位
    text, cut = clean_cell_text(emoji * MAX_CELL_CHARS)
    assert cut
    assert len(text.encode("utf-16-le")) // 2 <= MAX_CELL_CHARS
    assert clean_cell_text("short") == ("short", False)


def test_column_letters() -> None:
    assert [column_letter(index) for index in (0, 25, 26, 51, 52, 701, 702)] == [
        "A",
        "Z",
        "AA",
        "AZ",
        "BA",
        "ZZ",
        "AAA",
    ]
    with pytest.raises(ValueError, match="欄號"):
        column_letter(-1)


@pytest.mark.parametrize("name", ["", "a/b", "x" * 32, "'quoted'"])
def test_bad_sheet_names_are_rejected(name: str) -> None:
    with pytest.raises(XlsxError):
        write_workbook([Sheet(name=name, rows=[["a"]])])


def test_bool_is_not_a_write_value() -> None:
    with pytest.raises(XlsxError):
        write_workbook([Sheet(name="s", rows=[[True]])])


# --- 讀：Excel 的存法 -----------------------------------------------------------------------


def test_reads_shared_strings_rich_text_and_positional_cells() -> None:
    data = build_excel_style_workbook(
        {
            "first": [["h1", "h2"], [RICH_TITLE, 7], [None, Decimal("0.5")]],
            "second": [["a", "b"], ["x", None, "z"]],
        },
        positional=frozenset({"second"}),
    )

    sheets = read_workbook(data)

    assert sheets["first"] == (("h1", "h2"), (RICH_TITLE, Decimal(7)), (None, Decimal("0.5")))
    # 依位置的表：沒有 r 屬性，空的格是沒有值的 <c/>。
    assert sheets["second"] == (("a", "b", None), ("x", None, "z"))


def _workbook_with_sheet(sheet_xml: str, extra: dict[str, str] | None = None) -> bytes:
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    members = [
        (
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            '<sheet name="s" sheetId="1" r:id="rId1"/></sheets></workbook>',
        ),
        (
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        ),
        (
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{sheet_xml}</sheetData></worksheet>',
        ),
        *(extra or {}).items(),
    ]
    return zip_members(members, compress=True)


def test_reads_str_bool_and_inline_types() -> None:
    data = _workbook_with_sheet(
        '<row r="1"><c r="A1" t="str"><v>formula text</v></c><c r="B1" t="b"><v>1</v></c>'
        '<c r="C1" t="str"><v>#N/A</v></c><c r="D1" t="inlineStr"><is><r><t>in</t></r>'
        '<r><t>line</t></r></is></c><c r="E1"><v>1.5E-3</v></c></row>'
        '<row r="3"><c r="B3"><v>2</v></c></row>'
    )
    assert read_workbook(data)["s"] == (
        ("formula text", True, "#N/A", "inline", Decimal("0.0015")),
        (None, None, None, None, None),
        (None, Decimal(2), None, None, None),
    )


def test_workbook_without_root_rels_uses_default_path() -> None:
    data = _workbook_with_sheet('<row><c t="inlineStr"><is><t>x</t></is></c></row>')
    assert read_workbook(data) == {"s": (("x",),)}


# --- 讀：不信任的檔 -------------------------------------------------------------------------


def test_not_a_zip() -> None:
    with pytest.raises(XlsxError, match="zip"):
        read_workbook(b"PK\x03\x04 not really a zip")
    with pytest.raises(XlsxError):
        read_workbook(b"hello")


def test_file_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    data = write_workbook(_sample()).data
    monkeypatch.setattr(xlsx, "MAX_FILE_BYTES", len(data) - 1)
    with pytest.raises(XlsxError, match="MB"):
        read_workbook(data)


def test_zip_bomb_is_rejected_without_expanding() -> None:
    # 60 MB 的 0 壓縮後只有幾十 KB；單一成員解壓上限 50 MB。
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"0" * (60 * 1024 * 1024))
    data = buffer.getvalue()
    assert len(data) < 1024 * 1024
    with pytest.raises(XlsxError, match="太大"):
        read_workbook(data)


def test_total_uncompressed_size_is_limited() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(3):
            archive.writestr(f"junk{index}.bin", b"0" * (40 * 1024 * 1024))
    with pytest.raises(XlsxError, match="太大"):
        read_workbook(buffer.getvalue())


def test_too_many_members() -> None:
    members = [(f"m{index}.xml", "<a/>") for index in range(xlsx.MAX_MEMBERS + 1)]
    with pytest.raises(XlsxError, match="個"):
        read_workbook(zip_members(members, compress=True))


@pytest.mark.parametrize(
    "prefix",
    [
        '<!DOCTYPE worksheet [<!ENTITY lol "lol">]>',
        '<!DOCTYPE worksheet SYSTEM "http://example.invalid/x.dtd">',
        '<!ENTITY x "y">',
    ],
)
def test_doctype_and_entity_are_rejected(prefix: str) -> None:
    data = write_workbook(_sample()).data
    members = _members(data)
    sheet = members["xl/worksheets/sheet1.xml"].decode()
    head, body = sheet.split("\n", 1)
    members["xl/worksheets/sheet1.xml"] = (head + "\n" + prefix + body).encode()
    rebuilt = zip_members(
        [(name, content.decode()) for name, content in members.items()], compress=True
    )
    with pytest.raises(XlsxError, match="DOCTYPE"):
        read_workbook(rebuilt)


def test_non_utf8_xml_is_rejected() -> None:
    members = _members(write_workbook(_sample()).data)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            if name == "xl/workbook.xml":
                text = content.decode().replace('encoding="UTF-8"', 'encoding="UTF-16"')
                content = text.encode("utf-16")
            archive.writestr(name, content)
    with pytest.raises(XlsxError, match="UTF-8"):
        read_workbook(buffer.getvalue())


def test_too_many_rows_or_columns() -> None:
    with pytest.raises(XlsxError, match="列"):
        read_workbook(_workbook_with_sheet(f'<row r="{xlsx.MAX_ROWS + 1}"><c><v>1</v></c></row>'))
    with pytest.raises(XlsxError, match="欄"):
        read_workbook(_workbook_with_sheet('<row r="1"><c r="GS1"><v>1</v></c></row>'))  # 第 201 欄


@pytest.mark.parametrize(
    "cell",
    [
        '<c r="A1" t="s"><v>5</v></c>',  # 沒有這個共用字串
        '<c r="A1"><v>abc</v></c>',
        '<c r="A1"><v>NaN</v></c>',
        '<c r="A1" t="b"><v>2</v></c>',
        '<c r="A1" t="zzz"><v>1</v></c>',
        '<c r="1A"><v>1</v></c>',
        '<c r="A2"><v>1</v></c>',  # 和列號對不上
    ],
)
def test_bad_cells_are_rejected(cell: str) -> None:
    with pytest.raises(XlsxError):
        read_workbook(_workbook_with_sheet(f'<row r="1">{cell}</row>'))


def test_error_messages_do_not_include_cell_content() -> None:
    secret = "SECRET-CELL-CONTENT"
    with pytest.raises(XlsxError) as caught:
        read_workbook(_workbook_with_sheet(f'<row r="1"><c r="A1"><v>{secret}</v></c></row>'))
    assert secret not in str(caught.value)
    assert re.search("看不懂", str(caught.value))


def _corrupt_deflate_workbook() -> bytes:
    """工作表那份成員的壓縮資料換成壞掉的 deflate（區塊型別 11 是保留值）。"""
    data = _workbook_with_sheet('<row r="1"><c r="A1"><v>1</v></c></row>')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("xl/worksheets/sheet1.xml")
    header = info.header_offset
    name_length = int.from_bytes(data[header + 26 : header + 28], "little")
    extra_length = int.from_bytes(data[header + 28 : header + 30], "little")
    start = header + 30 + name_length + extra_length
    return data[:start] + b"\xff" * info.compress_size + data[start + info.compress_size :]


def test_corrupt_deflate_member_is_an_xlsx_error() -> None:
    with pytest.raises(XlsxError, match="讀不出"):
        read_workbook(_corrupt_deflate_workbook())


@pytest.mark.parametrize("row_ref", ["\u00b2", "1" * 5000, "00000001"])
def test_odd_row_numbers_are_rejected(row_ref: str) -> None:
    with pytest.raises(XlsxError, match="列號"):
        read_workbook(_workbook_with_sheet(f'<row r="{row_ref}"><c><v>1</v></c></row>'))


def test_negative_shared_string_index_is_rejected() -> None:
    shared = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>first</t></si><si><t>last</t></si></sst>"
    )
    data = _workbook_with_sheet(
        '<row r="1"><c r="B1" t="s"><v>-1</v></c></row>', {"xl/sharedStrings.xml": shared}
    )
    with pytest.raises(XlsxError, match="B1 共用字串的索引不對"):
        read_workbook(data)


def test_error_value_cells_are_rejected_with_position_only() -> None:
    data = _workbook_with_sheet('<row r="5"><c r="B5" t="e"><v>#DIV/0!</v></c></row>')
    with pytest.raises(XlsxError, match="B5") as caught:
        read_workbook(data)
    assert "#DIV/0!" not in str(caught.value)


# --- Opus 審查後補的測試 ----------------------------------------------------------------------


def _dense_rows(rows: int, columns: int) -> str:
    letters = [column_letter(index) for index in range(columns)]
    return "".join(
        f'<row r="{row}">'
        + "".join(f'<c r="{letter}{row}"><v>1</v></c>' for letter in letters)
        + "</row>"
        for row in range(1, rows + 1)
    )


def test_too_many_xml_elements_is_rejected_before_parsing() -> None:
    # 2,100 列 × 100 欄：列數、欄數、位元組都在上限內（zip 不到 1 MB），但元素約 42 萬個；建成樹要
    # 約 150 MB。要在建樹之前就擋下，記憶體只多用幾 MB。
    data = _workbook_with_sheet(_dense_rows(2100, 100))
    assert len(data) < 1024 * 1024
    tracemalloc.start()
    try:
        with pytest.raises(XlsxError, match="檔案太大：XML 元素超過"):
            read_workbook(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 30 * 1024 * 1024


def test_expanded_grid_is_limited() -> None:
    # 只有兩格，但展開成 20,000 列 × 200 欄（每張表 3,200 萬個位元組的指標）。
    sheet = (
        '<row r="1"><c r="A1"><v>1</v></c></row><row r="20000"><c r="GR20000"><v>1</v></c></row>'
    )
    with pytest.raises(XlsxError, match="檔案太大：展開後的儲存格"):
        read_workbook(_workbook_with_sheet(sheet))


def test_largest_workbook_within_limits_stays_under_memory_budget() -> None:
    # 上限內最多的小儲存格（1,980 × 100，約 39.8 萬個元素）：讀得完，而且讀的過程新配置的記憶體
    # 低於 200 MB（CLI 本身約 110 MB，加起來在 api 容器的 350m 以內；ADR-0045）。
    data = _workbook_with_sheet(_dense_rows(1980, 100))
    tracemalloc.start()
    try:
        values = read_workbook(data)["s"]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (len(values), len(values[0])) == (1980, 100)
    assert peak < 200 * 1024 * 1024


@pytest.mark.parametrize(
    ("sheet", "match"),
    [
        (
            '<row r="2"><c r="A2"><v>1</v></c></row><row r="2"><c r="B2"><v>2</v></c></row>',
            "第 2 列的列號重複或順序不對",
        ),
        (
            '<row r="3"><c r="A3"><v>1</v></c></row><row r="2"><c r="A2"><v>2</v></c></row>',
            "第 2 列的列號重複或順序不對",
        ),
        ('<row r="2"><c r="A2"><v>1</v></c><c r="A2"><v>2</v></c></row>', "A2 的儲存格位置重複"),
        ('<row r="2"><c r="B2"><v>1</v></c><c r="A2"><v>2</v></c></row>', "A2 的儲存格位置重複"),
        # 沒有 r 的格接在 B2 後面是 C2；接著寫 r="B2" 就是倒退。
        ('<row r="2"><c r="B2"><v>1</v></c><c><v>2</v></c><c r="B2"><v>3</v></c></row>', "B2"),
    ],
    ids=[
        "same-row-twice",
        "row-goes-back",
        "same-cell-twice",
        "cell-goes-back",
        "after-positional",
    ],
)
def test_duplicate_or_backward_rows_and_cells_are_rejected(sheet: str, match: str) -> None:
    with pytest.raises(XlsxError, match=match):
        read_workbook(_workbook_with_sheet(sheet))


def test_ooxml_escapes_are_decoded_when_reading() -> None:
    shared = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>line1_x000D_\nline2</t></si>"  # Excel 存歸位字元的寫法
        "<si><t>a_x005F_x0041_b</t></si>"  # 字面上的 _x0041_
        "<si><r><t>sm_xD83D_</t></r><r><t>_xDE00_ile</t></r></si>"  # 成對的代理字元
        "<si><t>nul_x0000_end_xZZZZ_</t></si></sst>"  # 控制字元拿掉；不是十六進位的照留
    )
    data = _workbook_with_sheet(
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c>'
        '<c r="E1" t="inlineStr"><is><t>tab_x0009_end</t></is></c>'
        '<c r="F1" t="str"><v>x_x0041_y</v></c></row>',
        {"xl/sharedStrings.xml": shared},
    )
    assert read_workbook(data)["s"] == (
        ("line1\r\nline2", "a_x0041_b", "sm\U0001f600ile", "nulend_xZZZZ_", "tab\tend", "xAy"),
    )


def test_literal_escape_text_is_escaped_when_writing() -> None:
    texts = ["title _x0041_ end", "_x0041_x0042_", "keep_x12_ and _X0041_"]
    data = write_workbook([Sheet(name="s", rows=[texts])]).data
    sheet = _members(data)["xl/worksheets/sheet1.xml"].decode()
    assert "title _x005F_x0041_ end" in sheet
    assert "_x005F_x0041_x005F_x0042_" in sheet
    assert "keep_x12_ and _X0041_" in sheet  # 不是 _xHHHH_ 的寫法不動
    assert read_workbook(data)["s"] == (tuple(texts),)


def test_list_rule_error_says_how_long() -> None:
    items = tuple(f"item_{index:02d}" for index in range(40))
    with pytest.raises(XlsxError, match=f"合起來 {len(','.join(items))} 個字元，超過"):
        write_workbook(
            [
                Sheet(
                    name="s", rows=[["a"]], validations=[ColumnValidation(0, 2, 3, ListRule(items))]
                )
            ]
        )
