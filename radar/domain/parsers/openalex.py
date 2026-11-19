"""OpenAlex 的一篇 work（原始 JSON 位元組）→ `ParsedPaper`（FR-2、FR-8、ADR-0014、ADR-0022）。

純函式：不連網路、不碰資料庫、不讀設定。內容是外部的不可信輸入，看不懂就丟 `ParseError`，
不猜：不是 UTF-8 的 JSON 物件、結構或型別不對（布林不算整數）、必要值缺（`id`、`title`）、
識別碼看不懂、關鍵字的 `id` 不是 `https://openalex.org/keywords/<key>`、倒排索引的位置重複或
不是 ≥0 的整數、巢狀太深。可省略的值是 `null` 或沒有時當作沒有（None 或空 tuple）。

欄位對應見 `parse_openalex_work` 的說明。
"""

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from radar.domain.identifiers import (
    InvalidIdentifierError,
    normalize_doi,
    normalize_issn_l,
    normalize_openalex_id,
)
from radar.domain.papers import (
    KEYWORD_KEY_MAX_LENGTH,
    AbstractSource,
    ParsedKeyword,
    ParsedPaper,
    check_storable_text,
)
from radar.domain.parsers import ParseError

# OpenAlex 關鍵字的 `id`：`https://openalex.org/keywords/<key>`，key 是小寫英文與數字、
# 中間可以用單一連字號接起來（和 `ParsedKeyword.key` 同一個格式）。
_KEYWORD_ID = re.compile(r"https://openalex\.org/keywords/([a-z0-9]+(?:-[a-z0-9]+)*)")

_MISSING = object()


def parse_openalex_work(content: bytes, *, fetched_at: datetime) -> ParsedPaper:
    """把 OpenAlex 的一篇 work 解析成 `ParsedPaper`。`fetched_at` 來自 `raw_fetch`。

    欄位對應：
    - `openalex_id`←`id`（必要）；`doi`←`doi`。
    - `title`←`title`，`null` 或空白時用 `display_name`（都沒有就是看不懂）。
    - `authors`←`authorships[].author.display_name`，`null` 或空白時用 `raw_author_name`；兩個都
      沒有的那位略過；保持順序。
    - `journal_issn_l`←`primary_location.source.issn_l`；`publication_year`←`publication_year`
      （比抓取年晚也照收、不改）。
    - `volume`／`issue`／`first_page`／`last_page`←`biblio`（去空白，空字串當沒有）。
    - `work_type`←`type`；`is_retracted`←`is_retracted`（沒有時 False）。
    - `abstract`←`abstract_inverted_index` 依位置排序後以單一空白接起來、去頭尾空白（位置可以有
      缺號）；沒有或還原後是空的就是 None。有摘要時 `abstract_source`＝`openalex`。
    - `referenced_works`←正規化的 W 開頭 ID，去重複、保持順序；`cited_by_count`。
    - `keywords`←（`id` 的最後一段, `display_name`），同一個 key 只留第一個。
    - `primary_license`←`primary_location.license`（去頭尾空白，看不懂的字串也照存）；
      `publisher_ids`←`primary_location.source.host_organization`＋`host_organization_lineage`
      （正規化的 OpenAlex ID，看不懂的照原字串）；`publisher_names`←`host_organization_name`＋
      `host_organization_lineage_names`。只看 `primary_location`，不看 `best_oa_location`、
      `locations`（ADR-0052）。這三個欄位只拿來判定示範站可用，所以**不丟 `ParseError`**：
      型別不對、空白、存不進資料庫的值當作沒有（判定一律往「不可用」那邊走），不會讓一篇
      論文因為這幾個欄位而整篇解析失敗。

    Raises:
        ParseError: 看不懂（訊息只有欄位路徑與原因，不含內容值）。
    """
    try:
        return _parse(content, fetched_at)
    except RecursionError:
        raise ParseError("（最上層）", "巢狀太深") from None


