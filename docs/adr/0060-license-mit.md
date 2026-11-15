# ADR-0060：這個 repo 用 MIT 授權；別人的東西各自照原本的授權

- 狀態：已接受（Kevin 2026-11-15）
- 日期：2026-11-15
- 對應：AR-48；Story 13.6（repo 公開前的檢查）；`LICENSE`
- 編號說明：0039～0045 留給 Epic 2 第四輪、0046 起留給前端第二輪（兩條線同時在跑），這份跳到 0060 避免撞號。

## 背景

repo 準備公開（放履歷，同時公開的 repo 跑 GitHub Actions 不計分鐘數）。沒有授權條款時，別人依法不能使用或修改這裡的程式；AR-48 要求公開之前先決定。

## 考慮過的選項

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | MIT | 最寬鬆、最多人認得；別人可以自由使用、修改、商用，只要保留著作權與授權聲明 |
| B | Apache-2.0 | 和 MIT 差不多寬鬆，多了專利條款，文字長 |
| C | 不放授權條款 | 別人看得到但不能用；之後還能改成 A 或 B |

## 決定

用 MIT（選項 A）。授權全文在 repo 根目錄的 `LICENSE`；檔案內容只放標準的 MIT 全文，不加別的說明，GitHub 才認得出是 MIT（多加一段就會顯示成認不出的授權）。

MIT 只涵蓋這個專案自己寫的程式與文件。下面這些是別人的東西，照各自的授權，不因為放在這個 repo 就變成 MIT：

| 位置 | 來源 | 授權（2026-11-15 查） |
|---|---|---|
| `_bmad/`、`.claude/skills/bmad-*`、`.agents/skills/bmad-*` | bmad-code-org/BMAD-METHOD | MIT；BMad 名稱是商標 |
| `.claude/skills/supabase-postgres-best-practices` | supabase/agent-skills | MIT |
| `.claude/skills/postgresql-table-design`、`hybrid-search-implementation`、`llm-evaluation` | wshobson/agents | MIT |
| `.claude/skills/react-best-practices` | vercel-labs/agent-skills | MIT（寫在 `SKILL.md`） |
| `.claude/skills/modern-python`、`property-based-testing` | trailofbits/skills | CC BY-SA 4.0：要標明出處，改作要用同一種授權 |
| `.claude/skills/frontend-design` | anthropics/skills | Apache-2.0（資料夾裡的 `LICENSE.txt`） |
| `.claude/skills/playwright-cli` | microsoft/playwright-cli | Apache-2.0（資料夾裡的 `LICENSE`） |
| 網站用的字體 | Fontsource 套件 | SIL Open Font License 1.1，見 `web/src/fonts/LICENSES.md` |
| 設計稿裡的一篇英文摘要 | Schneider, Dykmann & Teubner (2025)，JMIS | CC BY 4.0，出處寫在 `docs/ux/reference/README.md` |
| 書目資料（測試錄影檔、`docs/ux/reference/papers-data.js`） | OpenAlex | CC0 |

各 skill 的來源 commit 記在 `docs/skills-plan.md`。

## 理由

- 這個專案的目的是展示能力，不靠程式碼賺錢；MIT 最簡單、最常見。
- 沒有需要專利條款的內容，不需要 Apache-2.0 的額外文字。

## 取捨

- 已經用 MIT 放出去的版本收不回來；之後換授權只對新版本有效。
- 之後加進別人的檔案（新的 skill、字體、論文摘要）時，要先確認授權允許轉散布，並補進上面的表。付費期刊的摘要與全文不放進 repo。
- `pyproject.toml` 與 `web/package.json` 還沒有寫 license 欄位；這份 ADR 沒有動它們（兩條線正在改這些檔案），之後補。
