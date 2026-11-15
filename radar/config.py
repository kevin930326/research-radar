"""唯一讀環境變數的地方（AD-18）。只有入口與 `bootstrap.py` 可以 import 這個模組。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from radar.domain.budget import BudgetConfig
from radar.domain.context_values import ContextValues, context_values_registry_problems
from radar.domain.domain_config import (
    DomainConfig,
    ExclusionRulesConfig,
    JournalsConfig,
    KeywordSourcesConfig,
    OpenAlexConfig,
)
from radar.domain.import_report import ImportBaseline
from radar.domain.jobs import scheduled_jobs
from radar.domain.pricing import ModelsConfig
from radar.domain.settings import MigrationSettings, Settings, heartbeat_url_field
from radar.domain.taxonomy import Taxonomy, taxonomy_registry_problems
from radar.domain.validators.zh_summary import ZhSummaryRules

type ProcessKind = Literal["api", "worker", "cli"]

# 連線數預算（ADR-0003 決定第 4 點、AD-17）：以託管 PostgreSQL 可用的 35 條連線為準，
# api 8、worker 6、每個 CLI 行程 2，其餘留給備份與人工連線。
# 可用 RADAR_API_DATABASE_POOL_SIZE、RADAR_WORKER_DATABASE_POOL_SIZE、
# RADAR_CLI_DATABASE_POOL_SIZE 覆寫。
DATABASE_POOL_BUDGET: Mapping[ProcessKind, int] = {"api": 8, "worker": 6, "cli": 2}


class ConfigError(Exception):
    """設定缺漏或格式不對。訊息只列環境變數名稱與原因，不含任何設定值。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("設定錯誤：" + "；".join(problems))


def env_name(field: str) -> str:
    """設定欄位對應的環境變數名稱（`migration_database_url` → `RADAR_MIGRATION_DATABASE_URL`）。"""
    if field == "database_url":
        return "DATABASE_URL"
    return f"RADAR_{field.upper()}"


def _load[T: BaseModel](model: type[T], environ: Mapping[str, str] | None) -> T:
    """從環境變數組出設定物件；空字串當作沒設定。錯誤訊息不含任何設定值。"""
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for field in model.model_fields:
        raw = env.get(env_name(field), "")
        if raw != "":
            values[field] = raw
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        problems: list[str] = []
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            field = str(error["loc"][0]) if error["loc"] else "?"
            if error["type"] == "missing":
                problems.append(f"缺少環境變數 {env_name(field)}")
            else:
                problems.append(f"環境變數 {env_name(field)} 格式不對（{error['msg']}）")
        raise ConfigError(problems) from None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """api、worker 與一般 CLI 指令的設定。

    Raises:
        ConfigError: 缺少必填的值或格式不對；訊息不含任何設定值。
    """
    return _load(Settings, environ)


def load_migration_settings(environ: Mapping[str, str] | None = None) -> MigrationSettings:
    """`radar migrate` 與 alembic 指令的設定（擁有者角色的連線字串）。

    Raises:
        ConfigError: 缺少 `RADAR_MIGRATION_DATABASE_URL` 或格式不對；訊息不含任何設定值。
    """
    return _load(MigrationSettings, environ)


def database_pool_size(settings: Settings, process: ProcessKind) -> int:
    """這種行程的連線池大小：環境變數有覆寫就用覆寫值，否則用連線數預算。"""
    override = {
        "api": settings.api_database_pool_size,
        "worker": settings.worker_database_pool_size,
        "cli": settings.cli_database_pool_size,
    }[process]
    return DATABASE_POOL_BUDGET[process] if override is None else override


# worker 的連線怎麼分（ADR-0019）：佇列的連線池最多 2 條、佇列監聽新工作的 1 條，其餘給應用程式的
# 連線池（sweep 讀狀態表等）。預算 6 條時是 2＋1＋3。
WORKER_QUEUE_POOL_MAX = 2
WORKER_LISTEN_CONNECTIONS = 1
WORKER_MIN_CONNECTIONS = 3


@dataclass(frozen=True)
class WorkerConnections:
    """worker 的連線數：佇列的連線池、監聽、應用程式的連線池，加總等於 worker 的預算。"""

    queue_pool: int
    listen: int
    app_pool: int

    @property
    def total(self) -> int:
        return self.queue_pool + self.listen + self.app_pool


