"""prompt loader（AD-8、ADR-0009 第 3 步、ADR-0029 取捨、ADR-0030）：
唯一放行 `prompt_template` 的地方。

只從 `prompts/<工作代碼>/v<N>.md` 讀檔：

1. 讀 `prompts/registry.json`，找出這個工作要用的版本（沒指定就用 `current`）。
2. 讀檔案原始位元組，`prompt_sha256` 要和登記的一樣（改了內容卻沒升版就擋下）。
3. 解析設定區與本文；有 `output_schema` 時，由類別產生的 schema 雜湊要和登記的一樣。
4. 本文經 `clear_for_cloud` 放行成來源是 `prompt_template` 的 `CloudSafeText`。

架構測試規定 `ContentSource.PROMPT_TEMPLATE` 只能出現在 `domain/policy.py`、`domain/llm.py` 與
這個模組（`tests/arch/test_prompt_template_source.py`），所以其他程式做不出系統提示。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from radar.domain.ai_outputs import LlmTask
from radar.domain.fingerprints import prompt_sha256, schema_sha256
from radar.domain.llm import output_schema_json, output_schema_named
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from radar.domain.prompts import (
    PROMPT_FILE_PATTERN,
    PromptFormatError,
    PromptIntegrityError,
    PromptRegistry,
    PromptSettings,
    parse_prompt_file,
)

REGISTRY_FILE: Final = "registry.json"


@dataclass(frozen=True)
class LoadedPrompt:
    """讀好、核對過的 prompt。`template` 是放行過的系統提示（來源 `prompt_template`）。"""

    task: LlmTask
    version: int
    sha256: str
    settings: PromptSettings
    template: CloudSafeText
    schema_class: type[BaseModel] | None
    schema_sha256: str | None


def read_registry(root: Path) -> PromptRegistry:
    """讀 `<root>/registry.json`。

    Raises:
        PromptIntegrityError: 找不到登記檔。
        PromptFormatError: 格式不對。
    """
    path = root / REGISTRY_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptIntegrityError(f"找不到 prompt 登記檔（{path}）") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptFormatError(f"讀不出 prompt 登記檔（{type(exc).__name__}）") from None
    return PromptRegistry.parse(raw)


def prompt_path(root: Path, task: LlmTask, version: int) -> Path:
    """`<root>/<工作代碼>/v<N>.md`。"""
    return root / LlmTask(task).value / f"v{version}.md"


def current_prompt_version(root: Path, task: LlmTask) -> int:
    """這個工作現在要用的 prompt 版本（`registry.json` 的 `current`）。

    Raises:
        PromptIntegrityError: 沒有登記檔或沒有登記這個工作。
        PromptFormatError: 登記檔格式不對。
    """
    return read_registry(root).entry(task).current


def load_prompt(root: Path, task: LlmTask, version: int | None = None) -> LoadedPrompt:
    """讀這個工作的 prompt（沒指定版本就用 `current`），核對雜湊後放行模板。

    Raises:
        PromptIntegrityError: 沒有登記、版本沒登記、檔案不存在、檔案或 schema 的雜湊對不上。
        PromptFormatError: 登記檔或 prompt 檔格式不對、schema 名稱不對。
    """
    task = LlmTask(task)
    registry = read_registry(root)
    entry = registry.entry(task)
    chosen = entry.current if version is None else version
    registered = entry.versions.get(chosen)
    if registered is None:
        raise PromptIntegrityError(f"{task.value} 沒有登記 v{chosen}")
    path = prompt_path(root, task, chosen)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        raise PromptIntegrityError(f"找不到登記的 prompt 檔（{path}）") from None
    except OSError as exc:
        raise PromptIntegrityError(f"讀不出 prompt 檔（{type(exc).__name__}）") from None
    digest = prompt_sha256(content)
    if digest != registered.sha256:
        raise PromptIntegrityError(
            f"{task.value}/v{chosen}.md 的雜湊和 registry.json 不一樣：內容或設定區改了就要升版"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise PromptFormatError(f"{task.value}/v{chosen}.md 不是 UTF-8") from None
    parsed = parse_prompt_file(text)

    output_schema: type[BaseModel] | None = None
    schema_digest: str | None = None
    if parsed.settings.output_schema is not None:
        try:
            output_schema = output_schema_named(parsed.settings.output_schema)
        except ValueError as exc:
            raise PromptFormatError(
                f"{task.value}/v{chosen}.md 的 output_schema 不對：{exc}"
            ) from None
        schema_digest = schema_sha256(output_schema_json(output_schema))
    if schema_digest != registered.schema_sha256:
        raise PromptIntegrityError(
            f"{task.value}/v{chosen}.md 的 schema 雜湊和 registry.json 不一樣：schema 改了就要升版"
        )

    # 放行模板：來源是 prompt_template（不管開關一律放行）；只有這個模組做得到。
    template = clear_for_cloud(parsed.body, ContentSource.PROMPT_TEMPLATE, None, CloudPolicy())
    return LoadedPrompt(
        task=task,
        version=chosen,
        sha256=digest,
        settings=parsed.settings,
        template=template,
        schema_class=output_schema,
        schema_sha256=schema_digest,
    )


def registry_problems(root: Path) -> list[str]:
    """`root` 底下的 prompt 檔與登記對不上的地方（給架構測試與部署前檢查用）：

    - 登記的每個版本都讀得出來、雜湊對得上（`load_prompt`）。
    - `<工作代碼>/v<N>.md` 每個檔都有登記；資料夾名稱是工作代碼；檔名是 `v<N>.md`。
    """
    try:
        registry = read_registry(root)
    except (PromptIntegrityError, PromptFormatError) as exc:
        return [str(exc)]
    problems: list[str] = []
    for task, entry in sorted(registry.tasks.items()):
        for version in sorted(entry.versions):
            try:
                load_prompt(root, task, version)
            except (PromptIntegrityError, PromptFormatError) as exc:
                problems.append(str(exc))
    codes = {task.value: task for task in LlmTask}
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        task_code = codes.get(directory.name)
        if task_code is None:
            problems.append(f"{directory.name}/：資料夾名稱不是工作代碼")
            continue
        registered = registry.tasks.get(task_code)
        for file in sorted(path for path in directory.iterdir() if path.is_file()):
            match = PROMPT_FILE_PATTERN.fullmatch(file.name)
            if match is None:
                problems.append(f"{directory.name}/{file.name}：檔名要是 v<N>.md")
            elif registered is None or int(match.group(1)) not in registered.versions:
                problems.append(f"{directory.name}/{file.name}：沒有登記在 registry.json")
    return problems
