"""prompt 檔的設定區（AD-8、ADR-0009 第 3 步、NFR-14、ADR-0030）。

prompt 檔放在 `prompts/<工作代碼>/v<N>.md`：

```
---
effort: low
max_tokens: 2048
disable_thinking: false
output_schema: FakeResearchQuestion
---
（系統提示的本文：沒有變數，原樣送出）
```

- 設定區是 `---` 包住的 `key: value` 行（不用 YAML，避免型別自動轉換），只收 `PromptSettings`
  的欄位；思考設定與長度上限屬於 prompt 版本的一部分。切法在 `domain/prompt_format.py`。
- 本文是系統提示，沒有任何變數；文件內容由閘門用 `<document>` 區塊放進使用者訊息。本文只能經
  `domain/policy.py` 的 `clear_prompt_file` 放行。
- 每個版本整個檔的雜湊與 schema 雜湊登記在 `domain/prompt_hashes.py`。登記只增不改：內容或
  設定區改了就要升版（新增 `v<N+1>.md` 並登記），否則測試與執行期的 loader 都會擋。
"""

import re
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from radar.domain.llm import Effort
from radar.domain.prompt_format import PromptFormatError, split_prompt_file

__all__ = [
    "PROMPT_FILE_PATTERN",
    "ParsedPrompt",
    "PromptFormatError",
    "PromptIntegrityError",
    "PromptSettings",
    "parse_prompt_file",
]

_SETTING_LINE: Final = re.compile(r"^([a-z_][a-z0-9_]*):[ \t]*(.*?)[ \t]*$")
# 版本檔名：v1.md、v2.md……（不收 v01、v0）。
PROMPT_FILE_PATTERN: Final = re.compile(r"^v([1-9][0-9]*)\.md$")


class PromptIntegrityError(Exception):
    """prompt 檔或 schema 的雜湊和登記對不上（改了內容卻沒升版）、找不到登記的版本，或檔案
    讀不出來（不存在、是符號連結）。"""


class PromptSettings(BaseModel):
    """設定區：屬於 prompt 版本的一部分，改了就要升版。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: Effort | None = None
    # Haiku 5.5 的思考會吃 max_tokens，短輸出也要留空間（ADR-0009）。
    max_tokens: int = Field(ge=1, le=64_000, strict=True)
    disable_thinking: bool = Field(default=False, strict=True)
    # `services/prompt_loader.py` 的 `OUTPUT_SCHEMAS` 裡的名稱（類別在 `domain/llm_schemas.py`）；
    # 沒有時回應是一般文字。
    output_schema: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class ParsedPrompt(BaseModel):
    """解析好的 prompt 檔。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    settings: PromptSettings
    body: str


def parse_prompt_file(text: str) -> ParsedPrompt:
    """解析 prompt 檔：第一行是 `---`，接著 `key: value` 行，再一行 `---`，之後是本文。

    本文去掉頭尾空白後不能是空的。同一個 key 寫兩次、不認得的 key、值的型別不對都算錯。

    Raises:
        PromptFormatError: 格式不對。
    """
    lines, body = split_prompt_file(text)
    values: dict[str, str] = {}
    for number, line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _SETTING_LINE.match(line)
        if match is None:
            raise PromptFormatError(f"設定區第 {number} 行不是 key: value")
        key, value = match.group(1), match.group(2)
        if key in values:
            raise PromptFormatError(f"設定區的 {key} 寫了兩次")
        values[key] = value
    try:
        settings = PromptSettings.model_validate(_typed(values))
    except ValidationError as exc:
        problems = "；".join(
            f"{'.'.join(str(part) for part in error['loc']) or '設定區'}：{error['msg']}"
            for error in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        raise PromptFormatError(f"設定區不對（{problems}）") from None
    return ParsedPrompt(settings=settings, body=body)


def _typed(values: Mapping[str, str]) -> dict[str, object]:
    """設定區的字串轉成型別：`true`／`false` → bool，整數 → int，`null` 或空白 → None。"""
    typed: dict[str, object] = {}
    for key, value in values.items():
        if value in ("", "null"):
            typed[key] = None
        elif value in ("true", "false"):
            typed[key] = value == "true"
        elif re.fullmatch(r"[0-9]+", value):
            typed[key] = int(value)
        else:
            typed[key] = value
    return typed
