# ADR-0039：研究模型擷取 prompt v2：量級縮寫讀不懂、結果原文對得上關係的放寬

- 狀態：提議
- 日期：2026-11-15
- 對應：第四輪 0-A；上一輪 `docs/runs/epic-2a/SUMMARY.md` 第五節的 2.6-13、2.6-14（#60 的決定 13、14）；PRD FR-3（擷取規則 1「報告了結果才判定」）、NFR-4；ADR-0034（第 4、5、12 點與取捨「結果原文的判準是字面跡象」）、ADR-0030（prompt 登記只增不改）

## 背景

Story 2.6（ADR-0034）合併後留下兩個要 Kevin 決定的事。Kevin 2026-11-15 把它們交給規劃端判斷，兩個都選 B：

- **決定 13**：prompt v1 的規則 5 要模型「照原本的值寫，不換算」，但程式（`text_checks.structured_numbers`）只接受完整量級：摘要寫 `12 million` 時模型照字面填 12 會整篇拒收。反過來，摘要寫 `12M`、`10k` 時程式讀不出量級、只讀到 12，模型填 12 反而會過。兩邊不一致。
- **決定 14**：2.6 第二輪修正要求「結果原文要提到這條關係的因或果」，擋住了拿無關句子當結果的情況，但也把 `H1 was supported`、`All hypotheses were supported`、`Results support H1 and H2`、只用縮寫的 `PU significantly affects TR (β = 0.42)` 這類真的有結果的寫法降成「沒寫結果」；`we examine whether X and find that Y` 這種同一句先寫研究問題、後面才接結果的，也被當成「只提出假設」。

目前沒有任何論文跑過 AI（2.10 才第一次用真的 key），所以改了不需要重跑。

## 考慮過的選項

### 決定 13：`12M` 這類縮寫與規則 5

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 先不動 | 不用改 | prompt 和程式不一致；`12M` 會讓模型填的 12 通過，存進一個錯的樣本數 |
| B | 改規則 5（量級寫成完整的數，縮寫填 null），程式把縮寫當成讀不懂 | 前後一致；縮寫不會再存成錯的數 | 要升 prompt 版本；縮寫的量級一律拿不到數字 |

### 決定 14：結果原文要多嚴

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 維持嚴格（一定要提到因或果的名稱） | 只會少判，不會亂判 | 代號、全部假設、縮寫、研究問題後接結果這幾種常見寫法都拿不到判定 |
| B | 放寬：代號對得上、全部假設、研究問題後面另一個子句的結果、縮寫對得上構面，都算 | 這幾種常見寫法拿得到判定；仍然擋無關的句子 | 判準更複雜，有新的誤判可能（見取捨） |
| C | 拿掉這一關（回到只看「寫出了結果」） | 最簡單 | 拿另一條關係的結果當證據的情況又會放行 |

## 決定

1. **prompt 升成 `extract_model` v2**（`prompts/extract_model/v2.md`，`domain/prompt_hashes.py` 登記、`CURRENT_PROMPT_VERSIONS` 改成 2）。v1 的檔與雜湊不動。只改兩條規則，其餘（設定區 low／2560／不思考、schema `ResearchModelOutput`、情境值清單、範例）和 v1 一字不差，所以萃取 schema 版本（`EXTRACTION_SCHEMA_VERSION = 1`）不變：
   - **規則 5**：數字寫成完整的數。千分位逗號拿掉；英文單位詞寫的量級要乘進去（`12 million` 寫 12000000、`3.5 thousand` 寫 3500）；用字母縮寫寫的量級（`12M`、`10k`、`3.5K`、`2B`）看不出確切的數，那個欄位填 null。拿掉「不換算」，改成「除此之外照輸入的值寫，不四捨五入、不換單位」。
   - **規則 7**：補一句結果原文要看得出是這條關係的結果（寫到因或果的全名或縮寫，或寫了原句裡的假設代號，或說全部假設都成立而原句是一條假設）。
