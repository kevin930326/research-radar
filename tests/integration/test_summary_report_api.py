"""回報中文重點錯誤的 API 與管理指令對真的 PostgreSQL（Story 3.12、FR-14 附則、FR-4、NFR-16、
AD-10、ADR-0015、ADR-0055、ADR-0064）。

- 送出：一筆 `summary_report`（指向這一刻有效的那一筆 `zh_summary` 與欄位，內容不改）＋一筆稽核
  （同一個交易、同一個時間）；回應與論文頁的 `reported_fields` 多了那一欄，只有欄位名稱。
- 不收：不是四欄之一、說明沒寫或只有空白、超過 100 字、有換行或行分隔符、段落分隔符；中文重點沒有
  產生成功（`failed`）、四欄都沒有（`empty`）、還在產生、沒有摘要——一律 422，什麼都不寫。
- 送出帶畫面上看到的那一筆 `zh_summary_id`；送出前重新產生了（在鎖裡比對）→ 409 `summary_changed`，
  什麼都不寫（ADR-0064）。
- 標記只看「有回報、還沒處理結果、而且是現在這一筆」：重新產生後消失（回報還在、指著舊的那一筆）、
  管理指令標「已確認無誤」後消失。
- 兩位成員同時回報同一欄（兩條資料庫連線）：兩筆回報各有稽核，那一欄只有一個標記；只處理其中一筆，
  標記還在。
- 看不到的論文（別人的私人論文、被排除、追蹤範圍外）、不存在、壞 ID：完全相同的 404，什麼都不寫；
  看不到的論文帶壞本文也是 404。換人後寫入（預期成員不符）409。
- 公開身分（示範站產生器）：論文讀得到，回報的標記讀成空的；回報清單與寫入都直接丟錯。
- 稽核寫入失敗時回報也退回。
- `radar summary-reports list`／`confirm-ok`：列出還沒處理的回報（姓名、說明、台北時間，沒有
  email）；以管理者的名義標「已確認無誤」；不是管理者、已經處理過的不寫。

兩位成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。資料全是測試自己
編的（`*@example.org`、假標題、假的中文重點）。不呼叫任何模型。
"""

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
from radar.domain.ai_blocks import AiProduct
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.judgments import SummaryField
from radar.domain.library import AddedVia
from radar.domain.summary_reports import SummaryChangedError
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import EXPECTED_MEMBER_HEADER, SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database
from radar.repo.judgments import list_open_summary_reports
from radar.repo.library import create_library_item
from radar.services.ai_blocks import demo_ai_block_settings
from radar.services.judgments import (
    AdminRequiredError,
    SummaryReportNotOpenError,
    confirm_summary_report,
    report_summary,
)
from radar.services.members import allow_member, set_member_admin
from tests.backup_support import count_in_snapshot, publish_snapshot
from tests.database import ROOT, TestDatabase, migrate
from tests.demo_export_support import _insert_rejected
from tests.integration.test_effective_views import insert_header
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
NOW = datetime(2026, 11, 26, 4, 0, tzinfo=UTC)
OWNER_EMAIL = "report.owner@example.org"
OTHER_EMAIL = "report.other@example.org"
ADMIN_EMAIL = "report.admin@example.org"
NOT_FOUND = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}
REQUEST_INVALID = {
    "type": "about:blank",
    "title": "Unprocessable Content",
    "status": 422,
    "code": "request_invalid",
}
ZH = {
    "research_question": "（假資料）評分不一致怎麼影響信任？",
    "method": "（假資料）線上實驗",
    "theory": None,
    "findings": "（假資料）信任明顯下降。",
}
NOTE = "摘要沒有寫「明顯」"
BODY = {"field": "findings", "note": f"  {NOTE} "}


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

    def report(
        self, paper_id: object, token: str, body: Any = None, *, expected: str | None = None
    ) -> Response:
        """送出回報。本文是 dict 而且沒有 `zh_summary_id` 時，照前端的做法補上這位成員現在論文頁上
        看到的那一筆（讀不到論文頁或沒有內容時補一個不存在的 ID）。"""
        payload = BODY if body is None else body
        if isinstance(payload, dict) and "zh_summary_id" not in payload:
            payload = {**payload, "zh_summary_id": self.seen_summary_id(paper_id, token)}
        self.client.cookies.clear()
        headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": DEV_ORIGIN}
        if expected is not None:
            headers[EXPECTED_MEMBER_HEADER] = expected
        return self.client.post(
            f"/api/v1/papers/{paper_id}/summary-reports", headers=headers, json=payload
        )

    def seen_summary_id(self, paper_id: object, token: str) -> str:
        self.client.cookies.clear()
        response = self.client.get(
            f"/api/v1/papers/{paper_id}", headers={"Cookie": f"{SESSION_COOKIE}={token}"}
        )
        value = response.json()["zh_summary"]["value"] if response.status_code == 200 else None
        return str(uuid.uuid4()) if value is None else str(value["zh_summary_id"])

    def paper(self, paper_id: object, token: str) -> dict[str, Any]:
        self.client.cookies.clear()
        response = self.client.get(
            f"/api/v1/papers/{paper_id}", headers={"Cookie": f"{SESSION_COOKIE}={token}"}
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
def api(database: Database) -> Api:
    return Api(database)


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database, title="Fake Paper on Rating Signals")


