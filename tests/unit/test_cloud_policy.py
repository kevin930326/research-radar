"""送雲端的放行（`radar/domain/policy.py`）：I/O 表的每一列、開關開與關、錯誤與 repr 不帶文字、
`CloudSafeText` 只能由 `clear_for_cloud` 建立，以及 `RADAR_PAYWALLED_FULLTEXT_LLM` 怎麼讀。

文字全是自己編的假摘要。
"""

import copy
import pickle
from typing import Any

import pytest

from radar.config import ConfigError, load_settings
from radar.domain.errors import ErrorCode
from radar.domain.policy import (
    CLOUD_CLEARED_LICENSES,
    CloudClearanceError,
    CloudPolicy,
    CloudSafeText,
    ContentLicense,
    ContentSource,
    clear_for_cloud,
)

SECRET_TEXT = "（假摘要）這段文字只應該出現在 CloudSafeText.text 裡 MARKER-7Q"
OFF = CloudPolicy()
ON = CloudPolicy(paywalled_fulltext_llm=True)
BASE_ENV = {"DATABASE_URL": "postgresql://radar@db:5432/radar"}

ALL_LICENSES: list[ContentLicense | None] = [*ContentLicense, None]
NOT_CLEARED_LICENSES: list[ContentLicense | None] = [
    ContentLicense.CC_BY_NC,
    ContentLicense.CC_BY_ND,
    ContentLicense.CC_BY_NC_SA,
    ContentLicense.CC_BY_NC_ND,
    ContentLicense.OTHER_OA,
    ContentLicense.PAYWALLED,
    ContentLicense.UNKNOWN,
    None,
]
ALWAYS_CLEARED_SOURCES = [
    ContentSource.OPENALEX_ABSTRACT,
    ContentSource.CROSSREF_ABSTRACT,
    ContentSource.TITLE,
    ContentSource.USER_QUERY,
    ContentSource.PROMPT_TEMPLATE,
]


def _blocked(
    source: ContentSource, license: ContentLicense | None, policy: CloudPolicy
) -> CloudClearanceError:
    with pytest.raises(CloudClearanceError) as info:
        clear_for_cloud(SECRET_TEXT, source, license, policy)
    return info.value


# --- 放行表 --------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [OFF, ON], ids=["開關關", "開關開"])
@pytest.mark.parametrize("license", ALL_LICENSES)
@pytest.mark.parametrize("source", ALWAYS_CLEARED_SOURCES)
def test_abstracts_titles_queries_and_templates_are_always_cleared(
    source: ContentSource, license: ContentLicense | None, policy: CloudPolicy
) -> None:
    safe = clear_for_cloud(SECRET_TEXT, source, license, policy)

    assert isinstance(safe, CloudSafeText)
    assert safe.text == SECRET_TEXT
    assert safe.source is source


@pytest.mark.parametrize("policy", [OFF, ON], ids=["開關關", "開關開"])
@pytest.mark.parametrize(
    "license", [ContentLicense.CC_BY, ContentLicense.CC_BY_SA, ContentLicense.CC0]
)
def test_cc_licensed_oa_fulltext_is_cleared(license: ContentLicense, policy: CloudPolicy) -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.OA_FULLTEXT, license, policy)

    assert safe.source is ContentSource.OA_FULLTEXT


def test_cleared_licenses_are_exactly_by_by_sa_and_cc0() -> None:
    # ADR-0029 的保守選擇：NC、ND 類的 CC 不算。
    assert {ContentLicense.CC_BY, ContentLicense.CC_BY_SA, ContentLicense.CC0} == (
        CLOUD_CLEARED_LICENSES
    )


@pytest.mark.parametrize("license", NOT_CLEARED_LICENSES)
def test_paywalled_fulltext_is_blocked_when_switch_is_off(license: ContentLicense | None) -> None:
    error = _blocked(ContentSource.OA_FULLTEXT, license, OFF)

    assert error.reason is ErrorCode.CLOUD_BLOCKED_PAYWALLED
    assert error.source is ContentSource.OA_FULLTEXT


@pytest.mark.parametrize("license", NOT_CLEARED_LICENSES)
def test_paywalled_fulltext_is_cleared_when_switch_is_on(license: ContentLicense | None) -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.OA_FULLTEXT, license, ON)

    assert safe.source is ContentSource.OA_FULLTEXT


@pytest.mark.parametrize("license", ALL_LICENSES)
def test_uploaded_pdf_is_blocked_when_switch_is_off_even_if_cc_by(
    license: ContentLicense | None,
) -> None:
    error = _blocked(ContentSource.UPLOADED_PDF, license, OFF)

    assert error.reason is ErrorCode.CLOUD_BLOCKED_PAYWALLED


