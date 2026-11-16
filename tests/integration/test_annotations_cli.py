"""`radar annotations template`／`template-b`／`check` 對真的 PostgreSQL（`CliRunner`＋環境變數；
Story 2.13）。

成功、同 seed 同位元組、退出碼 2（不碰資料庫：資料庫連線字串指到連不上的位址）／1／130、輸出檔
已存在不覆蓋、補抽、補抽被擋、標註者 B、輸出與日誌不含標題與摘要。設定用 repo 的 `config/`
（複製一份），論文都是自己編的。
"""

import logging
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

import pytest
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from radar.config import load_context_values, load_taxonomy
from radar.domain.annotations import (
    PAPERS_SHEET,
    AnnotationParseError,
    AnnotationRow,
    Annotator,
    SampleSet,
    build_annotator_a_workbook,
    parse_annotation_workbook,
    read_sampled_papers,
)
from radar.domain.context_values import ContextValues
from radar.domain.eval_split import split_for_dois
from radar.domain.taxonomy import Taxonomy
from radar.domain.xlsx import read_workbook
from radar.repo.db import Database
from tests.annotation_support import build_excel_style_workbook
from tests.database import ROOT, TestDatabase, migrate
from tests.integration.test_annotations import JOURNAL_A, JOURNAL_B, paper, snapshot

runner = CliRunner()
UNREACHABLE_DATABASE = "postgresql://nobody@127.0.0.1:1/nothing"


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    return custom