@pytest.fixture
def summary_id(db: TestDatabase, paper_id: uuid.UUID) -> uuid.UUID:
    """這篇現在有效的中文重點（四欄有三欄有字）。"""
    return insert_header(db, paper_id, "zh_summary", ZH)


def _events(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT kind, member_id, actor_role, target_type, target_id, field, old_value, new_value,"
        " reason, created_at, report_event_id FROM judgment_event ORDER BY created_at, id",
    )


def _audits(db: TestDatabase, action: str = "report_summary") -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT actor_member_id, actor_role, action, target_type, target_id, detail, created_at"
        " FROM audit_event WHERE action = %s ORDER BY created_at",
        action,
    )


def _comparable(response: Response) -> tuple[int, bytes, dict[str, str]]:
    headers = {k: v for k, v in response.headers.items() if k.lower() != REQUEST_ID_HEADER.lower()}
    return response.status_code, response.content, headers


def _zh(api: Api, paper_id: uuid.UUID, token: str) -> dict[str, Any]:
    zh: dict[str, Any] = api.paper(paper_id, token)["zh_summary"]
    return zh


# --- 送出 -------------------------------------------------------------------------------------


def test_a_report_writes_one_event_and_the_audit_in_one_transaction(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    response = api.report(paper_id, api.owner_token)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"zh_summary"}
    assert body["zh_summary"]["value"]["reported_fields"] == ["findings"]
    assert body["zh_summary"]["value"]["zh_summary_id"] == str(summary_id)
    # 回應只有欄位名稱：沒有說明、回報的人。
    for text in (NOTE, "測試甲", str(api.owner_id), OWNER_EMAIL):
        assert text not in response.text

    [event] = _events(db)
    assert event[:9] == (
        "summary_report",
        api.owner_id,
        "member",
        "zh_summary",
        summary_id,
        "findings",
        None,
        None,
        NOTE,
    )
    [audit] = _audits(db)
    assert audit[:5] == (api.owner_id, "member", "report_summary", "zh_summary", summary_id)
    assert audit[5]["field"] == "findings"
    assert audit[5]["paper_id"] == str(paper_id)
    assert NOTE not in str(audit[5])
    # 拿到鎖之後取一次 clock_timestamp()，事件與稽核共用。
    assert audit[6] == event[9]

    # 內容不改；其他成員打開論文頁也看到那一欄的標記。
    zh = _zh(api, paper_id, api.other_token)
    assert zh["value"]["reported_fields"] == ["findings"]
    assert {key: zh["value"][key] for key in ZH} == ZH


