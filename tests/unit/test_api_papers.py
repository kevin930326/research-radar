"""api 的論文頁路由（Story 3.6、ADR-0048）：用假的函式組 `PaperRoutes`／`AuthRoutes`，不連資料庫。

- 三個端點都要登入：沒登入 401 `not_authenticated`。
- `GET /api/v1/papers/{paper_id}`：看不到（假函式回 None）與不是 UUID 的 ID 都回同一個 404，狀態碼、
  本文、標頭（除 `X-Request-ID`）相同；ID 不是 UUID 時不呼叫 services。
- 200 的回應照 `domain/contracts.py` 的形狀轉換，帶 `no-store`；讀的身分是 session 的成員。
- 顯示名稱與設定值照原樣回；掛上頁面殼之後仍由 API 回應；頁面殼已掛時裝路由丟錯。
- 論文頁新讀的四個設定檔（models、budget、taxonomy、context_values）改壞任何一個，api 不啟動。
資料全是測試自己編的。
"""

import dataclasses
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

import radar.entrypoints.api.main as api_main
from radar.domain.ai_blocks import AiBlock, AiBlockState, AiProduct
from radar.domain.contracts import DisplayName, DisplayNames, build_client_config
from radar.domain.paper_page import PaperPage
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import SESSION_COOKIE
from radar.entrypoints.api.papers import PaperRoutes, install_paper_routes
from radar.entrypoints.api.web import mount_web
from tests.llm_support import ROOT
from tests.unit.test_api_auth import GOOD_TOKEN, MEMBER, MEMBER_ID, ORIGIN, SHELL, FakeAuth
from tests.unit.test_contracts_paper_page import (
    AT_Z,
    CLASSIFICATION,
    FACTS,
    PROVENANCE,
    SNAPSHOT,
    ZH,
)

VISIBLE_ID = FACTS.paper_id
PAGE = PaperPage(
    facts=FACTS,
    journal_key="misq",
    zh_summary=AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, ZH, PROVENANCE),
    classification=AiBlock(
        AiProduct.CLASSIFICATION, AiBlockState.READY, CLASSIFICATION, PROVENANCE
    ),
    research_model=AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.NO_INPUT),
    snapshot=SNAPSHOT,
)
DISPLAY_NAMES = DisplayNames(
    journals=(DisplayName(key="misq", name="MIS Quarterly"),),
    topics=(DisplayName(key="erp", name="企業資源規劃"),),
    methods=(DisplayName(key="survey", name="問卷調查"),),
)
PATHS = ("/api/v1/papers/{id}", "/api/v1/display-names", "/api/v1/client-config")


@dataclass
class FakePapers:
    """記下每一次讀論文的身分與 ID；只有 `VISIBLE_ID` 看得到。"""

    calls: list[tuple[MemberViewer, uuid.UUID]] = field(default_factory=list)

    def load_paper(self, viewer: MemberViewer, paper_id: uuid.UUID) -> PaperPage | None:
        self.calls.append((viewer, paper_id))
        return PAGE if paper_id == VISIBLE_ID else None

    def routes(self) -> PaperRoutes:
        return PaperRoutes(
            load_paper=self.load_paper,
            display_names=DISPLAY_NAMES,
            client_config=build_client_config(),
        )


def _client(papers: FakePapers, *, web_dist: Path | None = None) -> TestClient:
    app = create_app(readiness_check=lambda: True)
    install_paper_routes(app, papers.routes(), FakeAuth().routes())
    if web_dist is not None:
        mount_web(app, web_dist)
    return TestClient(app, base_url=ORIGIN)


def _get(client: TestClient, path: str, *, token: str | None = GOOD_TOKEN) -> Response:
    headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
    client.cookies.clear()
    return client.get(path, headers=headers)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_every_endpoint_needs_a_session(path: str, token: str | None) -> None:
    papers = FakePapers()

    response = _get(_client(papers), path.format(id=VISIBLE_ID), token=token)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "code": "not_authenticated",
    }
    assert papers.calls == []