@pytest.mark.parametrize("license", ALL_LICENSES)
def test_uploaded_pdf_is_cleared_when_switch_is_on(license: ContentLicense | None) -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.UPLOADED_PDF, license, ON)

    assert safe.source is ContentSource.UPLOADED_PDF


@pytest.mark.parametrize("policy", [OFF, ON], ids=["開關關", "開關開"])
@pytest.mark.parametrize("license", ALL_LICENSES)
def test_semantic_scholar_abstract_is_blocked_regardless_of_switch(
    license: ContentLicense | None, policy: CloudPolicy
) -> None:
    error = _blocked(ContentSource.SEMANTIC_SCHOLAR_ABSTRACT, license, policy)

    assert error.reason is ErrorCode.CLOUD_BLOCKED_SOURCE


@pytest.mark.parametrize("policy", [OFF, ON], ids=["開關關", "開關開"])
@pytest.mark.parametrize("text", ["", " ", "\n\t　"])
@pytest.mark.parametrize("source", list(ContentSource))
def test_empty_or_blank_text_is_blocked(
    source: ContentSource, text: str, policy: CloudPolicy
) -> None:
    with pytest.raises(CloudClearanceError) as info:
        clear_for_cloud(text, source, ContentLicense.CC_BY, policy)

    assert info.value.reason is ErrorCode.CLOUD_BLOCKED_EMPTY


@pytest.mark.parametrize("policy", [OFF, ON], ids=["開關關", "開關開"])
@pytest.mark.parametrize("source", ALWAYS_CLEARED_SOURCES)
def test_text_that_is_not_utf8_encodable_is_blocked(
    source: ContentSource, policy: CloudPolicy
) -> None:
    # 單獨的代理字元：送出時 SDK 編碼會失敗，在放行時就擋下。
    text = "（假摘要）SURROGATE-MARKER \ud800 結尾"

    with pytest.raises(CloudClearanceError) as info:
        clear_for_cloud(text, source, None, policy)

    assert info.value.reason is ErrorCode.CLOUD_BLOCKED_INVALID_TEXT
    shown = str(info.value) + repr(info.value) + repr(info.value.args)
    assert "SURROGATE-MARKER" not in shown


def test_every_source_has_a_rule() -> None:
    # 每個來源都有結果（放行或有原因的擋下），沒有掉進別的例外。
    reasons: dict[ContentSource, ErrorCode | None] = {}
    for source in ContentSource:
        try:
            clear_for_cloud(SECRET_TEXT, source, None, OFF)
        except CloudClearanceError as exc:
            reasons[source] = exc.reason
        else:
            reasons[source] = None

    assert set(reasons) == set(ContentSource)
    assert set(reasons.values()) <= {
        None,
        ErrorCode.CLOUD_BLOCKED_PAYWALLED,
        ErrorCode.CLOUD_BLOCKED_SOURCE,
    }


# --- 參數型別 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (b"bytes", ContentSource.TITLE, None, OFF),
        (None, ContentSource.TITLE, None, OFF),
        (SECRET_TEXT, "title", None, OFF),
        (SECRET_TEXT, ContentSource.OA_FULLTEXT, "cc-by", OFF),
        (SECRET_TEXT, ContentSource.TITLE, None, {"paywalled_fulltext_llm": True}),
        (SECRET_TEXT, ContentSource.TITLE, None, True),
    ],
    ids=["文字是 bytes", "文字是 None", "來源是 str", "授權是 str", "設定是 dict", "設定是 bool"],
)
def test_wrong_argument_types_raise_type_error(args: tuple[Any, ...]) -> None:
    with pytest.raises(TypeError) as info:
        clear_for_cloud(*args)

    assert SECRET_TEXT not in str(info.value)


def test_cloud_policy_does_not_coerce_strings() -> None:
    # 開關只能是真的 bool；環境變數的字串由 Settings 轉好再交給它。
    with pytest.raises(ValueError, match="paywalled_fulltext_llm"):
        CloudPolicy.model_validate({"paywalled_fulltext_llm": "true"})
    with pytest.raises(ValueError, match="extra"):
        CloudPolicy.model_validate({"other": True})


# --- 錯誤與 repr 不帶文字 -------------------------------------------------------------------


def test_clearance_error_never_contains_the_text() -> None:
    error = _blocked(ContentSource.OA_FULLTEXT, ContentLicense.PAYWALLED, OFF)

    assert SECRET_TEXT not in str(error)
    assert SECRET_TEXT not in repr(error)
    assert all(SECRET_TEXT not in str(arg) for arg in error.args)
    assert "MARKER-7Q" not in str(error) + repr(error) + repr(error.args)
    assert "oa_fulltext" in str(error)
    assert "cloud_blocked_paywalled" in str(error)


