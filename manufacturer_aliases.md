# 廠商縮寫對照表

給 [mouser_lookup.py](mouser_lookup.py) 的 `_normalize_manufacturer()` 用,在比對 BOM
廠商欄位跟 Mouser 回傳的 `Manufacturer` 時,先把左邊的縮寫展開成完整名稱關鍵字再比對。

**新增縮寫只要在下面表格加一行就好,不需要改 Python 程式碼**(存檔後下次執行
`mouser_lookup.py` 會自動重新讀取)。

- 「縮寫」欄:BOM 裡實際出現的廠商代碼(不分大小寫)
- 「完整名稱」欄:Mouser 上會出現的名稱關鍵字(不用完全等於 Mouser 的官方全名,
  只要是能唯一辨識、會出現在 Mouser Manufacturer 欄位裡的關鍵字即可)

| 縮寫 | 完整名稱 |
|---|---|
| TI | Texas Instruments |
| ADI | Analog Devices |
| HRS | Hirose |
| DIOO | DIOO Microcircuits |
| Analog | Analog Devices |
| MPS | Monolithic Power Systems |
| AOS | Alpha & Omega Semiconductor |
| Anaren | TTM Technologies |

## 不能加的(已評估過,會造成回歸)

- **`NXP` → `Nexperia`**:品牌轉移只發生在 NXP 的邏輯 IC / 分立元件產品線
  (2017 年分割出去的部分),NXP 其他產品線(如 `PCAL6408AHK` 這類 GPIO
  擴充晶片)仍然掛 NXP 本身賣。這份 BOM 裡就有正反兩種案例同時存在——
  `PCAL6408AHK` 靠子字串比對正確拿到 0.85 分,如果加了全域 `NXP→Nexperia`
  別名反而會把它也錯誤轉成 Nexperia 去比對,分數變差。`74AUP1G11GW`/
  `74AUP1G09GW` 這兩筆邏輯閘的 NXP→Nexperia 判斷改用 `bom-match-review`
  skill 逐筆處理(見 `review_verdicts.json`),不透過這個全域對照表。

## 待確認

- `UN`:目前在報表裡對到 6 家完全不同的真實廠商(MCC / Bourns / Taiwan
  Semiconductor / Rectron / onsemi ×2),看起來不像某個廠商的縮寫,比較像「廠商
  不指定 / 通用件」的標記。**先不要加進上面的表格**,除非確認它對應到某個
  具體廠商——不然會讓程式誤判成該廠商,反而降低比對品質。`bom-match-review`
  skill 已經知道要謹慎處理這種通用標記(見它的 references 資料)。
- `CJ`(出現在 2SK3018,Mouser 給 ROHM Semiconductor):不確定 CJ 是不是某廠商
  縮寫,還是同上面 UN 一樣是通用標記。`bom-match-review` 判斷這筆描述資訊太
  籠統(只寫「MOSFET」),標記為 UNCERTAIN,建議對照原廠 datasheet。
- `FURUNO` 對到 Mouser 的 **Kaga FEI**(出現在 GT-100):看起來不像縮寫關係,
  `bom-match-review` 判斷把握不夠,標記為 UNCERTAIN,建議人工核實這筆是否
  為正確替代品。
