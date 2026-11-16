"""標註試算表的版面、產生與讀回（Story 2.13、ADR-0045、AR-63）。

- A／B 的欄名；B 沒有 A 的答案欄與任何 AI 欄；選項值來自傳入的分類表與情境值清單。
- 產生的檔：三張表加說明、預填的欄、每個有選項的欄都有資料驗證、同輸入同位元組。
- `read_sampled_papers`：預填欄讀回、「已經開始標註」的判斷。
- `parse_annotation_workbook`：範例檔解析成功且型別正確；spec I/O 矩陣「解析失敗」每一條各一個
  測試，檢查回報的工作表、列號、欄。範例檔要和 `build_filled_example` 產生的位元組一樣。
"""

import io
import uuid
import zipfile
from collections.abc import Callable
from decimal import Decimal
from xml.etree import ElementTree

import pytest

from radar.domain.annotations import (
    CONSTRUCTS_SHEET,
    GUIDE_SHEET,
    HYPOTHESES_SHEET,
    PAPERS_SHEET,
    AnnotatedPaper,
    AnnotationLevel,
    AnnotationParseError,
    AnnotationProblem,
    AnnotationRow,
    Annotator,
    SampleSet,
    annotator_a_columns,
    annotator_b_columns,
    build_annotator_a_workbook,
    build_annotator_b_workbook,
    detail_row_count,
    parse_annotation_workbook,
    read_sampled_papers,
    render_annotation_check,
)
from radar.domain.eval_split import EvalSplit, split_for_dois
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.xlsx import column_letter, read_workbook
from tests.annotation_support import (
    FIXTURE_FILES,
    RICH_TITLE,
    TEST_CONTEXT,
    TEST_TAXONOMY,
    Rows,
    build_excel_style_workbook,
    build_filled_example,
    example_sheets,
    paper_uuid,
    set_cell,
)

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

A_PAPERS = [
    "paper_id",
    "doi",
    "title",
    "abstract",
    "split",
    "set",
    "full_text",
    "annotator",
    "status",
    "topic:erp",
    "topic:supply_chain",
    "topic:e_commerce",
    "method:survey_sem",
    "method:experiment",
    "is_review_or_meta_analysis",
    "country",
    "industry",
    "research_design",
    "unit_of_analysis",
    "sample_size",
    "limitations",
    "future_research",
]
A_CONSTRUCTS = [
    "paper_id",
    "name_en",
    "label_zh",
    "definition",
    "scale_source",
    "item_location",
    "item_count",
    "cronbach_alpha",
    "composite_reliability",
    "ave",
]
A_HYPOTHESES = [
    "paper_id",
    "sentence",
    "cause",
    "effect",
    "evidence_type",
    "support",
    "result_quote",
    "beta",
    "p_value",
    "sample_size",
    "effect_type",
    "in_abstract",
]
B_PAPERS = [
    "paper_id",
    "doi",
    "title",
    "abstract",
    "annotator",
    "status",
    "topic:erp",
    "topic:supply_chain",
    "topic:e_commerce",
    "method:survey_sem",
    "method:experiment",
]
B_CONSTRUCTS = ["paper_id", "name_en", "label_zh", "definition"]
B_HYPOTHESES = [
    "paper_id",
    "sentence",
    "cause",
    "effect",
    "evidence_type",
    "support",
    "result_quote",
]


def _rows(count: int = 4) -> list[AnnotationRow]:
    rows: list[AnnotationRow] = []
    for number in range(1, count + 1):
        dois = (f"10.5555/gen.{number:04d}",)
        rows.append(
            AnnotationRow(
                paper_id=uuid.UUID(int=number),
                dois=dois,
                title=f"Generated title {number}",
                abstract=None if number == count else f"Generated abstract {number}.",
                split=split_for_dois(dois),
                sample_set=SampleSet.ABSTRACT if number <= 2 else SampleSet.CLASSIFICATION_ONLY,
            )
        )
    return rows


# --- 版面 ---------------------------------------------------------------------------------


