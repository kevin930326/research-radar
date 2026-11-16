"""`batch_submission`（Batch 的狀態表）與 `batch_item`（批次裡的一筆）兩張表：表的定義與唯一的寫入者
（AD-14、ADR-0004 決定第 2、4 點、ADR-0041、Story 2.9）。

- `batch_submission`：狀態 `queued` → `submitting` → `submitted` → `ended` → `imported`；`failed`；
  `needs_attention`。`queued`／`submitting` 時一定沒有供應商 batch ID，`submitted` 以後一定有；
  非空的 batch ID 唯一。`ended`／`imported` 一定有 `ended_at`，`imported` 一定有 `imported_at`
  （遷移 0017、Story 2.11）。狀態只由 `services/llm_batches.py` 改，每個轉移都是條件式 UPDATE（只在
  預期的起始狀態才改，回有沒有改到），並行時靠資料庫擋。
- `batch_item`：不是狀態表（沒有 `status`），進度跟著它的 `derivation_run`（`run_id` 唯一）；
  `custom_id` 就是它的 `id.hex`。`estimated_max_cost_usd` 是送出前的保守上限，run 還是 `running`
  的加總就是「尚未結束的 Batch 預留」。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
"""

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    RowMapping,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.ai_outputs import InputKind, LlmTask, RunKey, RunStatus
from radar.domain.errors import ErrorCode
from radar.domain.llm_batches import BatchItemRun, BatchSubmission, BatchSubmissionStatus
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.models import Base
from radar.repo.visibility import visible_papers, visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0016 建表） -----------------------------

_STATUSES = ", ".join(f"'{status.value}'" for status in BatchSubmissionStatus)

