"""論文頁的 API 對真的 PostgreSQL（Story 3.6、ADR-0048、ADR-0002、ADR-0015）：spec 的 I/O 表每一列。

- 讀得到：書目（DOI 與 OpenAlex 連結、被引用次數、`in_scope`／`origin`／`is_retracted`）、目前的
  摘要、三個 AI 區塊（狀態只由 `find_ai_block` 判定）、全文連結、目前已發佈的快照。一篇有兩個 DOI 時
  取最早記上的那一個。
- 看不到（成員看不到的私人論文）、不存在、ID 不是 UUID：三個 404 的狀態碼、本文、標頭（除
  `X-Request-ID`）完全相同，而且沒有金絲雀。
- 被排除的非研究文章（成員看得到、`excluded_reason` 有值）：和不存在的 404 完全相同（ADR-0015）。
- 沒登入：三個端點都是 401。
- 沒 DOI／沒快照／沒摘要：對應欄位是 null，區塊照 `find_ai_block`。
- 摘要只回 `is_current` 那一筆：另有不是目前的摘要時不受影響；只有不是目前的摘要時是 null。
- 預算用完：還沒有產出的區塊是 `deferred_budget`（預算照設定、帳本照 `now`）。
- 顯示名稱照設定檔的順序；設定值是台北時區。

兩位成員經開發環境的假登入取得真的 session（和 `test_permissions.py` 一樣的組法，路由用正式的
`install_api_routes`）。金絲雀（`CANARY`）放在看不到的論文的標題與摘要，以及成員 A 修正分類的理由
（理由不在論文頁的回應裡）。資料全是測試自己編的（`*@example.com`、假標題、假摘要）。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.adapters.fake.login import pick_member_identity
from radar.domain.ai_outputs import LlmTask
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.contracts import build_display_names
from radar.domain.judgments import ActorRole, SummaryField, classification_fix, summary_report
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import SystemViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo.db import Database
from radar.services.members import allow_member
from radar.services.paper_page import PaperPageSetup
from tests.database import TestDatabase, migrate
from tests.integration.test_effective_views import (
    MODEL,
    create_event,
    insert_classification,
    insert_header,
    insert_research_model,
)
from tests.llm_support import (
    budget_config,
    create_hidden_paper,
    create_paper,
    execute,
    insert_ledger,
    paper_page_setup,
    paper_routes,
)
from tests.permissions.framework import CANARY

DEV_ORIGIN = "http://localhost:8000"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
OWNER_EMAIL = "paper.owner@example.com"
OTHER_EMAIL = "paper.other@example.com"
NOT_FOUND = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}
ZH_CONTENT = {
    "research_question": "遠距團隊如何採用協作平台？",
    "method": "問卷調查",
    "theory": None,
    "findings": None,
}


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


class Api:
    """真的 app（正式的 `install_api_routes`）＋兩位登入的成員。"""

    def __init__(self, database: Database, setup: PaperPageSetup | None = None) -> None:
        settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
        app = create_app(readiness_check=lambda: True)
        install_api_routes(
            app,
            auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW),
            paper_routes=paper_routes(database, setup=setup, now=NOW),
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

    def get(self, path: str, token: str | None) -> Response:
        self.client.cookies.clear()
        headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
        return self.client.get(path, headers=headers)


@pytest.fixture
def api(database: Database) -> Api:
    return Api(database)


def _publish_snapshot(db: TestDatabase, number: int, as_of: str | None) -> None:
    (run,) = execute(db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id")
    execute(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of) VALUES (%s, %s, %s, %s)",
        number,
        run[0],
        "building" if as_of is None else "published",
        as_of,
    )


def _without_request_id(response: Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() != REQUEST_ID_HEADER.lower()
    }


# --- 讀得到 -----------------------------------------------------------------------------------


def _visible_paper(db: TestDatabase, database: Database, api: Api) -> uuid.UUID:
    paper_id = create_paper(database, doi="10.1/abc", openalex_id="W1")
    execute(db, "UPDATE paper SET cited_by_count = 12 WHERE id = %s", paper_id)
    # 後來才記上的第二個 DOI（字母順序在前）：論文頁仍用最早記上的那一個。
    execute(
        db,
        "INSERT INTO external_id (paper_id, scheme, value_norm, created_at)"
        " VALUES (%s, 'doi', '10.1/aaa-later', now() + interval '1 hour')",
        paper_id,
    )
    # 同樣地，後來才記上的第二個 OpenAlex ID（排序在 W1 前面）：仍用 W1。
    execute(
        db,
        "INSERT INTO external_id (paper_id, scheme, value_norm, created_at)"
        " VALUES (%s, 'openalex', 'W0', now() + interval '1 hour')",
        paper_id,
    )
    # 另一個來源、不是目前的摘要：論文頁只回 `is_current` 那一筆。
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'An older abstract from another source.', false)",
        paper_id,
    )
    zh_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    create_event(
        database,
        summary_report(
            member_id=api.owner_id,
            actor_role=ActorRole.MEMBER,
            zh_summary_id=zh_id,
            field=SummaryField.METHOD,
            reason="摘要沒有提到",
        ),
    )
    insert_classification(db, paper_id, ["erp"], ["survey"])
    # 兩位成員修正主題、意見不同；成員 A 的理由放金絲雀（論文頁不回理由）。
    for member_id, keys, reason in (
        (api.owner_id, ["collaboration"], f"我覺得是協作 {CANARY}"),
        (api.other_id, ["erp", "collaboration"], None),
    ):
        create_event(
            database,
            classification_fix(
                member_id=member_id,
                actor_role=ActorRole.MEMBER,
                paper_id=paper_id,
                field=TaxonomyField.TOPIC,
                keys=keys,
                reason=reason,
            ),
        )
    insert_research_model(db, paper_id, country="taiwan")
    _publish_snapshot(db, 1, "2026-11-01T00:00:00+00")
    _publish_snapshot(db, 2, "2026-11-08T03:30:00+08")
    _publish_snapshot(db, 3, None)  # 建立中的不算
    return paper_id


def test_member_reads_a_visible_paper(db: TestDatabase, database: Database, api: Api) -> None:
    paper_id = _visible_paper(db, database, api)

    response = api.get(f"/api/v1/papers/{paper_id}", api.other_token)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert CANARY not in response.text
    assert OWNER_EMAIL not in response.text
    assert OTHER_EMAIL not in response.text
    assert api.owner_token not in response.text
    body: dict[str, Any] = response.json()
    assert body["paper"] == {
        "id": str(paper_id),
        "title": "A study of information systems",
        "authors": [],
        "publication_year": 2025,
        "journal": "misq",
        "journal_issn_l": "0276-7783",
        "volume": None,
        "issue": None,
        "first_page": None,
        "last_page": None,
        "doi": "10.1/abc",
        "doi_url": "https://doi.org/10.1/abc",
        "openalex_id": "W1",
        "openalex_url": "https://openalex.org/W1",
        "cited_by_count": 12,
        "in_scope": True,
        "origin": "tracked",
        "is_retracted": False,
    }
    assert body["abstract"] == {
        "content": "We study how teams adopt information systems.",
        "source": "openalex",
    }
    zh = body["zh_summary"]
    assert zh["state"] == "ready"
    assert zh["value"]["research_question"] == ZH_CONTENT["research_question"]
    assert zh["value"]["reported_fields"] == ["method"]
    assert zh["provenance"]["model"] == MODEL
    assert zh["provenance"]["input_kind"] == "abstract"
    assert zh["provenance"]["generated_at"].endswith("Z")
    classification = body["classification"]
    assert classification["state"] == "ready"
    assert classification["value"]["topic"]["human_verified"] is True
    assert classification["value"]["topic"]["fix_member_count"] == 2
    assert classification["value"]["topic"]["members_disagree"] is True
    assert classification["value"]["method"] == {
        "keys": ["survey"],
        "human_verified": False,
        "fix_member_count": 0,
        "members_disagree": False,
    }
    research_model = body["research_model"]
    assert research_model["state"] == "ready"
    assert research_model["value"]["country"] == "taiwan"
    assert research_model["value"]["constructs"][0]["cronbach_alpha"] == "0.910"
    hypothesis = research_model["value"]["hypotheses"][0]
    assert (hypothesis["beta"], hypothesis["p_value"]) == ("-0.10000000000000001", "0.01")
    assert body["full_text_links"] == {
        "doi_url": "https://doi.org/10.1/abc",
        "open_access_url": None,
    }
    assert body["snapshot"] == {"number": 2, "as_of": "2026-11-07T19:30:00Z"}


def test_paper_without_doi_snapshot_or_abstract(database: Database, api: Api) -> None:
    paper_id = create_paper(database, doi=None, openalex_id="W2", abstract=None)

    body = api.get(f"/api/v1/papers/{paper_id}", api.owner_token).json()

    assert (body["paper"]["doi"], body["paper"]["doi_url"]) == (None, None)
    assert body["paper"]["openalex_url"] == "https://openalex.org/W2"
    assert body["abstract"] is None
    assert body["snapshot"] is None
    assert body["full_text_links"] == {"doi_url": None, "open_access_url": None}
    # 區塊照 find_ai_block：沒有摘要的論文不做中文重點與研究模型、分類只看標題（還沒跑）。
    assert body["zh_summary"] == {"state": "no_input", "value": None, "provenance": None}
    assert body["research_model"] == {"state": "no_input", "value": None, "provenance": None}
    assert body["classification"] == {"state": "pending_batch", "value": None, "provenance": None}


def test_only_a_non_current_abstract_counts_as_no_abstract(
    db: TestDatabase, database: Database, api: Api
) -> None:
    paper_id = create_paper(database, abstract=None)
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'uploaded_pdf', 'An abstract read from one member''s PDF.', false)",
        paper_id,
    )

    response = api.get(f"/api/v1/papers/{paper_id}", api.other_token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["abstract"] is None
    assert body["zh_summary"]["state"] == "no_input"
    assert "one member" not in response.text


def test_block_without_output_is_deferred_when_the_budget_is_used_up(
    db: TestDatabase, database: Database
) -> None:
    setup = paper_page_setup(budget=budget_config(zh_summary="0.01"))
    api = Api(database, setup)
    paper_id = create_paper(database)
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "0.02", NOW)

    body = api.get(f"/api/v1/papers/{paper_id}", api.other_token).json()

    assert body["zh_summary"] == {"state": "deferred_budget", "value": None, "provenance": None}
    # 其他工作的預算還在：照常排隊。
    assert body["classification"]["state"] == "pending_batch"


# --- 看不到、不存在、壞 ID ----------------------------------------------------------------------


def test_hidden_missing_and_malformed_get_identical_404s(
    db: TestDatabase, database: Database, api: Api
) -> None:
    hidden_id = create_hidden_paper(
        database,
        title=f"Hidden paper {CANARY}",
        abstract=f"An abstract that only one member may read: {CANARY}.",
    )
    # 前提：這篇真的存在、帶著金絲雀，只是成員看不到（不然「和不存在一樣」不能當證據）。
    assert execute(db, "SELECT lab_visible, title FROM paper WHERE id = %s", hidden_id) == [
        (False, f"Hidden paper {CANARY}")
    ]

    responses = {
        "hidden": api.get(f"/api/v1/papers/{hidden_id}", api.other_token),
        "missing": api.get(f"/api/v1/papers/{uuid.uuid4()}", api.other_token),
        "malformed": api.get("/api/v1/papers/not-a-uuid", api.other_token),
    }

    hidden = responses["hidden"]
    assert hidden.status_code == 404
    assert hidden.json() == NOT_FOUND
    assert hidden.headers["content-type"] == "application/problem+json"
    assert hidden.headers["cache-control"] == "no-store"
    for name, response in responses.items():
        assert response.status_code == hidden.status_code, name
        assert response.content == hidden.content, name
        assert _without_request_id(response) == _without_request_id(hidden), name
        texts = [response.text, *(f"{k}: {v}" for k, v in response.headers.items())]
        assert all(CANARY not in text for text in texts), name


def test_excluded_paper_gets_the_same_404_as_a_missing_one(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # ADR-0015：被排除的非研究文章沒有論文頁。
    excluded_id = create_paper(
        database,
        title=f"Editorial {CANARY}",
        abstract=f"An editorial note, not a research paper: {CANARY}.",
    )
    execute(db, "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s", excluded_id)
    # 前提：成員看得到這篇（`lab_visible` 為真）、帶著金絲雀，只是被排除（不然不能當證據）。
    assert execute(
        db, "SELECT lab_visible, excluded_reason, title FROM paper WHERE id = %s", excluded_id
    ) == [(True, "editorial", f"Editorial {CANARY}")]

    excluded = api.get(f"/api/v1/papers/{excluded_id}", api.other_token)
    missing = api.get(f"/api/v1/papers/{uuid.uuid4()}", api.other_token)

    assert excluded.status_code == 404
    assert excluded.json() == NOT_FOUND
    assert excluded.headers["content-type"] == "application/problem+json"
    assert excluded.headers["cache-control"] == "no-store"
    assert missing.status_code == excluded.status_code
    assert missing.content == excluded.content
    assert _without_request_id(missing) == _without_request_id(excluded)
    texts = [excluded.text, *(f"{k}: {v}" for k, v in excluded.headers.items())]
    assert all(CANARY not in text for text in texts)


# --- 沒登入 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/v1/papers/{id}", "/api/v1/display-names", "/api/v1/client-config"]
)
def test_signed_out_gets_401(database: Database, api: Api, path: str) -> None:
    paper_id = create_paper(database)

    response = api.get(path.format(id=paper_id), None)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "not_authenticated"
    assert "A study of information systems" not in response.text


# --- 顯示名稱與設定值 ---------------------------------------------------------------------------


def test_display_names_and_client_config(api: Api) -> None:
    setup = paper_page_setup()

    names = api.get("/api/v1/display-names", api.other_token)
    config = api.get("/api/v1/client-config", api.other_token)

    assert names.status_code == 200
    assert names.json() == build_display_names(setup.journals, setup.taxonomy).model_dump(
        mode="json"
    )
    assert config.status_code == 200
    assert config.json() == {"timezone": "Asia/Taipei"}
