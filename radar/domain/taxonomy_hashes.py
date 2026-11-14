"""分類表內容雜湊的登記（PRD FR-36、AD-18、Story 2.5、ADR-0033）。

`domain/taxonomy.py` 的 `taxonomy_registry_problems` 拿 `config/taxonomy.yaml` 的內容雜湊
（`domain/fingerprints.taxonomy_content_sha256`）和這裡比；版本沒登記，或內容改了卻沒升版，
讀設定時就 ConfigError。

規則（`tests/arch/test_taxonomy_hashes.py` 守著）：

- 只能寫字面值（`ast.literal_eval` 讀得出來），不 import、不呼叫函式；`radar/` 裡只有
  `domain/taxonomy.py` 讀它。
- 只增不改：已登記的版本不改雜湊；內容改了就把 `version` 加一，在這裡加一列。同一個測試檔拿
  main 第一父提交那條線上的登記比，已經進 main 的版本被改或被刪就失敗（要完整的 git 歷史）。
- 算雜湊：`uv run python -c "import yaml; from radar.domain.taxonomy import Taxonomy;
  from radar.domain.fingerprints import taxonomy_content_sha256;
  print(taxonomy_content_sha256(Taxonomy.model_validate(yaml.safe_load(open('config/taxonomy.yaml')))))"`。
"""

# 分類表版本 → 內容雜湊。
TAXONOMY_CONTENT_HASHES: dict[int, str] = {
    1: "094fb1c98efa935dbf681f47801eecf4a32e6d50a6d448c625e8398aad886610",
}