def test_annotator_a_columns() -> None:
    layout = annotator_a_columns(TEST_TAXONOMY, TEST_CONTEXT)
    assert [column.name for column in layout.papers] == A_PAPERS
    assert [column.name for column in layout.constructs] == A_CONSTRUCTS
    assert [column.name for column in layout.hypotheses] == A_HYPOTHESES
    by_name = {column.name: column for column in layout.papers}
    assert by_name["country"].options == ("tw", "us")
    assert by_name["unit_of_analysis"].options == ("individual", "organization")
    assert by_name["topic:erp"].options == ("yes",)
    assert by_name["status"].options == ("done",)
    assert by_name["set"].options == ("abstract", "classification_only")
    assert by_name["split"].options == ("tuning", "test")
    hypotheses = {column.name: column for column in layout.hypotheses}
    assert hypotheses["evidence_type"].options == tuple(item.value for item in EvidenceType)
    assert hypotheses["support"].options == tuple(item.value for item in SupportLevel)


def test_annotator_b_columns_have_no_answers_of_a_and_no_ai() -> None:
    layout = annotator_b_columns(TEST_TAXONOMY)
    assert [column.name for column in layout.papers] == B_PAPERS
    assert [column.name for column in layout.constructs] == B_CONSTRUCTS
    assert [column.name for column in layout.hypotheses] == B_HYPOTHESES
    names = {column.name for _, columns in layout.sheets() for column in columns}
    assert not names & {"split", "set", "full_text", "country", "beta", "in_abstract"}
    assert not any("ai" in name.split("_") or name.startswith("ai") for name in names)


def test_options_come_from_the_given_taxonomy() -> None:
    other = TEST_TAXONOMY.model_copy(update={"topics": TEST_TAXONOMY.topics[:1]})
    names = [column.name for column in annotator_b_columns(other).papers]
    assert "topic:erp" in names
    assert "topic:supply_chain" not in names


# --- 產生 ---------------------------------------------------------------------------------


def _sheet_xml(data: bytes, index: int) -> ElementTree.Element:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))


def _validations(data: bytes, index: int) -> dict[str, tuple[str | None, list[str | None]]]:
    """欄字母 → (驗證種類, formula 的文字)。"""
    found: dict[str, tuple[str | None, list[str | None]]] = {}
    for item in _sheet_xml(data, index).iter(f"{MAIN}dataValidation"):
        sqref = item.get("sqref", "")
        letter = "".join(char for char in sqref.split(":")[0] if char.isalpha())
        assert item.get("errorStyle") == "stop"
        found[letter] = (item.get("type"), [formula.text for formula in item])
    return found


def test_annotator_a_workbook_contents() -> None:
    rows = _rows()
    data = build_annotator_a_workbook(rows, TEST_TAXONOMY, TEST_CONTEXT)

    sheets = read_workbook(data)
    assert list(sheets) == [PAPERS_SHEET, CONSTRUCTS_SHEET, HYPOTHESES_SHEET, GUIDE_SHEET]
    papers = sheets[PAPERS_SHEET]
    assert list(papers[0]) == A_PAPERS
    first = dict(zip(A_PAPERS, papers[1], strict=True))
    assert first["paper_id"] == str(rows[0].paper_id)
    assert first["doi"] == rows[0].doi
    assert first["title"] == "Generated title 1"
    assert first["split"] == rows[0].split.value
    assert first["set"] == "abstract"
    assert first["annotator"] == "A"
    assert all(
        value is None
        for name, value in first.items()
        if name not in {"paper_id", "doi", "title", "abstract", "split", "set", "annotator"}
    )
    assert dict(zip(A_PAPERS, papers[4], strict=True))["abstract"] is None
    assert [list(sheets[CONSTRUCTS_SHEET][0]), list(sheets[HYPOTHESES_SHEET][0])] == [
        A_CONSTRUCTS,
        A_HYPOTHESES,
    ]
    assert sheets[GUIDE_SHEET][0] == ("工作表", "欄", "意思", "可填的值", "什麼時候填")
    guide = {(row[0], row[1]): row for row in sheets[GUIDE_SHEET][1:]}
    assert "tw：台灣" in str(guide[(PAPERS_SHEET, "country")][3])
    assert "全文層級" in str(guide[(HYPOTHESES_SHEET, "in_abstract")][4])