def test_cloud_safe_text_repr_and_str_show_only_source_and_length() -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.TITLE, None, OFF)

    assert repr(safe) == f"CloudSafeText(source='title', length={len(SECRET_TEXT)})"
    assert str(safe) == repr(safe)
    assert f"{safe}" == repr(safe)
    assert SECRET_TEXT not in repr([safe])


def test_cloud_safe_text_is_not_a_str() -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.TITLE, None, OFF)

    assert not isinstance(safe, str)
    assert not issubclass(CloudSafeText, str)


# --- 只能由 clear_for_cloud 建立、不可改 ------------------------------------------------------


def test_direct_construction_raises_type_error() -> None:
    # 不直接寫 CloudSafeText(...)：架構測試規定只有 policy.py 能這樣寫，這裡經由變數呼叫，
    # 證明就算繞過架構測試，執行期也建立不出來。
    factory: Any = CloudSafeText

    with pytest.raises(TypeError, match="clear_for_cloud"):
        factory(SECRET_TEXT, ContentSource.OA_FULLTEXT, clearance=object())
    with pytest.raises(TypeError):
        factory(SECRET_TEXT, ContentSource.OA_FULLTEXT)


def test_cannot_subclass() -> None:
    with pytest.raises(TypeError, match="繼承"):
        type("Fake", (CloudSafeText,), {})


def test_is_immutable() -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.TITLE, None, OFF)
    target: Any = safe

    with pytest.raises(AttributeError):
        target._text = "換掉"
    with pytest.raises(AttributeError):
        target.text = "換掉"
    with pytest.raises(AttributeError):
        target.extra = 1
    with pytest.raises(AttributeError):
        del target._source
    assert safe.text == SECRET_TEXT


def test_cannot_be_copied_or_pickled() -> None:
    safe = clear_for_cloud(SECRET_TEXT, ContentSource.TITLE, None, OFF)

    with pytest.raises(TypeError):
        copy.copy(safe)
    with pytest.raises(TypeError):
        pickle.dumps(safe)


def test_equal_texts_compare_equal() -> None:
    first = clear_for_cloud(SECRET_TEXT, ContentSource.TITLE, None, OFF)
    second = clear_for_cloud(SECRET_TEXT, ContentSource.TITLE, None, ON)
    other_source = clear_for_cloud(SECRET_TEXT, ContentSource.USER_QUERY, None, OFF)

    assert first == second
    assert hash(first) == hash(second)
    assert first != other_source


# --- RADAR_PAYWALLED_FULLTEXT_LLM ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), ("", False), ("false", False), ("0", False), ("true", True), ("1", True)],
)
def test_settings_read_the_switch(raw: str | None, expected: bool) -> None:
    env = dict(BASE_ENV)
    if raw is not None:
        env["RADAR_PAYWALLED_FULLTEXT_LLM"] = raw

    settings = load_settings(env)

    assert settings.paywalled_fulltext_llm is expected
    assert settings.cloud_policy == CloudPolicy(paywalled_fulltext_llm=expected)


def test_settings_reject_garbage_switch_value() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({**BASE_ENV, "RADAR_PAYWALLED_FULLTEXT_LLM": "maybe-later"})

    assert "RADAR_PAYWALLED_FULLTEXT_LLM" in str(info.value)
    assert "maybe-later" not in str(info.value)


@pytest.mark.parametrize(("raw", "opens"), [(None, False), ("false", False), ("true", True)])
def test_switch_from_environment_controls_paywalled_and_uploaded_content(
    raw: str | None, opens: bool
) -> None:
    # AC：經 load_settings 讀出、把 cloud_policy 交給 clear_for_cloud。
    env = dict(BASE_ENV)
    if raw is not None:
        env["RADAR_PAYWALLED_FULLTEXT_LLM"] = raw
    policy = load_settings(env).cloud_policy

    for source, license in [
        (ContentSource.OA_FULLTEXT, ContentLicense.PAYWALLED),
        (ContentSource.UPLOADED_PDF, None),
    ]:
        if opens:
            assert clear_for_cloud(SECRET_TEXT, source, license, policy).source is source
        else:
            with pytest.raises(CloudClearanceError):
                clear_for_cloud(SECRET_TEXT, source, license, policy)

    # OpenAlex 摘要與 CC 授權的 OA 全文三種，開關怎麼設都放行。
    assert clear_for_cloud(SECRET_TEXT, ContentSource.OPENALEX_ABSTRACT, None, policy)
    for license in (ContentLicense.CC_BY, ContentLicense.CC_BY_SA, ContentLicense.CC0):
        assert clear_for_cloud(SECRET_TEXT, ContentSource.OA_FULLTEXT, license, policy)
