# ADR-0024：秘密進容器用 `.env.radar`（選填的 env_file）

- 狀態：提議
- 日期：2026-11-14
- 對應：epics Story 1.8（D1）；架構文件 AD-17、AD-29；ADR-0003（部署拓撲）、ADR-0021（OpenAlex 金鑰與用量）、ADR-0023（compose 檔在根目錄）

## 背景

1.6 起 worker 要用 OpenAlex 的 API key、物件儲存的帳密與端點、心跳網址，這些都是 `RADAR_*` 環境變數。到 1.7 為止，`compose.yaml` 的規則是「不用 env_file、不做變數替換」，容器裡拿不到任何秘密；1.8 的每週更新是第一個要在容器裡真的抓資料的工作。

根目錄的 `.env` 已經存在，裡面有本機開發用的各種值，其中包含 `RADAR_MIGRATION_DATABASE_URL`（資料庫擁有者的連線字串，只給 `radar migrate` 用，不該進長駐的容器）。

## 考慮過的選項

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | api、worker 的 `env_file` 直接讀整個 `.env` | 不用多一個檔 | `.env` 裡的每個變數都進容器，包含擁有者連線字串；之後有人往 `.env` 加東西，也會不知不覺進容器 |
| B | api、worker 讀另一個檔 `.env.radar`，`required: false`；範本 `.env.radar.example` 只有名稱、值全空 | 和 `.env` 分開；進容器的是 `.env.radar` 的全部內容（範本是建議清單）；沒有這個檔時（乾淨的 clone、CI）照樣起得來 | 多一個要自己建的檔；`.env` 與 `.env.radar` 可能有重複的值要各自維護 |
| C | Docker secrets（掛成檔案）或正式主機的環境變數 | 秘密不出現在環境變數裡（secrets）；正式主機可以交給系統管理 | 程式要改成從檔案讀；本機開發的 compose 用不太到；正式主機的做法屬於 Story 3.19 |

## 決定

本機與 CI 選 B；正式主機用什麼方式（C 或沿用 B）留到 Story 3.19 決定。

1. `compose.yaml` 的 api、worker 加 `env_file: [{path: .env.radar, required: false, format: raw}]`；`db`、`rustfs` 不讀。`format: raw`（Codex 補審 C2 補上）讓 `.env.radar` 的值原樣讀入：沒有它時 compose 會對值做變數替換，`abc$SUFFIX` 會變成 `abc`、`${HOME}` 會取到主機的值。
2. `compose.yaml` 的檔頭規則改成「只用這個 env_file、不做變數替換」。`environment` 裡寫的值（`DATABASE_URL` 等）比 `.env.radar` 優先，所以 `.env.radar` 蓋不掉容器內的資料庫連線。
3. `.gitignore` 加 `.env.radar`，並用 `!.env.radar.example` 讓範本進 git。
4. `.env.radar.example` 只列名稱與說明、值全空：`RADAR_OPENALEX_API_KEY`、物件儲存的五個必要變數、兩個心跳網址。說明寫明不放 `DATABASE_URL`、`RADAR_MIGRATION_DATABASE_URL`，以及本機 RustFS 從容器裡連是 `http://rustfs:9000`。真的 `.env.radar` 由 Kevin 自己建。
5. 架構測試檢查：只有 api、worker 讀 `.env.radar` 且是選填、用 `format: raw`、compose 裡沒有變數替換、範本只列允許的名稱且值是空的、`.env.radar` 被忽略而範本有被追蹤。

## 理由

1. **進容器的東西和 `.env` 分開。** `env_file` 會把 `.env.radar` 的每一行都放進 api、worker 的容器；範本只是建議清單，真的檔裡放什麼由 Kevin 負責。和直接讀 `.env`（選項 A）比，至少 `.env` 裡給主機上的指令用的值不會自動跟著進容器。不要把 `RADAR_MIGRATION_DATABASE_URL`（資料庫擁有者的連線字串）放進 `.env.radar`：放了它就會進長駐的容器；也不要把整個 `.env` 複製成 `.env.radar`。
2. **乾淨的 clone 與 CI 不用多做事。** `required: false` 讓沒有這個檔時照樣 `docker compose up`；本機 Docker Compose v5.6.0 支援這個寫法（在 scratchpad 用假值試過：沒有檔時不報錯、有檔時變數進得了容器、`environment` 的值優先）。
3. **正式主機的做法不用現在決定。** 正式主機只有 api、worker、cloudflared，compose 檔也是另一份（ADR-0023），3.19 再依主機的情況選。

## 取捨

- **多一個要自己建的檔**：`.env` 給在主機上跑的指令（`radar migrate`、測試）用，`.env.radar` 給容器用；同一個值（例如 OpenAlex key）可能兩邊都要寫。接受，換到的是兩邊的範圍分得清楚。
- **秘密仍是環境變數**：容器裡 `docker inspect`、`/proc/*/environ` 看得到。本機與 CI 接受；正式主機要不要改成 Docker secrets 在 3.19 決定。
- **要 Docker Compose 2.30 以上**：`env_file` 的 `path`／`required` 寫法是 2.24 才有的，`format: raw` 是 2.30 才有的；版本太舊時整個 compose 檔解析失敗，連 CI 只起 `db`、`rustfs` 那一步也會失敗。本機是 v5.6.0（在 scratchpad 用假值試過：`format: raw` 下 `RADAR_X=abc$SUFFIX`、`RADAR_Y=${HOME}` 原樣進容器；拿掉 `format: raw` 就變成 `abc` 與主機的家目錄）；CI runner 與正式主機的版本在 3.19 一起確認。
- **`format: raw` 連引號與行尾註解也原樣保留**：`KEY="值"` 進容器的是含引號的 `"值"`，`KEY=值 # 說明` 的 `# 說明` 也算進值裡。所以 `.env.radar` 的值不加引號、同一行不寫註解（範本已寫明）；換到的是值裡的 `$` 不會被改寫，也不會取到主機或 `.env` 的值。
- **沒有「啟動時拒絕擁有者連線字串」的防呆**：`.env.radar` 裡多放了 `RADAR_MIGRATION_DATABASE_URL` 時，api、worker 照樣起得來，只靠範本與這份 ADR 的警告。秘密的清單與隔離屬 Story 11.4，防呆留到那時一起做。
- **之後改回讀整個 `.env`**：只要改 `compose.yaml` 的 `env_file` 與架構測試 `tests/arch/test_compose.py`；但要先確認 `.env` 裡沒有不該進容器的值。

## 名詞

- **env_file**：compose 裡「啟動容器時，把某個檔裡的 `名稱=值` 一行一行變成容器的環境變數」的設定；`required: false` 表示檔不存在也不報錯。
- **`format: raw`**：env_file 的讀法選項，等號後面整段原樣當成值，不做變數替換、不去掉引號。
- **變數替換（interpolation）**：compose 讀檔時把 `${名稱}` 換成主機上的值；本專案的 compose 不用，避免主機上的值不小心寫進設定。
