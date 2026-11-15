"""測試用的系統提示：在 `radar/domain/prompt_hashes.py` 暫時登記測試自己編的 prompt 檔。

`clear_for_cloud` 拒收 `PROMPT_TEMPLATE`（ADR-0030「依此更正」），系統提示只能經
`policy.clear_prompt_file` 放行，而它只認 `prompt_hashes.py` 寫死的雜湊。測試要做系統提示時，
用這裡的函式在登記表放一份只有測試知道的登記；`tests/conftest.py` 每個測試結束都會還原登記表。
內容全部是測試自己編的（不是正式的 prompt）。
"""

from radar.domain import prompt_hashes
from radar.domain.fingerprints import prompt_sha256
from radar.domain.policy import CloudSafeText, clear_prompt_file

# 不是工作代碼：只給 `template_text` 用，不會和 prompts/ 底下的正式登記混在一起。
TEMPLATE_TASK = "test_template"


def prompt_file(body: str, *, max_tokens: int = 1024) -> bytes:
    """一個最小的 prompt 檔（設定區＋本文）的位元組。"""
    return f"---\nmax_tokens: {max_tokens}\n---\n{body}\n".encode()


def template_text(body: str = "請整理。") -> CloudSafeText:
    """經 `clear_prompt_file` 放行的系統提示（來源 `prompt_template`），文字是 `body` 去掉頭尾空白。

    呼叫期間暫時把這份檔登記進 `PROMPT_FILE_HASHES`，放行後立刻還原；模組載入時呼叫也可以。
    """
    content = prompt_file(body)
    saved = prompt_hashes.PROMPT_FILE_HASHES
    prompt_hashes.PROMPT_FILE_HASHES = {
        **saved,
        TEMPLATE_TASK: {1: (prompt_sha256(content), None)},
    }
    try:
        return clear_prompt_file(TEMPLATE_TASK, 1, content)
    finally:
        prompt_hashes.PROMPT_FILE_HASHES = saved