batch_submission_table = Table(
    "batch_submission",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    # 屬於哪一次每週更新；手動補跑的可以沒有（ADR-0004 決定第 4 點）。
    Column("weekly_run_id", Uuid, ForeignKey("weekly_run.id"), index=True),
    Column("status", Text, nullable=False),
    Column("provider_batch_id", Text),
    Column("item_count", Integer, nullable=False),
    Column("estimated_max_cost_usd", Numeric(12, 6), nullable=False),
    Column("failure_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("submitted_at", DateTime(timezone=True)),
    # 遷移 0017（Story 2.11）：看到供應商 `ended` 的時間、匯入做完的時間。
    Column("ended_at", DateTime(timezone=True)),
    Column("imported_at", DateTime(timezone=True)),
    # 遷移 0017（Story 2.11 確認審查 D2）：同一個供應商 batch ID 連續幾次讀到的結果項數比供應商的
    # 計數少（`submitted` → `ended` 時歸零）。
    Column("incomplete_reads", Integer, nullable=False, server_default=text("0")),
    Index(
        "uq_batch_submission_provider_batch_id",
        "provider_batch_id",
        unique=True,
        postgresql_where=text("provider_batch_id IS NOT NULL"),
    ),
    CheckConstraint(f"status IN ({_STATUSES})", name="status"),
    CheckConstraint(
        "provider_batch_id IS NULL OR provider_batch_id ~ '^[A-Za-z0-9_-]{1,128}$'",
        name="provider_batch_id_format",
    ),
    CheckConstraint(
        "status NOT IN ('queued', 'submitting') OR provider_batch_id IS NULL",
        name="no_provider_batch_id_before_submitted",
    ),
    CheckConstraint(
        "status NOT IN ('submitted', 'ended', 'imported') OR provider_batch_id IS NOT NULL",
        name="provider_batch_id_once_submitted",
    ),
    CheckConstraint(
        "status NOT IN ('submitted', 'ended', 'imported') OR submitted_at IS NOT NULL",
        name="submitted_at_once_submitted",
    ),
    CheckConstraint(
        "submitted_at IS NULL OR provider_batch_id IS NOT NULL",
        name="submitted_at_needs_provider_batch_id",
    ),
    CheckConstraint(
        "(status IN ('failed', 'needs_attention')) = (failure_reason IS NOT NULL)",
        name="failure_reason_iff_failed_or_needs_attention",
    ),
    CheckConstraint(
        "failure_reason IS NULL OR failure_reason ~ '^[a-z][a-z0-9_]*$'",
        name="failure_reason_format",
    ),
    CheckConstraint(
        "status NOT IN ('ended', 'imported') OR ended_at IS NOT NULL", name="ended_at_once_ended"
    ),
    CheckConstraint(
        "status <> 'imported' OR imported_at IS NOT NULL", name="imported_at_once_imported"
    ),
    CheckConstraint("item_count >= 0", name="item_count_not_negative"),
    CheckConstraint("estimated_max_cost_usd >= 0", name="estimated_max_cost_usd_not_negative"),
    CheckConstraint("incomplete_reads >= 0", name="incomplete_reads_not_negative"),
)

batch_item_table = Table(
    "batch_item",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column(
        "batch_submission_id",
        Uuid,
        ForeignKey("batch_submission.id"),
        nullable=False,
        index=True,
    ),
    Column("run_id", Uuid, ForeignKey("derivation_run.id"), nullable=False),
    # 和表頭的 `abstract_id` 一樣不設外鍵（摘要列會被換掉，紀錄要留著）。
    Column("abstract_id", Uuid),
    Column("estimated_max_cost_usd", Numeric(12, 6), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 一筆 run 只屬於一個 batch_item（也當外鍵的索引）。
    UniqueConstraint("run_id"),
    CheckConstraint("estimated_max_cost_usd >= 0", name="estimated_max_cost_usd_not_negative"),
)

_SUBMISSION_COLUMNS = (
    "id, weekly_run_id, status, provider_batch_id, item_count, estimated_max_cost_usd,"
    " failure_reason"
)


def _submission(row: RowMapping) -> BatchSubmission:
    return BatchSubmission(
        id=row["id"],
        weekly_run_id=row["weekly_run_id"],
        status=BatchSubmissionStatus(row["status"]),
        provider_batch_id=row["provider_batch_id"],
        item_count=row["item_count"],
        estimated_max_cost_usd=Decimal(row["estimated_max_cost_usd"]),
        failure_reason=row["failure_reason"],
    )


# --- 讀取 ---------------------------------------------------------------------------------


def find_batch_submission(
    viewer: SystemViewer, connection: Connection, batch_submission_id: uuid.UUID
) -> BatchSubmission | None:
    """一筆 `batch_submission`；不存在回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    row = (
        connection.execute(
            text(
                f"SELECT {_SUBMISSION_COLUMNS} FROM batch_submission WHERE {visible} AND id = :id"
            ),
            {"id": batch_submission_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _submission(row)


def list_batch_submissions(
    viewer: SystemViewer,
    connection: Connection,
    statuses: Iterable[BatchSubmissionStatus],
    ids: Iterable[uuid.UUID] | None = None,
) -> list[BatchSubmission]:
    """狀態在 `statuses` 裡的批次（有給 `ids` 時只看這幾筆），依建立順序（`created_at`, `id`）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    wanted = [BatchSubmissionStatus(status).value for status in statuses]
    only = "" if ids is None else " AND id = ANY(:ids)"
    rows = (
        connection.execute(
            text(
                f"SELECT {_SUBMISSION_COLUMNS} FROM batch_submission"
                f" WHERE {visible} AND status = ANY(:statuses){only}"
                " ORDER BY created_at, id"
            ),
            {"statuses": wanted, "ids": None if ids is None else list(ids)},
        )
        .mappings()
        .all()
    )
    return [_submission(row) for row in rows]


def count_batch_submissions(
    viewer: SystemViewer, connection: Connection, status: BatchSubmissionStatus
) -> int:
    """這個狀態的批次有幾筆（例如 `needs_attention`）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(f"SELECT count(*) FROM batch_submission WHERE {visible} AND status = :status"),
        {"status": BatchSubmissionStatus(status).value},
    ).scalar_one()
    return int(value)


def count_batch_running_runs(
    viewer: SystemViewer, connection: Connection, batch_submission_id: uuid.UUID
) -> int:
    """這個批次裡還是 `running` 的 run 有幾筆（`radar batches resolve --fail` 確認前印）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(
            "SELECT count(*) FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
            f" WHERE {visible} AND i.batch_submission_id = :id AND r.status = 'running'"
        ),
        {"id": batch_submission_id},
    ).scalar_one()
    return int(value)


def list_batch_pending_paper_ids(
    viewer: SystemViewer,
    connection: Connection,
    batch_submission_ids: Iterable[uuid.UUID] | None,
) -> list[uuid.UUID]:
    """這幾批（`None`＝全部的批次）裡寫結果失敗、等互動接續的 run（`failed`／`llm_result_pending`：
    帳記了、輸出存著，表頭還沒寫）屬於哪些論文，不重複、依論文 id（`radar batches wait` 結束前印，
    Story 2.11 審查後）。同一篇同一個工作之後已經有更新的平常流程 run（例如 prompt 升版後重跑過）
    的不算：那筆的鍵已經不是現在的，接續不到，列出來只會讓 wait 一直以 1 結束（確認審查 D3）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    scope = "" if batch_submission_ids is None else " AND i.batch_submission_id = ANY(:ids)"
    rows = connection.execute(
        text(
            "SELECT DISTINCT r.paper_id"
            " FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
            f" WHERE {visible}{scope}"
            " AND r.status = 'failed' AND r.failure_reason = :pending"
            " AND NOT EXISTS ("
            " SELECT 1 FROM derivation_run AS newer"
            " WHERE newer.paper_id = r.paper_id AND newer.task = r.task"
            " AND newer.eval_run_id IS NULL AND newer.created_at > r.created_at)"
            " ORDER BY r.paper_id"
        ),
        {
            "ids": None if batch_submission_ids is None else list(batch_submission_ids),
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
        },
    ).all()
    return [uuid.UUID(str(paper_id)) for (paper_id,) in rows]


def find_batch_attention_run(viewer: Viewer, connection: Connection, key: RunKey) -> bool:
    """這組鍵有沒有平常流程的 `running` run，屬於卡在 `needs_attention` 的批次（AI 區塊判定
    `failed` 用，ADR-0004 決定第 4 點、ADR-0015）。論文不存在或 `viewer` 看不到時回 False。
    """
    visible = visible_papers(viewer, alias="paper")
    value = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM derivation_run AS r"
            " JOIN paper ON paper.id = r.paper_id"
            " JOIN batch_item AS i ON i.run_id = r.id"
            " JOIN batch_submission AS s ON s.id = i.batch_submission_id"
            f" WHERE {visible}"
            " AND r.paper_id = :paper_id AND r.task = :task AND r.prompt_version = :prompt_version"
            " AND r.model = :model AND r.input_sha256 = :input_sha256"
            " AND r.status = 'running' AND r.eval_run_id IS NULL"
            " AND s.status = 'needs_attention')"
        ),
        {
            "paper_id": key.paper_id,
            "task": LlmTask(key.task).value,
            "prompt_version": key.prompt_version,
            "model": key.model,
            "input_sha256": key.input_sha256,
        },
    ).scalar_one()
    return bool(value)


def list_batch_items(
    viewer: SystemViewer, connection: Connection, batch_submission_id: uuid.UUID
) -> list[BatchItemRun]:
    """這個批次的每一筆 `batch_item` 與它的 `derivation_run`（鍵、`input_kind`、狀態），連同所屬
    批次的 id 與讀出當下的供應商 batch ID，依 `batch_item.id` 排序。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    rows = (
        connection.execute(
            text(
                "SELECT i.id AS item_id, i.run_id, i.abstract_id, i.estimated_max_cost_usd,"
                " r.paper_id, r.task, r.prompt_version, r.model, r.input_sha256, r.input_kind,"
                " r.status AS run_status, s.id AS batch_submission_id, s.provider_batch_id"
                " FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
                " JOIN batch_submission AS s ON s.id = i.batch_submission_id"
                f" WHERE {visible} AND i.batch_submission_id = :id"
                " ORDER BY i.id"
            ),
            {"id": batch_submission_id},
        )
        .mappings()
        .all()
    )
    return [
        BatchItemRun(
            item_id=row["item_id"],
            run_id=row["run_id"],
            key=RunKey(
                paper_id=row["paper_id"],
                task=LlmTask(row["task"]),
                prompt_version=row["prompt_version"],
                model=row["model"],
                input_sha256=row["input_sha256"],
            ),
            input_kind=InputKind(row["input_kind"]),
            run_status=RunStatus(row["run_status"]),
            abstract_id=row["abstract_id"],
            estimated_max_cost_usd=Decimal(row["estimated_max_cost_usd"]),
            batch_submission_id=row["batch_submission_id"],
            provider_batch_id=row["provider_batch_id"],
        )
        for row in rows
    ]


def find_batch_submission_importing(
    viewer: SystemViewer,
    connection: Connection,
    batch_submission_id: uuid.UUID,
    provider_batch_id: str,
) -> bool:
    """（結果匯入的每個寫入交易開頭）這批還是 `ended`、而且供應商 batch ID 還是 `provider_batch_id`
    就以 `FOR SHARE` 鎖住這一列、回 True；否則回 False（Story 2.11 審查後）。

    鎖到交易結束：期間別的行程不能把這批改成 `needs_attention`、`imported` 或換 ID（那些
    UPDATE 會等這個交易做完，之後的交易就會看到新狀態）；兩個 poller 同時拿 `FOR SHARE` 不互擋。
    鎖的順序一律先批次、再論文、再 run。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(
            f"SELECT 1 FROM batch_submission WHERE {visible} AND id = :id"
            " AND status = 'ended' AND provider_batch_id = :batch_id FOR SHARE"
        ),
        {"id": batch_submission_id, "batch_id": provider_batch_id},
    ).scalar_one_or_none()
    return value is not None


def list_llm_spending_and_reservations_by_task(
    viewer: SystemViewer, connection: Connection, start: datetime, end: datetime
) -> tuple[dict[LlmTask, Decimal], dict[LlmTask, Decimal]]:
    """預算的兩個來源，**同一句 SQL** 讀（同一個快照）：回（帳本, 尚未結束的 Batch 預留），各依
    工作加總，沒有的工作不在結果裡。

    - 帳本：`llm_call.created_at` 在 [`start`, `end`) 之間的費用，依 `derivation_run.task` 加總
      （含評估與 Batch）。`radar costs` 也用這一句。
    - 預留：`batch_item.estimated_max_cost_usd` 中 run 還是 `running` 的（不分月份）。結果匯入
      把 run 改掉就自然釋放（ADR-0041）。

    同一句讀，結果匯入（2.11）在同一個交易裡「記帳＋把 run 改掉」時，這裡不會兩邊都沒看到
    （分兩句讀時，剛好在兩句之間 commit 就會少算一份）。互動的 `BudgetGuard` 與 Batch 的規劃、
    送出都經 `services/budget.read_budget_status` 用這一句。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `start`、`end` 沒有時區，或 `start` 不早於 `end`。
    """
    visible = visible_to_system_only(viewer)
    for moment in (start, end):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("start、end 要帶時區（不能是 naive datetime）")
    if start >= end:
        raise ValueError("start 要早於 end")
    rows = connection.execute(
        text(
            "SELECT kind, task, sum(amount) FROM ("
            " SELECT 'spent' AS kind, r.task AS task, c.cost_usd AS amount"
            " FROM llm_call AS c JOIN derivation_run AS r ON r.id = c.run_id"
            f" WHERE {visible} AND c.created_at >= :start AND c.created_at < :end"
            " UNION ALL"
            " SELECT 'reserved', r.task, i.estimated_max_cost_usd"
            " FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
            f" WHERE {visible} AND r.status = 'running'"
            ") AS usage GROUP BY kind, task"
        ),
        {"start": start, "end": end},
    ).all()
    spent: dict[LlmTask, Decimal] = {}
    reserved: dict[LlmTask, Decimal] = {}
    for kind, task, amount in rows:
        (spent if kind == "spent" else reserved)[LlmTask(task)] = Decimal(amount)
    return spent, reserved


def list_batch_reservations_ahead_by_task(
    viewer: SystemViewer, connection: Connection, batch_submission_id: uuid.UUID
) -> tuple[dict[LlmTask, Decimal], dict[LlmTask, Decimal]]:
    """一句 SQL 讀：回（這批的預留, 排在它前面、還在 `queued` 的其他批次的預留），各依工作加總，
    沒有的工作不在結果裡（ADR-0041 決定 12「再等也放不下」）。

    - 預留只算 run 還是 `running` 的 `batch_item.estimated_max_cost_usd`（和
      `list_llm_spending_and_reservations_by_task` 的預留一樣）。
    - 「前面」：`(created_at, id)` 比這批小（先建的先排；同時建的比 id，順序固定、不會互相等）。
    - 這批不存在時兩個都是空的。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    rows = connection.execute(
        text(
            "SELECT r.task,"
            " COALESCE(sum(i.estimated_max_cost_usd) FILTER (WHERE s.id = me.id), 0),"
            " COALESCE(sum(i.estimated_max_cost_usd) FILTER (WHERE s.id <> me.id), 0)"
            " FROM batch_submission AS me"
            " JOIN batch_submission AS s ON s.id = me.id OR (s.status = 'queued'"
            " AND (s.created_at, s.id) < (me.created_at, me.id))"
            " JOIN batch_item AS i ON i.batch_submission_id = s.id"
            " JOIN derivation_run AS r ON r.id = i.run_id"
            f" WHERE {visible} AND me.id = :id AND r.status = 'running'"
            " GROUP BY r.task"
        ),
        {"id": batch_submission_id},
    ).all()
    own: dict[LlmTask, Decimal] = {}
    ahead: dict[LlmTask, Decimal] = {}
    for task, own_amount, ahead_amount in rows:
        if own_amount:
            own[LlmTask(task)] = Decimal(own_amount)
        if ahead_amount:
            ahead[LlmTask(task)] = Decimal(ahead_amount)
    return own, ahead


# --- 寫入 ---------------------------------------------------------------------------------


def create_batch_submission(connection: Connection, weekly_run_id: uuid.UUID | None) -> uuid.UUID:
    """新增一筆 `queued` 的 `batch_submission`（筆數與預留先是 0，寫完 item 再更新），回它的 id。"""
    value = connection.execute(
        text(
            "INSERT INTO batch_submission (weekly_run_id, status, item_count,"
            " estimated_max_cost_usd)"
            " VALUES (:weekly_run_id, 'queued', 0, 0) RETURNING id"
        ),
        {"weekly_run_id": weekly_run_id},
    ).scalar_one()
    return uuid.UUID(str(value))


def create_batch_item(
    connection: Connection,
    batch_submission_id: uuid.UUID,
    run_id: uuid.UUID,
    abstract_id: uuid.UUID | None,
    estimated_max_cost_usd: Decimal,
) -> uuid.UUID:
    """新增一筆 `batch_item`，回它的 id（`custom_id` 用它的 `.hex`）。"""
    value = connection.execute(
        text(
            "INSERT INTO batch_item (batch_submission_id, run_id, abstract_id,"
            " estimated_max_cost_usd)"
            " VALUES (:batch_submission_id, :run_id, :abstract_id, :cost) RETURNING id"
        ),
        {
            "batch_submission_id": batch_submission_id,
            "run_id": run_id,
            "abstract_id": abstract_id,
            "cost": estimated_max_cost_usd,
        },
    ).scalar_one()
    return uuid.UUID(str(value))


def update_batch_submission_totals(
    connection: Connection,
    batch_submission_id: uuid.UUID,
    item_count: int,
    estimated_max_cost_usd: Decimal,
) -> bool:
    """規劃寫完 item 後記下筆數與預留合計；只在 `queued` 時改，回有沒有改到。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET item_count = :item_count, estimated_max_cost_usd = :cost,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'queued'"
        ),
        {"id": batch_submission_id, "item_count": item_count, "cost": estimated_max_cost_usd},
    )
    return result.rowcount == 1


def update_batch_submission_submitting(
    connection: Connection, batch_submission_id: uuid.UUID, item_count: int
) -> bool:
    """`queued` → `submitting`，同一句把 `item_count` 改成實際要送的筆數；不是 `queued`
    （別人先改走）就不改、回 False。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'submitting', item_count = :item_count,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'queued'"
        ),
        {"id": batch_submission_id, "item_count": item_count},
    )
    return result.rowcount == 1


