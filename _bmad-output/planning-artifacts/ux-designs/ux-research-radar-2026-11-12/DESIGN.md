---
name: Research Radar
description: 資管論文的研究雷達：用「資料組成的星系」呈現可計數、可追溯的文獻證據
status: final
sources:
  - docs/ux/homepage-visual-spec.md
  - _bmad-output/planning-artifacts/prds/prd-research-radar-2026-11-06/prd.md
updated: 2026-11-12
colors:
  background: '#050609'
  background-glow-inner: '#0B0F19'
  background-glow-mid: '#07090F'
  text-primary: '#EFEAE0'
  text-secondary: '#A8A294'
  text-tertiary: '#8E887B'
  text-reading: '#D5D0C5'
  accent-gold: '#E3B466'
  on-button: '#0A0B0E'
  hairline: '#EFEAE029'
  border: '#EFEAE042'
  popover-surface: '#050609EB'
  scrim: '#050609B8'
  data-warm: '#FFECD0'
  data-white: '#F4F5FA'
  data-cool: '#C4D6FF'
  data-dim: '#7E8CAC'
  data-ring: '#8A98BA'
  danger: '#E58B7B'
typography:
  display-zh:
    fontFamily: 中文標題字體（待選；原型用 Songti TC）
    fontSize: 60px
    fontWeight: '300'
    lineHeight: '1.3'
    letterSpacing: 0.04em
  page-title-zh:
    fontFamily: 中文標題字體（待選；原型用 Songti TC）
    fontSize: 40px
    fontWeight: '300'
    lineHeight: '1.3'
    letterSpacing: 0.04em
  finding-zh:
    fontFamily: 中文標題字體（待選；原型用 Songti TC）
    fontSize: 32px
    fontWeight: '300'
    lineHeight: '1.35'
    letterSpacing: 0.04em
  section-title-zh:
    fontFamily: 中文標題字體（待選；原型用 Songti TC）
    fontSize: 24px
    fontWeight: '300'
    lineHeight: '1.4'
    letterSpacing: 0.04em
  unit-zh:
    fontFamily: 中文標題字體（待選；原型用 Songti TC）
    fontSize: 19px
    fontWeight: '300'
  readout-number:
    fontFamily: Instrument Serif
    fontSize: 68px
    fontWeight: '400'
    lineHeight: '1.05'
    letterSpacing: -0.01em
  paper-title:
    fontFamily: Instrument Serif
    fontSize: 34px
    fontWeight: '400'
    lineHeight: '1.25'
    letterSpacing: 0.005em
  paper-title-hover:
    fontFamily: Instrument Serif
    fontSize: 25px
    fontWeight: '400'
    lineHeight: '1.25'
    letterSpacing: 0.005em
  paper-title-row:
    fontFamily: Instrument Serif
    fontSize: 21px
    fontWeight: '400'
    lineHeight: '1.3'
    letterSpacing: 0.005em
  brand:
    fontFamily: Instrument Serif
    fontSize: 24px
    fontWeight: '400'
    letterSpacing: 0.01em
  subtitle-en:
    fontFamily: Instrument Serif Italic
    fontSize: 23px
    fontWeight: '400'
    lineHeight: '1.2'
    letterSpacing: 0.01em
  body:
    fontFamily: PingFang TC, Microsoft JhengHei, Noto Sans TC, sans-serif
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.9'
    letterSpacing: 0.03em
  reading-zh:
    fontFamily: PingFang TC, Microsoft JhengHei, Noto Sans TC, sans-serif
    fontSize: 17px
    fontWeight: '400'
    lineHeight: '1.9'
    letterSpacing: 0.03em
  reading-en:
    fontFamily: -apple-system, Segoe UI, Roboto, sans-serif
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.7'
  button:
    fontFamily: PingFang TC, Microsoft JhengHei, Noto Sans TC, sans-serif
    fontSize: 15px
    fontWeight: '500'
    letterSpacing: 0.08em
  nav:
    fontFamily: PingFang TC, Microsoft JhengHei, Noto Sans TC, sans-serif
    fontSize: 14px
    fontWeight: '400'
    letterSpacing: 0.04em
  caption:
    fontFamily: PingFang TC, Microsoft JhengHei, Noto Sans TC, sans-serif
    fontSize: 13px
    fontWeight: '400'
    letterSpacing: 0.04em
  label-mono:
    fontFamily: DM Mono
    fontSize: 12px
    fontWeight: '400'
    letterSpacing: 0.1em
  label-mono-reading:
    fontFamily: DM Mono
    fontSize: 13px
    fontWeight: '400'
    letterSpacing: 0.08em
  micro-mono:
    fontFamily: DM Mono
    fontSize: 11px
    fontWeight: '400'
    letterSpacing: 0.1em
  table-number:
    fontFamily: DM Mono
    fontSize: 14px
    fontWeight: '400'
