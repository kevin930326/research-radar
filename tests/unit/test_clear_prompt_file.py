"""`policy.clear_prompt_file`：模板唯一的放行入口（ADR-0030「依此更正」）。

整個檔的 SHA-256 要和 `domain/prompt_hashes.py` 寫死的登記一樣，核對過才拿掉設定區、放行本文；
登記不從參數收。內容全部是測試自己編的。
"""

import pytest

from radar.domain import prompt_hashes
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import prompt_sha256
from radar.domain.policy import (
    CloudClearanceError,
    ContentSource,
    clear_prompt_file,
    cloud_source,
    cloud_text,
)
from radar.domain.prompts import PromptFormatError
from tests.prompt_support import prompt_file

TASK = "zh_summary"
CONTENT = prompt_file("（假）請整理成四欄。")


def _register(content: bytes, *, task: str = TASK, version: int = 1) -> None:
    prompt_hashes.PROMPT_FILE_HASHES = {task: {version: (prompt_sha256(content), None)}}


def _blocked(task: str, version: int, content: bytes) -> CloudClearanceError:
    with pytest.raises(CloudClearanceError) as info:
        clear_prompt_file(task, version, content)
    return info.value


def test_registered_file_is_cleared_without_the_settings_block() -> None:
    _register(CONTENT)

    safe = clear_prompt_file(TASK, 1, CONTENT)

    assert cloud_text(safe) == "（假）請整理成四欄。"
    assert cloud_source(safe) is ContentSource.PROMPT_TEMPLATE


def test_unregistered_file_is_blocked() -> None:
    # 登記是空的（2.2 沒有正式 prompt）：任何檔都放行不了。
    error = _blocked(TASK, 1, CONTENT)

    assert error.reason is ErrorCode.CLOUD_PROMPT_UNREGISTERED
    assert error.source is ContentSource.PROMPT_TEMPLATE


@pytest.mark.parametrize(
    ("task", "version", "content"),
    [
        (TASK, 1, CONTENT.replace("四欄".encode(), "五欄".encode())),  # 本文改了沒升版
        (TASK, 1, CONTENT.replace(b"1024", b"2048")),  # 設定區改了沒升版
        (TASK, 1, CONTENT + b"\n"),  # 多一個換行也算改了
        (TASK, 2, CONTENT),  # 版本沒登記
        ("classify", 1, CONTENT),  # 工作沒登記
    ],
    ids=["本文", "設定區", "換行", "版本", "工作"],
)
def test_changed_or_unregistered_content_is_blocked(
    task: str, version: int, content: bytes
) -> None:
    _register(CONTENT)

    error = _blocked(task, version, content)

    assert error.reason is ErrorCode.CLOUD_PROMPT_UNREGISTERED
    assert "四欄" not in str(error) + repr(error)


def test_paywalled_text_filled_into_a_template_is_blocked() -> None:
    # 把（假的）付費全文接在已登記的模板後面：雜湊變了，放行不了。
    _register(CONTENT)
    filled = CONTENT + "（假付費全文）PAYWALL-MARKER".encode()

    error = _blocked(TASK, 1, filled)

    assert "PAYWALL-MARKER" not in str(error) + repr(error)


def test_registration_is_read_at_call_time_from_the_domain_module() -> None:
    _register(CONTENT)
    assert cloud_text(clear_prompt_file(TASK, 1, CONTENT))

    prompt_hashes.PROMPT_FILE_HASHES = {}

    assert _blocked(TASK, 1, CONTENT).reason is ErrorCode.CLOUD_PROMPT_UNREGISTERED


def test_registered_file_that_is_not_utf8_is_blocked() -> None:
    content = b"---\nmax_tokens: 10\n---\n\xff\xfe"
    _register(content)

    assert _blocked(TASK, 1, content).reason is ErrorCode.CLOUD_BLOCKED_INVALID_TEXT


def test_registered_file_without_a_body_is_a_format_error() -> None:
    content = b"---\nmax_tokens: 10\n---\n   \n"
    _register(content)

    with pytest.raises(PromptFormatError, match="本文是空的"):
        clear_prompt_file(TASK, 1, content)


@pytest.mark.parametrize(
    ("task", "version", "content"),
    [
        (1, 1, CONTENT),
        (TASK, "1", CONTENT),
        (TASK, True, CONTENT),
        (TASK, 1, CONTENT.decode()),
        (TASK, 1, bytearray(CONTENT)),
    ],
)
def test_argument_types_are_checked(task: object, version: object, content: object) -> None:
    _register(CONTENT)

    with pytest.raises(TypeError):
        clear_prompt_file(task, version, content)  # type: ignore[arg-type]
