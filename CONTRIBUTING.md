# 要改這個專案之前

這個專案有一個一以貫之的設計原則:

> **會因「你的 BOM 長怎樣」而異的東西,一律做成資料;只有真正通用的邏輯才寫進 Python。**

所以絕大多數你想做的調整,**都不需要動程式碼**——找到對應的資料檔加一行就好。
下面按「你想做什麼」分類。

## 我的 BOM 欄位名稱程式認不得

到 `mouser_lookup.py` 的 `_COLUMN_ALIASES` 字典,幫對應的標準欄位加一個別名。
例如你的 BOM 廠商欄叫「Vendor」,就把 `"vendor"` 加進 `"manufacturer"` 那個 list。

**不要改 `parse_bom()` 的邏輯本身。** 這個函式的設計就是靠別名表運作,
改邏輯只會讓下一個人看不懂為什麼有兩套機制。

改完重跑 `bom-parse`,看終端機印出的「命中 N 個標準欄位」有沒有變多。

## 廠商名稱對不起來(TI vs Texas Instruments)

到 [manufacturer_aliases.md](manufacturer_aliases.md) 的表格加一行。
**這是 markdown 表格,不是程式碼**,存檔後下次執行會自動重新讀取。

加完重跑 `bom-match-score` 就會套用新的對照,**不需要**重新查 Mouser/DigiKey。

⚠️ **不確定的縮寫不要自己猜著加。** 如果一個代碼在報表裡對到好幾家完全不相關
的廠商,那多半是「不指定廠商/通用件」的內部標記,不是某一家的縮寫。硬加進去
會讓程式把它誤判成那家廠商,比對品質反而更差。這種先寫進該檔案的「待確認」區。

## 我遇到新的「一格塞多屬性」供應商格式

到 [adapters/supplier_profiles.py](adapters/supplier_profiles.py) 加一個
`SupplierProfile`,**不用碰 `normalize_compound_spec_bom.py` 的解析邏輯**。

建議流程:

1. 把該供應商實際的**標題列**,加上幾筆**規格型號欄位的文字範例**,貼給 Claude
2. 讓它照現有 profile 的格式提議一個新的 `SupplierProfile`——包括先判斷這家
   到底需不需要拆解,還是其實欄位本來就是分開的、直接用 `bom-parse` 就夠
3. **你自己確認欄位候選跟 regex 沒問題,才貼進檔案**

第 3 步不能省。規則是不是夠通用、會不會誤傷其他供應商已經跑通的資料,
需要人看過——這跟 `manufacturer_aliases.md` 的處理原則是同一套。

## 我要改 mouser_lookup.py 的邏輯

**改完一定先跑自測**,它全程使用 mock 資料,不打真實 API、不需要憑證:

```bash
python test_pipeline.py
```

看到最後一行 `全部自測通過 ✅` 才算過。

跑真實查詢前,**先用 `--limit 5` 之類的小批量測試**,確認邏輯對再跑整份 BOM
——不然欄位或邏輯錯了,你會浪費掉整份 BOM 的查詢額度,而且要等很久才發現。

## 開發時會踩到的幾件事

- **中文輸出**:所有進入點腳本開頭都有一段強制 stdout 走 UTF-8 的 shim。
  新增腳本時記得照抄——少了它,Windows 主控台(cp950)遇到簡體字會直接
  拋 `UnicodeEncodeError` 讓整支腳本中斷,不是印成亂碼而已。
- **`.xlsx` 輸出遇到 `PermissionError`**:通常是使用者在 Excel 裡開著舊報表。
  `bom-report-finalize` 會自動換一個版本化檔名重試,不要改成要求使用者關檔案。
- **`_lookup_key()` 是跨步驟的共用契約**:節點 B/C/D 各自會重算一次同一個 key。
  改動 `_build_keyword()` 或 `_KEYWORD_CHAR_REPLACEMENTS` 會讓既有的
  `.mouser_cache.json` 裡所有 keyword 快取靜默失效,改之前先想清楚。
- **不要接付費 LLM API**。這是專案明確的規矩,理由見 [CLAUDE.md](CLAUDE.md)。
  語意判斷交給 `bom-match-review` skill,由 Claude 在對話 session 裡直接完成,
  完全免費。

## 提交之前

確認沒有把自己的資料帶進版控:

```bash
git status --ignored
```

`.env`、`.mouser_cache.json`、pipeline 中間產物的 `*.json`、輸出的報表 xlsx/html
都應該出現在 **Ignored files** 那一區。這些檔案含有你的憑證或真實 BOM 明細
(料號、廠商、單價、位號),不該進公開版控。

`examples/` 底下的示範檔是刻意排除在忽略規則之外的假資料,那些可以進版控。