def test_annotator_a_workbook_validations() -> None:
    data = build_annotator_a_workbook(_rows(), TEST_TAXONOMY, TEST_CONTEXT)
    letter = {name: column_letter(index) for index, name in enumerate(A_PAPERS)}

    papers = _validations(data, 1)
    assert papers[letter["status"]] == ("list", ['"done"'])
    assert papers[letter["topic:erp"]] == ("list", ['"yes"'])
    assert papers[letter["country"]] == ("list", ['"tw,us"'])
    assert papers[letter["full_text"]] == ("list", ['"yes,no"'])
    assert papers[letter["annotator"]] == ("list", ['"A,B"'])
    assert papers[letter["sample_size"]] == ("whole", ["1", "2147483647"])
    assert {
        letter[name] for name in ("paper_id", "doi", "title", "abstract", "limitations")
    }.isdisjoint(papers)
    sqrefs = {item.get("sqref") for item in _sheet_xml(data, 1).iter(f"{MAIN}dataValidation")}
    assert f"{letter['status']}2:{letter['status']}5" in sqrefs  # 4 篇論文 → 第 2～5 列

    constructs = _validations(data, 2)
    assert constructs["A"] == ("list", ["'論文'!$A$2:$A$5"])
    assert constructs["G"] == ("whole", ["1", "2147483647"])
    assert constructs["H"] == ("decimal", ["0", "1"])
    hypotheses = _validations(data, 3)
    assert hypotheses["A"] == ("list", ["'論文'!$A$2:$A$5"])
    assert hypotheses["C"] == ("list", [f"'構面'!$B$2:$B${detail_row_count(4) + 1}"])
    assert hypotheses["E"][1] == ['"' + ",".join(item.value for item in EvidenceType) + '"']
    assert hypotheses["H"][0] == "decimal"  # beta：任意小數
    assert hypotheses["I"] == ("decimal", ["0", "1"])
    detail = {item.get("sqref") for item in _sheet_xml(data, 2).iter(f"{MAIN}dataValidation")}
    assert "A2:A201" in detail  # 至少 200 列


def test_detail_rows() -> None:
    assert detail_row_count(1) == 200
    assert detail_row_count(150) == 3000


def test_workbooks_are_deterministic() -> None:
    rows = _rows()
    assert build_annotator_a_workbook(rows, TEST_TAXONOMY, TEST_CONTEXT) == (
        build_annotator_a_workbook(rows, TEST_TAXONOMY, TEST_CONTEXT)
    )
    assert build_annotator_b_workbook(rows, TEST_TAXONOMY) == build_annotator_b_workbook(
        rows, TEST_TAXONOMY
    )


def test_generated_workbooks_parse_back() -> None:
    rows = _rows()
    parsed = parse_annotation_workbook(
        build_annotator_a_workbook(rows, TEST_TAXONOMY, TEST_CONTEXT), TEST_TAXONOMY, TEST_CONTEXT
    )
    assert parsed.annotator is Annotator.A
    assert [paper.paper_id for paper in parsed.papers] == [row.paper_id for row in rows]
    assert not any(paper.done for paper in parsed.papers)

    b = parse_annotation_workbook(
        build_annotator_b_workbook(rows[:2], TEST_TAXONOMY), TEST_TAXONOMY, TEST_CONTEXT
    )
    assert b.annotator is Annotator.B
    assert len(b.papers) == 2
    assert read_workbook(build_annotator_b_workbook(rows[:2], TEST_TAXONOMY))[PAPERS_SHEET][0] == (
        tuple(B_PAPERS)
    )


# --- read_sampled_papers ------------------------------------------------------------------


def test_read_sampled_papers_round_trip() -> None:
    rows = _rows()
    sampled = read_sampled_papers(build_annotator_a_workbook(rows, TEST_TAXONOMY, TEST_CONTEXT))
    assert sampled.rows == tuple(rows)
    assert not sampled.annotated


def _a_sheets_without_answers() -> dict[str, Rows]:
    sheets = example_sheets(Annotator.A)
    prefilled = {"paper_id", "doi", "title", "abstract", "split", "set", "annotator"}
    header = sheets[PAPERS_SHEET][0]
    for row in sheets[PAPERS_SHEET][1:]:
        for index, name in enumerate(header):
            if name not in prefilled:
                row[index] = None
    sheets[CONSTRUCTS_SHEET] = sheets[CONSTRUCTS_SHEET][:1]
    sheets[HYPOTHESES_SHEET] = sheets[HYPOTHESES_SHEET][:1]
    return sheets


