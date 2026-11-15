"""prompt loader（AD-8、ADR-0009 第 3 步、ADR-0029、ADR-0030「依此更正」）。

只從 `<prompts 資料夾>/<工作代碼>/v<N>.md` 讀檔：

1. 版本：沒指定就用 `domain/prompt_hashes.py` 登記的目前版本。
2. 讀整個檔的原始位元組：路徑由工作代碼（列舉）與版本號（整數）組成，不收任意路徑；工作的資料夾
   與檔案本身是符號連結就不讀（開檔用 `O_NOFOLLOW`）。
3. 把原始位元組原樣交給 `domain/policy.py` 的 `clear_prompt_file`：policy 核對整個檔的雜湊和
   domain 寫死的登記一樣，才拿掉設定區、放行本文（來源 `prompt_template`）。這裡不自己放行、
   不呼叫 `clear_for_cloud`、不把任何變數填進模板（`tests/arch/test_prompt_template_source.py`）。
4. 解析設定區；有 `output_schema` 時從 `OUTPUT_SCHEMAS` 取類別，凍結 schema 字串的雜湊要和登記的
   一樣（schema 改了卻沒升版就擋下）。
"""

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel

from radar.domain import llm_schemas, prompt_hashes
from radar.domain.ai_outputs import LlmTask
from radar.domain.fingerprints import schema_sha256
from radar.domain.llm import frozen_schema_json
from radar.domain.policy import CloudClearanceError, CloudSafeText, clear_prompt_file
from radar.domain.prompts import (
    PROMPT_FILE_PATTERN,
    PromptFormatError,
    PromptIntegrityError,
    PromptSettings,
    parse_prompt_file,
)

# prompt 設定區 `output_schema:` 可以寫的名稱 → `domain/llm_schemas.py` 的類別。新增結構化輸出的
# 工作時在這裡加一列；每個類別都要 `extra="forbid"`（`tests/unit/test_prompt_loader.py` 守著）。
# `LlmRequest` 執行期再核對類別在凍結的 schema 登記表裡。
OUTPUT_SCHEMAS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "FakeResearchQuestion": llm_schemas.FakeResearchQuestion,
        "ZhSummaryOutput": llm_schemas.ZhSummaryOutput,
    }
)
# prompt 檔最大的位元組數（系統提示不該這麼長；擋下誤放的大檔）。
MAX_PROMPT_BYTES: Final = 256 * 1024


@dataclass(frozen=True)
class LoadedPrompt:
    """讀好、核對過的 prompt。

    - `template`：放行過的系統提示（來源 `prompt_template`，只有 policy 做得出來）。
    - `schema_class`：結構化輸出的類別，閘門原樣傳給 `LlmRequest(output_schema=...)`。
    - `schema_sha256`：送出的凍結 schema 字串的 SHA-256，記進 `derivation_run` 的來歷。
    """

    task: LlmTask
    version: int
    sha256: str
    settings: PromptSettings
    template: CloudSafeText
    schema_class: type[BaseModel] | None
    schema_sha256: str | None


def prompt_path(root: Path, task: LlmTask, version: int) -> Path:
    """`<root>/<工作代碼>/v<N>.md`。"""
    if type(version) is not int or version < 1:
        raise ValueError("prompt 版本是從 1 開始的整數")
    return root / LlmTask(task).value / f"v{version}.md"


def current_prompt_version(task: LlmTask) -> int:
    """這個工作現在要用的 prompt 版本（`domain/prompt_hashes.py` 的 `CURRENT_PROMPT_VERSIONS`）。

    Raises:
        PromptIntegrityError: 沒有登記這個工作。
    """
    version = prompt_hashes.CURRENT_PROMPT_VERSIONS.get(LlmTask(task).value)
    if version is None:
        raise PromptIntegrityError(f"prompt_hashes.py 沒有登記工作 {LlmTask(task).value}")
    return version


