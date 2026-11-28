"""原因代碼：被擋下、被拒收或呼叫失敗時記下的代碼，全專案只有這一份（架構文件 AD-8、AD-9）。

值是小寫加底線的字串，會寫進資料庫（例如 2.2 的 `derivation_run`、`llm_call`）與日誌，
寫出去之後不改名；新增代碼時加在這裡。

另有 `LockWaitTimeoutError`：`repo/db.Database.transaction_with_lock_timeout` 等鎖逾時丟的例外
（放在 domain，services 不必 import 資料庫套件就能攔）。
"""

from enum import StrEnum, unique


class LockWaitTimeoutError(Exception):
    """交易裡等鎖超過 `lock_timeout`；丟出時交易已經退回。訊息固定，不帶連線資訊。"""

    def __init__(self) -> None:
        super().__init__("等鎖超過時限，這個交易已退回")


@unique
class ErrorCode(StrEnum):
    """被擋下、被拒收或呼叫失敗的原因。"""

    # 送雲端的放行（`radar/domain/policy.py` 的 `clear_for_cloud`）
    CLOUD_BLOCKED_PAYWALLED = "cloud_blocked_paywalled"  # 付費全文或上傳的 PDF，開關沒開
    CLOUD_BLOCKED_SOURCE = "cloud_blocked_source"  # 來源還沒取得授權（例如 Semantic Scholar）
    CLOUD_BLOCKED_EMPTY = "cloud_blocked_empty"  # 空字串或只有空白
    CLOUD_BLOCKED_INVALID_TEXT = "cloud_blocked_invalid_text"  # 編不成 UTF-8（例如單獨的代理字元）
    # prompt 檔（`policy.clear_prompt_file`）：沒登記在 `domain/prompt_hashes.py`，或整個檔的雜湊
    # 和登記的不一樣（改了內容或設定區卻沒升版）
    CLOUD_PROMPT_UNREGISTERED = "cloud_prompt_unregistered"
    # 送出前核對：CloudSafeText 不在放行登記裡（被硬改過、空殼或已回收），不送出
    CLOUD_TEXT_NOT_REGISTERED = "cloud_text_not_registered"

    # LLM 呼叫：拒收（寫成 rejected、不自動重試）
    LLM_REFUSAL = "llm_refusal"  # 模型拒答
    LLM_TRUNCATED = "llm_truncated"  # 長度截斷
    LLM_MALFORMED_RESPONSE = "llm_malformed_response"  # 回應格式不對
    LLM_INVALID_REQUEST = "llm_invalid_request"  # 請求不合法（400、413、422 等）

    # LLM 呼叫：設定錯（停下來讓人處理）
    LLM_CONFIGURATION = "llm_configuration"  # 401、403、404

    # LLM 呼叫：暫時性（留給重試）
    LLM_TIMEOUT = "llm_timeout"
    LLM_RATE_LIMITED = "llm_rate_limited"
    LLM_SERVER_ERROR = "llm_server_error"
    LLM_CONNECTION_ERROR = "llm_connection_error"

    # LLM 閘門（`radar/services/llm_gateway.py`，Story 2.2）
    LLM_INPUT_TOO_LARGE = "llm_input_too_large"  # 預估輸入超過 max_input_tokens，不送出
    LLM_OUTPUT_INVALID = "llm_output_invalid"  # 輸出沒通過 Pydantic（多欄位、缺欄位、型別錯）
    LLM_OUTPUT_UNSTORABLE = "llm_output_unstorable"  # 輸出的字串存不進資料庫（NUL、單獨的代理字元）
    LLM_RUN_EXPIRED = "llm_run_expired"  # 互動呼叫超過時限還是 running，由 sweep 改成 failed
    LLM_GATEWAY_ERROR = "llm_gateway_error"  # 閘門自己出錯（例如寫結果失敗），run 改 failed
    # 模型已經回應、帳與輸出已存進 `llm_call`，但驗證或寫結果時出錯（或 sweep 判定逾時）：下一次
    # 同一組鍵從存下的輸出接續驗證與寫入，不再呼叫模型（ADR-0030）
    LLM_RESULT_PENDING = "llm_result_pending"

    # 中文重點的 domain 驗證（`radar/domain/validators/zh_summary.py`，Story 2.4、ADR-0032）：
    # 整筆拒收、不寫內容
    ZH_SUMMARY_UNSAFE_TEXT = "zh_summary_unsafe_text"  # 欄位含 `<`、`>`、網址、反引號、`](`
    # 欄位裡夾「摘要未提及…」這類講摘要本身的句子（整欄只是「未提及」這類字的存 NULL，不算拒收）
    ZH_SUMMARY_PLACEHOLDER_TEXT = "zh_summary_placeholder_text"
    ZH_SUMMARY_FIELD_TOO_LONG = "zh_summary_field_too_long"  # 正規化後超過 max_chars_per_field
    ZH_SUMMARY_NOT_ZH_HANT = "zh_summary_not_zh_hant"  # 沒有漢字、漢字比例太低或含簡體專用字
    ZH_SUMMARY_SENTENCE_TRANSLATION = "zh_summary_sentence_translation"  # 看起來是逐句翻譯
    # 輸出有輸入（標題＋摘要）沒有的阿拉伯數字或英文詞（編造）
    ZH_SUMMARY_UNSUPPORTED_CONTENT = "zh_summary_unsupported_content"

    # 研究模型的 domain 驗證（`radar/domain/validators/research_model.py`，Story 2.6、ADR-0034）：
    # 整筆拒收、不寫內容。情境值不在清單或輸入沒寫出、證據類型與支持與否對不上、同一句同一條關係
    # 重複，只改值並計數，不算拒收。
    # 原文欄位（假設原句、構面定義、研究限制、未來研究、量表來源、題目位置、效果類型）正規化後不在
    # 輸入（標題或摘要其中一段）裡
    RESEARCH_MODEL_QUOTE_NOT_IN_INPUT = "research_model_quote_not_in_input"
    # 任何字串含雙向控制字元、tag 字元或 HTML 實體；英文名或繁中標籤含標記或連結
    RESEARCH_MODEL_UNSAFE_TEXT = "research_model_unsafe_text"
    # 英文名或繁中標籤格式不對（長度、沒有英文字母或漢字、簡體專用字）；原句、定義不到 3 個詞
    RESEARCH_MODEL_INVALID_TEXT = "research_model_invalid_text"
    RESEARCH_MODEL_DUPLICATE_CONSTRUCT = "research_model_duplicate_construct"  # 正規化後同名
    # 假設的因或果不是清單裡的構面，或因和果是同一個構面
    RESEARCH_MODEL_UNKNOWN_CONSTRUCT = "research_model_unknown_construct"
    # 樣本數、題數 < 1；α、CR、AVE、p 不在 0～1；β 不是有限數
    RESEARCH_MODEL_NUMBER_OUT_OF_RANGE = "research_model_number_out_of_range"
    # 數字不是輸入裡的數字；英文名的詞、繁中標籤的英文與數字不在輸入裡（編造）
    RESEARCH_MODEL_UNSUPPORTED_CONTENT = "research_model_unsupported_content"
    # 同一句、同一條關係給了互相衝突的值（證據類型、支持與否或統計數字不同）
    RESEARCH_MODEL_CONFLICTING_HYPOTHESIS = "research_model_conflicting_hypothesis"

    # 向量化（`radar/services/embeddings.py`、`radar/adapters/workers_ai/`，Story 2.8、ADR-0040）：
    # 寫進 `derivation_run.failure_reason`（task＝embed）；向量化沒有「拒收」，都是 failed
    EMBED_TRANSIENT = "embed_transient"  # 重試用完仍是逾時、限流、服務端錯誤或連線錯誤
    EMBED_CONFIGURATION = "embed_configuration"  # 401、403、404：token、帳號或模型設定不對
    EMBED_REQUEST_INVALID = "embed_request_invalid"  # 400、413、422 等：請求本身不合法
    # 回應的信封、筆數、維度或數字不對（例如不是 1,024 維、有 NaN）：整批不寫；只有某一篇的向量轉成
    # halfvec 後全是 0 時只有那一篇不寫
    EMBED_RESPONSE_INVALID = "embed_response_invalid"
    EMBED_INPUT_CHANGED = "embed_input_changed"  # 寫入前核對：送出後標題或摘要又變了，這篇不寫
    EMBED_FAILED = "embed_failed"  # 向量化服務自己出錯（例如寫入的交易失敗），run 改 failed
    # 重試途中今天的額度放不下下一次嘗試：這一批不寫、不算失敗，下一次續跑
    EMBED_QUOTA_REACHED = "embed_quota_reached"
    # 寫入前等論文那一列的鎖超過 lock_timeout：這篇不寫，下一次重算
    EMBED_LOCK_TIMEOUT = "embed_lock_timeout"
    # 占用額度時等額度鎖超過 lock_timeout：這一批不送（已取得資格的 run 改 failed），其他論文照做
    EMBED_QUOTA_LOCK_TIMEOUT = "embed_quota_lock_timeout"

    # 檢索（`radar/services/retrieval.py`，Story 4.1、ADR-0005）：搜尋要把查詢轉成向量，下面兩種
    # 情況不降級成只用關鍵字，回固定的代碼（api 的錯誤回應用它）
    SEARCH_QUOTA_EXHAUSTED = "search_quota_exhausted"  # Workers AI 今天的額度用完了（沒有送出）
    # Workers AI 連不上或回了錯（重試用完、設定錯、回應不對、等額度鎖逾時）
    SEARCH_EMBEDDING_UNAVAILABLE = "search_embedding_unavailable"

    # Batch 送出（`radar/services/llm_batches.py`，Story 2.9、ADR-0041）
    # 送出前重建請求，和規劃時記的 run 鍵對不上（摘要、prompt 版本、模型或設定版本變了，或論文
    # 沒了）：那筆 run 改 failed、不送（`derivation_run.failure_reason`）；整批都對不上時批次
    # 也記這個
    BATCH_INPUT_CHANGED = "batch_input_changed"
    # 寫入前在摘要來源鎖裡核對（Story 2.12 審查後、ADR-0044）：送出時用的摘要來源已經不是這篇
    # 目前的摘要（被移除、換成別的來源）或不再放行：不建 run／不寫表頭，run 改 failed
    # （`derivation_run.failure_reason`），不再接續
    ABSTRACT_SOURCE_CHANGED = "abstract_source_changed"
    # 送出時逾時、斷線、5xx、回應看不懂或非預期的例外（含一般的 TypeError、ValueError）：不知道
    # 供應商有沒有建好批次，標 `needs_attention`、不自動重送（沒有冪等鍵）；停在 submitting 太久
    # 也記這個。一定沒建立的另記：4xx 照狀態碼；送出前就丟的 `NotCloudSafeError` 記
    # `cloud_text_not_registered`、沒有狀態碼的 `LlmConfigurationError` 記 `llm_configuration`
    BATCH_SUBMIT_UNCERTAIN = "batch_submit_uncertain"
    # 供應商明確回了 4xx（429、401、403、404 以外）：一定沒有建立，批次與 run 都改 failed
    BATCH_SUBMIT_REJECTED = "batch_submit_rejected"
    # 送出時在台北時間月初窗口裡：不動、不送，列維持 queued，sweep 之後補排
    BATCH_MONTH_START_HOLD = "batch_month_start_hold"
    # 送出時本月已熔斷：不動、不送，列維持 queued，sweep 之後補排
    BATCH_BUDGET_TRIPPED = "batch_budget_tripped"
    # 送出前最後確認（全域 Batch 預算鎖裡）：本月帳＋全部尚未結束的 Batch 預留超過熔斷線或這批工作的
    # 功能預算（規劃之後互動又花了、或預算調低）：不動、不送，列維持 queued，sweep 之後補排
    BATCH_BUDGET_EXCEEDED = "batch_budget_exceeded"
    # 送出前最後確認：這批再等也放不下（熔斷線或功能預算調低後，這批自己的預留加上排在它前面、還在
    # queued 的批次的預留就超過；`domain/llm_batches.batch_never_fits`）：同一個交易把批次從
    # queued 改 failed、這批的 run 改 failed（釋放預留），下一次規劃依新預算重排
    BATCH_BUDGET_CANNOT_FIT = "batch_budget_cannot_fit"
    # 送出時等全域 Batch 預算鎖逾時（規劃的長交易拿著）：不動、不送，列維持 queued，sweep 之後補排
    BATCH_BUDGET_LOCK_BUSY = "batch_budget_lock_busy"

    # Batch 結果匯入（`radar/services/llm_batches.py`、`llm_gateway.import_llm_batch_result`，
    # Story 2.11、ADR-0043）：寫進 `derivation_run.failure_reason` 或
    # `batch_submission.failure_reason`。
    # 結果裡這一筆是 `errored`、錯誤類型不是 `invalid_request_error`（供應商那邊出錯，沒有產生訊息、
    # 不計費）：run 改 failed，下一次 `radar batches create` 會再送（`invalid_request_error` 沿用
    # `llm_invalid_request` 寫成 rejected）
    BATCH_ITEM_ERRORED = "batch_item_errored"
    BATCH_ITEM_CANCELED = "batch_item_canceled"  # 這一筆在送進模型前被取消（不計費）：run 改 failed
    BATCH_ITEM_EXPIRED = "batch_item_expired"  # 24 小時內沒送進模型（不計費）：run 改 failed
    # 結果下載完，這一批還有項目沒出現在結果裡：那些 run 改 failed
    BATCH_RESULT_MISSING = "batch_result_missing"
    # 結果這一行看不懂（不是 JSON 物件，或 result 的 type 不認得）：run 改 failed
    # （succeeded 但訊息形狀不對是 llm_malformed_response、寫拒收）
    BATCH_RESULT_UNREADABLE = "batch_result_unreadable"
    # 批次結束了但結果已經拿不到（建立超過 29 天，或供應商沒給 results_url）：還在跑的 run 改 failed
    BATCH_RESULTS_UNAVAILABLE = "batch_results_unavailable"
    # 查批次狀態時供應商回 404：批次 `submitted` → `needs_attention`，要人工確認（`radar batches
    # resolve`）
    BATCH_PROVIDER_NOT_FOUND = "batch_provider_not_found"
    # 同一個供應商 batch ID 連續 `incomplete_results_attention_after` 次
    # （`config/llm_batches.yaml`）讀到的結果項數比供應商的計數少：批次 `ended` →
    # `needs_attention`，要人工確認（`radar batches resolve`：`--fail` 接受缺項、
    # `--provider-batch-id` 再讀；Story 2.11 確認審查 D2）
    BATCH_RESULTS_INCOMPLETE = "batch_results_incomplete"
    # `radar batches resolve --fail`：人工把 `needs_attention` 的批次標失敗，還在跑的 run 一起改
    # failed
    BATCH_RESOLVED_FAILED = "batch_resolved_failed"

    # 登入與 session（Story 3.3、ADR-0008、ADR-0046）：api 回應的代碼
    NOT_AUTHENTICATED = "not_authenticated"  # 沒有有效的 session（401）
    ORIGIN_REJECTED = "origin_rejected"  # 會改資料的請求，Origin 缺少或不是本站（403）
    LOGIN_REJECTED = "login_rejected"  # 假登入被拒（不說是哪一關，403）
    ADMIN_MEMBER_MISSING = "admin_member_missing"  # 試用：管理者成員還沒建立（403）

    # api 的錯誤回應（Story 3.4、ADR-0047）：RFC 9457；HTTP 狀態只在 `entrypoints/api/problems.py`
    NOT_FOUND = "not_found"  # 路徑不存在，或資料不存在／沒有權限（同一個 404，AD-3）
    METHOD_NOT_ALLOWED = "method_not_allowed"  # 路徑對、方法不對（405）
    REQUEST_INVALID = "request_invalid"  # 請求的內容或參數不合格式（422）
    REQUEST_REJECTED = "request_rejected"  # 框架擋下、沒有專屬代碼的其他 4xx
    INTERNAL_ERROR = "internal_error"  # 未預期的錯誤（500）；回應不帶任何內部細節

    # 文獻庫的寫入帶了「這個分頁以為的成員」（epic-3h PR 3、ADR-0055）：api 回應的代碼
    MEMBER_CHANGED = "member_changed"  # 和 session 的成員不同（另一個分頁換了人），不寫（409）
    EXPECTED_MEMBER_INVALID = "expected_member_invalid"  # 值不是成員 ID 或重複出現，不寫（400）

    # 回報中文重點錯誤帶了畫面上看到的那一筆中文重點（Story 3.12、ADR-0064）：api 回應的代碼
    # 送出前中文重點已經換成新的一筆（重新產生），不寫（409）；和換了人的 `member_changed` 分開
    SUMMARY_CHANGED = "summary_changed"
