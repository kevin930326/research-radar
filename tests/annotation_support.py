"""標註試算表（Story 2.13）測試共用的小工具：測試用的小分類表與情境值清單、用 Excel 的存法手寫
XML 產生「填好的」範例試算表。

範例檔的存法照 Excel 存檔的樣子（不是 `domain/xlsx.py` 寫的樣子），解析函式才算真的讀過別人寫的
檔：字串放 `sharedStrings.xml`、儲存格 `t="s"`（其中一個是 rich text）、數字用 `<v>`、儲存格有
`r` 屬性；假設表的列與格故意沒有 `r` 屬性（依位置，空的格寫成沒有值的 `<c/>`）；活頁簿 rels 的
第一個 Target 用絕對路徑。zip 不壓縮、時間戳固定，所以同樣的內容在任何機器上產生的位元組都一樣。

論文、DOI（`10.5555/` 測試前綴）、標題、摘要、構面、假設都是自己編的。

`write_fixtures()` 把兩份範例寫到 `tests/fixtures/annotations/`（改了這裡的內容要重新產生並 commit；
單元測試會比對檔案和這裡產生的位元組）：

    uv run python -c "from tests.annotation_support import write_fixtures; write_fixtures()"
"""

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from radar.domain.annotations import (
    CONSTRUCTS_SHEET,
    GUIDE_SHEET,
    HYPOTHESES_SHEET,
    PAPERS_SHEET,
    Annotator,
    annotator_a_columns,
    annotator_b_columns,
)
from radar.domain.context_values import ContextValueItem, ContextValues
from radar.domain.eval_split import split_for_dois
from radar.domain.taxonomy import Taxonomy, TaxonomyItem
from radar.domain.xlsx import column_letter, zip_members

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "annotations"
FIXTURE_FILES = {
    Annotator.A: FIXTURE_DIR / "annotator-a-filled-example.xlsx",
    Annotator.B: FIXTURE_DIR / "annotator-b-filled-example.xlsx",
}

type Value = str | int | Decimal | None
type Rows = list[list[Value]]

TEST_TAXONOMY = Taxonomy(
    version=1,
    topics=(
        TaxonomyItem(key="erp", name="ERP", definition="企業資源規劃系統。"),
        TaxonomyItem(key="supply_chain", name="供應鏈", definition="供應鏈管理。"),
        TaxonomyItem(key="e_commerce", name="電子商務", definition="線上交易與平台。"),
    ),
    methods=(
        TaxonomyItem(key="survey_sem", name="問卷與 SEM", definition="問卷資料配結構方程模型。"),
        TaxonomyItem(key="experiment", name="實驗", definition="操弄變數的實驗。"),
    ),
)


def _context(key: str, name: str, terms: tuple[str, ...] | None = None) -> ContextValueItem:
    return ContextValueItem(key=key, name=name, definition=f"{name}（測試用）。", terms=terms)


TEST_CONTEXT = ContextValues(
    version=1,
    countries=(_context("tw", "台灣", ("Taiwan",)), _context("us", "美國", ("United States",))),
    industries=(
        _context("manufacturing", "製造業", ("manufacturing",)),
        _context("retail", "零售業", ("retail",)),
    ),
    research_designs=(_context("survey", "問卷調查"), _context("experiment", "實驗")),
    units_of_analysis=(_context("individual", "個人"), _context("organization", "組織")),
)


def paper_uuid(number: int) -> uuid.UUID:
    return uuid.UUID(f"01900000-0000-7000-8000-{number:012d}")


def paper_dois(number: int) -> tuple[str, ...]:
    """範例論文的 DOI：第 2 篇有兩個（測多個 DOI 的寫法）。"""
    if number == 2:
        return ("10.5555/rr.0002", "10.5555/rr.0102")
    return (f"10.5555/rr.{number:04d}",)


def _doi_text(number: int) -> str:
    return "; ".join(paper_dois(number))


def _split(number: int) -> str:
    return split_for_dois(paper_dois(number)).value


# 第 1 篇的標題在 sharedStrings 裡存成 rich text（兩段，第一段粗體）。
RICH_TITLE_PARTS = ("Enterprise systems", " and the adoption of shared services")
RICH_TITLE = "".join(RICH_TITLE_PARTS)


