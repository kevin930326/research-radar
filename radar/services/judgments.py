"""人工判斷：成員修正分類（Story 3.11、FR-4、FR-16、NFR-16、AD-10、AD-27）。

- `fix_classification`：一個交易做完——先看這位成員看不看得到這篇
  （`repo/papers.find_paper_page_facts`：不存在、看不到、被排除的非研究文章都回 None，和論文頁同一條
  路，ADR-0002；看不到就不去排隊等鎖）→ 拿這一篇的鎖（`classification_fix_lock_key`）→ 拿到鎖後再
  看一次 → 取一次 `clock_timestamp()` 當這次寫入的時間 → 讀目前的有效分類
  （`repo/ai_blocks.find_ai_block`）→ 驗成員送來的值
  （`domain/classification_fixes.check_classification_fix`）→ 寫主題、方法兩筆 `judgment_event`
  → 同一個交易寫一筆稽核（`repo/audit.create_audit_event`；驗收條件寫的 `repo.audit.record()`
  在程式裡就叫這個名字，見 `repo/audit.py` 檔頭）→ 在同一個交易裡重讀分類區塊與歷史回傳。
  兩筆事件與稽核用同一個時間（拿到鎖之後才取，所以排隊的修正新舊和實際寫入的順序一樣）。
  任何一步失敗整個交易退回：兩欄一起寫或都不寫，稽核也不留。
- 權限照 `domain/permissions.py`：`correct_classification` 是任何成員（AD-27）。
- `paper_is_visible`：路由在驗本文之前先查這位成員看不看得到這篇（錯誤的先後：404 在 422 之前，
  ADR-0063）。
- `get_classification_fixes`：論文頁讀分類區塊與修正歷史（只有姓名）；只給成員身分，看不到回 None。
- `list_classification_disagreements`：管理指令列出成員意見不同的論文（每位成員每一欄最新一次選的
  標籤與各自的時間）；意見不同只由有效值 view 判定。
- 日誌只有 id 與原因代號，不帶理由、論文標題、成員送的 key。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog

from radar.domain.ai_blocks import AiBlock, AiBlockSettings, AiProduct
from radar.domain.audit import member_audit_event
from radar.domain.classification_fixes import (
    CORRECT_CLASSIFICATION_ACTION,
    PAPER_TARGET,
    ClassificationFixEntry,
    CurrentClassification,
    MemberPicks,
    check_classification_fix,
    classification_fix_lock_key,
    group_fix_history,
    latest_picks_per_member,
)
from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.permissions import Action, require
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.ai_blocks import find_ai_block, list_disagreeing_paper_ids
from radar.repo.audit import create_audit_event
from radar.repo.db import Database, DbConnection
from radar.repo.judgments import create_judgment_event, list_classification_fix_events
from radar.repo.papers import find_paper_page_facts, get_paper
from radar.services.ai_blocks import current_ai_block_settings
from radar.services.budget import get_budget_status
from radar.services.paper_page import PaperPageSetup

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClassificationFixes:
    """一篇論文的分類區塊（照修正後的有效值）與修正歷史（新的在前）。"""

    classification: AiBlock
    history: tuple[ClassificationFixEntry, ...]


@dataclass(frozen=True)
class PaperDisagreement:
    """成員意見不同的一篇：論文 ID、標題、每位成員每一欄最新一次的選擇與時間（新的在前）。"""

    paper_id: uuid.UUID
    title: str
    picks: tuple[MemberPicks, ...]


def _settings(database: Database, setup: PaperPageSetup, now: datetime) -> AiBlockSettings:
    # 預算准不准只影響「還沒有值」時是哪一種等待狀態；帳本是全站的事實，用系統身分讀（同論文頁）。
    status = get_budget_status(SystemViewer(), database, setup.budget, now=now)
    settings = current_ai_block_settings(setup.models, status, setup.taxonomy, setup.context_values)
    return settings[AiProduct.CLASSIFICATION]


def _history(
    viewer: MemberViewer, connection: DbConnection, paper_id: uuid.UUID
) -> tuple[ClassificationFixEntry, ...]:
    return group_fix_history(list_classification_fix_events(viewer, connection, [paper_id]))


def fix_classification(
    actor: MemberViewer,
    database: Database,
    setup: PaperPageSetup,
    paper_id: uuid.UUID,
    *,
    topic: object,
    method: object,
    reason: object,
    now: datetime,
) -> ClassificationFixes | None:
    """`actor` 修正一篇看得到的論文的分類：主題與方法一起寫（見模組說明），回修正後的分類區塊與
    歷史。
    看不到、不存在、被排除的論文回 None，什麼都不寫。`now` 給預算算台北月份用。

    Raises:
        PermissionDeniedError: `actor` 不能修正分類（權限表）。
        ClassificationFixInvalidError: 成員送來的值不收（什麼都不寫）。
        ValueError: `now` 沒有時區。
        PromptIntegrityError: 有工作沒有登記目前的 prompt 版本。
    """
    require(actor, Action.CORRECT_CLASSIFICATION, None)
    settings = _settings(database, setup, now)
    with database.transaction() as connection:
        # 看不到的論文不去排隊等鎖（等多久會透露這篇有沒有人在修正）；拿到鎖後再看一次。
        if find_paper_page_facts(actor, connection, paper_id) is None:
            return None
        Database.transaction_lock(connection, classification_fix_lock_key(paper_id))
        if find_paper_page_facts(actor, connection, paper_id) is None:
            return None
        # 拿到鎖之後的這一刻（不是交易開始的時間）：兩筆事件與稽核共用。
        written_at = Database.clock_timestamp(connection)
        block = find_ai_block(actor, connection, paper_id, AiProduct.CLASSIFICATION, settings)
        if block is None:
            return None
        current = CurrentClassification.from_block(block)
        fix = check_classification_fix(
            topic=topic, method=method, reason=reason, taxonomy=setup.taxonomy, current=current
        )
        role = ActorRole.ADMIN if actor.is_admin else ActorRole.MEMBER
        event_ids: list[str] = []
        for field, keys in ((TaxonomyField.TOPIC, fix.topic), (TaxonomyField.METHOD, fix.method)):
            event_id = create_judgment_event(
                connection,
                classification_fix(
                    member_id=actor.member_id,
                    actor_role=role,
                    paper_id=paper_id,
                    field=field,
                    keys=keys,
                    reason=fix.reason,
                    previous_keys=current.keys(field),
                ),
                created_at=written_at,
            )
            # 分類修正一定寫入（只有處理結果會回 None）；保險起見，沒寫到就整個交易退回。
            if event_id is None:
                raise RuntimeError("分類修正沒有寫入")
            event_ids.append(str(event_id))
        create_audit_event(
            connection,
            member_audit_event(
                actor,
                action=CORRECT_CLASSIFICATION_ACTION,
                target_type=PAPER_TARGET,
                target_id=paper_id,
                detail={
                    "judgment_event_ids": event_ids,
                    "topic": list(fix.topic),
                    "method": list(fix.method),
                },
            ),
            created_at=written_at,
        )
        updated = find_ai_block(actor, connection, paper_id, AiProduct.CLASSIFICATION, settings)
        if updated is None:
            raise RuntimeError("剛修正的論文在同一個交易裡讀不到")
        history = _history(actor, connection, paper_id)
    log.info("已修正分類", paper_id=str(paper_id), judgment_event_ids=event_ids)
    return ClassificationFixes(classification=updated, history=history)


def paper_is_visible(viewer: MemberViewer, database: Database, paper_id: uuid.UUID) -> bool:
    """這位成員看不看得到這篇論文（和論文頁同一條路：不存在、看不到、被排除的都是 False）。修正
    分類的路由在驗本文之前先查，讓看不到的論文一律 404（ADR-0063 錯誤的先後）；寫入時服務層會
    再查一次。

    Raises:
        TypeError: `viewer` 不是成員身分。
    """
    if not isinstance(viewer, MemberViewer):
        raise TypeError("要是成員身分")
    with database.transaction() as connection:
        return find_paper_page_facts(viewer, connection, paper_id) is not None


def get_classification_fixes(
    viewer: MemberViewer,
    database: Database,
    setup: PaperPageSetup,
    paper_id: uuid.UUID,
    *,
    now: datetime,
) -> ClassificationFixes | None:
    """`viewer` 看得到的那篇論文的分類區塊與修正歷史（新的在前，只有姓名）；看不到、不存在、被排除的
    論文回 None。修正歷史只給成員：公開身分（示範站產生器）與維運身分在執行時就被擋（示範站不帶修正
    歷史；維運只用管理指令）。

    Raises:
        TypeError: `viewer` 不是成員身分。
        ValueError: `now` 沒有時區。
        PromptIntegrityError: 有工作沒有登記目前的 prompt 版本。
    """
    if not isinstance(viewer, MemberViewer):
        raise TypeError("修正歷史只給成員讀")
    settings = _settings(database, setup, now)
    with database.transaction() as connection:
        if find_paper_page_facts(viewer, connection, paper_id) is None:
            return None
        block = find_ai_block(viewer, connection, paper_id, AiProduct.CLASSIFICATION, settings)
        if block is None:
            return None
        history = _history(viewer, connection, paper_id)
    return ClassificationFixes(classification=block, history=history)


def list_classification_disagreements(
    viewer: SystemViewer, database: Database
) -> tuple[PaperDisagreement, ...]:
    """分類有成員意見不同的論文（照論文 ID），每篇列出每位成員每一欄最新一次選的主題、方法與各自的
    時間。

    Raises:
        TypeError: `viewer` 不是維運身分。
    """
    if not isinstance(viewer, SystemViewer):
        raise TypeError("要是維運身分")
    with database.transaction() as connection:
        paper_ids = list_disagreeing_paper_ids(viewer, connection)
        events = list_classification_fix_events(viewer, connection, paper_ids)
        result: list[PaperDisagreement] = []
        for paper_id in paper_ids:
            paper = get_paper(viewer, connection, paper_id)
            if paper is None:  # pragma: no cover - 系統身分看得到全部
                continue
            result.append(
                PaperDisagreement(
                    paper_id=paper_id,
                    title=paper.title,
                    picks=latest_picks_per_member(e for e in events if e.paper_id == paper_id),
                )
            )
    return tuple(result)
