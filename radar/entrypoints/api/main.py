"""api 行程的進入點：單一 uvicorn 行程；收到 SIGTERM 做完手上的請求後以退出碼 0 結束。"""

import signal
from functools import partial
from pathlib import Path
from types import FrameType

import structlog
import uvicorn

from radar.config import ConfigError, database_pool_size, load_domain_config, load_settings
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.web import mount_web
from radar.entrypoints.bootstrap import MigrationsNotFoundError, build_readiness_probes
from radar.entrypoints.logging_config import configure_logging
from radar.services.health import check_readiness

log = structlog.get_logger(__name__)

# 成員版前端的建置結果：和 migrations/、config/ 一樣固定在工作目錄下（映像的 WORKDIR 是 /app），
# 不另設設定欄位。不存在時 mount_web 記一筆警告，api 照常只提供 API。
WEB_DIST = Path("web/dist")


def main() -> int:
    configure_logging()
    try:
        # 領域設定（config/*.yaml）錯了也不啟動（AD-18）。
        load_domain_config()
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法啟動", service="api", problems=exc.problems)
        return 1

    try:
        probes = build_readiness_probes(settings, pool_size=database_pool_size(settings, "api"))
    except MigrationsNotFoundError:
        # /readyz 要拿資料庫的版本和程式帶的遷移檔比對；找不到就無法判斷，不啟動。
        log.error("找不到遷移檔（工作目錄下的 migrations/），無法啟動", service="api")
        return 1

    app = create_app(readiness_check=partial(check_readiness, probes.ping, probes.schema_state))
    # 頁面殼另外掛、不經 create_app 的 web_dist 參數：關機的整合測試會把 create_app 換成只收
    # readiness_check 的版本、再加自己的路由；分開掛，那些路由才排在「其餘路徑」之前。
    mount_web(app, WEB_DIST)

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