rounded:
  none: 0px
  full: 999px
spacing:
  '1': 4px
  '2': 8px
  '3': 12px
  '4': 16px
  '6': 24px
  '8': 32px
  '12': 48px
  '16': 64px
  '24': 96px
  gutter-desktop: 96px
  gutter-tablet: 48px
  gutter-mobile: 20px
  reading-width: 760px
  rail-width: 248px
  button-height: 50px
  button-height-compact: 44px
  tap-target: 44px
components:
  button-primary:
    background: '{colors.text-primary}'
    color: '{colors.on-button}'
    typography: '{typography.button}'
    height: '{spacing.button-height}'
    rounded: '{rounded.full}'
  button-secondary:
    background: transparent
    color: '{colors.text-primary}'
    border: '1px solid {colors.border}'
    typography: '{typography.button}'
    height: '{spacing.button-height}'
    rounded: '{rounded.full}'
  text-link:
    color: '{colors.accent-gold}'
    typography: '{typography.nav}'
    tapTarget: '{spacing.tap-target}'
  nav-bar:
    color: '{colors.text-secondary}'
    typography: '{typography.nav}'
    brandTypography: '{typography.brand}'
  scope-label:
    color: '{colors.text-tertiary}'
    typography: '{typography.label-mono}'
  data-date-line:
    color: '{colors.text-secondary}'
    snapshotColor: '{colors.text-tertiary}'
    typography: '{typography.label-mono}'
  readout:
    numberTypography: '{typography.readout-number}'
    unitTypography: '{typography.unit-zh}'
    labelTypography: '{typography.label-mono}'
    divider: '{colors.hairline}'
  ai-badge:
    color: '{colors.text-secondary}'
    typography: '{typography.label-mono}'
  coverage-mark:
    color: '{colors.text-secondary}'
    warningColor: '{colors.accent-gold}'
    typography: '{typography.label-mono}'
  status-tag:
    color: '{colors.text-secondary}'
    border: '1px solid {colors.border}'
    typography: '{typography.label-mono}'
    rounded: '{rounded.none}'
  classification-tag:
    color: '{colors.text-reading}'
    hoverColor: '{colors.text-primary}'
    border: '1px solid {colors.border}'
    hoverBorder: '1px solid {colors.text-primary}'
    typography: '{typography.label-mono-reading}'
    height: 26px
    rounded: '{rounded.full}'
  caution-line:
    color: '{colors.text-tertiary}'
    typography: '{typography.caption}'
  paper-row:
    titleTypography: '{typography.paper-title-row}'
    titleColor: '{colors.text-primary}'
    metaTypography: '{typography.label-mono}'
    metaColor: '{colors.text-tertiary}'
    divider: '{colors.hairline}'
  summary-block:
    labelTypography: '{typography.label-mono-reading}'
    labelColor: '{colors.text-secondary}'
    textTypography: '{typography.reading-zh}'
    textColor: '{colors.text-reading}'
    divider: '{colors.hairline}'
  filter-bar:
    typography: '{typography.caption}'
    chipBorder: '1px solid {colors.border}'
    chipSelectedBackground: '{colors.text-primary}'
    chipSelectedColor: '{colors.on-button}'
    chipRounded: '{rounded.full}'
  text-input:
    background: transparent
    color: '{colors.text-primary}'
    borderBottom: '1px solid {colors.border}'
    focusBorder: '{colors.accent-gold}'
    typography: '{typography.body}'
  data-table:
    headerTypography: '{typography.label-mono-reading}'
    headerColor: '{colors.text-secondary}'
    numberTypography: '{typography.table-number}'
    rowDivider: '{colors.hairline}'
  trend-chart:
    lineColor: '{colors.data-white}'
    contextLineColor: '{colors.data-dim}'
    axisColor: '{colors.hairline}'
    cursorColor: '{colors.accent-gold}'
    labelTypography: '{typography.micro-mono}'
  popover:
    background: '{colors.popover-surface}'
    border: '1px solid {colors.border}'
    color: '{colors.text-primary}'
    typography: '{typography.caption}'
    rounded: '{rounded.none}'
  overlay:
    scrim: '{colors.scrim}'
    background: '{colors.background}'
    border: '1px solid {colors.border}'
    rounded: '{rounded.none}'
  notice:
    color: '{colors.text-secondary}'
    errorColor: '{colors.danger}'
    typography: '{typography.body}'
    divider: '{colors.hairline}'
  drop-zone:
    border: '1px dashed {colors.border}'
    activeBorder: '1px solid {colors.accent-gold}'
    typography: '{typography.body}'
  skeleton:
    color: '{colors.hairline}'
  galaxy:
    spec: docs/ux/homepage-visual-spec.md §5–§7
    starWarm: '{colors.data-warm}'
    starWhite: '{colors.data-white}'
    starCool: '{colors.data-cool}'
    starDim: '{colors.data-dim}'
    ring: '{colors.data-ring}'
    instrument: '{colors.accent-gold}'