def _sampled(sheets: dict[str, Rows]) -> bool:
    data = build_excel_style_workbook(sheets, positional=frozenset())
    return read_sampled_papers(data).annotated


def test_annotated_detection() -> None:
    assert not _sampled(_a_sheets_without_answers())
    for column in ("status", "topic:erp", "country", "full_text", "limitations"):
        sheets = _a_sheets_without_answers()
        set_cell(sheets, PAPERS_SHEET, 2, column, "x")
        assert _sampled(sheets), column
    sheets = _a_sheets_without_answers()
    sheets[CONSTRUCTS_SHEET].append([str(paper_uuid(1)), "Trust"])
    assert _sampled(sheets)
    sheets = _a_sheets_without_answers()
    sheets[HYPOTHESES_SHEET].append([str(paper_uuid(1)), "H1"])
    assert _sampled(sheets)


def test_read_sampled_papers_rejects_b_and_bad_prefilled_values() -> None:
    with pytest.raises(AnnotationParseError) as caught:
        read_sampled_papers(build_filled_example(Annotator.B))
    assert {problem.column for problem in caught.value.problems} >= {"split", "set"}

    sheets = _a_sheets_without_answers()
    flipped = "test" if sheets[PAPERS_SHEET][1][4] == "tuning" else "tuning"
    set_cell(sheets, PAPERS_SHEET, 2, "split", flipped)
    set_cell(sheets, PAPERS_SHEET, 3, "paper_id", str(paper_uuid(1)))
    with pytest.raises(AnnotationParseError) as caught:
        read_sampled_papers(build_excel_style_workbook(sheets, positional=frozenset()))
    assert [(p.row, p.column) for p in caught.value.problems] == [
        (2, "split（E）"),
        (3, "paper_id（A）"),
    ]


# --- parse_annotation_workbook：成功 ---------------------------------------------------------


def test_fixture_files_are_up_to_date() -> None:
    for annotator, path in FIXTURE_FILES.items():
        assert path.read_bytes() == build_filled_example(annotator), (
            f"{path.name} 過期了：重新跑 write_fixtures()（見 tests/annotation_support.py）"
        )


def _paper(papers: tuple[AnnotatedPaper, ...], number: int) -> AnnotatedPaper:
    return next(paper for paper in papers if paper.paper_id == paper_uuid(number))


def test_parse_annotator_a_fixture() -> None:
    workbook = parse_annotation_workbook(
        FIXTURE_FILES[Annotator.A].read_bytes(), TEST_TAXONOMY, TEST_CONTEXT
    )
    assert workbook.annotator is Annotator.A
    assert len(workbook.papers) == 5
    first = _paper(workbook.papers, 1)
    assert first.done
    assert first.full_text
    assert first.levels == (AnnotationLevel.ABSTRACT, AnnotationLevel.FULL_TEXT)
    assert first.topics == ("erp",)
    assert first.methods == ("survey_sem",)
    assert first.is_review_or_meta_analysis is False
    assert (first.country, first.industry) == ("tw", "manufacturing")
    assert first.sample_size == 312
    assert type(first.sample_size) is int
    assert first.split is split_for_dois(["10.5555/rr.0001"])
    second = _paper(workbook.papers, 2)
    assert second.dois == ("10.5555/rr.0002", "10.5555/rr.0102")
    assert second.doi == "10.5555/rr.0002"
    assert second.split is split_for_dois(second.dois)
    assert second.topics == ("e_commerce", "supply_chain")  # 排序好的 key
    assert second.levels == (AnnotationLevel.ABSTRACT,)
    assert not _paper(workbook.papers, 3).done  # status 空白＝還沒標
    unclassified = _paper(workbook.papers, 4)
    assert unclassified.done
    assert unclassified.topics == ()
    assert unclassified.levels == (AnnotationLevel.CLASSIFICATION_ONLY,)

    construct = workbook.constructs[0]
    assert construct.name_en == "Perceived usefulness"
    assert construct.item_count == 4
    assert construct.cronbach_alpha == Decimal("0.89")
    assert isinstance(construct.ave, Decimal)
    assert len(workbook.constructs) == 4
    hypothesis = workbook.hypotheses[0]
    assert hypothesis.evidence_type is EvidenceType.EMPIRICAL_TEST
    assert hypothesis.support is SupportLevel.SUPPORTED
    assert hypothesis.beta == Decimal("0.42")
    assert hypothesis.p_value == Decimal("0.001")
    assert hypothesis.sample_size == 312
    assert hypothesis.in_abstract is True
    assert workbook.hypotheses[1].support is SupportLevel.NOT_DETERMINED


