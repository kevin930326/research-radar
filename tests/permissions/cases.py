"""每個 API 端點的登記：送哪一份請求，以及（如果豁免換人測試）為什麼（Story 3.4、ADR-0047）。

**新增端點的 Story 要在這裡加一筆**，否則 `tests/unit/test_permission_coverage.py` 失敗：

- 會讀或改「某位成員的資料」的端點（例如 `GET /api/v1/library`、看得到私人論文的
  `GET /api/v1/papers/{paper_id}`）：不豁免。登記的函式先替成員 A 建好資料（整合測試的
  `ctx.extras["database"]` 是 `Database`），再回傳**一份**請求（`EndpointRequest`：路徑參數、查詢、
  本文）。框架用這一份請求，先以成員 B 的身分送（要得到 404 或空清單），再以成員 A 的身分送（對照，
  要拿得到），兩次都要打到登記的這個端點。能放文字的私人資料，請把 `CANARY` 放進去，金絲雀測試才
  查得到。
- 不碰任何成員資料、或只回呼叫者自己的資料：寫 `exempt="理由"` 與 `status=成員 B 打它的預期狀態碼`；
  登記的函式照樣要寫（金絲雀測試用，狀態碼不是預期的就失敗）。
"""

import uuid

from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.library import AddedVia
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.judgments import create_judgment_event
from radar.repo.library import create_library_item
from radar.services.library import remove_library_item
from tests.integration.test_effective_views import insert_classification, insert_header
from tests.llm_support import create_hidden_paper, execute
from tests.permissions.framework import (
    CANARY,
    EndpointRegistry,
    EndpointRequest,
    PermissionContext,
)

REGISTRY = EndpointRegistry()


