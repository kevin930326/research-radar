"""修正分類的 API 與管理指令對真的 PostgreSQL（Story 3.11、FR-4、FR-16、NFR-16、ADR-0055）。

- 送出：主題與方法兩筆 `judgment_event`（同一個交易：`created_at` 相同、舊值是修正前的有效值）＋
  一筆稽核（同一個交易），立即生效（「已核對」、修正人數）；回應帶修正後的分類區塊與歷史。
- 兩欄一起寫或都不寫：寫第二欄時失敗，第一欄與稽核都不留。
- 不收的值（不是分類表的 key、unclassified、放錯欄位、沒選、和目前一樣、理由超過 100 字、理由有
  換行）一律 422，什麼都不寫；本文不回顯送來的值。
- 看不到的論文（別人的私人論文）、不存在的、壞 ID：修正與讀歷史都是完全相同的 404，什麼都不寫。
- 歷史：新的在前、只有姓名，回應裡沒有 email；兩人意見不同時「意見不同」；同一個人改兩次是兩筆歷史、
  修正人數 1。沒有刪除或撤回的端點。
- 2.7 的測試用 API 再驗一次：修正後重跑分類（新的 active），論文頁的有效值仍然是人工值。
- 換人後寫入（`X-Radar-Expected-Member` 不符）409，什麼都不寫。
- `radar classification disagreements`：只列意見不同的論文，每位成員最新一次的選擇與時間；
  沒有就印一行。

兩位成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。資料全是測試自己
編的（`*@example.com`、假標題）。
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
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.library import AddedVia
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import EXPECTED_MEMBER_HEADER, SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event
from radar.repo.library import create_library_item
from radar.services.judgments import fix_classification
from radar.services.members import allow_member, set_member_admin
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
OWNER_EMAIL = "fix.owner@example.com"
OTHER_EMAIL = "fix.other@example.com"
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
    # 同一個交易：now() 是交易開始的時間，三筆一樣。
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

    def second_fails(connection: Any, event: Any) -> uuid.UUID | None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("寫第二欄時失敗")
        return real(connection, event)

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


def test_reason_of_exactly_100_characters_is_kept(
    api: Api, db: TestDatabase, paper_id: uuid.UUID
) -> None:
    response = api.fix(paper_id, api.owner_token, {**NEW, "reason": "字" * 100})

    assert response.status_code == 200
    assert {e[8] for e in _events(db)} == {"字" * 100}


def test_a_paper_without_an_ai_classification_can_be_fixed(api: Api, database: Database) -> None:
    # 分類還沒有結果（沒有 active）：任何合格的值都和「目前」不同；舊值是 null。前端照設計稿不給入口
    # （列給 Kevin），後端照收。
    paper = create_paper(database, doi="10.1/no-ai", openalex_id="W2")

    response = api.fix(paper, api.owner_token)

    assert response.status_code == 200
    assert response.json()["classification"]["value"]["fix_member_count"] == 1


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
    for text in (OWNER_EMAIL, OTHER_EMAIL, "@example.com", str(api.owner_id), str(api.other_id)):
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

    insert_classification(db, paper_id, ["erp"], ["survey_sem"])

    assert execute(
        db,
        "SELECT count(*) FROM classification WHERE paper_id = %s AND status = 'active'",
        paper_id,
    ) == [(1,)]
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
    other_pick = {"topic": ["social_media_communities"], "method": ["experiment"]}
    assert api.fix(differ, api.other_token, other_pick).status_code == 200
    assert api.fix(differ, api.owner_token, {**NEW, "reason": "理由不印"}).status_code == 200
    # 只有一位修正過的不算意見不同（和目前一樣的不收，所以第二位不能送一樣的）。
    assert api.fix(agree, api.owner_token).status_code == 200

    result = _cli(db, monkeypatch)

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[0] == f"論文 {differ}  Fake Paper A"
    assert lines[1].endswith("  測試甲  主題 digital_platforms、e_commerce  方法 experiment")
    assert lines[2].endswith("  測試乙  主題 social_media_communities  方法 experiment")
    # 時間是台北時間（NOW 不影響資料庫的 now()；只看格式）。
    assert lines[1].startswith("  2")
    assert lines[-1] == "共 1 篇（時間是台北時間）"
    assert len(lines) == 4
    for text in ("Fake Paper B", "理由不印", OWNER_EMAIL, str(api.owner_id)):
        assert text not in result.output


def test_disagreements_command_without_any(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _cli(db, monkeypatch)

    assert result.exit_code == 0
    assert result.stdout.strip() == "沒有成員意見不同的分類修正"
