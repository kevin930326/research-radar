# Skill 安裝時機

> 原則：要用的前一刻才裝，不要一次全裝。裝之前先看過 SKILL.md。
> 安裝在專案層級（repo 的 `.claude/skills/` 或專案 plugin），不要裝成全域。
> 大型合集（wshobson/agents、trailofbits/skills）只裝單一 skill，不要整包裝。
> 已安裝的項目在「狀態」欄打勾。
> 清單整理於 2026-11-06；開始安裝前，先確認各 repo 仍有在維護。

| 時機（觸發點） | Skill | 來源 | 用途 | 狀態 |
|---|---|---|---|---|
| 跑 `bmad-architecture` 之前 | `supabase-postgres-best-practices` | [supabase/agent-skills](https://github.com/supabase/agent-skills) | 通用 PostgreSQL 規則：索引、連線、鎖、schema | [x] 2026-11-12，來源 commit `c9be0e9` |
| 跑 `bmad-architecture` 之前 | `postgresql-table-design` | [wshobson/agents](https://github.com/wshobson/agents) | 資料表設計與審查：型別、鍵、約束、JSONB | [x] 2026-11-12，來源 commit `9c61ab2` |
| 跑 `bmad-architecture` 之前 | `hybrid-search-implementation` | [wshobson/agents](https://github.com/wshobson/agents) | 向量搜尋＋關鍵字搜尋並用（pgvector＋全文檢索） | [x] 2026-11-12，來源 commit `9c61ab2` |
| 第一個實作 Story 開始前 | `modern-python` | [trailofbits/skills](https://github.com/trailofbits/skills) | uv、ruff、pytest | [x] 2026-11-12，來源 commit `e4dd0bb`（只裝 skill，沒裝 hooks） |
| 第一個前端 Story 開始前 | `frontend-design` | [anthropics/skills](https://github.com/anthropics/skills) | 照設計稿寫 React | [x] 2026-11-15，來源 commit `1780c5e` |
| 第一個前端 Story 開始前 | `react-best-practices` | [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills) | React 效能（部分規則針對 Next.js，不適用的略過） | [x] 2026-11-15，來源 commit `1a2c79d` |
| 第一個前端 Story 開始前 | `playwright-cli` | [Playwright 官方](https://playwright.dev/agent-cli/skills) | 開瀏覽器測試網站（`playwright-cli install --skills`） | [x] 2026-11-15，來源 commit `b217248`（只裝說明檔，工具本身在第一個前端 Story 加進開發相依套件） |
| 主要畫面做完之後 | `impeccable` | [pbakaus/impeccable](https://github.com/pbakaus/impeccable) | 審查與修飾現有畫面：手機版、對比、點擊範圍 | [ ] |
| 做摘要、問答功能時 | `llm-evaluation` | [wshobson/agents](https://github.com/wshobson/agents) | 建立 eval 題組、比較模型與 prompt | [x] 2026-11-14，來源 commit `9c61ab2` |
| 寫 DOI 正規化、去重複等函式時 | `property-based-testing` | [trailofbits/skills](https://github.com/trailofbits/skills) | 測輸入千變萬化的函式 | [x] 2026-11-14，來源 commit `149d7b2`（只裝 skill，沒裝 evals 與腳本） |
| 第一次部署上線之前 | `insecure-defaults`、`differential-review`、`supply-chain-risk-auditor` | [trailofbits/skills](https://github.com/trailofbits/skills) | 資安檢查、PyPI 套件風險 | [ ] |

## 選配

- `ui-ux-pro-max`（[nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)）：依產品類型產生設計系統，無障礙做得好，但比較測試中結果最制式。挑風格以 Claude Design 為主；想要一份「保守、可用」的基準方案時才用，時機是 `bmad-ux` 之後、畫設計稿之前。

## 不要裝

- `obra/superpowers`、`addyosmani/agent-skills` 整包：自帶一整套規格、計畫、實作流程，和 BMAD 衝突。
- `webapp-testing`（anthropics）：改用 Playwright 官方的 `playwright-cli`。
- `web-design-guidelines`（Vercel）：和 `impeccable` 重疊，擇一即可。
- `fastapi-templates` 等 FastAPI 範本：會自帶專案結構，和架構階段的決定衝突。
- Vercel 的 `deploy-to-vercel`、Supabase 的 `supabase`：和本專案架構無關。

## 不需要安裝（Claude app 內建）

- Claude Design（畫設計稿）、Design System（記錄定案的風格）：在 Claude app 使用，`bmad-ux` 之後。
- `claude-api`（串接 Haiku、Batch API）：Claude Code 已內建同名 skill，不另外複製進 repo（2026-11-12 決定；[anthropics/skills](https://github.com/anthropics/skills) 的那一份有 96 個檔、約 2.2 MB）。第一個實作 Story 開始前確認內建的還在即可。
