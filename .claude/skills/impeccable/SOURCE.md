# 來源與授權

- 來源：<https://github.com/pbakaus/impeccable>，`.claude/skills/impeccable/`，commit `b49177d`（2026-11-18），skill 版本 4.10.0。
- 授權：Apache-2.0，見同資料夾的 `LICENSE` 與 `NOTICE.md`（從來源庫根目錄複製）。
- **只裝說明文件**（Kevin 2026-11-21 決定）：`SKILL.md` 與 `reference/` 的 50 份準則，內容未修改。
- **沒有裝的**：來源的 `scripts/`（啟動腳本、瀏覽器腳本、字型索引）、`agents/`、hook 設定。啟動腳本會從網路下載一支引擎執行檔放到家目錄 `~/.impeccable/` 再執行，和 `docs/skills-plan.md`「裝在專案層級、不要裝成全域」的原則不合，所以不裝。
- 因為沒有腳本，`SKILL.md` 第一步的 `scripts/impeccable context` 會跑不起來；照它自己寫的「Launcher unavailable」那一段，改成直接讀專案的檔案。
- **在這個專案怎麼用**：只用審查與修飾現有畫面的準則（`audit`、`critique`、`polish`、`adapt`、`harden`、`layout`、`typeset`、`clarify` 等）。不要照文件裡的指示跑 `npx impeccable …`（`install`、`login`、`update`）、不要下載引擎、不要用 `init`／`document`／`hooks`／`live`／`generate`／`foundations`／`doctor` 這幾個指令（它們會產生 `PRODUCT.md`、另一份 `DESIGN.md` 或掛 hook；這個專案的設計規格在 `_bmad-output/planning-artifacts/ux-designs/`，畫面以 `docs/ux/reference/screens/` 的設計稿為準）。它的建議和設計稿或 `AGENTS.md` 衝突時，以設計稿與 `AGENTS.md` 為準。