def load_prompt(root: Path, task: LlmTask, version: int | None = None) -> LoadedPrompt:
    """讀這個工作的 prompt（沒指定版本就用目前版本），交給 policy 核對雜湊、放行模板。

    Raises:
        PromptIntegrityError: 沒有登記、檔案不存在或是符號連結、檔案或 schema 的雜湊對不上。
        PromptFormatError: prompt 檔格式不對、不是 UTF-8、schema 名稱不在 `OUTPUT_SCHEMAS`。
    """
    task = LlmTask(task)
    chosen = current_prompt_version(task) if version is None else version
    name = f"{task.value}/v{chosen}.md"
    content = _read_prompt_file(prompt_path(root, task, chosen), name)
    try:
        template = clear_prompt_file(task.value, chosen, content)
    except CloudClearanceError as exc:
        raise PromptIntegrityError(
            f"{name} 沒有通過雜湊核對（{exc.reason.value}）：沒有登記，或內容、設定區改了卻沒升版"
        ) from None
    # 核對過雜湊的檔一定是 UTF-8（policy 已經解碼過）。
    parsed = parse_prompt_file(content.decode("utf-8"))

    schema_class: type[BaseModel] | None = None
    schema_digest: str | None = None
    if parsed.settings.output_schema is not None:
        schema_class = OUTPUT_SCHEMAS.get(parsed.settings.output_schema)
        if schema_class is None:
            raise PromptFormatError(f"{name} 的 output_schema 不在 OUTPUT_SCHEMAS 裡")
        schema_digest = schema_sha256(frozen_schema_json(schema_class))
    registered = prompt_hashes.PROMPT_FILE_HASHES[task.value][chosen]
    if schema_digest != registered[1]:
        raise PromptIntegrityError(f"{name} 的 schema 雜湊和登記不一樣：schema 改了就要升版")
    return LoadedPrompt(
        task=task,
        version=chosen,
        sha256=registered[0],
        settings=parsed.settings,
        template=template,
        schema_class=schema_class,
        schema_sha256=schema_digest,
    )


def _read_prompt_file(path: Path, name: str) -> bytes:
    """讀整個 prompt 檔。工作的資料夾或檔案是符號連結、不是一般檔案、太大都不讀。

    Raises:
        PromptIntegrityError: 不存在、是符號連結、不是一般檔案、太大或讀不出來。
    """
    if path.parent.is_symlink():
        raise PromptIntegrityError(f"{name} 的資料夾是符號連結，不讀")
    # O_NONBLOCK：路徑是 FIFO 時開檔不會卡住（之後 fstat 會擋下不是一般檔案的）。
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise PromptIntegrityError(f"找不到 prompt 檔 {name}") from None
    except OSError as exc:
        # 檔案本身是符號連結時 O_NOFOLLOW 讓 open 失敗（ELOOP）。
        raise PromptIntegrityError(f"讀不出 prompt 檔 {name}（{type(exc).__name__}）") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PromptIntegrityError(f"{name} 不是一般檔案")
        if info.st_size > MAX_PROMPT_BYTES:
            raise PromptIntegrityError(f"{name} 太大（上限 {MAX_PROMPT_BYTES} 位元組）")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            content = file.read(MAX_PROMPT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > MAX_PROMPT_BYTES:
        raise PromptIntegrityError(f"{name} 太大（上限 {MAX_PROMPT_BYTES} 位元組）")
    return content


def registry_problems(root: Path) -> list[str]:
    """`root` 底下的 prompt 檔與 `domain/prompt_hashes.py` 的登記對不上的地方
    （給架構測試與部署前檢查用）：

    - 每個工作的目前版本都有登記；登記的每個版本都讀得出來、雜湊對得上（`load_prompt`）。
    - `<工作代碼>/v<N>.md` 每個檔都有登記；資料夾名稱是工作代碼；檔名是 `v<N>.md`。
    """
    if not root.is_dir():
        # 資料夾不在時每個登記的版本都會讀不到，只回這一條（不逐一列出找不到的檔）。
        return [f"{root}：prompt 資料夾不存在"]
    problems: list[str] = []
    codes = {task.value: task for task in LlmTask}
    for code, current in sorted(prompt_hashes.CURRENT_PROMPT_VERSIONS.items()):
        if code not in codes:
            problems.append(f"CURRENT_PROMPT_VERSIONS 的 {code} 不是工作代碼")
        elif current not in prompt_hashes.PROMPT_FILE_HASHES.get(code, {}):
            problems.append(f"{code} 的目前版本 v{current} 沒有登記雜湊")
    for code, versions in sorted(prompt_hashes.PROMPT_FILE_HASHES.items()):
        task = codes.get(code)
        if task is None:
            problems.append(f"PROMPT_FILE_HASHES 的 {code} 不是工作代碼")
            continue
        if code not in prompt_hashes.CURRENT_PROMPT_VERSIONS:
            problems.append(f"{code} 有登記版本，但沒有登記目前版本")
        for version in sorted(versions):
            try:
                load_prompt(root, task, version)
            except (PromptIntegrityError, PromptFormatError, ValueError) as exc:
                problems.append(str(exc))
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        task_code = codes.get(directory.name)
        if task_code is None:
            problems.append(f"{directory.name}/：資料夾名稱不是工作代碼")
            continue
        registered = prompt_hashes.PROMPT_FILE_HASHES.get(task_code.value, {})
        for file in sorted(directory.iterdir()):
            match = PROMPT_FILE_PATTERN.fullmatch(file.name)
            if match is None:
                problems.append(f"{directory.name}/{file.name}：檔名要是 v<N>.md")
            elif int(match.group(1)) not in registered:
                problems.append(f"{directory.name}/{file.name}：沒有登記在 prompt_hashes.py")
    return problems
