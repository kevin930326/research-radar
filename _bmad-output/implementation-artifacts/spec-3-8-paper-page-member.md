---
title: 'Story 3.8：論文頁成員版'
type: 'feature'
created: '2026-11-16'
status: 'done'
baseline_revision: '869448d9070915d2aeadd1e3dbc11600ae18297b'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/ux/reference/README.md'
  - '{project-root}/docs/adr/0038-design-reference-and-visual-compare.md'
  - '{project-root}/docs/adr/0037-design-tokens-guard-and-a11y-floor.md'
  - '{project-root}/docs/adr/0048-paper-page-api.md'
warnings: ['oversized']
deferred:
  - summary: >-
      「是否成立」的「未說明／不判定」旁邊沒有說明圖示與說明框。
    evidence: |-
      EXPERIENCE.md State Patterns 8「沒有依據：是否成立」要說明圖示，但沒有說明框的文字；列為 Kevin 決定。
    location: >-
      web/src/components/paper/ResearchModelSection.tsx
    severity: medium
  - summary: >-
      載入中是整頁骨架，不是「書目先出現、其餘區塊骨架」。
    evidence: |-
      3.6 的 API 一次回整頁，前端分不出先後；要分開得改 API。列為 Kevin 決定。
    location: >-
      web/src/routes/member.tsx
    severity: medium
  - summary: >-
      錯誤是整頁「載入失敗」，不是單一區塊的訊息條與重試。
    evidence: |-
      同上，只有一個請求。列為 Kevin 決定。
    location: >-
      web/src/routes/member.tsx
    severity: medium
  - summary: >-
      「分類意見不同」不能展開看歷史。
    evidence: |-
      修正歷史屬 Story 3.11。
    location: >-
      web/src/components/paper/PaperBiblio.tsx
    severity: low
  - summary: >-
      「有成員回報此欄可能有誤」放在那一欄下一行（EXPERIENCE 寫「旁邊」）。
    evidence: |-
      設計稿沒畫；沿用固定一行的樣子。列為 Kevin 決定。
    location: >-
      web/src/components/paper/ZhSummarySection.tsx
    severity: low
  - summary: >-
      not_applicable（手填書目）時三個 AI 區塊不顯示。
    evidence: |-
      對應的 State Patterns 列在 Epic 6；列為 Kevin 決定。
    location: >-
      web/src/pages/PaperPage.tsx
    severity: low
  - summary: >-
      「沒有摘要」那一塊寫「書目、分類和引用照常可以用」，但引用在 3.10 才有。
    evidence: |-
      設計稿原文；列為 Kevin 決定。
    location: >-
      web/src/i18n/zh-TW/paper.json
    severity: low
  - summary: >-
      「沒有摘要，所以沒有研究模型。」照設計稿有句號，EXPERIENCE.md 沒有。
    evidence: |-
      設計稿為準；列給 Kevin。
    location: >-
      web/src/i18n/zh-TW/paper.json
    severity: low
  - summary: >-
      英文原文摘要的字體堆疊和設計稿不同（Chrome／Firefox 斷行不同）。
    evidence: |-
      DESIGN.md 的 reading-en 沒有 Helvetica Neue；token-gaps.md 已記一列，Kevin 決定。
    location: >-
      web/src/styles.css
    severity: medium
  - summary: >-
      手機版「沒有摘要」沒有設計稿。
    evidence: |-
      用桌機「沒有摘要」的內容＋手機版版面；列給 Kevin。
    location: >-
      docs/ux/reference/screens/paper.html
    severity: low
---

<intent-contract>

## Intent

**Problem:** 3.6 已有 `GET /api/v1/papers/{id}` 與 `/display-names`，但成員版沒有論文頁：成員看不到書目、分類、中文重點、研究模型、原文摘要與取得全文的連結，也分不出哪些是 AI 產生的。

