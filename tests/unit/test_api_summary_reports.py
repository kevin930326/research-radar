"""api 的回報中文重點錯誤路由（Story 3.12、ADR-0055、ADR-0064）：用假的函式組 `PaperRoutes`／
`AuthRoutes`，不連資料庫。

- `POST /api/v1/papers/{paper_id}/summary-reports` 要登入（401）、本站的 `Origin`（403）；
  先認 session
  再比對 `X-Radar-Expected-Member`（不符 409、格式不對 400，都不查論文、不寫）。
- 看不到與不是 UUID 的 ID 都回同一個 404；在驗本文之前先查看不看得到：看不到或壞 ID 帶壞本文也是
  404，不呼叫寫入（錯誤的先後同 ADR-0063）。
- 送來的值不收（services 丟 `SummaryReportInvalidError`）→ 422 `request_invalid`，
  本文剛好是 RFC 9457
  的四個欄位，不回顯說明；本文形狀不對（多欄位、不是字串、少欄位）也是 422，不呼叫寫入。
- 成員看到的那一筆已經不是現在有效的（services 丟 `SummaryChangedError`）→ 409 `summary_changed`，
  和換了人的 `member_changed` 分開（ADR-0064）。
- 200 回中文重點區塊（同論文頁的形狀）；`reported_fields` 只有欄位名稱，回應裡沒有說明、
  成員姓名或 ID。
資料全是測試自己編的（假成員、假論文）。
"""

import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.domain.ai_blocks import AiBlock, ZhSummaryValue
from radar.domain.contracts import build_client_config
from radar.domain.judgments import SummaryField
from radar.domain.summary_reports import (
    SummaryChangedError,
    SummaryReportInvalidError,
    SummaryReportProblem,
)
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import EXPECTED_MEMBER_HEADER, SESSION_COOKIE
from radar.entrypoints.api.papers import PaperRoutes, SummaryReportBody, install_paper_routes
from tests.unit.test_api_auth import GOOD_TOKEN, MEMBER, MEMBER_ID, ORIGIN, FakeAuth
from tests.unit.test_api_papers import DISPLAY_NAMES, PAGE, VISIBLE_ID

PATH = "/api/v1/papers/{id}/summary-reports"
OTHER_MEMBER_ID = uuid.UUID("00000000-0000-7000-8000-00000000000b")
NOTE = "摘要沒有寫「明顯」"
SEEN_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
BODY = {"zh_summary_id": str(SEEN_ID), "field": "findings", "note": NOTE}
PROBLEM_KEYS = {"type", "title", "status", "code"}


def _reported_block() -> AiBlock:
    value = PAGE.zh_summary.value
    assert isinstance(value, ZhSummaryValue)
    return dataclasses.replace(
        PAGE.zh_summary,
        value=dataclasses.replace(value, reported_fields=(SummaryField.FINDINGS,)),
    )


@dataclass
class FakeReports:
    """記下每一次呼叫；只有 `VISIBLE_ID` 看得到。`reject` 有值時寫入丟這個原因。"""

    reject: SummaryReportProblem | None = None
    changed: bool = False
    checks: list[tuple[MemberViewer, uuid.UUID]] = field(default_factory=list)
    writes: list[tuple[MemberViewer, uuid.UUID, SummaryReportBody]] = field(default_factory=list)

    def visible(self, viewer: MemberViewer, paper_id: uuid.UUID) -> bool:
        self.checks.append((viewer, paper_id))
        return paper_id == VISIBLE_ID

    def report(
        self, viewer: MemberViewer, paper_id: uuid.UUID, body: SummaryReportBody
    ) -> AiBlock | None:
        self.writes.append((viewer, paper_id, body))
        if self.reject is not None:
            raise SummaryReportInvalidError(self.reject)
        if self.changed:
            raise SummaryChangedError
        return _reported_block() if paper_id == VISIBLE_ID else None

    def client(self) -> TestClient:
        app = create_app(readiness_check=lambda: True)
        routes = PaperRoutes(
            load_paper=lambda _viewer, _paper_id: None,
            display_names=DISPLAY_NAMES,
            client_config=build_client_config(),
            paper_visible=self.visible,
            load_classification_fixes=lambda _viewer, _paper_id: None,
            fix_classification=lambda _viewer, _paper_id, _body: None,
            report_summary=self.report,
        )
        install_paper_routes(app, routes, FakeAuth().routes())
        return TestClient(app, base_url=ORIGIN)