def update_batch_submission_submitted(
    connection: Connection, batch_submission_id: uuid.UUID, provider_batch_id: str
) -> bool:
    """`submitting`（還沒有 batch ID）→ `submitted`，記供應商 batch ID 與送出時間；已經不是（例如被
    sweep 標成 `needs_attention`）就不改、回 False。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'submitted', provider_batch_id = :batch_id,"
            " submitted_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'submitting' AND provider_batch_id IS NULL"
        ),
        {"id": batch_submission_id, "batch_id": provider_batch_id},
    )
    return result.rowcount == 1


def update_batch_submission_late_batch_id(
    connection: Connection, batch_submission_id: uuid.UUID, provider_batch_id: str
) -> bool:
    """已經標成 `needs_attention`、還沒有 batch ID 的批次，補記慢一步拿到的供應商 batch ID
    （狀態不變，給人工確認用）；不符就不改、回 False。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET provider_batch_id = :batch_id,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'needs_attention' AND provider_batch_id IS NULL"
        ),
        {"id": batch_submission_id, "batch_id": provider_batch_id},
    )
    return result.rowcount == 1


def update_batch_submission_requeued(
    connection: Connection, batch_submission_id: uuid.UUID
) -> bool:
    """`submitting`（還沒有 batch ID）→ `queued`（供應商回 429，一定沒有建立）；不符就不改。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'queued', updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'submitting' AND provider_batch_id IS NULL"
        ),
        {"id": batch_submission_id},
    )
    return result.rowcount == 1


def update_batch_submission_failed(
    connection: Connection,
    batch_submission_id: uuid.UUID,
    reason: ErrorCode,
    *,
    from_status: BatchSubmissionStatus,
) -> bool:
    """`from_status`（`queued` 或還沒有 batch ID 的 `submitting`）→ `failed`，記原因代碼；
    不符就不改。

    Raises:
        ValueError: `from_status` 不是 `queued` 或 `submitting`。
    """
    if from_status not in (BatchSubmissionStatus.QUEUED, BatchSubmissionStatus.SUBMITTING):
        raise ValueError("只有 queued 或 submitting 可以改成 failed")
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'failed', failure_reason = :reason,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = :from_status AND provider_batch_id IS NULL"
        ),
        {
            "id": batch_submission_id,
            "reason": ErrorCode(reason).value,
            "from_status": BatchSubmissionStatus(from_status).value,
        },
    )
    return result.rowcount == 1


def update_batch_submission_needs_attention(
    connection: Connection,
    batch_submission_id: uuid.UUID,
    reason: ErrorCode,
    *,
    stale_after: timedelta | None,
) -> bool:
    """還沒有 batch ID 的 `submitting` → `needs_attention`，記原因代碼；`stale_after` 有值時還要
    `updated_at` 早於「現在減 `stale_after`」（送出的工作已經不在）。不符就不改、回 False。

    Raises:
        ValueError: `stale_after` 不是正的時間長度。
    """
    if stale_after is not None and stale_after <= timedelta(0):
        raise ValueError("stale_after 要大於 0")
    stale = "" if stale_after is None else " AND updated_at < clock_timestamp() - :stale_after"
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'needs_attention', failure_reason = :reason,"
            " updated_at = clock_timestamp()"
            f" WHERE id = :id AND status = 'submitting' AND provider_batch_id IS NULL{stale}"
        ),
        {"id": batch_submission_id, "reason": ErrorCode(reason).value, "stale_after": stale_after},
    )
    return result.rowcount == 1


def update_batch_submission_ended(
    connection: Connection, batch_submission_id: uuid.UUID, provider_batch_id: str
) -> bool:
    """`submitted` → `ended`（供應商那邊處理完了），記 `ended_at`、`incomplete_reads` 歸零（人工
    確認後重新開始數）；不是 `submitted`、或供應商 batch ID 已經不是查到的那個就不改、回 False
    （Story 2.11）。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'ended', ended_at = clock_timestamp(),"
            " incomplete_reads = 0, updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'submitted' AND provider_batch_id = :batch_id"
        ),
        {"id": batch_submission_id, "batch_id": provider_batch_id},
    )
    return result.rowcount == 1