**Approach:** 成員版新增 `/papers/$paperId`，經資料來源層讀 3.6 的兩個端點，照設計稿 `paper.html`（正常狀態、沒有摘要）與 `paper-ai-failed.html`（只有中文重點沒成功、三個都沒成功）排版；三個 AI 區塊各自依信封 `state` 顯示，AI 文字只經新的 `AiText`（react-markdown＋rehype-sanitize，只留段落、粗體、清單）。設計稿沒畫的狀態用 `EXPERIENCE.md` 逐字的句子、沿用設計稿最接近那一塊的樣式。

## Boundaries & Constraints

**Always:**
- 字級、行高、字距、顏色、間距、髮線照設計稿行內樣式，用 `DESIGN.md` 的角色或尺度（沒寫行高的是 `leading-normal`、沒寫字距的是 `tracking-0`）；顏色換代幣（0.16＝`hairline`、0.26＝`border`）；尺度與代幣都沒有的值記進 `token-gaps.md`、先不做。
- 900px 以上兩欄（主要欄最寬 760、輔助欄 248 靠右、捲動時固定）；899px 以下輔助欄移到書目與分類下方，底部固定列只有「開啟全文」。
- 可點的東西至少 44×44（ADR-0037）：用負外距維持設計稿的視覺位置，比對工具量到的距離差寫明是這一條。
- 畫面文字全走 i18n：`EXPERIENCE.md` 有的逐字相同；只有設計稿（或 PRD 名詞表）才有的放 `otherSources`，測試核對它逐字出現在那份文件。顯示名稱（期刊、主題、方法）由 API 提供。
- AI 產生的文字（中文重點四欄、構面名稱、假設原句）只經 `AiText`；`failed` 不顯示 AI 標章；不顯示預計時間。
- 程式、測試、模擬 API 只用自己編的假論文（`example.com`、10.5555 DOI）。
- 模擬 API（`scripts/mock-api.mjs`）不用資料庫、不用 docker，用不同的論文 ID 切換每一種狀態；畫面上不加切換列。

**Never:**
- 不放引用（3.10）、修正分類（3.11）、回報錯誤（3.12）、分類標籤的連結（3.14）、頁尾（3.15）、我的文獻庫與手機底部的「加入我的文獻庫」（3.17）、找相似論文（Epic 4）、我的 PDF（Epic 6）、導覽列的其他連結。
- 不改後端、PRD、架構、既有 ADR 的決定、`epics.md`、`DESIGN.md`、`EXPERIENCE.md`、`docs/ux/reference/screens/`、`web/scripts/design-compare.mjs`。
- 不連真的後端、資料庫或外部服務；不把設計稿的摘要原文與中文重點放進程式、測試或模擬 API。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 三塊 ready | 有摘要、有 DOI、OA 有／無 | 書目（分類接最後一行）→ 中文重點四欄＋固定一行 → 研究模型 → 原文摘要（來源在標題旁）；第一個未核對標章寫全文；OA 沒有就不顯示 | — |
| 沒有摘要 | `abstract` null、中文重點與研究模型 `no_input`、分類 `input_kind=title` | 「中文重點與原文摘要」一塊訊息＋DOI 連結；研究模型一行；分類多一行「只根據標題分類」；「未分類」是方框 | 沒有 DOI 時不放 DOI 連結 |
| failed | 任一區塊 `failed` | 那一塊換成「沒有產生成功，可以請管理者重跑」，無標章；分類換成「分類」＋這一行 | — |
| 還在產生／額度用完 | `pending_interactive`／`pending_batch`／`deferred_budget` | 那一塊一行「還在產生，晚一點再回來看」／「這個月的 AI 額度用完了，下個月才會產生」（沿用 failed 那一行） | 無標章、無時間 |
| empty | 中文重點 `empty`、研究模型 `empty` | 四欄都「摘要未提及」；研究模型「摘要裡沒有寫出可以擷取的假設」；都有標章 | — |
| not_applicable | 手填書目的三塊 | 對應列在 Epic 6；先不顯示那一塊（列為 Kevin 決定） | — |
| 沒有 DOI | `doi_url` null | 輔助欄一行「這篇沒有 DOI，沒有全文連結」；手機沒有底部固定列 | — |
| 回報／修正 | `reported_fields`、`fix_member_count`、`members_disagree`、`human_verified` | 那一欄下一行「有成員回報此欄可能有誤」；分類下一行「有 N 位成員修正過」或「…，意見不同」；「◆ 已核對」時全文標章移到中文重點 | — |
| 找不到／壞 ID | API 404、ID 不是 UUID | 全站共用「找不到這一頁」（有導覽列）；壞 ID 不送請求 | — |
| 載入中／錯誤 | 慢／500、網路錯、回應壞 | 骨架（0.3 秒後）／全站共用「載入失敗」 | 401 → 登入頁（登入過期） |

