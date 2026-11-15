"""正式 prompt 檔的雜湊登記（AD-8、ADR-0009 第 3 步、NFR-14、ADR-0030「依此更正」）。

`domain/policy.py` 的 `clear_prompt_file` 只放行整個檔的 SHA-256 登記在這裡的 prompt 檔；
`services/prompt_loader.py` 從這裡讀每個工作目前用哪一版，並核對凍結 schema 的雜湊。
這是唯一一份登記（沒有另外的登記檔），`tests/arch/test_prompt_registry.py` 核對它和 `prompts/`
底下的檔一致：模板或設定區改了卻沒升版、schema 改了卻沒升版、有檔沒登記，測試都會失敗。

規則（`tests/arch/test_prompt_hashes.py` 守著）：

- 只能寫字面值（`ast.literal_eval` 讀得出來），不 import 任何東西、不呼叫函式，所以登記不會在
  執行期由別的資料組出來；`radar/` 裡只有 `domain/policy.py` 與 `services/prompt_loader.py` 讀它。
- 登記只增不改：已登記的版本不改雜湊，內容或設定區改了就新增 `v<N+1>.md` 並在這裡加一列。

2.2 還沒有正式的 prompt（2.4～2.6 才寫），所以兩張表都是空的。
"""

# 工作代碼 → 目前用的版本。
CURRENT_PROMPT_VERSIONS: dict[str, int] = {}

# 工作代碼 → 版本 → (整個 prompt 檔的 SHA-256, 凍結的輸出 schema 字串的 SHA-256)；
# 沒有結構化輸出時第二個是 None。
PROMPT_FILE_HASHES: dict[str, dict[int, tuple[str, str | None]]] = {}
