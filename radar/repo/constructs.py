"""構面表 `construct`：表的定義與唯一的寫入者（ADR-0012、AD-16、Story 2.6、ADR-0034）。

- 以 `normalized_name`（`domain/constructs.normalize_name` 的結果）為唯一鍵：正規化後相同的名稱
  從一開始就是同一個構面。
- `canonical_id` 不可為空：新構面建立時就指向自己（ADR-0012 第 3 點）；合併後由 Epic 10 的
  `rebuild_canonical` 重算（不在這個 Story）。
- 寫入只經 `upsert_constructs`（`repo/ai_outputs.create_ai_result` 寫研究模型時呼叫），一律依名稱
  排序、`ON CONFLICT DO NOTHING`：兩筆擷取同時寫同一批構面時，上鎖的順序一樣，不會互相等待成死結。
- 構面合併、代表的挑選規則、構面標籤的挑選函式都不在這裡（Epic 10；ADR-0034 只提案）。
"""

import uuid
from collections.abc import Iterable
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    bindparam,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY

from radar.repo.models import Base

construct_table = Table(
    "construct",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("normalized_name", Text, nullable=False),
    Column("canonical_id", Uuid, ForeignKey("construct.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("normalized_name"),
    # canonical_id 外鍵的索引（依代表找整組）。
    Index("ix_construct_canonical_id", "canonical_id"),
    CheckConstraint("normalized_name ~ '[^[:space:]]'", name="normalized_name_not_blank"),
)

# 新構面的 id 先在 CTE 裡產生，canonical_id 指向同一個 id；依名稱排序後新增，已經有的跳過。
_INSERT: Final = text(
    "WITH new AS (SELECT uuidv7() AS id, name FROM unnest(:names) AS name)"
    " INSERT INTO construct (id, normalized_name, canonical_id)"
    " SELECT id, name, id FROM new ORDER BY name"
    " ON CONFLICT (normalized_name) DO NOTHING"
).bindparams(bindparam("names", type_=ARRAY(Text)))
_SELECT_IDS: Final = text(
    "SELECT normalized_name, id FROM construct WHERE normalized_name = ANY(:names)"
).bindparams(bindparam("names", type_=ARRAY(Text)))


def upsert_constructs(connection: Connection, names: Iterable[str]) -> dict[str, uuid.UUID]:
    """確保每個正規化名稱都有一列構面（沒有的新增，`canonical_id` 指向自己），回名稱 → id。

    在呼叫端開好的交易裡做，不 commit。已經有的構面不改（不動 `canonical_id`）。

    Raises:
        ValueError: 名稱不是字串或只有空白（應該先經 `normalize_name`）。
    """
    given = list(names)
    for name in given:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("構面名稱要是 normalize_name 過、不是空白的字串")
    unique = sorted(set(given))
    if not unique:
        return {}
    connection.execute(_INSERT, {"names": unique})
    rows = connection.execute(_SELECT_IDS, {"names": unique}).all()
    found = {str(row[0]): uuid.UUID(str(row[1])) for row in rows}
    missing = [name for name in unique if name not in found]
    if missing:  # ON CONFLICT 之後一定查得到（別的交易剛新增的也已經 commit）
        raise RuntimeError("新增構面之後查不到")
    return found
