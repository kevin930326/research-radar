"""原因代碼：被擋下、被拒收或呼叫失敗時記下的代碼，全專案只有這一份（架構文件 AD-8、AD-9）。

值是小寫加底線的字串，會寫進資料庫（例如 2.2 的 `derivation_run`、`llm_call`）與日誌，
寫出去之後不改名；新增代碼時加在這裡。
"""

from enum import StrEnum, unique


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
