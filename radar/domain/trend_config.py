"""`config/trend.yaml`：趨勢與覆蓋率的規則用到的設定值（ADR-0011、ADR-0062）。

讀進來之後由 services 和追蹤起始年（`journals.yaml` 的 `start_year`）、時區
（`domain/snapshot.DATA_TIMEZONE`）一起組成回應裡的 `params`（`domain/contracts.TrendParams`）；
`repo` 不碰這份設定（AD-1）。
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

_MONTH_DAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
# 每個月最多幾天（2 月算 29 天：分界日只比月與日，不需要年份）。
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

MonthDay = Annotated[str, StringConstraints(pattern=_MONTH_DAY.pattern)]


def check_month_day(value: str) -> str:
    """`MM-DD` 要是一年裡存在的日期（2 月 29 日算存在）。"""
    month, day = (int(part) for part in value.split("-"))
    if day > _DAYS_IN_MONTH[month - 1]:
        raise ValueError(f"{value} 不是一年裡存在的日期")
    return value


class TrendConfig(BaseModel):
    """趨勢與覆蓋率的規則用到的設定值（ADR-0011「規則用到的設定值由回應帶給前端」）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 當年度納入「成長最快」的分界日（月-日，含當天）。
    current_year_cutoff: MonthDay
    # 成長最快每組幾年。
    growth_window_years: int = Field(ge=1, strict=True)
    # 原始篇數的對照值取最早幾個完整年度。
    baseline_years: int = Field(ge=1, strict=True)
    # 摘要覆蓋率低於這個比例時標成偏低（0 到 1）。
    low_coverage_threshold: float = Field(ge=0, le=1)

    @field_validator("current_year_cutoff")
    @classmethod
    def _real_date(cls, value: str) -> str:
        return check_month_day(value)