def _paper_rows(annotator: Annotator) -> list[dict[str, Value]]:
    common: list[dict[str, Value]] = [
        {
            "paper_id": str(paper_uuid(1)),
            "doi": _doi_text(1),
            "title": RICH_TITLE,
            "abstract": "We survey 312 employees in Taiwan manufacturing firms.",
            "status": "done",
            "topic:erp": "yes",
            "method:survey_sem": "yes",
        },
        {
            "paper_id": str(paper_uuid(2)),
            "doi": _doi_text(2),
            "title": "Trust and loyalty in supply networks",
            "abstract": "A conceptual model of trust and loyalty among retail suppliers.",
            "status": "done",
            "topic:supply_chain": "yes",
            "topic:e_commerce": "yes",
            "method:experiment": "yes",
        },
        {
            "paper_id": str(paper_uuid(3)),
            "doi": _doi_text(3),
            "title": "An unfinished example",
            "abstract": "Not annotated yet.",
        },
    ]
    for row in common:
        row["annotator"] = annotator.value
    if annotator is Annotator.B:
        # B 也標論文層級的研究模型欄；標完的摘要層級論文要填 is_review_or_meta_analysis。
        common[0] |= {
            "is_review_or_meta_analysis": "no",
            "country": "tw",
            "research_design": "survey",
            "sample_size": 300,
        }
        common[1] |= {"is_review_or_meta_analysis": "no"}
        return common
    common[0] |= {
        "split": _split(1),
        "set": "abstract",
        "full_text": "yes",
        "is_review_or_meta_analysis": "no",
        "country": "tw",
        "industry": "manufacturing",
        "research_design": "survey",
        "unit_of_analysis": "individual",
        "sample_size": 312,
        "limitations": "Single-country sample.",
        "future_research": "Replicate in other industries.",
    }
    common[1] |= {
        "split": _split(2),
        "set": "abstract",
        "full_text": "no",
        "is_review_or_meta_analysis": "no",
        "industry": "retail",
    }
    common[2] |= {"split": _split(3), "set": "abstract"}
    return [
        *common,
        {
            "paper_id": str(paper_uuid(4)),
            "doi": _doi_text(4),
            "title": "A paper without topics",
            "abstract": None,
            "split": _split(4),
            "set": "classification_only",
            "annotator": "A",
            "status": "done",
            "method:experiment": "yes",
        },
        {
            "paper_id": str(paper_uuid(5)),
            "doi": _doi_text(5),
            "title": "Another classification-only paper",
            "abstract": "Short abstract.",
            "split": _split(5),
            "set": "classification_only",
            "annotator": "A",
        },
    ]


def _construct_rows(annotator: Annotator) -> list[dict[str, Value]]:
    rows: list[dict[str, Value]] = [
        {
            "paper_id": str(paper_uuid(1)),
            "name_en": "Perceived usefulness",
            "label_zh": "知覺有用性",
            "definition": "The degree to which a system improves job performance.",
        },
        {"paper_id": str(paper_uuid(1)), "name_en": "Intention to use", "label_zh": "使用意圖"},
        {"paper_id": str(paper_uuid(2)), "name_en": "Trust", "label_zh": "信任"},
        {"paper_id": str(paper_uuid(2)), "name_en": "Loyalty", "label_zh": "忠誠"},
    ]
    if annotator is Annotator.A:
        rows[0] |= {
            "scale_source": "Davis (1989)",
            "item_location": "Appendix A",
            "item_count": 4,
            "cronbach_alpha": Decimal("0.89"),
            "composite_reliability": Decimal("0.91"),
            "ave": Decimal("0.72"),
        }
    return rows


def _hypothesis_rows(annotator: Annotator) -> list[dict[str, Value]]:
    rows: list[dict[str, Value]] = [
        {
            "paper_id": str(paper_uuid(1)),
            "sentence": "H1: Perceived usefulness increases intention to use.",
            "cause": "Perceived usefulness",
            "effect": "intention to use",  # 大小寫不同也對得上構面
            "evidence_type": "empirical_test",
            "support": "supported",
            "result_quote": "H1 was supported (beta = 0.42, p = 0.001).",
        },
        {
            "paper_id": str(paper_uuid(2)),
            "sentence": "Trust leads to loyalty.",
            "cause": "Trust",
            "effect": "Loyalty",
            "evidence_type": "conceptual",
            "support": "not_determined",
        },
    ]
    if annotator is Annotator.A:
        rows[0] |= {
            "beta": Decimal("0.42"),
            "p_value": Decimal("0.001"),
            "sample_size": 312,
            "effect_type": "direct",
            "in_abstract": "yes",
        }
    return rows


def example_sheets(annotator: Annotator) -> dict[str, Rows]:
    """填好的範例：工作表 → 列（第一列是表頭，照 `annotator_a_columns`／`annotator_b_columns` 用
    測試分類表算出的欄名）。測試可以改裡面的值再用 `build_excel_style_workbook` 產生壞掉的檔。"""
    layout = (
        annotator_a_columns(TEST_TAXONOMY, TEST_CONTEXT)
        if annotator is Annotator.A
        else annotator_b_columns(TEST_TAXONOMY, TEST_CONTEXT)
    )
    sources = {
        PAPERS_SHEET: _paper_rows(annotator),
        CONSTRUCTS_SHEET: _construct_rows(annotator),
        HYPOTHESES_SHEET: _hypothesis_rows(annotator),
    }
    sheets: dict[str, Rows] = {}
    for name, columns in layout.sheets():
        header = [column.name for column in columns]
        header_row: list[Value] = [*header]
        sheets[name] = [header_row]
        sheets[name] += [[row.get(column) for column in header] for row in sources[name]]
    sheets[GUIDE_SHEET] = [["說明"], ["範例檔（測試用）"]]
    return sheets


