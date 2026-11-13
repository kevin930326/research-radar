"""SQLAlchemy 模型的共同起點。`alembic check` 拿 `Base.metadata` 和資料庫比對（AD-18）。

業務資料表由之後的 Story 加進來；命名慣例讓約束與索引的名稱固定，遷移檔才寫得出確定的名字。
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