def _send(
    client: TestClient,
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
    return client.request(
        "POST", PATH.format(id=paper_id), headers=headers, json=BODY if body is None else body
    )


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_reporting_needs_a_session(token: str | None) -> None:
    fake = FakeReports()

    response = _send(fake.client(), token=token)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert fake.checks == []
    assert fake.writes == []


def test_signing_in_is_checked_before_the_body() -> None:
    fake = FakeReports()

    response = _send(fake.client(), token=None, body={"field": 1})

    assert response.status_code == 401
    assert fake.writes == []


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_reporting_needs_this_site_as_origin(origin: str | None) -> None:
    fake = FakeReports()

    response = _send(fake.client(), origin=origin)

    assert response.status_code == 403
    assert response.json()["code"] == "origin_rejected"
    assert fake.writes == []


@pytest.mark.parametrize(
    ("expected", "status", "code"),
    [
        ([str(OTHER_MEMBER_ID)], 409, "member_changed"),
        (["not-a-member-id"], 400, "expected_member_invalid"),
        ([str(MEMBER_ID), str(MEMBER_ID)], 400, "expected_member_invalid"),
    ],
)
def test_expected_member_mismatch_checks_and_writes_nothing(
    expected: list[str], status: int, code: str
) -> None:
    fake = FakeReports()

    response = _send(fake.client(), expected=expected)

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == code
    assert fake.checks == []
    assert fake.writes == []


@pytest.mark.parametrize("expected", [str(MEMBER_ID), str(MEMBER_ID).upper()])
def test_the_session_member_expected_goes_through(expected: str) -> None:
    fake = FakeReports()

    response = _send(fake.client(), expected=[expected])

    assert response.status_code == 200
    assert [call[:2] for call in fake.writes] == [(MEMBER.viewer, VISIBLE_ID)]
    assert fake.writes[0][2] == SummaryReportBody(
        zh_summary_id=SEEN_ID, field="findings", note=NOTE
    )


def test_hidden_and_malformed_ids_get_the_same_404() -> None:
    fake = FakeReports()
    client = fake.client()

    hidden = _send(client, uuid.UUID(int=7))
    malformed = _send(client, "not-a-uuid")

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
    # 不是 UUID 的不查；看不到的只查了看不看得到，不呼叫寫入。
    assert [call[1] for call in fake.checks] == [uuid.UUID(int=7)]
    assert fake.writes == []


@pytest.mark.parametrize("paper_id", [uuid.UUID(int=7), "not-a-uuid"])
@pytest.mark.parametrize(
    "body", [{"field": "findings"}, ["findings"], {**BODY, "extra": 1}, {"field": 1, "note": "x"}]
)
def test_a_hidden_paper_with_a_bad_body_is_still_404(paper_id: object, body: Any) -> None:
    fake = FakeReports()
    client = fake.client()

    bad = _send(client, paper_id, body=body)
    good = _send(client, uuid.UUID(int=7))

    assert bad.status_code == 404
    assert bad.content == good.content
    assert fake.writes == []


@pytest.mark.parametrize(
    "body",
    [
        {"zh_summary_id": str(SEEN_ID), "field": "findings"},
        {"zh_summary_id": str(SEEN_ID), "note": NOTE},
        {**BODY, "extra": 1},
        {**BODY, "field": ["findings"]},
        {**BODY, "note": None},
        {**BODY, "note": 3},
        {**BODY, "note": "長" * 1001},
        {"field": "findings", "note": NOTE},
        {**BODY, "zh_summary_id": "not-a-uuid"},
        {**BODY, "zh_summary_id": None},
    ],
)
def test_a_visible_paper_with_a_bad_body_is_422_without_writing(body: Any) -> None:
    fake = FakeReports()

    response = _send(fake.client(), body=body)

    assert response.status_code == 422
    assert response.json()["code"] == "request_invalid"
    assert set(response.json()) == PROBLEM_KEYS
    assert [call[1] for call in fake.checks] == [VISIBLE_ID]
    assert fake.writes == []


@pytest.mark.parametrize("problem", list(SummaryReportProblem))
def test_a_rejected_report_is_422_without_echoing_the_note(problem: SummaryReportProblem) -> None:
    fake = FakeReports(reject=problem)

    response = _send(fake.client())

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "code": "request_invalid",
    }
    assert NOTE not in response.text


def test_the_answer_is_the_summary_block_with_field_names_only() -> None:
    fake = FakeReports()

    response = _send(fake.client())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"zh_summary"}
    assert body["zh_summary"]["state"] == "ready"
    assert body["zh_summary"]["value"]["reported_fields"] == ["findings"]
    text = json.dumps(body, ensure_ascii=False)
    # 回應只有欄位名稱：沒有說明、成員 ID、姓名。
    assert NOTE not in text
    assert str(MEMBER_ID) not in text
    assert MEMBER.display_name not in text


def test_a_summary_that_changed_before_sending_is_409_summary_changed() -> None:
    # 送出前中文重點重新產生了：409，代碼和換了人的 member_changed 不同；本文是 RFC 9457 的
    # 四個欄位。
    fake = FakeReports(changed=True)

    response = _send(fake.client(), expected=[str(MEMBER_ID)])

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "code": "summary_changed",
    }
    assert NOTE not in response.text
