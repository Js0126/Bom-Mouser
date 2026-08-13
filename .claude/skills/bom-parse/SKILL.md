---
name: bom-parse
description: Step 1 of the BOM pricing pipeline — parse a raw BOM file (xlsx/csv) into standardized JSON, auto-detecting the header row and filtering out non-purchasable lines (PCB itself, packaging materials). Use this when the user hands over a new BOM file and wants to start pricing or costing it — "幫我算這份 BOM 的料錢"/"這份 BOM 幫我報價"/"parse this BOM" — or when a previous run's column mapping looked wrong and needs re-checking on a small sample before spending API calls. This is the first of five chained BOM-pricing skills (bom-parse → bom-vendor-lookup → bom-match-score → bom-match-review → bom-report-finalize); run this one first whenever the user names a BOM file they haven't parsed yet in the current session.
---

# BOM Parse(節點 A)

把一份原始 BOM 檔案(xlsx/csv)讀進來,標準化成統一欄位,濾掉 PCB 本身、
包材等非採購項,輸出 `parsed_bom.json` 給下一步(`bom-vendor-lookup`)用。
純本機腳本,不打任何外部 API,跑起來很快、免費,可以放心多跑幾次。

架構全貌、`_COLUMN_ALIASES` 欄位別名機制、`_NON_PURCHASABLE_CATEGORIES`
非採購項判斷邏輯,詳見專案根目錄 `ARCHITECTURE.md` 的〈節點 A:BOM 解析〉。

## 執行

在**專案根目錄**(`mouser_lookup.py` 所在的目錄)執行:

```bash
python .claude/skills/bom-parse/scripts/run.py <bom_path> -o parsed_bom.json
```

Windows 主控台印中文可能亂碼,需要的話加 `PYTHONIOENCODING=utf-8`。

`load_bom_file()` 會自動掃描前幾列找標題列(很多 BOM 檔案第一列是檔名/
公司資訊,不是真正的欄位標題),終端機會印出「自動偵測到欄位標題列位於
第 N 列,命中 X 個標準欄位」——**這行印出來的命中數要看一下**,如果明顯
偏低(例如只命中 2-3 個),代表這份 BOM 的欄位命名可能跟 `_COLUMN_ALIASES`
對不上,不要悶著頭往下跑。

## 欄位對不上怎麼辦

打開 `parsed_bom.json` 看幾筆,確認 `mpn`/`manufacturer`/`ref_des` 這些
關鍵欄位有沒有抓對值(不是空字串)。**先分辨是哪一種問題**,這兩種的解法
完全不同:

**(a) 欄位名稱不一樣** — 原始 BOM 本來就有獨立的廠商欄,只是叫「Vendor」
而程式只認得「Manufacturer」。這種到專案根目錄的 `mouser_lookup.py` 找
`_COLUMN_ALIASES` 字典,幫對應的標準欄位加上那個別名就好(把 `"vendor"`
加進 `"manufacturer"` 那個 alias list)。

**不要改 `parse_bom()` 的邏輯本身**,這個函式的設計就是靠別名表運作,加
別名就夠了。改完重跑這個 skill 確認命中數提高了,再往下一步。

**(b) 一格塞了多個屬性** — 原始 BOM 把型號、廠牌、備註全部塞進同一個
「规格型号」欄位,例如 `STM32F030CCT6（品牌：ST）（备注：LQFP-48）`,或者
被動元件根本沒有型號、只有一整段規格描述文字。**這種加別名永遠解不了**
——別名比對是整欄一對一對應,不會拆解儲存格內容。

改用 `adapters/normalize_compound_spec_bom.py` 取代這個 skill 的腳本,它會
把儲存格拆開再產生格式完全相同的 `parsed_bom.json`,下游步驟不用改:

```bash
python adapters/normalize_compound_spec_bom.py <bom_path> -o parsed_bom.json
```

詳見 `adapters/README.md`。

## 跑完之後回報什麼

用中文告訴使用者:總列數、濾掉幾列非採購項(如果有,順便說是哪些類別,
如 PCB)、剩下幾列待查價。如果有明顯異常(例如整批 `mpn` 都是空的),先
停下來跟使用者確認欄位對照,不要硬著頭皮繼續跑下一步(`bom-vendor-lookup`
會真的打外部 API,查詢額度不該浪費在欄位對錯的資料上)。
