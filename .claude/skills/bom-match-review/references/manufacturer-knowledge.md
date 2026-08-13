# 電子零件廠商背景知識

給 `bom-match-review` skill 用的參考資料——常見的廠商縮寫、企業併購/品牌
沿革、標準料號命名慣例。這些是拿來**輔助判斷**用的先驗知識,不是絕對規則;
遇到清單以外的案例,一樣要靠合理推論,沒把握就標 UNCERTAIN,不要硬猜。

這份清單會持續累積——每次 `bom-match-review` 判斷出新的、有把握的廠商
對照,值得補進來(以及對應加進 `manufacturer_aliases.md`),下次就不用
重新推導一次。

## 常見廠商縮寫

| BOM 常見縮寫 | 完整名稱 |
|---|---|
| TI | Texas Instruments |
| ADI / Analog | Analog Devices Inc. |
| MPS | Monolithic Power Systems |
| AOS | Alpha & Omega Semiconductor |
| ON / onsemi | onsemi(原 ON Semiconductor,原 Motorola 半導體部門) |
| TE / TE Conn | TE Connectivity |
| ST / STM | STMicroelectronics |
| Vishay | Vishay Intertechnology |
| Diodes | Diodes Incorporated |
| Rohm | ROHM Semiconductor |
| Nexperia | Nexperia B.V. |
| Renesas | Renesas Electronics |
| Microchip | Microchip Technology |
| WCH | 沁恆微電子(WCH,Nanjing Qinheng Microelectronics) |
| YAGEO | 國巨(Yageo Corporation) |

## 併購/品牌沿革(容易造成 BOM 廠商跟 Mouser/DigiKey 廠商不一致)

- **Linear Technology(LTC)→ Analog Devices**:2017 年 ADI 併購 Linear
  Technology,所有 LTC 開頭的料號現在都掛 Analog Devices Inc. 銷售。
  BOM 上常見寫 "Linear"、"LTC"、或就寫 "Analog"。
- **NXP 的邏輯 IC 產品線 → Nexperia**:2017 年 NXP 把標準邏輯 IC、分立
  元件、小訊號產品線分割出去成立 Nexperia。很多舊 NXP 品牌的邏輯 IC
  (74 系列邏輯閘、電晶體等)現在改掛 Nexperia 販售,但 BOM 上可能還是
  寫 NXP(尤其是舊 BOM 或工程師習慣沿用舊稱呼)。**這個轉變只發生在邏輯
  IC/分立元件產品線**,NXP 其他產品(如微控制器、GPIO 擴充晶片、RF 晶片)
  還是掛 NXP 賣,不要一律當成 Nexperia。
- **Fairchild Semiconductor → onsemi**:2016 年 onsemi 併購 Fairchild,
  Fairchild 型號現在多半掛 onsemi。
- **International Rectifier(IR)→ Infineon**:2015 年併購,IR 開頭型號
  現在掛 Infineon。
- **Cypress Semiconductor → Infineon**:2020 年併購。
- **Maxim Integrated → Analog Devices**:2021 年併購,MAX 開頭型號現在
  也掛 Analog Devices。
- **Silicon Labs 部分產品線 → Skyworks**:Skyworks 併購了 Silicon Labs
  的部分產品線(如時脈晶片 Si5xxx 系列),這類型號現在可能掛 Skyworks
  Solutions Inc.,但 Silicon Labs 品牌本身仍然存在、獨立經營其他產品。

## 標準料號命名慣例(用來輔助判斷「這顆料是不是這家廠商做的」)

這些是業界通用、多家廠商互相相容的標準命名,**看到這些前綴不能直接認定
只有某一家廠商做**,但可以拿來確認 Mouser/DigiKey 給的候選跟 BOM 描述
是不是同一種元件:

- `BZT52` 系列:SOD-123 封裝穩壓二極體(Zener),Vishay 風格命名,
  Taiwan Semiconductor、Rectron、onsemi 等多家廠商都有生產相容型號。
- `MMBT39xx`(如 MMBT3904/MMBT3906):SOT-23 封裝通用型 BJT,業界最常見
  的標準電晶體之一,幾乎每家分立元件廠都做。
- `SMDJ` 系列:SMC/DO-214AB 封裝 TVS 二極體,Bourns、Littelfuse 等廠商
  都有生產。
- 廠商專屬前綴(比較能拿來確認廠商身分,不是通用命名):
  - `AOZ` = AOS 的降壓/升壓穩壓器
  - `MPM`/`MP` = MPS 的電源管理 IC
  - `LTC`/`LT` = Linear Technology(現屬 Analog Devices)
  - `74AUP1G`、`74AUP2G` 等 = Nexperia(原 NXP)的超小型邏輯閘系列

## 判斷不出來,不要硬猜的情況

- **通用/不指定廠商標記**:如果同一個 BOM 廠商代碼(例如某個簡稱)在
  不同料件上對應到好幾家完全不相關的真實廠商,這通常代表這個代碼是
  「不限廠商/通用件」的內部標記,不是某一家具體廠商的縮寫。這種情況下,
  重點應該放在「Mouser/DigiKey 給的候選是不是真的符合 BOM 的規格描述」,
  而不是硬去湊廠商名稱——廠商對不上不代表配對錯誤,可能本來就沒有指定。
- **描述資訊太少**(例如只寫「MOSFET」、「IC」這種籠統類別,沒有具體
  電性規格)時,沒辦法只靠型號格式確認是否為同一顆料,應該標記
  UNCERTAIN,建議人工對照原廠 datasheet。
