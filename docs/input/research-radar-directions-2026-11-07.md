# 先砍廣度、守住驗證：Research Radar 的擴充與精進路線

*2026-11-07｜依據六份研究筆記（實驗室使用者、學術、跨產業、資料與方法、演進性、業界工程）與 PRD 討論背景｜〔估計〕＝工程判斷的工時，不是來源數據；〔未查證〕＝研究無法確認；〔假設〕＝待驗證的目標值｜工時級距：S ≤4 h、M 4–15 h、L >15 h*

## 1. 摘要：五個會改變現行計畫的結論

Research Radar 下一步最該投資的不是更多功能，而是在**第一次十年全量匯入與 LLM 萃取之前**，把幾個小而不可逆的決定做進資料骨架：資料來歷、人工修正、授權閘門與誠信呈現。它們大多是「既有 Must 要怎麼做」而不是新功能，合計淨增約 **21 小時**〔估計〕；用四項刪減支付後，Must 中位估算約 **145 小時**，落在 120–150 小時的功能預算內，悲觀情境則需要第二層刪減（第 6 節）。其餘擴充排進 Should，在春季學期同學寫提案時上線。

1. **付費牆 PDF 不能預設送進雲端 LLM。** 台灣沒有法定的文字與資料探勘（TDM）例外；智慧局 2026 年生成式 AI 著作權指引談 AI 訓練時，只把不受保護、已過期與 CC0 作品列為合理使用，「其餘利用，仍應取得著作權人授權」([工商時報](https://www.ctee.com.tw/news/20260805701974-430503))。這段雖然談的是訓練，但顯示官方立場保守。國外大學圖書館已明講多數訂閱資源不得上傳到生成式 AI 工具，違規可能讓整個校園失去存取 ([UWaterloo Library](https://uwaterloo.ca/lib/research-supports/copyright-and-licensing/use-library-resources-ai))。因此 PDF 應一律私有；只有 CC 授權的開放取用（OA）全文會自動送 LLM；付費牆全文的開關預設關閉，等圖書館回覆；實驗室只共享衍生事實（構念、假設、頁碼）。這是推論，不是法律意見。
2. **「AI 建議題目」要改成「AI 整理證據，題目自己寫」。** 中山管院 2026 年的學位論文聲明書要求學生聲明未以 AI「直接生成論文之核心內容」，而核心內容明列研究問題 ([中山管院](https://rpb73.nsysu.edu.tw/static/file/234/1234/img/777/127846497.pdf))；台大 2026 年指引把「以 AI 翻譯他人外文著作並當成自己的」列為抄襲 ([台大研究誠信辦公室](https://ori.ntu.edu.tw/ethics/download/fileSn/25))，中文摘要若被直接貼進論文就會踩線。候選題目應改名「證據簡報」，研究問題只給人寫，每個 AI 產物標註模型、日期與是否經人工核對。
3. **真正不可逆的是資料來歷與人工判斷，不是 LLM 費用。** 以 Haiku 5.5 批次價（輸入 $0.05、輸出 $0.25／百萬 token）推算，一萬篇分類重跑約 $1.25〔估計〕([Anthropic Batch](https://platform.claude.com/docs/en/build-with-claude/batch-processing))；救不回來的是「哪個模型、哪版 prompt、用哪段文字」產生了每筆資料，以及人工修正、合併決策與金標準標註。OpenAlex 也變了：2026 年 2 月起必須帶 API key、polite pool 停用 ([OpenAlex 使用者群組](https://groups.google.com/g/openalex-users/c/rI1GIAySpVQ))，改按用量計費，但每把 key 每天有 $1 免費額度、列表呼叫每次 $0.0001 ([OpenAlex 部落格](https://blog.openalex.org/author/jason))，十年全量約 $0.01–0.02〔估計〕。現在就要保留原始 JSON 與抓取日期、解析 OA 與授權欄位；Walden 改版換了 topics／keywords 演算法，burst 偵測上線前要先檢查斷層。
4. **「可計數」的定位要靠 SQL；引用存在不等於引用正確。** 在 TAG 基準（80 題針對資料庫內容、需要知識或推理的提問）中，純 RAG（檢索增強生成）的完全正確率是 0%，Text2SQL 不超過 20%，結合 SQL 與語言模型的手寫管線則達 55% ([TAG](https://arxiv.org/pdf/2408.14717))。所以「每年幾篇」必須導向 SQL 結果。Aaron Tay 的評測指出，現有工具「避免捏造引用」做得比「忠實呈現來源」好 ([Aaron Tay](https://aarontay.substack.com/p/a-2025-deep-dive-of-consensus-promises))，因此每條假設與主張都要附上程式驗證過的原句。
5. **預算打得平，3D 交給 Kevin。** 新增 12 項小型 Must（每項 S；另有 1 項營運保險計入非功能預算），用三種方式支付：上線時改用靜態快照加預先算好的展示問答，即時訪客問答延後；構念合併確認改用 CLI＋試算表；首頁動畫與主題交集延後。兩週後重估若仍超過 150 小時，再啟動第二層刪減。互動 3D 的證據偏弱，「哇」效果可以用離線算圖的飛越影片取得（第 3 節）。

## 2. 建議的整體方向：不比你的 AI 聰明，但比它可驗證

建議把定位寫成一句可檢驗的話：**Research Radar 不比你的 AI 聰明；它在一個有邊界、標明覆蓋率的 IS 文獻庫上，提供可計數、可追溯、可重現的證據，並把實驗室的人工判斷累積成可傳承的資產。** 這句話對準研究者真正的不安。Wiley 2025 年的調查中，80% 研究者使用通用型聊天機器人，只有 11% 聽過受調查的專門研究工具，擔心不準確與幻覺的比例則從 51% 升到 64% ([Wiley](https://newsroom.wiley.com/press-releases/press-release-details/2025/AI-Adoption-Jumps-to-84-Among-Researchers-as-Expectations-Undergo-Significant-Reality-Check/default.aspx))。Elsevier 2024 年的調查中，71% 期望生成式 AI 的結果只來自高品質的可信來源 ([Elsevier](https://www.elsevier.com/about/press-releases/the-insights-2024-attitudes-toward-ai-report-reveals-researchers-and-clinicians-believe-in-ai-potential-but-demand-transparency-in-order-to-trust-tools))。所有建議可歸入四根支柱：

| 支柱 | 核心承諾 | 主要對應項目（第 6 節） |
|---|---|---|
| 一、可驗證的證據 | 每個數字有覆蓋率與截至日期，每個引用有原句，模型準確率公開 | M02–M04、M09、M11、S05–S09 |
| 二、誠信與合規內建 | 預設就符合最嚴格的同儕校規；授權不明的全文不送雲端；AI 使用自動留下紀錄 | M08–M10、S14 |
| 三、嵌入既有工作流 | 接住同學已在用的 ChatGPT、Zotero、翻譯外掛，整合而不重造 | S01–S04、S13、C06、C12 |
| 四、可接手、可展示 | 學弟妹不改程式就能維護；面試官看得到成本、評估、還原演練與決策紀錄 | M05、M13、S10、S12、S15、S19 |

## 3. 各角色在意什麼

### 實驗室同學：痛點在「搜尋之後」

研究筆記整理出的困難，集中在找到論文之後：判斷 AI 給的東西能不能信、把幾十篇整理成文獻整理表、找有效量表與構念背後的理論、找出缺口，以及遵守剛出現的論文 AI 規範。例如文藻外語大學網域上一份 2026 年的國際事務系碩士論文規範，要求學生逐筆驗證 AI 提供的參考文獻，並保留「AI 透明紀錄」([WZU 規範](https://c030.wzu.edu.tw/datas/upload/files/國際事務系碩士論文生成式_AI_使用規範與自我檢核要點_中文版%20-%20Copy%201.pdf))。另一項研究發現，GPT-3.5 生成的引用有 55% 是捏造的，GPT-4 也有 18% ([Walters & Wilder](https://www.nature.com/articles/s41598-023-41032-5.pdf))。同學已經習慣問自己的 AI，最有槓桿的做法是接住這個習慣，而不是跟它比聰明。第一個是 **AI 文獻驗證器**：貼上 ChatGPT 給的文獻清單，系統比對資料庫、OpenAlex 與 Crossref，標示「在庫／存在但不在庫／資訊不符／疑似捏造」，並重用 RAG 的引用驗證器。其次是**文獻整理表匯出**（資料在萃取時就已經有了）、**RIS 匯入**（台大圖書館建議從 NDLTD 下載 RIS 再匯入 Zotero ([台大圖書館](https://tul.blog.ntu.edu.tw/archives/tag/ndltd))），以及中文查詢與「取得連結」（OA PDF、DOI、校內 OpenURL）。量表來源、題數與 α／CR／AVE 先在萃取 schema 以可空欄位預留，構念頁放 Could。翻譯、文獻管理、和 PDF 多輪聊天都已有同學在用或免費的工具（沉浸式翻譯的 Zotero 外掛、Zotero、NotebookLM），用匯出與深連結整合就好 ([沉浸式翻譯](https://immersivetranslate.com/zh-TW/zotero-translator/))。

### 學弟妹與交接：學生做的工具，多半在作者畢業那年停擺

一份 2020 年的研究追蹤了 2,396 個生物資訊網路工具：2010 年發表的只剩約一半連得上，一直正常運作的只有 31% ([NAR 2020](https://pesquisa.bvsalud.org/portal/resource/pt/mdl-33270886))。這是生物資訊領域的數據，但它是學術工具失效最接近的量化證據。一篇研究生指導知識移轉的系統性回顧，也把接班規劃列為建議 ([Mpungose 2025](https://www.emerald.com/gkmc/article/74/11/178/1267149/Exploring-knowledge-transfer-and-retention))。主機也不再「永遠免費」：Azure for Students 的額度以 12 個月計，學生身分結束就不能續 ([Microsoft Learn](https://learn.microsoft.com/en-au/azure/education-hub/azure-dev-tools-teaching/azure-students-program))；Oracle 在 2026 年 6 月未公告就把免費 Arm 額度砍半 ([InfoQ](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/))。所以交接不能只靠寫文件，要靠幾件具體的事：實驗室擁有的帳號與秘密清單（Must）；每晚備份，而且實際還原過（Must，自動演練為 Should）；管理介面可直接加 allowlist；LLM 額度用完時搜尋與瀏覽照常可用；中文 runbook；完整匯出；以及由學長姐註記「為什麼要讀」的**入門必讀合集**（Should）。公開 demo 做成靜態快照，就算主機到期，作品集也還在。

### 指導教授：要的是可監督、可重用，而不是更多 AI

IS 期刊的規範方向一致：AI 可以輔助，責任在人，文獻探討用了 AI 就要揭露。MISQE 對「未檢查 LLM 輸出」（例如幻覺文獻）的情形，會對整個作者團隊祭出一年投稿禁令 ([MISQE](https://aisel.aisnet.org/misqe/publication_ethics.html))。中央大學目前只找到 2023 年「不禁止學生使用」並規劃揭露規範的立場，沒找到學位論文專屬規定 ([NCU HESP](https://hesp.ncu.edu.tw/farmer/index.php/en/2018-05-14-05-44-10/2023-06-08-06-48-42/2023-06-07-00-53-00/585-112-chatgpt))；同儕學校卻在 2025–2026 年快速收緊，所以應以最嚴的同儕規則來設計。對老師最有價值的有四項：

- AI 標章與「證據簡報」框架（Must）。
- 一鍵產生的「方法＋AI 揭露」匯出（Should）。台大指引提到應保留關鍵 prompt、輸入、輸出、人工修正與採納理由，這正是資料來歷表記錄的內容。
- 撤稿警示（Should）。
- 「期刊趨勢反映數年前的研究」提示。MISQ 光是到第一次編輯決定就要 3–5 個月 ([MISQ](https://misq.org/review-process))。

老師專長的 ERP、供應鏈與電商，很適合做「理論×情境」矩陣。但 Hong 等人提醒，情境化理論要提出該情境特有的機制，不能只是換個樣本 ([Hong et al. 2014](https://econpapers.repec.org/RePEc:inm:orisre:v:25:y:2014:i:1:p:111-136))，介面要寫明這點。紅線：不把老師未發表的計畫書或審查中的稿件放進 AI，台大把這列為抄襲或違反保密 ([台大](https://ori.ntu.edu.tw/ethics/download/fileSn/25))。

### IS 學界：構念同一性、可重現、可比較

構念「同名異義、異名同義」（jingle-jangle）是 IS 的老問題，Larsen & Bong 2016 年已在 MISQ 提出處理工具 ([CU Boulder](https://vivo.colorado.edu/display/pubid_148364))。新一代的 ALIGNS（2025）用微調模型嵌入問卷題項，驗證資料包含 4,419 個頂級 IS 期刊題項 ([ALIGNS](https://arxiv.org/pdf/2509.09723))。Wulff & Mata 2025 用題項嵌入，把 IPIP 的 jingle-jangle 謬誤減少了 58%，但核心驗證靠的是題項而不是標籤 ([Wulff & Mata](https://pmc.ncbi.nlm.nih.gov/articles/PMC12106064))。所以每個合併決策都要記錄證據層級（標籤／定義／題項），也要能只看定義或題項層級確認的結果。

可重現性方面，PRISMA-S 列出 16 項檢索報告要素，包括完整檢索式、日期、各來源筆數與去重方法 ([PRISMA-S](https://pmc.ncbi.nlm.nih.gov/articles/PMC7839230))；Cochrane、Campbell、JBI 等組織 2025 年的聯合聲明，要求揭露 AI 的名稱、版本、日期、用途與驗證方式 ([聯合聲明](https://pmc.ncbi.nlm.nih.gov/articles/PMC12594113/))，資料來歷表足以支撐這些要求。LLM 萃取的上限也要誠實揭露：一篇 HICSS 2025 研究從 72 篇 SEM 論文萃取，完整假設的召回率只有 39.3% ([Funk & Scharfenberger](https://fis.leuphana.de/en/publications/construct-relation-extraction-from-scientific-papers-is-it-automa/))。

工具本身有機會成為設計科學研究（DSR，以建構並評估人造物作為研究貢獻）。JIT 2022 明確鼓勵針對 AI 輔助文獻回顧做 DSR ([Wagner et al. 2022](https://libraopen.lib.virginia.edu/public_view/st74cq58c))；最接近的前作 TheoryOn（MISQ 2020）報告的假設萃取 F1 為 95.25% ([TheoryOn](https://par.nsf.gov/servlets/purl/10282376))。較穩的路線是「行為研究碩論＋工具另投研討會」，台灣 ICIM 2026 的主題正好是 AI agents ([北科大](https://cmgt.ntut.edu.tw/p/16-1018-154174.php?Lang=en))。

### 業界面試官：完成度與判斷力，勝過功能數量

MLH 的整理是：「三個完成的小專案，幾乎在每次作品集審查中都勝過一個半途的大專案」，而光有 AI 流暢度已經換不到面試 ([MLH](https://news.mlh.io/the-ai-coding-skills-recruiters-are-looking-for-now-05-14-2026))。Canva 允許面試時用 AI，但評的是能否找出並修正 AI 程式碼的問題，以及能否對產出負全責 ([Canva](https://canva.dev/blog/engineering/yes-you-can-use-ai-in-our-interviews))。台積電 IT 基礎架構職缺列出單元、功能與整合測試、監控、Linux 系統與日誌的根因分析（RCA）、12-factor、Docker 與 Terraform ([104](https://www.104.com.tw/job/8qguz))；一篇台積電面試心得則提到精確率與召回率取捨的討論 ([HackMD](https://hackmd.io/@edJH8dxaRPOaOfJQPav3XQ/SJHZGHQLs))。

因此最值得展示的是：

- 金標準的 P/R/F1（精確率／召回率／F1）與混淆矩陣；
- LLM 成本帳本與每千次請求的成本；
- OWASP LLM10 的分層防護；
- 每晚備份加還原演練；
- ADR（架構決策紀錄）；
- AI 協作開發說明與退件紀錄；
- 每週一次「不靠 AI 解釋自己程式」的練習。

也要誠實面對限制：Google 已恢復至少一輪現場面試，用來確認基本功 ([Entrepreneur](https://www.entrepreneur.com/business-news/google-mckinsey-reintroduce-in-person-interviews-due-to-ai/496041))；NVIDIA 的新鮮人職缺偏 C++ 與硬體工具 ([Built In](https://builtin.com/job/software-engineer-hardware-tools-and-methodology-new-college-grad-2026/9601677))，不過這只根據一份職缺，信心低。這個專案取代不了演算法與 C++ 的準備。

### 一般大眾：10 秒看懂，而且不必點

使用者常在 10–20 秒內離開頁面，價值主張要在 10 秒內講完 ([NN/g](https://nngroup.com/articles/how-long-do-users-stay-on-web-pages/))。Gregor Aisch 引述過一個熱門互動圖表的數據：約 85% 的讀者沒有按任何按鈕 ([FlowingData](https://flowingdata.com/2017/04/05/interactive-visualization-is-still-alive/))。熟悉的隱喻比新奇的場景有效：Open Syllabus 用「地圖＋搜尋框」呈現上百萬本教材 ([Open Culture](https://www.openculture.com/2021/02/the-open-syllabus-project-visualizes-the-1000000-books-frequently-assigned-in-college-courses.html))，Spotify Wrapped 則從個人出發再拉遠 ([ADMA](https://adma.com.au/resources/personal-shareable-no-strings-attached-the-winning-formula-behind-spotify-wrapped))。Must 版首頁只要四樣東西：一句價值主張、一個附截至日期的真實數字、一張不用點就看得到的發現卡、一段 60 秒 GIF。雷達掃描動畫延到 Should，並讓它顯示「研究雷達期刊版」的真實點位（第 4 節）。

### 3D：證據攤開，由 Kevin 決定

反對互動 3D 的證據比較多。Munzner 的視覺化設計原則明言，抽象資料用 3D「需要非常謹慎的理由」，因為有遮擋、透視變形、傾斜文字難讀與導覽成本等問題 ([Munzner](https://cs.ubc.ca/~tmm/talks/vad/VAD-rules.pdf))。再加上前面提到的兩點：多數訪客不點互動，而且在 10 秒內就決定去留。ThemeScape 這類專利地圖，也是用平面上的等高線、色塊與山峰標籤來表現「地形」([ThemeScape](https://clarivate.com/intellectual-property/ja/download/di-analyst-themescape-textclustering-ja))。

支持的證據確實存在：McGuffin 等人 2022 年發現，在 VR 中手持旋轉時，34 位受試者在 3D 網路中追蹤路徑的錯誤比 2D 少，但這項研究沒有測桌機情境 ([McGuffin et al.](https://ar5iv.labs.arxiv.org/html/2207.11586))。

結論分成兩面。互動 3D 作為**分析工具**，證據薄弱。作為**「哇」效果**，則有便宜的替代方案：用同一份離線算好的座標，渲染一支 20–30 秒的飛越影片，放在首頁與 60 秒介紹影片裡（工時 S–M），手機也看得順。若 Kevin 想把 three.js／WebGL 當作技能展示，可以在 v1 上線後限時做互動版，例如 8 小時；因為座標已離線計算並版本化，加做的成本很低。建議先做影片，選擇權在 Kevin。

## 4. 跨產業可借鏡的做法，與還沒用到的資料和技術

### 4.1 跨產業：只挑值得動手的做法

**證據缺口地圖（EGM）是構念矩陣最直接的範本。** 3ie 的地圖以「列×欄」呈現介入×結果：泡泡大小代表研究數量，顏色代表研究類型或信心。它區分「絕對缺口」（幾乎沒有研究）與「綜整缺口」（研究很多，卻沒有好的回顧），並提醒「缺口只代表沒找到證據」([3ie](https://www.3ieimpact.org/sites/default/files/2024-03/how-to-read-egm-document-Feb2024.pdf))。Campbell 的製作指引補上幾條實用規則 ([Campbell](https://pmc.ncbi.nlm.nih.gov/articles/PMC8356343))：

- 地圖呈現證據在哪裡，不呈現證據說什麼；
- 一般不建議編碼效果的方向或大小；
- 計算的是獨立研究數；
- 寧可「少數大洞」，也不要「許多小洞」；
- 先用 20–30 篇試編。

套到 Research Radar，就是構念×構念的 EGM 檢視：重用既有的關係表，泡泡大小代表獨立研究數；研究數 ≥k、但庫內沒有統合分析的格子，標為「統合分析機會」（S09）。

**GRADE**（醫學界評估證據可信度的分級方法）提供了說明「為什麼證據弱」的語彙。觀察性研究的起始等級較低，降級理由有五個：偏誤風險、間接性、不一致、不精確、發表偏誤 ([GRADE](https://pmc.ncbi.nlm.nih.gov/articles/PMC6350753))。可以把它做成每條關係旁的理由標籤，標示「受 GRADE 啟發」，而不是宣稱自己就是 GRADE（S16）。

**活的系統性回顧**每次搜尋都記錄日期，並維護一份「What's New」([Cochrane](https://resources.cochrane.org/sites/resources.cochrane.org/files/uploads/inline-files/Transform/201912_LSR_Revised_Guidance.pdf))。對應到 Research Radar，就是每個數字都附上截至日期與快照編號（Must）。

**專利地圖與技術雷達，教的是「怎麼說話」。** ThemeScape 說明地圖的軸沒有意義，只有相對距離有意義 ([ThemeScape](https://clarivate.com/intellectual-property/ja/download/di-analyst-themescape-textclustering-ja))。PatSnap 指出空白區域「很少真的是空的」；它有一個很實用的功能，是貼上一段文字看它落在地圖哪裡 ([PatSnap](https://help.patsnap.com/hc/en-us/articles/115000409785))。前者變成警語「在這 14 本期刊中稀疏」，而不是「研究缺口」；後者變成「我的點子落在哪？」（S11）。

Thoughtworks 技術雷達每年出兩版，沒有移動的點位會從雷達上淡出，也明說不追求全面 ([Thoughtworks](https://www.thoughtworks.com/de/radar/faq))。Backstage 的資料模型讓每個點位都有帶日期的移動歷史 ([Backstage](https://github.com/backstage/community-plugins/blob/main/workspaces/tech-radar/plugins/tech-radar/README.md))。Research Radar 可以每學期出一版「研究雷達」，只為新增或移動的點位寫說明，維護量就很小。但要做兩個調整：

- 環改成描述性的「新興／成長／成熟／降溫」，依透明規則放置（可參考 Rotolo 等人提出的新興技術五個屬性 ([Rotolo et al.](https://arxiv.org/abs/1503.00673))），人工改動必須寫理由；
- 不用 Adopt／Hold 這類建議性的環名，也不畫 hype cycle。《經濟學人》發現，只有約五分之一的突破性技術走完整個循環 ([The Economist](https://www.economist.com/finance-and-economics/2024/08/19/artificial-intelligence-is-losing-hype))。

**其餘只取一句話的教訓。**

- 監測工具的單一分數不要學，例如 CB Insights Mosaic 的頁面沒有公開計算方法 ([CB Insights](https://cbinsights.com/mosaic-score))；改用可拆解的指標。
- 英國 Defra「每兩週挑訊號、每半年歸納趨勢」的節奏，可以縮小成實驗室的例行工作（Could）([GOV.UK](https://www.gov.uk/government/publications/weak-signals-and-trend-analysis-horizon-scanning/weak-signals-and-trend-analysis-horizon-scanning))。
- LitCovid 公開了主題分類的 micro-F1：整體 0.81，有摘要時 0.89 ([LitCovid](https://academic.oup.com/nar/article/49/D1/D1534/5964074))。Research Radar 也應該公開評估數字。
- 遊戲化的效果小，而且約 4 週後新鮮感就開始下降 ([Rodrigues et al.](https://link.springer.com/article/10.1186/s41239-021-00314-6))；放 Later，不做排行榜。

### 4.2 還沒用到的資料與技術

| 資料／技術 | 帶來什麼 | 建議用法 | 優先 |
|---|---|---|---|
| OpenAlex 已解析全文（GROBID TEI） | 約 4,300 萬篇已轉成結構化 XML，每篇 $0.01；PDF 版權仍屬原權利人 ([OpenAlex 全文](https://help.openalex.org/access/fulltext.md)) | v1 全文的主要來源，只取 CC 授權者；省掉在 1 GB VM 上跑 GROBID | Must（規則）／Should（使用） |
| OpenAlex 還沒用的欄位 | 引用清單、近 10 年逐年被引次數、FWCI、百分位、topics／keywords 分數、作者國別、OA 與授權；`updated_date` 不會隨指標更新而變 ([OpenAlex 屬性](https://help.openalex.org/data/works/attributes.md)) | 原始 JSON 全存；OA／授權現在解析；其餘需要時再從原始資料解析 | Must（存）／Could（分析） |
| Crossref 的 Retraction Watch 資料 | 免費撤稿資料，工作日每天更新 CSV ([Crossref](https://crossref.org/documentation/retrieve-metadata/retraction-watch/)) | 每週比對一次。OpenAlex 的 `is_retracted` 只當「待查」：它曾把更正誤標為撤稿，後來修正了約 2,300 筆 ([Hauschke & Nazarovets](https://arxiv.org/abs/2403.13339v1)) | Should |
| Qwen3-Embedding-0.6B（Workers AI） | 多語檢索 MMTEB 得分 64.64，高於 bge-m3 的 54.60 ([Qwen3 論文](https://arxiv.org/pdf/2506.05176))；每百萬 token $0.012，免費額度每天約 930 萬 token ([Cloudflare 價格](https://developers.cloudflare.com/workers-ai/platform/pricing/)) | v1 唯一的嵌入模型；整個語料在一天的免費額度內就能嵌完 | Must（選定） |
| LLM 改寫查詢＋英文全文檢索＋RRF | Azure PostgreSQL 不支援 pg_bigm，且沒有預定時程 ([Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/5825157/when-can-azure-for-postgresql-support-pg-bigm-exte)) | Haiku 把中文問題改寫成英文關鍵字與篩選條件，再和兩路向量結果用倒數排名融合（RRF）合併；構念中英對照由萃取時順便產生 | Should |
| 參數化 SQL 工具（tool use） | 計數題能得到正確答案；省下 GraphRAG 類方法的索引成本 ([LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)) | Haiku 只負責選工具與參數，由 SQL 計算數字，並顯示「怎麼算的」 | Must（導流）／Should（工具） |
| Anthropic `search_result` 引用與 structured outputs | 引用以區塊為單位回傳，`cited_text` 不計入輸出 token ([Anthropic](https://platform.claude.com/docs/en/build-with-claude/search-results))；JSON schema 輸出已 GA，但不支援數值範圍限制 ([Anthropic](https://platform.claude.com/docs/en/build-with-claude/structured-outputs.md)) | 問答用 `search_result`，萃取用 structured outputs；數值範圍仍由 Pydantic 驗證 | Should |
| AISeL OAI-PMH 與 CORE | AISeL 在 COAR 登錄了線上的 OAI-PMH（機構典藏常用的開放收割協定）端點 ([COAR](https://ird.coar-repositories.org/systems/8fa69709-76b1-4cc4-9589-0160456e0503?lang=en))；CORE 已收錄 64,441 篇 AISeL 全文 ([CORE](https://core.ac.uk/data-providers/10830/?offset=10)) | 研討會序列當作「新興」訊號，和期刊序列分開呈現；也可以用集合來清理 JAIS 的誤歸 | Should／Could |
| 引用分析與標準工具匯出 | 書目耦合（研究前沿）、共被引（知識基礎）、引用速度；VOSviewer 可直接匯入 OpenAlex 查詢，上限 5 萬筆 ([OpenAlex 說明](https://help.openalex.org/hc/en-us/articles/27218772811543-How-do-I-import-OpenAlex-data-into-VOSviewer)) | 從原始 JSON 解析出引用邊之後離線計算；先做匯出按鈕，不重造視覺化 | Could |
| 離線 2D 地圖與主題模型 | datamapplot 可輸出單一自含的 HTML ([datamapplot](https://datamapplot.readthedocs.io/en/latest/interactive_intro.html))；BERTopic 支援零樣本主題與隨時間的變化 ([BERTopic](https://pypi.org/project/bertopic)) | 取代 3D 當作分析總覽；用「固定分類 vs 新興主題」產生題目候選 | Could |
| NDLTD RIS、國圖開放資料、中大機構典藏 | 國圖論文書目開放資料含指導教授欄位（102–107 學年）([NCHC](https://scidm.nchc.org.tw/en/dataset/best_wish14024))；中大機構典藏列有 OAI-PMH 端點 ([PhilPapers](https://api.philpapers.org/archives/view.pl?id=1649)) | RIS 匯入（Should）；學長姐論文脈絡（Could）；不爬 NDLTD | Should／Could |
| MCP（2026-07-28 規格） | 核心改為無狀態；遠端授權必須是完整的 OAuth 2.1 資源伺服器 ([WorkOS](https://workos.com/blog/mcp-2026-spec-agent-authentication))；Python SDK v2 把 FastMCP 改名為 MCPServer ([SDK v2](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)) | phase 2 再做：本機 stdio＋個人 token，或公開唯讀的遠端伺服器；只放 3–6 個精選工具 | Could |
| Scopus API | 學術用途條款禁止在網站上公開顯示 ([Elsevier](https://dev.elsevier.com/academic_research_scopus.html)) | 不用它補 Elsevier 摘要；維持覆蓋率標示 | 不做 |

## 5. 現在就要在架構裡預留的東西

判斷標準只有一條：**事後無法重建的東西現在做；事後能從保留的原始資料重算、或能用新增表格補上的東西就等。** Haiku 5.5 讓重跑 LLM 幾乎免費，OpenAlex 全量重抓只要幾美分，重新嵌入整個語料也只佔一天的免費額度，所以「欄位不夠」已經不是大問題。真正貴的是四件事：遺失的來歷、被覆蓋的人工判斷、混在一起的隱私邊界，以及重複的識別碼。這也符合 Rule of Three：預留的是**資料形狀**，不是預先蓋好的**框架**。

### 5(a) 不可逆：第一次全量匯入或萃取前一定要做（毛工時 18 h／淨增約 11 h〔估計〕）

「毛工時」是整個項目的工作量，「淨增」只算原本計畫之外多出來的部分。原本計畫就需要 LLM 紀錄（預算上限）、修正與合併的儲存、DOI upsert、visibility 與 abstract-source 欄位，所以淨增小於毛工時。

| 預留 | 具體內容 | 工時（毛／淨增） | 晚做為什麼貴 | 變更情境 |
|---|---|---|---|---|
| R1 資料來歷（M02） | `derivation_run`（task、task_version、model_id、prompt_sha256、output_schema、batch_id、status、cost）；AI 產出的每一列都帶 NOT NULL `run_id`、`input_kind`（title／abstract／fulltext）、`input_sha256`、`lang`、`status`（active／superseded／rejected）；`llm_calls` 帳本 | 4／2 h | 既有資料的來歷救不回，只能全部重跑，也失去與評估、人工修正的可比性；PDF 進來後，分不出哪些是摘要版、哪些是全文版 | C2 |
| R2 原始資料（M03） | OpenAlex 原始 JSON＋抓取日期、LLM 原始回應、日後的 TEI，以 sha256 為鍵存在 R2 | 1.5／1 h | 修解析器或加欄位都得重抓、重付費；OpenAlex 的指標每晚重算，`updated_date` 卻不變，當時的快照無法重建 | C1、C2 |
| R3 萃取 schema v1（M04） | 情境（國家、產業、樣本數 n、分析單位、研究設計）、limitations／future research、構念英文名＋繁中標籤、假設原句＋驗證器、可空的 β／p／N 與量表來源 | 3／2 h | 錢不是問題（重跑約 $1–3），貴的是 50 篇金標準要重標，已確認的修正與合併也要重核 | C2 |
| R4 人工判斷事件表（M05） | `correction`：以 paper_id、task、field_path 為邏輯鍵，記錄新舊值與理由；`merge_decision`：含被拒絕的決定與 evidence_level；有效值以人工為優先 | 2.5／1.5 h | 直接覆寫等於丟掉標註；重跑會洗掉人工成果；被拒絕的合併會一再被建議 | C2 |
| R5 向量表（M06） | `embedding(item_type, item_id, model_id)`＋模型登錄表＋每個模型各一個 partial HNSW 索引；選定 Qwen3-Embedding-0.6B | 2／1 h | 若用 `papers.embedding vector(n)` 這種設計，換模型就是一次停機大搬家；pgvector 只能對同維度的資料建索引 ([pgvector FAQ](https://docs.pgedge.com/pgvector/v0-8-5/frequently-asked-questions/)) | C3 |
| R6 識別碼（M07） | `external_id(scheme, value_norm)`＋DOI／OpenAlex／ISSN-L 正規化；冪等 upsert 以它為目標 | 2／1 h | DOI 大小寫或前綴不一致會產生重複資料，直接灌水研究計數 | C1 |
| R7 擁有權與授權（M08） | 使用者的資料帶 `workspace_id`（書目資料不帶）；visibility enum＋單一的 `policy.can_read()`；PDF 只能 private，並記錄 `access_type`、`license`、`attested_at`；付費牆全文送 LLM 的旗標預設關閉 | 3／2.5 h | 若先用布林值 `is_shared`，之後要遷移資料並改每一個查詢；隱私外洩與授權違規無法撤回 | C4 |

建議的順序是 R6 → R2 → R1 → R3／R4 → R5 → R7，全部在第一次十年匯入、以及 50 篇金標準標註之前完成。M01（OpenAlex 匯入衛生）與 M09–M12 不屬於架構，但同樣是 Must。

### 5(b) 從第一天就採用的便宜慣例（設定約 6–8 h〔估計〕，計入非功能預算）

| 慣例（從第一個 commit 開始做，邊際成本接近零） | 晚做的代價 |
|---|---|
| UUIDv7 主鍵；Azure PostgreSQL 若沒有 `uuidv7()`（PG18 才內建 ([Nile](https://thenile.dev/blog/uuidv7))），就在應用層產生 | 主鍵型別一改，每張表、外鍵與網址都要跟著動 |
| 時間一律用 `timestamptz`：資料庫存 UTC，介面顯示台北時間 | 混用時區的歷史資料，事後無法判讀 |
| 設定即資料：`journals.yaml`（OpenAlex source ID、ISSN-L）、版本化的分類表、`keyword_sources.yaml`、領域停用詞、`prompts/<task>/vN.md`、模型 ID 與價格表；「領域」是一筆資料，不是外掛框架 | 學弟妹得改程式才能加期刊、換模型 |
| Alembic 命名慣例，CI 跑 `alembic check`；回填寫成可續跑的 CLI 工作；破壞性變更走 expand→migrate→contract（先擴充、再遷移、最後收斂） | 自動產生的遷移會把改名當成「刪除＋新增」 |
| 單一 `radar` CLI 包住應用服務，所有工作都是普通函式 | 之後的 worker、CI、MCP 都只是薄薄一層轉接（C4）；否則每個入口各寫一套 |
| `/api/v1`＋RFC 9457 錯誤格式＋commit `openapi.json`＋自動產生 TypeScript client | API 漂移要到執行時才會被發現 |
| react-i18next 用鍵值；生成的內容帶 `lang` 欄位 | 事後抽字串，要改每一個元件 |
| 測試：錄製 cassette（真實 API 回應的錄音檔）並在 CI 封鎖網路；FakeProvider 加負面樣本（截斷的 JSON、捏造的引文）；prompt 快照，並在「模板雜湊變了卻沒升版」時讓測試失敗；schema 體檢測試（AI 表必有 `run_id`，使用者表必有 `workspace_id`） | 資料來歷與離線測試（C5）會在趕工時被悄悄破壞 |
| 結構化 JSON 日誌＋request ID；設定 Docker 日誌大小上限；秘密只走環境變數，CI 跑 gitleaks | 事故當下的線索事後補不回；秘密一旦進了 git 歷史就很難清 |
| `audit_event` 只記可見性變更、修正、合併、刪除與 allowlist 變更；刪除時把資料複製到 `deleted_record`，不在每張表加 `deleted_at` | 歷史無法回補；`deleted_at` 會滲進每一個查詢 |
| 工作放置：VM 只當服務層；GROBID、重新嵌入、UMAP 與圖分析，用同一個 Docker 映像＋`radar` CLI 在 Kevin 的 Mac 或 Windows 電腦上跑；GitHub Actions 只做 CI；排程放在 VM | 私有 repo 的 GitHub runner 只有 2 vCPU／8 GB ([GitHub Docs](https://docs.github.com/en/actions/reference/runners/github-hosted-runners))；公開 repo 的排程只要 60 天沒有活動就會自動停用 ([GitHub Docs](https://docs.github.com/en/actions/using-workflows/disabling-and-enabling-a-workflow)) |
| 註冊表只給 LLM 與嵌入用（真實實作加 Fake 已經是兩個）；資料來源的註冊表等第二個來源出現再做（Rule of Three） | 過早抽象會讓每次修改都更費工 |
| 約 10 份一頁的 ADR：模型選擇、部署拓撲、GitHub Actions 的角色、/goal 代理人規則、授權條款（選 MIT 或 Apache-2.0；Google 禁止內部使用 AGPL 程式碼 ([Google](https://opensource.google/docs/using/agpl-policy/))） | 決策的脈絡會隨著畢業消失 |

### 5(c) 可以安全等待的

可以等的有兩類。

**第一類是「新增式」的表與功能**，晚加不必回填舊資料：個人化回饋、追蹤與閱讀事件、saved query、Zotero 連結表、個人存取 token、雷達期刊版的 `ring_history`、地圖座標表。以 4 到 10 人的規模，晚加損失的歷史資料很少。

**第二類是可以從原始資料重算的東西**：OpenAlex 的引用指標與主題欄位、地圖座標、主題模型、burst 結果，以及重新嵌入。

至於真正的框架，依 Rule of Three 一律等到出現第二個真實需求：

- 多實驗室的列層級安全（RLS）與成員角色。在那之前，一個實驗室一套部署。
- 領域外掛框架。
- 資料來源與匯出器的註冊表。
- 工作佇列。真有需要時，用跑在既有 PostgreSQL 上的 Procrastinate ([PyPI](https://pypi.org/project/procrastinate))，不要在 1 GB VM 上加 Redis。
- 功能旗標服務。
- 統合分析介面。β、p、N 已在 R3 以可空欄位預留。
- DSPy 最佳化。DSPy 自己的建議是約 50 筆以上才用隨機搜尋、200 筆以上才用 MIPROv2 ([DSPy](https://dspy-docs.vercel.app/building-blocks/6-optimizers))。

## 6. 優先順序總表

### 6.1 全部建議

工時欄：Must 寫的是**淨增工時**（第 6.3 節的預算用它來算），其餘寫級距或估計時數。服務對象縮寫：同學、接手（學弟妹）、老師、學界、面試、大眾、Kevin。

| 項目 | 服務誰 | 優先順序 | 工時 | 為什麼 | 依據 |
|---|---|---|---|---|---|
| **M01** OpenAlex 匯入衛生：ingester 帶 api_key、cursor 分頁（每頁 100 筆）、記錄 `X-RateLimit-Credits-Used`、遇 409／429 退避 | 全部 | Must | 淨 +1.5 h | 沒有 key 的正式匯入不受支援；成本可追蹤 | [OpenAlex 群組](https://groups.google.com/g/openalex-users/c/rI1GIAySpVQ)、[OpenAlex 認證](https://help.openalex.org/api/authentication/) |
| **M02** 資料來歷骨架＋LLM 呼叫與成本帳本＋Batch 狀態機（5a R1） | 老師、學界、面試、接手 | Must | 淨 +2 h | 來歷事後救不回；也是預算熔斷與 AI 揭露的前提 | [Dagster](https://dagster.io/docs/guides/build/assets/asset-versioning-and-caching)、[S2 平台論文](https://arxiv.org/abs/2301.10140) |
| **M03** 保留原始資料（5a R2） | 接手、Kevin | Must | 淨 +1 h | 加欄位、修 bug 只需重新解析 | [OpenAlex 屬性](https://help.openalex.org/data/works/attributes.md) |
| **M04** 萃取 schema v1 定版（5a R3） | 同學、學界 | Must | 淨 +2 h | 金標準與人工修正不必重做；完整假設召回只有 39.3%，需要原句佐證 | [Funk & Scharfenberger](https://fis.leuphana.de/en/publications/construct-relation-extraction-from-scientific-papers-is-it-automa/)、[Elicit](https://elicit.com/blog/how-we-evaluated-elicit-systematic-review) |
| **M05** 人工修正與合併決策事件表（5a R4） | 同學、接手、學界 | Must | 淨 +1.5 h | 被覆蓋的人工判斷無法復原 | [Wulff & Mata](https://pmc.ncbi.nlm.nih.gov/articles/PMC12106064)、[DSPy](https://dspy-docs.vercel.app/building-blocks/6-optimizers) |
| **M06** 向量表以（項目, 模型）為鍵＋選定多語模型（5a R5） | 同學 | Must | 淨 +1 h | 換模型不用停機；中文查詢品質 | [Qwen3 論文](https://arxiv.org/pdf/2506.05176)、[pgvector FAQ](https://docs.pgedge.com/pgvector/v0-8-5/frequently-asked-questions/) |
| **M07** DOI 與外部 ID 正規化（5a R6） | 全部 | Must | 淨 +1 h | 重複資料會灌水計數 | 工程判斷（演進性筆記） |
| **M08** 擁有權邊界＋PDF 授權閘門（5a R7） | 老師、同學 | Must | 淨 +2.5 h | 隱私外洩與授權違規都無法撤回 | [UWaterloo](https://uwaterloo.ca/lib/research-supports/copyright-and-licensing/use-library-resources-ai)、[中大圖書館](https://lib.ncu.edu.tw/ebook)、[OpenAlex 全文](https://help.openalex.org/access/fulltext.md) |
| **M09** 誠信與誠實呈現：AI 標章、證據簡報（不產生研究問題）、不提供可貼上的段落、固定警語、數字附截至日期與快照編號 | 老師、同學、大眾 | Must | 淨 +3 h | 防止誤導與學術誠信問題 | [中山管院](https://rpb73.nsysu.edu.tw/static/file/234/1234/img/777/127846497.pdf)、[台大](https://ori.ntu.edu.tw/ethics/download/fileSn/25)、[3ie](https://www.3ieimpact.org/sites/default/files/2024-03/how-to-read-egm-document-Feb2024.pdf) |
| **M10** LLM 安全與預算：淨化輸出＋CSP（內容安全政策）、專用 Anthropic workspace 與上限、預算熔斷後切換為只檢索、prompt 不含秘密 | Kevin、面試 | Must | 淨 +3 h | 斷開注入攻擊的外洩管道；防「刷爆錢包」攻擊 | [OWASP LLM10](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/)、[致命三要素](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)、[隱藏指令事件](https://the-decoder.com/researchers-hide-prompts-in-scientific-papers-to-sway-ai-powered-peer-review/) |
| **M11** 計數與趨勢題不走 RAG，導向 SQL 或趨勢頁 | 同學、老師 | Must | 淨 +1 h | 純 RAG 在資料庫問答基準上的完全正確率是 0% | [TAG](https://arxiv.org/pdf/2408.14717) |
| **M12** burst 驗收條件加上 Walden 斷層檢查 | 同學、老師 | Must | 淨 +1 h | 演算法換代可能造出假的突發 | [OpenAlex 部落格](https://blog.openalex.org/author/jason) |
| **M13** 營運最小保險：實驗室擁有帳號與秘密清單；/goal 代理人碰不到正式環境秘密，並開分支保護；每晚加密 pg_dump 上傳 R2 並送心跳訊號；上線前實際還原一次 | 接手、Kevin | Must | 約 3 h（非功能預算） | 單點故障與秘密外洩都不可逆；能還原才算備份 | [SSI](https://www.software.ac.uk/blog/dont-fear-reaper-dealing-bus-factor)、[SRE](https://sre.google/sre-book/data-integrity/) |
| **S01** AI 文獻驗證器 | 同學、接手、面試 | Should | M（6–8 h） | 接住用通用 AI 的同學；重用引用驗證器 | [Walters & Wilder](https://www.nature.com/articles/s41598-023-41032-5.pdf)、[WZU](https://c030.wzu.edu.tw/datas/upload/files/國際事務系碩士論文生成式_AI_使用規範與自我檢核要點_中文版%20-%20Copy%201.pdf) |
| **S02** 文獻整理表＋RIS／BibTeX／CSV 匯出（含證據層級欄） | 同學、學界 | Should | S–M（4–6 h） | 資料在萃取時已經產生；可接 CoLRev、Zotero | [CoLRev](https://colrev.readthedocs.io) |
| **S03** RIS／BibTeX 匯入 | 同學、接手 | Should | S（3–4 h） | 合法取得本地論文的實際管道 | [台大圖書館](https://tul.blog.ntu.edu.tw/archives/tag/ndltd) |
| **S04** 中文查詢管線＋構念中英對照 | 同學 | Should | M（6–10 h） | Azure 沒有中文斷詞擴充 | [Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/5825157/when-can-azure-for-postgresql-support-pg-bigm-exte) |
| **S05** 參數化 SQL 工具回答計數題，並顯示算法 | 同學、老師 | Should | M | 讓問答也能「可計數」 | [TAG](https://arxiv.org/pdf/2408.14717)、[LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/) |
| **S06** 原生引用：`search_result` 區塊＋伺服器端驗證＋顯示原句（微調既有 Must） | 同學、面試 | Should | S–M | 引用更精細，驗證更簡單 | [Anthropic](https://platform.claude.com/docs/en/build-with-claude/search-results) |
| **S07** 公開評估報告：主題準確率分有摘要／無摘要；假設分三層報 P/R；RAG 30–50 題（含無答案題與注入金絲雀）；LLM 評審先用人工標籤校準 | 面試、學界、老師 | Should | M（8–12 h） | 能回答面試的 P/R 題；增加可信度 | [LitCovid](https://academic.oup.com/nar/article/49/D1/D1534/5964074)、[Eugene Yan](https://eugeneyan.kit.com/posts/an-llm-as-judge-won-t-save-your-product-fixing-your-process-will)、[Hamel（Willison 整理）](https://simonwillison.net/2024/Oct/30/llm-as-a-judge) |
| **S08** 撤稿警示 | 老師、學界 | Should | S（3–4 h） | 便宜又看得見的誠信功能 | [Crossref](https://www.crossref.org/blog/retraction-watch-retractions-now-in-the-crossref-api/) |
| **S09** 證據缺口地圖（構念×構念）；「不一致發現」同時呈現兩邊研究的情境與方法 | 同學、老師、學界 | Should | M（8–12 h） | 不一致可轉成調節變數假說；區分絕對缺口與綜整缺口 | [3ie](https://www.3ieimpact.org/sites/default/files/2024-03/how-to-read-egm-document-Feb2024.pdf)、[Campbell](https://pmc.ncbi.nlm.nih.gov/articles/PMC8356343) |
| **S10** 交接包：admin UI 管理 allowlist（已決定為 Should）、LLM-off 模式、每週自動還原演練、中文 runbook、完整匯出、外部 uptime 檢查 | 接手、老師 | Should | M（8–12 h） | 學術工具常在作者離開後失效 | [NAR 2020](https://pesquisa.bvsalud.org/portal/resource/pt/mdl-33270886)、[Fehr et al.](https://refworks.reference-global.com/article/10.5334/jors.307)、[Healthchecks](https://healthchecks.io/docs/healthchecks_cronitor_comparison/) |
| **S11** 「我的點子落在哪？」 | 同學、面試 | Should | S–M | 重用語意搜尋，也是好 demo | [PatSnap](https://help.patsnap.com/hc/en-us/articles/115000409785) |
| **S12** 即時訪客問答（從 Must 延後）＋Turnstile、每 IP 限流、token 與長度上限 | 面試、大眾 | Should | M | 求職季前再開 | [OWASP LLM10](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/)、[Turnstile](https://developers.cloudflare.com/turnstile/plans/) |
| **S13** 取得連結（OA PDF、DOI、中大 OpenURL）＋「台灣做過了嗎？」NDLTD 連結搜尋 | 同學、老師 | Should | S（各 2–4 h） | 不爬蟲也能省下找全文的時間 | [Aaron Tay](https://aarontay.substack.com/p/a-2025-deep-dive-of-consensus-promises) |
| **S14** 方法＋AI 揭露一鍵匯出（PRISMA-S 欄位＋AI 使用紀錄） | 同學、老師 | Should（系上若有規定則提前） | M | 學位論文的 AI 揭露要求正在擴散 | [PRISMA-S](https://pmc.ncbi.nlm.nih.gov/articles/PMC7839230)、[聯合聲明](https://pmc.ncbi.nlm.nih.gov/articles/PMC12594113/) |
| **S15** 研究雷達期刊版（每學期一版、描述性四環、透明規則＋人工理由）；首頁掃描動畫改用真資料 | 大眾、面試、同學 | Should | M | 雷達有實際內容，維護量小 | [Thoughtworks](https://www.thoughtworks.com/de/radar/faq)、[Backstage](https://github.com/backstage/community-plugins/blob/main/workspaces/tech-radar/plugins/tech-radar/README.md) |
| **S16** 受 GRADE 啟發的證據理由標籤 | 老師、同學 | Should | M | 說清楚「為什麼證據弱」 | [GRADE](https://pmc.ncbi.nlm.nih.gov/articles/PMC6350753) |
| **S17** 合集與入門必讀 | 接手、同學 | Should | M（6–10 h） | 把學長姐分享的資料夾變成持久的物件 | [Mpungose 2025](https://www.emerald.com/gkmc/article/74/11/178/1267149/Exploring-knowledge-transfer-and-retention) |
| **S18** 依合併證據層級篩選＋外部構念工具連結（先測試是否仍在線） | 學界、同學 | Should | S–M | 標籤層級的合併證據最弱 | [ALIGNS](https://arxiv.org/pdf/2509.09723)、[TheoryOn](https://par.nsf.gov/servlets/purl/10282376) |
| **S19** 工程證據包：Sentry 免費版、/healthz 與 /readyz、2–3 條 SLO、runbook＋一份 RCA、模型選擇與擴充性 ADR、小型負載測試、AI 協作開發說明＋退件紀錄、Anthropic 契約測試、招募用 README | 面試 | Should（多屬非功能預算） | M–L（分散進行） | 對應台積電 JD 與 AI 時代的面試重點 | [104](https://www.104.com.tw/job/8qguz)、[Canva](https://canva.dev/blog/engineering/yes-you-can-use-ai-in-our-interviews)、[Cake](https://www.cake.me/resources/engineer-resume-tips) |
| **S20** AISeL OAI-PMH 研討會序列（若需要清理 JAIS 誤歸則提前） | 學界、老師 | Should／Could | M（8–12 h） | 官方管道；補足期刊落後的問題 | [Digital Commons](https://digitalcommons.elsevier.com/digital-commons-and-oai-pmh)、[MISQ](https://misq.org/review-process) |
| 已決定的 Should（burst 圖表、2D ego network、PDF 段落引用與多輪問答、從 PDF 萃取 limitations）：微調見第 7 節 | — | Should（已決定） | — | ego network 採 Connected Papers 的慣例；PDF 相關功能受授權閘門約束 | [Connected Papers](https://medium.com/connectedpapers/announcing-connected-papers-a-visual-tool-for-researchers-to-find-and-explore-academic-papers-89146a54c7d4) |
| **C01** 3D 選項：(a) 離線算圖的飛越影片；(b) 互動 3D，限時 8 h | 大眾、面試 | Could（Kevin 決定） | (a) S–M／(b) L | 互動 3D 作為分析工具的證據弱 | [Munzner](https://cs.ubc.ca/~tmm/talks/vad/VAD-rules.pdf)、[McGuffin et al.](https://ar5iv.labs.arxiv.org/html/2207.11586) |
| **C02** 離線 2D 地形圖（UMAP＋密度等高線＋自動標籤，輸出單一 HTML） | 同學、大眾 | Could | M | 可以取代 3D 當分析總覽 | [datamapplot](https://datamapplot.readthedocs.io/en/latest/interactive_intro.html)、[Nomic](https://home.nomic.ai/blog/posts/data-mapping) |
| **C03** 引用分析，以及 VOSviewer／bibliometrix／Gephi 匯出 | 同學、學界 | Could | S～M | 從原始 JSON 解析出引用邊就能離線計算 | [OpenAlex 說明](https://help.openalex.org/hc/en-us/articles/27218772811543-How-do-I-import-OpenAlex-data-into-VOSviewer)、[bibliometrix](https://github.com/massimoaria/bibliometrix/releases/tag/v5.5.0) |
| **C04** BERTopic：固定分類 vs 新興主題 | 同學 | Could | M–L | 題目候選的來源 | [BERTopic](https://pypi.org/project/bertopic) |
| **C05** 理論×情境、方法×主題矩陣（與 S09 同一個元件） | 同學、老師 | Could | S（做完 S09 之後） | 適用於老師的 ERP、供應鏈、電商領域 | [Hong et al.](https://econpapers.repec.org/RePEc:inm:orisre:v:25:y:2014:i:1:p:111-136) |
| **C06** 單向推送到實驗室的 Zotero 群組（群組由老師擁有） | 同學、接手 | Could | M（8–12 h） | RIS 匯出已能手動匯入；群組儲存空間算在擁有者的額度上 | [Zotero 寫入 API](https://www.zotero.org/support/dev/web_api/v3/write_requests)、[UNC](https://guides.lib.unc.edu/zotero/groups) |
| **C07** 每週摘要（email／Discord）與 RSS | 同學、老師 | Could | S–M | LINE Notify 已在 2025-03-31 停止服務 | [數位時代](https://www.bnext.com.tw/article/88845/)、[Apprise](https://pypi.org/project/apprise) |
| **C08** 題目登記與撞題提醒；國科會計畫用的圖表匯出（附資料版本） | 老師 | Could（老師常用則升為 Should） | M | 方便監督與重用 | [台大](https://ori.ntu.edu.tw/ethics/download/fileSn/25) |
| **C09** 學長姐論文脈絡（國圖開放資料、中大機構典藏 OAI-PMH） | 接手、老師 | Could | M | 實驗室在地傳承 | [NCHC](https://scidm.nchc.org.tw/en/dataset/best_wish14024)、[PhilPapers](https://api.philpapers.org/archives/view.pl?id=1649) |
| **C10** 複製研究候選；假設地圖（用來挑戰既有假設） | 同學 | Could | M | 補 gap-spotting 的盲點 | [TRR](https://aisel.aisnet.org/trr/authorinfo.html)、[Sandberg & Alvesson](https://lup.lub.lu.se/record/1925769) |
| **C11** 訊號收件匣、IS Research Wrapped、預先渲染的時光縮影影片 | 大眾、同學 | Could | 各 S–M | 推廣與例行節奏 | [GOV.UK](https://www.gov.uk/government/publications/weak-signals-and-trend-analysis-horizon-scanning/weak-signals-and-trend-analysis-horizon-scanning)、[ADMA](https://adma.com.au/resources/personal-shareable-no-strings-attached-the-winning-formula-behind-spotify-wrapped) |
| **C12** MCP（已決定為 phase 2）：本機 stdio＋個人 token，或公開唯讀遠端，3–6 個精選工具 | 同學、面試 | Could | M | 遠端存取私有資料需要 OAuth 2.1 | [WorkOS](https://workos.com/blog/mcp-2026-spec-agent-authentication)、[FastMCP](https://gofastmcp.com/v3/integrations/fastapi) |
| **C13** 補覆蓋：Elsevier 摘要用綠色 OA、TEI、CORE 補；OpenCitations 補引用；OpenAlex 語意搜尋找庫外文獻 | 同學 | Could | 各 S–M | 合法提高覆蓋率 | [CORE](https://core.ac.uk/data-providers/10830/?offset=10)、[OpenCitations](https://api.opencitations.net/index)、[OpenAlex 語意搜尋](https://help.openalex.org/api/semantic-search.md) |
| **C14** 用第二個領域包證明可以泛化 | 面試、接手 | Could | M | 增加架構說服力 | [ORKG](https://academy.orkg.org/getting-started.html) |
| **L01** 多實驗室 RLS 與成員角色 | 接手 | Later | M–L | 等第二個實驗室真的加入 | [MakerKit](https://makerkit.dev/blog/md/tutorials/multi-tenant-saas-architecture) |
| **L02** 個人化 mute／boost；遊戲化（不做排行榜） | 同學 | Later | M | 4 人規模用不到；效果小而且會衰退 | [Feedly](https://feedly.com/new-features/posts/feedly-ai-and-mute-filters)、[Sailer & Homner](https://link.springer.com/10.1007/s10648-019-09498-w) |
| **L03** Zotero 雙向同步、瀏覽器擴充、PWA、LINE 官方帳號推播 | 同學 | Later | M–L | 衝突處理成本高，需求也還沒證實 | [Zotero 同步](https://www.zotero.org/support/dev/web_api/v3/syncing) |
| **L04** DSPy 最佳化與微調（標註達 50–200 筆之後） | 面試 | Later | M–L | DSPy 本身的資料門檻 | [DSPy](https://dspy-docs.vercel.app/building-blocks/6-optimizers) |
| **L05** 統合分析／MASEM 支援 | 學界 | Later | L | 需要全文與人工驗證；欄位已預留 | [Funk & Scharfenberger](https://fis.leuphana.de/en/publications/construct-relation-extraction-from-scientific-papers-is-it-automa/) |
| **L06** 遠端 MCP 存取私有資料、Terraform、自架 reranker | 面試、同學 | Later | M–L | 超出目前範圍；Terraform 可以先寫 ADR，說明何時再加 | [WorkOS](https://workos.com/blog/mcp-2026-spec-agent-authentication)、[104](https://www.104.com.tw/job/8qguz) |

寒假期間若有餘裕，Should 依序做 S06 → S03 → S02 → S01 → S08 → S10 的備份演練部分。春季學期同學寫提案時，做 S04、S05、S09、S11、S13、S14、S15、S17。求職季之前，做 S12 與 S19 的 README。

### 6.2 用來支付新 Must 的刪減與延後

先說明一點：3D 本來就是 Could，刪掉它省不到 Must 的工時。跨產業筆記建議「刪掉 3D 來支付新 Must」，這條不適用；工時必須從 Must 內部刪。

| 編號 | 刪減／延後 | 省下〔估計〕 | 代價與緩解 | 何時做 |
|---|---|---|---|---|
| K1 | 公開 demo 的上線版改為「靜態快照＋10–20 題預先算好、附驗證引用的展示問答」；即時訪客問答（每日 30 題、每人 5 題、獨立預算、只用 OA）延到 S12 | 5–8 h | 少了即時互動；但上線時沒有「刷爆錢包」的風險，主機到期後作品集也還在 | 第一層（現在） |
| K2 | 構念合併確認改成用 CLI 匯出、匯入試算表做批次確認（只有 Kevin 與老師操作）；分類修正只保留論文頁上最小的下拉選單；完整介面併入 admin UI | 3–5 h | 同學暫時不能批次修正；事件表（M05）不變，之後補介面不需要遷移 | 第一層 |
| K3 | 首頁雷達掃描動畫延到 S15；Must 版首頁只留一句話、一個附日期的數字、一張發現卡、60 秒 GIF | 3–6 h（前提是動畫原本算在 Must） | 首頁比較樸素；等有了真資料（雷達期刊版）再做動畫更有意義 | 第一層 |
| K4 | 主題交集延到 Should（之後併入 S09 的矩陣元件） | 2–4 h | 「ERP×AI」這類問題暫時用搜尋＋篩選處理 | 第一層 |
| K5 | PDF 上傳＋自己跑 GROBID 延到 Should；v1 的全文只用 OpenAlex 已解析、CC 授權的 TEI | 6–10 h（前提是原本在 Must） | 全文覆蓋率取決於 OA 比例，而 OA 比例還沒量測 | 第二層：圖書館否定或 12/20 前未回覆，或第 2 週末重估超過 150 h |
| K6 | Kleinberg burst 表延到 Should，Must 只留「成長最快」 | 3–5 h | 少了突發偵測；若 M12 發現斷層，本來也得換關鍵詞來源 | 第二層 |
| K7 | 「不一致發現」改成關係表上的「混合支持」篩選條件，不另做頁面 | 2–3 h | 呈現比較陽春，資料不變 | 第二層 |
| K8 | 最後手段之一：v1 只做規則式的構念正規化（大小寫、單複數、縮寫表），LLM 合併建議移到 2 月下旬 | 4–6 h | 關係表比較分散 | 只在悲觀情境 |
| K9 | 最後手段之二：v1 先以「Kevin 單人＋老師唯讀」上線，Google 登入與 lab 可見性在春季開學後兩週內補上（schema 已由 M08 預留，補上時不需遷移） | 4–6 h | 同學晚兩週才能使用；不過同學真正需要用的時間本來就在春季 | 只在悲觀情境 |

### 6.3 預算重算（小時；功能預算為 120–150）

| 步驟 | 樂觀 | 中位 | 悲觀 |
|---|---|---|---|
| 現有 Must 估計 | 120 | 142 | 164 |
| ＋新增 Must 淨增（M01–M12） | +18 | +21 | +24 |
| 累計 | 138 | 163 | 188 |
| −第一層刪減（K1–K4） | −23 | −18 | −13 |
| 累計 | **115** | **145** | 175 |
| −第二層刪減（K5–K7，觸發才做） | 不需要 | 視需要 −11～18 | −11 |
| 累計 | 115 | 127–145 | 164 |
| −最後手段（K8、K9） | — | — | −8 |
| 最終 | **115** | **127–145** | **約 156（仍超出 6 h）** |

在中位情境下，Must 剩下約 5 小時緩衝。如果首頁動畫原本就不在 Must 裡，K3 省不到工時，中位會升到約 149 小時，第二層刪減幾乎一定要啟動。悲觀情境代表原本的 Must 估計就已經超支 14 小時；即使用盡所有槓桿，仍超出約 6 小時。這時正確的做法，是接受 v1 上線延到春季開學後一到兩週，而不是砍掉驗證（評估、引用驗證、授權閘門）。M13 與第 5(b) 節的慣例計入另外 30–40% 的非功能預算（審查、ADR、部署），合計約 9–11 小時〔估計〕，會與 Azure 部署工作互相排擠，所以慣例要守住「最小」。

## 7. 對 PRD 的具體修改建議

1. **把定位寫進 PRD**：「Research Radar 不比你的 AI 聰明；它在有邊界、標明覆蓋率的 IS 文獻庫上，提供可計數、可追溯、可重現的證據，並累積實驗室的人工判斷。」以第 2 節的四根支柱作為需求分類。
2. **新增 Must M01（OpenAlex 匯入衛生）**：api_key 從 secrets 讀取；用 cursor 分頁，`per_page=100`；每次呼叫把 `X-RateLimit-Credits-Used` 寫進 `api_calls(endpoint, cost, ts)`；遇到 409／429 時指數退避。不要用 OpenAlex 的 search 與 semantic search 端點做匯入：search 每次 $0.001，是列表呼叫的 10 倍；semantic search 每秒限 1 次，價格也未公開。驗收：用錄製的 cassette 做離線測試並通過。
3. **新增 Must M02（資料來歷）**：
   - 建立 `derivation_run`，欄位包括 task、task_version、provider、model_id、prompt_ref、prompt_sha256、output_schema、params、git_sha、provider_batch_id、status、cost_usd 與時間。
   - 所有 AI 產出表加上 NOT NULL `run_id`、`input_kind`（title／abstract／fulltext）、`input_sha256`、`lang`、`status`（active／superseded／rejected）、`superseded_by`。用部分唯一索引，保證每篇論文、每種語言只有一筆 active。
   - Batch 依 `derivation_run` 的狀態推進（queued → submitted → ingested 或 failed）。`custom_id` 用 `w{openalexId}_{task}_{promptVersion}`，不超過 64 字元。
   - `llm_calls` 帳本記錄 Anthropic usage 的各個欄位，並用經過單元測試的計算器算出 `cost_usd`。
   - 驗收：schema 體檢測試確認每張 AI 表都有 `run_id NOT NULL`。
4. **新增 Must M03（原始資料）**：OpenAlex 每一筆原始 JSON 連同 `fetched_at`，以及每次 LLM 的原始回應，都以 sha256 為鍵存到 R2（或 jsonb）。之後新增欄位，一律從原始資料重新解析，不重抓。
5. **新增 Must M04（萃取 schema v1）**：在第一次十年批次與 50 篇金標準標註之前定版，內容包括：
   - 每篇的 `context`（國家、產業、樣本數 n、分析單位、研究設計）；
   - `limitations` 與 `future_research`（輸入文字裡有才填，否則為 null）；
   - 每個構念的英文名＋繁中標籤，順便產生中英對照；
   - 每條假設的 `evidence` 原句，配一個驗證器：正規化之後必須出現在輸入文字裡，否則拒收；
   - 可空的 `beta`、`p_value`、`n`、`effect_type`、`measurement_source`。
   
   標註指引同步更新。
6. **新增 Must M05（人工判斷事件表）**：
   - `correction`：記錄 workspace、user、paper_id、task、field_path、corrected_run_id、新舊值、reason、時間、review_status。
   - `merge_decision`：記錄構念 A 與 B、提出建議的 run、accept 或 reject、evidence_level（label／definition／items）、理由、決定者與時間。
   - 有效值是最新一筆被接受的人工值；沒有人工值時，用 active 的 AI 值。
   - 重跑萃取不得覆蓋人工值，只記錄新模型是否同意。這個同意率就是換模型時的回歸指標。
7. **新增 Must M06（向量）**：
   - `embedding(item_type, item_id, model_id, input_sha256, run_id, vec)`，主鍵為 (item_type, item_id, model_id)。
   - `embedding_model` 登錄表，記錄維度、距離與 instruction 版本。
   - 每個模型一個 partial HNSW 索引；查詢時必須同時帶型別轉換與 `model_id` 條件，否則用不到索引。
   - v1 模型用 Workers AI 的 `qwen3-embedding-0.6b`。查詢端加任務指令、文件端不加〔依 Qwen3 的慣例，需對照 model card〕。
   - 全文切塊以 4,096 token 為上限（官方兩處文件分別寫 4,096 與 8,192，兩者不一致）。
8. **新增 Must M07（識別碼）**：
   - `external_id(entity_type, entity_id, scheme, value_norm, value_raw)`，`UNIQUE(scheme, value_norm)`。
   - `identifiers.py` 負責正規化：DOI（去掉 https://doi.org/ 與 doi: 前綴、URL decode、轉小寫、regex 驗證）、OpenAlex ID、ISSN-L。
   - 冪等 upsert 以 `external_id` 為目標。
   - 加性質測試：`norm(norm(x)) == norm(x)`。
9. **修改已決定的「visibility private／lab」，並新增 Must M08**：
   - 使用者擁有的資料列（筆記、上傳、修正、查詢、配額）加上 `workspace_id`，書目資料不加。
   - 用 visibility enum（private／lab／public），所有權限判斷都經過唯一的 `policy.can_read(user, resource)`。
   - **PDF 只能是 private**，取消 PDF 的 lab 選項。PDF 欄位包括 `access_type`（oa／subscription／other）、`license`、`attested_at`、`processing_mode`。
   - 只有 `best_oa_location.license` 為 CC 授權的全文，才會自動送雲端 LLM。付費牆全文送 LLM 的旗標 `PAYWALLED_FULLTEXT_LLM` 預設關閉，等圖書館書面回覆。
   - lab 層級只共享衍生事實（構念、假設、支持與否、頁碼）。bronze（僅供免費閱讀）不視為可再利用。
10. **新增 Must M09（誠信與誠實呈現）**：
    - 每個 AI 產物都顯示「AI 生成・未經人工核對／已核對、模型、日期」。
    - 只提供「複製引用＋連結」，不提供「複製成段落」，也不做改寫或去 AI 化的功能。
    - 「候選題目」改名為「證據簡報」，內容只有計數、不一致發現、未研究的情境與 CFP 連結；系統不產生研究問題的文字。
    - 固定警語四句：「缺口＝在這 14 本期刊中沒找到研究，不代表值得做」「支持＝作者報告支持，不是效果大小」「這是證據地圖，不是統合分析」「期刊趨勢反映的是數年前開始的研究」。
    - 中文摘要頁加註「供篩選閱讀，不可直接放入論文」。
    - 所有數字（包括首頁的「已讀 N 篇」）都附截至日期與 `snapshot_id`。
11. **新增 Must M10（LLM 安全與預算）**：
    - 答案用淨化過的 Markdown 呈現：不允許原始 HTML、圖片與模型產生的外部連結；站內論文連結只由驗證過的引用 ID 產生。
    - CSP 至少設定 `img-src 'self'; connect-src 'self'`。
    - Research Radar 使用專用的 Anthropic workspace 與 spend limit；應用層的上限要設得比它低，因為 batch 可能略為超支。
    - 建立每月各功能的預算表與熔斷機制，超支時切換成「只檢索」模式（列出最相關的段落，但不生成回答）。
    - Q&A 路徑不給任何工具；系統提示聲明文件內容是不可信資料。
    - prompt 裡不放秘密或授權邏輯，因為要假設 prompt 會外洩。
12. **新增 Must M11（計數題導流）**：問答遇到「幾篇、多少、每年、成長、趨勢」這類計數題時，不用檢索片段回答，改為導向趨勢頁的 SQL 結果，或回覆「請用趨勢功能」並附上連結。既有 RAG Must 的驗收加上 10 題冒煙測試：2 題計數題、2 題庫內無答案的題目、1 題注入金絲雀 PDF，其餘為一般題。
13. **修改既有的 burst Must（M12）**：驗收條件加上 Walden 斷層檢查，也就是比較各出版年的 OpenAlex keywords 詞彙分布。若在演算法切換處出現斷層，就改用自家 LLM 正規化的關鍵詞（關鍵詞來源本來就設計成可替換）。表格要標示關鍵詞的快照日期。
14. **新增 Must M13（營運最小保險，計入非功能預算）**：
    - 列出帳號與秘密清單，把擁有者改成實驗室或老師能控制的身分（GitHub、網域、Azure、Anthropic、OpenAlex、Google OAuth）。
    - /goal 夜間代理人的環境不放正式環境的秘密，並限制網路；開分支保護，要求必要檢查與人工合併；把這些規則寫成 ADR。
    - 每晚加密 `pg_dump` 並上傳 R2，成功後 ping Healthchecks.io。
    - 上線前實際還原一次。
15. **刪減 K1–K4（第一層）**：
    - 即時訪客問答改為 Should（S12），上線時改用靜態快照與預先算好的展示問答，仍遵守「只用 OA、不含 S2 摘要」。
    - 構念合併確認改用 CLI＋試算表。
    - 首頁動畫與主題交集延到 Should。
    - **寫入觸發規則**：第 2 週末依實際進度重估；若 Must 預估超過 150 h，依序啟動 K5、K6、K7。
16. **修改評估規格（pending notes 裡的 eval）**：
    - 主題準確率 ≥80%〔假設〕，分「有摘要／無摘要」分開報告（LitCovid 分別是 0.81 與 0.89）。
    - 研究模型萃取，分別報告構念、關係、完整假設三層的 P/R，並列出 HICSS 2025（完整假設召回 39.3%）與 TheoryOn 作為參考，註明資料集不同。
    - dev／test 以 paper_id 的雜湊固定切分；few-shot 範例不得取自 test。
    - LLM 評審只做二元判定，並先用約 40 筆人工標籤校準。
17. **微調既有 RAG Must 的引用做法**（工時屬 Should 級）：
    - 改用 Anthropic 的 `search_result` 區塊，`source="paper:{openalex_id}#chunk:{n}"`，由伺服器端驗證每一個引用索引。
    - 問答路徑維持「散文＋引用」，因為只要求 JSON 會讓引用變少。
    - 分類與萃取路徑用 structured outputs：數值範圍仍由 Pydantic 驗證，enum 不分大小寫比較，`refusal` 與 `max_tokens` 截斷都當作有型別的錯誤處理。
    - 〔需確認所用的模型支援 `search_result`〕
18. **新增 Should 清單並排定順序**：依第 6.1 節表格末尾的順序。S12 重新開啟時，同時加上以下限制：Turnstile、每 IP 限流、問題不超過 500 字、top-k 不超過 8、max_tokens 不超過 800、30 秒逾時、每人同時只能有一個進行中的請求。
19. **3D（已決定為 Could）提供兩個選項**：(a) 用同一份離線座標渲染 20–30 秒的飛越影片，放在首頁與 60 秒介紹影片；(b) 互動 3D 在 v1 之後限時 8 h。所有圖的座標一律離線計算，並以 `layout_version` 版本化，1 GB VM 不跑 UMAP。由 Kevin 決定，建議先做 (a)。
20. **2D ego network（已決定為 Should）採用 Connected Papers 與 Open Syllabus 的慣例**：入口是搜尋框加前 N 名清單；節點大小代表研究數，顏色代表新近程度，邊的粗細代表證據強度；側欄列出「最常被檢驗的前因」與「最新的結果變數」。
21. **非功能需求加入第 5(b) 節的慣例**，並各寫一條驗收條件。
22. **架構決策（寫成 ADR）**：
    - VM 只做服務層；重運算用同一個 Docker 映像＋CLI，在 Kevin 的 Mac 或 Windows 上跑。
    - GitHub Actions 只做 CI；排程放在 VM。
    - 不引入 Redis、自架 Langfuse 或 Sentry、獨立的向量資料庫，也不引入編排器。
    - 資料來源的註冊表，等第二個來源出現再抽出來。
23. **MCP（已決定為 phase-2 Could）的範圍**：
    - 用 Python SDK v2 的 `MCPServer`，做本機 stdio＋個人存取 token，或公開唯讀的遠端伺服器。
    - 只暴露 3–6 個精選工具（search_papers、get_paper、find_construct、topic_trend、ask_library），不要自動鏡像整個 API。
    - 遠端存取私有資料需要完整的 OAuth 2.1 資源伺服器，列為 Later。
24. **寒假前（12/25 之前）要做的事**：
    - 用 OpenAlex `group_by` 量出 14 本期刊的 OA 比例與授權組合（約 42 次列表呼叫，費用不到 $0.01）。
    - 寄信給圖書館（8.1）。
    - 詢問系辦的 AI 規範（8.2）。
    - 給老師看 brief，並詢問 DSR 與帳號歸屬（8.3）。
    - 在 Anthropic Console 核對 Haiku 5.5 的價格，再寫模型選擇 ADR。

## 8. 要確認的事

### 8.1 中央大學圖書館（建議寒假前寄出，併入 pending notes 已規劃的那封信）

1. 本校（含 CONCERT 聯盟）的電子資源授權，是否允許把訂閱論文的全文送到第三方生成式 AI 服務處理？這裡指的是透過 API 使用，而且資料不會被拿去訓練。如果不行，送短摘錄可以嗎？授權裡有沒有 TDM 條款？
2. 以下模式是否符合規定：每位使用者用自己的權限逐篇下載，只在個人帳號內處理，實驗室只共享構念、假設等衍生事實？
3. 圖書館有沒有 OpenURL（link resolver）的基底網址，可以讓我們做連結？
4. 機構典藏的 OAI-PMH 端點（`http://ir.lib.ncu.edu.tw:8081/ir-oai/request`）是否仍在服務？能否依系所或指導教授篩選？有沒有頻率限制？
5. 想確認一下我們的理解：Scopus API 的學術條款禁止在網站上公開顯示資料 ([Elsevier](https://dev.elsevier.com/academic_research_scopus.html))，所以我們不打算用 Scopus 補摘要。Elsevier 的期刊還有其他合法補摘要的途徑嗎？

### 8.2 系辦／教務處

1. 資管所是否已有、或即將推出學位論文的 AI 使用聲明書（類似中山管院 2026、台大 2026 的規定）？是否禁止用 AI 生成研究問題？口試時需要附上 AI 使用紀錄嗎？
2. 中央大學 2023 年表明「不禁止使用」之後，是否有更新的學位論文規定？
3. Azure for Students 到期後，系上或電算中心能否提供實驗室使用的 VM？

### 8.3 指導教授

1. 碩論是否接受 DSR？還是老師傾向「行為研究碩論＋工具另投研討會（例如 ICIM）」？
2. AI 使用計畫是否需要在提案前經過老師核可？老師希望看到哪些紀錄？
3. 帳號歸屬：GitHub organization、Anthropic workspace、網域與 Zotero 群組應該由誰擁有？（Zotero 群組的儲存空間算在擁有者的額度上。）
4. 研究雷達期刊版的環，老師是否願意和 Kevin 一起決定，取代 Thoughtworks 的顧問委員會角色？
5. 老師是否需要題目登記與撞題提醒，以及國科會計畫用的圖表匯出？這會決定 C08 是否升為 Should。
6. 做 15 篇雙人標註（Cohen's kappa）的同學人選與時間。

### 8.4 研究無法查證的項目

- 14 本期刊的 OA 比例與授權組合。這決定全文萃取值不值得做，可用第 7 節第 24 點的方法量測。
- Walden 改版是否重算了舊論文的 keywords 與 topics（M12 會實測）。
- AISeL 的 OAI 集合名稱與使用條款；被誤歸到 JAIS 的約 9,200 筆會議論文，是否已被「DOI 必填」規則排除；用 DOI 前綴 `10.17705/1jais` 篩選是否可靠〔未查證〕。
- INN、TheoryOn、nomologicalnetwork.org 是否仍在線；ALIGNS 的資料是否已公開。
- NDLTD 有沒有 API 或 OAI-PMH。目前沒找到，只能用 RIS 匯出與連結。
- Azure B1MS 的 PostgreSQL 版本（決定能不能用內建的 `uuidv7()`）、pgvector 在 Burstable 層的支援與 halfvec、預設的備份保留期，以及 12 個月免費期之後的價格。
- GROBID 的記憶體需求；Procrastinate 的 worker 能否跑在 FastAPI 的行程裡。
- Haiku 5.5 的官方價格頁。目前的價格來自二手來源，但與官方的 caching、batch 文件一致；發布日期則有 10/7 與 10/15 兩種說法。另外，所用的模型是否支援 `search_result` 引用。
- 智慧局生成式 AI 著作權指引的正式版、著作權法第 52 條的原文、用 LLM 摘要他人的摘要是否另有疑慮，以及 CONCERT 的授權是否有 AI 條款。
- 教育部在 2024 年之後是否發布了高教的生成式 AI 指引。2024 年 9 月時尚未發布 ([聯合報](https://udn.com/news/story/6885/8212676))。
- LINE 官方帳號 2026 年在台灣的免費額度、Zotero API 的認證細節、Cloudflare R2 的免費額度、GitHub Actions 私有 repo 的免費分鐘數，以及 NotebookLM 免費版的來源上限（目前的數字來自競品的部落格 ([Elephas](https://elephas.app/blog/notebooklm-source-limits))）。
- 研討會的主題是否領先期刊的主題。這目前只是假說。

### 8.5 筆記之間的衝突與處理方式

| 衝突 | 處理 |
|---|---|
| GitHub runner 規格：公開 repo 為 4 vCPU／16 GB、私有 repo 為 2 vCPU／8 GB。資料筆記建議把分析程式放到公開 repo 換取 16 GB；演進性筆記則建議重運算在本機跑 | repo 是私有的。重運算用同一個 Docker 映像，在 Kevin 的 Mac 或 Windows 上跑；8 GB 處理約 2.5 萬篇也夠。CI 只跑測試，排程放在 VM |
| OpenAlex 沒帶 key 時的行為：群組公告說沒有 key 只有 100 credits，之後會回 409；說明頁則描述沒有 key 時每秒可呼叫 10 次 | 一律帶 key（Kevin 已經有了），不依賴沒有 key 時的行為 |
| OpenAlex 每頁筆數：說明頁寫上限 100，資料筆記的成本模型卻假設 200（未驗證） | 用 100；全量成本仍約 $0.01–0.02 |
| polite pool：實驗室筆記建議 OpenAlex 與 Crossref 都用帶 `mailto` 的 polite pool | OpenAlex 的 polite pool 已經停用；Crossref 的還在，單筆每秒 10 次、列表每秒 3 次 ([Crossref](https://crossref.org/blog/announcing-changes-to-rest-api-rate-limits/)) |
| qwen3-embedding 的上下文長度：changelog 寫 4,096，模型頁寫 8,192 | 摘要遠低於兩者；全文切塊以 4,096 為上限 |
| Haiku 5.5 的價格只見於二手來源，發布日期也有兩種說法 | 價格與官方 caching、batch 文件一致；寫 ADR 前在 Console 核對 |
| 跨產業筆記建議「刪掉 3D 來支付新 Must」 | 3D 本來就是 Could，刪它省不到 Must 工時；必須從 Must 內部刪（6.2） |
| 要不要新增 Must：實驗室筆記主張一項都不加，其他筆記各提了 2–4 項 | 用「S 級且能降低風險」篩選後，保留 12 項（另加 1 項非功能），並用刪減支付 |
| 預留範圍：跨產業筆記估 3–4 h，實驗室筆記估 6–8 h，演進性筆記估 20–30 h | 以「第一次全量執行前不做就回不去」為界：架構淨增約 11 h（5a），其餘歸為慣例（5b）或可以等（5c） |
| Zotero 推送：實驗室筆記列為 Should | 降為 Could：RIS 匯出已經能手動匯入，預算優先給驗證 |
| 靜態 demo 快照：跨產業筆記列為 Should | 升為公開 demo 的上線版（K1），取代即時訪客問答 |
| 引用邊表：資料筆記建議在匯入時就建 `citations` 表（Must，只建 schema） | 改為保留原始 JSON（M03），引用邊等要做引用分析（C03）時再解析；OpenAlex 原始資料已含 `referenced_works`，符合 Rule of Three |
| MCP 的 Sampling／Logging 是被「移除」還是「棄用」 | 對 Research Radar 沒有影響，因為只做工具型伺服器 |

## 9. 不建議做的事

| 不做 | 原因 | 改做 |
|---|---|---|
| 自建文獻管理器（Word 內引用、樣式編輯、PDF 註記同步） | Zotero 免費，有群組與 Word 外掛，台灣的圖書館也在教它 ([台大圖書館](https://tul.blog.ntu.edu.tw/archives/tag/ndltd)) | RIS／BibTeX 匯出；之後做 Zotero 單向推送（C06） |
| PDF 翻譯與雙語閱讀 | 同學已經在用沉浸式翻譯，它有 Zotero 外掛 ([沉浸式翻譯](https://immersivetranslate.com/zh-TW/zotero-translator/)) | 中文結構化摘要只用來篩選 |
| v1 就做多輪「和 PDF 聊天」或代理式 RAG | NotebookLM 免費版已經涵蓋這個需求〔數字來自競品部落格〕；多輪加上工具，會湊齊「致命三要素」（私有資料＋不可信內容＋對外傳輸管道）([Willison](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)) | 單輪問答（已決定）；匯出給 NotebookLM 用的資料包（Could） |
| 超出 ego network 的引用圖探索 | ResearchRabbit、Litmaps、Connected Papers 免費或低價就有 ([Aaron Tay](https://aarontay.substack.com/p/researchrabbits-2025-revamp-iterative)) | 用 DOI 深連結過去 |
| 「共識計」式的結論；把「支持比例」當成效果強度；單一的合成動能分數 | 本質上是計票，忽略樣本數、效果量與異質性 ([Aaron Tay](https://aarontay.substack.com/p/a-2025-deep-dive-of-consensus-promises))；不透明的分數無法驗證 ([CB Insights](https://cbinsights.com/mosaic-score)) | 證據地圖＋可拆解的指標（S09、S16） |
| AI 代寫文獻探討；改寫或「去 AI 化」；一鍵複製段落 | 與台灣同儕學校的校規衝突 ([中山管院](https://rpb73.nsysu.edu.tw/static/file/234/1234/img/777/127846497.pdf)) | 文獻整理表、引用、AI 揭露匯出 |
| 爬 NDLTD、華藝、Google Scholar 或出版社網站；透過 VPN 大量下載；放影子圖書館連結 | 違反圖書館規定 ([中大圖書館](https://lib.ncu.edu.tw/ebook)) 與各網站的服務條款 | RIS 匯入、國圖開放資料、OAI-PMH |
| 實驗室共享付費 PDF 資料夾；把整個 PDF 庫放在 VM 上 | 著作權法的個人使用重製規定，不一定涵蓋實驗室共享，只能靠不確定的合理使用判斷 ([智慧局函釋摘要](https://www.rootlaw.com.tw/LetterContent.aspx?LCode=13&Soid=233275))；授權條款也可能另有限制 | 每人自行下載；只共享衍生事實 |
| 用 Scopus API 補 Elsevier 摘要並公開顯示 | 學術條款禁止在網站上公開顯示 ([Elsevier](https://dev.elsevier.com/academic_research_scopus.html)) | 覆蓋率標示（已決定）；用綠色 OA 補（C13） |
| Hype cycle；Adopt／Hold 這類建議性的環名；「空白＝研究缺口」；「預測下一個熱門題目」 | 實證薄弱 ([The Economist](https://www.economist.com/finance-and-economics/2024/08/19/artificial-intelligence-is-losing-hype))；地圖上的空白區域「很少真的是空的」([PatSnap](https://help.patsnap.com/hc/en-us/articles/115000409785)) | 描述性四環；寫「在這 14 本期刊中稀疏」 |
| 用互動動畫當分析工具；把關鍵發現藏在滑鼠懸停裡 | 「眼睛勝過記憶」：要比較很多個狀態時，動畫不適合 ([Munzner](https://cs.ubc.ca/~tmm/talks/vad/VAD-rules.pdf)) | 小倍數圖、靜態發現卡 |
| 排行榜、徽章、連續打卡 | 效果小，而且約 4 週後開始衰退 ([Rodrigues et al.](https://link.springer.com/article/10.1186/s41239-021-00314-6)) | 最多做實驗室的共同目標（Later） |
| 在 1 GB VM 上自架 Langfuse、Sentry、GlitchTip、Prometheus 或 Grafana；用 Kubernetes；用 Redis＋Celery；架獨立向量資料庫；用 Dagster 或 Airflow | 光是 Langfuse 自架的最低需求合計就約 21.5 GiB ([Langfuse](https://langfuse.com/self-hosting/scaling)) | PostgreSQL 帳本＋SaaS 免費層＋pgvector＋CLI |
| 現在就做多租戶 RLS、領域外掛框架、entry-point 外掛、旗標服務、GraphQL、event sourcing、PROV-O 或 RDF | 違反 Rule of Three | `workspace_id`＋visibility＋「領域」資料列＋typed settings |
| GraphRAG 或 LightRAG；模型路由；語意快取 | 資料庫問答靠 SQL 更準，也更便宜 ([TAG](https://arxiv.org/pdf/2408.14717)) | 參數化 SQL 工具；精確比對快取 |
| 把商業 AI 防火牆當主要防線 | 95% 的攔截率，在資安領域等於不及格 ([Willison](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)) | 斷開致命三要素（M10） |
| 標註不到 50–200 筆就上 DSPy 或微調 | 這是 DSPy 自己訂的資料門檻 ([DSPy](https://dspy-docs.vercel.app/building-blocks/6-optimizers)) | 先累積修正事件（M05） |
| 在 Postgres 裝中文斷詞擴充 | Azure 不支援 ([Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/5825157/when-can-azure-for-postgresql-support-pg-bigm-exte)) | 用 LLM 改寫查詢（S04） |
| 採用 AGPL 授權 | Google 禁止內部使用 AGPL 程式碼 ([Google](https://opensource.google/docs/using/agpl-policy/)) | MIT 或 Apache-2.0 |
| 把老師未發表的計畫書或審查中的稿件放進 AI | 台大把這列為抄襲或違反保密 ([台大](https://ori.ntu.edu.tw/ethics/download/fileSn/25)) | 只處理已出版的文獻 |
| 把 GitHub Actions cron 當正式排程 | 公開 repo 的排程 60 天沒有活動就會自動停用 ([GitHub](https://docs.github.com/en/actions/using-workflows/disabling-and-enabling-a-workflow))；而且資料庫必須對外開放 | 排程放在 VM 上 |
| 為了廣度犧牲驗證；忙著收集證照 | 「完成勝過野心」([MLH](https://news.mlh.io/the-ai-coding-skills-recruiters-are-looking-for-now-05-14-2026)) | 先上線一個薄而完整的垂直切片 |

## 10. Sources

**政策與學術誠信**

- [國立中山大學管理學院〈學位論文學術誠信暨AI使用聲明書〉（2026）](https://rpb73.nsysu.edu.tw/static/file/234/1234/img/777/127846497.pdf)
- [國立臺灣大學〈生成式AI使用與研究誠信指引〉（2026 年 6 月）](https://ori.ntu.edu.tw/ethics/download/fileSn/25)
- [國際事務系碩士論文生成式 AI 使用規範與自我檢核要點（文藻外語大學網域，2026）](https://c030.wzu.edu.tw/datas/upload/files/國際事務系碩士論文生成式_AI_使用規範與自我檢核要點_中文版%20-%20Copy%201.pdf)
- [MIS Quarterly Executive — Publication Ethics](https://aisel.aisnet.org/misqe/publication_ethics.html)
- [國立中央大學高教深耕計畫：112 學年 ChatGPT 使用原則](https://hesp.ncu.edu.tw/farmer/index.php/en/2018-05-14-05-44-10/2023-06-08-06-48-42/2023-06-07-00-53-00/585-112-chatgpt)
- [聯合報／中央社：教育部尚未訂定大學生成式 AI 指引（2024-09-07）](https://udn.com/news/story/6885/8212676)
- [Flemyng et al. (2025) Position statement on AI use in evidence synthesis, JBI Evidence Synthesis](https://pmc.ncbi.nlm.nih.gov/articles/PMC12594113/)
- [Rethlefsen et al. (2021) PRISMA-S, Systematic Reviews](https://pmc.ncbi.nlm.nih.gov/articles/PMC7839230)

**著作權與授權**

- [工商時報：智慧局〈生成式人工智慧著作權指引〉（2026-08-05）](https://www.ctee.com.tw/news/20260805701974-430503)
- [University of Waterloo Library: Use library resources with AI (2026)](https://uwaterloo.ca/lib/research-supports/copyright-and-licensing/use-library-resources-ai)
- [國立中央大學圖書館：電子資源使用規範](https://lib.ncu.edu.tw/ebook)
- [法源：智慧局 2021-03-09 函釋摘要（著作權法第 50、51、65 條）](https://www.rootlaw.com.tw/LetterContent.aspx?LCode=13&Soid=233275)
- [Elsevier Developer Portal: Scopus API for academic research](https://dev.elsevier.com/academic_research_scopus.html)
- [OpenAlex help: Full text (PDF and GROBID TEI)](https://help.openalex.org/access/fulltext.md)

**OpenAlex 與其他資料來源**

- [OpenAlex users group: API key requirement announcement (J. Priem, 2026-01-13)](https://groups.google.com/g/openalex-users/c/rI1GIAySpVQ)
- [OpenAlex blog (2025–2026 posts: Walden, usage-based pricing, Unpaywall update)](https://blog.openalex.org/author/jason)
- [OpenAlex help: Authentication and rate limits](https://help.openalex.org/api/authentication/)
- [OpenAlex help: Work attributes](https://help.openalex.org/data/works/attributes.md)
- [OpenAlex help: Semantic search](https://help.openalex.org/api/semantic-search.md)
- [OpenAlex help: How do I import OpenAlex data into VOSviewer](https://help.openalex.org/hc/en-us/articles/27218772811543-How-do-I-import-OpenAlex-data-into-VOSviewer)
- [Crossref documentation: Retraction Watch](https://crossref.org/documentation/retrieve-metadata/retraction-watch/)
- [Crossref blog: Retraction Watch retractions now in the Crossref API (2025-01-29)](https://www.crossref.org/blog/retraction-watch-retractions-now-in-the-crossref-api/)
- [Crossref blog: Announcing changes to REST API rate limits (2025-11-05)](https://crossref.org/blog/announcing-changes-to-rest-api-rate-limits/)
- [Hauschke & Nazarovets: OpenAlex is_retracted false positives (arXiv 2403.13339)](https://arxiv.org/abs/2403.13339v1)
- [COAR Repository Registry: AIS Electronic Library (AISeL)](https://ird.coar-repositories.org/systems/8fa69709-76b1-4cc4-9589-0160456e0503?lang=en)
- [CORE data provider: AIS Electronic Library (AISeL)](https://core.ac.uk/data-providers/10830/?offset=10)
- [Digital Commons help: Digital Commons and OAI-PMH](https://digitalcommons.elsevier.com/digital-commons-and-oai-pmh)
- [OpenCitations Index REST API v2.2.0](https://api.opencitations.net/index)
- [NCHC SciDM：國家圖書館臺灣博碩士論文系統書目開放資料](https://scidm.nchc.org.tw/en/dataset/best_wish14024)
- [PhilPapers OAI archive listing: NCU Institutional Repository](https://api.philpapers.org/archives/view.pl?id=1649)
- [臺大圖書館部落格：NDLTD 書目以 RIS 匯入 Zotero（2026）](https://tul.blog.ntu.edu.tw/archives/tag/ndltd)

**檢索、嵌入與 LLM 技術**

- [Qwen3 Embedding technical report (arXiv 2506.05176)](https://arxiv.org/pdf/2506.05176)
- [Cloudflare Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [Microsoft Q&A: pg_bigm support on Azure Database for PostgreSQL (2026-03)](https://learn.microsoft.com/en-us/answers/questions/5825157/when-can-azure-for-postgresql-support-pg-bigm-exte)
- [pgvector 0.8.5 FAQ (pgEdge mirror)](https://docs.pgedge.com/pgvector/v0-8-5/frequently-asked-questions/)
- [Nile: UUIDv7 comes to PostgreSQL 18](https://thenile.dev/blog/uuidv7)
- [TAG: Text2SQL is Not Enough (arXiv 2408.14717)](https://arxiv.org/pdf/2408.14717)
- [Microsoft Research: LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)
- [Anthropic docs: Batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- [Anthropic docs: Search results (citations)](https://platform.claude.com/docs/en/build-with-claude/search-results)
- [Anthropic docs: Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs.md)
- [datamapplot documentation: Interactive plots](https://datamapplot.readthedocs.io/en/latest/interactive_intro.html)
- [BERTopic on PyPI](https://pypi.org/project/bertopic)
- [Nomic blog: Data mapping](https://home.nomic.ai/blog/posts/data-mapping)
- [bibliometrix v5.5.0 release notes](https://github.com/massimoaria/bibliometrix/releases/tag/v5.5.0)
- [DSPy optimizers (documentation mirror)](https://dspy-docs.vercel.app/building-blocks/6-optimizers)
- [Dagster: Asset versioning and caching](https://dagster.io/docs/guides/build/assets/asset-versioning-and-caching)
- [The Semantic Scholar Open Data Platform (arXiv 2301.10140)](https://arxiv.org/abs/2301.10140)
- [Elicit: How we evaluated Elicit Systematic Review (2025)](https://elicit.com/blog/how-we-evaluated-elicit-systematic-review)
- [Procrastinate on PyPI](https://pypi.org/project/procrastinate)

**構念、評估與學術方法**

- [Larsen & Bong (2016) A tool for addressing construct identity, MIS Quarterly (CU Boulder VIVO)](https://vivo.colorado.edu/display/pubid_148364)
- [Larsen et al. (2025) ALIGNS (arXiv 2509.09723)](https://arxiv.org/pdf/2509.09723)
- [Wulff & Mata (2025) Semantic embeddings reveal and address taxonomic incommensurability, Nature Human Behaviour](https://pmc.ncbi.nlm.nih.gov/articles/PMC12106064)
- [Funk & Scharfenberger (2025) Construct relation extraction from scientific papers, HICSS-58](https://fis.leuphana.de/en/publications/construct-relation-extraction-from-scientific-papers-is-it-automa/)
- [Li, Larsen & Abbasi (2020) TheoryOn, MIS Quarterly (NSF PAR)](https://par.nsf.gov/servlets/purl/10282376)
- [Wagner, Lukyanenko & Paré (2022) Artificial intelligence and the conduct of literature reviews, JIT](https://libraopen.lib.virginia.edu/public_view/st74cq58c)
- [Hong et al. (2014) Context-specific theorizing, Information Systems Research](https://econpapers.repec.org/RePEc:inm:orisre:v:25:y:2014:i:1:p:111-136)
- [AIS Transactions on Replication Research: Author information](https://aisel.aisnet.org/trr/authorinfo.html)
- [Sandberg & Alvesson (2011) Ways of constructing research questions, Organization](https://lup.lub.lu.se/record/1925769)
- [MIS Quarterly: Review process](https://misq.org/review-process)
- [臺北科技大學管理學院：ICIM 2026 新聞（2026-05-20）](https://cmgt.ntut.edu.tw/p/16-1018-154174.php?Lang=en)
- [Chen, Allot & Lu (2021) LitCovid, Nucleic Acids Research](https://academic.oup.com/nar/article/49/D1/D1534/5964074)
- [Eugene Yan: An LLM-as-judge won't save your product](https://eugeneyan.kit.com/posts/an-llm-as-judge-won-t-save-your-product-fixing-your-process-will)
- [Simon Willison summarizing Hamel Husain on LLM-as-a-judge (2024-10-30)](https://simonwillison.net/2024/Oct/30/llm-as-a-judge)
- [Aaron Tay: A 2025 deep dive of Consensus (2025-11-15)](https://aarontay.substack.com/p/a-2025-deep-dive-of-consensus-promises)
- [Aaron Tay: ResearchRabbit's 2025 revamp (2025-11-02)](https://aarontay.substack.com/p/researchrabbits-2025-revamp-iterative)
- [Walters & Wilder (2023) Fabrication and errors in ChatGPT citations, Scientific Reports](https://www.nature.com/articles/s41598-023-41032-5.pdf)
- [Wiley ExplanAItions 2025 press release (2025-10-07)](https://newsroom.wiley.com/press-releases/press-release-details/2025/AI-Adoption-Jumps-to-84-Among-Researchers-as-Expectations-Undergo-Significant-Reality-Check/default.aspx)
- [Elsevier Insights 2024: Attitudes toward AI press release (2024-07-09)](https://www.elsevier.com/about/press-releases/the-insights-2024-attitudes-toward-ai-report-reveals-researchers-and-clinicians-believe-in-ai-potential-but-demand-transparency-in-order-to-trust-tools)

**跨產業做法**

- [3ie: How to read an Evidence Gap Map (2024)](https://www.3ieimpact.org/sites/default/files/2024-03/how-to-read-egm-document-Feb2024.pdf)
- [White et al. (2020) Guidance for producing a Campbell evidence and gap map](https://pmc.ncbi.nlm.nih.gov/articles/PMC8356343)
- [Montgomery et al. (2019) GRADE primer, BMJ Global Health](https://pmc.ncbi.nlm.nih.gov/articles/PMC6350753)
- [Cochrane: Living systematic review guidance (2019)](https://resources.cochrane.org/sites/resources.cochrane.org/files/uploads/inline-files/Transform/201912_LSR_Revised_Guidance.pdf)
- [Clarivate: ThemeScape user guide (Japanese, 2024)](https://clarivate.com/intellectual-property/ja/download/di-analyst-themescape-textclustering-ja)
- [PatSnap help center: Landscape](https://help.patsnap.com/hc/en-us/articles/115000409785)
- [Thoughtworks Technology Radar FAQ](https://www.thoughtworks.com/de/radar/faq)
- [Backstage tech-radar plugin README](https://github.com/backstage/community-plugins/blob/main/workspaces/tech-radar/plugins/tech-radar/README.md)
- [Rotolo, Hicks & Martin (2015) What is an emerging technology? (arXiv 1503.00673)](https://arxiv.org/abs/1503.00673)
- [The Economist (2024-08-19) Artificial intelligence is losing hype](https://www.economist.com/finance-and-economics/2024/08/19/artificial-intelligence-is-losing-hype)
- [CB Insights: Mosaic score](https://cbinsights.com/mosaic-score)
- [GOV.UK: Weak signals and trend analysis horizon scanning (2025)](https://www.gov.uk/government/publications/weak-signals-and-trend-analysis-horizon-scanning/weak-signals-and-trend-analysis-horizon-scanning)
- [Rodrigues et al. (2022) Gamification over a semester, IJETHE](https://link.springer.com/article/10.1186/s41239-021-00314-6)
- [Sailer & Homner (2020) The gamification of learning: a meta-analysis, Educational Psychology Review](https://link.springer.com/10.1007/s10648-019-09498-w)
- [Feedly: Feedly AI and mute filters (2019)](https://feedly.com/new-features/posts/feedly-ai-and-mute-filters)
- [ORKG Academy: Getting started](https://academy.orkg.org/getting-started.html)
- [MakerKit: Multi-tenant SaaS architecture (2026)](https://makerkit.dev/blog/md/tutorials/multi-tenant-saas-architecture)

**視覺化與大眾溝通**

- [Nielsen Norman Group: How long do users stay on web pages](https://nngroup.com/articles/how-long-do-users-stay-on-web-pages/)
- [FlowingData: Interactive visualization is still alive (2017)](https://flowingdata.com/2017/04/05/interactive-visualization-is-still-alive/)
- [Open Culture: Open Syllabus Galaxy (2021)](https://www.openculture.com/2021/02/the-open-syllabus-project-visualizes-the-1000000-books-frequently-assigned-in-college-courses.html)
- [ADMA: The winning formula behind Spotify Wrapped (2026)](https://adma.com.au/resources/personal-shareable-no-strings-attached-the-winning-formula-behind-spotify-wrapped)
- [Munzner: Visualization Analysis & Design — rules of thumb](https://cs.ubc.ca/~tmm/talks/vad/VAD-rules.pdf)
- [McGuffin, Servera & Forest (2022) Path tracing in 2D vs 3D network layouts in VR (arXiv 2207.11586)](https://ar5iv.labs.arxiv.org/html/2207.11586)
- [Connected Papers: Announcement (2020)](https://medium.com/connectedpapers/announcing-connected-papers-a-visual-tool-for-researchers-to-find-and-explore-academic-papers-89146a54c7d4)

**工程、安全、營運與整合**

- [OWASP GenAI: LLM10:2025 Unbounded Consumption](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/)
- [Simon Willison: The lethal trifecta (2025-06-16)](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
- [The Decoder: Researchers hide prompts in scientific papers (2025-07)](https://the-decoder.com/researchers-hide-prompts-in-scientific-papers-to-sway-ai-powered-peer-review/)
- [Google SRE Book: Data integrity](https://sre.google/sre-book/data-integrity/)
- [Healthchecks.io documentation](https://healthchecks.io/docs/healthchecks_cronitor_comparison/)
- [Cloudflare Turnstile plans](https://developers.cloudflare.com/turnstile/plans/)
- [Langfuse: Self-hosting scaling](https://langfuse.com/self-hosting/scaling)
- [GitHub Docs: GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub Docs: Disabling and enabling a workflow](https://docs.github.com/en/actions/using-workflows/disabling-and-enabling-a-workflow)
- [Google Open Source: AGPL policy](https://opensource.google/docs/using/agpl-policy/)
- [WorkOS: The MCP 2026 spec and agent authentication](https://workos.com/blog/mcp-2026-spec-agent-authentication)
- [MCP Python SDK v2.0.0 release notes](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
- [FastMCP: FastAPI integration](https://gofastmcp.com/v3/integrations/fastapi)
- [Zotero Web API v3: Write requests](https://www.zotero.org/support/dev/web_api/v3/write_requests)
- [Zotero Web API v3: Syncing](https://www.zotero.org/support/dev/web_api/v3/syncing)
- [UNC Libraries: Zotero groups guide](https://guides.lib.unc.edu/zotero/groups)
- [Apprise on PyPI](https://pypi.org/project/apprise)
- [CoLRev documentation](https://colrev.readthedocs.io)
- [沉浸式翻譯：Zotero 翻譯外掛](https://immersivetranslate.com/zh-TW/zotero-translator/)
- [Elephas: NotebookLM source limits（競品部落格，2026）](https://elephas.app/blog/notebooklm-source-limits)
- [數位時代：LINE Notify 終止服務與替代方案](https://www.bnext.com.tw/article/88845/)

**業界招募訊號**

- [MLH: The AI coding skills recruiters are looking for now (2026)](https://news.mlh.io/the-ai-coding-skills-recruiters-are-looking-for-now-05-14-2026)
- [Canva Engineering: Yes, you can use AI in our interviews (2025-06-11)](https://canva.dev/blog/engineering/yes-you-can-use-ai-in-our-interviews)
- [Cake：工程師履歷與作品集建議（2026-09-18）](https://www.cake.me/resources/engineer-resume-tips)
- [104 人力銀行：台積電 IT 基礎架構工程師職缺](https://www.104.com.tw/job/8qguz)
- [HackMD：台積電 IPC(S) 工程師面試心得](https://hackmd.io/@edJH8dxaRPOaOfJQPav3XQ/SJHZGHQLs)
- [Entrepreneur: Google and McKinsey reintroduce in-person interviews (2025-08-18)](https://www.entrepreneur.com/business-news/google-mckinsey-reintroduce-in-person-interviews-due-to-ai/496041)
- [Built In: NVIDIA Software Engineer, Hardware Tools and Methodology – New College Grad 2026](https://builtin.com/job/software-engineer-hardware-tools-and-methodology-new-college-grad-2026/9601677)

**交接與永續**

- [Nucleic Acids Research (2020) availability of bioinformatics web tools (BVS record)](https://pesquisa.bvsalud.org/portal/resource/pt/mdl-33270886)
- [Mpungose et al. (2025) Exploring knowledge transfer and retention, GKMC](https://www.emerald.com/gkmc/article/74/11/178/1267149/Exploring-knowledge-transfer-and-retention)
- [Microsoft Learn: Azure for Students](https://learn.microsoft.com/en-au/azure/education-hub/azure-dev-tools-teaching/azure-students-program)
- [InfoQ: Oracle Cloud free tier limits (2026-07)](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
- [Software Sustainability Institute: Dealing with the bus factor](https://www.software.ac.uk/blog/dont-fear-reaper-dealing-bus-factor)
- [Fehr, Himpe, Rave & Saak (2021) Research software handover, JORS](https://refworks.reference-global.com/article/10.5334/jors.307)
