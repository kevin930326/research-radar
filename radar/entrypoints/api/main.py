"""api 行程的進入點：單一 uvicorn 行程；收到 SIGTERM 做完手上的請求後以退出碼 0 結束。"""

import signal
from functools import partial
from types import FrameType

import structlog
import uvicorn

from radar.config import ConfigError, load_settings
from radar.entrypoints.api.app import create_app
from radar.entrypoints.bootstrap import build_database_ping
from radar.entrypoints.logging_config import configure_logging
from radar.services.health import check_readiness

log = structlog.get_logger(__name__)


def main() -> int:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法啟動", service="api", problems=exc.problems)
        return 1

    app = create_app(readiness_check=partial(check_readiness, build_database_ping(settings)))

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            workers=1,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=settings.api_shutdown_graceful_timeout,
        )
    )

    def request_exit(_signum: int, _frame: FrameType | None) -> None:
        """在 uvicorn 接手訊號前收到 SIGTERM，也照樣請 uvicorn 收工。

        uvicorn 關完後會把收到的訊號再丟一次，會落在這裡：此時伺服器已經停了，
        設旗標沒有作用，行程也不會被訊號殺掉（退出碼 143）。
        """
        server.should_exit = True

    # 必須在 uvicorn 啟動前裝好：uvicorn 結束時會還原成這裡的 handler。
    signal.signal(signal.SIGTERM, request_exit)
    signal.signal(signal.SIGINT, request_exit)

    log.info("api 啟動", service="api", port=settings.api_port)
    try:
        server.run()
    except SystemExit:
        # uvicorn 啟動失敗（例如連接埠被占用）時會呼叫 sys.exit。
        log.error("api 沒有成功啟動", service="api")
        return 1
    log.info("正常關機", service="api")
    return 0
