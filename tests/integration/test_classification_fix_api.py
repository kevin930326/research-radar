"""修正分類的 API 與管理指令對真的 PostgreSQL（Story 3.11、FR-4、FR-16、NFR-16、ADR-0055）。

- 送出：主題與方法兩筆 `judgment_event`（同一個交易：`created_at` 相同、舊值是修正前的有效值）＋
  一筆稽核（同一個交易、同一個時間），立即生效（「已核對」、修正人數）；回應帶修正後的分類區塊與
  歷史。
- 兩欄一起寫或都不寫：寫第二欄時失敗、寫稽核時失敗，兩欄與稽核都不留。
- 排隊的兩次修正：時間是拿到鎖之後取的，歷史與有效值的新舊和實際寫入的順序一樣；選的一樣只收一次。
- 不收的值（不是分類表的 key、unclassified、放錯欄位、沒選、和目前一樣、理由超過 100 字、理由有
  換行或行分隔符、段落分隔符）一律 422，什麼都不寫；本文不回顯送來的值。分類沒有成功的論文先
  不收（等 Kevin 決定）。
- 看不到的論文（別人的私人論文、追蹤範圍外沒人加過的、被排除的）、不存在的、壞 ID：修正與讀歷史
  都是完全相同的 404，什麼都不寫；看不到的論文帶壞本文也是 404。
- 公開身分（示範站產生器）看得到論文也讀不到修正歷史。
- 歷史：新的在前、只有姓名，回應裡沒有 email；兩人意見不同時「意見不同」；同一個人改兩次是兩筆歷史、
  修正人數 1。沒有刪除或撤回的端點。
- 2.7 的測試用 API 再驗一次：修正後重跑分類（新的 active），論文頁的有效值仍然是人工值。
- 換人後寫入（`X-Radar-Expected-Member` 不符）409，什麼都不寫。
- `radar classification disagreements`：只列意見不同的論文，每位成員每一欄最新一次的選擇與各自的
  時間（台北時間）；沒有就印一行。

兩位成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。資料全是測試自己
編的（`*@example.org`、假標題）。
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
import radar.services.judgments as judgments_service
from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.classification_fixes import (
    ClassificationFixInvalidError,
    ClassificationFixProblem,
)
from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.library import AddedVia
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import EXPECTED_MEMBER_HEADER, SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event, list_classification_fix_events
from radar.repo.library import create_library_item
from radar.repo.papers import find_paper_page_facts
from radar.services.judgments import fix_classification, get_classification_fixes
from radar.services.members import allow_member, set_member_admin
from tests.backup_support import count_in_snapshot, publish_snapshot
from tests.database import ROOT, TestDatabase, migrate
from tests.integration.test_effective_views import insert_classification
from tests.llm_support import (
    create_hidden_paper,
    create_paper,
    execute,
    library_routes,
    paper_list_routes,
    paper_page_setup,
    paper_routes,
)

DEV_ORIGIN = "http://localhost:8000"
NOW = datetime(2026, 11, 21, 4, 0, tzinfo=UTC)
OWNER_EMAIL = "fix.owner@example.org"
OTHER_EMAIL = "fix.other@example.org"
NOT_FOUND = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}
REQUEST_INVALID = {
    "type": "about:blank",
    "title": "Unprocessable Content",
    "status": 422,
    "code": "request_invalid",
}
NEW = {"topic": ["e_commerce", "digital_platforms"], "method": ["experiment"]}


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


class Api:
    """真的 app（正式的 `install_api_routes`）＋兩位登入的成員（甲是 owner、乙是 other）。"""

    def __init__(self, database: Database) -> None:
        settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
        app = create_app(readiness_check=lambda: True)
        install_api_routes(
            app,
            auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW),
            paper_routes=paper_routes(database, now=NOW),
            library_routes=library_routes(database),
            paper_list_routes=paper_list_routes(database),
        )
        self.client = TestClient(app, base_url="https://testserver")
        self.owner_id, self.owner_token = self._log_in(database, OWNER_EMAIL, "測試甲")
        self.other_id, self.other_token = self._log_in(database, OTHER_EMAIL, "測試乙")

    def _log_in(self, database: Database, email: str, name: str) -> tuple[uuid.UUID, str]:
        member_id = allow_member(SystemViewer(), database, email=email, display_name=name).member_id
        response = self.client.post(
            "/api/v1/auth/fake-login",
            json={"member_id": str(member_id)},
            headers={"Origin": DEV_ORIGIN},
        )
        assert response.status_code == 200, response.text
        self.client.cookies.clear()
        name_value, _, rest = response.headers["set-cookie"].partition("=")
        assert name_value == SESSION_COOKIE
        return member_id, rest.split(";", 1)[0]

    def send(
        self,
        method: str,
        path: str,
        token: str,
        *,
        body: Any = None,
        expected: str | None = None,
    ) -> Response:
        self.client.cookies.clear()
        headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": DEV_ORIGIN}
        if expected is not None:
            headers[EXPECTED_MEMBER_HEADER] = expected
        return self.client.request(method, path, headers=headers, json=body)

    def fix(self, paper_id: object, token: str, body: Any = None, **kw: Any) -> Response:
        return self.send(
            "POST",
            f"/api/v1/papers/{paper_id}/classification-fixes",
            token,
            body=NEW if body is None else body,
            **kw,
        )

    def history(self, paper_id: object, token: str) -> Response:
        return self.send("GET", f"/api/v1/papers/{paper_id}/classification-fixes", token)

    def paper(self, paper_id: object, token: str) -> Response:
        return self.send("GET", f"/api/v1/papers/{paper_id}", token)


@pytest.fixture
def api(database: Database) -> Api:
    return Api(database)


@pytest.fixture
def paper_id(db: TestDatabase, database: Database) -> uuid.UUID:
    """一篇看得到的論文，AI 分類是主題 e_commerce、方法 experiment（設計稿的「正常狀態」）。"""
    paper = create_paper(database, title="Fake Paper on Rating Signals")
    insert_classification(db, paper, ["e_commerce"], ["experiment"])
    return paper


def _events(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT kind, member_id, actor_role, target_type, target_id, field, old_value, new_value,"
        " reason, created_at FROM judgment_event ORDER BY field",
    )


def _audits(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT actor_member_id, actor_role, action, target_type, target_id, detail, created_at"
        " FROM audit_event WHERE action = 'correct_classification'",
    )


def _comparable(response: Response) -> tuple[int, bytes, dict[str, str]]:
    headers = {k: v for k, v in response.headers.items() if k.lower() != REQUEST_ID_HEADER.lower()}
    return response.status_code, response.content, headers


# --- 送出 -------------------------------------------------------------------------------------


def test_fix_writes_both_fields_and_the_audit_in_one_transaction(
    api: Api, db: TestDatabase, paper_id: uuid.UUID
) -> None:
    response = api.fix(paper_id, api.owner_token, {**NEW, "reason": " 研究的是跨平台的評分 "})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    value = body["classification"]["value"]
    assert value["topic"]["keys"] == ["digital_platforms", "e_commerce"]
    assert value["topic"]["human_verified"] is True
    assert value["method"] == {
        "keys": ["experiment"],
        "human_verified": True,
        "fix_member_count": 1,
        "members_disagree": False,
    }
    assert value["fix_member_count"] == 1
    assert body["classification"]["provenance"]["human_verified"] is True
    [entry] = body["history"]
    assert entry["member_name"] == "測試甲"
    assert entry["topic"] == ["digital_platforms", "e_commerce"]
    assert entry["method"] == ["experiment"]
    assert entry["reason"] == "研究的是跨平台的評分"

    events = _events(db)
    assert [(e[5], e[6], e[7], e[8]) for e in events] == [
        ("method", ["experiment"], ["experiment"], "研究的是跨平台的評分"),
        ("topic", ["e_commerce"], ["digital_platforms", "e_commerce"], "研究的是跨平台的評分"),
    ]
    assert {(e[0], e[1], e[2], e[3], e[4]) for e in events} == {
        ("classification_fix", api.owner_id, "member", "paper", paper_id)
    }
    [audit] = _audits(db)
    assert audit[:5] == (api.owner_id, "member", "correct_classification", "paper", paper_id)
    assert audit[5]["topic"] == ["digital_platforms", "e_commerce"]
    assert audit[5]["method"] == ["experiment"]
    assert len(audit[5]["judgment_event_ids"]) == 2
    assert "研究的是" not in str(audit[5])
    # 同一次送出：拿到鎖之後取一次 clock_timestamp()，兩筆事件與稽核共用，三筆一樣。
    assert {events[0][9], events[1][9], audit[6]} == {events[0][9]}

    # 立即生效：論文頁也是「已核對」。
    page = api.paper(paper_id, api.other_token).json()
    assert page["classification"]["provenance"]["human_verified"] is True
    assert page["classification"]["value"]["topic"]["keys"] == ["digital_platforms", "e_commerce"]


def test_an_admin_fix_is_recorded_as_admin(
    api: Api, db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    set_member_admin(SystemViewer(), database, email=OWNER_EMAIL)

    assert api.fix(paper_id, api.owner_token).status_code == 200

    assert {e[2] for e in _events(db)} == {"admin"}
    assert [a[1] for a in _audits(db)] == ["admin"]


def test_a_failure_on_the_second_field_writes_nothing(
    db: TestDatabase, database: Database, paper_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = allow_member(
        SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲"
    ).member_id
    real = create_judgment_event
    calls: list[int] = []

    def second_fails(connection: Any, event: Any, **kw: Any) -> uuid.UUID | None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("寫第二欄時失敗")
        return real(connection, event, **kw)

    monkeypatch.setattr(judgments_service, "create_judgment_event", second_fails)

    with pytest.raises(RuntimeError, match="第二欄"):
        fix_classification(
            MemberViewer(member_id=member, is_admin=False),
            database,
            paper_page_setup(),
            paper_id,
            topic=NEW["topic"],
            method=NEW["method"],
            reason=None,
            now=NOW,
        )

    assert len(calls) == 2
    assert _events(db) == []
    assert _audits(db) == []


def test_a_failure_writing_the_audit_rolls_back_both_fields(
    db: TestDatabase, database: Database, paper_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 稽核和兩欄同一個交易（`create_audit_event` 用傳進來的連線、不自己 commit）：稽核寫入丟例外時，
    # 已經寫進去的兩欄也退回。用替身丟例外，不動資料庫。
    member = allow_member(
        SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲"
    ).member_id
    written: list[uuid.UUID | None] = []
    real_event = create_judgment_event

    def counting_event(connection: Any, event: Any, **kw: Any) -> uuid.UUID | None:
        written.append(real_event(connection, event, **kw))
        return written[-1]

    def audit_fails(connection: Any, event: Any, **kw: Any) -> uuid.UUID:
        raise RuntimeError("寫稽核時失敗")

    monkeypatch.setattr(judgments_service, "create_judgment_event", counting_event)
    monkeypatch.setattr(judgments_service, "create_audit_event", audit_fails)

    with pytest.raises(RuntimeError, match="稽核"):
        fix_classification(
            MemberViewer(member_id=member, is_admin=False),
            database,
            paper_page_setup(),
            paper_id,
            topic=NEW["topic"],
            method=NEW["method"],
            reason=None,
            now=NOW,
        )

    # 兩欄在丟例外之前都寫進去過（交易裡），交易退回後一筆都不留。
    assert len(written) == 2
    assert all(event_id is not None for event_id in written)
    assert _events(db) == []
    assert _audits(db) == []


@pytest.mark.parametrize(
    "body",
    [
        {"topic": ["unclassified"], "method": ["experiment"]},
        {"topic": ["experiment"], "method": ["experiment"]},
        {"topic": ["e_commerce"], "method": ["e_commerce"]},
        {"topic": ["secret_unknown_topic"], "method": ["experiment"]},
        {"topic": [], "method": ["survey_sem"]},
        {"topic": ["erp"], "method": []},
        {"topic": ["erp", "erp"], "method": ["experiment"]},
        {"topic": ["e_commerce"], "method": ["experiment"]},  # 和目前一樣
        {"topic": ["erp"], "method": ["experiment"], "reason": "秘" * 101},
        {"topic": ["erp"], "method": ["experiment"], "reason": "第一行\nsecret 第二行"},
        {"topic": ["erp"], "method": ["experiment"], "reason": "第一行\u2028secret 第二行"},
        {"topic": ["erp"], "method": ["experiment"], "reason": "第一段\u2029secret 第二段"},
        {"topic": ["erp"], "method": ["experiment"], "reason": "秘" + "😀" * 100},
        {"topic": ["erp"], "method": ["experiment"], "extra": "secret"},
    ],
)
def test_rejected_values_are_422_and_write_nothing(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, body: dict[str, Any]
) -> None:
    response = api.fix(paper_id, api.owner_token, body)

    assert response.status_code == 422
    assert response.json() == REQUEST_INVALID
    assert "secret" not in response.text
    assert "秘" not in response.text
    assert _events(db) == []
    assert _audits(db) == []


@pytest.mark.parametrize("reason", ["字" * 100, "😀" * 100, " " + "字" * 100 + " "])
def test_reason_of_exactly_100_characters_is_kept(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, reason: str
) -> None:
    # 100 字算 code point（表情符號一個字；前端同一種算法），頭尾空白不算。101 字在上面的不收清單。
    response = api.fix(paper_id, api.owner_token, {**NEW, "reason": reason})

    assert response.status_code == 200
    assert {e[8] for e in _events(db)} == {reason.strip()}


def test_a_paper_without_an_ai_classification_is_rejected_until_kevin_decides(
    api: Api, db: TestDatabase, database: Database
) -> None:
    # 分類還沒有成功的結果（沒有 active）：能不能修正等 Kevin 決定（TASK 第 6 部分第 8 點）。決定前
    # 後端不收（422，什麼都不寫），和畫面照設計稿不給入口一致（triage T7；原本這條斷言
    # 「可以修正」）。
    paper = create_paper(database, doi="10.1/no-ai", openalex_id="W2")

    response = api.fix(paper, api.owner_token)

    assert response.status_code == 422
    assert response.json() == REQUEST_INVALID
    assert _events(db) == []
    assert _audits(db) == []


# --- 排隊的修正 --------------------------------------------------------------------------------

OTHER_PICK = {"topic": ["social_media_communities"], "method": ["experiment"]}
# 甲的交易佔一條連線時，乙還要讀預算與寫入。
QUEUE_POOL = 3


def _member_fix(
    database: Database, member_id: uuid.UUID, paper_id: uuid.UUID, pick: dict[str, Any]
) -> Any:
    return fix_classification(
        MemberViewer(member_id=member_id, is_admin=False),
        database,
        paper_page_setup(),
        paper_id,
        topic=pick["topic"],
        method=pick["method"],
        reason=None,
        now=NOW,
    )


def _jump_the_queue(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    member_id: uuid.UUID,
    paper_id: uuid.UUID,
    pick: dict[str, Any],
) -> list[Any]:
    """第一次拿修正分類的鎖之前，乙在另一條連線上整個做完一次修正並提交：甲的交易已經開始
    （查過看得到這篇），乙比甲先拿到鎖。回乙的結果。"""
    real_lock = Database.transaction_lock
    other: list[Any] = []
    started: list[bool] = []

    def lock_after_other(connection: Any, key: int) -> None:
        if not started:
            started.append(True)  # 乙自己拿鎖時走真的鎖
            other.append(_member_fix(database, member_id, paper_id, pick))
        real_lock(connection, key)

    monkeypatch.setattr(Database, "transaction_lock", staticmethod(lock_after_other))
    return other


def test_queued_fixes_take_effect_in_the_order_they_were_written(
    db: TestDatabase, paper_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 甲先開交易、乙先拿到鎖並提交、甲再拿到鎖（兩條資料庫連線）：甲的時間是拿到鎖之後取的，所以比乙
    # 新；歷史甲在前，目前的有效值是甲選的（用交易開始的 now() 時，甲會排在乙後面、有效值變成
    # 乙的）。
    database = Database(db.app_url, connect_timeout=5, pool_size=QUEUE_POOL)
    owner = allow_member(SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲")
    other = allow_member(SystemViewer(), database, email=OTHER_EMAIL, display_name="測試乙")
    jumped = _jump_the_queue(monkeypatch, database, other.member_id, paper_id, OTHER_PICK)

    result = _member_fix(database, owner.member_id, paper_id, NEW)

    assert jumped[0] is not None
    assert result is not None
    assert [e.member_name for e in result.history] == ["測試甲", "測試乙"]
    assert result.classification.value.topic.keys == ("digital_platforms", "e_commerce")
    rows = execute(
        db,
        "SELECT member_id, min(created_at), max(created_at) FROM judgment_event GROUP BY member_id",
    )
    times = {member: (low, high) for member, low, high in rows}
    assert times[owner.member_id][0] == times[owner.member_id][1]
    assert times[owner.member_id][0] > times[other.member_id][1]
    [(audit_at,)] = execute(
        db,
        "SELECT created_at FROM audit_event WHERE action = 'correct_classification'"
        " AND actor_member_id = %s",
        owner.member_id,
    )
    assert audit_at == times[owner.member_id][0]
    page = get_classification_fixes(
        MemberViewer(member_id=other.member_id, is_admin=False),
        database,
        paper_page_setup(),
        paper_id,
        now=NOW,
    )
    assert page is not None
    assert page.classification.value.topic.keys == ("digital_platforms", "e_commerce")  # type: ignore[union-attr]


def test_queued_fixes_with_the_same_pick_are_taken_once(
    db: TestDatabase, paper_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 兩人同時送一樣的：乙先拿到鎖寫進去，甲拿到鎖後讀到乙的修正，和目前一樣，不收、什麼都不寫。
    database = Database(db.app_url, connect_timeout=5, pool_size=QUEUE_POOL)
    owner = allow_member(SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲")
    other = allow_member(SystemViewer(), database, email=OTHER_EMAIL, display_name="測試乙")
    jumped = _jump_the_queue(monkeypatch, database, other.member_id, paper_id, NEW)

    with pytest.raises(ClassificationFixInvalidError) as caught:
        _member_fix(database, owner.member_id, paper_id, NEW)

    assert jumped[0] is not None
    assert caught.value.problem is ClassificationFixProblem.UNCHANGED
    assert {e[1] for e in _events(db)} == {other.member_id}
    assert len(_events(db)) == 2
    assert [a[0] for a in _audits(db)] == [other.member_id]


# --- 看不到的論文 ------------------------------------------------------------------------------


def test_fixing_or_reading_a_paper_you_cannot_see_is_the_same_404(
    api: Api, db: TestDatabase, database: Database
) -> None:
    hidden = create_hidden_paper(database, title="Fake hidden paper")
    insert_classification(db, hidden, ["e_commerce"], ["experiment"])
    with database.transaction() as connection:
        create_library_item(
            connection,
            SystemViewer(),
            owner_id=api.owner_id,
            paper_id=hidden,
            added_via=AddedVia.UPLOAD,
        )
    missing = uuid.uuid4()

    for request in (api.fix, api.history):
        responses = [
            request(hidden, api.other_token),
            request(missing, api.other_token),
            request("not-a-uuid", api.other_token),
        ]
        assert [r.status_code for r in responses] == [404, 404, 404]
        assert responses[0].json() == NOT_FOUND
        assert _comparable(responses[0]) == _comparable(responses[1]) == _comparable(responses[2])
    assert _events(db) == []
    assert _audits(db) == []
    # 對照：甲看得到自己文獻庫裡的這篇，可以修正。
    assert api.fix(hidden, api.owner_token).status_code == 200


def test_excluded_and_out_of_scope_papers_are_the_same_404(
    api: Api, db: TestDatabase, database: Database
) -> None:
    # 被排除的非研究文章（成員看得到、`excluded_reason` 有值）與追蹤範圍外、沒有人加進文獻庫的論文：
    # 讀歷史與修正都和不存在的完全相同的 404，什麼都不寫。
    excluded = create_paper(database, doi="10.1/excluded", openalex_id="W3", title="Fake editorial")
    insert_classification(db, excluded, ["e_commerce"], ["experiment"])
    execute(db, "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s", excluded)
    outside = create_hidden_paper(database, title="Fake paper nobody added")
    insert_classification(db, outside, ["e_commerce"], ["experiment"])
    missing = uuid.uuid4()

    for request in (api.fix, api.history):
        responses = [request(paper, api.owner_token) for paper in (excluded, outside, missing)]
        assert [r.status_code for r in responses] == [404, 404, 404]
        assert responses[0].json() == NOT_FOUND
        assert _comparable(responses[0]) == _comparable(responses[1]) == _comparable(responses[2])
    assert _events(db) == []
    assert _audits(db) == []


@pytest.mark.parametrize("body", [{"topic": "e_commerce"}, ["e_commerce"], {"extra": "secret"}])
def test_a_hidden_paper_with_a_bad_body_is_404_not_422(
    api: Api, db: TestDatabase, database: Database, body: Any
) -> None:
    # 錯誤的先後（ADR-0063）：先查論文看不看得到，再驗本文。看不到、不存在、壞 ID 帶壞本文都和
    # 帶好本文的看不到一樣是 404，分不出論文存不存在。
    hidden = create_hidden_paper(database, title="Fake hidden paper")
    insert_classification(db, hidden, ["e_commerce"], ["experiment"])

    responses = [
        api.fix(hidden, api.owner_token, body),
        api.fix(uuid.uuid4(), api.owner_token, body),
        api.fix("not-a-uuid", api.owner_token, body),
        api.fix(hidden, api.owner_token),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert all(_comparable(r) == _comparable(responses[-1]) for r in responses)
    assert _events(db) == []


def test_a_visible_paper_with_a_bad_body_is_422(
    api: Api, db: TestDatabase, paper_id: uuid.UUID
) -> None:
    response = api.fix(paper_id, api.owner_token, {"topic": "e_commerce"})

    assert response.status_code == 422
    assert response.json() == REQUEST_INVALID
    assert _events(db) == []


def test_the_public_viewer_sees_the_paper_but_never_the_history(
    api: Api, db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    # 示範站產生器的公開身分：論文算進已發佈的快照、看得到，但服務層與 repo 都不給修正歷史
    # （型別只收成員或維運身分，執行時也擋）。
    assert api.fix(paper_id, api.owner_token, {**NEW, "reason": "不給公開身分"}).status_code == 200
    count_in_snapshot(db, paper_id, publish_snapshot(db))
    with database.transaction() as connection:
        assert find_paper_page_facts(PublicViewer(), connection, paper_id) is not None

    with pytest.raises(TypeError, match="成員"):
        get_classification_fixes(
            PublicViewer(),  # type: ignore[arg-type]
            database,
            paper_page_setup(),
            paper_id,
            now=NOW,
        )
    with database.transaction() as connection, pytest.raises(TypeError, match="成員"):
        list_classification_fix_events(PublicViewer(), connection, [paper_id])  # type: ignore[arg-type]


def test_writing_after_the_member_changed_is_409_and_writes_nothing(
    api: Api, db: TestDatabase, paper_id: uuid.UUID
) -> None:
    response = api.fix(paper_id, api.owner_token, expected=str(api.other_id))

    assert response.status_code == 409
    assert response.json()["code"] == "member_changed"
    assert _events(db) == []
    assert _audits(db) == []


# --- 歷史 -------------------------------------------------------------------------------------


def test_history_shows_names_only_and_disagreement(api: Api, paper_id: uuid.UUID) -> None:
    assert (
        api.fix(
            paper_id,
            api.other_token,
            {"topic": ["social_media_communities"], "method": ["experiment"]},
        ).status_code
        == 200
    )
    assert api.fix(paper_id, api.owner_token, {**NEW, "reason": "跨平台"}).status_code == 200

    response = api.history(paper_id, api.other_token)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [(e["member_name"], e["topic"], e["reason"]) for e in body["history"]] == [
        ("測試甲", ["digital_platforms", "e_commerce"], "跨平台"),
        ("測試乙", ["social_media_communities"], None),
    ]
    assert body["history"][0]["fixed_at"].endswith("Z")
    assert set(body["history"][0]) == {"fixed_at", "member_name", "topic", "method", "reason"}
    for text in (OWNER_EMAIL, OTHER_EMAIL, "@example.org", str(api.owner_id), str(api.other_id)):
        assert text not in response.text
    value = body["classification"]["value"]
    assert value["fix_member_count"] == 2
    assert value["topic"]["members_disagree"] is True
    # 目前採用的是最新那一筆。
    assert value["topic"]["keys"] == ["digital_platforms", "e_commerce"]


def test_fixing_again_adds_history_and_cannot_be_deleted(api: Api, paper_id: uuid.UUID) -> None:
    assert api.fix(paper_id, api.owner_token).status_code == 200
    # 和剛剛自己的修正一樣：不收。
    assert api.fix(paper_id, api.owner_token).status_code == 422
    assert (
        api.fix(
            paper_id, api.owner_token, {"topic": ["e_commerce"], "method": ["survey_sem"]}
        ).status_code
        == 200
    )

    body = api.history(paper_id, api.owner_token).json()

    assert [e["method"] for e in body["history"]] == [["survey_sem"], ["experiment"]]
    assert body["classification"]["value"]["fix_member_count"] == 1
    assert body["classification"]["value"]["method"]["members_disagree"] is False
    # 沒有刪除或撤回：同一個路徑的 DELETE 是 405。
    deleted = api.send("DELETE", f"/api/v1/papers/{paper_id}/classification-fixes", api.owner_token)
    assert deleted.status_code == 405


def test_rerunning_the_classification_keeps_the_human_value(
    api: Api, db: TestDatabase, paper_id: uuid.UUID
) -> None:
    # Story 2.7 的測試用 API 再驗一次（AC）：修正後重跑分類（新的 active 取代舊的），有效值仍是
    # 人工值。
    assert api.fix(paper_id, api.owner_token).status_code == 200
    [(old_active,)] = execute(
        db, "SELECT id FROM classification WHERE paper_id = %s AND status = 'active'", paper_id
    )

    rerun = insert_classification(db, paper_id, ["erp"], ["survey_sem"])

    # 重跑的那一筆真的成為唯一的 active（不是舊的那一筆還留著），標籤是重跑的主題與方法。
    assert rerun != old_active
    assert execute(
        db, "SELECT id FROM classification WHERE paper_id = %s AND status = 'active'", paper_id
    ) == [(rerun,)]
    assert sorted(
        execute(
            db,
            "SELECT field, key FROM classification_label WHERE classification_id = %s",
            rerun,
        )
    ) == [("method", "survey_sem"), ("topic", "erp")]
    value = api.paper(paper_id, api.other_token).json()["classification"]
    assert value["value"]["topic"]["keys"] == ["digital_platforms", "e_commerce"]
    assert value["value"]["method"]["keys"] == ["experiment"]
    assert value["provenance"]["human_verified"] is True


# --- 管理指令 -----------------------------------------------------------------------------------


def _cli(db: TestDatabase, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(Path(ROOT) / "config"))
    return CliRunner().invoke(cli.app, ["classification", "disagreements"])


def test_disagreements_command_lists_each_members_latest_pick(
    api: Api, db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    differ = create_paper(database, doi="10.1/differ", openalex_id="W11", title="Fake Paper A")
    agree = create_paper(database, doi="10.1/agree", openalex_id="W12", title="Fake Paper B")
    insert_classification(db, differ, ["e_commerce"], ["experiment"])
    insert_classification(db, agree, ["e_commerce"], ["experiment"])
    assert api.fix(differ, api.other_token, OTHER_PICK).status_code == 200
    assert api.fix(differ, api.owner_token, {**NEW, "reason": "理由不印"}).status_code == 200
    # 只有一位修正過的不算意見不同（和目前一樣的不收，所以第二位不能送一樣的）。
    assert api.fix(agree, api.owner_token).status_code == 200

    result = _cli(db, monkeypatch)

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[0] == f"論文 {differ}  Fake Paper A"
    # 時間是資料庫寫入當下的時鐘（不是 NOW），這裡只看格式；台北時間的換算見下一條測試。
    when = r"（\d{4}-\d{2}-\d{2} \d{2}:\d{2}）"
    assert re.fullmatch(
        rf"  測試甲  主題 digital_platforms、e_commerce{when}  方法 experiment{when}", lines[1]
    )
    assert re.fullmatch(
        rf"  測試乙  主題 social_media_communities{when}  方法 experiment{when}", lines[2]
    )
    assert lines[-1] == "共 1 篇（時間是台北時間）"
    assert len(lines) == 4
    for text in ("Fake Paper B", "理由不印", OWNER_EMAIL, str(api.owner_id)):
        assert text not in result.output


def _write_fix(
    database: Database,
    member_id: uuid.UUID,
    paper_id: uuid.UUID,
    at: datetime,
    picks: dict[TaxonomyField, list[str]],
) -> None:
    with database.transaction() as connection:
        for field, keys in picks.items():
            create_judgment_event(
                connection,
                classification_fix(
                    member_id=member_id,
                    actor_role=ActorRole.MEMBER,
                    paper_id=paper_id,
                    field=field,
                    keys=keys,
                    reason=None,
                ),
                created_at=at,
            )


def test_disagreements_command_prints_each_field_with_its_own_taipei_time(
    api: Api, db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 甲先改主題與方法、後來只改了主題；甲的方法和乙的不同（意見不同在方法）。按欄取最新，甲的方法仍
    # 印得出來（只取最後一次送出時會印成「-」），兩欄各自的時間換成台北時間（UTC+8，跨日）。
    differ = create_paper(database, doi="10.1/differ", openalex_id="W11", title="Fake Paper A")
    insert_classification(db, differ, ["e_commerce"], ["experiment"])
    topic, method = TaxonomyField.TOPIC, TaxonomyField.METHOD
    _write_fix(
        database,
        api.owner_id,
        differ,
        datetime(2026, 11, 10, 1, 30, tzinfo=UTC),
        {topic: ["erp"], method: ["survey_sem"]},
    )
    _write_fix(
        database,
        api.other_id,
        differ,
        datetime(2026, 11, 15, 3, 0, tzinfo=UTC),
        {topic: ["e_commerce"], method: ["experiment"]},
    )
    _write_fix(
        database,
        api.owner_id,
        differ,
        datetime(2026, 11, 20, 16, 5, tzinfo=UTC),
        {topic: ["e_commerce"]},
    )

    result = _cli(db, monkeypatch)

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines == [
        f"論文 {differ}  Fake Paper A",
        "  測試甲  主題 e_commerce（2026-11-21 00:05）  方法 survey_sem（2026-11-10 09:30）",
        "  測試乙  主題 e_commerce（2026-11-15 11:00）  方法 experiment（2026-11-15 11:00）",
        "共 1 篇（時間是台北時間）",
    ]


def test_disagreements_command_without_any(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _cli(db, monkeypatch)

    assert result.exit_code == 0
    assert result.stdout.strip() == "沒有成員意見不同的分類修正"
