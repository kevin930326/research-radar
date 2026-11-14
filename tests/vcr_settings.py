"""錄製外部 API 回應（vcrpy／pytest-recording）的唯一一份設定。

- 請求：網址濾掉 `api_key`、`mailto`、`email`；標頭濾掉 `authorization`、`cookie`、`x-api-key`、
  `api-key`、`user-agent`。比對請求時也套同一份過濾，所以帶假 key 的請求對得上錄好的回應。
- 回應：拿掉 `set-cookie` 與值裡有 email 的標頭；本文裡的 email 換成 `<email>`。
- record mode 用 pytest-recording 的預設 `none`：對不上錄好的請求就失敗，不會連到外部。
  要重錄時由 Kevin 在本機加 `--record-mode=once` 並帶真的 key 執行（不在 CI）。

`tests/arch/test_cassettes.py` 另外掃 `tests/cassettes/`，確認錄好的檔沒有 key 與 email。
"""

import re
from typing import Any

FILTERED_QUERY_PARAMETERS = ("api_key", "mailto", "email")
FILTERED_REQUEST_HEADERS = ("authorization", "cookie", "x-api-key", "api-key", "user-agent")
REMOVED_RESPONSE_HEADERS = frozenset({"set-cookie", "set-cookie2"})
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_PLACEHOLDER = "<email>"


def scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """錄下回應前：拿掉會帶身分的標頭、把本文裡的 email 換掉。"""
    headers: dict[str, list[str]] = response.get("headers", {})
    for name in list(headers):
        values = [str(value) for value in headers[name]]
        if name.lower() in REMOVED_RESPONSE_HEADERS or any(EMAIL.search(v) for v in values):
            del headers[name]
    body = response.get("body", {})
    content = body.get("string")
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="surrogateescape")
        body["string"] = EMAIL.sub(EMAIL_PLACEHOLDER, text).encode("utf-8", "surrogateescape")
    elif isinstance(content, str):
        body["string"] = EMAIL.sub(EMAIL_PLACEHOLDER, content)
    return response


def vcr_config() -> dict[str, Any]:
    """交給 pytest-recording 的 `vcr_config` fixture。不設 record_mode（預設 none）。"""
    return {
        "filter_query_parameters": list(FILTERED_QUERY_PARAMETERS),
        "filter_headers": list(FILTERED_REQUEST_HEADERS),
        "before_record_response": scrub_response,
        "decode_compressed_response": True,
    }
