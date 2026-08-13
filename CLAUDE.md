# BOM-Mouser 專案指引

BOM → Mouser/DigiKey 比價 pipeline。快速上手見 [README.md](README.md);
節點怎麼運作、欄位定義、match_status 完整定義見
[ARCHITECTURE.md](ARCHITECTURE.md);廠商縮寫對照表在
[manufacturer_aliases.md](manufacturer_aliases.md)。開始工作前先讀這三份。

## BOM 解析不出 MPN 時的分流(重要)

`bom-parse` 跑出來 `mpn` 整欄是空的時候,**先分辨是哪一種問題再動手**:

- **欄位名稱對不上**(原始 BOM 有獨立型號欄,只是叫別的名字)→ 到
  `mouser_lookup.py` 的 `_COLUMN_ALIASES` 加別名。
- **一格塞多屬性**(型號/廠牌/備註全塞在同一個「规格型号」儲存格,被動元件
  完全沒有型號)→ **加別名永遠解不了**(別名是整欄一對一對應,不拆儲存格
  內容)。改用 `adapters/normalize_compound_spec_bom.py` 取代 `bom-parse`
  那一步,它產出的 `parsed_bom.json` 格式完全相同,下游不用改。詳見
  [adapters/README.md](adapters/README.md)。

不要在第二種情況下叫使用者去加別名——那是死路,會浪費他很多時間。

## 溝通方式

用中文回覆。

## 隱私與安全規則(MUST 遵守)

- Mouser/DigiKey API 憑證只能從環境變數讀取(`.env`,已設定好
  `MOUSER_SEARCH_API_KEY` / `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET`),
  程式碼與任何輸出中不得出現明文憑證。要檢查有沒有讀到,用 masked 方式印
  (長度+前後幾碼),不要印出完整值。
- 送往 Mouser/DigiKey 的 payload 只能是 MPN 或元件技術屬性(manufacturer/
  value/package/tolerance),不得附帶 ref_des、專案代號、客戶名稱。
- 不要把 `.env` 內容貼進對話、不要 commit 進版控(`.gitignore` 已排除)。

## 已決定不做的事

- **不接付費 LLM API。** 信心分數卡在門檻附近的模糊案例,理論上可以再送去
  Anthropic Messages API 之類的服務做語意判斷,但那是獨立於 Claude Code
  訂閱之外、按用量另外計費的 API,不是免費的——這條專案政策就是不引入這筆
  額外開銷。**這不代表沒有語意判斷**——`bom-match-review` skill(見下方)
  就是靠 Claude 在 session 裡親自用背景知識判斷,完全免費,只是不透過額外
  的付費 API 呼叫。不要主動提議把這個 skill 換成呼叫外部 LLM API;如果你
  認為某個案例真的需要更強的判斷力,先問使用者要不要接受額外費用,不要
  自己默默加上去。

## Skills

專案拆成五個 Claude Code skill,放在 `.claude/skills/`,對應四個節點
(節點 C 拆成規則式評分 `bom-match-score` + Claude 語意判斷
`bom-match-review` 兩步)。快速對照見 README.md「怎麼用」那節,節點對應的
完整說明見 ARCHITECTURE.md「四個節點 vs 五個 Skill」,細節見各自的
SKILL.md。只有 `bom-vendor-lookup` 會打外部 API(免費的 Mouser/
DigiKey);改分數/判決重跑 `bom-match-score`/`bom-report-finalize` 即可,
不用重新查詢。

## 開發慣例

- **改完 `mouser_lookup.py` 一定要先跑 `python test_pipeline.py`**(全程
  mock,不打真實 API)確認沒壞掉,再跑真實查詢。
- **跑真實查詢前先用 `--limit N` 小批量測試**(例如 5~10 筆),確認邏輯
  對再跑整份 BOM,避免浪費 API 額度或跑很久才發現錯誤。
- 廠商縮寫對照(如 `TI` → `Texas Instruments`)寫在 `manufacturer_aliases.md`
  的表格裡,不要寫死在 `.py` 裡。不確定的縮寫(例如對到多家不同廠商的代碼)
  記在該檔案「待確認」區,不要自己猜著加。
- Windows 上若 `.xlsx` 輸出時出現 `PermissionError`,通常是使用者在 Excel
  裡開著舊報表——換一個檔名(如加版本號)重新輸出,不要要求使用者關檔案
  才能繼續。
- 報表欄位/貨幣單位以 Mouser/DigiKey 回傳為準,常見是 TWD;`price_delta_pct`
  不會自動換算貨幣,結果需要跟 `mouser_currency`/`bom_unit_price` 的原始
  貨幣對照確認。
- `.mouser_cache.json` 是持久化查詢快取,結構為
  `{key: {"mouser": [...]|None, "digikey": [...]|None, "mouser_fetched_at":
  "YYYY-MM-DD"|None, "digikey_fetched_at": ...}}`。重跑 pipeline 會自動吃
  快取,只對真正沒查過(或上次查詢失敗)的部分發出新請求。**快取沒有時效**
  ——查過的價格/MOQ 不會自動過期,要下單前記得確認報表的「供應商報價查詢
  日期」夠新,不夠新就刪掉這個檔案重查。