</intent-contract>

## Code Map

- `docs/ux/reference/screens/paper.html`、`paper-ai-failed.html` -- 設計稿（唯讀）；桌機 `.v-desk`、手機 `.v-mobile`，899px 以下切手機。「沒有摘要」只有桌機版。
- `web/src/routes/member.tsx` -- 路由表；`appRoute` 底下加論文頁；`appRoute.beforeLoad` 處理登入。
- `web/src/routes/session.ts` -- `meQuery`、`clearMemberQueries`（論文 API 回 401 時照登入過期處理）。
- `web/src/datasource/{types,api/index,static/index}.ts` -- 加 `papers`（成員版呼叫產生的 `paper`、`displayNames`；示範站 null，3.9 才做）。
- `web/src/generated/model/*` -- 資料形狀（`PaperPageResponse`、三種 Block、`Provenance`、`DisplayNames`）。
- `web/src/components/AiBadge.tsx` -- 標章（菱形改用設計稿的 SVG；`model` 可以是 null）；`InfoPopover.tsx`、`MessageBar.tsx`、`PaperText.tsx`、`states/*` -- 沿用。
- `web/src/i18n/{index.ts,zh-TW/*.json,expected/notice.zh-TW.json,text.test.ts}` -- 文字與逐字比對（加佔位符與 `otherSources`）。
- `web/e2e/{pages.ts,api-mock.ts,layout.spec.ts,a11y-floor.spec.ts,links.spec.ts,screenshots/}` -- 頁面清單、後端替身、截圖做法。
- `web/scripts/mock-api.mjs` -- 本機預覽的模擬後端。
- `docs/ux/reference/expect-missing/`、`token-gaps.md` -- 比對清單與代幣缺口。

## Tasks & Acceptance

**Execution:**
- `web/package.json` -- 加 `react-markdown@10.1.0`、`rehype-sanitize@6.0.0`（版本照架構文件 Stack 表、固定）。
- `web/src/components/AiText.tsx` -- 淨化過的 Markdown（只留 `p`、`strong`、`ul`、`ol`、`li`；`inline` 模式只留粗體）。
- `web/src/components/DataDateBar.tsx` -- 資料日期列（截至日期＋快照編號；「#N」說明框）。
- `web/src/components/ExternalArrow.tsx` -- 設計稿的站外箭頭圖示。
- `web/src/components/AiBadge.tsx` -- 設計稿的 SVG 菱形；`model` 可空。
- `web/src/components/paper/*.tsx`、`web/src/pages/PaperPage.tsx`、`web/src/pages/paper-format.ts` -- 論文頁各區塊與格式化。
- `web/src/datasource/*`、`web/src/routes/member.tsx` -- 資料與路由。
- `web/src/i18n/zh-TW/paper.json`、`common.json`、`index.ts`、`expected/notice.zh-TW.json`、`text.test.ts` -- 文字。
- `web/src/components/AiText.test.tsx`、`web/src/pages/PaperPage.test.tsx` -- 元件測試（含 State Patterns 每一列、淨化）。
- `web/e2e/fixtures/papers.json`、`api-mock.ts`、`pages.ts`、`paper.spec.ts`、`fixtures.ts`（擋站外請求） -- 瀏覽器測試與 1440／1000／500 截圖。
- `web/scripts/mock-api.mjs` -- 論文頁的每一種狀態（不同 ID）、`--papers-file`。
- `docs/ux/reference/expect-missing/paper.txt`、`paper-ai-failed.txt`、`token-gaps.md`、`docs/glossary.md`、`sprint-status.yaml` -- 文件。

