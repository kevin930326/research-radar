"""示範站匯出（Story 3.7）的測試資料：全部是自己編的假論文（標題 `Fake Paper …`），不是真的論文、
真的摘要或真的 AI 產出。

- 論文經 `upsert_by_external_id` 寫入（OpenAlex 的形狀照 `tests/demo_works.py`），授權、出版社、
  `demo_eligible` 由正式的 `config/`（`journals.yaml`、`demo_publishers.yaml`）算；AI 產出用整合
  測試的輔助函式直接寫進表。
- 兩個已發佈的快照：舊的（第 1 號）與最新的（第 2 號）；`new_this_week` 的論文算進最新的那一個。
- `tests/integration/test_demo_export.py` 與 `tests/demo_preview.py`（產生
  `web/e2e/fixtures/demo-preview/`）共用。
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.config import load_context_values, load_domain_config, load_taxonomy
from radar.domain.ai_blocks import AiProduct
from radar.domain.ai_outputs import InputKind, build_llm_input
from radar.domain.fingerprints import input_sha256
from radar.domain.papers import PaperOrigin
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.scope import ScopeSettings
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import find_paper_llm_source, upsert_by_external_id
from radar.services.ai_blocks import demo_ai_block_settings
from radar.services.demo_export import DemoExportSetup
from radar.services.members import allow_member
from tests.database import TestDatabase
from tests.demo_works import demo_work, demo_work_bytes
from tests.integration.test_effective_views import (
    fix,
    insert_classification,
    insert_header,
    insert_research_model,
    insert_run,
)
from tests.llm_support import execute, models_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}

MISQ = "0276-7783"
ISR = "1047-7047"
IO = "1471-7727"
TEM = "0018-9391"  # IEEE Transactions on Engineering Management（設定檔依期刊排除）
IEEE_NAME = "Institute of Electrical and Electronics Engineers"
IEEE_ID = "https://openalex.org/P4310319808"

OLD_AS_OF = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)
LATEST_AS_OF = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
EXPORTED_AT = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
# 修正分類的假成員（自我檢查會找這個 email，它不該出現在任何匯出的檔裡）。
FIXER_EMAIL = "demo.fixer@example.org"

ZH_CONTENT = {
    "research_question": "（假資料）團隊怎麼採用協作工具？",
    "method": "（假資料）問卷調查",
    "theory": None,
    "findings": "（假資料）信任提高採用意願。",
}


@dataclass(frozen=True)
class PaperSpec:
    """一篇假論文。

    - `license`、`publisher_*`：出版社自己那一版的授權與出版社（決定 `demo_eligible`）。
    - `abstract`：有沒有 OpenAlex 的摘要；`s2_abstract`：另外放一筆 Semantic Scholar 的摘要當
      目前的。
    - `topics`／`methods`：有效分類（AI 值）；`topics` 是 None 表示沒有分類產出。
    - `classification_failed`：目前這組鍵只有被拒收的一筆（區塊狀態 `failed`）。
    - `human_topics`：一位假成員修正過的主題（「已核對」）。
    - `zh_summary`、`research_model`：有沒有有效的中文重點、研究模型。
    """

    name: str
    number: int
    issn_l: str = MISQ
    year: int = 2024
    license: str | None = "cc-by"
    publisher_id: str | None = "https://openalex.org/P9000000001"
    publisher_name: str | None = "Example Open Press"
    abstract: bool = True
    s2_abstract: bool = False
    excluded_reason: str | None = None
    new_this_week: bool = False
    topics: tuple[str, ...] | None = ("erp",)
    methods: tuple[str, ...] = ("survey_sem",)
    classification_failed: bool = False
    human_topics: tuple[str, ...] | None = None
    zh_summary: bool = False
    research_model: bool = False
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def demo_setup(canaries: Sequence[str] = ()) -> DemoExportSetup:
    """正式的 `config/` 組成的匯出設定（金絲雀字串由測試給）。"""
    return DemoExportSetup(
        models=models_config(),
        taxonomy=load_taxonomy(CONFIG_ENV),
        context_values=load_context_values(CONFIG_ENV),
        journals=load_domain_config(CONFIG_ENV).journals,
        canaries=tuple(canaries),
    )


def real_scope() -> ScopeSettings:
    """正式的追蹤範圍與示範站排除清單（`DomainConfig.scope_settings()`）。"""
    return load_domain_config(CONFIG_ENV).scope_settings()


def publish_snapshots(db: TestDatabase) -> tuple[uuid.UUID, uuid.UUID]:
    """兩個已發佈的快照（第 1、2 號），回（舊的, 最新的）id。"""
    ids: list[uuid.UUID] = []
    for number, as_of in ((1, OLD_AS_OF), (2, LATEST_AS_OF)):
        (run,) = execute(db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id")
        (row,) = execute(
            db,
            "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
            " VALUES (%s, %s, 'published', %s) RETURNING id",
            number,
            run[0],
            as_of,
        )
        ids.append(uuid.UUID(str(row[0])))
    return ids[0], ids[1]


def seed_papers(
    db: TestDatabase,
    database: Database,
    specs: Sequence[PaperSpec],
    *,
    snapshots: tuple[uuid.UUID, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """照 `specs` 寫入假論文與它們的 AI 產出，回（名稱 → 論文 id）。"""
    scope = real_scope()
    setup = demo_setup()
    settings = demo_ai_block_settings(setup.models, setup.taxonomy, setup.context_values)
    ids: dict[str, uuid.UUID] = {}
    fixer: uuid.UUID | None = None
    for spec in specs:
        work = demo_work(
            spec.number,
            issn_l=spec.issn_l,
            license=spec.license,
            publisher_id=spec.publisher_id,
            publisher_name=spec.publisher_name,
            abstract=spec.abstract,
            year=spec.year,
        )
        title = spec.title or f"Fake Paper {spec.number}: {spec.name.replace('_', ' ')}"
        work["title"] = work["display_name"] = title
        work["authorships"] = [
            {"author": {"display_name": f"Test Author {spec.number}"}},
            {"author": {"display_name": "Second Author"}},
        ]
        work.update(spec.extra)
        paper = parse_openalex_work(demo_work_bytes(work), fetched_at=OLD_AS_OF)
        with database.transaction() as connection:
            result = upsert_by_external_id(
                connection,
                paper,
                origin=PaperOrigin.TRACKED,
                excluded_reason=spec.excluded_reason,
                scope=scope,
            )
        paper_id = result.paper_id
        ids[spec.name] = paper_id
        first = snapshots[1] if spec.new_this_week else snapshots[0]
        execute(db, "UPDATE paper SET first_snapshot_id = %s WHERE id = %s", first, paper_id)
        if spec.s2_abstract:
            execute(
                db, "UPDATE paper_abstract SET is_current = false WHERE paper_id = %s", paper_id
            )
            execute(
                db,
                "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
                " VALUES (%s, 'semantic_scholar', %s, true)",
                paper_id,
                f"Fake abstract from another source {spec.number}.",
            )
        has_abstract = spec.abstract or spec.s2_abstract
        input_kind = "abstract" if has_abstract else "title"
        if spec.classification_failed:
            key = settings[AiProduct.CLASSIFICATION]
            run_id = insert_run(
                db,
                paper_id,
                "classify",
                input_sha256=_current_sha(database, paper_id, key),
                prompt_version=key.prompt_version,
                model=key.model,
                input_kind=input_kind,
            )
            insert_header(
                db, paper_id, "classify", status="rejected", run_id=run_id, input_kind=input_kind
            )
        elif spec.topics is not None:
            insert_classification(db, paper_id, spec.topics, spec.methods, input_kind=input_kind)
        if spec.human_topics is not None:
            if fixer is None:
                fixer = allow_member(
                    SystemViewer(), database, email=FIXER_EMAIL, display_name="假成員"
                ).member_id
            fix(database, paper_id, TaxonomyField.TOPIC, spec.human_topics, member_id=fixer)
        if spec.zh_summary:
            insert_header(db, paper_id, "zh_summary", ZH_CONTENT, input_kind=input_kind)
        if spec.research_model:
            insert_research_model(db, paper_id, country="taiwan")
    return ids


def _current_sha(database: Database, paper_id: uuid.UUID, settings: Any) -> str:
    """這篇論文的分類現在的 `input_sha256`（和 `find_ai_block` 用同一套 domain 函式）。"""
    with database.transaction() as connection:
        source = find_paper_llm_source(SystemViewer(), connection, paper_id, for_update=False)
    assert source is not None
    kind = InputKind.ABSTRACT if source.abstract is not None else InputKind.TITLE
    return input_sha256(
        build_llm_input(source, kind).blocks,
        AiProduct.CLASSIFICATION.llm_task,
        taxonomy_version=settings.taxonomy_version,
        context_values_version=settings.context_values_version,
    )


# --- 涵蓋各種情況的一組（整合測試與給前端的範例資料共用）--------------------------------------

_TOPICS = (
    ("erp",),
    ("e_commerce", "digital_platforms"),
    ("it_adoption_use",),
    ("security_privacy", "health_it"),
    ("knowledge_collaboration",),
    ("unclassified",),
)
_METHODS = (
    ("survey_sem",),
    ("experiment",),
    ("interview_case_study", "design_science"),
    ("secondary_data_econometrics",),
)
_JOURNALS = (MISQ, ISR, IO)


def preview_specs(count: int = 60) -> list[PaperSpec]:
    """給前端的範例資料：`count` 篇（預設 60，超過一頁 50 篇），涵蓋每一種狀態與篩選。

    前幾篇是固定的特殊情況，其餘照順序輪流換期刊、年份、主題、方法、授權。
    """
    specs = [
        PaperSpec(
            "full_content",
            1,
            year=2025,
            new_this_week=True,
            zh_summary=True,
            research_model=True,
        ),
        PaperSpec("eligible_without_abstract", 2, abstract=False, year=2025),
        PaperSpec("biblio_only_not_open", 3, license="cc-by-nc", zh_summary=True),
        PaperSpec("classification_failed", 4, classification_failed=True),
        PaperSpec("no_ai_output_yet", 5, topics=None),
        PaperSpec("full_content_ai_pending", 6, issn_l=ISR, year=2023),
        PaperSpec(
            "ieee_excluded_publisher",
            7,
            issn_l=IO,
            publisher_id=IEEE_ID,
            publisher_name=IEEE_NAME,
            zh_summary=True,
        ),
        PaperSpec(
            "ieee_journal_only",
            8,
            issn_l=TEM,
            publisher_id="https://openalex.org/P9000000077",
            publisher_name="The Engineering Management Press",
            zh_summary=True,
        ),
        PaperSpec("other_source_abstract", 9, abstract=False, s2_abstract=True),
        PaperSpec("title_only_classification", 10, abstract=False, license=None, year=2022),
        PaperSpec("human_verified_topic", 11, human_topics=("it_strategy_digital_transformation",)),
    ]
    fixed = len(specs)
    for number in range(fixed + 1, count + 1):
        index = number - fixed - 1
        open_access = index % 3 != 0
        specs.append(
            PaperSpec(
                f"paper_{number}",
                number,
                issn_l=_JOURNALS[index % len(_JOURNALS)],
                year=2022 + index % 4,
                license="cc-by" if open_access else "cc-by-nc",
                abstract=index % 7 != 0,
                new_this_week=index % 5 == 0,
                topics=_TOPICS[index % len(_TOPICS)],
                methods=_METHODS[index % len(_METHODS)],
                zh_summary=open_access and index % 2 == 0,
                research_model=open_access and index % 4 == 0,
            )
        )
    return specs
