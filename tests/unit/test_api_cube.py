"""api 的計數方塊路由 `GET /api/v1/cube`（Story 3.15、ADR-0062）：用假的函式組路由，不連資料庫。

- 要登入：沒登入 401 `not_authenticated`、`no-store`，不讀計數。
- 讀計數的身分是 session 的成員；回應是 `{rows, params, snapshot}`，只有計數（沒有比例），快照
  結尾 `Z`，沒有快照是 null；帶 `no-store`。
- `PaperListRoutes` 沒有組計數方塊時（只測清單的測試），呼叫是 500，不會回一份假的空資料。
資料全是測試自己編的。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.domain.contracts import CubeDim, CubeResponse, CubeRow, SnapshotRef, TrendParams
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import SESSION_COOKIE
from radar.entrypoints.api.paper_list import PaperListRoutes, install_paper_list_routes
from tests.unit.test_api_auth import GOOD_TOKEN, MEMBER_ID, ORIGIN, FakeAuth
from tests.unit.test_api_paper_list import FakePaperList

PARAMS = TrendParams(
    timezone="Asia/Taipei",
    tracking_start_year=2016,
    current_year_cutoff="07-01",
    growth_window_years=3,
    baseline_years=3,
    low_coverage_threshold=0.5,
)
AS_OF = datetime(2026, 11, 16, 2, 30, 0, 123456, tzinfo=UTC)
ROWS = (
    CubeRow(journal="misq", year=2025, dim=CubeDim.TOTAL, n=40),
    CubeRow(journal="misq", year=2025, dim=CubeDim.WITH_ABSTRACT, n=39),
)
PARAMS_JSON = {
    "timezone": "Asia/Taipei",
    "tracking_start_year": 2016,
    "current_year_cutoff": "07-01",
    "growth_window_years": 3,
    "baseline_years": 3,
    "low_coverage_threshold": 0.5,
}


@dataclass
class FakeCube:
    """記下每一次讀取的身分；回固定的計數。"""

    snapshot: SnapshotRef | None = field(
        default_factory=lambda: SnapshotRef(number=12, as_of=AS_OF)
    )
    viewers: list[MemberViewer] = field(default_factory=list)

    def load(self, viewer: MemberViewer) -> CubeResponse:
        self.viewers.append(viewer)
        rows = ROWS if self.snapshot is not None else ()
        return CubeResponse(rows=rows, params=PARAMS, snapshot=self.snapshot)

    def routes(self) -> PaperListRoutes:
        base = FakePaperList().routes()
        return PaperListRoutes(
            list_page=base.list_page,
            parse_filters=base.parse_filters,
            journal_key=base.journal_key,
            load_cube=self.load,
        )


def _get(routes: PaperListRoutes, *, token: str | None = GOOD_TOKEN) -> Response:
    app = create_app(readiness_check=lambda: True)
    install_paper_list_routes(app, routes, FakeAuth().routes())
    client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
    headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
    return client.get("/api/v1/cube", headers=headers)


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_needs_a_session(token: str | None) -> None:
    fake = FakeCube()

    response = _get(fake.routes(), token=token)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert response.headers["cache-control"] == "no-store"
    assert fake.viewers == []


def test_returns_counts_params_and_snapshot_for_the_member() -> None:
    fake = FakeCube()

    response = _get(fake.routes())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "rows": [
            {
                "journal": "misq",
                "year": 2025,
                "dim": "total",
                "key_a": None,
                "key_b": None,
                "n": 40,
            },
            {
                "journal": "misq",
                "year": 2025,
                "dim": "with_abstract",
                "key_a": None,
                "key_b": None,
                "n": 39,
            },
        ],
        "params": PARAMS_JSON,
        "snapshot": {"number": 12, "as_of": "2026-11-16T02:30:00.123456Z"},
    }
    assert [viewer.member_id for viewer in fake.viewers] == [MEMBER_ID]


def test_no_published_snapshot_yet_is_null_with_no_rows() -> None:
    response = _get(FakeCube(snapshot=None).routes())

    assert response.status_code == 200
    assert response.json() == {"rows": [], "params": PARAMS_JSON, "snapshot": None}


def test_routes_without_a_cube_reader_fail_loudly() -> None:
    response = _get(FakePaperList().routes())

    assert response.status_code == 500


def test_cube_response_never_has_rows_without_a_snapshot() -> None:
    with pytest.raises(ValueError, match="快照"):
        CubeResponse(rows=ROWS, params=PARAMS, snapshot=None)
