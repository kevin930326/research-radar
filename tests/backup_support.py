"""備份與還原的測試共用（Story 3.18、ADR-0051）：自己編的測試資料、外部工具、臨時的 age 金鑰。

資料全是測試自己編的（`example.com` 的信箱、假標題與假摘要）。age 的金鑰在測試執行時用
`age-keygen` 產生在 pytest 的臨時資料夾，測試結束就跟著刪掉，不進 repo。
"""

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event
from radar.repo.sessions import create_session
from radar.services.members import allow_member
from tests.database import TestDatabase
from tests.integration.test_effective_views import (
    ZH_CONTENT,
    insert_classification,
    insert_header,
    insert_research_model,
)
from tests.llm_support import create_paper, execute

BACKUP_TOOLS = ("pg_dump", "pg_restore", "psql", "age", "age-keygen")
# CI 設這個環境變數：找不到工具時直接失敗，不跳過（本機沒有工具時才跳過）。
REQUIRE_TOOLS_ENV = "RADAR_REQUIRE_BACKUP_TOOLS"
EMBEDDING_MODEL = "@cf/qwen/qwen3-embedding-0.6b"


def require_backup_tools() -> None:
    """找不到 pg_dump／pg_restore／psql／age 時：CI 失敗、本機跳過。"""
    missing = [tool for tool in BACKUP_TOOLS if shutil.which(tool) is None]
    if not missing:
        return
    reason = f"找不到 {'、'.join(missing)}（備份與還原的整合測試要 PostgreSQL 18 的用戶端與 age）"
    if os.environ.get(REQUIRE_TOOLS_ENV):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@dataclass(frozen=True)
class AgeKey:
    """測試用的 age 金鑰：私鑰檔在 pytest 的臨時資料夾。"""

    identity: Path
    recipient: str

    def decrypt(self, data: bytes) -> bytes:
        result = subprocess.run(
            ["age", "--decrypt", "--identity", str(self.identity)],
            input=data,
            capture_output=True,
            check=True,
        )
        return result.stdout


def make_age_key(directory: Path) -> AgeKey:
    identity = directory / "test-identity.txt"
    subprocess.run(["age-keygen", "--output", str(identity)], capture_output=True, check=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)], capture_output=True, check=True, text=True
    ).stdout.strip()
    return AgeKey(identity=identity, recipient=recipient)


@dataclass(frozen=True)
class Seeded:
    """造好的資料：兩篇論文（只有第一篇在已發佈的快照裡、算數）、兩位成員（甲有文獻庫、session、
    修正分類；乙有一筆封存）。"""

    papers: tuple[uuid.UUID, uuid.UUID]
    published_snapshot: uuid.UUID
    member_a: uuid.UUID
    member_b: uuid.UUID


def seed_source(db: TestDatabase) -> Seeded:
    """在遷移好的 `db` 造備份要帶的各種資料：論文、三種 AI 產出、向量，以及成員資料。

    第一篇指到一個已發佈的快照（算數的論文 1 篇）；第二篇沒有快照，只在 `paper` 表裡（2 列）。
    """
    database = Database(db.app_url, connect_timeout=5, pool_size=2)
    first = create_paper(
        database,
        doi="10.5555/backup-one",
        openalex_id="W9000001",
        title="A made-up study of team backups",
        abstract="We invented this abstract for a backup test.",
    )
    second = create_paper(
        database,
        doi="10.5555/backup-two",
        openalex_id="W9000002",
        title="Another made-up study",
        abstract=None,
    )
    snapshot = publish_snapshot(db)
    count_in_snapshot(db, first, snapshot)
    insert_header(db, first, "zh_summary", ZH_CONTENT)
    insert_classification(db, first, ["erp"], ["survey_sem"])
    insert_research_model(db, first)
    _embedding(db, first)

    system = SystemViewer()
    member_a = allow_member(
        system, database, email="backup-a@example.com", display_name="備份測試甲"
    ).member_id
    member_b = allow_member(
        system, database, email="backup-b@example.com", display_name="備份測試乙"
    ).member_id
    with database.transaction() as connection:
        create_session(
            connection, member_id=member_a, token_sha256=b"\x01" * 32, now=datetime.now(UTC)
        )
        create_judgment_event(
            connection,
            classification_fix(
                member_id=member_a,
                actor_role=ActorRole.MEMBER,
                paper_id=first,
                field=TaxonomyField.TOPIC,
                keys=["erp"],
                reason=None,
            ),
        )
    execute(
        db,
        "INSERT INTO library_item (owner_id, paper_id, added_via) VALUES (%s, %s, 'manual')",
        member_a,
        first,
    )
    execute(
        db,
        "INSERT INTO deleted_record (source_table, source_id, content, owner_id,"
        " deleted_by_member_id) VALUES ('library_item', %s, %s::jsonb, %s, %s)",
        uuid.uuid4(),
        '{"paper_id": "' + str(second) + '", "added_via": "manual"}',
        member_b,
        member_b,
    )
    return Seeded(
        papers=(first, second), published_snapshot=snapshot, member_a=member_a, member_b=member_b
    )


def publish_snapshot(db: TestDatabase) -> uuid.UUID:
    """一個已發佈的快照（統計的「算數的論文」要指到已發佈的快照）。"""
    [(run_id,)] = execute(db, "INSERT INTO weekly_run (status) VALUES ('failed') RETURNING id")
    [(snapshot_id,)] = execute(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " VALUES (1, %s, 'published', %s) RETURNING id",
        run_id,
        datetime(2026, 11, 16, tzinfo=UTC),
    )
    return uuid.UUID(str(snapshot_id))


def count_in_snapshot(db: TestDatabase, paper_id: uuid.UUID, snapshot_id: uuid.UUID) -> None:
    """讓論文算數：`first_snapshot_id` 指到已發佈的快照（論文本身在追蹤範圍內、沒被排除）。"""
    execute(db, "UPDATE paper SET first_snapshot_id = %s WHERE id = %s", snapshot_id, paper_id)


def _embedding(db: TestDatabase, paper_id: uuid.UUID) -> None:
    """一筆遷移 0015 登記的 1,024 維向量。"""
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, call_mode, status, finished_at) VALUES (%s, 'embed', %s, %s, 'abstract',"
        " '{}'::jsonb, 'test', 'interactive', 'succeeded', now()) RETURNING id",
        paper_id,
        EMBEDDING_MODEL,
        uuid.uuid4().hex * 2,
    )
    execute(
        db,
        "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
        " run_id, abstract_source) SELECT 'paper', paper_id, model, 1024, CAST(%s AS halfvec),"
        " input_sha256, id, 'openalex' FROM derivation_run WHERE id = %s",
        "[" + ",".join(["0.5"] * 1024) + "]",
        run_id,
    )


def table_rows(db: TestDatabase, table: str) -> int:
    [(rows,)] = execute(db, f'SELECT count(*) FROM public."{table}"')
    return int(rows)
