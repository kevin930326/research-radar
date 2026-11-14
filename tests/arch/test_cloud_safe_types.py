"""mypy 會擋下「把 `str` 傳給 port」（AD-9、ADR-0029）。

用 `mypy.api.run`（`--config-file pyproject.toml`、cache 放 tmp、工作目錄是 repo 根目錄）檢查兩段
寫在這裡的程式片段：
- 錯的片段：I/O 表「port 傳 `str`」的五種寫法，各自在預期的那一行報 `arg-type`；
- 對的片段：先經過 `clear_for_cloud` 再傳，0 個錯誤。
"""

from pathlib import Path

import pytest
from mypy import api as mypy_api

ROOT = Path(__file__).resolve().parents[2]

HEADER = """\
from radar.domain.llm import LlmRequest
from radar.domain.policy import CloudPolicy, ContentSource, clear_for_cloud
from radar.ports import EmbeddingProvider, LlmProvider

POLICY = CloudPolicy()
TASK = clear_for_cloud("請整理。", ContentSource.PROMPT_TEMPLATE, None, POLICY)
TITLE = clear_for_cloud("（假）標題", ContentSource.TITLE, None, POLICY)

"""

# 每一行的結尾註記「# 錯：說明」的那一行應該報 arg-type。
WRONG = (
    HEADER
    + """\
def wrong(llm: LlmProvider, embedder: EmbeddingProvider) -> None:
    llm.complete("文字")  # 錯：complete 傳 str
    LlmRequest(model="m", system="文字", content=(TITLE,), max_tokens=1)  # 錯：system 是 str
    embedder.embed_query("文字")  # 錯：embed_query 傳 str
    documents = ["文字"]
    embedder.embed_documents(documents)  # 錯：embed_documents 傳 list[str]
    embedder.embed_documents("文字")  # 錯：embed_documents 傳 str 本身
"""
)

RIGHT = (
    HEADER
    + """\
def right(llm: LlmProvider, embedder: EmbeddingProvider) -> None:
    request = LlmRequest(model="m", system=TASK, content=(TITLE,), max_tokens=1)
    llm.complete(request)
    embedder.embed_query(TITLE)
    embedder.embed_documents([TITLE, TASK])
    embedder.embed_documents((TITLE,))
"""
)


def _expected_lines(source: str) -> list[int]:
    return [number for number, line in enumerate(source.splitlines(), 1) if "# 錯：" in line]


def _run_mypy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], int]:
    (tmp_path / "wrong_snippet.py").write_text(WRONG, encoding="utf-8")
    (tmp_path / "right_snippet.py").write_text(RIGHT, encoding="utf-8")
    monkeypatch.chdir(ROOT)
    stdout, stderr, status = mypy_api.run(
        [
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            "--no-error-summary",
            "--show-error-codes",
            "--no-pretty",
            str(tmp_path / "wrong_snippet.py"),
            str(tmp_path / "right_snippet.py"),
        ]
    )
    assert stderr == "", stderr
    return [line for line in stdout.splitlines() if line.strip()], status


def test_mypy_rejects_str_passed_to_ports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines, status = _run_mypy(tmp_path, monkeypatch)

    errors = [line for line in lines if ": error: " in line]
    wrong = sorted(
        int(line.split(":")[1])
        for line in errors
        if Path(line.split(":")[0]).name == "wrong_snippet.py"
    )
    right = [line for line in errors if Path(line.split(":")[0]).name == "right_snippet.py"]
    others = [line for line in errors if line not in right and "wrong_snippet.py" not in line]

    assert status == 1
    assert right == []
    assert others == []
    assert wrong == _expected_lines(WRONG)
    assert all(line.endswith("[arg-type]") for line in errors), errors
    assert len(_expected_lines(WRONG)) == 5