---

# Research Radar 視覺設計

這份文件定義 Research Radar **長什麼樣子**。怎麼運作（畫面清單、狀態、流程）在同一個資料夾的 `EXPERIENCE.md`。

**和首頁規格的關係。** 示範站首頁第一屏（桌機、深色）已經定案並鎖定，規格在 `docs/ux/homepage-visual-spec.md`（以下稱「首頁規格」），參考畫面在 `docs/ux/reference/`。這份文件不重新設計它，只把首頁規格 §2 的設計原則和 §3 的設計代幣（design token：全站共用的一組固定數值，像油漆的色號）整理成全站可以引用的名稱，再往其他畫面延伸。兩份文件有出入時，首頁第一屏以首頁規格為準。

**哪些是定案、哪些是延伸。** 上方代幣裡，來自首頁規格的是定案值。下面這些是這份文件新增的延伸值，Kevin 還沒看過畫面，設計稿出來後才算確認：

| 延伸的代幣 | 為什麼需要 |
|---|---|
| `colors.text-reading` | 閱讀類畫面的長段文字。暖白 `#EFEAE0` 對近黑底的對比是 16.9 : 1，長時間讀會刺眼，降到 13.2 : 1 |
| `colors.danger` | 錯誤訊息。首頁沒有錯誤狀態，所以原本沒有這個顏色 |
| `colors.popover-surface`、`colors.scrim` | 說明框的底色沿用首頁快照編號說明框的值；`scrim` 是彈出視窗後面壓暗頁面的那一層 |
| `colors.data-*` | 星星的顏色原本只寫在首頁規格 §5.3，這裡取名字，讓圖表可以沿用 |
| `typography.page-title-zh`、`section-title-zh`、`paper-title`、`paper-title-row`、`reading-zh`、`reading-en`、`table-number` | 閱讀類畫面需要的字級 |
| `spacing` 全部（`gutter-desktop`、`button-height`、`tap-target` 除外） | 首頁規格只定了左右留白與按鈕高度 |

## Brand & Style

Research Radar 是一台觀測儀器：它把 14 本資管期刊的論文攤開給人看，每個數字都點得回原論文。視覺要傳達的是**安靜、精準、可查證**，像專業天文星圖，不像太空遊戲，也不像學校系統或後台。

全站遵守首頁規格 §2 的九條原則。它們在其他畫面的意思：

1. **星星要是資料，不是桌布。** 任何畫面上的光點、線、長條都對應真的資料。不放純裝飾的星空、星雲、插圖。
2. **白與藍是資料，金色是儀器。** 資料（星星、趨勢線、長條、表格裡的數字）用暖白到冷藍。儀器（游標、十字線、選取中的年份、連結、焦點框、系統對資料提出的警告）用金色。
3. **功能名稱用白話，比喻只放在視覺。** 按鈕寫「找論文」「加入我的文獻庫」，不寫「觀測」「收進星圖」。
4. **資料上方不放會蓋住資料的面板。** 說明放在固定位置；圖表旁只能有小標籤。
5. **同一件事畫面上只講一次。**
6. **誠實呈現資料缺口。** 沒有摘要、覆蓋率低、證據只來自摘要，都照實畫出來，不修飾掉。
7. **安靜、精準。** 沒有霓虹、沒有粗重的膠囊按鈕、沒有大面積色塊。
8. **實心是完整的，空心是不完整的。** 實心點、實線代表資料完整；空心點、虛線、只有外框代表資料不完整或證據較弱。
9. **畫面上有的就要能用，還不能用的先不放。** 不畫按了沒反應的按鈕、連結或圖示，也不畫「即將推出」的入口。看起來可以點的就要點得進去；點不進去的數字（百分比、0 篇）不畫成連結的樣子。

首頁規格 §2.1 的否決清單全站適用。

### 兩類畫面

每個畫面屬於其中一類，分類寫在 `EXPERIENCE.md` 的資訊架構表。

