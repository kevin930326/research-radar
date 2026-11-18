"""api 行程的進入點：單一 uvicorn 行程；收到 SIGTERM 做完手上的請求後以退出碼 0 結束。"""

import signal
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import FrameType

import structlog
import uvicorn
from fastapi import FastAPI

from radar.config import (
    ConfigError,
    database_pool_size,
    load_auth_settings,
    load_budget_config,
    load_context_values,
    load_domain_config,
    load_models_config,
    load_settings,
    load_taxonomy,
)
from radar.domain.contracts import build_client_config, build_display_names
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import AuthRoutes, build_auth_routes, install_auth_routes
from radar.entrypoints.api.library import (
    LibraryRoutes,
    build_library_routes,
    install_library_routes,
)
from radar.entrypoints.api.papers import PaperRoutes, build_paper_routes, install_paper_routes
from radar.entrypoints.api.web import mount_web
from radar.entrypoints.bootstrap import (
    MigrationsNotFoundError,
    build_fake_login_confirm,
    build_readiness_probes,
)
from radar.entrypoints.logging_config import configure_logging
from radar.services.health import check_readiness
from radar.services.paper_page import PaperPageSetup

log = structlog.get_logger(__name__)

# 成員版前端的建置結果：和 migrations/、config/ 一樣固定在工作目錄下（映像的 WORKDIR 是 /app），
# 不另設設定欄位。不存在時 mount_web 記一筆警告，api 照常只提供 API。
WEB_DIST = Path("web/dist")


def install_api_routes(
    app: FastAPI,
    *,
    auth_routes: AuthRoutes,
    paper_routes: PaperRoutes,
    library_routes: LibraryRoutes,
) -> None:
    """把全部 API 路由裝到 `app` 上：登入路由、論文頁路由（Story 3.6）、文獻庫路由（Story 3.16）。
    一定要在 `mount_web` 之前呼叫。

    正式組裝（下面的 `main`）與輸出 `openapi.json`（`openapi.py`）都只經這個函式裝路由，所以兩邊的
    端點清單一定相同；換人測試的登記表又和 `openapi.json` 比對，新的端點沒登記就失敗（ADR-0047）。
    之後的 Story 新增 router 時加在這裡，不要在 `main` 另外裝。
    """
    # 經模組層級的名稱呼叫（測試會把 install_auth_routes 換掉，確認 main() 走到組裝這一步）。
    install_auth_routes(app, auth_routes)
    install_paper_routes(app, paper_routes, auth_routes)
    install_library_routes(app, library_routes, auth_routes)


def _utc_now() -> datetime:
    """session 與論文頁（預算的台北月份）的時鐘。"""
    return datetime.now(UTC)


def main() -> int:
    configure_logging()
    try:
        # 領域設定（config/*.yaml）錯了也不啟動（AD-18）。
        domain_config = load_domain_config()
        settings = load_settings()
        # 論文頁要的設定（Story 3.6）：模型與分類表、情境值清單判定 AI 區塊的「目前這組鍵」，
        # 預算判定 `deferred_budget`，期刊與分類表也給顯示名稱。錯了一樣不啟動。
        taxonomy = load_taxonomy()
        paper_setup = PaperPageSetup(
            models=load_models_config(),
            budget=load_budget_config(),
            taxonomy=taxonomy,
            context_values=load_context_values(),
            journals=domain_config.journals,
        )
        # 登入設定的組合不合法（正式環境開假登入、假登入與 Google 登入同時開、試用環境缺 Access
        # 設定等）也不啟動（ADR-0046）。
        auth_settings = load_auth_settings()
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
    # 全部 API 路由只經 install_api_routes 裝（openapi.json 也用它，端點清單一致）；登入、論文頁與
    # 文獻庫路由用就緒檢查的同一個連線池。一定要在 mount_web 之前裝（install_*_routes 會檢查）。
    install_api_routes(
        app,
        auth_routes=build_auth_routes(
            probes.database, auth_settings, build_fake_login_confirm(auth_settings), _utc_now
        ),
        paper_routes=build_paper_routes(
            probes.database,
            paper_setup,
            build_display_names(paper_setup.journals, taxonomy),
            build_client_config(),
            _utc_now,
        ),
        library_routes=build_library_routes(probes.database, paper_setup.journals),
    )
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