**Acceptance Criteria:**
- Given 三塊都 ready 的論文，when 打開論文頁，then 主要欄順序與文字照設計稿、資料日期列有截至日期與快照編號、輔助欄有「開啟全文」（新分頁）與有連結時的「開放取用版本」，沒有複製 AI 文字的按鈕。
- Given AI 文字含 HTML 標籤、`javascript:` 連結、Markdown 連結與圖片、腳本樣字串，when 顯示，then 畫面上沒有可點或可執行的東西（Vitest＋Playwright）。
- Given 同一頁三個區塊各是不同狀態，when 打開，then 各自照自己的狀態顯示。
- Given 設計稿的四個狀態，when 各在 1280／390 跑 `design-compare.mjs`，then 會擋的四段每一條都有三種理由之一。
- Given 1440／1000／500，when 跑 e2e，then 有整頁截圖並 commit；a11y、連結、axe 檢查含論文頁。

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 53 findings — high 0, medium 11, low 30, false 11, maybe-false 1
- findings:
  - `[medium]` `[defer]` （盲審 1）「未說明／不判定」旁邊沒有說明圖示 — EXPERIENCE.md 只寫「說明圖示解釋差別」，沒有說明框的字；照任務說明「不清楚時保守處理」，先不做，列為 Kevin 決定（不 HALT）。
  - `[false]` `[reject]` （盲審 2）「已核對」沒讀欄位上的 `human_verified` — `radar/repo/ai_blocks.py` 把信封的 `provenance.human_verified` 算成「兩欄都是人工值」，前端讀信封就是 API 的定義。
  - `[false]` `[reject]` （盲審 3）重疊檢查豁免了桌機的 sticky 輔助欄 — 重疊檢查只在 720×450 跑，那個寬度輔助欄是 `display: none`，本來就不在比對裡。
  - `[medium]` `[patch]` （盲審 4）手機底部的「開啟全文」沒被點過 — 加 e2e「both open full text buttons…」：500 寬點底部那一顆，核對 `window.open` 的網址與 `noopener,noreferrer`。
  - `[medium]` `[patch]` （盲審 5）站外網址沒檢查 http(s) — 加 `externalUrl()`（`paper-format.ts`），DOI、OpenAlex、開放取用版本都經過它；單元測試「external addresses that are not http(s)…」。
  - `[low]` `[patch]` （盲審 6）AI 回空字串時不顯示「摘要未提及」 — 空字串與只有空白當作 null；單元測試。
  - `[low]` `[patch]` （盲審 7）關係對不到構面時只剩箭頭、序號重複時 key 撞 — 對不到的關係不顯示（關係數跟著算）、key 改用位置；單元測試。
  - `[low]` `[reject]` （盲審 8）顯示名稱回 401 時是「載入失敗」 — 兩個請求同時送、論文先確認過 401，只剩兩個請求之間剛好過期這種情況；修正要改資料來源的回傳型別，不值得。
  - `[low]` `[reject]` （盲審 9）對不到顯示名稱時主題顯示代號、期刊不顯示 — 名稱與分類代號同一份設定檔產生，日常不會對不到；要統一規則得另訂。
  - `[low]` `[patch]` （盲審 10）分類 `ready` 但沒有標籤時全文標章不見；失敗那一行沒有 `data-block` — `firstUnverifiedBadge` 要有標籤才算分類；沒有標籤時整行不畫；失敗那一行加 `data-block="classification"`；單元測試。
  - `[low]` `[patch]` （盲審 11）「只有中文重點沒成功」沒有截圖 — 1440／1000／500 截圖加這個狀態（共 12 張）。
  - `[low]` `[patch]` （盲審 12）兩份 expect-missing 清單沒有測試確認相同 — 單元測試比對兩份（標頭以外）。
  - `[low]` `[patch]` （盲審 13）沒有快照時用瀏覽器時區的年份 — 改成台北日期的年份；單元測試用假時間（台北已跨年、UTC 還沒）。
  - `[low]` `[patch]` （盲審 14）`AiText` 註解說假設原句用 inline 模式 — 改註解（原句用區塊模式）；原句含清單時引號分段屬極少見情況，不改。
  - `[false]` `[reject]` （盲審 15）sprint-status 與 package-lock 不在 diff — sprint-status 在收尾時改；lock 是刻意不放進審查 diff（版本在 package.json 固定）。
  - `[low]` `[patch]` （邊界 1）路由記住的論文資料在換人登入後先顯示 — `paperRoute` 設 `gcTime: 0`，離開就丟掉。
  - `[low]` `[reject]` （邊界 2）同盲審 8。
  - `[low]` `[patch]` （邊界 3）同盲審 10。
  - `[low]` `[reject]` （邊界 4）沒有摘要但中文重點有內容（或反過來） — 後端：沒有摘要時中文重點一定是 `no_input`；日常不會出現。
  - `[low]` `[patch]` （邊界 5）同盲審 6。
  - `[low]` `[patch]` （邊界 6）同盲審 7。
  - `[low]` `[reject]` （邊界 7）構面名稱以「1. 」「- 」開頭時 Markdown 吃掉符號 — 構面名稱是短名詞，日常不會這樣開頭；要改得讓 inline 模式不解析 Markdown，違背「AI 文字一律經淨化過的 Markdown」。
  - `[low]` `[patch]` （邊界 8）沒有 DOI 但有開放取用版本時，同時寫「沒有全文連結」 — 兩個都沒有時才寫那一行；單元測試。
  - `[medium]` `[patch]` （邊界 9）同盲審 5。
  - `[low]` `[patch]` （邊界 10）研究模型 ready 但沒有構面時是「構面 0 個」 — 當作「摘要裡沒有寫出可以擷取的假設」，有標章；單元測試。
  - `[low]` `[patch]` （邊界 11）主題或方法的標籤清單是空的時只剩小標 — 空的那一組不畫；key 改用位置。
  - `[low]` `[patch]` （邊界 12）有 DOI 網址沒有 DOI 號碼、也沒有 OpenAlex 時留一列空白 — 那一列改看實際要放的連結。
  - `[false]` `[reject]` （邊界 13）spec 說要改 sprint-status — 收尾時改（3.8 → review）。
  - `[medium]` `[patch]` （驗證缺口 1）研究模型的內容沒有測試 — 單元測試「research model: constructs, then each relation…」。
  - `[medium]` `[patch]` （驗證缺口 2）顯示名稱沒有測試 — 單元測試「journal, topic and method names…」。
  - `[medium]` `[patch]` （驗證缺口 3）同盲審 4。
  - `[low]` `[patch]` （驗證缺口 4）被引用那一行的其他分支沒測 — 單元測試（沒有快照、沒有出版年）。
  - `[low]` `[patch]` （驗證缺口 5）摘要來源沒測 — 單元測試（OpenAlex、Semantic Scholar、上傳的 PDF 不寫）。
  - `[low]` `[patch]` （驗證缺口 6）`window.open` 的 `noopener,noreferrer` 沒測 — 併入盲審 4 的 e2e。
  - `[low]` `[patch]` （驗證缺口・其他）同盲審 13。
  - `[medium]` `[defer]` （意圖 1）載入中是整頁骨架，不是「書目先出現」 — 3.6 一次回整頁，做不到；Kevin 決定。
  - `[medium]` `[defer]` （意圖 2）錯誤是整頁「載入失敗」，不是單一區塊重試 — 同上；Kevin 決定。
  - `[low]` `[defer]` （意圖 3）「意見不同」不能展開看歷史 — 修正歷史在 3.11。
  - `[medium]` `[defer]` （意圖 4）同盲審 1。
  - `[low]` `[defer]` （意圖 5）「有成員回報」放在那一欄下一行，不是「旁邊」 — 設計稿沒畫；沿用固定一行的樣子；Kevin 決定。
  - `[low]` `[defer]` （意圖 6）`not_applicable` 不顯示區塊 — 對應的列在 Epic 6；Kevin 決定。
  - `[low]` `[defer]` （意圖 7）沒有引用，但「沒有摘要」那句提到引用 — 設計稿原文；引用在 3.10；Kevin 決定。
  - `[low]` `[defer]` （意圖 8）「沒有摘要，所以沒有研究模型。」有句號 — 設計稿為準，列給 Kevin。
  - `[maybe-false]` `[reject]` （意圖 9）截圖沒有和基準比 — 專案從 3.1 起都是拍圖供人看（加上量位置的版面測試）；要不要做基準比對不在這個 Story 的意圖裡，若要做只會是 low。
  - `[false]` `[reject]` （意圖 10）Kevin 對照設計畫布才合併、比對報告 — 是流程，放在 PR 說明，不在 diff。
  - `[medium]` `[defer]` （比對 1）英文摘要的字體堆疊和設計稿不同（Chrome 退到不同的字，390 寬斷行 24 對 23） — 代幣照 DESIGN.md，不能自己改；`token-gaps.md` 補一列，Kevin 決定。
  - `[low]` `[defer]` （比對 2）手機版「沒有摘要」沒有設計稿 — 用桌機「沒有摘要」的內容＋手機版版面；列給 Kevin。
  - `[false]` `[reject]` （比對 3）品牌有連結、帳號不是連結 — Kevin 2026-11-16 決定（3.5 PR 差異表）。
  - `[false]` `[reject]` （比對 4）連結的「和上一段距離」變小 — ADR-0037 第 5 點 44×44，用負外距維持位置（外層高度和設計稿相同）。
  - `[false]` `[reject]` （比對 5）手機 OpenAlex「上一段是誰」不同 — 同上，可點範圍重疊 8px 的量法結果。
  - `[false]` `[reject]` （比對 6）標籤不是連結、少箭頭、後面位置左移 — Story 3.14。
  - `[false]` `[reject]` （比對 7）手機底部「開啟全文」寬 350 — 「加入我的文獻庫」屬 Story 3.17；固定列的上一段不同是 3.10、3.17、Epic 6 的區塊不在。
  - `[false]` `[reject]` （比對 8）假論文造成的文字差異 — Kevin 2026-11-16 決定（TASK.md Story 3.8 第 10 點）；用設計稿內容補跑後消失。

