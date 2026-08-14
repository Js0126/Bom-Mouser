# 架構參考

這份是給想深入了解實作細節、或要動程式碼的人看的技術參考。**第一次用這個
專案,請先看 [README.md](README.md)**——那份是安裝、快速上手、常見問題;
這份是節點怎麼運作、每個欄位怎麼算出來的完整說明。

## 四個節點 vs 五個 Skill

`mouser_lookup.py` 本身照四個節點設計:

| 節點 | 說明 | 是否需要 LLM |
| --- | --- | --- |
| A. bom_parsing | 讀取 BOM,自動偵測標題列 + 欄位別名對照,濾掉 PCB/包材等非採購項 | 否,純腳本 |
| B. mouser_lookup | 對每個唯一 MPN/關鍵字查 Mouser;查無結果且有設定 DigiKey 憑證時 fallback 查 DigiKey | 否,純 API 呼叫 |
| C. match_validation | 比對候選結果與原始 BOM 是否為同一料件,產出信心分數 | 底層(`match_validation()`)是規則式評分,不呼叫 LLM;規則式評分卡住的案例可另外交給 `bom-match-review` skill,由 Claude 在 session 裡親自判斷(免費、不是付費 API) |
| D. report_generation | 本機合併節點 A/B/C 結果,輸出 xlsx 報表(Summary + 明細兩個分頁) | 否,純腳本 |

但 `.claude/skills/` 底下實際是**五個** skill——節點 C 在實務上拆成兩件事:
**規則式評分**(`bom-match-score`)與**語意判斷**(`bom-match-review`)。
中間用 JSON artifact 串接,只有最後一步輸出 xlsx:

| Skill | 對應節點 | 輸入 → 輸出 | 會不會打外部 API |
| --- | --- | --- | --- |
| `bom-parse` | A | BOM 檔案 → `parsed_bom.json` | 否 |
| `bom-vendor-lookup` | B | `parsed_bom.json` → `vendor_candidates.json` | 是(唯一會打 Mouser/DigiKey 的一步) |
| `bom-match-score` | C(規則式) | + `vendor_candidates.json` → `match_scores.json` | 否 |
| `bom-match-review` | C(語意判斷) | `match_scores.json` 裡的模糊項 → `review_verdicts.json` | 否(Claude 在 session 裡親自判斷,不是付費 API) |
| `bom-report-finalize` | D | 以上全部(+ 選用的 `review_verdicts.json`)→ **最終 xlsx + html** | 否 |

好處:改了 `manufacturer_aliases.md` 或有新的 Claude 判決,只要重跑
`bom-match-score`/`bom-report-finalize`,**不需要**重新查詢 Mouser/
DigiKey——真正打外部 API 的只有 `bom-vendor-lookup` 這一步,而且有本機
快取,同一批 MPN 不會重複查。各 skill 的詳細用法見各自的 SKILL.md。

`bom-match-review` 是五個裡唯一沒有 `scripts/` 資料夾的 skill——那一步的
工作內容就是 Claude 的推理本身,沒有東西可以寫成腳本。少了這一步只是少一層
自動複核,其餘四步(含最終報表)不受影響。

## 1. API 憑證處理

