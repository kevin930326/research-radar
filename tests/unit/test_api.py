import json
from functools import partial

import pytest
from fastapi.testclient import TestClient

from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app, resolve_request_id
from radar.entrypoints.logging_config import configure_logging
from radar.services.health import check_readiness


def _client(ready: bool = True) -> TestClient:
    return TestClient(create_app(readiness_check=lambda: ready))


def test_healthz_ok_without_touching_database() -> None:
    calls: list[None] = []

    def readiness() -> bool:
        calls.append(None)
        return False

    response = TestClient(create_app(readiness_check=readiness)).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == []


def test_readyz_ready() -> None:
    response = _client(ready=True).get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_not_ready_when_database_down() -> None:
    def ping() -> bool:
        raise OSError("connection refused")

    client = TestClient(create_app(readiness_check=partial(check_readiness, ping)))
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_valid_request_id_is_reused() -> None:
    response = _client().get("/healthz", headers={REQUEST_ID_HEADER: "abc.DEF_123-x"})

    assert response.headers[REQUEST_ID_HEADER] == "abc.DEF_123-x"


@pytest.mark.parametrize(
    "candidate",
    ["a" * 65, "bad id", "<script>", "abc/def", "oké", "中文"],
)
def test_invalid_request_id_is_replaced(candidate: str) -> None:
    assert resolve_request_id(candidate) != candidate
    assert len(resolve_request_id(candidate)) == 32


def test_invalid_request_id_header_is_replaced() -> None:
    response = _client().get("/healthz", headers={REQUEST_ID_HEADER: "x" * 65})

    returned = response.headers[REQUEST_ID_HEADER]
    assert returned != "x" * 65
    assert resolve_request_id(returned) == returned


def test_missing_request_id_gets_generated() -> None:
    response = _client().get("/healthz")

    assert (
        resolve_request_id(response.headers[REQUEST_ID_HEADER])
        == response.headers[REQUEST_ID_HEADER]
    )


def test_max_length_request_id_is_kept() -> None:
    assert resolve_request_id("a" * 64) == "a" * 64


def test_request_log_carries_same_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    response = _client().get("/readyz", headers={REQUEST_ID_HEADER: "req-42"})

    assert response.headers[REQUEST_ID_HEADER] == "req-42"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finished = [r for r in records if r["event"] == "請求完成"]
    assert len(finished) == 1
    assert finished[0]["request_id"] == "req-42"
    assert finished[0]["path"] == "/readyz"
    assert finished[0]["status"] == 200


def test_failed_request_is_logged_with_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    app = create_app(readiness_check=lambda: True)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/boom", headers={REQUEST_ID_HEADER: "req-boom"}
    )

    assert response.status_code == 500
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failed = [r for r in records if r["event"] == "請求失敗"]
    assert len(failed) == 1
    assert failed[0]["request_id"] == "req-boom"
    assert failed[0]["path"] == "/boom"
    assert failed[0]["level"] == "error"
    assert "RuntimeError" in failed[0]["exception"]
