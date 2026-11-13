"""唯一讀環境變數的地方（AD-18）。只有入口與 `bootstrap.py` 可以 import 這個模組。"""

import os
from collections.abc import Mapping

from pydantic import ValidationError

from radar.domain.settings import Settings


class ConfigError(Exception):
    """設定缺漏或格式不對。訊息只列環境變數名稱與原因，不含任何設定值。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("設定錯誤：" + "；".join(problems))


def env_name(field: str) -> str:
    """設定欄位對應的環境變數名稱。"""
    if field == "database_url":
        return "DATABASE_URL"
    return f"RADAR_{field.upper()}"


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """從環境變數組出 `Settings`；空字串當作沒設定。

    Raises:
        ConfigError: 缺少必填的值或格式不對；訊息不含任何設定值。
    """
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for field in Settings.model_fields:
        raw = env.get(env_name(field), "")
        if raw != "":
            values[field] = raw
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        problems: list[str] = []
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            field = str(error["loc"][0]) if error["loc"] else "?"
            if error["type"] == "missing":
                problems.append(f"缺少環境變數 {env_name(field)}")
            else:
                problems.append(f"環境變數 {env_name(field)} 格式不對（{error['msg']}）")
        raise ConfigError(problems) from None
