"""登出的「已登出」日誌在交易 commit 之後才記（commit 失敗時那一列還在，不能記成已登出）。

用假的 `Database` 與替換掉的 repo 函式，不連資料庫；真資料庫的登出流程在
`tests/integration/test_sessions.py`。
"""

import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import pytest
from structlog.testing import capture_logs

import radar.services.auth as auth_service
from radar.domain.auth import SessionOwner

TOKEN = "t" * 43
MEMBER_ID = uuid.UUID("00000000-0000-7000-8000-00000000000a")
SESSION_ID = uuid.UUID("00000000-0000-7000-8000-0000000000aa")


class CommitFailedError(Exception):
    """假的 commit 失敗（例如連線在 commit 時斷了）。"""


class FakeDatabase:
    """`transaction()` 結束時記下 commit 當下已經有的日誌；`fail_commit` 時在 commit 丟錯。"""

    def __init__(self, logs: list[MutableMapping[str, Any]], *, fail_commit: bool = False) -> None:
        self._logs = logs
        self._fail_commit = fail_commit
        self.logs_at_commit: list[str] | None = None

    @contextmanager
    def transaction(self) -> Iterator[object]:
        yield object()
        self.logs_at_commit = [entry["event"] for entry in self._logs]
        if self._fail_commit:
            raise CommitFailedError


@pytest.fixture
def deleted(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """替換掉查 session 與刪 session 的 repo 函式；回刪過的 session ID。"""
    removed: list[uuid.UUID] = []

    def find_session_owner(_connection: object, _digest: bytes) -> SessionOwner:
        return SessionOwner(session_id=SESSION_ID, member_id=MEMBER_ID, is_admin=False)

    def delete_session(_connection: object, *, session_id: uuid.UUID, member_id: uuid.UUID) -> bool:
        assert member_id == MEMBER_ID
        removed.append(session_id)
        return True

    monkeypatch.setattr(auth_service, "find_session_owner", find_session_owner)
    monkeypatch.setattr(auth_service, "delete_session", delete_session)
    return removed


def test_logged_out_is_logged_after_commit(deleted: list[uuid.UUID]) -> None:
    with capture_logs() as logs:
        database = FakeDatabase(logs)
        auth_service.log_out_session(database, TOKEN)  # type: ignore[arg-type]

    assert deleted == [SESSION_ID]
    assert database.logs_at_commit == []  # commit 的那一刻還沒記
    assert [entry["event"] for entry in logs] == ["已登出"]
    assert logs[0]["member_id"] == str(MEMBER_ID)


def test_failed_commit_does_not_log_logged_out(deleted: list[uuid.UUID]) -> None:
    with capture_logs() as logs:
        database = FakeDatabase(logs, fail_commit=True)
        with pytest.raises(CommitFailedError):
            auth_service.log_out_session(database, TOKEN)  # type: ignore[arg-type]

    assert deleted == [SESSION_ID]
    assert [entry["event"] for entry in logs] == []


@pytest.mark.parametrize("found", [True, False])
def test_end_session_reports_whether_it_deleted(
    monkeypatch: pytest.MonkeyPatch, found: bool
) -> None:
    def delete_session(_connection: object, *, session_id: uuid.UUID, member_id: uuid.UUID) -> bool:
        return found

    monkeypatch.setattr(auth_service, "delete_session", delete_session)
    owner = SessionOwner(session_id=SESSION_ID, member_id=MEMBER_ID, is_admin=False)
    with capture_logs() as logs:
        ended = auth_service.end_session(owner.viewer, object(), session_id=SESSION_ID)  # type: ignore[arg-type]

    assert ended is found
    assert logs == []  # end_session 自己不記日誌；由呼叫端在 commit 之後記
