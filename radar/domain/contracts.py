"""跨邊界共用的資料形狀（AR-24、AD-21「共用的形狀只有一個模型」）。

這裡的模型是成員版 API、示範站的 `cube.json`、測試資料檔（`tests/golden/`）共用的唯一形狀；
之後的 Story 要用同一種資料時 import 這裡的模型，不另訂一份。每個模型由第一個用到它的 Story
定義（計數方塊的列：Story 1.10）。
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class CubeDim(StrEnum):
    """計數方塊的一列是哪一種計數（AD-19、ADR-0011「計數方塊」）。剛好這六種。"""

    TOTAL = "total"  # 每個（期刊, 年份）算數的論文篇數
    WITH_ABSTRACT = "with_abstract"  # 其中有目前的摘要的篇數
    PENDING_CLASSIFICATION = "pending_classification"  # 其中還沒分類的篇數
    TOPIC = "topic"  # 每個（期刊, 年份, 主題）的篇數
    TOPIC_PAIR = "topic_pair"  # 每個（期刊, 年份, 主題 A, 主題 B）的篇數（A < B）
    METHOD = "method"  # 每個（期刊, 年份, 方法）的篇數


# 不帶 key 的三種；`topic`、`method` 只有 `key_a`；`topic_pair` 兩個都有。
_NO_KEYS = frozenset({CubeDim.TOTAL, CubeDim.WITH_ABSTRACT, CubeDim.PENDING_CLASSIFICATION})
_KEY_A_ONLY = frozenset({CubeDim.TOPIC, CubeDim.METHOD})

_Key = Annotated[str, StringConstraints(min_length=1)]


class CubeRow(BaseModel):
    """計數方塊的一列（一格）：（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`）。

    - `journal` 是設定檔的期刊代號（不是 ISSN-L）。
    - 主題、方法用設定檔的代號，未分類是 `unclassified`；`topic_pair` 把字母順序在前的放
      `key_a`（ADR-0011）。
    - 只給計數，不給比例：比例由前端的 `web/src/stats/` 用這些格子算。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    journal: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    year: int
    dim: CubeDim
    key_a: _Key | None = None
    key_b: _Key | None = None
    n: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        if self.dim in _NO_KEYS:
            if self.key_a is not None or self.key_b is not None:
                raise ValueError(f"{self.dim} 不帶 key_a、key_b")
        elif self.dim in _KEY_A_ONLY:
            if self.key_a is None or self.key_b is not None:
                raise ValueError(f"{self.dim} 要有 key_a、不能有 key_b")
        else:  # topic_pair
            if self.key_a is None or self.key_b is None:
                raise ValueError("topic_pair 要有 key_a 與 key_b")
            if not self.key_a < self.key_b:
                raise ValueError("topic_pair 的 key_a 要排在 key_b 前面（字母順序、不能相同）")
        return self