| | 探索類 | 閱讀類 |
|---|---|---|
| 目的 | 第一眼看到全貌、引起興趣 | 讀懂內容、比對數字、長時間使用 |
| 背景 | `{colors.background}` 加右側的橢圓漸層、背景小星、顆粒質感 | 純 `{colors.background}`，沒有漸層、沒有星空、沒有顆粒 |
| 資料的畫法 | 資料畫成星星，完整使用首頁規格 §5–§7 的畫法 | 資料畫成表格、清單、趨勢線；沿用實心／空心與配色 |
| 動態 | 進場、自轉、流星（首頁規格 §6） | 只有 0.2–0.25 秒的淡入與滑過回饋，沒有裝飾性動態 |
| 文字 | 字少，大標題與大數字 | 內文用 `{typography.reading-zh}`，欄寬不超過 `{spacing.reading-width}` |
| v1 的畫面 | 示範站首頁 | 其他全部 |

v1 的 Must 畫面裡只有示範站首頁是探索類。之後的 2D 鄰居圖、3D 全景網路圖、論文地圖、研究雷達期刊版（都是 Should 或 Could）屬於探索類，到時沿用首頁的畫法。

### 還沒設計的

- **淺色主題。** Kevin 已決定全站深淺兩套主題可切換，但淺色版還沒設計。上面的顏色代幣全部是深色主題的值；代幣名稱描述的是用途（`text-primary`、`data-white`），淺色版出來時換值不換名。在那之前，示範站和成員版的導覽列都不放主題切換圖示（原則 9；Kevin 2026-11-12 決定）。
- **中文標題字體。** 見 Typography。
- **首頁手機版、首頁第一屏以下、星系的立體感。** 見首頁規格 §9。手機版設計好之前的暫時做法（星系照樣進場與自轉，拿掉指到星星與那行提示，跑不順時換成靜止版）也寫在那裡。

## Colors

| 代幣 | 值 | 用在哪裡 | 不用在哪裡 | 對背景的對比 |
|---|---|---|---|---|
| `background` | `#050609` | 所有頁面的底色 | — | — |
| `background-glow-inner`、`background-glow-mid` | `#0B0F19`、`#07090F` | 探索類畫面右側的橢圓漸層，亮區中心對準星系。完整寫法見首頁規格 §3.1 | 閱讀類畫面 | — |
| `text-primary` | `#EFEAE0` | 標題、數字、論文標題、主要按鈕的底色 | 長段內文 | 16.9 : 1 |
| `text-reading` | `#D5D0C5` | 閱讀類畫面的長段文字（中文重點、原文摘要、問答的回答） | 標題、探索類畫面 | 13.2 : 1 |
| `text-secondary` | `#A8A294` | 一般內文、說明、導覽連結 | — | 8.0 : 1 |
| `text-tertiary` | `#8E887B` | 小標籤、頁尾、快照編號、警語列 | 需要讀很久的內容 | 5.7 : 1 |
| `accent-gold` | `#E3B466` | 儀器與強調：連結、焦點框、圖表的游標與十字線、選取中的項目、覆蓋率警告、首頁金色標題行 | 資料本身（星星、趨勢線、長條）；大面積底色 | 10.6 : 1 |
| `on-button` | `#0A0B0E` | 暖白按鈕與選取中篩選標籤上的字 | — | 16.4 : 1（對暖白） |
| `hairline` | `#EFEAE0` 不透明度 16% | 分隔線、表格列線、圖表座標軸 | 外框 | — |
| `border` | `#EFEAE0` 不透明度 26% | 次要按鈕、輸入框、說明框、狀態標記的框 | — | — |
| `popover-surface` | `#050609` 不透明度 92% | 說明框、選單的底 | — | — |
| `scrim` | `#050609` 不透明度 72% | 彈出視窗後面壓暗頁面 | — | — |
| `data-warm`、`data-white`、`data-cool` | `#FFECD0`、`#F4F5FA`、`#C4D6FF` | 資料。星系裡由內圈到外圈；圖表的主要線條用 `data-white` | 按鈕、文字 | 17.5、18.6、13.9 : 1 |
| `data-dim` | `#7E8CAC` | 不完整或作為背景的資料：沒有摘要的星、圖表裡沒被選取的線 | — | 6.0 : 1 |
| `data-ring` | `#8A98BA` | 星系上的空心圈 | — | 7.0 : 1 |
| `danger` | `#E58B7B` | 錯誤：上傳失敗、表單錯誤、載入失敗 | 警語、覆蓋率警告（那些用金色，因為不是出錯） | 8.0 : 1 |