def test_an_admin_report_is_recorded_as_admin(
    api: Api, db: TestDatabase, database: Database, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    set_member_admin(SystemViewer(), database, email=OWNER_EMAIL)

    assert api.report(paper_id, api.owner_token).status_code == 200

    assert [e[2] for e in _events(db)] == ["admin"]
    assert [a[1] for a in _audits(db)] == ["admin"]


@pytest.mark.parametrize("field", list(SummaryField))
def test_each_of_the_four_fields_can_be_reported(
    api: Api, paper_id: uuid.UUID, summary_id: uuid.UUID, field: SummaryField
) -> None:
    # 摘要沒提到的那一欄（theory 是 None）也可以回報。
    response = api.report(paper_id, api.owner_token, {"field": field.value, "note": "說明"})

    assert response.status_code == 200, response.text
    assert response.json()["zh_summary"]["value"]["reported_fields"] == [field.value]


def test_reports_on_two_fields_show_in_the_fixed_order(
    api: Api, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    assert api.report(paper_id, api.owner_token).status_code == 200
    second = api.report(paper_id, api.other_token, {"field": "research_question", "note": "說明"})

    assert second.json()["zh_summary"]["value"]["reported_fields"] == [
        "research_question",
        "findings",
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"field": "abstract", "note": "說明"},
        {"field": "Findings", "note": "說明"},
        {"field": "findings", "note": ""},
        {"field": "findings", "note": "   "},
        {"field": "findings", "note": "長" * 101},
        {"field": "findings", "note": "秘" + "😀" * 100},
        {"field": "findings", "note": "第一行\n第二行"},
        {"field": "findings", "note": "句中 換行"},
        {"field": "findings", "note": "句中 換段"},
        {"field": "findings", "note": "零寬\u200b空白"},
        {"field": "findings", "note": "方向\u202e反轉"},
        {"field": "findings"},
        {"field": "findings", "note": "說明", "member_id": "x"},
    ],
)
def test_rejected_values_are_422_and_write_nothing(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID, body: Any
) -> None:
    response = api.report(paper_id, api.owner_token, body)

    assert response.status_code == 422
    assert response.json() == REQUEST_INVALID
    assert _events(db) == []
    assert _audits(db) == []
    assert _zh(api, paper_id, api.owner_token)["value"]["reported_fields"] == []


@pytest.mark.parametrize("note", ["長" * 100, "😀" * 100, "  " + "長" * 100 + "  "])
def test_a_note_of_exactly_100_characters_is_kept(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID, note: str
) -> None:
    response = api.report(paper_id, api.owner_token, {"field": "method", "note": note})

    assert response.status_code == 200, response.text
    [event] = _events(db)
    assert event[8] == note.strip()


def _failed(db: TestDatabase, database: Database, paper: uuid.UUID) -> None:
    setup = paper_page_setup()
    settings = demo_ai_block_settings(setup.models, setup.taxonomy, setup.context_values)
    _insert_rejected(db, database, paper, AiProduct.ZH_SUMMARY, settings, "abstract")


def _empty(db: TestDatabase, database: Database, paper: uuid.UUID) -> None:
    insert_header(db, paper, "zh_summary", dict.fromkeys(ZH))


def _pending(db: TestDatabase, database: Database, paper: uuid.UUID) -> None:
    del db, database, paper  # 什麼都沒有：還在產生（pending_batch）


@pytest.mark.parametrize(
    ("make", "state"),
    [(_failed, "failed"), (_empty, "empty"), (_pending, "pending_batch")],
)
def test_a_summary_that_is_not_ready_cannot_be_reported(
    api: Api, db: TestDatabase, database: Database, paper_id: uuid.UUID, make: Any, state: str
) -> None:
    make(db, database, paper_id)
    assert _zh(api, paper_id, api.owner_token)["state"] == state

    response = api.report(paper_id, api.owner_token)

    assert response.status_code == 422
    assert response.json() == REQUEST_INVALID
    assert _events(db) == []
    assert _audits(db) == []


