# 引用匯出的標準答案（Story 3.10）

`web/src/components/paper/citation.test.ts` 讀這個資料夾的每一個 `*.json`，比對三種格式是否逐字相同。

- 全部是**假論文**（標題都是 Fake Paper…）。
- 預期值是**先照下面的規則手寫**的，不是程式跑出來的。唯一例外是 `08-twenty-one-authors.json`：21 位作者的名單很長，是用一段 Python 把「Ann Fake01～Fake21」照下面 APA 規則第 2 點逐字展開（不是呼叫這個專案的程式）。
- `input` 是論文頁用來組引用的資料（作者是 OpenAlex 的顯示名稱，例如「Bob C. Testwriter」）。
- `apa` 是按「複製 APA 引用」放進剪貼簿的整段文字（引用加連結：有 DOI 時最後是 DOI 連結）。
- `ris` 一行一個元素，實際的檔案是每行接 CR LF（`\r\n`），最後一行後面也接。
- `bibtex` 一行一個元素，實際的檔案是每行接 LF（`\n`），最後一行後面也接。

> **第 1 輪審查修正（PR #133）補的條文**（APA 1 的後綴、APA 6 的文章編號、RIS 3 與 BibTeX 2 的後綴）：APA Style 網站這次抓下來仍是空白頁、CTAN 與 RIS 原址同上，一樣沒能逐字貼出原文，下面寫的是那幾節的意思（請一併核對）：
> - APA-PM §9.27／APA-J「有文章編號的期刊論文」例：論文沒有頁碼範圍、改用文章編號（eLocator）時，在原本頁碼的位置寫「Article」加編號，例如 `PLOS ONE, 13(3), Article e0193972.`
> - APA-PM §9.8／APA-E 作者：名字有後綴（Jr.、III）時放在名縮寫之後、中間用逗號，例如 `Author, A. A., Jr.`
> - RIS 規格 `AU`：作者寫成 `Lastname, Firstname, Suffix`。
> - BTX §4（names）：有後綴的名字寫成 `von Last, Jr, First` 三段（例如 `Ford, Jr., Henry`）；名字裡大括號外的逗號最多兩個。

> **要 Kevin 核對**：寫的時候想上官方網站逐字核對，但 APA Style 網站抓下來是空白頁（內容由瀏覽器端產生）、RIS 規格的官方網址回 403、CTAN 的 PDF 被轉到需要付費的鏡像站，所以下面的出處是**官方文件的網址與章節**，條文內容是照這些文件寫的，沒能在這次逐字貼出原文。請用瀏覽器打開對一下。

## 出處

| 代號 | 文件 | 網址 |
|---|---|---|
| APA-J | APA Style（第 7 版）：Journal article references | https://apastyle.apa.org/style-grammar-guidelines/references/examples/journal-article-references |
| APA-E | APA Style：Elements of reference list entries（作者、日期、標題、來源） | https://apastyle.apa.org/style-grammar-guidelines/references/elements-list-entry |
| APA-M | APA Style：Missing reference information | https://apastyle.apa.org/style-grammar-guidelines/references/missing-information |
| APA-D | APA Style：DOIs and URLs | https://apastyle.apa.org/style-grammar-guidelines/references/dois-urls |
| APA-PM | *Publication Manual of the APA*（第 7 版）第 9 章 | §9.8 作者（含 Jr.、III 這類後綴）、§9.12 沒有作者、§9.17 沒有日期、§9.19 標題、§9.25–9.26 期刊資訊、§9.27 文章編號（Article numbers）、§9.34–9.35 DOI、§10.1 例 1、例 6（文章編號） |
| RIS | RIS Format Specifications（Research Information Systems，Reference Manager；現在屬 Clarivate EndNote） | 原址 http://www.refman.com/support/risformat_intro.asp ，現轉到 https://endnote.com/support/risformat_intro.asp |
| BTX | Oren Patashnik, *BibTeXing*（`btxdoc`） | https://ctan.org/pkg/bibtex （文件 https://mirrors.ctan.org/biblio/bibtex/base/btxdoc.pdf ）§3.1 entry types、§4 Helpful hints（names、pages） |
| BLX | *The biblatex Package* 使用手冊 §2.2.2 Data Fields 的 `doi` | https://ctan.org/pkg/biblatex |

## 規則（每一筆的依據）

### APA（複製的文字）