規則：

- 全站只有金色一個強調色。沒有綠色的「成功」：成功用實心點加一句話表示。
- 警告和錯誤是兩件事。「可能是資料缺口，不是研究缺口」是儀器在提醒資料的限制，用金色的空心標記；「上傳失敗」是出錯，用 `danger`。兩者都要有文字，不能只靠顏色。
- 對比數字用 WCAG 的公式計算。所有文字用的顏色都超過 4.5 : 1。

## Typography

四種字體，各有固定的角色：

| 角色 | 字體 | 用在哪裡 |
|---|---|---|
| 中文標題 | **待選**（原型用 Songti TC，字重 300） | 大標題、頁面標題、區塊標題、「篇」 |
| 中文內文 | 系統黑體（PingFang TC、微軟正黑體、Noto Sans TC） | 內文、導覽、按鈕、說明、所有會變動的中文內容 |
| 英文與數字展示 | Instrument Serif（含斜體） | 品牌名、大數字、英文論文標題 |
| 小標籤 | DM Mono | 編號、日期、欄位名稱、表格裡的數字、圖表刻度 |

**中文標題字體待選**（Kevin 2026-11-12 決定，首頁規格 §3.2、§11 第 1 項）：

- 正式版內嵌一套開放授權、可以合法內嵌的中文字體，只打包標題用到的字（子集字型：只含用得到的那些字的精簡版字型檔）。
- 不限宋體，好看且適合就可以；但不能是預設感的黑體大標題（首頁規格 §2.1 否決過）。
- 動手實作首頁之前，把 2–3 套候選字和原型的 Songti TC 並排渲染成截圖給 Kevin 挑。
- 內嵌的標題字只用在固定的介面文字。會變動的中文（中文重點、構念的中文標籤、問答的回答）用內文字體，否則會缺字。
- 例外：示範站首頁發現卡的關係名稱（`{typography.finding-zh}`）是會變動的內容。示範站是事先產生的快照，產生時把用到的字一起打包；成員版顯示構念名稱時用內文字體 `[ASSUMPTION]`（這是這份文件提的做法，選字體時一起確認）。
- 頁面標題裡會變動的部分也照這條：證據簡報的標題「構念 A → 構念 B」、論文清單的標題「ERP · 2021 年 · 48 篇」，大小照 `{typography.page-title-zh}`，字體用內文字體。

字級：

| 代幣 | 大小／行高 | 用在哪裡 | 來源 |
|---|---|---|---|
| `display-zh` | 60／1.3 | 示範站首頁大標題 | 首頁規格 |
| `readout-number` | 68／1.05 | 讀數的大數字（等寬數字） | 首頁規格 |
| `page-title-zh` | 40／1.3 | 閱讀類畫面的頁面標題 | 延伸 |
| `paper-title` | 34／1.25 | 論文頁的論文標題 | 延伸 |
| `finding-zh` | 32／1.35 | 發現卡的關係 | 首頁規格 |
| `paper-title-hover` | 25／1.25 | 首頁資訊區的論文標題 | 首頁規格 |
| `brand` | 24 | 品牌名 | 首頁規格 |
| `section-title-zh` | 24／1.4 | 頁面裡的區塊標題 | 延伸 |
| `subtitle-en` | 23／1.2 斜體 | 首頁英文副標 | 首頁規格 |
| `paper-title-row` | 21／1.3 | 論文列的標題 | 延伸 |
| `unit-zh` | 19 | 大數字後面的「篇」 | 首頁規格 |
| `reading-zh` | 17／1.9 | 閱讀類的長段中文 | 延伸 |
| `body` | 16／1.9 | 一般內文 | 首頁規格 |
| `reading-en` | 16／1.7 | 英文原文摘要 | 延伸 |
| `button` | 15，字重 500 | 按鈕 | 首頁規格 |
| `nav` | 14 | 導覽、連結 | 首頁規格 |
| `table-number` | 14 | 表格裡的數字（等寬） | 延伸 |
| `caption` | 13 | 說明小字、警語列、說明框 | 首頁規格 |
| `label-mono-reading` | 13 | 閱讀類畫面的欄位名稱、輔助欄的小標題、分類標籤 | 延伸（論文頁定稿，2026-11-12） |
| `label-mono` | 12 | 小標籤、日期列；探索類畫面的欄位名稱 | 首頁規格 |
| `micro-mono` | 11 | 頁尾、圖表刻度、星星旁的標籤 | 首頁規格 |

規則：

