# 字體授權

網站打包進建置結果的字體（ADR-0010 第 10 點：自己放、不連外部字體服務）。字型檔由 npm 套件提供，
建置時複製到 `dist/assets/`；repo 裡沒有另外放字型檔。中文標題字體還沒選定（`DESIGN.md` Typography），
目前用系統字堆疊，沒有內嵌任何中文字體。

| 字體 | 用到的字重 | 來源套件與版本 | 授權 | 著作權 |
|---|---|---|---|---|
| Instrument Serif | 400、400 斜體 | `@fontsource/instrument-serif` 5.3.0（字體來源 Google Fonts，Fontsource 字型版本 v5） | SIL Open Font License 1.1 | Copyright 2022 The Instrument Serif Project Authors（<https://github.com/Instrument/instrument-serif>） |
| DM Mono | 400 | `@fontsource/dm-mono` 5.3.0（字體來源 Google Fonts，Fontsource 字型版本 v16） | SIL Open Font License 1.1 | Copyright 2020 The DM Mono Project Authors（<https://www.github.com/googlefonts/dm-mono>） |

授權全文在各套件的 `LICENSE`（`node_modules/@fontsource/<名稱>/LICENSE`），也可見 <https://openfontlicense.org>。
SIL OFL 1.1 允許把字型檔隨網站一起散布與內嵌；不能單獨販售字型檔。
