# adapters/ — 處理「一格塞多屬性」的 BOM

## 你需要這個資料夾嗎?

**多數人不需要。** 先跑一次 `bom-parse`,看 `parsed_bom.json` 長什麼樣子再決定:

```
跑完 bom-parse,打開 parsed_bom.json
        │
        ├── mpn / manufacturer 都有抓到值
        │   → 什麼都不用做,直接往下跑 bom-vendor-lookup
        │
        ├── 欄位抓錯(例如廠商欄空的,但原始 BOM 明明有「Vendor」欄)
        │   → 這是欄位「名稱」對不上,不是這裡的問題。
        │     去 mouser_lookup.py 的 _COLUMN_ALIASES 加別名就好
        │
        └── mpn 整欄都是空的,而且原始 BOM 根本沒有獨立的型號欄——
            型號、廠牌、備註全塞在同一個「规格型号」儲存格裡
            → 你需要這個資料夾
```

長這樣的 BOM 就是第三種:

| 名称描述 | 规格型号 | 位号 | 组成用量 |
|---|---|---|---|
| 微控制器 | `STM32F030CCT6（品牌：ST）（备注：LQFP-48）` | U1 | 1 |
| 厚膜电阻 | `厚膜电阻 10KΩ ±1% 1/16W SMD 0402(公制1005)` | R1,R2 | 10 |

第一列的型號、廠牌、封裝被括號標籤塞在一格;第二列(被動元件)**根本沒有型號**,
只有一整段規格描述文字。

## 為什麼加別名解不了這件事

`_COLUMN_ALIASES` 的機制是**整欄一對一對應**——「原始檔案的『Vendor』欄
= 標準的 `manufacturer` 欄」。它從頭到尾不會去看儲存格裡面裝什麼。

所以你可以把「规格型号」這一欄對應到 `mpn`,結果就是 `mpn` 欄裝著
`STM32F030CCT6（品牌：ST）（备注：LQFP-48）` 這一整串——拿這個去 Mouser 查,
一筆都查不到。**這條路怎麼調別名都走不通**,不是你別名寫錯。

要解決得先把儲存格**拆開**,那是另一層的工作,所以獨立成這個 adapter。

## 怎麼用

用 `normalize_compound_spec_bom.py` **取代** `bom-parse` 那一步。它產出的
`parsed_bom.json` 跟正常走 `bom-parse` 的格式**一模一樣**,所以後面四步
(`bom-vendor-lookup` → `bom-match-score` → `bom-match-review` →
`bom-report-finalize`)完全不用改,也不需要知道這份 BOM 走的是哪條路徑。

在**專案根目錄**執行:

```bash
python adapters/normalize_compound_spec_bom.py <你的BOM.xlsx>
```

不加 `-o` 的話,會跟 `bom-parse` 一樣自動建立「今天日期_BOM檔名」資料夾
(例如 `20260814_bom_2024Q1_projectX/`),`parsed_bom.json` 存到裡面,下游
`bom-vendor-lookup` 等步驟會沿用同一個資料夾。需要指定固定路徑的話仍可以
加 `-o parsed_bom.json`。

> **注意**:這支腳本用 `openpyxl` 讀檔,**只吃 `.xlsx`**,不吃 csv。
> (`bom-parse` 是吃 csv 的,兩邊不一樣。)

想先看看效果,倉庫裡有一份示範檔:

```bash
python adapters/normalize_compound_spec_bom.py examples/sample_compound_spec_bom.xlsx
```

### 檢查拆出來的東西對不對

拆解用的是規則式 regex,**不是 100% 準確**,第一次處理某家供應商的 BOM 時
一定要抽查。加 `--debug-xlsx` 會另外輸出一份「拆解後」的 xlsx 給你用 Excel 開:

```bash
python adapters/normalize_compound_spec_bom.py <你的BOM.xlsx> --debug-xlsx check_me.xlsx
```

**這份 debug 檔只給人看,不要再餵回 pandas。** 原因寫在 `build_rows()` 的
docstring 裡:pandas 讀 Excel 時會把「整欄都是純數字字串」的欄位(例如封裝
代碼 `0402`)自動推斷成數字,讀回來變 `402`、前導 0 不見了。這也正是這支
腳本**不透過中間 xlsx**、直接在記憶體裡把資料交給 `parse_bom()` 的原因。

### 其他參數

| 參數 | 用途 |
|---|---|
| `--list-profiles` | 列出目前支援哪些供應商排版格式,不用給 BOM 檔案也能跑 |
| `--profile <id>` | 強制指定用哪個 profile。平常不用填,腳本會自己掃標題列判斷;只有自動偵測失敗、或你想除錯時才需要 |

## 已知拆不乾淨的情況(這是預期行為,不是壞掉)

- **只有偵測到「品牌」標籤時才會填 `mpn`/`manufacturer`**。這是刻意保守:
  沒有品牌標籤的一律當成被動元件的自由格式規格文字處理,不去猜型號。
  猜錯型號比留空更糟——留空還會走 keyword 搜尋,猜錯就是拿錯字串去查。
- **完全沒有品牌標籤、也沒有 EIA 封裝代碼(0402/0603…)可辨識的自由文字**
  (例如純連接器描述「8P8C 90度 无灯带屏蔽」),`value`/`package` 可能抓不到,
  `mpn` 會是空的。這類料件後續會用 keyword 搜尋碰運氣,查不到就落成
  `missing_mpn` / `no_candidates`。
- **型號帶特殊符號**(斜線、頓號)的料件建議特別抽查幾筆。

## 遇到新供應商的格式

腳本會掃描標題列,比對 `supplier_profiles.py` 裡登記的格式特徵。都對不上時
會直接告訴你它掃到了哪些欄位標題,以及兩條出路——加一個新 profile,或者
其實你的 BOM 是一般格式、根本不需要這個 adapter。

**這支腳本本身不寫死任何特定供應商的規則。** 封裝/公差/數值怎麼判斷、
「哪個 token 才是型號」這些是通用邏輯,所有 profile 共用;會因供應商而異的
只有「欄位標題叫什麼」「品牌/備註標籤長什麼樣」,那些全部在
[supplier_profiles.py](supplier_profiles.py) 裡。

新增一家供應商的流程,以及為什麼**要由人確認過才能加**,見
[supplier_profiles.py](supplier_profiles.py) 開頭的說明與根目錄的
[CONTRIBUTING.md](../CONTRIBUTING.md)。
