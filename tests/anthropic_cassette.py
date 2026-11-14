"""讀 `tests/cassettes/anthropic/` 的手寫假回應，轉成 `httpx2.Response`（不連網路）。

那些檔是照官方文件手寫的，不是真的錄的（見同資料夾的 README.md）。
"""

import functools
from pathlib import Path
from typing import Any

import httpx2
import yaml

CASSETTES = Path(__file__).resolve().parent / "cassettes" / "anthropic"
NAMES = (
    "ok_json",
    "ok_text",
    "refusal",
    "max_tokens",
    "rate_limited",
    "overloaded",
    "invalid_request",
    "authentication",
)


@functools.cache
def _interaction(name: str) -> dict[str, Any]:
    document = yaml.safe_load((CASSETTES / f"{name}.yaml").read_text(encoding="utf-8"))
    [interaction] = document["interactions"]
    result: dict[str, Any] = interaction
    return result


def cassette_request(name: str) -> tuple[str, str]:
    """這個假回應對應的請求（方法、網址）。"""
    request = _interaction(name)["request"]
    return str(request["method"]), str(request["uri"])


def cassette_body(name: str) -> str:
    """回應本文（字串）。"""
    return str(_interaction(name)["response"]["body"]["string"])


def cassette_response(name: str) -> httpx2.Response:
    """每次回一個新的 `httpx2.Response`（回應讀過就不能再讀，所以不共用）。"""
    response = _interaction(name)["response"]
    headers = [
        (str(key), str(value)) for key, values in response["headers"].items() for value in values
    ]
    return httpx2.Response(
        int(response["status"]["code"]),
        headers=headers,
        content=cassette_body(name).encode("utf-8"),
    )