- **Mouser**:`MOUSER_SEARCH_API_KEY`,必填,`query_mouser()`/`query_mouser_keyword()` 用。
- **DigiKey**:`DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`,選填。兩者都設定
  才會啟用 DigiKey fallback(2-legged OAuth / Client Credentials,免費申請於
  [developer.digikey.com/products/product-information-v4](https://developer.digikey.com/products/product-information-v4))。
  Access token 存在記憶體(不落地),約 10 分鐘過期會自動換新。
- 兩者都只從環境變數讀取(延遲到實際呼叫時才檢查,`import mouser_lookup`
  本身不需要任何 Key),程式碼與輸出中不得出現明文。
- `.env.example` 是範本,複製為 `.env` 並填入;已安裝 `python-dotenv` 的話,
  執行 `mouser_lookup.py` 會自動載入 `.env`。
- `.gitignore` 已排除 `.env`、快取檔 `.mouser_cache.json`、輸出的 `*.xlsx`。
- Windows 上若遇到 `SSLCertVerificationError`(常見於企業網路/防毒軟體的
  HTTPS 掃描),程式已用 `truststore` 改走系統原生憑證庫,不需要額外處理。

## 2. BOM 資料最小化

- 送往 Mouser/DigiKey 的 payload **只有 MPN**,或(缺 MPN 時)由
  `manufacturer + value + package + tolerance` 組成的關鍵字——一律是元件
  本身的技術屬性,不包含 ref_des / 專案代號 / 客戶名稱。
- `ref_des`、`manufacturer`、`bom_unit_price` 等欄位保留在本機的 `BOMLine`
  dataclass,查詢完成後才在節點 D(`build_report()`)重新合併回結果。
- 同一 MPN/關鍵字只查一次:記憶體去重 + 本機持久化快取
  `.mouser_cache.json`(結構為
  `{key: {"mouser": [...]|None, "digikey": [...]|None, "mouser_fetched_at": "YYYY-MM-DD"|None, ...}}`,
  分別記錄兩個供應商各自的查詢狀態與查詢日期),重跑 pipeline 不會重複打 API。

### ⚠ 快取沒有時效,報價是快照不是即時值

**快取沒有任何有效期限機制**:一個 key 查過一次,之後永遠命中快取,不管
是一週前還是一年前查的。而快取存的是供應商回傳的完整候選資料,包含
**單價階梯、MOQ、訂購倍數**——這些數字全部凍結在第一次查詢那一天。

這件事在畫面上看不出來(只會印「快取命中 N 筆」),所以報表會主動標示:

- **Summary 分頁**:「報表產生日期」與「供應商報價查詢日期」兩列分開列出。
  同一份報表的料件如果是不同時間查的,會顯示成日期區間(`最舊 ~ 最新`)。
- **BOM_vs_Vendors 分頁**:每一列都有 `data_fetched_at` 欄位。空白代表這筆
  來自加入日期欄位之前的舊快取,**日期不詳,不是今天查的**。

要拿到新報價,`--no-cache` 的語意是「這次不讀快取」而**不是**「刷新快取」
——它不會更新 `.mouser_cache.json`,下次不加參數又會吃到舊資料。要徹底
洗掉得刪檔:

```bash
rm .mouser_cache.json     # Windows cmd: del .mouser_cache.json
```

刪掉會整批重查,花查詢額度也花時間,所以不要每次都刪。**該重查的時機**:
準備真的下單、報價要給客戶、或距上次查詢已隔一段時間(元件價格與 MOQ
變動頻繁,缺料時期尤其明顯)。

## 3. 節點 A:BOM 解析

### 欄位映射

`parse_bom()` 用 `_COLUMN_ALIASES` 對照表自動猜測欄位(不分大小寫、忽略
底線/連字號/空白,同時涵蓋繁體、簡體中文與英文)。標準欄位:`ref_des`,
`mpn`, `manufacturer`, `qty_per_unit`, `description`, `value`, `package`,
`tolerance`, `bom_unit_price`, `category`。**若欄位命名對不上**,直接在
`_COLUMN_ALIASES` 對應 list 補別名即可,不用動邏輯本身。

`description` 允許同時比對到多個原始欄位(如「名稱」+「特性參數」)並串接,
其餘欄位取第一個命中的原始欄位。

### 標題列自動偵測

很多實際 BOM 檔案在真正的欄位標題列之前還有一列檔名/公司資訊。
`load_bom_file()` 會自動掃描前幾列,選出「比對到最多標準欄位」的那一列
當標題列,不用手動指定。

### 非採購項過濾

`filter_non_purchasable()` 會把 PCB 本身、包材、標籤等不會在 Mouser/DigiKey
查到報價的品項濾掉,不佔查詢額度。判斷依據是 `category` 欄位(名稱/名称)
是否命中 `_NON_PURCHASABLE_CATEGORIES` 清單。這些品項仍會出現在報表裡,
標記 `match_status = excluded_non_purchasable`。

## 4. 節點 B:Mouser + DigiKey 查詢

1. 有 MPN 的料件先用 Mouser partnumber 搜尋(`partSearchOptions=Exact`);
   沒有 MPN 的料件改用 Mouser keyword 搜尋。
   ⚠ **Mouser 的 keyword 搜尋端點只接受 ASCII 字元**——關鍵字裡出現
   `±`/`Ω`/`µ`/`μ` 這類符號,整個請求會被判 `InvalidCharacters`、回應
   HTTP 400(不是「查無此料件」,是請求本身被拒)。`_build_keyword()` 組完
   關鍵字後一定會經過 `_sanitize_keyword_text()` 淨化:`µ`/`μ` 轉成 ASCII
   的 `u`,`Ω` 轉成 `ohm`(直接砍成空字串會讓「10Ω」變成純數字「10」,
   Mouser 找不到明確單位、常常配到完全不相干類別的料件,例如電感、電容),
   `±` 直接移除,其餘漏網的非 ASCII 字元(含中文廠商名稱)一律捨棄——
   寧可送出一個資訊較少但查得到的關鍵字,也不要整筆直接失敗。partnumber
   搜尋(有明確 MPN 的路徑)不受這個限制,只有走 keyword fallback 的被動
   元件才會踩到。
2. **Mouser 查無結果**、且 `.env` 有設定 DigiKey 憑證時,用同一個查詢字串
   (MPN 或關鍵字)改查 DigiKey Product Information V4 keyword search。
   DigiKey 的候選會被轉換成跟 Mouser 完全相同的欄位形狀
   (`Manufacturer`/`ManufacturerPartNumber`/`Description`/`PriceBreaks`/
   `Min`/`Mult`),外加一個 `_source: "DigiKey"` 標記,這樣節點 C/D 完全
   不用管資料來自哪個供應商。
3. **節流**:預設每次請求間隔 1 秒(`MOUSER_RATE_LIMIT_SECONDS`,可調),
   Mouser 官方未公布硬限制,業界估算保守值約 30 req/min,目前設定已略高於
   此值,若開始遇到限流建議調高。DigiKey 官方限制寬鬆許多(120 req/min,
   1000 req/day),共用同一個節流設定。
4. **重試機制**:對逾時/連線錯誤/429/5xx 做指數退避重試(`MOUSER_MAX_RETRIES`,
   預設 3 次)。重試後仍失敗,拋出 `VendorQueryError`,標記為 `query_failed`
   ——**跟「供應商明確回應查無此料件」(`no_candidates`)刻意分開**,前者
   不會被寫進快取,重新執行同一條指令會自動只重試這幾筆。
5. 每查到 10 筆新結果就存一次快取,長批次查詢中途斷線不會弄丟已查到的結果。

## 5. 節點 C:match_validation 比對邏輯

信心分數 = `0.5 × 廠商相似度 + 0.5 × 描述相似度 + MPN 精確相符加成(0.15)`,
再套一個身分覆寫規則,細節如下:

- **廠商相似度**:先查 [manufacturer_aliases.md](manufacturer_aliases.md)
  這份縮寫對照表(如 `TI` → `Texas Instruments`),展開後再做完全比對 →
  包含比對 → 字串相似度(`difflib`)。**這份對照表是給人編輯的 markdown,
  新增縮寫不用改程式碼**,存檔後下次執行會自動重新讀取。
- **描述相似度**:取「字元序列相似度」與「token 命中率」兩者較大值。
  Token 命中率把 BOM 端組出的技術屬性(如 `1uf`/`10v`/`x5r`/`0402`)拆成
  token,看有多少比例真的出現在供應商回傳的 Description 裡——這樣 BOM
  跟供應商用不同語言/詞序描述同一顆料時也能拿到合理分數。中文字元在這一步
  會被濾掉(見 `_normalize_text()`),不是 bug——BOM 端的技術屬性本來就是
  英數字,濾掉中文之後兩邊比對的雜訊反而變少。
- **身分覆寫**:MPN 完全相符 + 廠商高度相符(≥0.85)時,分數至少墊到
  0.92,不會被供應商簡略的 Description(甚至完全不含規格數字)拖累到門檻
  以下——MPN+廠商在電子料件的世界裡已經是近乎確定的身分證明。
- 信心分數 **低於門檻(預設 0.7,可用環境變數 `MATCH_CONFIDENCE_THRESHOLD`
  調整)標記為 `needs_human_review`**。

`match_validation(bom_line, candidates)` 本身是規則式(rule-based)的近似
判斷,**不呼叫任何 LLM API**——這兩個參數已經是純本機/純供應商回傳資料,
不含 ref_des 以外的專案脈絡,設計上就是拿來當語意判斷的介接點用的。

真正的語意層級判斷已經實作出來了,但不是接在 `match_validation()` 裡面,
而是做成獨立的 `bom-match-review` skill:讀規則式評分卡住
(`needs_human_review`/`no_candidates`)的項目,由 Claude 在對話 session
裡親自用背景知識判斷(廠商縮寫、企業併購/品牌沿革、料號命名慣例),輸出
CONFIRM/REJECT/UNCERTAIN 判決,再由 `bom-report-finalize` skill 疊加成
`matched_by_claude`/`rejected_by_claude` 狀態。**全程不呼叫任何付費 LLM
API**——這是專案明確定下的規矩(見 `CLAUDE.md`),判斷力來自 Claude 當下
在 session 裡的推理,不是額外的 API 呼叫。細節見 `bom-match-review` 的
SKILL.md。

## 6. 節點 D:xlsx 報表輸出

`write_report_xlsx()` 輸出兩個分頁:

### Summary 分頁

- **報表產生日期**,以及**供應商報價查詢日期**(兩者是不同的事——快取沒有
  時效,今天產生的報表可能用的是幾個月前查的價格。同一份報表的料件若是
  分不同時間查的,會顯示成 `最舊 ~ 最新` 區間;有來自舊快取、日期不詳的
  料件時會另外標示筆數)
- 料件總數 / 唯一 MPN 數量
- 各 `match_status` 筆數統計
- 分貨幣列出的兩種總價(貨幣依供應商回傳而定,常見 TWD;若混到多種貨幣
  會警告,不會誤加總):
  - **單價總價**(忽略 MOQ)= Σ(單價 × BOM 需求數量)
  - **含 MOQ 的預算評估** = Σ(套用供應商 MOQ/訂購倍數規則後的實際下單金額)
- 三份 Top 10 高價料件表格(由高到低排序,皆已移除 ref_des):
  - 依單價排序(不看數量)
  - 依單價總價排序(忽略 MOQ)
  - 依含 MOQ 預算排序(常常跟前兩份排名差很多,MOQ 過高的料件會在這裡現形)

### BOM_vs_Vendors 分頁(逐筆明細)

共 26 欄,以 `_REPORT_COLUMNS`(`mouser_lookup.py`)為準:

| 來源 | 欄位 |
| --- | --- |
| BOM 原始資料 | `ref_des, mpn, manufacturer, qty_per_unit, bom_unit_price` |
| 比對結果 | `match_status, match_confidence` |
| 供應商報價(忽略 MOQ) | `mouser_unit_price, mouser_currency, mouser_line_total` |
| 供應商 MOQ 規則 | `moq, order_multiple, moq_order_qty, moq_unit_price, moq_line_total` |
| 供應商回傳的料件資訊 | `manufacturer_part_number, mouser_part_number, mouser_manufacturer, mouser_description` |
| 交期與現貨 | `lead_time, availability` |
| 資料來源與時間 | `price_source, data_fetched_at` |
| 價差 | `price_delta_pct` |
| Claude 語意複核 | `claude_verdict, claude_reasoning` |

- **`manufacturer_part_number` vs `mouser_part_number`——這兩個容易搞混,
  但是不同的東西**:
  - `manufacturer_part_number`:**製造商編號**(供應商回傳的
    `ManufacturerPartNumber`),例如 `B39941B8048P810`。BOM 沒填 MPN 的被動
    元件(靠 keyword 搜尋配對)在 `mpn` 欄一定是空的,要看這一欄才知道
    實際該買哪顆料
  - `mouser_part_number`:**Mouser 自己網站上的內部料號**,例如網站顯示成
    `871-B39941B8048P810`(前面 `871-` 是 Mouser 自己加的字首,不是型號的
    一部分)。想直接到 Mouser 網站核對或下單,要用這個號碼搜尋,不是
    `manufacturer_part_number`
  - ⚠ **`mouser_part_number` 只有 Mouser 有。** DigiKey 用的是自己一套完全
    不同的編號系統(概念上等價,但不是同一個號碼,也不能拿去 Mouser 網站
    搜尋),所以 `price_source = DigiKey` 的列這一欄**故意留空**,不會拿
    DigiKey 的內部料號冒充填進去
- `mouser_unit_price` 依 `qty_per_unit` 對應的價格階梯自動挑選
- `moq_order_qty` 依供應商的 MOQ/訂購倍數規則,算出滿足需求的最小合法下單量
  (可能大於 `qty_per_unit`),`moq_unit_price`/`moq_line_total` 是對應的
  單價與小計
- **`lead_time` / `availability`:交期與現貨數量**,原始文字直接保留供應商
  回傳的格式,不同供應商格式不一致,不強行正規化或換算。這兩欄不受上面
  「只有 Mouser 有」的限制,兩個供應商都可能有值。
  ⚠ DigiKey 目前只有 `availability`(來自 `QuantityAvailable`),沒有交期
  欄位,`lead_time` 會是空的
- `price_source`:這筆資料最後是從 `Mouser` 還是 `DigiKey` 查到的
- **`data_fetched_at`:這筆報價是哪一天查回來的**(快取沒有時效,見第 2 節)。
  空白代表資料來自加入這個欄位之前的舊快取,**日期不詳,不是今天查的**
- `price_delta_pct` = `(mouser_unit_price − bom_unit_price) / bom_unit_price × 100`,
  BOM 沒有原始單價時為空;**注意此欄位假設兩邊同貨幣,不會自動換算**,
  務必對照 `mouser_currency` 確認
- `claude_verdict` / `claude_reasoning`:只有跑過 `bom-match-review` 才有值

**`mouser_part_number`/`lead_time`/`availability` 這三欄第一次用請先小批量
驗證。** Mouser 端讀的是 `MouserPartNumber`/`LeadTime`/`Availability` 這三個
欄位名稱,是依公開 API 文件寫的,沒有拿真實帳號跑過完整回應驗證過——
`--limit 3` 跑完打開報表看這三欄有沒有值,不是整欄空白。抓不到的話多半是
Mouser 實際回傳的欄位名稱跟這裡假設的不同,去
`_mouser_part_number_lead_availability()` 調整。

### 輸出路徑:自動開資料夾(從節點 A 就開始,不是到節點 D 才開)

節點 A(`bom-parse`,或走 `adapters/normalize_compound_spec_bom.py` 那條路徑)
沒指定 `-o` 時,不是直接把 `parsed_bom.json` 丟在專案根目錄,而是先呼叫
`mouser_lookup.default_run_folder()` 建立一個
`{今天日期}_{來源 BOM 檔名}/` 資料夾,寫進裡面。節點 B/C(`bom-vendor-lookup`/
`bom-match-score`)沒指定 `-o` 時,預設直接沿用「輸入的 `parsed_bom.json`
所在的資料夾」(不會重新呼叫 `default_run_folder()` 自己再算一次日期——
避免 pipeline 跨夜執行時,同一個 run 因為「今天」變了被拆進不同名稱的
資料夾)。`bom-match-review` 產出的 `review_verdicts.json` 依 SKILL.md 指示
也存進同一個資料夾。節點 D(`bom-report-finalize`)一樣沿用該資料夾,
xlsx/html 用資料夾名稱當檔名前綴。

這樣一次 pipeline run 的全部中繼檔(`parsed_bom.json`/`vendor_candidates.json`/
`match_scores.json`/`review_verdicts.json`)跟最終報表都收在同一個資料夾——
**不同 BOM 從第一步就落在不同資料夾,不會共用固定檔名互相覆蓋**(這是這套
資料夾機制存在的主要原因:兩個 Claude Code session 同時處理不同 BOM 時,
原本各步驟預設寫死在專案根目錄的固定檔名會互相覆蓋)。

同一份來源 BOM 重跑(改完 `manufacturer_aliases.md` 重算分數、套用新的
`bom-match-review` 判決)會重用同一個資料夾直接覆蓋裡面的舊檔案,不會每次
重跑就多開一個資料夾——「重跑這一步很便宜」是這條 pipeline 的核心設計原則
之一。**注意:這解決不了「同一份 BOM、同一天、兩個 session 幾乎同時處理」
這種邊緣情況**——這種情況兩邊還是會落到同一個資料夾互相覆蓋,只是機率遠低於
「不同 BOM 撞名」。xlsx 本身若被使用者在 Excel 裡開著鎖住,檔名層級的
`_v2`/`_v3` 版本化重試邏輯不受影響,照常運作。使用者用 `-o` 明確指定路徑時,
完全不套用這層資料夾邏輯。

**這套資料夾邏輯只涵蓋節點 A~D 五個 skill 的預設輸出路徑,不含
`.mouser_cache.json`**——那是刻意設計成全專案共用的查詢快取(見〈2. BOM
資料最小化〉),資料夾隔離對它沒有作用,兩個 session 同時查詢仍可能互相
覆蓋對方剛寫入還沒存檔的快取項目(不是檔案損毀,是其中一方新查到的結果被
蓋掉、下次要重查)。

**`mouser_lookup.py` 自己的一次到底 CLI**(`python mouser_lookup.py
your_bom.xlsx` 這種直接跑完整流程、不透過五個 skill 的用法,見
`run_pipeline()`)現在也套用同一套資料夾邏輯了——不指定 `-o` 時一樣用
`default_run_folder()` 建「今天日期_BOM檔名」資料夾。這支 CLI 是
README〈快速開始〉主推給沒裝 Claude Code 的人用的入口,兩個人各自拿它
處理不同 BOM 也不會撞名互相覆蓋。用 `-o` 明確指定路徑時一樣完全不套用
這層邏輯,行為跟其他四步一致。

### HTML 視覺化摘要(`render_html_report()`)

`bom-report-finalize` 預設除了 xlsx,還會用 `render_html_report()` 輸出
一份同檔名的 `.html`(給人看的摘要頁面,不是給下游步驟讀的中繼檔)——
直接吃 `write_report_xlsx()` 也在用的同一份 `report`/`summary`(來自
`build_report()` / `compute_summary()`),**不重新計算任何金額**,兩份
輸出的數字保證一致。內容包含:單價總價/含 MOQ 預算的大數字、
`match_status` 分佈(區分「有報價但排除」vs「本來就沒報價」兩類)、
排除清單明細(BOM 記載 vs 供應商配到對照 + Claude 判斷理由)、Top 10
排行(單價最貴 / 對總價貢獻最大)。

樣板檔案在 `.claude/skills/bom-report-finalize/assets/report_template.html`,
用 `{{TOKEN}}` 佔位符 + Python 端純字串 `.replace()` 帶入資料(沒有引入
樣板引擎依賴);Top 10 / 排除清單的資料是以 JSON 形式內嵌進 `<script>`
標籤,頁面本身用少量內嵌 JS 渲染,離線可開、不依賴外部資源。不需要的話
`run.py` 加 `--no-html`。

## match_status 完整定義

| 狀態 | 意思 |
| --- | --- |
| `matched` | 信心分數 ≥ 門檻(預設 0.7),高信心配對成功 |
| `needs_human_review` | 有候選但信心分數 < 門檻,需人工確認 |
| `no_candidates` | 有實際查詢過,但 Mouser 與 DigiKey(若啟用)都明確回應查無此料件 |
| `missing_mpn` | 沒有 MPN,也湊不出關鍵字(value/package/tolerance/manufacturer 全空) |
| `query_failed` | 查詢過程失敗(網路/API 問題),不是「查無此料件」;重跑同一條指令會自動只重試這幾筆 |
| `excluded_non_purchasable` | 節點 A 判定為非採購項(PCB/包材等),未送去查詢 |
| `matched_by_claude` | 規則式評分卡在 `needs_human_review`,但 `bom-match-review` skill(Claude 用背景知識親自判斷,非付費 API)確認是同一顆料 |
| `rejected_by_claude` | `bom-match-review` skill 判斷供應商回傳的候選其實是不同料件(常見於關鍵字搜尋抓錯類別),明確跟「還沒判斷過」的 `needs_human_review` 分開 |

只有 `matched` 和 `matched_by_claude` 會被計入 Summary 分頁的總價/預算——
其餘狀態即使候選剛好帶了價格,也不代表那是真正該買的東西,不能算進總價
(不然總價會被明知是錯的配對灌水)。這些行仍然完整保留在 BOM_vs_Vendors
明細裡,只是不計入加總。

## 已知限制

- 節點 A 的欄位別名是通用猜測 + 已對過幾份實際 BOM,遇到欄位命名對不上的
  新格式,加別名或用 `adapters/` 前處理即可調整,見
  [CONTRIBUTING.md](CONTRIBUTING.md)。
- `manufacturer_aliases.md` 的「待確認」區列了幾個目前無法自動判斷的廠商
  對照(如某些通用/不指定廠商的代碼),需要人工確認過才會加進正式對照表
  ——不確定的縮寫不要自己猜著加,加錯了反而讓比對品質變差。
- `mouser_part_number`/`lead_time`/`availability` 的 Mouser 欄位名稱沒有拿
  真實帳號驗證過,見上面第 6 節的說明。
- CSV 讀取沒有指定編碼,Big5 編碼的 CSV(台灣供應商常見)可能會解碼失敗或
  亂碼;xlsx 不受影響。
- 本機自測(`test_pipeline.py`)全程 mock 資料,未對外連線;真正查詢需要
  網路權限與已設定的 API 憑證。
