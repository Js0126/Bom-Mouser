---
name: bom-match-review
description: Step 4 of the BOM pricing pipeline — Claude personally reviews the ambiguous match_status rows (needs_human_review, no_candidates) from match_scores.json and classifies each as CONFIRM / REJECT / UNCERTAIN using general electronics-industry knowledge (manufacturer abbreviations, company mergers/acquisitions/brand transitions, part-numbering conventions, category plausibility) — no paid LLM API call, this is Claude reasoning live in the current session. Use this after bom-match-score has produced match_scores.json, whenever the user asks to review ambiguous BOM matches, resolve unclear vendor matches, or "幫我看那些不確定的料件"/"這些配對對不對". Never trigger this to call an external LLM API — the whole point is doing it for free within the session.
---

# BOM Match Review(節點 C — Claude 語意判斷)

`bom-match-score` 的規則式評分對「字串/token 層面對得上多少」有天生的
天花板——遇到廠商併購改名、縮寫、或需要背景知識判斷規格是否相容的案例,
規則怎麼調都調不準。這個 skill 的工作是**你(Claude)親自用背景知識逐筆
判斷**這些卡住的案例,不是接另一個更複雜的規則引擎,也**不是**呼叫付費
LLM API——這正是這個 skill 存在的意義:同樣的判斷力,但完全免費,因為
就是你在這個 session 裡當下的推理能力。

**這條規則沒有例外:絕對不要在這個流程裡呼叫 Anthropic Messages API 或
任何其他付費 LLM 服務。**見專案根目錄的 `CLAUDE.md`——這是專案明確定下來、
不能自己默默打破的規矩。

## 讀取待複核清單

`match_scores.json`(`bom-match-score` 產出)裡 `match_status` 是
`needs_human_review` 或 `no_candidates` 的項目就是這一步要看的。直接用
Read 工具讀 JSON,配合 `parsed_bom.json` 對照每筆的 BOM 端原始資訊
(manufacturer/description/value/package/tolerance)。

`no_candidates` 的項目如果 `candidates_considered` 是 0,代表 Mouser/
DigiKey 都真的查無此料——這種通常沒什麼好判斷的(廠商沒賣就是沒賣),
除非你懷疑查詢用的關鍵字本身有問題(例如型號被截斷、混進了不該送出去
的字元),否則不用花時間深究,直接算 UNCERTAIN 或略過即可,把力氣留給
`needs_human_review`(這些至少有候選料件可以比對)。

## 逐筆判斷方法

對每一筆 `needs_human_review` 的項目,參考
`references/manufacturer-knowledge.md`(常見廠商縮寫、併購沿革、標準
命名慣例),依序檢查:

1. **廠商是不是同一家**:BOM 端的廠商欄位可能是縮寫、法人全名的簡寫、
   或因為併購/品牌轉移導致跟 Mouser/DigiKey 給的廠商名稱不同(例如
   LTC 現在掛 Analog Devices 賣、NXP 的部分邏輯 IC 現在掛 Nexperia)。
   用你對電子業的背景知識判斷這是不是合理的沿革,不要只看字面像不像。

2. **型號是否吻合**:MPN 是否完全相符、或只是封裝/包裝代碼(reel vs
   cut tape)這種不影響電性的後綴差異。也可以用料號的命名慣例反推
   (例如 `AOZ` 開頭通常是 AOS 的穩壓器)輔助確認廠商身分。

3. **描述/規格是否合理**:Mouser/DigiKey 回傳的 Description 是否描述
   同一種元件類別。**這一步特別重要**——如果 BOM 期待的是某種功能的
   晶片,但供應商配對到的是完全不同類別的元件(例如 BOM 要的是網通
   PHY 晶片,配對結果卻是接線端子台),這是關鍵字搜尋抓錯的訊號,應該
   判 REJECT,不是照單全收。

4. **給不出把握就老實承認**:資訊不足以下判斷時(廠商欄位是通用/不指定
   標記、描述太籠統只寫元件大類、沒有具體規格可比對),標記 UNCERTAIN,
   不要為了湊出一個答案硬猜。UNCERTAIN 不是失敗,是誠實的結果——人工
   複核本來就該花在真正需要人判斷的地方。

## 輸出判決

寫一份 `review_verdicts.json`,格式:

```json
{
  "verdicts": [
    {
      "lookup_key": "<match_scores.json 裡的 key,通常就是 MPN>",
      "verdict": "CONFIRM" | "REJECT" | "UNCERTAIN",
      "reasoning": "一句話中文說明判斷依據"
    }
  ]
}
```

`lookup_key` 要跟 `match_scores.json`/`vendor_candidates.json` 裡的 key
完全一致(直接複製,不要自己重新組)。

同時用中文,依 CONFIRM / REJECT / UNCERTAIN 分組,整理一份表格給使用者
看(欄位:ref_des、mpn、BOM 廠商、供應商廠商、判決、理由)——這是給人看
的摘要,`review_verdicts.json` 才是給下一步 `bom-report-finalize` 讀的
機器格式,兩者都要產出。

## 提議更新 manufacturer_aliases.md

如果某筆 CONFIRM 的原因是「廠商縮寫/併購沿革導致名稱不一致」(不是描述
內容本身的判斷),這代表以後同樣的廠商還會再卡住——值得把這個對照加進
專案根目錄的 `manufacturer_aliases.md`,下次規則式評分就能自動認出來,
不用每次都靠這個 skill 重新判斷一次。

**寫具體要加的表格列,問使用者要不要加,不要自己動手改檔案**——這是
判斷性質的決定,使用者應該親自確認過再套用(這個專案一路以來都是這樣
處理廠商別名的)。同理,如果判斷出新的、有把握的廠商沿革知識,值得順便
問使用者要不要也補進 `references/manufacturer-knowledge.md`,讓這個
skill 下次判斷更快。

## 跑完之後回報什麼

給使用者看那份分組表格,並且明確講清楚:CONFIRM 幾筆、REJECT 幾筆、
UNCERTAIN 幾筆(需要使用者自己核實)。提醒使用者下一步是 `bom-report-finalize`
——把 `review_verdicts.json` 餵給它,判決就會反映進最終 xlsx(`matched_by_claude`
/`rejected_by_claude` 狀態),不需要重跑前面任何查詢步驟。