def update_batch_submission_imported(
    connection: Connection, batch_submission_id: uuid.UUID, provider_batch_id: str
) -> bool:
    """`ended` → `imported`，記 `imported_at`；只在供應商 batch ID 還是 `provider_batch_id`、而且
    這批已經沒有 `running` 的 run 時才改（同一句判斷），不符就不改、回 False（Story 2.11）。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'imported', imported_at = clock_timestamp(),"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'ended' AND provider_batch_id = :batch_id"
            " AND NOT EXISTS ("
            " SELECT 1 FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
            " WHERE i.batch_submission_id = batch_submission.id AND r.status = 'running')"
        ),
        {"id": batch_submission_id, "batch_id": provider_batch_id},
    )
    return result.rowcount == 1


def update_batch_submission_results_incomplete(
    connection: Connection,
    batch_submission_id: uuid.UUID,
    provider_batch_id: str,
    *,
    attention_after: int,
) -> tuple[BatchSubmissionStatus, int] | None:
    """這次讀到的結果項數比供應商的計數少：`incomplete_reads` 加一；加完到了 `attention_after` 就
    同一句 `ended` → `needs_attention`（`batch_results_incomplete`）。回（之後的狀態, 次數）；不是
    `ended`、或供應商 batch ID 已經不是 `provider_batch_id` 就不改、回 None（Story 2.11 確認審查
    D2）。

    Raises:
        ValueError: `attention_after` 不是至少 1 的整數。
    """
    if type(attention_after) is not int or attention_after < 1:
        raise ValueError("attention_after 要是至少 1 的整數")
    # SET 右邊的欄位都是改之前的值，所以三個 CASE 看的是同一個「加一之後」的次數。
    row = connection.execute(
        text(
            "UPDATE batch_submission SET incomplete_reads = incomplete_reads + 1,"
            " status = CASE WHEN incomplete_reads + 1 >= :limit THEN 'needs_attention'"
            " ELSE status END,"
            " failure_reason = CASE WHEN incomplete_reads + 1 >= :limit THEN :reason"
            " ELSE failure_reason END,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'ended' AND provider_batch_id = :batch_id"
            " RETURNING status, incomplete_reads"
        ),
        {
            "id": batch_submission_id,
            "batch_id": provider_batch_id,
            "limit": attention_after,
            "reason": ErrorCode.BATCH_RESULTS_INCOMPLETE.value,
        },
    ).one_or_none()
    if row is None:
        return None
    return BatchSubmissionStatus(row[0]), int(row[1])


def update_batch_submission_attention_on_not_found(
    connection: Connection,
    batch_submission_id: uuid.UUID,
    provider_batch_id: str,
    reason: ErrorCode,
) -> bool:
    """`submitted`／`ended` → `needs_attention`，記原因代碼（例如供應商回 404）；`ended` 是匯入到
    一半中斷、之後供應商找不到的情況。不是這兩種狀態、或供應商 batch ID 已經不是查的那個（人工確認
    換了 ID）就不改、回 False（Story 2.11）。供應商 batch ID 留著，人工確認時查得到。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'needs_attention', failure_reason = :reason,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status IN ('submitted', 'ended') AND provider_batch_id = :batch_id"
        ),
        {
            "id": batch_submission_id,
            "batch_id": provider_batch_id,
            "reason": ErrorCode(reason).value,
        },
    )
    return result.rowcount == 1