@pytest.fixture
def env(db: TestDatabase, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def offline_env(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """資料庫連不上：參數錯的指令要在碰資料庫之前就退出碼 2。"""
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE)
    monkeypatch.setenv("RADAR_DATABASE_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def papers(db: TestDatabase, database: Database) -> list[uuid.UUID]:
    published = snapshot(db, 1)
    made = [paper(database, db, n, snapshot_id=published) for n in range(1, 9)]
    made += [
        paper(database, db, n, snapshot_id=published, issn=JOURNAL_B, year=2021, abstract=False)
        for n in range(9, 15)
    ]
    made.append(paper(database, db, 15, snapshot_id=published, issn=JOURNAL_A, doi=False))
    return made


def _run(*args: str) -> Result:
    """執行 `radar annotations …`；結束後把 root logger 的 handler 換回原本的（見
    test_costs_cli）。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["annotations", *args])
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _stdout(result: Result) -> list[str]:
    return [line for line in result.stdout.splitlines() if not line.startswith("{")]


def _logs(result: Result) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("{")]


def _template(out: Path, *extra: str, seed: str = "7") -> Result:
    return _run(
        "template",
        "--seed",
        seed,
        "--out",
        str(out),
        "--abstract-count",
        "4",
        "--classification-count",
        "5",
        *extra,
    )


def test_template_writes_a_parsable_workbook(
    env: Path, papers: list[uuid.UUID], tmp_path: Path
) -> None:
    out = tmp_path / "A.xlsx"

    result = _template(out, "--interest", "erp")

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert lines[0] == "seed 7；興趣主題：erp"
    assert any(line.startswith("摘要層級：4 篇（") for line in lines)
    assert any(line.startswith("分類專用：5 篇（") for line in lines)
    assert "沒有 DOI、沒有抽的算數論文：1 篇" in lines
    assert "各主題篇數（AI 分類或人工修正後的主題）：" in lines
    assert any(line.split()[:2] == ["erp", "ERP"] for line in lines)
    assert lines[-1] == f"已寫入 {out}"
    environ = {"RADAR_CONFIG_DIR": str(env)}
    workbook = parse_annotation_workbook(
        out.read_bytes(), load_taxonomy(environ), load_context_values(environ)
    )
    assert workbook.annotator is Annotator.A
    assert len(workbook.papers) == 9
    sheet = read_workbook(out.read_bytes())[PAPERS_SHEET]
    header = list(sheet[0])
    for row in sheet[1:]:
        values = dict(zip(header, row, strict=False))
        assert all(values[name] for name in ("paper_id", "doi", "title", "split"))
    # 輸出與日誌不帶標題與摘要。
    assert "Annotation study" not in result.output
    assert "Made-up abstract" not in result.output
    [log_line] = [line for line in _logs(result) if "標註抽樣" in line]
    assert '"seed": 7' in log_line


def test_same_seed_writes_identical_files(
    env: Path, papers: list[uuid.UUID], tmp_path: Path
) -> None:
    first, second = tmp_path / "A.xlsx", tmp_path / "A2.xlsx"
    assert _template(first).exit_code == 0
    assert _template(second).exit_code == 0
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    "args",
    [
        ["template", "--out", "X.xlsx"],  # 沒有 --seed
        ["template", "--seed", "1", "--out", "X.xlsx", "--interest", "blockchain"],
        ["template", "--seed", "1", "--out", "X.xlsx", "--interest", "survey_sem"],
        ["template", "--seed", "1", "--out", "X.xlsx", "--abstract-count", "-1"],
        ["template", "--seed", "1", "--out", "X.xlsx", "--classification-count", "-5"],
        [
            "template",
            "--seed",
            "1",
            "--out",
            "X.xlsx",
            "--extend",
            "A.xlsx",
            "--abstract-count",
            "0",
            "--classification-count",
            "0",
        ],
        ["template", "--seed", "1", "--out", "X.xlsx", "--extend", "A.xlsx"],  # 補抽預設 0／0
        ["template-b", "--from", "A.xlsx", "--seed", "1", "--out", "X.xlsx", "--count", "0"],
        ["template-b", "--from", "A.xlsx", "--out", "X.xlsx"],  # 沒有 --seed
    ],
)
def test_bad_arguments_exit_2_without_database(
    offline_env: Path, tmp_path: Path, args: list[str]
) -> None:
    result = _run(*(str(tmp_path / arg) if arg.endswith(".xlsx") else arg for arg in args))
    assert result.exit_code == 2, result.output
    assert not (tmp_path / "X.xlsx").exists()


def test_unreachable_database_exits_1(offline_env: Path, tmp_path: Path) -> None:
    result = _template(tmp_path / "A.xlsx")
    assert result.exit_code == 1, result.output
    assert "產生試算表失敗（" in result.output
    assert not (tmp_path / "A.xlsx").exists()
    assert "nobody" not in result.output


def test_existing_output_is_not_overwritten(
    env: Path, papers: list[uuid.UUID], tmp_path: Path
) -> None:
    out = tmp_path / "A.xlsx"
    out.write_bytes(b"keep me")

    result = _template(out)

    assert result.exit_code == 1
    assert "輸出檔已經存在，不覆蓋" in result.output
    assert out.read_bytes() == b"keep me"


def test_write_race_is_caught_by_exclusive_create(
    env: Path, papers: list[uuid.UUID], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "A.xlsx"

    def build_then_someone_writes(
        rows: Sequence[AnnotationRow], taxonomy: Taxonomy, context_values: ContextValues
    ) -> bytes:
        data = build_annotator_a_workbook(rows, taxonomy, context_values)
        out.write_bytes(b"written meanwhile")
        return data

    monkeypatch.setattr(cli, "build_annotator_a_workbook", build_then_someone_writes)
    result = _template(out)
    assert result.exit_code == 1
    assert out.read_bytes() == b"written meanwhile"


def test_ctrl_c_exits_130(
    env: Path, papers: list[uuid.UUID], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "prepare_annotation_sample", interrupted)
    result = _template(tmp_path / "A.xlsx")
    assert result.exit_code == 130
    assert not (tmp_path / "A.xlsx").exists()

    monkeypatch.setattr(cli, "parse_annotation_workbook", interrupted)
    (tmp_path / "B.xlsx").write_bytes(b"x")
    assert _run("check", str(tmp_path / "B.xlsx")).exit_code == 130


def test_extend_adds_new_papers_only(env: Path, papers: list[uuid.UUID], tmp_path: Path) -> None:
    first, extended = tmp_path / "A.xlsx", tmp_path / "A2.xlsx"
    assert _template(first).exit_code == 0

    result = _run(
        "template",
        "--extend",
        str(first),
        "--abstract-count",
        "2",
        "--seed",
        "7",
        "--out",
        str(extended),
    )

    assert result.exit_code == 0, result.output
    assert "既有的試算表：9 篇（照原順序保留）" in _stdout(result)
    old = read_sampled_papers(first.read_bytes()).rows
    new = read_sampled_papers(extended.read_bytes()).rows
    assert len(new) == 11
    old_abstract = [row for row in old if row.sample_set is SampleSet.ABSTRACT]
    new_abstract = [row for row in new if row.sample_set is SampleSet.ABSTRACT]
    assert new_abstract[:4] == old_abstract
    assert [row for row in new if row.sample_set is SampleSet.CLASSIFICATION_ONLY] == [
        row for row in old if row.sample_set is SampleSet.CLASSIFICATION_ONLY
    ]
    added = new_abstract[4:]
    assert {row.paper_id for row in added}.isdisjoint(row.paper_id for row in old)
    assert {row.doi for row in added}.isdisjoint(row.doi for row in old)


def test_extend_is_blocked_once_annotation_started(
    env: Path, papers: list[uuid.UUID], tmp_path: Path
) -> None:
    first, extended = tmp_path / "A.xlsx", tmp_path / "A2.xlsx"
    assert _template(first).exit_code == 0
    sheets = {
        name: [list(row) for row in rows]
        for name, rows in read_workbook(first.read_bytes()).items()
    }
    status = sheets[PAPERS_SHEET][0].index("status")
    sheets[PAPERS_SHEET][1][status] = "done"
    started = tmp_path / "started.xlsx"
    started.write_bytes(build_excel_style_workbook(sheets, positional=frozenset()))

    result = _run(
        "template",
        "--extend",
        str(started),
        "--abstract-count",
        "1",
        "--seed",
        "7",
        "--out",
        str(extended),
    )

    assert result.exit_code == 1
    assert "已經開始標註" in result.output
    assert not extended.exists()


def test_extend_with_unreadable_file_exits_1(env: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a workbook")
    result = _run(
        "template", "--extend", str(bad), "--abstract-count", "1", "--seed", "1", "--out", "o.xlsx"
    )
    assert result.exit_code == 1
    assert "看不懂" in result.output
    missing = _run(
        "template",
        "--extend",
        str(tmp_path / "missing.xlsx"),
        "--abstract-count",
        "1",
        "--seed",
        "1",
        "--out",
        str(tmp_path / "o.xlsx"),
    )
    assert missing.exit_code == 1


def test_template_b_from_a(env: Path, papers: list[uuid.UUID], tmp_path: Path) -> None:
    a_file, b_file = tmp_path / "A.xlsx", tmp_path / "B.xlsx"
    assert _template(a_file).exit_code == 0

    result = _run("template-b", "--from", str(a_file), "--seed", "3", "--out", str(b_file))

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert lines[0].startswith("seed 3：從摘要層級 4 篇抽 4 篇給標註者 B")
    assert "摘要層級只有 4 篇，少於要的 15 篇，全部放進來" in lines
    environ = {"RADAR_CONFIG_DIR": str(env)}
    workbook = parse_annotation_workbook(
        b_file.read_bytes(), load_taxonomy(environ), load_context_values(environ)
    )
    assert workbook.annotator is Annotator.B
    a_rows = read_sampled_papers(a_file.read_bytes()).rows
    abstract_ids = {row.paper_id for row in a_rows if row.sample_set is SampleSet.ABSTRACT}
    assert {paper.paper_id for paper in workbook.papers} == abstract_ids
    header = read_workbook(b_file.read_bytes())[PAPERS_SHEET][0]
    assert not {"split", "set", "full_text"} & set(header)
    assert {"is_review_or_meta_analysis", "country", "limitations"} <= set(header)
    assert "Annotation study" not in result.output

    again = _run("template-b", "--from", str(a_file), "--seed", "3", "--out", str(b_file))
    assert again.exit_code == 1  # 輸出已存在


def test_check_reports_counts_or_problems(
    env: Path, papers: list[uuid.UUID], tmp_path: Path
) -> None:
    a_file = tmp_path / "A.xlsx"
    assert _template(a_file).exit_code == 0

    ok = _run("check", str(a_file))
    assert ok.exit_code == 0, ok.output
    assert _stdout(ok) == [
        "標註者 A：沒有問題",
        "摘要層級：已標 0 篇、還沒標 4 篇",
        "分類專用：已標 0 篇、還沒標 5 篇",
        "構面 0 列、假設 0 列",
    ]

    sheets = {
        name: [list(row) for row in rows]
        for name, rows in read_workbook(a_file.read_bytes()).items()
    }
    status = sheets[PAPERS_SHEET][0].index("status")
    sheets[PAPERS_SHEET][1][status] = "SECRET-VALUE"
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(build_excel_style_workbook(sheets, positional=frozenset()))

    bad = _run("check", str(broken))
    assert bad.exit_code == 1
    assert _stdout(bad)[:2] == [
        "標註試算表有 1 個問題：",
        f"- 論文 第 2 列 status（{chr(ord('A') + status)}）：不在下拉清單裡",
    ]
    assert "SECRET-VALUE" not in bad.output

    junk = tmp_path / "junk.xlsx"
    junk.write_bytes(b"junk")
    not_xlsx = _run("check", str(junk))
    assert not_xlsx.exit_code == 1
    assert _stdout(not_xlsx)[0] == "標註試算表有 1 個問題："
    assert _run("check", str(tmp_path / "missing.xlsx")).exit_code == 1
    with pytest.raises(AnnotationParseError):
        read_sampled_papers(junk.read_bytes())


# --- 審查後補的測試 ---------------------------------------------------------------------------


def _a_file(tmp_path: Path, env: Path, sample_sets: Sequence[SampleSet]) -> Path:
    """不連資料庫、直接用 domain 產生一份 A 試算表（論文是編的）。"""
    environ = {"RADAR_CONFIG_DIR": str(env)}
    rows = [
        AnnotationRow(
            paper_id=uuid.UUID(int=number),
            dois=(f"10.5555/cli.{number:04d}",),
            title=f"Made-up {number}",
            abstract="Made-up abstract.",
            split=split_for_dois((f"10.5555/cli.{number:04d}",)),
            sample_set=sample_set,
        )
        for number, sample_set in enumerate(sample_sets, start=1)
    ]
    path = tmp_path / "A-made.xlsx"
    path.write_bytes(
        build_annotator_a_workbook(rows, load_taxonomy(environ), load_context_values(environ))
    )
    return path


def test_template_b_without_abstract_rows_exits_1(offline_env: Path, tmp_path: Path) -> None:
    a_file = _a_file(tmp_path, offline_env, [SampleSet.CLASSIFICATION_ONLY] * 2)
    b_file = tmp_path / "B.xlsx"

    result = _run("template-b", "--from", str(a_file), "--seed", "3", "--out", str(b_file))

    assert result.exit_code == 1
    assert "沒有摘要層級的列" in result.output
    assert not b_file.exists()


def test_missing_output_folder_exits_1_before_database(offline_env: Path, tmp_path: Path) -> None:
    out = tmp_path / "no-such-folder" / "A.xlsx"

    result = _template(out)

    assert result.exit_code == 1
    assert "輸出檔的資料夾不存在" in result.output
    assert "產生試算表失敗" not in result.output  # 沒有走到連資料庫

    a_file = _a_file(tmp_path, offline_env, [SampleSet.ABSTRACT])
    b_result = _run("template-b", "--from", str(a_file), "--seed", "3", "--out", str(out))
    assert b_result.exit_code == 1
    assert "輸出檔的資料夾不存在" in b_result.output


class _FailingFile:
    """寫一小段之後丟例外的檔案（包住真的檔，關檔照常）。"""

    def __init__(self, file: IO[bytes], error: BaseException) -> None:
        self._file = file
        self._error = error

    def __enter__(self) -> _FailingFile:
        return self

    def __exit__(self, *args: object) -> None:
        self._file.close()

    def write(self, data: bytes) -> int:
        self._file.write(data[:10])
        raise self._error


@pytest.mark.parametrize(
    ("error", "exit_code"), [(OSError("disk full"), 1), (KeyboardInterrupt(), 130)]
)
def test_failed_write_removes_partial_file(
    offline_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    exit_code: int,
) -> None:
    a_file = _a_file(tmp_path, offline_env, [SampleSet.ABSTRACT] * 2)
    b_file = tmp_path / "B.xlsx"
    real_open = Path.open

    def open_failing(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        file = real_open(self, mode, *args, **kwargs)
        return _FailingFile(file, error) if mode == "xb" else file

    monkeypatch.setattr(Path, "open", open_failing)
    result = _run("template-b", "--from", str(a_file), "--seed", "3", "--out", str(b_file))

    assert result.exit_code == exit_code
    assert not b_file.exists()


def test_template_default_counts(env: Path, papers: list[uuid.UUID], tmp_path: Path) -> None:
    result = _run("template", "--seed", "7", "--out", str(tmp_path / "A.xlsx"))

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert any(line.startswith("摘要層級：") and "要 50 篇" in line for line in lines)
    assert any(line.startswith("分類專用：") and "要 100 篇" in line for line in lines)


# --- Opus 審查後補的測試 ----------------------------------------------------------------------


def test_too_long_dropdown_fails_before_database(
    offline_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {"RADAR_CONFIG_DIR": str(offline_env)}
    context = load_context_values(environ)
    designs = tuple(
        item.model_copy(update={"key": f"research_design_option_{index:02d}"})
        for index, item in enumerate(context.research_designs * 2)
    )
    long_context = context.model_copy(update={"research_designs": designs})
    length = len(",".join(item.key for item in designs))
    assert length > 255
    a_file = _a_file(tmp_path, offline_env, [SampleSet.ABSTRACT])  # 用原本的清單先產生 A

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("不該抽樣或連資料庫")

    monkeypatch.setattr(cli, "load_context_values", lambda *args: long_context)
    monkeypatch.setattr(cli, "prepare_annotation_sample", must_not_run)
    expected = f"論文表的 research_design 欄：下拉選項合起來 {length} 個字元，超過 Excel 的上限 255"

    result = _template(tmp_path / "A.xlsx")
    assert result.exit_code == 1, result.output
    assert expected in result.output
    assert not (tmp_path / "A.xlsx").exists()

    b_result = _run(
        "template-b", "--from", str(a_file), "--seed", "3", "--out", str(tmp_path / "B.xlsx")
    )
    assert b_result.exit_code == 1, b_result.output
    assert expected in b_result.output
    assert not (tmp_path / "B.xlsx").exists()
