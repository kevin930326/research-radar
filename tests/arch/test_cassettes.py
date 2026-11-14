"""錄好的外部 API 回應（`tests/cassettes/`）不得帶 key 與 email（epic 1「離線測試」、AD-29）。

掃每個 `.yaml`／`.yml`／`.json`：
- 請求網址的查詢參數名稱只能是允許清單（`filter`、`per-page`、`cursor`）；
- 請求標頭名稱只能是允許清單（`accept`、`accept-encoding`、`connection`、`host`）；
- 請求與回應都不能有 `Authorization`／`Cookie`／`Set-Cookie` 標頭；
- 檔案裡任何地方都不能出現 `api_key`、`mailto` 或 email 格式的字串。

至少要掃到一個檔；掃描函式另外拿故意寫錯的假檔證明逐項抓得到。
"""

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

from tests import vcr_settings
from tests.conftest import CASSETTES_DIR

CASSETTE_SUFFIXES = frozenset({".yaml", ".yml", ".json"})
ALLOWED_QUERY_PARAMETERS = frozenset({"filter", "per-page", "cursor"})
ALLOWED_REQUEST_HEADERS = frozenset({"accept", "accept-encoding", "connection", "host"})
FORBIDDEN_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})
FORBIDDEN_TEXT = re.compile(r"api_key|mailto", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def cassette_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix in CASSETTE_SUFFIXES
    )


def _header_names(headers: object) -> list[str]:
    return [str(name).lower() for name in headers] if isinstance(headers, Mapping) else []


def _interactions(path: Path, text: str) -> Iterable[Mapping[str, Any]]:
    data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    interactions = data.get("interactions") if isinstance(data, Mapping) else None
    if not isinstance(interactions, list):
        raise ValueError("沒有 interactions 清單")
    return [item for item in interactions if isinstance(item, Mapping)]


def cassette_problems(path: Path, *, base: Path) -> list[str]:
    """一個錄製檔違反的規則；沒有問題時是空清單。"""
    name = str(path.relative_to(base))
    text = path.read_text(encoding="utf-8")
    problems = [f"{name}：出現 {match!r}" for match in sorted(set(FORBIDDEN_TEXT.findall(text)))]
    if EMAIL.search(text):
        problems.append(f"{name}：出現 email 格式的字串")
    try:
        interactions = _interactions(path, text)
    except (ValueError, yaml.YAMLError) as exc:
        return [*problems, f"{name}：看不懂這個錄製檔（{exc}）"]

    for index, interaction in enumerate(interactions):
        request = interaction.get("request") or {}
        response = interaction.get("response") or {}
        where = f"{name}：第 {index + 1} 個請求"
        query = urlsplit(str(request.get("uri", ""))).query
        for parameter in sorted({key for key, _ in parse_qsl(query, keep_blank_values=True)}):
            if parameter not in ALLOWED_QUERY_PARAMETERS:
                problems.append(f"{where}：查詢參數 {parameter!r} 不在允許清單")
        request_headers = _header_names(request.get("headers"))
        for header in sorted(set(request_headers)):
            if header not in ALLOWED_REQUEST_HEADERS:
                problems.append(f"{where}：請求標頭 {header!r} 不在允許清單")
        for header in sorted(set(request_headers + _header_names(response.get("headers")))):
            if header in FORBIDDEN_HEADERS:
                problems.append(f"{where}：有 {header} 標頭")
    return problems


def test_real_cassettes_have_no_keys_or_emails() -> None:
    files = cassette_files(CASSETTES_DIR)

    assert files, "tests/cassettes/ 底下一個錄製檔都沒有，掃描沒有作用"
    problems = [
        problem for path in files for problem in cassette_problems(path, base=CASSETTES_DIR)
    ]
    assert problems == []


GOOD_REQUEST: dict[str, Any] = {
    "uri": "https://api.openalex.org/works?filter=x&per-page=100&cursor=%2A",
    "method": "GET",
    "headers": {"accept": ["*/*"], "host": ["api.openalex.org"]},
}


def _write(path: Path, interactions: list[dict[str, Any]], *, as_json: bool = False) -> Path:
    data = {"interactions": interactions, "version": 1}
    path.write_text(json.dumps(data) if as_json else yaml.safe_dump(data), encoding="utf-8")
    return path


def _interaction(request: Mapping[str, Any] | None = None, **response: Any) -> dict[str, Any]:
    return {
        "request": dict(request or GOOD_REQUEST),
        "response": {"status": {"code": 200}, "headers": {}, "body": {"string": "{}"}, **response},
    }