- 英文論文標題一律用 Instrument Serif，不翻成中文。
- 英文原文摘要用系統無襯線字（`reading-en`），不用 Instrument Serif：那是展示用的字，小字長段不好讀。
- 手機上 `display-zh` 與 `readout-number` 要縮小，數值在首頁手機版設計時決定；`page-title-zh` 在手機上是 28／1.3，`paper-title` 是 26／1.25。內文字級不縮。
- 內文最小 13px，只有 DM Mono 的小標籤可以到 11px。

## Layout & Spacing

- 間距只用 `spacing` 裡的值：4、8、12、16、24、32、48、64、96。
- 頁面左右留白：桌機 `{spacing.gutter-desktop}`、平板 `{spacing.gutter-tablet}`、手機 `{spacing.gutter-mobile}`。
- 斷點：寬度 1200px 以上是桌機，768–1199px 是平板，767px 以下是手機。
- **閱讀類畫面的版面**（論文頁定稿，2026-11-12）：
  - 主要欄最寬 `{spacing.reading-width}`；右側可以有一條輔助欄，內容寬 `{spacing.rail-width}`，左邊 32px 內距加一條直的髮線，放動作與附屬資訊。輔助欄要比主要欄輕：裡面的按鈕高 `{spacing.button-height-compact}`、字 14px，不用 `{spacing.button-height}`。
  - 輔助欄在捲動時固定在畫面上（離頂端 32px），往下讀的時候動作一直按得到。
  - 兩欄版面用到寬度 900px 為止，不照上面的平板斷點切：1232px 以上左右留白是 `{spacing.gutter-desktop}`；900–1231px 左右留白縮成 `{spacing.gutter-tablet}`，主要欄跟著視窗變窄，輔助欄寬度不變，兩欄至少隔 48px。
  - 899px 以下輔助欄移到主要欄第一個區塊（論文頁是書目）的下方、其餘內容之前，最常用的動作固定在畫面底部；固定在底部的按鈕維持 `{spacing.button-height}`。
  - 閱讀類畫面的欄位名稱與輔助欄的小標題用 `{typography.label-mono-reading}`、`{colors.text-secondary}`；12px 的 `{colors.text-tertiary}` 在長時間閱讀的畫面上太吃力。
  - 英文論文標題的換行要平均（`text-wrap: balance`），不讓最後一行只剩一個字。
- **表格與圖表頁**可以用到整個內容寬度，但說明文字仍限制在 `{spacing.reading-width}` 以內。
- 區塊之間用 `{spacing.12}` 的留白加一條 `{colors.hairline}` 分隔，不用卡片外框，也不用色塊。
- 每頁最上面是導覽列（上方留 30px），最下面是頁尾；有數字的頁面，資料日期列放在頁面標題的上方。
- 示範站首頁第一屏的位置與尺寸見首頁規格 §4.1，不在這裡重複。

## Elevation & Depth

- 不用陰影、不用毛玻璃、不用漸層按鈕。
- 只有兩層：頁面，以及浮在上面的說明框與彈出視窗。浮起來的東西靠 `{colors.popover-surface}` 的底加 1px `{colors.border}` 的框區分，不靠陰影。
- 彈出視窗後面用 `{colors.scrim}` 壓暗頁面。
- 探索類畫面的深度來自資料本身（星星的疏密與明暗），不來自介面元件。

## Shapes

- 兩種圓角：`{rounded.full}` 給可以按的東西（按鈕、篩選標籤、分類標籤）；`{rounded.none}` 給其他全部（說明框、彈出視窗、輸入框、狀態標記、表格）。
- 分隔一律用 1px 的髮線。
- 圖示用 1px 線條的空心圖示；實心圖示只用來表示「完整」（原則 8）。

## Components

每個元件的行為在 `EXPERIENCE.md` 的 Component Patterns，名稱兩邊相同。

