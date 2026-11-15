"""正式 prompt 檔的雜湊登記（AD-8、ADR-0009 第 3 步、NFR-14、ADR-0030「依此更正」）。

`domain/policy.py` 的 `clear_prompt_file` 只放行整個檔的 SHA-256 登記在這裡的 prompt 檔；
`services/prompt_loader.py` 從這裡讀每個工作目前用哪一版，並核對凍結 schema 的雜湊。
這是唯一一份登記（沒有另外的登記檔），`tests/arch/test_prompt_registry.py` 核對它和 `prompts/`
底下的檔一致：模板或設定區改了卻沒升版、schema 改了卻沒升版、有檔沒登記，測試都會失敗。

規則（`tests/arch/test_prompt_hashes.py` 守著）：

- 只能寫字面值（`ast.literal_eval` 讀得出來），不 import 任何東西、不呼叫函式，所以登記不會在
  執行期由別的資料組出來；`radar/` 裡只有 `domain/policy.py` 與 `services/prompt_loader.py` 讀它。
- 登記只增不改：已登記的版本不改雜湊，內容或設定區改了就新增 `v<N+1>.md` 並在這裡加一列。

正式 prompt：`zh_summary` v1（Story 2.4、ADR-0032）、`classify` v1（Story 2.5、ADR-0033）、
`extract_model` v1（Story 2.6、ADR-0034）。
"""

# 工作代碼 → 目前用的版本。
CURRENT_PROMPT_VERSIONS: dict[str, int] = {"zh_summary": 1, "classify": 1, "extract_model": 1}

# 工作代碼 → 版本 → (整個 prompt 檔的 SHA-256, 凍結的輸出 schema 字串的 SHA-256)；
# 沒有結構化輸出時第二個是 None。
PROMPT_FILE_HASHES: dict[str, dict[int, tuple[str, str | None]]] = {
    "zh_summary": {
        1: (
            "26a03aa09d0da7e8860a2e8f5eb21219964ff61c6b72855d8ad953dff7c8c5e2",
            "0de6e57cd05014d0c54aff8bd0db5d9cab9e5748be372d90ed8b0f7fe5ce1899",
        ),
    },
    "classify": {
        1: (
            "31bcc8434aaaaa180b7eb5e62123470e14e8077eede5526280a80547915af479",
            "9a9cb33f829686bb43c9bbb769b24a26279c647f7f9c516fd050d1fc90c31c09",
        ),
    },
    "extract_model": {
        1: (
            "593ffab9c3c09bde32f7486f9f00f49c822c18ea63d5a1935614063acf0bedf7",
            "3069864a203ec7c369c2780b453e5232e0c23c54545dfeb49ff35c7c59f0a00a",
        ),
    },
}