2. **字母縮寫的量級讀不懂**（`text_checks._ABBREVIATED_MAGNITUDE`）：`structured_numbers` 讀輸入前，把「數字＋（最多一個空白）＋ k、m、b、bn（不分大小寫），後面不再接字母、數字或連字號」整段換成空白，照科學記號的做法不取數字：`12M` 不會變成 12，也不會變成 12000000。數字前面接字母、數字、`.`、`,` 的不算（`H2b`、`B2B` 的 2 照常讀得到）；字母後面再接字母、數字或連字號的不算（`12 months`、`5 MB`、`312 K-12 teachers`）。同一個數字在輸入別處寫清楚了照常可以用。只改結構化數值；英文名、繁中標籤與中文重點（2.4）用的 `numbers(keep_unscaled=True)` 不變。
3. **研究問題後面的結果也算**（`research_model._reports_result`）：2.6 的「只提出假設」寫法拆成兩組，合起來和原本一樣：假設句（we hypothesize／expect／predict／propose／posit、is expected／proposed… to、hypothesized that、H1 posits…、`H1:`）與研究問題（we test／examine／investigate／explore／ask whether）。有假設句寫法的照舊不算。只有研究問題的，要在**最後一個研究問題後面另一個子句**寫了**明確的結果寫法**才算：
   - 明確的結果寫法：find／show／reveal／indicate／demonstrate／confirm… `that`、was／were（not、partially…）supported／confirmed／rejected、find（no、strong…）support／evidence。只寫 results、significant、support 不算，因為研究問題本身常帶這些字（`we test whether trust significantly increases loyalty`、`whether H1 is supported`）。
   - 另一個子句：結果寫法和研究問題之間要有斷點（逗號、分號、冒號、` - `、句尾、and、but、then），而且最後一個斷點到結果寫法之間只能是空的或一個主詞（we、it、they、results、the findings、data、H1、this hypothesis…，可以再接 also）。
4. **結果原文對得上這條關係，四種都算**（`research_model._result_quote`）。前提和 2.6 一樣：有文字、正規化後在輸入裡、寫出了結果（上一點）。
   1. **提到因或果的名稱**（2.6 原本的規則）。
   2. **提到因或果的縮寫**：結果原文裡至少兩個大寫字母的字（`PU`、`TR`、`eWOM`；所以 `it`、`It`、`H1` 都不是縮寫），而且是那個構面的縮寫：英文名的首字母（全部的詞或跳過虛詞，至少兩個字母；`Trust` 沒有），或輸入裡寫了「名稱 (縮寫)」而括號前面的文字正規化後以這個構面的名稱結尾（`… trust (TR)`；名稱看正規化名稱，也看英文名拿掉括號後的正規化名稱，所以模型取名 `Trust (TR)` 也對得上）。好幾個構面都對得上時只給名稱最長的（`satisfaction with trust (ST)` 的 ST 給 Satisfaction with trust，不給 Trust）。模型自己在英文名括號裡寫、輸入沒有這樣定義的縮寫不算（免得模型改名字就能把別的句子算成這條關係的結果）。輸入裡只看前 200 個縮寫括號（擋故意塞滿括號拖慢驗證）。
   3. **假設代號**：原句裡剛好只有一個代號（`H1`、`H2a`，H 後面最多兩位數字與一個字母；同一個代號出現多次也算一個），結果原文也寫了同一個代號（`H1 was supported`、`Results support H1 and H2`），而且結果原文有明確的判定字（support、confirm、reject、accepted、significant…）或統計量。代號要完全一樣：H1 不等於 H1a、H10；`H1–H3` 這種範圍不展開。原句有好幾個不同的代號時分不出這條關係是哪一個，不判。
   4. **全部假設**：結果原文寫 `all (of the) (…) hypotheses`，有明確的判定字或統計量，而且沒有 not、but、except、other than、however、partially、most、some、only 這類字；這條關係的原句本身是一個假設（有代號，或有假設句的寫法；只寫 `we examine whether` 的研究問題不算）。
   3、4 另外要**原句同時提到這條關係的因和果**（名稱或縮寫），確定「這條關係就是那個假設」；只提到其中一個時，可能是同一個構面的另一條假設（`H1: PU affects trust` 配 trust → loyalty 不算）。