1. 一位作者：`姓, 名縮寫.`；名縮寫每個字取第一個字母加句點，中間空一格（`Testwriter, B. C.`）。有連字號的名字縮寫保留連字號（`Jean-Paul` → `J.-P.`）。有後綴時接在名縮寫後面、中間逗號：`Fakesmith, R. L., Jr.`。〔APA-E 作者；APA-PM §9.8〕
2. 2～20 位：每位之間逗號，最後一位前面是「, &」（兩位也有逗號）；21 位以上列前 19 位，接「, . . . 」再接最後一位，不寫 &。〔APA-E 作者；APA-J 的 21 位作者例；APA-PM §9.8〕
3. 年份放在括號裡接句點：`(2025).`；沒有年份寫 `(n.d.).`〔APA-E 日期；APA-M；APA-PM §9.17〕
4. 沒有作者：標題移到作者的位置，後面接日期：`標題. (2022).`〔APA-M；APA-PM §9.12〕
5. 標題後面接句點；標題已經以問號或驚嘆號結尾時不再加句點。〔APA-E 標題；APA-PM §9.19〕
6. 期刊資訊：`期刊, 卷(期), 起–迄.`；頁碼之間用短破折號（en dash，`–`）；缺的部分省略（沒有期就只寫卷，只有起始頁就只寫起始頁；起始頁等於結束頁就只寫一個）。〔APA-E 來源；APA-J；APA-PM §9.25–9.26〕
   - 文章編號：沒有頁碼範圍（沒有結束頁，或結束頁等於起始頁）、而且那個值不是純數字（例如 `e101`）時，寫 `Article e101`。純數字一律當頁碼。這條「不是純數字就當文章編號」的判準是本專案自己定的（資料裡沒有標明哪個是文章編號；`S12` 這種增刊頁碼也會被寫成 Article，列給 Kevin）。〔APA-PM §9.27；APA-J 文章編號例〕
   - 沒有期刊名稱時整段來源（期刊、卷期、頁碼）都不寫（APA 的來源是期刊，沒有期刊就無從寫起；RIS、BibTeX 照有的欄位寫）。本專案自己定的，列給 Kevin。
7. DOI 寫成 `https://doi.org/…` 的網址，後面不加句點。網址用 API 給的 `doi_url`（和論文頁上的 DOI 連結同一個）；沒給時照後端 `radar/domain/contracts.py` 的 `doi_url` 同一個規則組：`/` 以外的特殊字元都編碼（`<` → `%3C`、`{` → `%7B`、`;` → `%3B`、`#` → `%23`）。資料檔不給 `doi_url`，所以是組出來的。〔APA-D；APA-PM §9.34–9.35〕
8. 純文字無法表現斜體（期刊名與卷號在 APA 是斜體）：複製的是純文字。標題保留原本的大小寫（APA 要求句首大寫，但無法從資料可靠判斷專有名詞，列給 Kevin）。

### RIS（下載 RIS）

1. 每一行是「兩個字元的標籤、兩個空白、連字號、一個空白、內容」（`TY  - JOUR`），每行以 CR LF 結尾。〔RIS〕
2. 第一行一定是 `TY`，期刊論文是 `JOUR`；最後一行一定是 `ER  - `（內容空白）。〔RIS〕
3. `AU` 一位作者一行，寫成 `姓, 名`（有後綴時 `姓, 名, 後綴`）；`TI` 標題；`T2` 期刊名稱（JOUR 的 secondary title）；`PY` 四位數年份；`VL` 卷、`IS` 期、`SP` 起始頁、`EP` 結束頁；`DO` DOI（不帶 `https://doi.org/`）。〔RIS〕
4. 沒有的欄位整行不寫（沒有 DOI 就沒有 `DO` 這一行）。內容裡的換行、所有控制字元（含 NEL U+0085）與行分隔符號（U+2028、U+2029）改成一個空白（RIS 一個欄位只能一行；有的讀取程式會把 NEL、U+2028 當換行）。

### BibTeX（下載 BibTeX）