def test_paper_reads_as_the_session_member_and_returns_the_page() -> None:
    papers = FakePapers()

    response = _get(_client(papers), f"/api/v1/papers/{VISIBLE_ID}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert papers.calls == [(MEMBER.viewer, VISIBLE_ID)]
    assert papers.calls[0][0].member_id == MEMBER_ID
    body = response.json()
    assert set(body) == {
        "paper",
        "abstract",
        "zh_summary",
        "classification",
        "research_model",
        "full_text_links",
        "snapshot",
        "in_my_library",
    }
    assert body["paper"]["doi_url"] == "https://doi.org/10.1/abc"
    assert body["paper"]["journal"] == "misq"
    assert body["abstract"] == {"content": "We study teams.", "source": "openalex"}
    assert body["zh_summary"]["state"] == "ready"
    assert body["zh_summary"]["value"]["reported_fields"] == ["method"]
    assert body["classification"]["value"]["topic"]["members_disagree"] is True
    assert body["research_model"] == {"state": "no_input", "value": None, "provenance": None}
    assert body["full_text_links"] == {
        "doi_url": "https://doi.org/10.1/abc",
        "open_access_url": None,
    }
    assert body["snapshot"] == {"number": 3, "as_of": AT_Z}


def test_paper_without_abstract_or_snapshot_has_nulls() -> None:
    page = PaperPage(
        facts=dataclasses.replace(FACTS, abstract=None, doi=None),
        journal_key=None,
        zh_summary=AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.NO_INPUT),
        classification=AiBlock(AiProduct.CLASSIFICATION, AiBlockState.PENDING_BATCH),
        research_model=AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.NO_INPUT),
        snapshot=None,
    )
    routes = PaperRoutes(
        load_paper=lambda _viewer, _paper_id: page,
        display_names=DISPLAY_NAMES,
        client_config=build_client_config(),
    )
    app = create_app(readiness_check=lambda: True)
    install_paper_routes(app, routes, FakeAuth().routes())

    body = _get(TestClient(app, base_url=ORIGIN), f"/api/v1/papers/{VISIBLE_ID}").json()

    assert body["abstract"] is None
    assert body["snapshot"] is None
    assert (body["paper"]["doi"], body["paper"]["doi_url"], body["paper"]["journal"]) == (
        None,
        None,
        None,
    )
    assert body["full_text_links"] == {"doi_url": None, "open_access_url": None}


def _comparable(response: Response) -> tuple[int, bytes, dict[str, str]]:
    headers = {k: v for k, v in response.headers.items() if k.lower() != REQUEST_ID_HEADER.lower()}
    return response.status_code, response.content, headers


def test_hidden_missing_and_malformed_ids_get_the_same_404() -> None:
    papers = FakePapers()
    client = _client(papers)

    hidden = _get(client, f"/api/v1/papers/{uuid.UUID(int=0xDEAD)}")
    malformed = _get(client, "/api/v1/papers/not-a-uuid")

    assert hidden.status_code == 404
    assert hidden.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "code": "not_found",
    }
    assert hidden.headers["cache-control"] == "no-store"
    assert _comparable(hidden) == _comparable(malformed)
    # ID 不是 UUID 時不呼叫 services。
    assert [paper_id for _viewer, paper_id in papers.calls] == [uuid.UUID(int=0xDEAD)]


def test_display_names_and_client_config() -> None:
    client = _client(FakePapers())

    names = _get(client, "/api/v1/display-names")
    config = _get(client, "/api/v1/client-config")

    assert names.status_code == config.status_code == 200
    assert names.json() == {
        "journals": [{"key": "misq", "name": "MIS Quarterly"}],
        "topics": [{"key": "erp", "name": "企業資源規劃"}],
        "methods": [{"key": "survey", "name": "問卷調查"}],
    }
    assert config.json() == {
        "timezone": "Asia/Taipei",
        "library_page_size": 50,
        "paper_list_page_size": 20,
    }
    assert names.headers["cache-control"] == config.headers["cache-control"] == "no-store"


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    return dist


@pytest.mark.parametrize(
    ("path", "status", "content_type"),
    [
        ("/api/v1/papers/not-a-uuid", 404, "application/problem+json"),
        ("/api/v1/display-names", 200, "application/json"),
        ("/api/v1/client-config", 200, "application/json"),
    ],
)
def test_routes_answer_as_api_after_the_web_shell_is_mounted(
    path: str, status: int, content_type: str, web_dist: Path
) -> None:
    client = _client(FakePapers(), web_dist=web_dist)

    signed_out = _get(client, path, token=None)
    signed_in = _get(client, path)

    assert signed_out.status_code == 401
    assert signed_out.headers["content-type"] == "application/problem+json"
    assert signed_in.status_code == status
    assert signed_in.headers["content-type"] == content_type
    assert SHELL not in signed_out.text
    assert SHELL not in signed_in.text


# 論文頁新讀的四個設定檔：改壞任何一個，api 都不啟動，也不去建資料庫連線。
BROKEN_CONFIG = {
    "models.yaml": "models.yaml：",
    "budget.yaml": "budget.yaml：",
    "taxonomy.yaml": "taxonomy.yaml：",
    "context_values.yaml": "context_values.yaml：",
}


@pytest.mark.parametrize("filename", list(BROKEN_CONFIG))
def test_api_does_not_start_when_a_paper_page_config_file_is_broken(
    filename: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    (target / filename).write_text("not: [valid yaml\n", encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    reached: list[object] = []
    monkeypatch.setattr(
        api_main, "build_readiness_probes", lambda *args, **kwargs: reached.append(args)
    )

    assert api_main.main() == 1

    assert reached == []
    problems = [
        str(problem)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
        for record in [json.loads(line)]
        if record.get("event") == "設定錯誤，無法啟動"
        for problem in record["problems"]
    ]
    assert any(problem.startswith(BROKEN_CONFIG[filename]) for problem in problems), problems


def test_installing_after_the_web_shell_is_rejected(web_dist: Path) -> None:
    app = create_app(readiness_check=lambda: True)
    mount_web(app, web_dist)

    with pytest.raises(RuntimeError, match="mount_web"):
        install_paper_routes(app, FakePapers().routes(), FakeAuth().routes())
