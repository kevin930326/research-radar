"""api 的錯誤回應一律是 RFC 9457（Story 3.4、Conventions「錯誤」、ADR-0047）。

找不到路由、方法不對、驗證錯誤、沒登入、未預期的例外，都是 `application/problem+json`、只有
`type`／`title`／`status`／`code` 四個欄位，不帶例外訊息與輸入值；代碼在 `domain/errors.py`，狀態
只在 `problems.py` 對應；`openapi.json` 的錯誤回應與代碼列舉和實際一致。
"""

import json
from http import HTTPStatus
from typing import get_args

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.domain.auth import FakeLoginRequest, NewSession
from radar.domain.auth_settings import FakeLoginMode
from radar.domain.errors import ErrorCode
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import AuthRoutes, FakeLoginRoutes, install_auth_routes
from radar.entrypoints.api.openapi import build_schema_app
from radar.entrypoints.api.problems import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_STATUS,
    ApiProblemError,
    ProblemCode,
    code_for_status,
)
from radar.entrypoints.logging_config import configure_logging

ORIGIN = "https://testserver"
SECRET = "secret-input-9c41"


def _problem(code: str, status: int) -> dict[str, object]:
    return {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
    }


def _never_log_in(_request: FakeLoginRequest) -> NewSession:
    raise AssertionError("本文驗證失敗時不該走到登入")


def _app_with_auth() -> FastAPI:
    app = create_app(readiness_check=lambda: True)
    install_auth_routes(
        app,
        AuthRoutes(
            authenticate=lambda _token: None,
            log_out=lambda _token: None,
            allowed_origins=frozenset({ORIGIN}),
            cookie_max_age=60,
            fake_login=FakeLoginRoutes(
                mode=FakeLoginMode.PICK_MEMBER, list_members=list, log_in=_never_log_in
            ),
        ),
    )
    return app


def _assert_problem(response: Response, code: str, status: int) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json() == _problem(code, status)


# --- I/O 矩陣 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/x", "/nothing-here"])
def test_unknown_route_is_404_problem(path: str) -> None:
    client = TestClient(create_app(readiness_check=lambda: True))

    _assert_problem(client.get(path), "not_found", 404)
    head = client.head(path)
    assert head.status_code == 404
    assert head.content == b""


def test_wrong_method_is_405_problem_and_keeps_allow() -> None:
    response = TestClient(create_app(readiness_check=lambda: True)).post("/healthz")

    _assert_problem(response, "method_not_allowed", 405)
    assert response.headers["allow"]


def test_validation_error_is_422_problem_without_input_or_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    app = create_app(readiness_check=lambda: True)

    @app.post("/api/v1/echo")
    def echo(value: int) -> dict[str, int]:
        return {"value": value}

    response = TestClient(app).post(f"/api/v1/echo?value={SECRET}")

    _assert_problem(response, "request_invalid", 422)
    assert SECRET not in response.text
    logged = capsys.readouterr().out
    assert SECRET not in logged
    records = [json.loads(line) for line in logged.splitlines()]
    (warning,) = [r for r in records if r["event"] == "請求的格式不對"]
    assert warning["kinds"] == ["int_parsing"]


def test_fake_login_body_with_extra_field_is_422_problem() -> None:
    response = TestClient(_app_with_auth(), base_url=ORIGIN).post(
        "/api/v1/auth/fake-login", json={"extra": SECRET}, headers={"Origin": ORIGIN}
    )

    _assert_problem(response, "request_invalid", 422)
    assert SECRET not in response.text


def test_not_authenticated_is_401_problem_with_no_store() -> None:
    response = TestClient(_app_with_auth(), base_url=ORIGIN).get("/api/v1/me")

    _assert_problem(response, "not_authenticated", 401)
    assert response.headers["cache-control"] == "no-store"


def test_unexpected_exception_is_500_problem_without_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    app = create_app(readiness_check=lambda: True)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"secret detail {SECRET}")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    _assert_problem(response, "internal_error", 500)
    assert SECRET not in response.text
    assert "RuntimeError" not in response.text
    assert response.headers[REQUEST_ID_HEADER]
    # 日誌照舊記下例外（給開發者看）。
    assert "RuntimeError" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "request_rejected"),
        (413, "request_rejected"),
        (418, "request_rejected"),
        (503, "internal_error"),
    ],
)
def test_framework_http_exception_keeps_its_status(status: int, code: str) -> None:
    app = create_app(readiness_check=lambda: True)

    @app.get("/raise")
    def raise_it() -> None:
        raise HTTPException(status_code=status, detail="英文或內部的說明")

    response = TestClient(app).get("/raise")

    _assert_problem(response, code, status)
    assert "說明" not in response.text


def test_api_problem_uses_the_status_table() -> None:
    app = create_app(readiness_check=lambda: True)

    @app.get("/gone")
    def gone() -> None:
        raise ApiProblemError(ErrorCode.NOT_FOUND, headers={"X-Test": "1"})

    response = TestClient(app).get("/gone")

    _assert_problem(response, "not_found", 404)
    assert response.headers["x-test"] == "1"


# --- 對照表 -----------------------------------------------------------------------------------


def test_api_problem_rejects_codes_without_a_status() -> None:
    with pytest.raises(ValueError, match="llm_refusal"):
        ApiProblemError(ErrorCode.LLM_REFUSAL)


def test_problem_code_literal_is_exactly_the_status_table() -> None:
    assert sorted(get_args(ProblemCode)) == sorted(code.value for code in PROBLEM_STATUS)
    assert list(get_args(ProblemCode)) == sorted(get_args(ProblemCode))


def test_status_table_only_has_error_statuses() -> None:
    assert all(400 <= status < 600 for status in PROBLEM_STATUS.values())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErrorCode.NOT_AUTHENTICATED),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (422, ErrorCode.REQUEST_INVALID),
        (400, ErrorCode.REQUEST_REJECTED),
        (409, ErrorCode.REQUEST_REJECTED),
        (500, ErrorCode.INTERNAL_ERROR),
        (502, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_framework_status_to_code(status: int, code: ErrorCode) -> None:
    assert code_for_status(status) is code


# --- OpenAPI ----------------------------------------------------------------------------------


def test_openapi_error_responses_are_problem_json_only() -> None:
    document = build_schema_app().openapi()

    for item in document["paths"].values():
        for operation in item.values():
            default = operation["responses"]["default"]
            assert list(default["content"]) == [PROBLEM_MEDIA_TYPE]
    # FastAPI 自己格式的 422 不出現（我們的 422 是 RFC 9457）。
    assert "HTTPValidationError" not in document["components"]["schemas"]


def test_openapi_code_enum_is_the_status_table() -> None:
    schema = build_schema_app().openapi()["components"]["schemas"]["ProblemDetails"]

    assert schema["properties"]["code"]["enum"] == sorted(code.value for code in PROBLEM_STATUS)
    assert schema["required"] == ["type", "title", "status", "code"]
    assert "llm_refusal" not in schema["properties"]["code"]["enum"]


def test_openapi_operation_ids_are_unique_route_names() -> None:
    # orval 產生的前端函式名稱就是 operationId；重複的話會產生同名函式。
    document = build_schema_app().openapi()
    ids = [op["operationId"] for item in document["paths"].values() for op in item.values()]

    assert len(ids) == len(set(ids))
    assert "me" in ids
