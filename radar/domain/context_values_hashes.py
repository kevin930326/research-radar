"""情境值清單內容雜湊的登記（PRD FR-3、AD-18、Story 2.6、ADR-0034）。

`domain/context_values.py` 的 `context_values_registry_problems` 拿 `config/context_values.yaml` 的
內容雜湊（`domain/fingerprints.context_values_content_sha256`）和這裡比；版本沒登記，或內容改了
卻沒升版，讀設定時就 ConfigError。

規則（`tests/arch/test_context_values_hashes.py` 守著）：

- 只能寫字面值（`ast.literal_eval` 讀得出來），不 import、不呼叫函式；`radar/` 裡只有
  `domain/context_values.py` 讀它。
- 只增不改：已登記的版本不改雜湊；內容改了就把 `version` 加一，在這裡加一列。同一個測試檔拿
  main 第一父提交那條線上的登記比，已經進 main 的版本被改或被刪就失敗（要完整的 git 歷史）。
- 算雜湊：`uv run python -c "import yaml; from radar.domain.context_values import ContextValues;
  from radar.domain.fingerprints import context_values_content_sha256;
  print(context_values_content_sha256(ContextValues.model_validate(yaml.safe_load(
  open('config/context_values.yaml')))))"`。
"""

# 情境值清單版本 → 內容雜湊。
CONTEXT_VALUES_CONTENT_HASHES: dict[int, str] = {
    1: "b871b639107fd9ee7d03a9d0d4d048add96f6800a22d26f537e27f6189ee04d8",
}
