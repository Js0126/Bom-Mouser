---
name: bom-vendor-lookup
description: Step 2 of the BOM pricing pipeline — query Mouser (and DigiKey as a fallback when Mouser has no result) for every unique part in a parsed_bom.json, producing vendor_candidates.json. This is the ONLY step in the whole pipeline that calls external APIs. Use this right after bom-parse, or when the user wants to look up / re-fetch vendor pricing and stock for a BOM that's already been parsed — "去查一下 Mouser 有沒有這些料"/"重新抓一次報價"/"look these parts up on Mouser". Always suggest a small --limit test run first for a BOM whose column mapping hasn't been verified yet, to avoid burning query volume on misparsed data.
---

# BOM Vendor Lookup(節點 B)

對 `parsed_bom.json` 裡每個唯一 MPN(或缺 MPN 時湊出的關鍵字)查 Mouser,
查無結果時自動 fallback 查 DigiKey。**這是整條 pipeline 裡唯一會打外部
API 的一步**——Mouser/DigiKey 都是免費 API,但仍然是對外連線,有節流、
快取機制。

節流、重試、快取結構(`.mouser_cache.json`)、`query_failed` 跟
`no_candidates` 的差別,詳見專案根目錄 `ARCHITECTURE.md` 的
〈節點 B:Mouser + DigiKey 查詢〉。

## 執行前檢查

1. **確認專案根目錄的 `.env` 有 `MOUSER_SEARCH_API_KEY`**(必填)。
   `DIGIKEY_CLIENT_ID`/`DIGIKEY_CLIENT_SECRET` 選填,沒設定就只查 Mouser,
   不用特別提醒使用者。**不要把 Key 內容印出來**。
2. **這份 BOM 第一次跑,或欄位對照沒把握**:加 `--limit 5` 或 `--limit 10`
   先測小批量,確認 `vendor_candidates.json` 裡的候選料件看起來合理
   (廠商、描述至少沾得上邊),再拿掉 `--limit` 跑整份。這樣欄位對錯了
   也只浪費幾筆查詢額度。
3. **`mouser_part_number`/`lead_time`/`availability` 這三個報表欄位沒有拿
   真實帳號驗證過完整回應**(依公開 API 文件寫的欄位名稱)。小批量測試順便
   打開 `bom-report-finalize` 產出的報表看這三欄有沒有值——空白的話多半是
   Mouser 實際回傳的欄位名稱跟假設的不同,去 `mouser_lookup.py` 的
   `_mouser_part_number_lead_availability()` 調整。`mouser_part_number` 只有
   Mouser 來源的列才會有值,DigiKey 來源那欄本來就是空的,不是抓失敗。

## 執行

在**專案根目錄**執行(`parsed_bom.json` 換成 bom-parse 那一步實際建立的
run 資料夾路徑,例如 `20260814_bom_2024Q1_projectX/parsed_bom.json`;不
指定 `-o` 的話,`vendor_candidates.json` 會自動存進同一個資料夾):

```bash
python .claude/skills/bom-vendor-lookup/scripts/run.py <run資料夾>/parsed_bom.json
```

小批量測試:

```bash
python .claude/skills/bom-vendor-lookup/scripts/run.py <run資料夾>/parsed_bom.json --limit 10
```

## 重跑很便宜,但便宜的代價是資料不會更新

同一個 `parsed_bom.json` 重跑這個 skill,已經查過的 key 會直接吃本機快取
(`.mouser_cache.json`),**不會**重新打 API——所以：
- 如果部分項目 `query_failed`(網路問題,不是查無結果),直接重跑同一條
  指令,只有失敗的那幾筆會自動重試。
- 如果之後改了 `manufacturer_aliases.md` 想重新評分,不需要重跑這一步,
  直接跳去 `bom-match-score` 用同一份 `vendor_candidates.json` 就好。

### ⚠ 快取沒有時效,價格與 MOQ 都是快照

**快取裡沒有任何有效期限的機制**——一個 key 查過一次,之後永遠命中快取,
不管是一週前還是一年前查的。而快取存的是供應商回傳的完整候選資料,包含
**單價階梯、MOQ、訂購倍數**,所以這些數字全部凍結在第一次查詢的那一天。

這在畫面上看起來完全正常(會印「快取命中 N 筆」),很容易讓人以為拿到的
是最新報價。**要下單、要對外報價之前,一定要重新查。**

報表的 Summary 分頁有「供應商報價查詢日期」,明細分頁每一列也有
`data_fetched_at` 欄位——回報結果時如果日期距今已久,主動提醒使用者。

### 怎麼真的拿到新報價

加 `--no-cache` 的語意是「**這次不讀快取**」,**不是**「刷新快取」:

```bash
python .claude/skills/bom-vendor-lookup/scripts/run.py <run資料夾>/parsed_bom.json --no-cache
```

這樣會全部重查,但 `.mouser_cache.json` **原封不動留在原地**,下次沒加這個
參數又會吃到同一份舊資料。要徹底洗掉,直接刪檔再重跑:

```bash
rm .mouser_cache.json
```

(Windows `cmd` 用 `del .mouser_cache.json`。)刪掉之後這份 BOM 會整批重查,
會花掉相對應的查詢額度與時間,所以不要每次都刪——**該刪的時機**是:準備
真的下單、報價要給客戶、或距離上次查詢已經隔了一段時間(元件價格與 MOQ
變動頻繁,尤其缺料時期)。

## 跑完之後回報什麼

用中文告訴使用者:總共幾個唯一 key 有查詢結果、DigiKey fallback 補到幾筆
(如果有)、有沒有 `query_failed` 的筆數(有的話說明是暫時性失敗,重跑
同一條指令會自動只重試那幾筆)。

## 硬性規則

這步只打 Mouser/DigiKey(免費、使用者已授權的服務)。**絕對不要在這裡加
其他要額外付費的 API 呼叫**——這條規則見專案根目錄的 `CLAUDE.md`,沒有
例外,即使看起來能讓查詢更準也要先問過使用者。