1. 期刊論文用 `@article`；欄位 `author`、`title`、`journal`、`year`、`volume`、`number`、`pages`、`doi`；值一律用大括號包起來，每個欄位一行、前面兩個空白，最後一個欄位後面不加逗號。〔BTX §3.1；doi 見 BLX〕
2. 作者之間用 ` and `，每位寫成 `姓, 名`；有後綴時 `姓, 後綴, 名`（`Fakesmith, Jr., Robert L.`）。姓裡有空白、名字的任何一段有獨立的 and（不分大小寫，BibTeX 切作者時不分大小寫）或逗號時，那一段整段包大括號。〔BTX §4 names〕
3. 頁碼範圍用 `--`（`894--925`）。〔BTX §4〕
4. 沒有的欄位不寫（沒有作者就沒有 `author`；沒有期刊就沒有 `journal`，雖然 BTX 說 `@article` 要有，但沒有資料就不能編）。
5. 跳脫（BibTeX 讀檔時大括號一定要成對，`\{` 也會被算進去，所以大括號換成成對的指令）：`\` → `\textbackslash{}`、`{` → `\textbraceleft{}`、`}` → `\textbraceright{}`、`&` `%` `$` `#` `_` → 前面加 `\`、`~` → `\textasciitilde{}`、`^` → `\textasciicircum{}`。雙引號在大括號包的值裡不用跳脫（BTX §2「用大括號包時雙引號可以直接寫」）。非 ASCII 字元照原字（UTF-8），不轉成 `\"u` 這種寫法。`doi` 照原字（BLX：`doi` 是逐字欄位）；但有大括號或反斜線時整段照一般文字跳脫（不然 .bib 檔的大括號會壞）。
6. 引用鍵：第一位作者的姓 ＋ 年份（沒有寫 `nd`）＋ 標題第一個不是 a／an／the 的英文字；先照轉寫表換（`Ł`→`L`、`Ø`→`O`、`Æ`→`AE`、`Œ`→`OE`、`ß`→`ss`、`Đ`／`Ð`→`D`、`Þ`→`TH`、`ı`→`i`，大小寫各自對應；這些字母不會被拆成「字母＋重音」），再去掉重音符號、轉小寫、只留 `a-z0-9`；沒有作者用 `anon`。例：`Müller` → `muller`、`Fakeßtrøm` → `fakesstrom`。只有中文姓名時姓轉不出英數字，也是 `anon`（不自己發明拼音，列給 Kevin）。這是本專案自己定的規則（BTX 只要求鍵不含逗號、空白與大括號）。

## 每一筆

| 檔案 | 測的是 | 用到的規則 |
|---|---|---|
| `01-basic.json` | 三位作者、有中間名、DOI、卷期頁碼齊全 | APA 1、2、3、5、6、7；RIS 1–3；BibTeX 1–3、6 |
| `02-no-doi.json` | 沒有 DOI（State Patterns「8. 論文頁」：引用匯出照常、少 DOI 欄位） | APA 6（最後是句點）、7 不適用；RIS 4；BibTeX 4 |
| `03-two-authors-hyphen.json` | 兩位作者、連字號的名字、兩個字的名、沒有期、只有文章編號 `e101` | APA 1、2、6（文章編號）；RIS 4；BibTeX 4 |
| `04-no-author.json` | 沒有作者 | APA 4；RIS 4；BibTeX 4、6（`anon`） |
| `05-no-year.json` | 沒有年份 | APA 3（`n.d.`）；RIS 4；BibTeX 4、6（`nd`） |
| `06-no-volume-pages.json` | 沒有卷期頁碼 | APA 6；RIS 4；BibTeX 4 |
| `07-special-characters.json` | 大括號、反斜線、引號、& % $ # _ ~ ^、非 ASCII、變音符號 | APA 1；BibTeX 5、6 |
| `08-twenty-one-authors.json` | 21 位作者 | APA 2（前 19 位＋. . .＋最後一位）；RIS 3、BibTeX 2（全部列出） |
| `09-question-title-no-journal.json` | 標題以問號結尾；沒有期刊、卷期頁碼、DOI | APA 5、6；RIS 4；BibTeX 4 |
| `10-name-suffix.json` | 名字有後綴（Jr.、III、Sr.）的三種寫法 | APA 1、2；RIS 3；BibTeX 2、6 |
| `11-name-forms.json` | 「姓, 名」且姓有空白、只有一個字的名字、名裡有獨立的 and、起始頁等於結束頁 | APA 1、2、6；RIS 3；BibTeX 2、3、6 |
| `12-no-journal-odd-doi.json` | 沒有期刊但有卷期頁碼；DOI 有大括號、`<>`、`;`、`#` | APA 6（沒有期刊）、7（編碼）；RIS 3、4；BibTeX 4、5 |
| `13-key-transliteration.json` | 引用鍵的轉寫表（ß、ø、Ø） | BibTeX 6 |

第 1 輪審查修正（PR #133）：`03` 的 APA 改成 `Article e101`（原本寫成頁碼，錯）；`07` 的標題補 `$ # ~ ^`；`10`～`13` 是新的。都是先手寫預期值、再改程式（改程式前跑一次，`03`、`10`、`12`、`13` 照預期失敗）。
