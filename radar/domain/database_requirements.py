"""資料庫要具備的功能（ADR-0003 決定第 3 點、AD-17）。

第一個遷移只「檢查」這些功能，不建立：擴充套件由管理者在建資料庫時建立
（本機與 CI 是 `deploy/db-init/` 的初始化腳本）。
"""

import re
from dataclasses import dataclass

# 應用程式角色：api、worker 與一般 CLI 指令用（`DATABASE_URL`）。
APP_ROLE = "radar_app"
# 擁有者角色：只給 `radar migrate`（`RADAR_MIGRATION_DATABASE_URL`）。
OWNER_ROLE = "radar_owner"
MIN_PGVECTOR_VERSION = (0, 8, 0)

_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+){0,2}")


@dataclass(frozen=True)
class DatabaseFeatures:
    """從資料庫讀到的功能。`pgvector_version` 是 `vector` 擴充套件的版本字串，沒裝是 None。

    `runs_as_database_owner`：執行遷移的角色是目前資料庫的擁有者，而且不是超級使用者。
    用別的帳號跑的話，預設權限會綁到錯的角色、之後建的表擁有者也錯，`radar_app` 拿不到權限。
    """

    has_uuidv7: bool
    pgvector_version: str | None
    has_pg_trgm: bool
    has_app_role: bool
    runs_as_database_owner: bool


class DatabaseRequirementsError(Exception):
    """資料庫缺少需要的功能；`problems` 是每一條原因（不含連線字串）。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("資料庫不符合需求：" + "；".join(problems))


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """`0.8.7` → (0, 8, 7)；`0.8` → (0, 8, 0)；看不懂的回 None。"""
    if not _VERSION_PATTERN.fullmatch(text):
        return None
    parts = [int(part) for part in text.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def find_missing_features(features: DatabaseFeatures) -> list[str]:
    """回傳每一條不符合的原因；全部符合時回空清單。"""
    problems: list[str] = []
    if not features.has_uuidv7:
        problems.append("沒有 uuidv7() 函式：需要 PostgreSQL 18 以上")
    minimum = _format_version(MIN_PGVECTOR_VERSION)
    if features.pgvector_version is None:
        problems.append(
            f"沒有安裝 pgvector（vector 擴充套件，需要 {minimum} 以上）："
            "請管理者在這個資料庫執行 CREATE EXTENSION vector"
        )
    else:
        version = _parse_version(features.pgvector_version)
        if version is None:
            shown = features.pgvector_version[:40]
            problems.append(f"無法判讀 pgvector 的版本「{shown}」（需要 {minimum} 以上）")
        elif version < MIN_PGVECTOR_VERSION:
            problems.append(f"pgvector 版本 {features.pgvector_version} 低於 {minimum}")
    if not features.has_pg_trgm:
        problems.append("沒有啟用 pg_trgm：請管理者在這個資料庫執行 CREATE EXTENSION pg_trgm")
    if not features.has_app_role:
        problems.append(f"沒有應用程式角色 {APP_ROLE}：請管理者先建立這個角色")
    if not features.runs_as_database_owner:
        problems.append(
            "執行遷移的不是資料庫的擁有者：RADAR_MIGRATION_DATABASE_URL"
            f" 要用擁有者角色 {OWNER_ROLE}，不能用超級使用者或其他帳號"
        )
    return problems
