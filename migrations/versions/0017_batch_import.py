"""Batch 結果匯入：防重複記帳的唯一索引與批次的時間欄（Story 2.11、AD-14、ADR-0004 決定第 4 點、
ADR-0043）。

- `llm_call`：新增部分唯一索引 `uq_llm_call_batch_run_id`（`run_id`，`WHERE is_batch`）。匯入用
  `ON CONFLICT (run_id) WHERE is_batch DO NOTHING` 記帳：兩個 poller 同時匯入、同一行結果匯入兩次、
  匯入到一半中斷再跑，同一筆 Batch run 都只記一次帳（0010 的 `uq_llm_call_run_id` 已經擋所有
  run；這個索引把「Batch 一筆 run 一筆帳」寫成匯入時明講的衝突目標，`run_id` 的約束之後改了
  也還在）。
- `batch_submission`：新增 `ended_at`（供應商那邊處理完、我們看到 `ended` 的時間）、`imported_at`
  （匯入做完的時間），都可以是 NULL；CHECK：`ended`／`imported` 一定有 `ended_at`，`imported`
  一定有 `imported_at`。從 0016 第一次升上來時，既有的列只會是 `queued`～`submitted`、`failed`、
  `needs_attention`（2.9 不會寫 `ended`／`imported`）；但降版到 0016 再升回來時，已經 `ended`／
  `imported` 的列兩欄是空的，會違反 CHECK。所以加 CHECK 前先回填：這兩種狀態的列，空的
  `ended_at`／`imported_at` 用 `updated_at`（最後一次狀態轉移的時間：`imported` 的列就是匯入做完
  的時間，`ended` 的列就是看到結束的時間；不會早於送出時間，是手上最接近的值；Story 2.11 審查後）。
- `batch_submission.incomplete_reads`（Story 2.11 確認審查 D2）：同一個供應商 batch ID 連續幾次讀到
  的結果項數比供應商的計數少；NOT NULL、預設 0、CHECK 不能是負的。到了設定的次數就把批次標
  `needs_attention`（`batch_results_incomplete`），交給人工確認。既有的列用預設值 0（還沒數過）。

和 `radar/repo/llm_ledger.py`、`radar/repo/llm_batches.py` 的表定義一致（`alembic check` 比對；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件）。可降版：降版刪掉三欄（連同裡面的時間與次數）
與索引。

版本：0017
上一版：0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_llm_call_batch_run_id",
        "llm_call",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("is_batch"),
    )
    op.add_column(
        "batch_submission", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "batch_submission", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "batch_submission",
        sa.Column("incomplete_reads", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        "ck_batch_submission_incomplete_reads_not_negative",
        "batch_submission",
        "incomplete_reads >= 0",
    )
    # 降版再升版時，已經 ended／imported 的列要先有時間才加得上 CHECK（見模組說明）。
    op.execute(
        "UPDATE batch_submission SET ended_at = updated_at"
        " WHERE status IN ('ended', 'imported') AND ended_at IS NULL"
    )
    op.execute(
        "UPDATE batch_submission SET imported_at = updated_at"
        " WHERE status = 'imported' AND imported_at IS NULL"
    )
    op.create_check_constraint(
        "ck_batch_submission_ended_at_once_ended",
        "batch_submission",
        "status NOT IN ('ended', 'imported') OR ended_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_batch_submission_imported_at_once_imported",
        "batch_submission",
        "status <> 'imported' OR imported_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_batch_submission_imported_at_once_imported", "batch_submission", type_="check"
    )
    op.drop_constraint("ck_batch_submission_ended_at_once_ended", "batch_submission", type_="check")
    op.drop_constraint(
        "ck_batch_submission_incomplete_reads_not_negative", "batch_submission", type_="check"
    )
    op.drop_column("batch_submission", "incomplete_reads")
    op.drop_column("batch_submission", "imported_at")
    op.drop_column("batch_submission", "ended_at")
    op.drop_index("uq_llm_call_batch_run_id", table_name="llm_call")