def test_parse_annotator_b_fixture() -> None:
    workbook = parse_annotation_workbook(
        FIXTURE_FILES[Annotator.B].read_bytes(), TEST_TAXONOMY, TEST_CONTEXT
    )
    assert workbook.annotator is Annotator.B
    assert [paper.paper_id for paper in workbook.papers] == [paper_uuid(n) for n in (1, 2, 3)]
    first = workbook.papers[0]
    assert first.sample_set is SampleSet.ABSTRACT
    assert first.levels == (AnnotationLevel.ABSTRACT,)
    assert first.split is EvalSplit(split_for_dois(first.dois))
    assert first.country is None
    assert workbook.constructs[0].item_count is None
    assert workbook.hypotheses[0].beta is None
    assert workbook.hypotheses[0].in_abstract is None


def test_parse_reads_rich_text_title_without_error() -> None:
    # 範例第 1 篇的標題是 rich text；標題不進結果，但整份要讀得過。
    assert read_workbook(build_filled_example(Annotator.A))[PAPERS_SHEET][1][2] == RICH_TITLE


def test_lenient_values_are_accepted() -> None:
    sheets = example_sheets(Annotator.A)
    set_cell(sheets, PAPERS_SHEET, 2, "status", " DONE ")
    set_cell(sheets, PAPERS_SHEET, 2, "topic:erp", "Yes")
    set_cell(sheets, PAPERS_SHEET, 2, "sample_size", "312.0")
    set_cell(sheets, CONSTRUCTS_SHEET, 2, "item_count", Decimal("4.0"))
    set_cell(sheets, CONSTRUCTS_SHEET, 2, "cronbach_alpha", "0.9")
    sheets[PAPERS_SHEET].insert(3, [None, "   "])  # 全空白的列略過
    workbook = parse_annotation_workbook(
        build_excel_style_workbook(sheets), TEST_TAXONOMY, TEST_CONTEXT
    )
    first = _paper(workbook.papers, 1)
    assert first.done
    assert first.sample_size == 312
    assert workbook.constructs[0].item_count == 4
    assert workbook.constructs[0].cronbach_alpha == Decimal("0.9")


def test_column_order_does_not_matter() -> None:
    sheets = example_sheets(Annotator.A)
    for name in (PAPERS_SHEET, CONSTRUCTS_SHEET):
        sheets[name] = [
            list(reversed(row + [None] * (len(sheets[name][0]) - len(row)))) for row in sheets[name]
        ]
    workbook = parse_annotation_workbook(
        build_excel_style_workbook(sheets), TEST_TAXONOMY, TEST_CONTEXT
    )
    assert len(workbook.papers) == 5
    assert workbook.constructs[0].cronbach_alpha == Decimal("0.89")


# --- parse_annotation_workbook：失敗（I/O 矩陣逐條） ------------------------------------------


def _problems(
    change: Callable[[dict[str, Rows]], object], annotator: Annotator = Annotator.A
) -> list[AnnotationProblem]:
    sheets = example_sheets(annotator)
    change(sheets)
    with pytest.raises(AnnotationParseError) as caught:
        parse_annotation_workbook(build_excel_style_workbook(sheets), TEST_TAXONOMY, TEST_CONTEXT)
    return list(caught.value.problems)


def _where(problems: list[AnnotationProblem]) -> list[tuple[str | None, int | None, str | None]]:
    return [(problem.sheet, problem.row, problem.column) for problem in problems]


def _col(annotator: Annotator, sheet: str, name: str) -> str:
    header = example_sheets(annotator)[sheet][0]
    return f"{name}（{column_letter(header.index(name))}）"