5. **驗證器不分 prompt 版本**：和 ADR-0034 一樣，`ResearchModelValidator` 只拿情境值清單，不看 prompt 版本，所以第 2～4 點對 v1 的結果也一樣適用。目前沒有任何論文跑過 AI、資料庫裡沒有 v1 的結果，不需要重跑；之後有 v1 的結果時（不會有，預設已經是 v2），`services/ai_results.py` 寫入前的驗證也照新規則。
6. **其他 2.6 的決定都不動**：ADR-0034 的狀態與決定文字不改；這份 ADR 補充它第 4、5、12 點在 v2 之後的做法。

## 理由

- 決定 13：讓 prompt 和程式說同一件事。縮寫的量級讀不出確切的數（`M` 也可能是別的單位），讀成 12 或猜成 12000000 都可能存進錯的數；讀不懂、填 null 只是少一個數字。
- 決定 14：B 讓常見的代號、全部假設、縮寫寫法拿得到判定，同時保留「結果原文要對得上這條關係」這個擋無關句子的關卡。不確定的情況（原句有好幾個代號、全部假設有例外、研究問題和結果寫法在同一個子句）一律不判，錯也是錯在少判。
- 只放寬研究問題、不放寬假設句：假設句常寫 `H1: X significantly affects Y`、`we hypothesize that X significantly…`，後面的 significant 不是結果；研究問題之後另一個子句的 find that、was supported 才是。

## 取捨

- **結果原文的判準仍是字面跡象**。v2 之後還是抓不到、會判成沒寫結果的：`Hypothesis 1 was supported`（寫成全名的代號）、`H1–H3 were supported`（範圍不展開）、原句有兩個以上代號的關係、原句沒有代號也沒有假設句寫法時的 `All hypotheses were supported`、研究問題後面沒有 that 的結果（`we examine whether X and find a positive effect`）、沒有在輸入定義的單字縮寫。這些都改成 `not_stated`（`corrected_support_count` 看得出來），交給 Epic 8 的評估量。
- **新的可能誤判**（以前會擋、現在可能放行）：
  - 代號：原句只有一個代號、結果原文寫了同一個代號與判定字，就算有結果，但程式不看結果原文是不是在說這個代號的結果（`H1 was not tested, but H2 was supported` 對 H1 那條也會放行）；方向（supported 還是 not_supported）和 2.6 一樣不核對。
  - 全部假設：原句是假設句、結果原文說全部假設都怎樣，就套到每一條假設；同一篇有好幾個研究時可能套錯。
  - 縮寫：兩個構面的首字母相同（`Perceived Usefulness` 與 `Price Uncertainty` 都是 PU）時，PU 對兩個都算提到。這和 2.6「提到因或果其中一個就算」一樣寬。
  - 研究問題：`we examine whether users find that…` 這種研究問題裡本來就有 find that 的寫法會被當成結果（要有斷點才算，`whether users find that` 前面沒有斷點，所以這個例子不會；`we examine whether X, and users find that Y` 會）。
- **縮寫的量級一律拿不到數字**：`10k users` 的論文樣本數存空值。比存錯的數好；評估時看有多少。
- **`12 m`（公尺）、`120 m and 80 f`（男女人數）、`Study 5b` 這類也會被當成讀不懂**：只影響結構化數值，頂多那個數字對不上、模型填了會拒收；prompt 規則 5 要模型這時填 null。

## 名詞

新名詞見 `docs/glossary.md`：字母縮寫的量級、假設代號、全部假設的結果原文、研究問題後的結果、構面縮寫。