## Design Notes

- **比對用的資料**：正式報告用假論文（照指示），論文內容的段落和設計稿文字不同，工具會列成「設計稿沒有／缺少」，理由寫 Kevin 2026-11-16 的決定（TASK.md Story 3.8 第 10 點）。另外用 `--papers-file` 載入一份「執行時從設計稿 HTML 讀出、不進 repo」的資料補跑一次，量內容段落的樣式（補充證據，不取代正式報告）。
- **平均每年**：`round(被引用次數 ÷ max(1, 截至年份 − 出版年))`；截至年份取快照日期（台北），沒有快照用今天。設計稿的數字（2019 年 10,639 次 → 1,520；2025 年 8 次 → 8）照這個算得出來。
- **整頁只有一個請求**：3.6 一次回三塊，所以「書目先出現、其餘骨架」與「單一區塊載入失敗、只有那塊重試」做不出來；整頁用全站共用的骨架與「載入失敗」，列為 Kevin 決定。
- **「開啟全文」是按鈕**（設計稿是 `<button>`），用 `window.open(…, "noopener,noreferrer")` 開新分頁；其他站外連結是 `<a target="_blank">`＋只給報讀器的「（開新分頁）」。

## Verification

**Commands:**
- `cd web && npm run lint && npm run typecheck && npm test && npm run build:member && npm run build:demo && npm run e2e` -- 全過
- `node scripts/design-compare.mjs …`（四個狀態 × 1280／390） -- 會擋的四段每一條有理由