def set_cell(sheets: dict[str, Rows], sheet: str, row: int, column: str, value: Value) -> None:
    """把 `sheet` 的 Excel 第 `row` 列、欄名 `column` 的值換成 `value`（需要時補長那一列）。"""
    header = sheets[sheet][0]
    position = header.index(column)
    cells = sheets[sheet][row - 1]
    cells.extend([None] * (position + 1 - len(cells)))
    cells[position] = value


def _shared_string_xml(text: str) -> str:
    if text == RICH_TITLE:
        first, second = RICH_TITLE_PARTS
        return (
            f"<si><r><rPr><b/></rPr><t>{escape(first)}</t></r>"
            f'<r><t xml:space="preserve">{escape(second)}</t></r></si>'
        )
    return f'<si><t xml:space="preserve">{escape(text)}</t></si>'


def build_excel_style_workbook(
    sheets: Mapping[str, Sequence[Sequence[Value]]],
    *,
    positional: frozenset[str] = frozenset({HYPOTHESES_SHEET}),
) -> bytes:
    """用 Excel 的存法寫 xlsx（見模組說明）；`positional` 裡的表，列與格都不寫 `r` 屬性。"""
    strings: list[str] = []
    string_index: dict[str, int] = {}
    sheet_xml: list[str] = []
    for name, rows in sheets.items():
        row_parts: list[str] = []
        for row_number, row in enumerate(rows, start=1):
            cells: list[str] = []
            last = max((index for index, value in enumerate(row) if value is not None), default=-1)
            for column, value in enumerate(row[: last + 1]):
                if value is None:
                    if name in positional:
                        cells.append("<c/>")  # 依位置的表：空的格寫成沒有值的 <c/> 佔位
                    continue
                ref = "" if name in positional else f' r="{column_letter(column)}{row_number}"'
                if isinstance(value, str):
                    if value not in string_index:
                        string_index[value] = len(strings)
                        strings.append(value)
                    cells.append(f'<c{ref} t="s"><v>{string_index[value]}</v></c>')
                else:
                    cells.append(f"<c{ref}><v>{value}</v></c>")
            if name in positional:
                row_parts.append(f"<row>{''.join(cells)}</row>")
            else:
                row_parts.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheetData>{''.join(row_parts)}</sheetData></worksheet>"
        )
    names = list(sheets)
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    header = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    content_types = (
        header + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(names) + 1)
        )
        + '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )
    root_rels = (
        header + f'<Relationships xmlns="{package_rel}">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        header + f'<workbook xmlns="{main}" xmlns:r="{rel}"><bookViews><workbookView/></bookViews>'
        "<sheets>"
        + "".join(
            f'<sheet name={quoteattr(name)} sheetId="{index}" r:id="rId{index}"/>'
            for index, name in enumerate(names, start=1)
        )
        + "</sheets></workbook>"
    )
    # 第一張表的 Target 用絕對路徑，其他用相對路徑（兩種 Excel 都會寫）。
    workbook_rels = (
        header
        + f'<Relationships xmlns="{package_rel}">'
        + "".join(
            f'<Relationship Id="rId{index}" Type="{rel}/worksheet" Target="'
            + ("/xl/" if index == 1 else "")
            + f'worksheets/sheet{index}.xml"/>'
            for index in range(1, len(names) + 1)
        )
        + f'<Relationship Id="rId{len(names) + 1}" Type="{rel}/sharedStrings"'
        ' Target="sharedStrings.xml"/>'
        "</Relationships>"
    )
    shared = (
        header + f'<sst xmlns="{main}" count="{len(strings)}" uniqueCount="{len(strings)}">'
        f"{''.join(_shared_string_xml(text) for text in strings)}</sst>"
    )
    members = [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", root_rels),
        ("xl/workbook.xml", workbook),
        ("xl/_rels/workbook.xml.rels", workbook_rels),
        *((f"xl/worksheets/sheet{index}.xml", xml) for index, xml in enumerate(sheet_xml, start=1)),
        ("xl/sharedStrings.xml", shared),
    ]
    return zip_members(members, compress=False)


def build_filled_example(annotator: Annotator) -> bytes:
    """填好的範例試算表（A 或 B）。"""
    return build_excel_style_workbook(example_sheets(annotator))


def write_fixtures() -> None:
    """把兩份範例寫到 `tests/fixtures/annotations/`（覆寫；只放自己產生的假資料）。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for annotator, path in FIXTURE_FILES.items():
        path.write_bytes(build_filled_example(annotator))
