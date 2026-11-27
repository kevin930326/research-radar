"""找論文清單 API 的搜尋與找相似論文（Story 4.1 PR B）的 domain：下一頁的位置（名次位移）、一頁的
形狀、錯誤代碼的 HTTP 狀態、api 啟動時要有查詢向量自己的時限。

- 檢索的位置只收自己產生的寫法，而且要是同一種檢索（搜尋／找相似論文）；清單的 keyset 位置一定不收，
  反過來清單也不收檢索的位置（互相誤用是 422，不是 500）。
"""

import base64
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import radar.entrypoints.api.main as api_main
from radar.domain.errors import ErrorCode
from radar.domain.paper_list import (
    PaperListCursor,
    decode_paper_list_cursor,
    encode_paper_list_cursor,
)
from radar.domain.retrieval import (
    MAX_RETRIEVAL_OFFSET,
    PaperSearchPage,
    RetrievalCursor,
    RetrievalMode,
    decode_retrieval_cursor,
    encode_retrieval_cursor,
)
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.entrypoints.api.problems import PROBLEM_STATUS

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", list(RetrievalMode))
@pytest.mark.parametrize("offset", [1, 20, 199, MAX_RETRIEVAL_OFFSET])
def test_retrieval_cursor_round_trips(mode: RetrievalMode, offset: int) -> None:
    cursor = RetrievalCursor(mode=mode, offset=offset)
    encoded = encode_retrieval_cursor(cursor)

    assert decode_retrieval_cursor(encoded, mode=mode) == cursor
    assert "=" not in encoded


def test_search_cursor_is_not_a_similar_cursor_and_the_reverse() -> None:
    search = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SEARCH, offset=20))
    similar = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SIMILAR, offset=20))

    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_retrieval_cursor(search, mode=RetrievalMode.SIMILAR)
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_retrieval_cursor(similar, mode=RetrievalMode.SEARCH)


@pytest.mark.parametrize("year", [2020, None])
def test_list_cursor_and_retrieval_cursor_never_decode_as_each_other(year: int | None) -> None:
    listed = encode_paper_list_cursor(PaperListCursor(year=year, paper_id=uuid.uuid4()))
    ranked = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SEARCH, offset=20))

    for mode in RetrievalMode:
        with pytest.raises(ValueError, match="下一頁的位置"):
            decode_retrieval_cursor(listed, mode=mode)
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_paper_list_cursor(ranked)


def _b64(plain: str) -> str:
    return base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-cursor",
        _b64("search:0"),  # 第一頁沒有位置
        _b64("search:-1"),
        _b64(f"search:{MAX_RETRIEVAL_OFFSET + 1}"),
        _b64("search:01"),  # 重新編碼不一樣
        _b64("search:+5"),
        _b64("search: 5"),
        _b64("search:5:5"),
        _b64("search:٥"),  # 不是 ASCII 的數字
        _b64("search:1e3"),
        _b64("other:5"),
        _b64("SEARCH:5"),
        _b64("search:5") + "==",
        "A" * 41,
    ],
)
def test_bad_retrieval_cursors_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_retrieval_cursor(value, mode=RetrievalMode.SEARCH)


@pytest.mark.parametrize("offset", [0, -1, MAX_RETRIEVAL_OFFSET + 1, True, 1.0])
def test_retrieval_cursor_offset_must_be_a_positive_int(offset: Any) -> None:
    with pytest.raises(ValueError, match="offset"):
        RetrievalCursor(mode=RetrievalMode.SEARCH, offset=offset)


def test_paper_search_page_checks_total_and_snapshot() -> None:
    created = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    published = Snapshot(
        id=uuid.uuid4(),
        number=1,
        weekly_run_id=uuid.uuid4(),
        status=SnapshotStatus.PUBLISHED,
        as_of=datetime(2026, 11, 16, 2, 30, tzinfo=UTC),
        created_at=created,
    )
    assert PaperSearchPage(rows=(), next_cursor=None, total=0, snapshot=published).total == 0
    with pytest.raises(ValueError, match="total"):
        PaperSearchPage(rows=(), next_cursor=None, total=-1, snapshot=None)
    building = Snapshot(
        id=uuid.uuid4(),
        number=2,
        weekly_run_id=uuid.uuid4(),
        status=SnapshotStatus.BUILDING,
        as_of=None,
        created_at=created,
    )
    with pytest.raises(ValueError, match="快照"):
        PaperSearchPage(rows=(), next_cursor=None, total=0, snapshot=building)


def test_search_errors_are_503_with_their_own_codes() -> None:
    assert PROBLEM_STATUS[ErrorCode.SEARCH_QUOTA_EXHAUSTED] == 503
    assert PROBLEM_STATUS[ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE] == 503
    assert ErrorCode.SEARCH_QUOTA_EXHAUSTED.value == "search_quota_exhausted"
    assert ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE.value == "search_embedding_unavailable"


class _ShouldNotBuildError(Exception):
    pass


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise _ShouldNotBuildError


def test_api_does_not_start_without_short_query_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # embeddings.yaml 拿掉 query_request：背景向量化照樣能讀（worker 不用它），api 不啟動。
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    path = target / "embeddings.yaml"
    text = path.read_text(encoding="utf-8")
    block = "query_request:\n  timeout_seconds: 2\n  total_timeout_seconds: 3\n  attempts: 2\n"
    assert block in text
    path.write_text(text.replace(block, ""), encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    assert "embeddings.yaml：query_request" in capsys.readouterr().out


def test_api_does_not_start_with_a_broken_retrieval_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # load_retrieval_config 接到 api 的啟動流程：retrieval.yaml 寫錯就不啟動。
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    path = target / "retrieval.yaml"
    text = path.read_text(encoding="utf-8")
    assert "rrf_k: 60" in text
    path.write_text(text.replace("rrf_k: 60", "rrf_k: 0"), encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    assert "retrieval.yaml：rrf_k" in capsys.readouterr().out