## Auto Run Result

**做了什麼**：成員版新增論文頁 `/papers/$paperId`。經資料來源層讀 3.6 的論文與顯示名稱兩個端點，照設計稿 `paper.html`、`paper-ai-failed.html` 排版（900px 以上兩欄、以下單欄＋底部固定的「開啟全文」）。分類、中文重點、研究模型三塊各依信封 `state` 顯示；AI 文字只經新的 `AiText`（react-markdown 10.1.0＋rehype-sanitize 6.0.0，只留段落、粗體、清單）。找不到／壞 ID 用全站「找不到這一頁」，401 走登入過期，其他錯誤用全站「載入失敗」。模擬 API 用不同的論文 ID 切換 17 種狀態與 4 種錯誤。

**改了哪些檔**：
- `web/package.json`、`package-lock.json` -- 加 react-markdown 10.1.0、rehype-sanitize 6.0.0（固定版本，MIT）。
- `web/src/components/AiText.tsx`、`AiText.test.tsx` -- 淨化過的 Markdown 與 17 個測試。
- `web/src/components/DataDateBar.tsx`、`ExternalArrow.tsx` -- 資料日期列；站外箭頭與共用的「（開新分頁）」說明。
- `web/src/components/AiBadge.tsx` -- 設計稿的 SVG 菱形；`model` 可空；`triggerClassName`。
- `web/src/components/paper/*.tsx` -- 書目＋分類、中文重點、研究模型、原文摘要／沒有摘要、全文（輔助欄、手機、底部固定列）、共用小元件。
- `web/src/pages/PaperPage.tsx`、`paper-format.ts`、`PaperPage.test.tsx` -- 頁面組合、格式化、38 個測試。
- `web/src/datasource/{types,api/index,static/index}.ts`、`web/src/routes/member.tsx` -- `papers` 資料來源與路由（`gcTime: 0`）。
- `web/src/i18n/{index.ts,zh-TW/paper.json,zh-TW/common.json,expected/notice.zh-TW.json,text.test.ts}` -- 文字、`otherSources`、佔位符逐字比對。
- `web/e2e/{fixtures/papers.json,api-mock.ts,pages.ts,fixtures.ts,expected.ts,paper.spec.ts,links.spec.ts,a11y-floor.spec.ts,dev-components.spec.ts}`、`screenshots/` -- 假論文、後端替身、25 個論文頁 e2e、12 張論文頁截圖、既有測試的調整。
- `web/scripts/mock-api.mjs` -- 論文與顯示名稱端點、`--papers-file`、`/__mock/papers` 索引。
- `docs/ux/reference/expect-missing/paper.txt`、`paper-ai-failed.txt`、`token-gaps.md`、`docs/glossary.md` -- 比對清單、代幣缺口、名詞。

