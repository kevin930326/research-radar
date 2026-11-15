"""全站安全標頭與成員版前端的頁面殼（Story 3.1、AD-21、AD-22、ADR-0029）。"""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from radar.entrypoints.api import web
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.web import security_headers

ROOT = Path(__file__).resolve().parents[2]
HEADERS_FILE = ROOT / "radar" / "entrypoints" / "api" / "security_headers.json"
SHELL = "<!doctype html><title>shell</title>"
ASSET = "console.log('asset');"


def _expected_headers() -> dict[str, str]:
    loaded: dict[str, str] = json.loads(HEADERS_FILE.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text(ASSET, encoding="utf-8")
    # 建置資料夾外面的檔案：路徑穿越不可以讀到它。
    (tmp_path / "web" / "secret.txt").write_text("outside", encoding="utf-8")
    return dist


@pytest.fixture
def client(web_dist: Path) -> TestClient:
    return TestClient(create_app(readiness_check=lambda: True, web_dist=web_dist))


def _assert_security_headers(headers: Mapping[str, str]) -> None:
    for name, value in _expected_headers().items():
        assert headers[name] == value, name


# ADR-0029 決定 1 的內容：檔案被改掉時這裡會失敗，不是拿檔案和自己比。
CSP_DIRECTIVES = {
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
}
OTHER_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def test_security_headers_match_the_single_source_file() -> None:
    assert security_headers() == _expected_headers()


def test_security_headers_are_exactly_the_decided_set() -> None:
    headers = security_headers()
    csp = headers["Content-Security-Policy"]

    assert {part.strip() for part in csp.split(";")} == CSP_DIRECTIVES
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    others = {name: value for name, value in headers.items() if name != "Content-Security-Policy"}
    assert others == OTHER_HEADERS
    # HSTS 不在應用程式設（ADR-0029）。
    assert "Strict-Transport-Security" not in headers


@pytest.mark.parametrize("content", ["[]", "{}", '{"X": ""}', '{"X": 1}'])
def test_broken_headers_file_stops_startup(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    monkeypatch.setattr(web, "_read_headers_file", lambda: content)
    security_headers.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"security_headers\.json"):
            security_headers()
        security_headers.cache_clear()
        # create_app 本身就失敗，不是等到第一個請求。
        with pytest.raises(ValueError, match=r"security_headers\.json"):
            create_app(readiness_check=lambda: True)
    finally:
        security_headers.cache_clear()


@pytest.mark.parametrize("path", ["/", "/notice", "/a/b", "/api/v1/x", "/healthz", "/readyz"])
def test_every_response_carries_security_headers(client: TestClient, path: str) -> None:
    _assert_security_headers(client.get(path).headers)


def test_server_error_response_carries_security_headers() -> None:
    # 不掛頁面殼：測試路由要排在「其餘路徑」之前才打得到。
    app = create_app(readiness_check=lambda: True)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("測試用的例外")

    with structlog.testing.capture_logs():
        response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    _assert_security_headers(response.headers)


@pytest.mark.parametrize("path", ["/", "/notice", "/a/b", "/papers/123?tab=x"])
def test_unknown_non_api_paths_return_the_shell(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.text == SHELL
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"


def test_head_request_returns_the_shell_headers(client: TestClient) -> None:
    response = client.head("/notice")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/api", "/api/x", "/api/v1/x", "/api/v1/papers/1"])
def test_api_paths_stay_404_json(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/api/v1/x", "/api/v1/不存在", "/api"])
def test_api_paths_are_404_json_for_every_method(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    _assert_security_headers(response.headers)


# 不只常見方法：TRACE、CONNECT、自訂方法也不能因為方法清單變成 405（Codex 審查 PR #61）。
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE", "CONNECT", "PROPFIND", "PURGE"])
@pytest.mark.parametrize("path", ["/api/v1/x", "/api"])
def test_api_paths_are_404_for_methods_outside_any_list(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)

    assert response.status_code == 404
    if method != "HEAD":
        assert response.json() == {"detail": "Not Found"}
    _assert_security_headers(response.headers)


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND"])
def test_shell_paths_reject_unlisted_methods_with_405(client: TestClient, method: str) -> None:
    response = client.request(method, "/notice")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


def test_direct_index_request_is_not_cached(client: TestClient) -> None:
    for method in ("GET", "HEAD"):
        response = client.request(method, "/index.html")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
    assert client.get("/index.html").text == SHELL


def test_static_assets_do_not_get_the_shell_cache_header(client: TestClient) -> None:
    assert "cache-control" not in client.get("/assets/index-abc123.js").headers


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_shell_paths_only_accept_get_and_head(client: TestClient, method: str) -> None:
    response = client.request(method, "/notice")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    _assert_security_headers(response.headers)


def test_health_routes_win_over_the_shell(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_static_file_is_served(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.text == ASSET
    assert "javascript" in response.headers["content-type"]
    _assert_security_headers(response.headers)


@pytest.mark.parametrize(
    "path",
    [
        "/assets/../../pyproject.toml",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2E%2E%2F%2E%2E%2Fsecret.txt",
        "/..%2fsecret.txt",
        "/assets/%00.js",
    ],
)
def test_path_traversal_never_reads_outside_the_build(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code in {200, 404}
    assert "outside" not in response.text
    assert "[project]" not in response.text
    if response.status_code == 200:
        assert response.text == SHELL


def test_symlink_out_of_the_build_is_not_followed(web_dist: Path, client: TestClient) -> None:
    (web_dist / "assets" / "link.txt").symlink_to(web_dist.parent / "secret.txt")

    response = client.get("/assets/link.txt")

    assert response.text == SHELL


def test_index_symlinked_out_of_the_build_is_not_mounted(tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    outside = tmp_path / "web" / "secret.txt"
    outside.write_text("outside", encoding="utf-8")
    (dist / "index.html").symlink_to(outside)

    with structlog.testing.capture_logs() as logs:
        client = TestClient(create_app(readiness_check=lambda: True, web_dist=dist))

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    for path in ("/", "/notice", "/index.html"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "outside" not in response.text
    assert any(entry["log_level"] == "warning" for entry in logs)


def test_index_symlinked_inside_the_build_is_still_served(tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "real.html").write_text(SHELL, encoding="utf-8")
    (dist / "index.html").symlink_to(dist / "real.html")

    client = TestClient(create_app(readiness_check=lambda: True, web_dist=dist))

    response = client.get("/notice")
    assert response.text == SHELL
    assert response.headers["cache-control"] == "no-cache"


def test_without_web_dist_only_api_is_served() -> None:
    client = TestClient(create_app(readiness_check=lambda: True))

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    response = client.get("/notice")
    assert response.status_code == 404
    _assert_security_headers(response.headers)


def test_missing_web_dist_logs_warning_and_starts(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as logs:
        client = TestClient(
            create_app(readiness_check=lambda: True, web_dist=tmp_path / "web" / "dist")
        )

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/").status_code == 404
    assert client.get("/notice").status_code == 404
    assert any(entry["log_level"] == "warning" for entry in logs)


# Opus 修正確認（PR #61 第二輪）：/healthz、/readyz 收 HEAD，其他方法回 405、不落到頁面殼。
@pytest.mark.parametrize(("ready", "status"), [(False, 503), (True, 200)])
def test_head_readyz_matches_get_status(web_dist: Path, ready: bool, status: int) -> None:
    client = TestClient(create_app(readiness_check=lambda: ready, web_dist=web_dist))

    response = client.head("/readyz")

    assert response.status_code == status
    assert response.content == b""
    assert response.headers["content-type"] == "application/json"
    _assert_security_headers(response.headers)


def test_head_readyz_without_web_dist_matches_get_status() -> None:
    client = TestClient(create_app(readiness_check=lambda: False))

    assert client.head("/readyz").status_code == 503


def test_head_healthz_is_200_and_not_the_shell(client: TestClient) -> None:
    response = client.head("/healthz")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"] == "application/json"
    assert "cache-control" not in response.headers


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "TRACE", "OPTIONS", "PROPFIND"])
@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_routes_reject_other_methods_with_405(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert not response.headers.get("content-type", "").startswith("text/html")
    _assert_security_headers(response.headers)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_framework_routes_reject_post_with_405(client: TestClient, path: str) -> None:
    response = client.post(path)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


def test_health_routes_head_is_not_in_the_api_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/healthz"]) == {"get"}
    assert set(paths["/readyz"]) == {"get"}


# 掛上之後 index.html 被換成指到外面的符號連結：每次請求都重新檢查，不送外面的檔案。
def test_index_swapped_for_outside_symlink_after_mount_is_404(
    web_dist: Path, client: TestClient
) -> None:
    index = web_dist / "index.html"
    index.unlink()
    index.symlink_to(web_dist.parent / "secret.txt")

    with structlog.testing.capture_logs():
        for method in ("GET", "HEAD"):
            for path in ("/", "/notice", "/index.html"):
                response = client.request(method, path)
                assert response.status_code == 404, (method, path)
                assert "outside" not in response.text
                assert not response.headers.get("content-type", "").startswith("text/html")
                _assert_security_headers(response.headers)
    # 建置資料夾裡真的存在的檔案照送。
    assert client.get("/assets/index-abc123.js").text == ASSET