| 元件 | 外觀 |
|---|---|
| **主要按鈕**（`button-primary`） | 暖白底、深色字、沒有邊框與陰影，高 `{spacing.button-height}`，全圓角，左右內距 `{spacing.8}`。滑過：往上移 1px，箭頭往右移 4px，0.25 秒。停用：不透明度 40%。一個畫面最多一個 |
| **次要按鈕**（`button-secondary`） | 透明底、1px `{colors.border}` 的框、暖白字，尺寸同主要按鈕。滑過：框變成 `{colors.text-primary}` |
| **文字連結**（`text-link`） | 金色字，沒有底線，滑過出現 1px 底線。可點範圍高度至少 `{spacing.tap-target}`。內文裡的連結固定有底線 |
| **導覽列**（`nav-bar`） | 左邊金色雷達圖示加品牌名；右邊是導覽連結（`{colors.text-secondary}`，目前所在頁是 `{colors.text-primary}` 加一條 1px 金色底線）、帳號或「成員登入」外框按鈕（高 `{spacing.tap-target}`）。主題切換圖示在淺色主題做好之前不放；做好之後放在導覽連結和帳號或「成員登入」之間。手機上連結收進選單按鈕 |
| **範圍標示**（`scope-label`） | 品牌名右邊的一行 DM Mono 小字，例如「示範站 · 靜態快照」。`{colors.text-tertiary}` |
| **資料日期列**（`data-date-line`） | 金色實心小圓點＋「資料截至 2026-11-12」＋第三層文字色的「· #12」。樣式同首頁（首頁規格 §4.2） |
| **讀數**（`readout`） | 小標籤（編號＋名稱）、大數字＋「篇」、一行說明。多個讀數並排時，上緣一條髮線、欄與欄之間一條直的髮線。數字是連結，但平常看起來和一般數字一樣（`{colors.text-primary}`、沒有底線）；滑過或鍵盤聚焦時，數字與「篇」下方淡入一條 1px、`{colors.text-primary}`、不透明度 70% 的底線（0.2 秒），游標變成手指。數值以首頁原型的 `.rr-num` 為準 |
| **AI 標章**（`ai-badge`） | 空心菱形 ◇＋「AI 整理」是未經人工核對；實心菱形 ◆＋「已核對」是人工核對過。DM Mono 小字，`{colors.text-secondary}`。同一個畫面上，未核對的 AI 標章第一次出現時寫全文「AI 生成・未經人工核對」，之後的寫「AI 整理」；已核對的一律是「◆ 已核對」，不管是不是第一次出現 |
| **覆蓋率標記**（`coverage-mark`） | 「摘要覆蓋率 98%」。前面一個小圓：50% 以上是實心，未滿 50% 是金色空心並加一個金色空心三角 △ |
| **狀態標記**（`status-tag`） | 1px 框、直角、DM Mono 小字，高 22px。例如「追蹤範圍外」「只有你看得到」「本資料庫中少見（≤2 篇）」「合併未完成」「資料不足」。全部同一個樣式，不用顏色區分種類 |
| **分類標籤**（`classification-tag`） | 論文頁上的主題與方法標籤，可以點，帶入這個分類當條件：成員版到找論文，示範站到論文清單（找論文是成員功能）。一篇可以有多個主題或多個方法（PRD FR-15 多標籤），每個標籤各自可以點，放不下就換行。全圓角、1px 框、高 26px、`{typography.label-mono-reading}`、`{colors.text-reading}`，字後面一個小箭頭 ›。滑過：框與字變成 `{colors.text-primary}`，沒有底線；提示文字「看全部『電子商務』的論文」。「未分類」不是一個分類，不能點：直角、`{colors.hairline}` 的框、`{colors.text-tertiary}` 的字，沒有箭頭 |
| **警語列**（`caution-line`） | 一行 `{typography.caption}`、`{colors.text-tertiary}` 的字，前面一個空心圓圈 i 圖示。固定在它說明的圖表或表格正下方 |
| **論文列**（`paper-row`） | 第一行：論文標題（`{typography.paper-title-row}`）。第二行：期刊簡稱 · 年份 · ● 有摘要／○ 沒有摘要，加上狀態標記（DM Mono）。第三行（有的話）：AI 標章＋中文重點的「研究問題」一欄，`{colors.text-secondary}`，最多兩行。右側是動作。示範站的論文清單裡，第二行後面接這篇的主題與方法標籤（樣式同第二行的其他小字，前面一個 AI 標章），右側改成 DOI 連結與「● 有完整內容／○ 只有書目」。列與列之間一條髮線，沒有外框 |
| **中文重點區塊**（`summary-block`） | 四欄直排：研究問題、方法、理論、發現。每欄左邊是 DM Mono 的欄位名稱（寬 96px，`{typography.label-mono-reading}`、`{colors.text-secondary}`），右邊是 `{typography.reading-zh}`、`{colors.text-reading}` 的內容；欄與欄之間一條髮線。「摘要未提及」用 `{colors.text-tertiary}`。手機上欄位名稱移到內容上方 |
| **篩選列**（`filter-bar`） | 一排小的篩選標籤：高 32px、全圓角、1px 框、`{typography.caption}`。選取中是暖白底、深色字。兩選一的切換（例如「占比／原始篇數」）是並排的兩個文字，選取中的有 1px 金色底線 |
| **文字輸入**（`text-input`） | 透明底，只有下緣一條 1px `{colors.border}` 的線；聚焦時這條線變金色。搜尋框的字級用 `{typography.section-title-zh}` 的大小（24px）但用內文字體。多行輸入（問答）是 1px 框的直角方框 |
| **資料表**（`data-table`） | 沒有外框與直線，只有列與列之間的髮線。表頭是 `{typography.label-mono-reading}`、`{colors.text-secondary}`（和閱讀類畫面的其他欄位名稱一致）。數字靠右、用 `{typography.table-number}`。列高至少 `{spacing.12}`。滑過的列底色是 `{colors.text-primary}` 不透明度 4%。展開的列左邊有一條 1px 金線 |
| **趨勢圖**（`trend-chart`） | 小倍數圖（把同一種小圖排成一排來比較，一個主題一張，座標軸相同）。完整年度：`{colors.data-white}` 1.5px 實線＋實心點。未滿一年的當年度：虛線＋空心點；長條模式只畫外框。比較用的背景線：`{colors.data-dim}` 1px。座標軸與格線：髮線。滑鼠所在的年份：一條 1px 金色直線加金色刻度字。不用填色漸層、不用圖例色塊（線的旁邊直接標名稱） |
| **說明框**（`popover`） | `{colors.popover-surface}` 的底、1px 框、直角、`{typography.caption}`、內距 7px 11px，淡入 0.2 秒。最寬 320px |
| **彈出視窗**（`overlay`） | 後面是 `{colors.scrim}`；視窗本身是 `{colors.background}` 的底加 1px 框、直角，右上角一個關閉按鈕（可點範圍 `{spacing.tap-target}`）。桌機最寬 960px，手機滿版 |
| **訊息條**（`notice`） | 沒有底色與外框。左邊一個圖示（空心圓圈 i、金色空心三角、或 `{colors.danger}` 的空心圓圈 !），右邊一句標題（`{colors.text-primary}`）加一句說明（`{colors.text-secondary}`），需要時再加一個文字連結。用在空的、錯誤、找不到依據這些狀態 |
| **上傳區**（`drop-zone`） | 1px 虛線框、直角、內距 `{spacing.12}`。檔案拖到上方時框變成金色實線 |
| **骨架畫面**（`skeleton`） | 載入時先畫出版面的灰色長條，佔住之後內容的位置。長條是 `{colors.hairline}`，不閃動（閃動屬於裝飾性動態） |
| **星系圖**（`galaxy`） | 見首頁規格 §5–§7，不在這裡重複。顏色對應：內圈 `{colors.data-warm}`、中段 `{colors.data-white}`、外圈 `{colors.data-cool}`、沒有摘要 `{colors.data-dim}`、空心圈 `{colors.data-ring}`、掃描線與時間軸 `{colors.accent-gold}` |