def test_scanner_passes_a_clean_cassette(tmp_path: Path) -> None:
    path = _write(tmp_path / "clean.yaml", [_interaction()])

    assert cassette_problems(path, base=tmp_path) == []


def test_scanner_catches_each_mistake(tmp_path: Path) -> None:
    cases = {
        "api_key.yaml": _interaction({**GOOD_REQUEST, "uri": GOOD_REQUEST["uri"] + "&api_key=abc"}),
        "mailto.yml": _interaction({**GOOD_REQUEST, "uri": GOOD_REQUEST["uri"] + "&mailto=x"}),
        "email_body.json": _interaction(body={"string": '{"contact": "someone@example.org"}'}),
        "authorization.yaml": _interaction(
            {**GOOD_REQUEST, "headers": {"Authorization": ["Bearer x"]}}
        ),
        "set_cookie.yaml": _interaction(headers={"Set-Cookie": ["session=1"]}),
        "cookie.yaml": _interaction({**GOOD_REQUEST, "headers": {"cookie": ["a=b"]}}),
        "param.yaml": _interaction({**GOOD_REQUEST, "uri": GOOD_REQUEST["uri"] + "&search=ai"}),
        "user_agent.yaml": _interaction(
            {**GOOD_REQUEST, "headers": {"user-agent": ["python-httpx2"]}}
        ),
    }
    found: dict[str, list[str]] = {}
    for filename, interaction in cases.items():
        path = _write(tmp_path / filename, [interaction], as_json=filename.endswith(".json"))
        found[filename] = cassette_problems(path, base=tmp_path)

    assert any("'api_key'" in p for p in found["api_key.yaml"])
    assert any("查詢參數 'api_key'" in p for p in found["api_key.yaml"])
    assert any("'mailto'" in p for p in found["mailto.yml"])
    assert found["email_body.json"] == ["email_body.json：出現 email 格式的字串"]
    assert "authorization.yaml：第 1 個請求：有 authorization 標頭" in found["authorization.yaml"]
    assert found["set_cookie.yaml"] == ["set_cookie.yaml：第 1 個請求：有 set-cookie 標頭"]
    assert "cookie.yaml：第 1 個請求：有 cookie 標頭" in found["cookie.yaml"]
    assert found["param.yaml"] == ["param.yaml：第 1 個請求：查詢參數 'search' 不在允許清單"]
    assert found["user_agent.yaml"] == [
        "user_agent.yaml：第 1 個請求：請求標頭 'user-agent' 不在允許清單"
    ]


def test_scanner_reports_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("just: text\n", encoding="utf-8")

    assert cassette_problems(path, base=tmp_path) == [
        "broken.yaml：看不懂這個錄製檔（沒有 interactions 清單）"
    ]


def test_scanner_finds_nested_files(tmp_path: Path) -> None:
    (tmp_path / "openalex").mkdir()
    _write(tmp_path / "openalex" / "a.yaml", [_interaction()])
    (tmp_path / "notes.txt").write_text("api_key", encoding="utf-8")

    assert [p.relative_to(tmp_path) for p in cassette_files(tmp_path)] == [Path("openalex/a.yaml")]


# --- 錄製設定只有一份（tests/vcr_settings.py） -----------------------------------------------

TESTS_ROOT = CASSETTES_DIR.parent
VCR_SETTING_NAMES = re.compile(r"filter_query_parameters|filter_headers|before_record_response")


def test_recording_settings_live_in_one_file() -> None:
    owners = sorted(
        str(path.relative_to(TESTS_ROOT))
        for path in TESTS_ROOT.rglob("*.py")
        if path.name != "test_cassettes.py"
        and VCR_SETTING_NAMES.search(path.read_text(encoding="utf-8"))
    )

    assert owners == ["vcr_settings.py"]


def test_recording_settings_filter_secrets() -> None:
    config = vcr_settings.vcr_config()

    assert set(config["filter_query_parameters"]) >= {"api_key", "mailto", "email"}
    assert set(config["filter_headers"]) >= {
        "authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "user-agent",
    }
    # record mode 留給 pytest-recording 的預設（none）：寫在這裡會蓋掉 --record-mode。
    assert "record_mode" not in config


def test_scrub_response_removes_cookies_and_emails() -> None:
    response = {
        "headers": {
            "Set-Cookie": ["session=1"],
            "X-Contact": ["ops@example.org"],
            "content-type": ["application/json"],
        },
        "body": {"string": b'{"author": "a.b@example.edu", "n": 1}'},
    }

    scrubbed = vcr_settings.scrub_response(response)

    assert scrubbed["headers"] == {"content-type": ["application/json"]}
    assert scrubbed["body"]["string"] == b'{"author": "<email>", "n": 1}'
