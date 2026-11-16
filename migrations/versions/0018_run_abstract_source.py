"""`derivation_run` 記下送出時用的摘要來源（Story 2.12、FR-11、AD-26、ADR-0044）。

- 新增 `abstract_source text NULL`：這次呼叫送出的摘要是哪個來源（`openalex`／`semantic_scholar`
  ／`uploaded_pdf`）；只看標題的 run 是 NULL。`radar runs remove-by-source` 照它找出要刪的表頭：
  `paper_abstract` 在來源拿掉摘要時整列刪掉（表頭的 `abstract_id` 就接不回來源了），run 自己記著
  才查得到。
- CHECK：值只能是那三個來源；向量化（`task = 'embed'`）先不記（ADR-0044）。
- 回填：表頭（三張）與 `batch_item` 記的 `abstract_id` 還接得到 `paper_abstract` 的 run，補上那一筆
  摘要的來源；接不到的（摘要列已刪）留 NULL，`remove-by-source` 印成「來源不明」。只補還是 NULL
  的列，做兩次結果一樣。
- 回填在途的 run（審查後第 5 點）：還沒有表頭、也沒有 `batch_item` 的 `running`／
  `llm_result_pending`（看摘要、不是向量化），用論文現在的標題與目前摘要、run 記的設定版本重算
  輸入指紋（`domain/fingerprints.input_sha256`，指紋算進每段的來源），和 run 的一樣就補上目前摘要
  的來源；不一樣（輸入已經變了，這些 run 也不會再被接續）留 NULL。
- `embedding.abstract_source text NULL`（審查後第 2 點）：向量化當時用的摘要來源（只看標題的是
  NULL），`remove-by-source` 照它刪向量。CHECK 同上三個值。回填（審查後第二輪 R2）：只在**能
  證明**時補，證明要同時成立：
  1. 論文現在的目前摘要那一列，`updated_at` 不晚於向量化 run 的 `created_at`（取得資格的交易）：
     那一列的內容與「是目前的」狀態一改就會更新 `updated_at`，所以向量化當時讀到的目前摘要就是
     它、內容也一樣；
  2. 用現在的標題與這一列算出的向量輸入指紋（`embedding_input_sha256`）等於向量記的。
  其他（目前摘要在向量之後換過來源或改過內容、文字不同、論文已經沒有目前摘要）一律留 NULL，
  不拿目前來源去猜。NULL 的看摘要向量：文字變了的照 2.8 的指紋規則在下一次 `embed_papers`
  （每週更新後排入，或 `radar embeddings backfill`）重算；文字沒變的不會被指紋規則挑到，改由
  `remove-by-source` 保守處理：移除任何一個來源時，連同 NULL 的看摘要向量一起刪掉，下一次
  `embed_papers` 照現在的輸入重算並記下來源。限制：時間戳記是交易開始的時間，極端的長交易交錯
  （更新摘要的交易比向量化的交易早開始、晚改目前摘要，而且換成文字完全相同的另一個來源）不在
  證明範圍內；遷移前只有 OpenAlex 會寫摘要（ADR-0022），實際上不會發生。

和 `radar/repo/llm_ledger.py`、`radar/repo/embeddings.py` 的表定義一致（`alembic check` 比對；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件）。可降版：降版刪掉這兩欄（連同記下的來源）。

版本：0018
上一版：0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from radar.domain.ai_outputs import (
    CurrentAbstract,
    InputKind,
    LlmTask,
    NoLlmInputError,
    PaperLlmSource,
    build_llm_input,
)
from radar.domain.embeddings import NoEmbeddingInputError, build_embedding_input
from radar.domain.fingerprints import embedding_input_sha256, input_sha256
from radar.domain.papers import AbstractSource

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 回填：表頭與 batch_item 記的 abstract_id → 那一筆摘要的來源。只補 NULL 的列（重跑結果一樣）。
BACKFILL = (
    "UPDATE derivation_run AS r SET abstract_source = a.source"
    " FROM ("
    " SELECT run_id, abstract_id FROM zh_summary WHERE abstract_id IS NOT NULL"
    " UNION ALL SELECT run_id, abstract_id FROM classification WHERE abstract_id IS NOT NULL"
    " UNION ALL SELECT run_id, abstract_id FROM research_model WHERE abstract_id IS NOT NULL"
    " UNION ALL SELECT run_id, abstract_id FROM batch_item WHERE abstract_id IS NOT NULL"
    ") AS used JOIN paper_abstract AS a ON a.id = used.abstract_id"
    " WHERE r.id = used.run_id AND r.abstract_source IS NULL AND r.task <> 'embed'"
    " AND r.input_kind = 'abstract'"
)


# 在途、還沒有表頭也沒有 batch_item、看摘要的 run，連同論文現在的標題與目前摘要。
IN_FLIGHT = (
    "SELECT r.id, r.paper_id, r.task, r.input_sha256, r.taxonomy_version,"
    " r.context_values_version, p.title, a.id AS abstract_id, a.source, a.content"
    " FROM derivation_run AS r JOIN paper AS p ON p.id = r.paper_id"
    " JOIN paper_abstract AS a ON a.paper_id = r.paper_id AND a.is_current"
    " WHERE r.abstract_source IS NULL AND r.task <> 'embed' AND r.input_kind = 'abstract'"
    " AND (r.status = 'running'"
    " OR (r.status = 'failed' AND r.failure_reason = 'llm_result_pending'))"
    " AND NOT EXISTS (SELECT 1 FROM zh_summary AS h WHERE h.run_id = r.id)"
    " AND NOT EXISTS (SELECT 1 FROM classification AS h WHERE h.run_id = r.id)"
    " AND NOT EXISTS (SELECT 1 FROM research_model AS h WHERE h.run_id = r.id)"
    " AND NOT EXISTS (SELECT 1 FROM batch_item AS b WHERE b.run_id = r.id)"
)

# 向量：run 看摘要、還沒記來源，而且論文現在的目前摘要從向量化之前就沒改過（見模組說明）。
EMBEDDINGS_TO_PROVE = (
    "SELECT e.item_type, e.item_id, e.model_id, e.input_sha256, p.title, a.id AS abstract_id,"
    " a.source, a.content"
    " FROM embedding AS e JOIN derivation_run AS r ON r.id = e.run_id"
    " JOIN paper AS p ON p.id = e.item_id"
    " JOIN paper_abstract AS a ON a.paper_id = e.item_id AND a.is_current"
    " WHERE e.item_type = 'paper' AND r.input_kind = 'abstract' AND e.abstract_source IS NULL"
    " AND a.updated_at <= r.created_at"
)


def backfill_in_flight(connection: sa.Connection) -> int:
    """在途的 run 用輸入指紋辨識摘要來源（見模組說明），回補了幾筆。只補還是 NULL 的列。"""
    filled = 0
    for row in connection.execute(sa.text(IN_FLIGHT)).mappings().all():
        source = AbstractSource(row["source"])
        paper = PaperLlmSource(
            paper_id=row["paper_id"],
            title=row["title"],
            abstract=CurrentAbstract(id=row["abstract_id"], source=source, content=row["content"]),
        )
        try:
            current = build_llm_input(paper, InputKind.ABSTRACT)
            fingerprint = input_sha256(
                current.blocks,
                LlmTask(row["task"]),
                taxonomy_version=row["taxonomy_version"],
                context_values_version=row["context_values_version"],
            )
        except NoLlmInputError, ValueError:
            continue
        if fingerprint != row["input_sha256"]:
            continue
        filled += connection.execute(
            sa.text(
                "UPDATE derivation_run SET abstract_source = :source"
                " WHERE id = :id AND abstract_source IS NULL"
            ),
            {"id": row["id"], "source": source.value},
        ).rowcount
    return filled


def backfill_embeddings(connection: sa.Connection) -> int:
    """能證明來源的看摘要向量補上來源（見模組說明），回補了幾筆。只補還是 NULL 的列。"""
    filled = 0
    for row in connection.execute(sa.text(EMBEDDINGS_TO_PROVE)).mappings().all():
        source = AbstractSource(row["source"])
        paper = PaperLlmSource(
            paper_id=row["item_id"],
            title=row["title"],
            abstract=CurrentAbstract(id=row["abstract_id"], source=source, content=row["content"]),
        )
        try:
            current = build_embedding_input(paper, InputKind.ABSTRACT)
            fingerprint = embedding_input_sha256(current.text, InputKind.ABSTRACT.value)
        except NoEmbeddingInputError, ValueError:
            continue
        if fingerprint != row["input_sha256"]:
            continue
        filled += connection.execute(
            sa.text(
                "UPDATE embedding SET abstract_source = :source"
                " WHERE item_type = :item_type AND item_id = :item_id AND model_id = :model_id"
                " AND abstract_source IS NULL"
            ),
            {
                "source": source.value,
                "item_type": row["item_type"],
                "item_id": row["item_id"],
                "model_id": row["model_id"],
            },
        ).rowcount
    return filled


def upgrade() -> None:
    op.add_column("derivation_run", sa.Column("abstract_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_derivation_run_abstract_source",
        "derivation_run",
        "abstract_source IS NULL"
        " OR abstract_source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
    )
    op.create_check_constraint(
        "ck_derivation_run_abstract_source_not_embed",
        "derivation_run",
        "abstract_source IS NULL OR task <> 'embed'",
    )
    op.execute(BACKFILL)
    backfill_in_flight(op.get_bind())
    op.add_column("embedding", sa.Column("abstract_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_embedding_abstract_source",
        "embedding",
        "abstract_source IS NULL"
        " OR abstract_source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
    )
    backfill_embeddings(op.get_bind())


def downgrade() -> None:
    op.drop_constraint("ck_embedding_abstract_source", "embedding", type_="check")
    op.drop_column("embedding", "abstract_source")
    op.drop_constraint(
        "ck_derivation_run_abstract_source_not_embed", "derivation_run", type_="check"
    )
    op.drop_constraint("ck_derivation_run_abstract_source", "derivation_run", type_="check")
    op.drop_column("derivation_run", "abstract_source")