A = Annotator.A


def test_value_not_in_dropdown() -> None:
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 2, "status", "maybe"))
    assert _where(problems) == [(PAPERS_SHEET, 2, _col(A, PAPERS_SHEET, "status"))]
    assert problems[0].reason == "不在下拉清單裡"
    assert "maybe" not in str(problems)


def test_context_value_not_in_list() -> None:
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 2, "country", "fr"))
    assert _where(problems) == [(PAPERS_SHEET, 2, _col(A, PAPERS_SHEET, "country"))]


@pytest.mark.parametrize("value", [Decimal("3.5"), "many", "1_000", "0"])
def test_integer_column_rejects_decimal_text_and_zero(value: str | Decimal) -> None:
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 2, "sample_size", value))
    assert _where(problems) == [(PAPERS_SHEET, 2, _col(A, PAPERS_SHEET, "sample_size"))]


def test_unit_interval_column_out_of_range() -> None:
    problems = _problems(
        lambda s: set_cell(s, CONSTRUCTS_SHEET, 2, "cronbach_alpha", Decimal("1.2"))
    )
    assert _where(problems) == [(CONSTRUCTS_SHEET, 2, _col(A, CONSTRUCTS_SHEET, "cronbach_alpha"))]
    assert problems[0].reason == "要在 0～1 之間"


def test_cause_must_be_a_construct_of_the_same_paper() -> None:
    # 假設表第 3 列是第 2 篇；Perceived usefulness 是第 1 篇的構面。假設表沒有 r 屬性（依位置）。
    problems = _problems(
        lambda s: set_cell(s, HYPOTHESES_SHEET, 3, "cause", "Perceived usefulness")
    )
    assert _where(problems) == [(HYPOTHESES_SHEET, 3, _col(A, HYPOTHESES_SHEET, "cause"))]


def test_cause_and_effect_must_differ() -> None:
    problems = _problems(lambda s: set_cell(s, HYPOTHESES_SHEET, 3, "effect", "trust"))
    assert _where(problems) == [(HYPOTHESES_SHEET, 3, _col(A, HYPOTHESES_SHEET, "effect"))]


def test_non_empirical_must_be_not_determined() -> None:
    problems = _problems(lambda s: set_cell(s, HYPOTHESES_SHEET, 3, "support", "supported"))
    assert _where(problems) == [(HYPOTHESES_SHEET, 3, _col(A, HYPOTHESES_SHEET, "support"))]


def test_classification_only_cannot_have_model_columns_or_constructs() -> None:
    def change(sheets: dict[str, Rows]) -> None:
        set_cell(sheets, PAPERS_SHEET, 5, "country", "tw")
        set_cell(sheets, PAPERS_SHEET, 5, "full_text", "yes")
        sheets[CONSTRUCTS_SHEET].append([str(paper_uuid(4)), "Orphan"])
        sheets[HYPOTHESES_SHEET].append(
            [str(paper_uuid(4)), "S", "Orphan", "Other", "conceptual", "not_determined"]
        )

    problems = _problems(change)
    assert (PAPERS_SHEET, 5, _col(A, PAPERS_SHEET, "country")) in _where(problems)
    assert (PAPERS_SHEET, 5, _col(A, PAPERS_SHEET, "full_text")) in _where(problems)
    assert (CONSTRUCTS_SHEET, 6, _col(A, CONSTRUCTS_SHEET, "paper_id")) in _where(problems)
    assert (HYPOTHESES_SHEET, 4, _col(A, HYPOTHESES_SHEET, "paper_id")) in _where(problems)


def test_in_abstract_only_for_full_text_papers() -> None:
    problems = _problems(lambda s: set_cell(s, HYPOTHESES_SHEET, 3, "in_abstract", "no"))
    assert _where(problems) == [(HYPOTHESES_SHEET, 3, _col(A, HYPOTHESES_SHEET, "in_abstract"))]


def test_duplicate_paper_id() -> None:
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 4, "paper_id", str(paper_uuid(1))))
    assert _where(problems) == [(PAPERS_SHEET, 4, _col(A, PAPERS_SHEET, "paper_id"))]
    assert "重複" in problems[0].reason


