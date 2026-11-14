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
