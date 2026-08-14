---
name: bom-match-score
description: Step 3 of the BOM pricing pipeline — apply rule-based confidence scoring (manufacturer-name similarity, description token overlap, exact-MPN-match bonus) to the vendor candidates fetched in bom-vendor-lookup, classifying each part as a confident match or one needing human review. Pure local scoring, no LLM, no API calls — fast and free to re-run. Use this right after bom-vendor-lookup, or whenever the user says the match results look wrong, wants to re-score after teaching the tool a manufacturer abbreviation ("TI 跟 Texas Instruments 應該算同一家"/"配對分數怪怪的"/"re-score the matches"), or wants to try a different confidence threshold — none of which need re-querying the vendors.
---

# BOM Match Score(節點 C — 規則式評分)

對 `vendor_candidates.json` 裡每個查到候選的料件套用規則式評分,產出
`match_scores.json`。純本機、純腳本,**不叫任何 LLM,不打任何外部 API**。
跑起來幾乎瞬間完成,想重跑幾次都沒關係。

信心分數公式(廠商相似度 + 描述 token 命中率 + MPN 精確相符加成 + 身分
覆寫規則)的完整說明在專案根目錄 `ARCHITECTURE.md` 的
〈節點 C:match_validation 比對邏輯〉。

## 執行

在**專案根目錄**執行(兩個輸入路徑都換成 run 資料夾裡的實際路徑;不指定
`-o` 的話,`match_scores.json` 會自動存進同一個資料夾):

```bash
python .claude/skills/bom-match-score/scripts/run.py <run資料夾>/parsed_bom.json <run資料夾>/vendor_candidates.json
```

## 什麼時候要重跑這一步(不用碰節點 B)

- 使用者確認了某個廠商縮寫(例如「MPS 就是 Monolithic Power Systems」),
  你把它加進 `manufacturer_aliases.md` 之後——重跑這一步就能讓分數重新
  反映新的對照表,**不需要**重跑 `bom-vendor-lookup`(那份 JSON 裡的候選
  料件資料沒變,只是評分邏輯變了)。
- 想調整信心分數門檻(環境變數 `MATCH_CONFIDENCE_THRESHOLD`,預設 0.7)
  看結果會怎麼變化,也是重跑這一步就好。

## 跑完之後回報什麼

用中文告訴使用者 `match_status` 的分布(幾筆 matched、幾筆
needs_human_review、幾筆 no_candidates)。如果 `needs_human_review` 或
`no_candidates` 筆數不少,主動提一句可以用 `bom-match-review` skill 幫忙
逐筆看那些模糊案例——不需要使用者自己想到要接下一步。

## 這一步刻意不做的事

**不會**幫你判斷 `needs_human_review` 裡哪些其實是對的、哪些是錯的——
那是 `bom-match-review` skill 的工作(靠 Claude 自己的背景知識判斷,不是
更複雜的規則,也不是付費 API)。這一步的分數只反映「字串/token 層面對得
上多少」,對真正需要語意理解的案例(例如企業併購導致品牌名稱改變)天生
有極限,這是預期內的行為,不是 bug。
