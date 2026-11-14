"""prompt 檔的格式與雜湊登記（AD-8、ADR-0009 第 3 步、NFR-14、ADR-0030）。

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
  的欄位；思考設定與長度上限屬於 prompt 版本的一部分。
- 本文是系統提示，沒有任何變數；文件內容由閘門用 `<document>` 區塊放進使用者訊息。
- 每個版本的檔案雜湊與 schema 雜湊登記在 `prompts/registry.json`（`PromptRegistry`）。登記只增
  不改：內容或設定區改了就要升版（新增 `v<N+1>.md` 並登記），否則測試與執行期的 loader 都會擋。
"""

import json
import re
from collections.abc import Mapping
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from radar.domain.ai_outputs import LlmTask
from radar.domain.llm import Effort

SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
FENCE: Final = "---"
_SETTING_LINE: Final = re.compile(r"^([a-z_][a-z0-9_]*):[ \t]*(.*?)[ \t]*$")
# 版本檔名：v1.md、v2.md……（不收 v01、v0）。
PROMPT_FILE_PATTERN: Final = re.compile(r"^v([1-9][0-9]*)\.md$")


class PromptFormatError(ValueError):
    """prompt 檔或登記檔的格式不對。訊息只說哪裡不對，不帶模板內容。"""


class PromptIntegrityError(Exception):
    """prompt 檔或 schema 的雜湊和登記對不上（改了內容卻沒升版），或找不到登記的版本。"""


class PromptSettings(BaseModel):
    """設定區：屬於 prompt 版本的一部分，改了就要升版。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: Effort | None = None
    # Haiku 5.5 的思考會吃 max_tokens，短輸出也要留空間（ADR-0009）。
    max_tokens: int = Field(ge=1, le=64_000)
    disable_thinking: bool = False
    # `radar/domain/llm_schemas.py` 最上層的類別名稱；沒有時回應是一般文字。
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
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FENCE:
        raise PromptFormatError("prompt 檔第一行要是 ---（設定區開頭）")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == FENCE)
    except StopIteration:
        raise PromptFormatError("設定區沒有結尾的 ---") from None
    values: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], start=2):
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
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise PromptFormatError("prompt 的本文是空的")
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


class PromptVersionEntry(BaseModel):
    """登記的一個版本：檔案雜湊與 schema 雜湊（沒有 schema 時是 None）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str = Field(pattern=SHA256_PATTERN)
    schema_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class PromptTaskEntry(BaseModel):
    """一個工作的登記：目前用哪一版，以及每一版的雜湊。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current: int = Field(ge=1, strict=True)
    versions: Mapping[int, PromptVersionEntry]

    @model_validator(mode="after")
    def _current_is_registered(self) -> Self:
        if any(version < 1 for version in self.versions):
            raise ValueError("版本號從 1 開始")
        if self.current not in self.versions:
            raise ValueError("current 指向沒有登記的版本")
        return self


class PromptRegistry(BaseModel):
    """`prompts/registry.json`：工作代碼 → 登記。2.2 沒有正式 prompt，所以是 `{}`。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: Mapping[LlmTask, PromptTaskEntry]

    @classmethod
    def parse(cls, raw: str) -> PromptRegistry:
        """解析登記檔（JSON 物件：工作代碼 → 登記）。

        Raises:
            PromptFormatError: 不是合法的 JSON 或格式不對。
        """
        try:
            data = json.loads(raw)
        except ValueError, RecursionError:
            raise PromptFormatError("registry.json 不是合法的 JSON") from None
        if not isinstance(data, dict):
            raise PromptFormatError("registry.json 最上層要是 JSON 物件（工作代碼 → 登記）")
        try:
            return cls.model_validate({"tasks": data})
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_url=False, include_context=False)
            problems = "；".join(
                f"{'.'.join(str(part) for part in error['loc'][1:]) or '最上層'}：{error['msg']}"
                for error in errors
            )
            raise PromptFormatError(f"registry.json 不對（{problems}）") from None

    def entry(self, task: LlmTask) -> PromptTaskEntry:
        """這個工作的登記。

        Raises:
            PromptIntegrityError: 沒有登記。
        """
        found = self.tasks.get(LlmTask(task))
        if found is None:
            raise PromptIntegrityError(f"registry.json 沒有登記工作 {LlmTask(task).value}")
        return found