def _parse(content: bytes, fetched_at: datetime) -> ParsedPaper:
    if not isinstance(content, bytes):
        raise ParseError("（最上層）", "內容要是位元組")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ParseError("（最上層）", "不是 UTF-8") from None
    try:
        work: Any = json.loads(text)
    except ValueError:
        raise ParseError("（最上層）", "不是合法的 JSON") from None
    if not isinstance(work, dict):
        raise ParseError("（最上層）", "要是 JSON 物件")

    openalex_id = _identifier(_require(work, "id", str, "id"), "id", normalize_openalex_id)
    doi_value = _optional(work, "doi", str, "doi")
    doi = None if doi_value is None else _identifier(doi_value, "doi", normalize_doi)

    title = _blank_to_none(_optional(work, "title", str, "title"))
    if title is None:
        title = _blank_to_none(_optional(work, "display_name", str, "display_name"))
    if title is None:
        raise ParseError("title", "缺少（title 與 display_name 都沒有或是空白）")

    abstract = _abstract(work)
    biblio = _optional(work, "biblio", dict, "biblio") or {}
    values: dict[str, object] = {
        "source": "openalex",
        "fetched_at": fetched_at,
        "doi": doi,
        "openalex_id": openalex_id,
        "title": title,
        "journal_issn_l": _journal_issn_l(work),
        "publication_year": _optional(work, "publication_year", int, "publication_year"),
        "authors": _authors(work),
        "volume": _biblio_text(biblio, "volume"),
        "issue": _biblio_text(biblio, "issue"),
        "first_page": _biblio_text(biblio, "first_page"),
        "last_page": _biblio_text(biblio, "last_page"),
        "work_type": _optional(work, "type", str, "type"),
        "is_retracted": _optional(work, "is_retracted", bool, "is_retracted") or False,
        "abstract": abstract,
        "abstract_source": None if abstract is None else AbstractSource.OPENALEX,
        "referenced_works": _referenced_works(work),
        "cited_by_count": _optional(work, "cited_by_count", int, "cited_by_count"),
        "keywords": _keywords(work),
        "primary_license": _primary_license(work),
        "publisher_ids": _publisher_values(work, _PUBLISHER_ID_KEYS, _publisher_id),
        "publisher_names": _publisher_values(work, _PUBLISHER_NAME_KEYS, _publisher_name),
    }
    try:
        return ParsedPaper.model_validate(values)
    except ValidationError as exc:
        # 只帶欄位路徑與錯誤種類：pydantic 的訊息可能含輸入值（例如識別碼）。
        error = exc.errors(include_input=False, include_url=False, include_context=False)[0]
        location = ".".join(str(part) for part in error["loc"]) or "（最上層）"
        raise ParseError(location, f"驗證失敗（{error['type']}）") from None


# --- 型別檢查 -------------------------------------------------------------------------------

_KIND_NAMES: Mapping[type, str] = {
    str: "字串",
    int: "整數",
    bool: "布林",
    dict: "物件",
    list: "陣列",
}


def _check_kind[T](value: object, kind: type[T], path: str) -> T:
    """`value` 要是 `kind`；布林不算整數。"""
    if kind is int and isinstance(value, bool):
        raise ParseError(path, f"要是{_KIND_NAMES[kind]}")
    if not isinstance(value, kind):
        raise ParseError(path, f"要是{_KIND_NAMES[kind]}")
    return value


def _optional[T](container: Mapping[str, Any], key: str, kind: type[T], path: str) -> T | None:
    """可省略的值：沒有或 `null` 回 None，否則要是 `kind`。"""
    value = container.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    return _check_kind(value, kind, path)


def _require[T](container: Mapping[str, Any], key: str, kind: type[T], path: str) -> T:
    value = _optional(container, key, kind, path)
    if value is None:
        raise ParseError(path, "缺少")
    return value


def _identifier(value: str, path: str, normalize: Callable[[str], str]) -> str:
    try:
        normalized = normalize(value)
    except InvalidIdentifierError:
        raise ParseError(path, "看不懂的識別碼") from None
    return normalized


# --- 各欄位 ---------------------------------------------------------------------------------


def _journal_issn_l(work: Mapping[str, Any]) -> str | None:
    location = _optional(work, "primary_location", dict, "primary_location")
    if location is None:
        return None
    source = _optional(location, "source", dict, "primary_location.source")
    if source is None:
        return None
    path = "primary_location.source.issn_l"
    issn_l = _optional(source, "issn_l", str, path)
    return None if issn_l is None else _identifier(issn_l, path, normalize_issn_l)


def _primary_source(work: Mapping[str, Any]) -> Mapping[str, Any] | None:
    location = _optional(work, "primary_location", dict, "primary_location")
    if location is None:
        return None
    return _optional(location, "source", dict, "primary_location.source")


