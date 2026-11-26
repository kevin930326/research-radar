"""api 的修正分類路由（Story 3.11、ADR-0055）：用假的函式組 `PaperRoutes`／`AuthRoutes`，
不連資料庫。

- `GET`／`POST /api/v1/papers/{paper_id}/classification-fixes` 都要登入（401），先認 session 再
  比對 `X-Radar-Expected-Member`（不符 409、格式不對 400，都不呼叫 services）；POST 要本站的
  `Origin`（403）。
- 看不到（假函式回 None）與不是 UUID 的 ID 都回同一個 404。POST 在驗本文之前先查看不看得到：
  看不到或壞 ID 帶壞本文也是 404（ADR-0063 錯誤的先後），不呼叫寫入。
- 送來的值不收（services 丟 `ClassificationFixInvalidError`）→ 422 `request_invalid`，本文剛好是
  RFC 9457 的四個欄位，不回顯送來的值；本文形狀不對（多欄位、不是字串的清單）也是 422，不呼叫
  services。
- 200 照 services 的結果轉換：分類區塊與歷史；歷史每筆只有時間、姓名、主題、方法、理由（沒有
  email、成員 ID）。
資料全是測試自己編的（假成員、假論文）。
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.domain.ai_blocks import AiBlock, AiBlockState, AiProduct
from radar.domain.classification_fixes import (
    ClassificationFixEntry,
    ClassificationFixInvalidError,
    ClassificationFixProblem,
)
from radar.domain.contracts import build_client_config
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import EXPECTED_MEMBER_HEADER, SESSION_COOKIE
from radar.entrypoints.api.papers import (
    ClassificationFixBody,
    PaperRoutes,
    install_paper_routes,
)
from radar.services.judgments import ClassificationFixes
from tests.unit.test_api_auth import GOOD_TOKEN, MEMBER, MEMBER_ID, ORIGIN, FakeAuth
from tests.unit.test_api_papers import DISPLAY_NAMES, PAGE, VISIBLE_ID

PATH = "/api/v1/papers/{id}/classification-fixes"
OTHER_MEMBER_ID = uuid.UUID("00000000-0000-7000-8000-00000000000b")
FIXED_AT = datetime(2026, 11, 19, 2, 30, tzinfo=UTC)
FIXES = ClassificationFixes(
    classification=PAGE.classification,
    history=(
        ClassificationFixEntry(
            fixed_at=FIXED_AT,
            member_name="測試成員甲",
            topic=("digital_platforms", "e_commerce"),
            method=("experiment",),
            reason="研究的是跨平台的評分",
        ),
        ClassificationFixEntry(
            fixed_at=FIXED_AT.replace(day=14),
            member_name="測試成員乙",
            topic=None,
            method=("experiment",),
            reason=None,
        ),
    ),
)
BODY = {"topic": ["e_commerce", "digital_platforms"], "method": ["experiment"], "reason": "跨平台"}


@dataclass
class FakeFixes:
    """記下每一次呼叫；只有 `VISIBLE_ID` 看得到。`reject` 有值時寫入丟這個原因。"""

    reject: ClassificationFixProblem | None = None
    # 換掉讀到的分類區塊（沒給就用 FIXES）。
    blocks: dict[uuid.UUID, AiBlock] = field(default_factory=dict)
    reads: list[tuple[MemberViewer, uuid.UUID]] = field(default_factory=list)
    checks: list[tuple[MemberViewer, uuid.UUID]] = field(default_factory=list)
    writes: list[tuple[MemberViewer, uuid.UUID, ClassificationFixBody]] = field(
        default_factory=list
    )

    def load(self, viewer: MemberViewer, paper_id: uuid.UUID) -> ClassificationFixes | None:
        self.reads.append((viewer, paper_id))
        if paper_id in self.blocks:
            return ClassificationFixes(classification=self.blocks[paper_id], history=())
        return FIXES if paper_id == VISIBLE_ID else None

    def visible(self, viewer: MemberViewer, paper_id: uuid.UUID) -> bool:
        self.checks.append((viewer, paper_id))
        return paper_id == VISIBLE_ID or paper_id in self.blocks

    def fix(
        self, viewer: MemberViewer, paper_id: uuid.UUID, body: ClassificationFixBody
    ) -> ClassificationFixes | None:
        self.writes.append((viewer, paper_id, body))
        if self.reject is not None:
            raise ClassificationFixInvalidError(self.reject, "topic")
        return FIXES if paper_id == VISIBLE_ID else None

    def client(self) -> TestClient:
        app = create_app(readiness_check=lambda: True)
        routes = PaperRoutes(
            load_paper=lambda _viewer, _paper_id: None,
            display_names=DISPLAY_NAMES,
            client_config=build_client_config(),
            paper_visible=self.visible,
            load_classification_fixes=self.load,
            fix_classification=self.fix,
            # 回報中文重點錯誤（Story 3.12）：這個檔不測（見 test_api_summary_reports.py）。
            report_summary=lambda _viewer, _paper_id, _body: None,
        )
        install_paper_routes(app, routes, FakeAuth().routes())
        return TestClient(app, base_url=ORIGIN)


def _send(
    client: TestClient,
    method: str,
    paper_id: object = VISIBLE_ID,
    *,
    token: str | None = GOOD_TOKEN,
    origin: str | None = ORIGIN,
    expected: list[str] | None = None,
    body: Any = None,
) -> Response:
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Cookie", f"{SESSION_COOKIE}={token}"))
    if origin is not None:
        headers.append(("Origin", origin))
    headers.extend((EXPECTED_MEMBER_HEADER, value) for value in expected or [])
    client.cookies.clear()
    path = PATH.format(id=paper_id)
    if method == "POST":
        return client.request(method, path, headers=headers, json=BODY if body is None else body)
    return client.request(method, path, headers=headers)


PROBLEM_KEYS = {"type", "title", "status", "code"}


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_both_need_a_session(method: str, token: str | None) -> None:
    fake = FakeFixes()

    response = _send(fake.client(), method, token=token)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert fake.reads == []
    assert fake.checks == []
    assert fake.writes == []


def test_signing_in_is_checked_before_the_body() -> None:
    # 本文不對、也沒登入：先回 401（相依項在本文驗證之前），不先給 422。
    fake = FakeFixes()

    response = _send(fake.client(), "POST", token=None, body={"topic": 1})

    assert response.status_code == 401


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_fixing_needs_this_site_as_origin(origin: str | None) -> None:
    fake = FakeFixes()

    response = _send(fake.client(), "POST", origin=origin)

    assert response.status_code == 403
    assert response.json()["code"] == "origin_rejected"
    assert fake.writes == []


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    ("expected", "status", "code"),
    [
        ([str(OTHER_MEMBER_ID)], 409, "member_changed"),
        (["not-a-member-id"], 400, "expected_member_invalid"),
        ([str(MEMBER_ID), str(MEMBER_ID)], 400, "expected_member_invalid"),
    ],
)
def test_expected_member_mismatch_reads_and_writes_nothing(
    method: str, expected: list[str], status: int, code: str
) -> None:
    fake = FakeFixes()

    response = _send(fake.client(), method, expected=expected)

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == code
    assert fake.reads == []
    assert fake.checks == []
    assert fake.writes == []


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("expected", [str(MEMBER_ID), str(MEMBER_ID).upper()])
def test_the_session_member_expected_goes_through(method: str, expected: str) -> None:
    fake = FakeFixes()

    response = _send(fake.client(), method, expected=[expected])

    assert response.status_code == 200
    calls = fake.reads if method == "GET" else fake.writes
    assert [call[:2] for call in calls] == [(MEMBER.viewer, VISIBLE_ID)]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_hidden_and_malformed_ids_get_the_same_404(method: str) -> None:
    fake = FakeFixes()
    client = fake.client()

    hidden = _send(client, method, uuid.UUID(int=7))
    malformed = _send(client, method, "not-a-uuid")

    for response in (hidden, malformed):
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "code": "not_found",
        }
    assert hidden.content == malformed.content
    # 不是 UUID 的不呼叫 services；POST 看不到的只查了看不看得到，不呼叫寫入。
    calls = fake.reads if method == "GET" else fake.checks
    assert [call[1] for call in calls] == [uuid.UUID(int=7)]
    assert fake.writes == []


@pytest.mark.parametrize("paper_id", [uuid.UUID(int=7), "not-a-uuid"])
@pytest.mark.parametrize("body", [{"topic": "e_commerce"}, ["e_commerce"], {"extra": 1}])
def test_a_hidden_paper_with_a_bad_body_is_still_404(paper_id: object, body: Any) -> None:
    # 錯誤的先後（ADR-0063）：先解析 ID、查看不看得到，再驗本文；壞本文不會蓋過 404。
    fake = FakeFixes()
    client = fake.client()

    response = _send(client, "POST", paper_id, body=body)
    hidden_good_body = _send(client, "POST", uuid.UUID(int=7))

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.content == hidden_good_body.content
    assert fake.writes == []


def test_a_visible_paper_with_a_bad_body_is_422_after_the_check() -> None:
    fake = FakeFixes()

    response = _send(fake.client(), "POST", body={"topic": "e_commerce"})

    assert response.status_code == 422
    assert fake.checks == [(MEMBER.viewer, VISIBLE_ID)]
    assert fake.writes == []


def test_fixing_passes_the_body_as_sent_and_returns_the_new_state() -> None:
    fake = FakeFixes()

    response = _send(fake.client(), "POST")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    [(viewer, paper_id, body)] = fake.writes
    assert (viewer, paper_id) == (MEMBER.viewer, VISIBLE_ID)
    assert body == ClassificationFixBody(
        topic=["e_commerce", "digital_platforms"], method=["experiment"], reason="跨平台"
    )
    assert set(response.json()) == {"classification", "history"}


def test_history_has_only_the_name_and_no_member_details() -> None:
    response = _send(FakeFixes().client(), "GET")

    body = response.json()
    assert body["classification"]["state"] == "ready"
    assert body["history"] == [
        {
            "fixed_at": "2026-11-19T02:30:00Z",
            "member_name": "測試成員甲",
            "topic": ["digital_platforms", "e_commerce"],
            "method": ["experiment"],
            "reason": "研究的是跨平台的評分",
        },
        {
            "fixed_at": "2026-11-14T02:30:00Z",
            "member_name": "測試成員乙",
            "topic": None,
            "method": ["experiment"],
            "reason": None,
        },
    ]
    assert "@" not in response.text
    assert "email" not in response.text
    assert "member_id" not in response.text
    assert str(MEMBER_ID) not in response.text


@pytest.mark.parametrize("problem", list(ClassificationFixProblem))
def test_rejected_values_are_422_without_echo(problem: ClassificationFixProblem) -> None:
    fake = FakeFixes(reject=problem)
    body = {"topic": ["<x>secret-topic"], "method": ["experiment"], "reason": "secret-reason"}

    response = _send(fake.client(), "POST", body=body)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "code": "request_invalid",
    }
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"topic": ["e_commerce"], "method": ["experiment"], "extra": 1},
        {"topic": "e_commerce", "method": ["experiment"]},
        {"topic": [1], "method": ["experiment"]},
        {"topic": ["e_commerce"]},
        {"topic": ["e_commerce"], "method": ["experiment"], "reason": 3},
        {"topic": ["e_commerce"] * 65, "method": ["experiment"]},
        {"topic": ["e_commerce"], "method": ["experiment"], "reason": "字" * 1001},
        ["e_commerce"],
    ],
)
def test_malformed_bodies_are_422_and_never_reach_services(body: Any) -> None:
    fake = FakeFixes()

    response = _send(fake.client(), "POST", body=body)

    assert response.status_code == 422
    assert set(response.json()) == PROBLEM_KEYS
    assert response.json()["code"] == "request_invalid"
    assert fake.writes == []


def test_reason_may_be_left_out() -> None:
    fake = FakeFixes()

    response = _send(fake.client(), "POST", body={"topic": ["erp"], "method": ["experiment"]})

    assert response.status_code == 200
    assert fake.writes[0][2].reason is None


def test_classification_block_is_the_paper_page_shape() -> None:
    # 回應的分類區塊和論文頁的 `classification` 同一個形狀（前端直接換上）。
    block = AiBlock(AiProduct.CLASSIFICATION, AiBlockState.PENDING_BATCH)
    fake = FakeFixes(blocks={VISIBLE_ID: block})

    body = _send(fake.client(), "GET").json()

    assert body == {
        "classification": {"state": "pending_batch", "value": None, "provenance": None},
        "history": [],
    }