**審查結果**：53 條（high 0、medium 11、low 30、false 11、maybe-false 1）。
- patch：medium 6 條（盲審 4、5，邊界 9，驗證缺口 1、2、3；實際是 4 件事）、low 19 條。
- defer：10 條（見 frontmatter `deferred`，都列為 Kevin 決定）。
- reject：盲審 2、3、8、9、15，邊界 2、4、7、13，意圖 9、10，比對 3～8，理由見 Review Triage Log。
- 後續審查建議：`false`。patch 的 medium 有兩條以上，但每一條都已有對應的單元測試或 e2e，說不出還沒驗證的風險。

**驗證**：
- `npm run lint`、`typecheck`、`test`（14 檔 322 個）、`build:member`、`build:demo` 全過。
- `CI=1 npx playwright test --project=chromium --project=webkit`：365 passed、25 skipped（截圖只在 chromium）、0 failed。
- firefox 本機啟動不了（`browserType.launch: … Could not find profile folder`，連第一頁都沒開；和程式無關），firefox 交給 CI。
- `design-compare.mjs`：四個狀態 × 1280／390，會擋的四段每一條都有理由；另用設計稿內容補跑一次。

**剩下的風險**：
- firefox 只會在 CI 跑到。
- 建置有「chunk 超過 500 kB」的警告（加了 react-markdown 後）；功能不受影響，要不要拆檔留給之後。
- 真實資料的 `open_access_url` 目前一律是 null，「開放取用版本」只在假資料看得到。