def test_paper_id_not_in_paper_sheet() -> None:
    problems = _problems(lambda s: set_cell(s, CONSTRUCTS_SHEET, 5, "paper_id", str(paper_uuid(9))))
    assert (CONSTRUCTS_SHEET, 5, _col(A, CONSTRUCTS_SHEET, "paper_id")) in _where(problems)
    assert problems[0].reason == "paper_id 不在論文表"


def test_split_differs_from_doi() -> None:
    def change(sheets: dict[str, Rows]) -> None:
        current = sheets[PAPERS_SHEET][2][4]
        set_cell(sheets, PAPERS_SHEET, 3, "split", "test" if current == "tuning" else "tuning")

    problems = _problems(change)
    assert _where(problems) == [(PAPERS_SHEET, 3, _col(A, PAPERS_SHEET, "split"))]


def test_annotator_does_not_match_layout() -> None:
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 2, "annotator", "B"))
    assert _where(problems) == [(PAPERS_SHEET, 2, _col(A, PAPERS_SHEET, "annotator"))]
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 3, "annotator", "A"), Annotator.B)
    assert _where(problems) == [(PAPERS_SHEET, 3, _col(Annotator.B, PAPERS_SHEET, "annotator"))]


def test_missing_and_extra_columns() -> None:
    def change(sheets: dict[str, Rows]) -> None:
        set_cell(sheets, PAPERS_SHEET, 1, "status", "statuz")

    problems = _problems(change)
    letter = column_letter(A_PAPERS.index("status"))
    assert _where(problems) == [(PAPERS_SHEET, 1, letter), (PAPERS_SHEET, 1, "status")]
    assert [problem.reason for problem in problems] == ["不認得的欄名", "缺少這一欄"]


def test_duplicate_column_and_value_without_header() -> None:
    def change(sheets: dict[str, Rows]) -> None:
        sheets[CONSTRUCTS_SHEET][0].append("label_zh")
        sheets[HYPOTHESES_SHEET][1].extend([None] * 5 + ["stray"])

    problems = _problems(change)
    width = len(A_CONSTRUCTS)
    assert (CONSTRUCTS_SHEET, 1, f"label_zh（{column_letter(width)}）") in _where(problems)
    stray = [p for p in problems if p.sheet == HYPOTHESES_SHEET]
    assert [p.reason for p in stray] == ["這一欄沒有欄名卻有值"]
    assert stray[0].row == 2


def test_missing_sheet() -> None:
    problems = _problems(lambda s: s.pop(HYPOTHESES_SHEET))
    assert _where(problems) == [(HYPOTHESES_SHEET, None, None)]


def test_required_fields_bad_ids_and_duplicate_constructs() -> None:
    def change(sheets: dict[str, Rows]) -> None:
        set_cell(sheets, PAPERS_SHEET, 2, "doi", "not a doi")
        set_cell(sheets, PAPERS_SHEET, 3, "paper_id", "not-a-uuid")
        set_cell(sheets, CONSTRUCTS_SHEET, 3, "name_en", "  PERCEIVED   usefulness ")
        set_cell(sheets, HYPOTHESES_SHEET, 2, "evidence_type", None)

    problems = _problems(change)
    assert _where(problems) == [
        (PAPERS_SHEET, 2, _col(A, PAPERS_SHEET, "doi")),
        (PAPERS_SHEET, 3, _col(A, PAPERS_SHEET, "paper_id")),
        (CONSTRUCTS_SHEET, 3, _col(A, CONSTRUCTS_SHEET, "name_en")),
        (CONSTRUCTS_SHEET, 4, _col(A, CONSTRUCTS_SHEET, "paper_id")),
        (CONSTRUCTS_SHEET, 5, _col(A, CONSTRUCTS_SHEET, "paper_id")),
        (HYPOTHESES_SHEET, 2, _col(A, HYPOTHESES_SHEET, "evidence_type")),
        # Intention to use 被改成重複的名稱，第 1 篇就沒有這個構面了。
        (HYPOTHESES_SHEET, 2, _col(A, HYPOTHESES_SHEET, "effect")),
        (HYPOTHESES_SHEET, 3, _col(A, HYPOTHESES_SHEET, "paper_id")),
    ]
    assert problems[2].reason.startswith("同一篇的構面名稱重複")
    assert problems[5].reason == "必填"