def worker_connection_split(settings: Settings) -> WorkerConnections:
    """把 worker 的連線數預算（預設 6，可用 `RADAR_WORKER_DATABASE_POOL_SIZE` 覆寫）拆成三份。

    監聽固定 1 條；佇列的連線池最多 2 條，應用程式的連線池至少 1 條、拿剩下的。

    Raises:
        ConfigError: 預算少於 3 條（三份各至少要 1 條）。
    """
    budget = database_pool_size(settings, "worker")
    if budget < WORKER_MIN_CONNECTIONS:
        raise ConfigError(
            [
                f"環境變數 {env_name('worker_database_pool_size')} 至少要"
                f" {WORKER_MIN_CONNECTIONS}（佇列連線池、監聽、應用程式連線池各 1 條）"
            ]
        )
    rest = budget - WORKER_LISTEN_CONNECTIONS
    queue_pool = min(WORKER_QUEUE_POOL_MAX, rest // 2)
    return WorkerConnections(
        queue_pool=queue_pool, listen=WORKER_LISTEN_CONNECTIONS, app_pool=rest - queue_pool
    )


def heartbeat_urls(settings: Settings) -> dict[str, str]:
    """每個排程的心跳網址（排程名稱 → 網址）；沒設網址的排程不在裡面。"""
    urls: dict[str, str] = {}
    for job in scheduled_jobs():
        value = getattr(settings, heartbeat_url_field(job.check))
        if isinstance(value, SecretStr):
            urls[job.check] = value.get_secret_value()
    return urls


# --- 領域設定（AD-18）：config/*.yaml ----------------------------------------------------------

# 設定資料夾：環境變數 RADAR_CONFIG_DIR，沒設時是工作目錄下的 config/（映像的 WORKDIR 是 /app）。
CONFIG_DIR_FIELD = "config_dir"
DEFAULT_CONFIG_DIR = "config"
JOURNALS_FILE = "journals.yaml"
OPENALEX_FILE = "openalex.yaml"
EXCLUSION_RULES_FILE = "exclusion_rules.yaml"
KEYWORD_SOURCES_FILE = "keyword_sources.yaml"
# OQ-02 的基準篇數：只有 `radar weekly-run report` 讀；不放進 DomainConfig，api、worker 不讀。
IMPORT_BASELINE_FILE = "import_baseline.yaml"
# 模型與價格（AD-8、ADR-0009）：只有要呼叫 LLM 的指令與工作讀（`load_models_config`）。
MODELS_FILE = "models.yaml"
# 每月預算（NFR-3、ADR-0031）：閘門的 BudgetGuard 與 `radar costs` 讀（`load_budget_config`）。
BUDGET_FILE = "budget.yaml"
# 中文重點的驗證門檻（PRD FR-14、ADR-0032）：跑 `zh_summary` 的工作讀（`load_zh_summary_rules`）。
ZH_SUMMARY_FILE = "zh_summary.yaml"
# 主題與方法的分類表（PRD FR-15、FR-36、ADR-0033）：跑 `classify` 的工作讀（`load_taxonomy`）。
TAXONOMY_FILE = "taxonomy.yaml"
# 研究情境的值清單（PRD FR-3、ADR-0034）：跑 `extract_model` 的工作讀（`load_context_values`）。
CONTEXT_VALUES_FILE = "context_values.yaml"


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """領域設定的資料夾（相對路徑以工作目錄為起點）。"""
    env = os.environ if environ is None else environ
    return Path(env.get(env_name(CONFIG_DIR_FIELD), "") or DEFAULT_CONFIG_DIR)


def load_domain_config(environ: Mapping[str, str] | None = None) -> DomainConfig:
    """讀 `RADAR_CONFIG_DIR` 下的 `journals.yaml`、`openalex.yaml`、`exclusion_rules.yaml`、
    `keyword_sources.yaml` 並驗證。

    四個檔的問題一起回報，每條寫成「檔名：欄位路徑：原因」（例如
    `journals.yaml：journals.2.issn_l：…`）；缺檔、不是合法 YAML、同一個 mapping 的鍵寫兩次
    也各有一條。

    Raises:
        ConfigError: 任何一個檔有問題。
    """
    directory = config_dir(environ)
    problems: list[str] = []
    journals = _load_yaml_model(directory, JOURNALS_FILE, JournalsConfig, problems)
    openalex = _load_yaml_model(directory, OPENALEX_FILE, OpenAlexConfig, problems)
    exclusion_rules = _load_yaml_model(
        directory, EXCLUSION_RULES_FILE, ExclusionRulesConfig, problems
    )
    keyword_sources = _load_yaml_model(
        directory, KEYWORD_SOURCES_FILE, KeywordSourcesConfig, problems
    )
    if (
        problems
        or journals is None
        or openalex is None
        or exclusion_rules is None
        or keyword_sources is None
    ):
        raise ConfigError(problems)
    return DomainConfig(
        journals=journals,
        openalex=openalex,
        exclusion_rules=exclusion_rules,
        keyword_sources=keyword_sources,
    )


def load_import_baseline(environ: Mapping[str, str] | None = None) -> ImportBaseline:
    """讀 `RADAR_CONFIG_DIR` 下的 `import_baseline.yaml`（OQ-02 的各期刊各年篇數）並驗證。

    讀法與錯誤格式和 `load_domain_config` 相同（「檔名：欄位路徑：原因」）。

    Raises:
        ConfigError: 缺檔、不是合法 YAML、鍵寫兩次或內容不對。
    """
    problems: list[str] = []
    baseline = _load_yaml_model(config_dir(environ), IMPORT_BASELINE_FILE, ImportBaseline, problems)
    if problems or baseline is None:
        raise ConfigError(problems)
    return baseline


def load_models_config(environ: Mapping[str, str] | None = None) -> ModelsConfig:
    """讀 `RADAR_CONFIG_DIR` 下的 `models.yaml`（模型、價格、token 估計常數、各工作的模型）並驗證。

    讀法與錯誤格式和 `load_domain_config` 相同（「檔名：欄位路徑：原因」）。

    Raises:
        ConfigError: 缺檔、不是合法 YAML、鍵寫兩次或內容不對（例如價格寫成數字、模型不在白名單）。
    """
    problems: list[str] = []
    models = _load_yaml_model(config_dir(environ), MODELS_FILE, ModelsConfig, problems)
    if problems or models is None:
        raise ConfigError(problems)
    return models


def load_budget_config(environ: Mapping[str, str] | None = None) -> BudgetConfig:
    """讀 `RADAR_CONFIG_DIR` 下的 `budget.yaml`（workspace 上限、熔斷線、各功能預算）並驗證。

    讀法與錯誤格式和 `load_domain_config` 相同（「檔名：欄位路徑：原因」）。

    Raises:
        ConfigError: 缺檔、不是合法 YAML、鍵寫兩次或內容不對（例如金額寫成數字、負數、缺工作、
            熔斷線不低於 workspace 上限）。
    """
    problems: list[str] = []
    budget = _load_yaml_model(config_dir(environ), BUDGET_FILE, BudgetConfig, problems)
    if problems or budget is None:
        raise ConfigError(problems)
    return budget


def load_zh_summary_rules(environ: Mapping[str, str] | None = None) -> ZhSummaryRules:
    """讀 `RADAR_CONFIG_DIR` 下的 `zh_summary.yaml`（中文重點的驗證門檻）並驗證。

    讀法與錯誤格式和 `load_domain_config` 相同（「檔名：欄位路徑：原因」）。

    Raises:
        ConfigError: 缺檔、不是合法 YAML、鍵寫兩次、缺鍵或多鍵、值不是整數（含浮點數、布林）、
            0 或負數、門檻超出範圍。
    """
    problems: list[str] = []
    rules = _load_yaml_model(config_dir(environ), ZH_SUMMARY_FILE, ZhSummaryRules, problems)
    if problems or rules is None:
        raise ConfigError(problems)
    return rules


def load_taxonomy(environ: Mapping[str, str] | None = None) -> Taxonomy:
    """讀 `RADAR_CONFIG_DIR` 下的 `taxonomy.yaml`（主題與方法的分類表），驗證格式後再核對內容雜湊
    和 `domain/taxonomy_hashes.py` 的登記。

    行程啟動時讀一次（AD-18）：同一個行程的分類工作從開始到寫入都用這一個物件當「現在的分類表」。
    讀法與錯誤格式和 `load_domain_config` 相同（「檔名：欄位路徑：原因」）。

    Raises:
        ConfigError: 缺檔、不是合法 YAML、鍵寫兩次、缺 `version`、多欄、`key` 寫法不對或重複、
            用了保留字 `unclassified`；內容改了卻沒升版，或版本沒有登記雜湊。
    """
    problems: list[str] = []
    taxonomy = _load_yaml_model(config_dir(environ), TAXONOMY_FILE, Taxonomy, problems)
    if taxonomy is not None:
        problems.extend(
            f"{TAXONOMY_FILE}：{problem}" for problem in taxonomy_registry_problems(taxonomy)
        )
    if problems or taxonomy is None:
        raise ConfigError(problems)
    return taxonomy


def load_context_values(environ: Mapping[str, str] | None = None) -> ContextValues:
    """讀 `RADAR_CONFIG_DIR` 下的 `context_values.yaml`（研究情境的值清單），驗證格式後再核對內容
    雜湊和 `domain/context_values_hashes.py` 的登記。

    行程啟動時讀一次（AD-18）：同一個行程的研究模型擷取從開始到寫入都用這一個物件當「現在的情境值
    清單」。讀法與錯誤格式和 `load_domain_config` 相同（「檔名：欄位路徑：原因」）。

    Raises:
        ConfigError: 缺檔、不是合法 YAML、鍵寫兩次、缺 `version`、多欄、`key` 寫法不對或同一欄
            重複、國家或產業沒有 `terms`、研究設計或分析單位有 `terms`；內容改了卻沒升版，或版本
            沒有登記雜湊。
    """
    problems: list[str] = []
    values = _load_yaml_model(config_dir(environ), CONTEXT_VALUES_FILE, ContextValues, problems)
    if values is not None:
        problems.extend(
            f"{CONTEXT_VALUES_FILE}：{problem}"
            for problem in context_values_registry_problems(values)
        )
    if problems or values is None:
        raise ConfigError(problems)
    return values


def _load_yaml_model[T: BaseModel](
    directory: Path, filename: str, model: type[T], problems: list[str]
) -> T | None:
    """讀一個 YAML 檔、驗證成 `model`；有問題就加進 `problems`、回 None。"""
    path = directory / filename
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append(f"{filename}：找不到檔案（{path}）")
        return None
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{filename}：讀不出來（{type(exc).__name__}）")
        return None
    try:
        return _parse_yaml_model(text, filename, model, problems)
    except RecursionError:
        # 例如 `journals: &a [*a]`：alias 指回自己，走節點樹或驗證時會無限遞迴。
        problems.append(f"{filename}：YAML 有循環參照（alias 指回自己）或巢狀太深")
        return None


def _parse_yaml_model[T: BaseModel](
    text: str, filename: str, model: type[T], problems: list[str]
) -> T | None:
    """解析 YAML 文字、檢查重複的鍵、驗證成 `model`；有問題就加進 `problems`、回 None。"""
    try:
        document = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        problems.append(f"{filename}：不是合法的 YAML（{_yaml_position(exc)}）")
        return None
    duplicates = _duplicate_keys(document, ())
    if duplicates:
        problems.extend(f"{filename}：{path_}：同一層的鍵寫了兩次" for path_ in duplicates)
        return None

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        problems.append(f"{filename}：不是合法的 YAML（{_yaml_position(exc)}）")
        return None
    if not isinstance(data, dict):
        problems.append(f"{filename}：最上層要是「鍵: 值」的對照（mapping）")
        return None

    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            location = ".".join(str(part) for part in error["loc"]) or "（最上層）"
            message = error["msg"].removeprefix("Value error, ")
            problems.append(f"{filename}：{location}：{message}")
        return None


def _duplicate_keys(node: yaml.Node | None, path: tuple[str, ...]) -> list[str]:
    """YAML 節點樹裡，同一個 mapping 寫了兩次的鍵（以「欄位路徑（第幾行）」表示）。"""
    found: list[str] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[object] = set()
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else repr(key_node)
            child = (*path, str(key))
            if key in seen:
                found.append(f"{'.'.join(child)}（第 {key_node.start_mark.line + 1} 行）")
            seen.add(key)
            found.extend(_duplicate_keys(value_node, child))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            found.extend(_duplicate_keys(item, (*path, str(index))))
    return found


def _yaml_position(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    if mark is None:
        return "格式錯誤"
    return f"第 {mark.line + 1} 行第 {mark.column + 1} 欄"