def test_a_paper_without_an_abstract_has_nothing_to_report(
    api: Api, db: TestDatabase, database: Database
) -> None:
    paper = create_paper(database, doi="10.1/none", openalex_id="W2", abstract=None)
    assert _zh(api, paper, api.owner_token)["state"] == "no_input"

    response = api.report(paper, api.owner_token)

    assert response.status_code == 422
    assert _events(db) == []


def test_a_failure_writing_the_audit_rolls_back_the_report(
    db: TestDatabase,
    database: Database,
    paper_id: uuid.UUID,
    summary_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = allow_member(SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲")

    def audit_fails(connection: Any, event: Any, **kw: Any) -> uuid.UUID:
        raise RuntimeError("寫稽核時失敗")

    monkeypatch.setattr(judgments_service, "create_audit_event", audit_fails)

    with pytest.raises(RuntimeError, match="稽核"):
        report_summary(
            MemberViewer(member_id=member.member_id, is_admin=False),
            database,
            paper_page_setup(),
            paper_id,
            zh_summary_id=summary_id,
            field="findings",
            note=NOTE,
            now=NOW,
        )

    assert _events(db) == []
    assert _audits(db) == []


# --- 標記：只看現在這一筆、還沒處理的 -----------------------------------------------------------


def test_the_mark_disappears_after_the_summary_is_regenerated(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    assert api.report(paper_id, api.owner_token).status_code == 200

    # 重新產生：新的一筆 active，舊的變 superseded（AD-10：回報不跟過去）。
    new_id = insert_header(db, paper_id, "zh_summary", ZH)

    zh = _zh(api, paper_id, api.owner_token)
    assert zh["value"]["zh_summary_id"] == str(new_id)
    assert zh["value"]["reported_fields"] == []
    # 回報本身還在，指著舊的那一筆（只新增，不刪）。
    [event] = _events(db)
    assert (event[0], event[4]) == ("summary_report", summary_id)


def test_a_report_points_at_the_summary_current_when_it_was_sent(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    # 成員打開論文頁時已經是新的一筆：回報指向新的那一筆（成員看到的就是它）。
    new_id = insert_header(db, paper_id, "zh_summary", ZH)

    assert api.report(paper_id, api.owner_token).status_code == 200

    assert [e[4] for e in _events(db)] == [new_id]


def test_a_summary_regenerated_before_sending_is_409_and_writes_nothing(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    # 成員看著舊的一筆填表、送出前重新產生了：回報不貼到成員沒看過的新一筆（Codex 審查 3.12 中）。
    new_id = insert_header(db, paper_id, "zh_summary", ZH)

    response = api.report(paper_id, api.owner_token, {**BODY, "zh_summary_id": str(summary_id)})

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "code": "summary_changed",
    }
    assert NOTE not in response.text
    assert _events(db) == []
    assert _audits(db) == []
    zh = _zh(api, paper_id, api.owner_token)
    assert zh["value"]["zh_summary_id"] == str(new_id)
    assert zh["value"]["reported_fields"] == []
    # 帶不存在的 ID 也一樣（不透露任何 ID）。
    other = api.report(paper_id, api.owner_token, {**BODY, "zh_summary_id": str(uuid.uuid4())})
    assert other.status_code == 409
    assert other.content == response.content


def test_a_body_problem_is_still_422_before_comparing_the_summary(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    # 錯誤的先後：本文不對（說明沒寫）先回 422，才比對看到的那一筆（ADR-0064 決定第 1 點）。
    insert_header(db, paper_id, "zh_summary", ZH)

    response = api.report(
        paper_id,
        api.owner_token,
        {"field": "findings", "note": " ", "zh_summary_id": str(summary_id)},
    )

    assert response.status_code == 422
    assert response.json() == REQUEST_INVALID
    assert _events(db) == []


def test_the_mark_disappears_once_an_admin_confirms_it(
    api: Api, db: TestDatabase, database: Database, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    allow_member(SystemViewer(), database, email=ADMIN_EMAIL, display_name="測試管理者")
    set_member_admin(SystemViewer(), database, email=ADMIN_EMAIL)
    assert api.report(paper_id, api.owner_token).status_code == 200
    [report_event] = execute(db, "SELECT id FROM judgment_event WHERE kind = 'summary_report'")

    confirmed = confirm_summary_report(
        SystemViewer(), database, report_event[0], admin_email=ADMIN_EMAIL.upper()
    )

    assert confirmed.report_id == report_event[0]
    assert _zh(api, paper_id, api.other_token)["value"]["reported_fields"] == []
    resolution = _events(db)[-1]
    assert resolution[0] == "summary_report_resolution"
    assert (resolution[2], resolution[3], resolution[4], resolution[5]) == (
        "admin",
        "zh_summary",
        summary_id,
        "findings",
    )
    assert resolution[7] == "confirmed_ok"
    assert resolution[10] == report_event[0]
    [audit] = _audits(db, "resolve_summary_report")
    assert audit[1:5] == ("admin", "resolve_summary_report", "zh_summary", summary_id)
    assert audit[5]["resolution"] == "confirmed_ok"
    assert audit[6] == resolution[9]

    # 處理過的不能再處理一次。
    with pytest.raises(SummaryReportNotOpenError):
        confirm_summary_report(SystemViewer(), database, report_event[0], admin_email=ADMIN_EMAIL)
    assert len(_audits(db, "resolve_summary_report")) == 1


@pytest.mark.parametrize("email", [OWNER_EMAIL, "nobody@example.org"])
def test_only_an_admin_on_the_allowlist_can_confirm(
    api: Api,
    db: TestDatabase,
    database: Database,
    paper_id: uuid.UUID,
    summary_id: uuid.UUID,
    email: str,
) -> None:
    assert api.report(paper_id, api.owner_token).status_code == 200
    [report_event] = execute(db, "SELECT id FROM judgment_event WHERE kind = 'summary_report'")

    with pytest.raises(AdminRequiredError):
        confirm_summary_report(SystemViewer(), database, report_event[0], admin_email=email)

    assert len(_events(db)) == 1
    assert _audits(db, "resolve_summary_report") == []
    assert _zh(api, paper_id, api.owner_token)["value"]["reported_fields"] == ["findings"]


# --- 同時回報、鎖裡比對（兩條資料庫連線）-------------------------------------------------------

QUEUE_POOL = 3


def _member_report(
    database: Database, member_id: uuid.UUID, paper_id: uuid.UUID, seen_id: uuid.UUID, note: str
) -> Any:
    return report_summary(
        MemberViewer(member_id=member_id, is_admin=False),
        database,
        paper_page_setup(),
        paper_id,
        zh_summary_id=seen_id,
        field="findings",
        note=note,
        now=NOW,
    )


def _before_the_first_lock(monkeypatch: pytest.MonkeyPatch, step: Any) -> list[Any]:
    """第一次拿回報的鎖之前，先在另一條連線上做完 `step` 並提交：甲的交易已經開始（查過看得到
    這篇），`step` 比甲先做完。回 `step` 的結果。"""
    real_lock = Database.transaction_lock
    done: list[Any] = []

    def lock_after_step(connection: Any, key: int) -> None:
        if not done:
            done.append(None)  # step 自己拿鎖時走真的鎖
            done[0] = step()
        real_lock(connection, key)

    monkeypatch.setattr(Database, "transaction_lock", staticmethod(lock_after_step))
    return done


def test_two_members_reporting_the_same_field_at_once_show_one_mark(
    db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 甲先開交易、乙在另一條連線先回報同一欄並提交、甲再拿到鎖：兩筆回報各有稽核，那一欄只有一個
    # 標記（Codex 審查 3.12 中）。
    database = Database(db.app_url, connect_timeout=5, pool_size=QUEUE_POOL)
    owner = allow_member(SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲")
    other = allow_member(SystemViewer(), database, email=OTHER_EMAIL, display_name="測試乙")
    admin = allow_member(SystemViewer(), database, email=ADMIN_EMAIL, display_name="測試管理者")
    set_member_admin(SystemViewer(), database, email=ADMIN_EMAIL)
    first = _before_the_first_lock(
        monkeypatch,
        lambda: _member_report(database, other.member_id, paper_id, summary_id, "乙的說明"),
    )

    block = _member_report(database, owner.member_id, paper_id, summary_id, NOTE)

    assert first[0] is not None
    assert block is not None
    assert block.value.reported_fields == (SummaryField.FINDINGS,)
    events = _events(db)
    assert [(e[0], e[1], e[4], e[5]) for e in events] == [
        ("summary_report", other.member_id, summary_id, "findings"),
        ("summary_report", owner.member_id, summary_id, "findings"),
    ]
    audits = _audits(db)
    assert [a[0] for a in audits] == [other.member_id, owner.member_id]
    assert [a[6] for a in audits] == [e[9] for e in events]
    # 甲的時間是拿到鎖之後取的，比乙的晚。
    assert events[1][9] > events[0][9]

    # 只處理其中一筆：同一欄還有一筆沒處理，標記還在；兩筆都處理了才消失。
    report_ids = [
        row[0]
        for row in execute(
            db, "SELECT id FROM judgment_event WHERE kind = 'summary_report' ORDER BY created_at"
        )
    ]
    monkeypatch.undo()
    confirm_summary_report(SystemViewer(), database, report_ids[0], admin_email=ADMIN_EMAIL)
    setup = paper_page_setup()
    settings = demo_ai_block_settings(setup.models, setup.taxonomy, setup.context_values)[
        AiProduct.ZH_SUMMARY
    ]
    viewer = MemberViewer(member_id=admin.member_id, is_admin=True)
    with database.transaction() as connection:
        still = find_ai_block(viewer, connection, paper_id, AiProduct.ZH_SUMMARY, settings)
    assert still is not None
    assert still.value.reported_fields == (SummaryField.FINDINGS,)  # type: ignore[union-attr]
    confirm_summary_report(SystemViewer(), database, report_ids[1], admin_email=ADMIN_EMAIL)
    with database.transaction() as connection:
        gone = find_ai_block(viewer, connection, paper_id, AiProduct.ZH_SUMMARY, settings)
    assert gone is not None
    assert gone.value.reported_fields == ()  # type: ignore[union-attr]


def test_a_regeneration_while_waiting_for_the_lock_is_caught_inside_it(
    db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 甲的交易已經開始、還沒拿到鎖時，另一條連線重新產生了中文重點：甲拿到鎖後讀到新的一筆，和
    # 看到的不同，不寫（ADR-0064）。
    database = Database(db.app_url, connect_timeout=5, pool_size=QUEUE_POOL)
    owner = allow_member(SystemViewer(), database, email=OWNER_EMAIL, display_name="測試甲")
    regenerated = _before_the_first_lock(
        monkeypatch, lambda: insert_header(db, paper_id, "zh_summary", ZH)
    )

    with pytest.raises(SummaryChangedError):
        _member_report(database, owner.member_id, paper_id, summary_id, NOTE)

    assert regenerated[0] is not None
    assert _events(db) == []
    assert _audits(db) == []


# --- 看不到、換人、公開身分 ----------------------------------------------------------------------


def test_reporting_a_paper_you_cannot_see_is_the_same_404(
    api: Api, db: TestDatabase, database: Database
) -> None:
    hidden = create_hidden_paper(database, title="Fake hidden paper")
    insert_header(db, hidden, "zh_summary", ZH)
    with database.transaction() as connection:
        create_library_item(
            connection,
            SystemViewer(),
            owner_id=api.owner_id,
            paper_id=hidden,
            added_via=AddedVia.UPLOAD,
        )

    responses = [
        api.report(hidden, api.other_token),
        api.report(uuid.uuid4(), api.other_token),
        api.report("not-a-uuid", api.other_token),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404]
    assert responses[0].json() == NOT_FOUND
    assert _comparable(responses[0]) == _comparable(responses[1]) == _comparable(responses[2])
    assert _events(db) == []
    assert _audits(db) == []
    # 對照：甲看得到自己文獻庫裡的這篇，可以回報。
    assert api.report(hidden, api.owner_token).status_code == 200


def test_excluded_and_out_of_scope_papers_are_the_same_404(
    api: Api, db: TestDatabase, database: Database
) -> None:
    excluded = create_paper(database, doi="10.1/excluded", openalex_id="W3", title="Fake editorial")
    insert_header(db, excluded, "zh_summary", ZH)
    execute(db, "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s", excluded)
    outside = create_hidden_paper(database, title="Fake paper nobody added")
    insert_header(db, outside, "zh_summary", ZH)

    responses = [api.report(p, api.owner_token) for p in (excluded, outside, uuid.uuid4())]

    assert [r.status_code for r in responses] == [404, 404, 404]
    assert _comparable(responses[0]) == _comparable(responses[1]) == _comparable(responses[2])
    assert _events(db) == []


@pytest.mark.parametrize("body", [{"field": "findings"}, ["findings"], {"extra": "secret"}])
def test_a_hidden_paper_with_a_bad_body_is_404_not_422(
    api: Api, db: TestDatabase, database: Database, body: Any
) -> None:
    hidden = create_hidden_paper(database, title="Fake hidden paper")
    insert_header(db, hidden, "zh_summary", ZH)

    responses = [
        api.report(hidden, api.owner_token, body),
        api.report(uuid.uuid4(), api.owner_token, body),
        api.report("not-a-uuid", api.owner_token, body),
        api.report(hidden, api.owner_token),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert all(_comparable(r) == _comparable(responses[-1]) for r in responses)
    assert _events(db) == []


def test_reporting_after_the_member_changed_is_409_and_writes_nothing(
    api: Api, db: TestDatabase, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    response = api.report(paper_id, api.owner_token, expected=str(api.other_id))

    assert response.status_code == 409
    assert response.json()["code"] == "member_changed"
    assert _events(db) == []
    assert _audits(db) == []


def test_the_public_viewer_reads_the_summary_but_never_the_reports(
    api: Api, db: TestDatabase, database: Database, paper_id: uuid.UUID, summary_id: uuid.UUID
) -> None:
    assert api.report(paper_id, api.owner_token).status_code == 200
    count_in_snapshot(db, paper_id, publish_snapshot(db))
    setup = paper_page_setup()
    settings = demo_ai_block_settings(setup.models, setup.taxonomy, setup.context_values)[
        AiProduct.ZH_SUMMARY
    ]

    with database.transaction() as connection:
        public = find_ai_block(PublicViewer(), connection, paper_id, AiProduct.ZH_SUMMARY, settings)
        system = find_ai_block(SystemViewer(), connection, paper_id, AiProduct.ZH_SUMMARY, settings)
    assert public is not None
    assert system is not None
    assert public.value.research_question == ZH["research_question"]  # type: ignore[union-attr]
    assert public.value.reported_fields == ()  # type: ignore[union-attr]
    assert system.value.reported_fields == (SummaryField.FINDINGS,)  # type: ignore[union-attr]
    with database.transaction() as connection, pytest.raises(TypeError):
        list_open_summary_reports(PublicViewer(), connection)  # type: ignore[arg-type]
    with database.transaction() as connection, pytest.raises(TypeError):
        list_open_summary_reports(
            MemberViewer(member_id=api.owner_id, is_admin=True),  # type: ignore[arg-type]
            connection,
        )
    with pytest.raises(TypeError, match="成員"):
        report_summary(
            PublicViewer(),  # type: ignore[arg-type]
            database,
            setup,
            paper_id,
            zh_summary_id=summary_id,
            field="findings",
            note=NOTE,
            now=NOW,
        )
    assert len(_events(db)) == 1


# --- 管理指令 -----------------------------------------------------------------------------------


def _cli(db: TestDatabase, monkeypatch: pytest.MonkeyPatch, *args: str) -> Any:
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(Path(ROOT) / "config"))
    return CliRunner().invoke(cli.app, ["summary-reports", *args])


def test_list_command_without_any(db: TestDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _cli(db, monkeypatch, "list")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "沒有還沒處理的中文重點回報"


def test_list_command_prints_open_reports_with_taipei_time(
    api: Api,
    db: TestDatabase,
    database: Database,
    paper_id: uuid.UUID,
    summary_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert api.report(paper_id, api.owner_token).status_code == 200
    other = api.report(paper_id, api.other_token, {"field": "method", "note": "方法寫錯"})
    assert other.status_code == 200
    # 甲的那一筆改成跨日的時間（UTC 16:05 → 台北隔天 00:05），乙的晚一點；之後重新產生。
    execute(
        db,
        "UPDATE judgment_event SET created_at = '2026-11-25T16:05:00+00:00' WHERE member_id = %s",
        api.owner_id,
    )
    execute(
        db,
        "UPDATE judgment_event SET created_at = '2026-11-25T17:00:00+00:00' WHERE member_id = %s",
        api.other_id,
    )
    insert_header(db, paper_id, "zh_summary", ZH)
    [(first_id,), (second_id,)] = execute(
        db, "SELECT id FROM judgment_event WHERE kind = 'summary_report' ORDER BY created_at"
    )

    result = _cli(db, monkeypatch, "list")

    assert result.exit_code == 0, result.output
    old = "  （這一筆中文重點已經不是目前的，論文頁不顯示標記）"
    assert result.stdout.splitlines() == [
        f"回報 {first_id}",
        f"  論文 {paper_id}  Fake Paper on Rating Signals",
        f"  發現  測試甲（2026-11-26 00:05）{old}",
        f"  說明 {NOTE}",
        "",
        f"回報 {second_id}",
        f"  論文 {paper_id}  Fake Paper on Rating Signals",
        f"  方法  測試乙（2026-11-26 01:00）{old}",
        "  說明 方法寫錯",
        "",
        "共 2 筆（時間是台北時間）",
    ]
    for text in (OWNER_EMAIL, OTHER_EMAIL, str(api.owner_id)):
        assert text not in result.output


def test_confirm_command_resolves_one_report(
    api: Api,
    db: TestDatabase,
    database: Database,
    paper_id: uuid.UUID,
    summary_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_member(SystemViewer(), database, email=ADMIN_EMAIL, display_name="測試管理者")
    set_member_admin(SystemViewer(), database, email=ADMIN_EMAIL)
    assert api.report(paper_id, api.owner_token).status_code == 200
    [(report_id,)] = execute(db, "SELECT id FROM judgment_event WHERE kind = 'summary_report'")

    done = _cli(db, monkeypatch, "confirm-ok", str(report_id), "--admin", ADMIN_EMAIL)
    again = _cli(db, monkeypatch, "confirm-ok", str(report_id), "--admin", ADMIN_EMAIL)
    not_admin = _cli(db, monkeypatch, "confirm-ok", str(uuid.uuid4()), "--admin", OWNER_EMAIL)
    bad_id = _cli(db, monkeypatch, "confirm-ok", "not-a-uuid", "--admin", ADMIN_EMAIL)

    assert done.exit_code == 0, done.output
    # 日誌也寫到 stdout（JSON 一行）；指令自己印的是最後一行。
    assert done.stdout.strip().splitlines()[-1] == f"已確認無誤：回報 {report_id}（發現）"
    assert again.exit_code == 1
    assert "已經處理過" in again.output
    assert not_admin.exit_code == 1
    assert "管理者" in not_admin.output
    assert bad_id.exit_code == 1
    assert len(_audits(db, "resolve_summary_report")) == 1
    assert _cli(db, monkeypatch, "list").stdout.strip() == "沒有還沒處理的中文重點回報"
    assert _zh(api, paper_id, api.owner_token)["value"]["reported_fields"] == []