@REGISTRY.case("GET", "/healthz", exempt="不需要登入，只回行程活著；不讀任何成員資料", status=200)
def _healthz(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/readyz",
    exempt="不需要登入，只回資料庫連得上、遷移不落後；不讀成員資料",
    status=200,
)
def _readyz(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/api/v1/me",
    exempt="只回呼叫者自己的成員 ID 與名稱，不收別人的 ID，沒有別人的資料可拿",
    status=200,
)
def _me(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "POST",
    "/api/v1/auth/logout",
    exempt="只刪呼叫者自己 cookie 對到的 session，不收成員 ID 或 session ID",
    status=204,
)
def _logout(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/api/v1/auth/fake-login",
    exempt=(
        "假登入的選人清單：開發環境依設計列出全部測試成員的名稱（不含 email），試用環境是空清單，"
        "正式環境沒有這個端點（ADR-0046）"
    ),
    status=200,
)
def _fake_login_options(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "POST",
    "/api/v1/auth/fake-login",
    exempt=(
        "登入本身：開發環境選人登入就是它的用途，試用環境只讓管理者登入，正式環境沒有這個端點"
        "（ADR-0046）；回應只有轉回路徑"
    ),
    status=200,
)
def _fake_login(ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest(body={"member_id": ctx.other.member_id})


# --- 論文頁（Story 3.6、ADR-0048）---------------------------------------------------------------


def _private_paper_in_owner_library(ctx: PermissionContext) -> uuid.UUID:
    """成員 A 的私人論文（成員看不到：成員匯入、不在追蹤範圍），標題與摘要放金絲雀，以
    `added_via = upload` 放進 A 的文獻庫（成員自己加不了看不到的論文，所以直接呼叫 repo 的寫入
    函式、用系統身分）。A 看得到它（在自己的文獻庫），B 看不到。"""
    database = ctx.extras["database"]
    paper_id = create_hidden_paper(
        database,
        doi="10.9/canary-hidden",
        openalex_id="W987654321",
        title=f"Hidden paper {CANARY}",
        abstract=f"An abstract that only one member may read: {CANARY}.",
    )
    with database.transaction() as connection:
        item = create_library_item(
            connection,
            SystemViewer(),
            owner_id=uuid.UUID(ctx.owner.member_id),
            paper_id=paper_id,
            added_via=AddedVia.UPLOAD,
        )
    assert item is not None
    return paper_id


@REGISTRY.case("GET", "/api/v1/papers/{paper_id}")
def _paper(ctx: PermissionContext) -> EndpointRequest:
    # Story 3.16 接上文獻庫後不再豁免：A 的私人論文在 A 的文獻庫，A 看得到、B 和不存在一樣 404。
    paper_id = _private_paper_in_owner_library(ctx)
    return EndpointRequest(path_params={"paper_id": str(paper_id)})


# --- 修正分類（Story 3.11）---------------------------------------------------------------------


@REGISTRY.case("GET", "/api/v1/papers/{paper_id}/classification-fixes")
def _classification_fixes(ctx: PermissionContext) -> EndpointRequest:
    # A 的私人論文在 A 的文獻庫，A 修正過一次（理由有金絲雀）：A 讀得到歷史；B 看不到這篇，和不存在
    # 一樣 404（歷史、理由、姓名都不回）。
    paper_id = _private_paper_in_owner_library(ctx)
    with ctx.extras["database"].transaction() as connection:
        for field, keys in ((TaxonomyField.TOPIC, ("e_commerce",)), (TaxonomyField.METHOD, ())):
            create_judgment_event(
                connection,
                classification_fix(
                    member_id=uuid.UUID(ctx.owner.member_id),
                    actor_role=ActorRole.MEMBER,
                    paper_id=paper_id,
                    field=field,
                    keys=keys,
                    reason=f"修正理由 {CANARY}",
                ),
            )
    return EndpointRequest(path_params={"paper_id": str(paper_id)})


@REGISTRY.case("POST", "/api/v1/papers/{paper_id}/classification-fixes")
def _fix_classification(ctx: PermissionContext) -> EndpointRequest:
    # B 修正 A 的私人論文（B 看不到）：和不存在一樣 404，什麼都不寫；A 修正自己看得到的這篇：200。
    # 這篇要有成功的 AI 分類（沒有時先不收修正，ADR-0063），方法和送出的不同（相同會被當成沒有改）。
    paper_id = _private_paper_in_owner_library(ctx)
    insert_classification(ctx.extras["test_database"], paper_id, ["e_commerce"], ["survey_sem"])
    return EndpointRequest(
        path_params={"paper_id": str(paper_id)},
        body={"topic": ["e_commerce"], "method": ["experiment"], "reason": None},
    )


# --- 回報中文重點錯誤（Story 3.12）------------------------------------------------------------


@REGISTRY.case("POST", "/api/v1/papers/{paper_id}/summary-reports")
def _report_summary(ctx: PermissionContext) -> EndpointRequest:
    # B 回報 A 的私人論文（B 看不到）：和不存在一樣 404，什麼都不寫；A 回報自己看得到的這篇：200。
    # 這篇要有有效的中文重點（沒有時回報不收）；本文帶畫面上看到的那一筆（ADR-0064）。
    paper_id = _private_paper_in_owner_library(ctx)
    summary_id = insert_header(
        ctx.extras["test_database"],
        paper_id,
        "zh_summary",
        {
            "research_question": "（假資料）研究問題",
            "method": None,
            "theory": None,
            "findings": None,
        },
    )
    return EndpointRequest(
        path_params={"paper_id": str(paper_id)},
        body={
            "zh_summary_id": str(summary_id),
            "field": "research_question",
            "note": "（假資料）說明",
        },
    )


@REGISTRY.case(
    "GET",
    "/api/v1/display-names",
    exempt="全站共用的期刊、主題、方法顯示名稱（設定檔），不讀任何成員資料",
    status=200,
)
def _display_names(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/api/v1/client-config",
    exempt="全站共用的前端設定值（時區），不讀任何成員資料",
    status=200,
)
def _client_config(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/api/v1/cube",
    exempt="計數方塊只有實驗室看得到的算數論文（counted_paper）的篇數，每位成員看到的一樣，"
    "不含任何成員自己的資料（Story 3.15）",
    status=200,
)
def _cube(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


# --- 個人文獻庫（Story 3.16）-------------------------------------------------------------------


@REGISTRY.case("GET", "/api/v1/library")
def _library(ctx: PermissionContext) -> EndpointRequest:
    # A 的清單有一篇（標題有金絲雀）；B 讀到的是自己的空清單（total 0、沒有下一頁）。
    _private_paper_in_owner_library(ctx)
    return EndpointRequest()


@REGISTRY.case("PUT", "/api/v1/library/{paper_id}")
def _add_library_item(ctx: PermissionContext) -> EndpointRequest:
    # B 看不到 A 的私人論文：加不進去、和不存在一樣 404；A 再加一次回原本那一筆（upload）。
    paper_id = _private_paper_in_owner_library(ctx)
    return EndpointRequest(path_params={"paper_id": str(paper_id)})


@REGISTRY.case("DELETE", "/api/v1/library/{paper_id}")
def _remove_library_item(ctx: PermissionContext) -> EndpointRequest:
    # B 的文獻庫沒有這一篇：404，A 的那一筆不動；A 移出回移出前的那一筆。
    paper_id = _private_paper_in_owner_library(ctx)
    return EndpointRequest(path_params={"paper_id": str(paper_id)})


@REGISTRY.case("POST", "/api/v1/library/{paper_id}/restore")
def _restore_library_item(ctx: PermissionContext) -> EndpointRequest:
    # 先經 services 替 A 移出（留下 A 的封存）；B 沒有這一篇的封存：404；A 復原回原本那一筆。
    paper_id = _private_paper_in_owner_library(ctx)
    owner = MemberViewer(member_id=uuid.UUID(ctx.owner.member_id), is_admin=False)
    assert remove_library_item(owner, ctx.extras["database"], paper_id) is not None
    return EndpointRequest(path_params={"paper_id": str(paper_id)})


# --- 找論文清單（Story 3.13）---------------------------------------------------------------------


@REGISTRY.case("GET", "/api/v1/papers")
def _paper_list(ctx: PermissionContext) -> EndpointRequest:
    # A 的私人論文（標題有金絲雀）在 A 的文獻庫：A 的 `personal` 清單有它；B 的 `personal` 是空清單
    # （total 0、沒有下一頁）。發佈一個快照：空清單的 `snapshot` 要是物件（框架不收 null）。
    _private_paper_in_owner_library(ctx)
    test_database = ctx.extras["test_database"]
    ((run_id,),) = execute(
        test_database, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id"
    )
    execute(
        test_database,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " VALUES (1, %s, 'published', '2026-11-16T02:30:00Z')",
        run_id,
    )
    return EndpointRequest(query={"scope": "personal"})
