---
name: bom-report-finalize
description: Step 5 (final step) of the BOM pricing pipeline — merge the earlier steps' JSON artifacts and (optionally) bom-match-review's verdicts into the final priced report the user actually opens: an xlsx (Summary sheet with totals/Top10, BOM_vs_Vendors detail sheet) plus a same-name HTML visual summary by default, both auto-organized into a dated per-run folder. Pure local merge, no API calls, cheap to re-run. Use this as the last step after bom-match-score (and optionally bom-match-review), or whenever the user asks for the finished quote — "產出報價表"/"給我最終的 Excel"/"regenerate the report" — including after re-scoring or adding new review verdicts, which never require re-running the vendor lookup.
---

# BOM Report Finalize(節點 D)

把前面幾步的 JSON artifact 合併,產出使用者真正要看的**最終 xlsx + html 報表**
(Summary 分頁:料件總數、match_status 統計、單價總價/含 MOQ 預算、Top 10
高價料件;BOM_vs_Vendors 分頁:逐筆明細)。純本機合併,不打任何外部 API,
跑起來很快,可以放心重跑。

報表欄位定義、Summary 分頁結構詳見專案根目錄 `ARCHITECTURE.md` 的
〈節點 D:xlsx 報表輸出〉。

## 執行

**不指定 `-o` 就好**,預設會寫進「輸入的 `parsed_bom.json` 所在的資料夾」
——這個 `{今天日期}_{來源 BOM 檔名}/` 資料夾是 pipeline 第一步
`bom-parse` 就已經建好的(例如 `20260814_bom_2024Q1_projectX/`),不是
這一步才建立,xlsx/html 直接沿用同一個資料夾、用資料夾名稱當檔名前綴,例如
`20260814_bom_2024Q1_projectX/20260814_bom_2024Q1_projectX_報價.xlsx`
(+ 同資料夾裡同檔名的 `.html`)——這樣一次 pipeline run 從 `parsed_bom.json`
到最終報表全部收在同一個資料夾,不同 BOM(=不同 session)各自落在不同
資料夾,不會共用檔名互相覆蓋。同一份來源 BOM 重跑會重用同一個資料夾直接
覆蓋,不會每重跑一次就多開一個。使用者有指定想要的檔名時才用 `-o`(用
`-o` 就完全不套用這個資料夾邏輯,尊重使用者自己選的路徑)。

**不含 Claude 判決**(只用規則式評分結果,`bom-match-review` 還沒跑或
使用者不需要):

在**專案根目錄**執行(三個輸入路徑都換成 run 資料夾裡的實際路徑):

```bash
python .claude/skills/bom-report-finalize/scripts/run.py <run資料夾>/parsed_bom.json <run資料夾>/vendor_candidates.json <run資料夾>/match_scores.json
```

**含 Claude 判決**(`bom-match-review` 已經產出 `review_verdicts.json`):

```bash
python .claude/skills/bom-report-finalize/scripts/run.py <run資料夾>/parsed_bom.json <run資料夾>/vendor_candidates.json <run資料夾>/match_scores.json --review <run資料夾>/review_verdicts.json
```

加了 `--review` 之後,判決會直接反映進 `match_status`:
- `CONFIRM` → 改成 `matched_by_claude`
- `REJECT` → 改成 `rejected_by_claude`(明確跟「還沒判斷過」的
  `needs_human_review` 分開,讓使用者一眼看出「Claude 認為這是錯的」)
- `UNCERTAIN` → `match_status` 不變,但 `claude_verdict`/`claude_reasoning`
  兩個欄位照樣會寫進報表,讓人工複核時看得到 Claude 已經想過、只是沒把握

## HTML 視覺化摘要(預設會一起出)

除了 xlsx,預設還會在**同一個路徑、同檔名**(副檔名改 `.html`)輸出一份
給人看的視覺化摘要網頁——單價總價/含 MOQ 預算的大數字、配對狀況分佈、
「有報價但被排除」的明細(BOM 記載 vs 供應商配到對照 + Claude 判斷理由)、
單價最貴 / 對總價貢獻最大的 Top 10 排行。

這份 HTML 是用 `mouser_lookup.render_html_report()` 直接吃 `build_report()`
的 `report` 跟 `compute_summary()` 的 `summary` 產生,**不重新算任何金額**,
數字保證跟 xlsx 一致。樣板檔案在
`.claude/skills/bom-report-finalize/assets/report_template.html`——要改
版面/文案就編輯這個檔案(用 `{{TOKEN}}` 佔位符,Python 端用純字串
`.replace()` 帶入,沒有引入 Jinja2 之類的樣板引擎依賴)。

不需要這份網頁的話加 `--no-html`(只出 xlsx)。網頁本身是純靜態 HTML +
少量內嵌 JS(渲染 Top10 清單、排除清單、表格切換),沒有外部資源,離線
也能開。

## 檔案輸出路徑衝突

腳本自己會處理:如果輸出檔名跟使用者在 Excel 裡開著的舊報表衝突
(`PermissionError`),會自動改用版本化檔名(`_v2`、`_v3`...)重試,不會
要求使用者先關掉 Excel。你不用手動處理這個情況,`run()` 回傳的 dict 裡
`_out_path` 就是實際成功寫入的路徑——回報給使用者的時候用這個值,不要
假設就是你原本要求的那個檔名。

## 什麼時候要重跑這一步

- 更新了 `manufacturer_aliases.md`,重跑了 `bom-match-score` 產生新的
  `match_scores.json` 之後——重跑這一步套用新分數,不需要碰
  `bom-vendor-lookup`。
- `bom-match-review` 產出了新的 `review_verdicts.json`——重跑這一步套用
  判決,一樣不需要碰前面任何查詢步驟。也就是說,**只有第一次執行
  `bom-vendor-lookup` 真的會打外部 API**,之後不管重算幾次分數、加幾筆
  判決,都是本機瞬間完成的操作。

## 跑完之後回報什麼

用中文整理 Summary 分頁的內容:料件總數、`match_status` 各狀態筆數
(含 `matched_by_claude`/`rejected_by_claude`,如果有的話特別提一下這是
Claude 語意判斷的結果)、各貨幣的單價總價與含 MOQ 預算。把 xlsx **跟**
html 檔案路徑都明確告訴使用者(預設兩份都會出,同檔名只差副檔名)——
xlsx 是拿去操作/篩選用的明細,html 是給人一眼看懂的摘要,兩者互補,不是
其中一份取代另一份。

**順便看一下「供應商報價查詢日期」那幾列。** 快取沒有時效,報表可能是拿
幾個月前查的價格產生的。如果查詢日期距今已久、或有「日期不詳(來自舊快取)」
的料件,主動提醒使用者:要下單或對外報價之前,先刪掉 `.mouser_cache.json`
重跑 `bom-vendor-lookup` 拿新報價。不要等使用者自己發現。