def _storable(value: object) -> str | None:
    """字串去頭尾空白；不是字串、空白、存不進資料庫的回 None（寬鬆讀取，不丟錯）。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        check_storable_text(stripped, "出版社或授權")
    except ValueError:
        return None
    return stripped


def _primary_license(work: Mapping[str, Any]) -> str | None:
    location = _optional(work, "primary_location", dict, "primary_location")
    if location is None:
        return None
    return _storable(location.get("license"))


# （直屬的欄位, lineage 的欄位）
_PUBLISHER_ID_KEYS = ("host_organization", "host_organization_lineage")
_PUBLISHER_NAME_KEYS = ("host_organization_name", "host_organization_lineage_names")


def _publisher_id(value: object) -> str | None:
    text = _storable(value)
    if text is None:
        return None
    try:
        return normalize_openalex_id(text)
    except InvalidIdentifierError:
        return text


def _publisher_name(value: object) -> str | None:
    return _storable(value)


def _publisher_values(
    work: Mapping[str, Any],
    keys: tuple[str, str],
    clean: Callable[[object], str | None],
) -> tuple[str, ...]:
    """直屬的值在最前面，接著 lineage；去重複、保持順序；看不懂的值略過（不丟錯）。"""
    source = _primary_source(work)
    if source is None:
        return ()
    direct, lineage = source.get(keys[0]), source.get(keys[1])
    candidates = [direct, *(lineage if isinstance(lineage, list) else [])]
    seen: dict[str, None] = {}
    for candidate in candidates:
        cleaned = clean(candidate)
        if cleaned is not None:
            seen.setdefault(cleaned, None)
    return tuple(seen)


def _authors(work: Mapping[str, Any]) -> tuple[str, ...]:
    authorships = _optional(work, "authorships", list, "authorships") or []
    names: list[str] = []
    for index, authorship in enumerate(authorships):
        path = f"authorships.{index}"
        entry = _check_kind(authorship, dict, path)
        author = _optional(entry, "author", dict, f"{path}.author")
        name = None
        if author is not None:
            name = _blank_to_none(
                _optional(author, "display_name", str, f"{path}.author.display_name")
            )
        if name is None:
            name = _blank_to_none(
                _optional(entry, "raw_author_name", str, f"{path}.raw_author_name")
            )
        if name is not None:
            names.append(name)
    return tuple(names)


def _blank_to_none(value: str | None) -> str | None:
    """去頭尾空白後是空的就當作沒有。"""
    if value is None:
        return None
    return value.strip() or None


def _biblio_text(biblio: Mapping[str, Any], key: str) -> str | None:
    return _blank_to_none(_optional(biblio, key, str, f"biblio.{key}"))


def _abstract(work: Mapping[str, Any]) -> str | None:
    """倒排索引（字 → 出現的位置）還原成原文：依位置排序、以單一空白接起來、去頭尾空白。"""
    path = "abstract_inverted_index"
    index = _optional(work, "abstract_inverted_index", dict, path)
    if not index:
        return None
    words: dict[int, str] = {}
    for word, positions in index.items():
        for position in _check_kind(positions, list, path):
            if not isinstance(position, int) or isinstance(position, bool) or position < 0:
                raise ParseError(path, "位置要是 ≥0 的整數")
            if position in words:
                raise ParseError(path, "同一個位置出現兩次")
            words[position] = word
    restored = " ".join(words[position] for position in sorted(words)).strip()
    return restored or None


def _referenced_works(work: Mapping[str, Any]) -> tuple[str, ...]:
    references = _optional(work, "referenced_works", list, "referenced_works") or []
    seen: dict[str, None] = {}
    for index, reference in enumerate(references):
        path = f"referenced_works.{index}"
        work_id = _identifier(_check_kind(reference, str, path), path, normalize_openalex_id)
        if not work_id.startswith("W"):
            raise ParseError(path, "要是論文的 OpenAlex ID（W 開頭）")
        seen.setdefault(work_id, None)
    return tuple(seen)


def _keywords(work: Mapping[str, Any]) -> tuple[ParsedKeyword, ...]:
    entries = _optional(work, "keywords", list, "keywords") or []
    keywords: dict[str, ParsedKeyword] = {}
    for index, item in enumerate(entries):
        path = f"keywords.{index}"
        entry = _check_kind(item, dict, path)
        keyword_id = _require(entry, "id", str, f"{path}.id")
        match = _KEYWORD_ID.fullmatch(keyword_id)
        if match is None or len(match[1]) > KEYWORD_KEY_MAX_LENGTH:
            raise ParseError(f"{path}.id", "要是 https://openalex.org/keywords/<key>")
        if match[1] in keywords:
            continue  # 同一個 key 只留第一個，後面那筆不再看
        display_name = _require(entry, "display_name", str, f"{path}.display_name")
        try:
            keywords[match[1]] = ParsedKeyword(key=match[1], display_name=display_name)
        except ValidationError:
            raise ParseError(f"{path}.display_name", "去掉空白後是空的或存不進資料庫") from None
    return tuple(keywords.values())
