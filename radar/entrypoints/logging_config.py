"""日誌設定：structlog 輸出 JSON 到 stdout，中文不跳脫（NFR-15、Conventions「日誌」）。

標準函式庫 logging 的紀錄（例如 uvicorn 自己的訊息）也走同一個 handler，一樣是 JSON。
"""

import logging
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger


def _ensure_request_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """每筆日誌固定有 `request_id` 欄位；不在請求裡的紀錄是 null。"""
    event_dict.setdefault("request_id", None)
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    """設定 structlog 與標準函式庫 logging。可以重複呼叫（測試會用到）。"""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _ensure_request_id,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # 不快取：測試會替換設定，快取住的 logger 會沿用舊設定。
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 自己會掛 handler；清掉讓它往上交給 root，統一輸出格式。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
