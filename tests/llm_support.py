"""LLM 閘門測試共用的小工具：在 tmp 資料夾寫 prompt 檔並暫時登記、讀真的 `config/models.yaml`。

prompt 的內容全部是測試自己編的（不是正式的 prompt）。登記寫進 `radar/domain/prompt_hashes.py`
的登記表（policy 只認那裡的雜湊），`tests/conftest.py` 每個測試結束都會還原。
"""

import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from radar.adapters.fake import FakeLlmProvider
from radar.config import load_models_config
from radar.domain import prompt_hashes
from radar.domain.ai_outputs import CallMode, GatewayConfig, LlmTask
from radar.domain.budget import BudgetConfig
from radar.domain.fingerprints import prompt_sha256, schema_sha256
from radar.domain.llm import LlmBatchRequest, LlmRequest, LlmResult, frozen_schema_json
from radar.domain.papers import PaperOrigin, ParsedPaper
from radar.domain.policy import CloudPolicy
from radar.domain.pricing import ModelsConfig
from radar.domain.prompts import PROMPT_FILE_PATTERN, parse_prompt_file
from radar.domain.scope import ScopeSettings
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.services.prompt_loader import OUTPUT_SCHEMAS
from tests.database import TestDatabase

ROOT = Path(__file__).resolve().parents[1]
CODE_VERSION = "test-0.0.0"


def prompt_text(
    body: str = "請只根據文件找出研究問題，用 JSON 回答。",
    *,
    effort: str | None = "low",
    max_tokens: int = 1024,
    disable_thinking: bool = False,
    output_schema: str | None = "FakeResearchQuestion",
) -> str:
    """一個 prompt 檔的內容（設定區＋本文）。"""
    lines = ["---"]
    if effort is not None:
        lines.append(f"effort: {effort}")
    lines.append(f"max_tokens: {max_tokens}")
    lines.append(f"disable_thinking: {'true' if disable_thinking else 'false'}")
    if output_schema is not None:
        lines.append(f"output_schema: {output_schema}")
    lines += ["---", body, ""]
    return "\n".join(lines)


def write_prompt(root: Path, task: LlmTask, version: int, text: str) -> Path:
    """寫 `<root>/<工作>/v<N>.md`（不登記）。"""
    path = root / task.value / f"v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def register_prompts(root: Path, current: dict[LlmTask, int] | None = None) -> None:
    """依 `root` 底下現有的檔案，在 `prompt_hashes` 的登記表登記（算檔案雜湊與 schema 雜湊）。

    會整個換掉登記表（`tests/conftest.py` 在測試結束時還原）。`current` 沒給的工作用最大的版本。
    """
    hashes: dict[str, dict[int, tuple[str, str | None]]] = {}
    currents: dict[str, int] = {}
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        task = LlmTask(directory.name)
        versions: dict[int, tuple[str, str | None]] = {}
        for file in sorted(directory.iterdir()):
            match = PROMPT_FILE_PATTERN.fullmatch(file.name)
            assert match is not None, file
            content = file.read_bytes()
            parsed = parse_prompt_file(content.decode("utf-8"))
            schema = None
            if parsed.settings.output_schema is not None:
                schema = schema_sha256(
                    frozen_schema_json(OUTPUT_SCHEMAS[parsed.settings.output_schema])
                )
            versions[int(match.group(1))] = (prompt_sha256(content), schema)
        hashes[task.value] = versions
        currents[task.value] = (current or {}).get(task, max(versions))
    prompt_hashes.PROMPT_FILE_HASHES = hashes
    prompt_hashes.CURRENT_PROMPT_VERSIONS = currents


def make_prompts(root: Path, *tasks: LlmTask, text: str | None = None) -> Path:
    """每個工作寫一個 v1 並登記，回 `root`。"""
    root.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        write_prompt(root, task, 1, text if text is not None else prompt_text())
    register_prompts(root)
    return root


def models_config() -> ModelsConfig:
    """真的 `config/models.yaml`。"""
    return load_models_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})


def gateway_config(
    prompts_root: Path,
    *,
    models: ModelsConfig | None = None,
    call_mode: CallMode = CallMode.INTERACTIVE,
    paywalled_fulltext_llm: bool = False,
) -> GatewayConfig:
    return GatewayConfig(
        models=models or models_config(),
        prompts_root=prompts_root,
        cloud_policy=CloudPolicy(paywalled_fulltext_llm=paywalled_fulltext_llm),
        code_version=CODE_VERSION,
        call_mode=call_mode,
    )


# --- 整合測試：造論文、改摘要、數列數 -------------------------------------------------------