鍵盤焦點（所有可以操作的元件）：1px 金色外框，外擴 4px。

## Do's and Don'ts

| 要 | 不要 |
|---|---|
| 每個光點、線、長條都對應真的資料 | 放裝飾用的星空、星雲、行星、插圖 |
| 白與藍畫資料，金色畫儀器與強調 | 用金色畫資料，或再加第二個強調色 |
| 用髮線與留白分區 | 用卡片外框、色塊、陰影、毛玻璃 |
| 實心表示完整，空心與虛線表示不完整 | 用紅黃綠表示資料品質 |
| 閱讀類畫面用純色底，長段文字用 `{colors.text-reading}` | 在閱讀類畫面放星空背景或進場動畫 |
| 沒有摘要、覆蓋率低、未滿一年，照實畫出來 | 把缺口修飾掉，或推估數字補上 |
| 按鈕用細字重、全圓角、細框 | 粗重的膠囊按鈕、漸層按鈕 |
| 大標題用細的標題字體 | 預設感的黑體大標題 |
| 有數字的地方附資料日期列 | 讓裝飾性的動態改動任何數字（首頁規格 §11 第 4 項） |
| 放上去的按鈕、連結、圖示都能用 | 放還不能用的圖示（例如淺色主題做好之前的主題切換圖示）、「即將推出」的入口 |
| 警語用一行安靜的小字，固定在它說明的東西下方 | 整頁大段警告、紅色警告框 |
| 圖表用小倍數圖，線旁直接標名稱 | 一張圖塞六種顏色的線再配圖例；用動畫當分析工具 |
| 任何視覺元素只代表一種資訊 | 用線粗、顏色深淺暗示效果大小（PRD FR-39：支持不是效果大小） |
