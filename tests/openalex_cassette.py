"""從錄好的 OpenAlex 回應（`tests/cassettes/openalex/list_works_issn_1471-7727.yaml`）取出論文。

Information and Organization（ISSN-L 1471-7727）2016 年起的 248 篇，分在三頁回應裡。
OpenAlex 的資料是 CC0（公眾領域），可以放在 repo 當測試資料；這裡只讀、不連網路、不重錄。
"""

import functools
import json
from pathlib import Path
from typing import Any

import yaml

CASSETTE = (
    Path(__file__).resolve().parent / "cassettes" / "openalex" / "list_works_issn_1471-7727.yaml"
)
ISSN_L = "1471-7727"
# 有摘要、有關鍵字、有卷期頁碼的一篇（2025 年，Human-AI agency in the age of generative AI）。
SAMPLE_ID = "W4408070694"


@functools.cache
def cassette_pages() -> tuple[tuple[dict[str, Any], ...], ...]:
    """每一頁回應的 `results`（照錄製的順序）。"""
    document = yaml.safe_load(CASSETTE.read_text(encoding="utf-8"))
    pages: list[tuple[dict[str, Any], ...]] = []
    for interaction in document["interactions"]:
        body = json.loads(interaction["response"]["body"]["string"])
        pages.append(tuple(body["results"]))
    return tuple(pages)


def cassette_works() -> list[dict[str, Any]]:
    """全部 248 篇（照錄製的順序）。回傳新的 list，呼叫端可以改。"""
    return [json.loads(json.dumps(work)) for page in cassette_pages() for work in page]


def cassette_work(openalex_id: str = SAMPLE_ID) -> dict[str, Any]:
    """其中一篇（深拷貝，呼叫端可以改）。"""
    for work in cassette_works():
        if work["id"].rsplit("/", 1)[-1] == openalex_id:
            return work
    raise KeyError(openalex_id)


def work_bytes(work: dict[str, Any]) -> bytes:
    """一篇 work 的 JSON 位元組（當作原始內容）。"""
    return json.dumps(work, ensure_ascii=False).encode("utf-8")