@pytest.mark.parametrize(
    "data",
    [
        b"not a zip at all",
        b"PK\x03\x04garbage",
    ],
)
def test_unreadable_file_is_a_parse_error(data: bytes) -> None:
    with pytest.raises(AnnotationParseError) as caught:
        parse_annotation_workbook(data, TEST_TAXONOMY, TEST_CONTEXT)
    [problem] = caught.value.problems
    assert (problem.sheet, problem.row, problem.column) == (None, None, None)
    with pytest.raises(AnnotationParseError):
        read_sampled_papers(data)


def test_doctype_file_is_a_parse_error() -> None:
    data = build_filled_example(Annotator.A)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "xl/sharedStrings.xml":
                content = content.replace(b"<sst", b'<!DOCTYPE sst [<!ENTITY a "aa">]><sst', 1)
            target.writestr(info.filename, content)
    with pytest.raises(AnnotationParseError) as caught:
        parse_annotation_workbook(buffer.getvalue(), TEST_TAXONOMY, TEST_CONTEXT)
    assert "DOCTYPE" in caught.value.problems[0].reason


# --- 審查後補的測試 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dois",
    [
        ("10.5555/multi.0001", "10.5555/multi.0002"),
        ("10.1002/(sici)1097-4571(199806)49:8<693::aid-asi4>3.0.co;2-o", "10.5555/second"),
    ],
    ids=["two-dois", "doi-with-semicolon"],
)
def test_multi_doi_round_trip(dois: tuple[str, ...]) -> None:
    rows = [
        AnnotationRow(
            paper_id=uuid.UUID(int=1),
            dois=dois,
            title="Multi DOI",
            abstract="An abstract.",
            split=split_for_dois(dois),
            sample_set=SampleSet.ABSTRACT,
        )
    ]
    data = build_annotator_a_workbook(rows, TEST_TAXONOMY, TEST_CONTEXT)
    assert read_sampled_papers(data).rows == tuple(rows)
    paper = parse_annotation_workbook(data, TEST_TAXONOMY, TEST_CONTEXT).papers[0]
    assert paper.dois == dois
    assert paper.split is split_for_dois(dois)


@pytest.mark.parametrize("value", [Decimal("1E+300000000"), Decimal("1E-300000000")])
def test_huge_exponent_number_in_text_column_is_not_expanded(value: Decimal) -> None:
    problems = _problems(lambda s: set_cell(s, PAPERS_SHEET, 2, "limitations", value))
    assert _where(problems) == [(PAPERS_SHEET, 2, _col(A, PAPERS_SHEET, "limitations"))]
    assert problems[0].reason == "要是文字"


def test_in_abstract_required_for_done_full_text_papers() -> None:
    # 假設表第 2 列是第 1 篇（全文層級、done）。
    problems = _problems(lambda s: set_cell(s, HYPOTHESES_SHEET, 2, "in_abstract", None))
    assert _where(problems) == [(HYPOTHESES_SHEET, 2, _col(A, HYPOTHESES_SHEET, "in_abstract"))]
    assert problems[0].reason == "全文層級的論文要填"


def test_cause_and_effect_are_stored_as_the_construct_name() -> None:
    sheets = example_sheets(A)
    set_cell(sheets, HYPOTHESES_SHEET, 2, "cause", "perceived   USEFULNESS")
    workbook = parse_annotation_workbook(
        build_excel_style_workbook(sheets), TEST_TAXONOMY, TEST_CONTEXT
    )
    assert workbook.hypotheses[0].cause == "Perceived usefulness"
    assert workbook.hypotheses[0].effect == "Intention to use"  # 範例填的是小寫


def test_render_annotation_check_on_filled_example() -> None:
    workbook = parse_annotation_workbook(
        FIXTURE_FILES[Annotator.A].read_bytes(), TEST_TAXONOMY, TEST_CONTEXT
    )
    lines = render_annotation_check(workbook)
    abstract_line = next(line for line in lines if line.startswith("摘要層級"))
    assert "已標 0 篇" not in abstract_line
    assert any(line.startswith("全文層級") for line in lines)