LEDGER_TABLES = ("derivation_run", "llm_call", "zh_summary", "classification", "research_model")
FETCHED_AT = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def create_paper(
    database: Database,
    *,
    doi: str = "10.1/abc",
    openalex_id: str = "W1",
    title: str = "A study of information systems",
    abstract: str | None = "We study how teams adopt information systems.",
) -> uuid.UUID:
    """經 `upsert_by_external_id` 新增一篇論文（摘要的來源是 OpenAlex），回它的 id。"""
    paper = ParsedPaper.model_validate(
        {
            "source": "openalex",
            "fetched_at": FETCHED_AT,
            "doi": doi,
            "openalex_id": openalex_id,
            "title": title,
            "journal_issn_l": "0276-7783",
            "publication_year": 2025,
            "abstract": abstract,
            "abstract_source": "openalex" if abstract is not None else None,
        }
    )
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            paper,
            origin=PaperOrigin.TRACKED,
            excluded_reason=None,
            scope=ScopeSettings(frozenset({"0276-7783"})),
        )
    return result.paper_id


def execute(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    """用超級使用者執行一句 SQL（測試直接改資料、查結果用），回所有列（沒有回傳列時是空的）。"""
    with psycopg.connect(db.admin_url, autocommit=True, connect_timeout=5) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def set_abstract(db: TestDatabase, paper_id: uuid.UUID, content: str) -> None:
    execute(db, "UPDATE paper_abstract SET content = %s WHERE paper_id = %s", content, paper_id)


def table_counts(db: TestDatabase) -> dict[str, int]:
    """帳本與表頭表的列數，加上費用合計（NFR-5 比對用）。"""
    counts = {
        table: int(execute(db, f"SELECT count(*) FROM {table}")[0][0]) for table in LEDGER_TABLES
    }
    counts["cost_micro_usd"] = int(
        execute(db, "SELECT COALESCE(sum(cost_usd), 0) * 1000000 FROM llm_call")[0][0]
    )
    return counts


class GatedProvider:
    """包一個 `FakeLlmProvider`：呼叫時先通知 `entered`，再等 `release` 才回覆（並行測試用）。

    `before_reply`：等完之後、回覆之前在呼叫的執行緒裡執行（例如改摘要、跑 sweep）。
    """

    def __init__(
        self,
        inner: FakeLlmProvider,
        *,
        gated: bool = True,
        before_reply: Callable[[], None] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        if not gated:
            self.release.set()
        self.before_reply = before_reply
        self.timeout = timeout
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, request: LlmRequest) -> LlmResult:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if not self.release.wait(self.timeout):
            raise AssertionError("GatedProvider 等不到 release")
        if self.before_reply is not None:
            self.before_reply()
        return self.inner.complete(request)

    def submit_batch(self, requests: Sequence[LlmBatchRequest]) -> str:
        """`LlmProvider` 的 Batch 送出（Story 2.9）：直接交給包著的替身，不經閘門。"""
        return self.inner.submit_batch(requests)


# --- 預算（Story 2.3）：設定與假帳本 -----------------------------------------------------------


def budget_config(**budgets: str | None) -> BudgetConfig:
    """熔斷線 50、workspace 上限 70；沒給的工作沒有個別上限。"""
    task_budgets: dict[str, str | None] = {task.value: None for task in LlmTask}
    task_budgets.update(budgets)
    return BudgetConfig.model_validate(
        {"workspace_limit_usd": "70", "monthly_breaker_usd": "50", "task_budgets_usd": task_budgets}
    )


def insert_ledger(
    db: TestDatabase,
    paper_id: uuid.UUID,
    task: LlmTask,
    cost: str,
    at: datetime,
    *,
    is_batch: bool = False,
    eval_run_id: uuid.UUID | None = None,
) -> None:
    """假帳本：一筆做完的 `derivation_run` 加一筆 `llm_call`（`created_at`＝`at`）。"""
    run_id = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, eval_run_id, status,"
        " finished_at, created_at)"
        " VALUES (%s, %s, 1, %s, %s, %s, 'abstract', '{}'::jsonb, 'test', %s, %s, 'succeeded',"
        " %s, %s) RETURNING id",
        paper_id,
        task.value,
        "a" * 64,
        "claude-haiku-5-5",
        uuid.uuid4().hex * 2,
        "batch" if is_batch else "interactive",
        eval_run_id,
        at,
        at,
    )[0][0]
    execute(
        db,
        "INSERT INTO llm_call (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
        " cache_read_input_tokens, cost_usd, price_tier, is_batch, created_at)"
        " VALUES (%s, 0, 0, 0, 0, %s, 'standard', %s, %s)",
        run_id,
        Decimal(cost),
        is_batch,
        at,
    )