def update_batch_submission_resumed(
    connection: Connection, batch_submission_id: uuid.UUID, provider_batch_id: str
) -> bool:
    """`needs_attention` → `submitted`（`radar batches resolve --provider-batch-id`）：記人工查到的
    供應商 batch ID、清掉原因代碼、沒有送出時間的補上。這個 ID 已經是別批的（同一句判斷）或這批不是
    `needs_attention` 就不改、回 False（Story 2.11）。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'submitted', provider_batch_id = :batch_id,"
            " failure_reason = NULL, submitted_at = COALESCE(submitted_at, clock_timestamp()),"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'needs_attention' AND NOT EXISTS ("
            " SELECT 1 FROM batch_submission AS other"
            " WHERE other.provider_batch_id = :batch_id AND other.id <> :id)"
        ),
        {"id": batch_submission_id, "batch_id": provider_batch_id},
    )
    return result.rowcount == 1


def update_batch_submission_resolved_failed(
    connection: Connection, batch_submission_id: uuid.UUID
) -> bool:
    """`needs_attention` → `failed`（`radar batches resolve --fail`，原因
    `batch_resolved_failed`）；
    不是 `needs_attention` 就不改、回 False（Story 2.11）。"""
    result = connection.execute(
        text(
            "UPDATE batch_submission SET status = 'failed', failure_reason = :reason,"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'needs_attention'"
        ),
        {"id": batch_submission_id, "reason": ErrorCode.BATCH_RESOLVED_FAILED.value},
    )
    return result.rowcount == 1
