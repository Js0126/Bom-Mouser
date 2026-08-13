"""
bom_mouser_price_check — BOM → Mouser 比價 Pipeline

四個節點(依序執行):
  A. bom_parsing        — 讀取 BOM(xlsx/csv),標準化成統一欄位;濾掉 PCB/包材等
                           非採購項。純腳本邏輯。
  B. mouser_lookup      — 對每個唯一 MPN 呼叫 Mouser Search API(partnumber 搜尋);
                           沒有 MPN 的料件改用 keyword 搜尋(manufacturer + value +
                           package + tolerance 組成關鍵字)。純 API 呼叫。
  C. match_validation   — 比對 Mouser 候選結果與原始 BOM 是否為同一料件(廠商 +
                           描述 / Value / Package / Tolerance),產出信心分數;
                           低於門檻標記 needs_human_review。
  D. report_generation  — 本機把節點 A 保留的 ref_des 等欄位與節點 B/C 結果合併,
                           輸出 xlsx 報表。純腳本邏輯。

隱私緩解重點:
  1. API Key 只從環境變數 MOUSER_SEARCH_API_KEY 讀取,程式碼與任何輸出中不得出現明文 Key
  2. 送往 Mouser API 的 payload 只有 MPN,或(缺 MPN 時)manufacturer/value/package/
     tolerance 組成的關鍵字 —— 一律是元件本身的技術屬性,Ref Designator / 專案代號 /
     客戶名稱一律留在本機的 BOMLine,查詢完成後才在節點 D 於本機重新合併
  3. 節流(預設每次請求間隔 1 秒,可用環境變數 MOUSER_RATE_LIMIT_SECONDS 調整)+
     同一 MPN/關鍵字只查一次(記憶體去重 + 可選的本機持久化快取
     .mouser_cache.json),降低對外查詢次數與曝光面
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

try:
    # Windows 上 requests/certifi 內建的 CA bundle 有時缺少企業網路或防毒軟體
    # HTTPS 掃描所需的憑證鏈,導致 SSLCertVerificationError(即使系統(如 curl/
    # schannel)本身信任該憑證)。改用 truststore 讓 Python 走系統原生的憑證庫,
    # 驗證邏輯與作業系統一致。
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- 全域設定 ----------

MOUSER_API_URL = "https://api.mouser.com/api/v1/search/partnumber"
MOUSER_KEYWORD_API_URL = "https://api.mouser.com/api/v1/search/keyword"
# 預設 1 秒(= 最多 60 req/min)。Mouser 官方未公布硬限制,業界估算的保守
# 安全值約 30 req/min;1 秒節流已超出該估算值,換取查大量料件時不用等太久。
# 若開始收到 429 / 限流回應,調高 MOUSER_RATE_LIMIT_SECONDS 即可,不用改
# 程式碼。
RATE_LIMIT_SECONDS = float(os.environ.get("MOUSER_RATE_LIMIT_SECONDS", "1.0"))
MATCH_CONFIDENCE_THRESHOLD = float(os.environ.get("MATCH_CONFIDENCE_THRESHOLD", "0.7"))
DEFAULT_CACHE_PATH = Path(__file__).with_name(".mouser_cache.json")


def _get_api_key() -> str:
    """延遲讀取 API Key(呼叫時才檢查),避免 import 這個模組就強制要求環境變數。"""
    key = os.environ.get("MOUSER_SEARCH_API_KEY")
    if not key:
        raise RuntimeError(
            "未偵測到 MOUSER_SEARCH_API_KEY 環境變數。"
            "請複製 .env.example 為 .env 並填入 Key,"
            "再用 `python -m dotenv run -- python mouser_lookup.py <bom_file>` "
            "或先 export 至環境變數後執行。"
        )
    return key


# =====================================================================
# 節點 A: BOM 解析
# =====================================================================

@dataclass
class BOMLine:
    ref_des: str = ""                          # 只留在本機,絕不送 Mouser
    mpn: str = ""                               # 送 Mouser 的唯一識別欄位
    manufacturer: str = ""                      # 本機比對用,不主動送 API
    qty_per_unit: int = 1
    description: str = ""                       # 本機比對用(節點 C)
    value: str = ""                             # 被動元件:電阻/電容數值,如 "10K"
    package: str = ""                           # 被動元件:封裝,如 "0603"
    tolerance: str = ""                         # 被動元件:誤差,如 "±1%"
    bom_unit_price: Optional[float] = None      # 原始 BOM 單價(若有),用於計算差異
    category: str = ""                          # 品項類別(名稱/名称 欄位),用於濾掉 PCB/包材等非採購項


# 欄位別名對照表:key = 標準欄位,value = 可能出現的原始欄位名稱
# (比對時不分大小寫、忽略底線/連字號/多餘空白)。同時涵蓋繁體、簡體中文與英文,
# 因為實務上不同供應商 / EMS 廠給的 BOM 用字習慣不同(如 廠商/厂家、位號/位置)。
# 若你的實際 BOM 欄位命名不在清單中,直接在對應 list 補上別名即可,
# 不需要更動 parse_bom() 的邏輯。
_COLUMN_ALIASES: dict[str, list[str]] = {
    "ref_des": [
        "ref des", "refdes", "ref designator", "reference designator",
        "reference", "designator", "位號", "位置編號", "位置",
    ],
    "mpn": [
        "mpn", "part number", "partnumber", "manufacturer part number",
        "manufacturer part no", "mfr part number", "mfr part no", "mfr p/n",
        "mfg pn", "mfg part number", "mfg p/n",
        "料號", "廠商料號", "製造商料號", "型号", "型號",
    ],
    "manufacturer": [
        "manufacturer", "mfr", "mfg", "vendor", "brand", "mfg name",
        "廠商", "製造商", "厂家", "制造商",
    ],
    "qty_per_unit": [
        "qty", "quantity", "qty per unit", "qty/unit", "quantity per unit",
        "qty per assy", "數量", "数量",
    ],
    # description 允許同時比對多個欄位並串接(見 _MULTI_VALUE_FIELDS),
    # 因為像「名稱/名称」+「特性參數/特性参数」這種組合常常合起來才是完整描述。
    "description": [
        "description", "desc", "part description", "說明", "描述", "品名",
        "名稱", "名称", "特性參數", "特性参数", "參數", "参数",
    ],
    "value": ["value", "數值", "值"],
    "package": ["package", "package/case", "footprint", "pcb footprint", "封裝", "封装"],
    "tolerance": ["tolerance", "誤差", "公差"],
    "bom_unit_price": [
        "unit price", "price", "unit cost", "cost", "bom price",
        "bom unit price", "單價", "单价",
    ],
    # 只取「名稱/名称」類欄位本身(不含 description 也會併入的其他欄位),
    # 用來判斷這一列是不是 PCB / 包材等非採購項(見 filter_non_purchasable()）。
    "category": ["category", "item type", "type", "名稱", "名称", "類別", "类别"],
}

# description 允許比對到的所有別名欄位都保留下來、串接成一段文字(而不是只取第一個
# 命中的欄位),其餘標準欄位仍是「取第一個命中的欄位」。
_MULTI_VALUE_FIELDS = {"description"}


def _normalize_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(k).strip().lower())


def _build_column_map(columns) -> dict[str, object]:
    """
    回傳 {標準欄位: 原始欄位名稱},依 _COLUMN_ALIASES 猜測。
    _MULTI_VALUE_FIELDS 中的欄位(目前是 description)值為 list(所有命中的原始欄位),
    其餘欄位值為單一字串(第一個命中的原始欄位)。
    """
    normalized = {_normalize_key(c): c for c in columns}
    colmap: dict[str, object] = {}
    for std_field, aliases in _COLUMN_ALIASES.items():
        matched = [normalized[a] for a in aliases if a in normalized]
        if not matched:
            continue
        colmap[std_field] = matched if std_field in _MULTI_VALUE_FIELDS else matched[0]
    return colmap


def load_bom_file(path: str, header_row: Optional[int] = None, max_header_scan: int = 8) -> list[dict]:
    """
    讀取 xlsx/csv,回傳 list of dict(欄位為原始 BOM 檔案的欄位名稱)。

    很多實際的 BOM 檔案在真正的欄位標題列之前,還有一列標題/檔名/公司資訊
    (例如合併儲存格的檔名列),導致 pandas 預設把那一列當成欄位名稱會抓錯。
    若未指定 header_row,會自動掃描前 max_header_scan 列,選出「比對到最多
    _COLUMN_ALIASES 標準欄位」的那一列當作標題列。
    """
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        def _read(h):
            return pd.read_excel(p, header=h)
    elif p.suffix.lower() == ".csv":
        def _read(h):
            return pd.read_csv(p, header=h)
    else:
        raise ValueError(f"不支援的 BOM 檔案格式: {p.suffix}(僅支援 .xlsx / .xls / .csv)")

    if header_row is not None:
        df = _read(header_row)
    else:
        best_df, best_score, best_row = None, -1, 0
        for h in range(max_header_scan):
            try:
                candidate = _read(h)
            except Exception:
                continue
            score = len(_build_column_map(candidate.columns))
            if score > best_score:
                best_df, best_score, best_row = candidate, score, h
        if best_df is None or best_score <= 0:
            raise ValueError(
                f"無法在前 {max_header_scan} 列中找到看起來像欄位標題的那一列,"
                "請用 header_row 參數手動指定,或檢查 _COLUMN_ALIASES 是否需要補充別名。"
            )
        df = best_df
        print(f"[bom_parsing] 自動偵測到欄位標題列位於第 {best_row + 1} 列,"
              f"命中 {best_score} 個標準欄位")

    df = df.dropna(how="all")
    return df.to_dict(orient="records")


def parse_bom(rows: list[dict]) -> list[BOMLine]:
    """
    將原始 BOM(list of dict,欄位名稱依來源而定)轉為標準化 BOMLine。
    欄位對照使用 _COLUMN_ALIASES 自動猜測;若猜測結果不符合實際檔案,
    請把該檔案的欄位名稱補進 _COLUMN_ALIASES,不必更動這裡的邏輯。
    """
    if not rows:
        return []
    colmap = _build_column_map(rows[0].keys())

    def _get(row: dict, std_field: str, default=""):
        src = colmap.get(std_field)
        if src is None:
            return default
        if isinstance(src, list):
            vals = []
            for col in src:
                v = row.get(col)
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    continue
                v = str(v).strip()
                if v:
                    vals.append(v)
            return " ".join(vals) if vals else default
        v = row.get(src, default)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return v

    parsed = []
    for row in rows:
        qty_raw = _get(row, "qty_per_unit", 1)
        try:
            qty = int(float(qty_raw))
        except (ValueError, TypeError):
            qty = 1

        price_raw = _get(row, "bom_unit_price", None)
        try:
            price = float(price_raw) if price_raw not in (None, "") else None
        except (ValueError, TypeError):
            price = None

        parsed.append(BOMLine(
            ref_des=str(_get(row, "ref_des", "")).strip(),
            mpn=str(_get(row, "mpn", "")).strip(),
            manufacturer=str(_get(row, "manufacturer", "")).strip(),
            qty_per_unit=qty,
            description=str(_get(row, "description", "")).strip(),
            value=str(_get(row, "value", "")).strip(),
            package=str(_get(row, "package", "")).strip(),
            tolerance=str(_get(row, "tolerance", "")).strip(),
            bom_unit_price=price,
            category=str(_get(row, "category", "")).strip(),
        ))
    return parsed


# 常見「非電子料件 / 不會在 Mouser 查到價格」的類別關鍵字(依 BOMLine.category 判斷,
# 也就是原始 BOM 的「名稱/名称」欄位)。不分大小寫、完全比對(去除前後空白後)。
# 依你實際遇到的 BOM 自行增減即可。
_NON_PURCHASABLE_CATEGORIES = {
    "pcb", "pcba", "包材", "标签", "標籤", "说明书", "說明書", "手册", "手冊",
    "外箱", "彩盒", "保护膜", "保護膜", "胶袋", "膠袋", "标贴", "標貼",
}


def filter_non_purchasable(bom_lines: list[BOMLine]) -> tuple[list[BOMLine], list[BOMLine]]:
    """
    過濾掉 PCB 本身、包材、標籤等不會在 Mouser 查到價格的品項,避免浪費查詢額度。
    判斷依據:BOMLine.category(原始「名稱/名称」欄位文字)是否命中
    _NON_PURCHASABLE_CATEGORIES。
    回傳 (要送去查詢比價的, 被濾掉的) 兩個 list —— 被濾掉的品項不會遺失,
    節點 D 仍會把它們列進報表,標記 match_status = "excluded_non_purchasable"。
    """
    kept, excluded = [], []
    for line in bom_lines:
        if line.category.strip().lower() in _NON_PURCHASABLE_CATEGORIES:
            excluded.append(line)
        else:
            kept.append(line)
    return kept, excluded


# =====================================================================
# 節點 B: Mouser 查詢(資料最小化在此執行)
# =====================================================================

def _blank_cache_entry() -> dict:
    return {"mouser": None, "digikey": None,
            "mouser_fetched_at": None, "digikey_fetched_at": None}


def _load_cache(cache_path: Optional[Path]) -> dict:
    """
    快取結構:{key: {"mouser": list|None, "digikey": list|None,
                    "mouser_fetched_at": "YYYY-MM-DD"|None, "digikey_fetched_at": ...}}。
    None 代表「這個供應商還沒查過」,[] 代表「查過,查無結果」,非空 list 代表「查過,有結果」。
    這樣加入 DigiKey 之前存的舊快取(cache[key] = 候選 list,只代表 Mouser 結果)
    可以自動轉成新格式,不會遺失、也不用手動清快取重查。

    `*_fetched_at` 記錄「這批候選是哪一天查回來的」。**快取本身沒有時效**——
    查過一次就永遠命中,不會自動重查——所以價格/MOQ/訂購倍數都是查詢當下的
    快照。把日期存下來,報表才能誠實告訴使用者手上的報價有多舊。加入這個
    欄位之前存的快取沒有日期,一律當「不詳」,**不要拿今天的日期充數**:
    那會讓半年前的報價看起來像剛查的,比沒有日期更危險。
    """
    if cache_path is None or not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    migrated = {}
    for k, v in raw.items():
        if isinstance(v, list):
            entry = _blank_cache_entry()
            entry["mouser"] = v
            migrated[k] = entry
        elif isinstance(v, dict):
            migrated[k] = {
                "mouser": v.get("mouser"), "digikey": v.get("digikey"),
                "mouser_fetched_at": v.get("mouser_fetched_at"),
                "digikey_fetched_at": v.get("digikey_fetched_at"),
            }
    return migrated


def _save_cache(cache_path: Optional[Path], cache: dict) -> None:
    if cache_path is None:
        return
    try:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


MAX_RETRIES = int(os.environ.get("MOUSER_MAX_RETRIES", "3"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("MOUSER_REQUEST_TIMEOUT_SECONDS", "20"))
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class VendorQueryError(Exception):
    """
    代表這筆查詢重試後仍然失敗(網路問題 / 429 限流 / 5xx),不是供應商(Mouser
    或 DigiKey)明確回應「查無此料件」。跟這兩者刻意分開,是為了不要把暫時性
    失敗誤判成 no_candidates、也不要把失敗結果寫進快取(寫進快取的話下次重跑
    就永遠不會重試了)。Mouser 跟 DigiKey 的查詢函式共用這個例外類別。
    """


def _post_with_retry(url: str, params: dict, payload: dict, headers: dict) -> Optional[requests.Response]:
    """
    對暫時性網路錯誤(逾時 / 連線錯誤 / 429 限流 / 5xx)做重試,指數退避(2s, 4s, 8s...)。
    重試次數用完仍失敗就回傳 None,呼叫端(query_mouser / query_mouser_keyword)
    要把這種情況當成「查詢失敗」拋出 VendorQueryError,而不是當成「查無結果」。
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, params=params, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code in _RETRYABLE_STATUS and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"  [warn] 收到 HTTP {resp.status_code}(第 {attempt} 次),{wait}s 後重試")
                time.sleep(wait)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"  [warn] 請求失敗(第 {attempt} 次): {e.__class__.__name__},{wait}s 後重試")
                time.sleep(wait)
    print(f"  [warn] 重試 {MAX_RETRIES} 次仍失敗: {last_err.__class__.__name__ if last_err else '未知錯誤'}")
    return None


def query_mouser(mpn: str, api_key: str, max_candidates: int = 5) -> list[dict]:
    """
    只送出 MPN,不附帶 ref_des / manufacturer / 專案資訊。
    回傳 Mouser 回應中的候選料件列表(最多 max_candidates 筆,可能是空 list,
    代表 Mouser 明確回應「查無此料件」),節點 C 再對這些候選做信心評分與匹配驗證。

    若重試後仍拿不到有效回應(網路問題 / 429 / 5xx),拋出 VendorQueryError ——
    這跟「Mouser 回應查無此料件」是不同狀態,呼叫端(lookup_all)不會快取這種失敗。
    """
    payload = {
        "SearchByPartRequest": {
            "mouserPartNumber": mpn,
            "partSearchOptions": "Exact",
        }
    }
    headers = {"Content-Type": "application/json"}
    params = {"apiKey": api_key}

    resp = _post_with_retry(MOUSER_API_URL, params, payload, headers)
    time.sleep(RATE_LIMIT_SECONDS)  # 節流

    if resp is None:
        raise VendorQueryError(f"查詢 MPN={mpn!r} 時網路請求持續失敗")
    if resp.status_code != 200:
        raise VendorQueryError(f"查詢 MPN={mpn!r} 時 Mouser 回應 HTTP {resp.status_code}")
    data = resp.json()
    parts = data.get("SearchResults", {}).get("Parts", []) or []
    return parts[:max_candidates]


#  Mouser 的 keyword 搜尋端點(/api/v1/search/keyword)只接受 ASCII 字元,
#  出現 ±、Ω、µ/μ 這類符號會讓整個請求被判 InvalidCharacters、回應 HTTP 400
#  (不是「查無此料件」,是請求本身被拒)。被動元件從中文 BOM 規格文字拆出來的
#  value/tolerance 常常帶這些符號(例如 "0Ω"、"±5%"),所以組 keyword 前
#  一定要先淨化,不然 Mouser 100% 拒絕。這些符號優先轉成有意義的 ASCII
#  替代(µ/μ -> u),其餘查不到對應、Mouser 一樣不接受的非 ASCII 字元
#  (含中文廠商名稱)則直接捨棄,讓查詢至少送得出去,而不是整筆失敗。
_KEYWORD_CHAR_REPLACEMENTS = {
    "µ": "u", "μ": "u",
    # 注意:Ω 不能直接砍成空字串——電阻類 value(如 "10Ω")靠這個符號才知道
    # 單位是歐姆,砍掉後 "10" 這種純數字對 Mouser 來說意義模糊,常常配到
    # 完全不相干類別的料件(電感、電容...)。改成 "ohm"(Mouser 接受、也
    # 驗證過能正確配對,例如 "10ohm 0402 5%" 能精準找到 10Ω 電阻)。
    "Ω": "ohm", "±": "",
}


def _sanitize_keyword_text(s: str) -> str:
    for old, new in _KEYWORD_CHAR_REPLACEMENTS.items():
        s = s.replace(old, new)
    s = s.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip()


def _build_keyword(line: BOMLine) -> str:
    """
    給沒有明確 MPN 的料件(常見於被動元件/接插件)組關鍵字。
    只使用元件本身的技術屬性(manufacturer + value + package + tolerance),
    絕不放 ref_des / 專案代號 / 客戶名稱。組好後會經過 _sanitize_keyword_text()
    淨化,確保不含 Mouser keyword 端點會拒絕的非 ASCII 符號。
    """
    parts = [line.manufacturer, line.value, line.package, line.tolerance]
    raw = " ".join(p for p in parts if p).strip()
    return _sanitize_keyword_text(raw)


def query_mouser_keyword(keyword: str, api_key: str, max_candidates: int = 5) -> list[dict]:
    """
    關鍵字搜尋(Mouser keyword search endpoint)。用於缺少明確 MPN 的料件,
    keyword 只能是元件技術屬性組成的字串(見 _build_keyword()),不得包含
    ref_des / 專案代號 / 客戶名稱。失敗時的行為同 query_mouser(拋出 VendorQueryError)。
    """
    if not keyword:
        return []
    payload = {
        "SearchByKeywordRequest": {
            "keyword": keyword,
            "records": max_candidates,
            "startingRecord": 0,
            "searchOptions": "None",
            "searchWithYourSignUpLanguage": "",
        }
    }
    headers = {"Content-Type": "application/json"}
    params = {"apiKey": api_key}

    resp = _post_with_retry(MOUSER_KEYWORD_API_URL, params, payload, headers)
    time.sleep(RATE_LIMIT_SECONDS)  # 節流

    if resp is None:
        raise VendorQueryError(f"關鍵字查詢 {keyword!r} 時網路請求持續失敗")
    if resp.status_code != 200:
        raise VendorQueryError(f"關鍵字查詢 {keyword!r} 時 Mouser 回應 HTTP {resp.status_code}")
    data = resp.json()
    parts = data.get("SearchResults", {}).get("Parts", []) or []
    return parts[:max_candidates]


# ---------- DigiKey(第二資料源:Mouser 查無結果時的 fallback) ----------
#
# 只有同時設定 DIGIKEY_CLIENT_ID + DIGIKEY_CLIENT_SECRET 才會啟用,兩者缺一
# 視同「沒接 DigiKey」,完全不影響原本 Mouser-only 的行為。
# 用 Product Information V4 的 keyword search(2-legged OAuth / Client
# Credentials),回傳的候選料件會被轉成跟 Mouser 完全相同的欄位形狀
# (Manufacturer / ManufacturerPartNumber / Description / PriceBreaks / Min /
# Mult),這樣節點 C(match_validation)、節點 D(report/MOQ 計算)完全不用管
# 資料到底來自哪個供應商,只在 report 裡多一個 price_source 欄位標示來源。

DIGIKEY_CLIENT_ID = os.environ.get("DIGIKEY_CLIENT_ID")
DIGIKEY_CLIENT_SECRET = os.environ.get("DIGIKEY_CLIENT_SECRET")
DIGIKEY_ENABLED = bool(DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET)

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_KEYWORD_URL = "https://api.digikey.com/products/v4/search/keyword"
DIGIKEY_LOCALE_SITE = os.environ.get("DIGIKEY_LOCALE_SITE", "TW")
DIGIKEY_LOCALE_LANGUAGE = os.environ.get("DIGIKEY_LOCALE_LANGUAGE", "en")
DIGIKEY_LOCALE_CURRENCY = os.environ.get("DIGIKEY_LOCALE_CURRENCY", "TWD")

_digikey_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_digikey_token() -> str:
    """
    取得 DigiKey OAuth2 access token(2-legged / Client Credentials),記憶體內
    快取到快過期為止。實測 expires_in 約 599 秒(~10 分鐘),提前 30 秒視為
    過期以留緩衝,避免用到剛好卡在邊界過期的 token。
    """
    now = time.time()
    if _digikey_token_cache["token"] and now < _digikey_token_cache["expires_at"]:
        return _digikey_token_cache["token"]

    resp = requests.post(
        DIGIKEY_TOKEN_URL,
        data={
            "client_id": DIGIKEY_CLIENT_ID,
            "client_secret": DIGIKEY_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise VendorQueryError(f"DigiKey token 請求失敗: HTTP {resp.status_code}")
    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 540)
    _digikey_token_cache["token"] = token
    _digikey_token_cache["expires_at"] = now + max(expires_in - 30, 30)
    return token


def _normalize_digikey_part(product: dict, currency: str) -> dict:
    """
    把 DigiKey Product Information V4 的 Product 物件轉成跟 Mouser 候選料件
    相同的欄位形狀。一個 Product 可能有多個 ProductVariations(不同包裝方式,
    如 Cut Tape / Reel / Digi-Reel,各自有不同 MinimumOrderQuantity 跟價格階梯),
    選 MinimumOrderQuantity 最小的那個 variation 當代表(對採購來說最有彈性、
    門檻最低的買法)。

    "LeadTime"/"Availability" 兩個是刻意取的**廠商中立**鍵名,不像
    "ManufacturerPartNumber"/"Min"/"Mult" 沿用 Mouser 的欄位命名——後者是
    歷史包袱(DigiKey 支援是後來加的,當時圖方便直接借用 Mouser 的詞彙,
    導致報表裡 mouser_manufacturer 等欄位其實可能裝的是 DigiKey 資料,只有
    price_source 能分辨),這兩個新欄位不重蹈覆轍。
      - Availability:現貨數量,DigiKey v4 給的是整數(QuantityAvailable)。
      - LeadTime:DigiKey v4 沒有像 Mouser 那樣的現成「幾週交期」欄位,這裡
        用不到就留空字串,不硬湊。

    **這裡刻意不帶 DigiKeyProductNumber。** 報表的 `mouser_part_number` 欄位
    專指「Mouser 自己網站的內部料號」,DigiKey 的內部料號是完全不同的號碼、
    在不同網站查——硬塞進同一欄會誤導使用者拿一個 DigiKey 料號去 Mouser
    網站搜尋。所以資料來源是 DigiKey 時,`mouser_part_number` 就是留空,
    見 `_mouser_part_number_lead_availability()`。
    """
    variations = product.get("ProductVariations") or []
    best_variation = None
    if variations:
        best_variation = min(
            variations,
            key=lambda v: v.get("MinimumOrderQuantity") if v.get("MinimumOrderQuantity") else 10 ** 9,
        )

    price_breaks = []
    moq, mult = 1, 1
    if best_variation:
        moq = best_variation.get("MinimumOrderQuantity") or 1
        mult = best_variation.get("StandardPackage") or 1
        for pb in best_variation.get("StandardPricing") or []:
            qty, price = pb.get("BreakQuantity"), pb.get("UnitPrice")
            if qty is not None and price is not None:
                price_breaks.append({"Quantity": qty, "Price": price, "Currency": currency})

    qty_available = product.get("QuantityAvailable")
    availability = f"{qty_available} In Stock" if qty_available is not None else ""

    desc = product.get("Description") or {}
    return {
        "Manufacturer": (product.get("Manufacturer") or {}).get("Name", ""),
        "ManufacturerPartNumber": product.get("ManufacturerProductNumber", ""),
        "Description": desc.get("ProductDescription") or desc.get("DetailedDescription") or "",
        "PriceBreaks": price_breaks,
        "Min": moq,
        "Mult": mult,
        "LeadTime": "",
        "Availability": availability,
        "_source": "DigiKey",
    }


def query_digikey_keyword(keyword: str, max_candidates: int = 5) -> list[dict]:
    """
    DigiKey keyword 搜尋,只送出 keyword(MPN 或元件技術屬性組成的字串,規則
    跟 query_mouser_keyword 一樣,不帶 ref_des / 專案脈絡)。回傳跟 Mouser
    候選料件相同形狀的 list(見 _normalize_digikey_part)。
    DIGIKEY_ENABLED 為 False(沒設定 Client ID/Secret)時直接回傳 []。
    """
    if not DIGIKEY_ENABLED or not keyword:
        return []

    token = _get_digikey_token()
    headers = {
        "X-DIGIKEY-Client-Id": DIGIKEY_CLIENT_ID,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-DIGIKEY-Locale-Site": DIGIKEY_LOCALE_SITE,
        "X-DIGIKEY-Locale-Language": DIGIKEY_LOCALE_LANGUAGE,
        "X-DIGIKEY-Locale-Currency": DIGIKEY_LOCALE_CURRENCY,
    }
    body = {"Keywords": keyword, "Limit": max_candidates}

    resp = _post_with_retry(DIGIKEY_KEYWORD_URL, {}, body, headers)
    time.sleep(RATE_LIMIT_SECONDS)  # 節流

    if resp is None:
        raise VendorQueryError(f"DigiKey 關鍵字查詢 {keyword!r} 時網路請求持續失敗")
    if resp.status_code != 200:
        raise VendorQueryError(f"DigiKey 關鍵字查詢 {keyword!r} 時回應 HTTP {resp.status_code}")

    data = resp.json()
    products = data.get("ExactMatches") or data.get("Products") or []
    currency = (data.get("SearchLocaleUsed") or {}).get("Currency", DIGIKEY_LOCALE_CURRENCY)
    return [_normalize_digikey_part(p, currency) for p in products[:max_candidates]]


def _lookup_key(line: BOMLine) -> Optional[str]:
    """
    決定這筆料件在 vendor_results / matches 字典中的 key。
    有 MPN 就用 MPN(走 partnumber 搜尋);沒有 MPN 但湊得出關鍵字,
    就用一個帶前綴的合成 key(走 keyword 搜尋);兩者都沒有則回傳 None
    (無法查詢,節點 D 會標記為 missing_mpn)。
    """
    if line.mpn:
        return line.mpn
    kw = _build_keyword(line)
    return f"__keyword__:{kw}" if kw else None


def lookup_all(
    bom_lines: list[BOMLine],
    api_key: Optional[str] = None,
    cache_path: Optional[Path] = DEFAULT_CACHE_PATH,
    use_cache: bool = True,
    enable_keyword_fallback: bool = True,
    enable_digikey_fallback: bool = True,
    save_every: int = 10,
) -> dict[str, list[dict]]:
    """
    以 _lookup_key(line) 為 key 建立查詢結果字典(值為候選料件 list)。
    - 有 MPN 的料件先用 Mouser partnumber 搜尋;沒有 MPN 的料件(若
      enable_keyword_fallback)改用 Mouser keyword 搜尋。
    - Mouser 查無結果、且 enable_digikey_fallback 為真(且 DIGIKEY_ENABLED,
      即有設定 Client ID/Secret)時,改用同一個查詢字串(MPN 或關鍵字)查
      DigiKey。DigiKey 找到的候選會轉成跟 Mouser 相同欄位形狀(見
      _normalize_digikey_part),節點 C/D 不用管來源是誰。
    - 快取以「每個 key 分別記錄 Mouser / DigiKey 各自的查詢狀態」儲存(見
      _load_cache),同一個 key 的 Mouser 結果查過一次就不會再查,DigiKey
      也是;只有「Mouser 查過是空、但這次才第一次啟用 DigiKey」的情況會
      補查 DigiKey,其餘一律吃快取。
    - 每查到 save_every 筆新結果就存一次快取,長批次查詢中途斷線不會把已經
      查到的結果弄丟。
    - 查詢失敗(VendorQueryError,重試後仍拿不到有效回應)**不會**被寫進
      快取——跟「供應商明確回應查無此料件」(空 list,會被快取)刻意分開,
      讓重跑指令可以自動只重試真正失敗的那幾筆。
    """
    api_key = api_key or _get_api_key()
    cache = _load_cache(cache_path) if use_cache else {}
    today = date.today().isoformat()
    results: dict[str, list[dict]] = {}
    new_since_save = 0
    failed_keys: list[str] = []
    digikey_used = 0

    to_query = []
    for line in bom_lines:
        key = _lookup_key(line)
        if key is None or key in results:
            continue
        to_query.append((key, line))

    total_lines = len(to_query)
    queried_count = 0

    for i, (key, line) in enumerate(to_query, start=1):
        entry = cache.get(key, _blank_cache_entry()) if use_cache else _blank_cache_entry()
        entry_dirty = False

        # ---- Mouser ----
        if entry["mouser"] is not None:
            mouser_parts = entry["mouser"]
        else:
            try:
                if line.mpn:
                    mouser_parts = query_mouser(line.mpn, api_key)
                elif enable_keyword_fallback:
                    mouser_parts = query_mouser_keyword(_build_keyword(line), api_key)
                else:
                    mouser_parts = None
            except VendorQueryError as e:
                print(f"  [warn] {key!r} Mouser 查詢失敗(非「查無此料件」),本次不快取,重跑指令會自動重試: {e}")
                failed_keys.append(key)
                continue
            except Exception as e:
                print(f"  [warn] 查詢 {key!r} 時發生非預期例外,本次不快取: {e.__class__.__name__}: {e}")
                failed_keys.append(key)
                continue
            if mouser_parts is None:
                continue  # 沒有 MPN 也沒開 keyword fallback,無法查詢
            entry["mouser"] = mouser_parts
            entry["mouser_fetched_at"] = today
            entry_dirty = True

        # ---- DigiKey(只在 Mouser 查無結果時當 fallback) ----
        digikey_parts = entry.get("digikey")
        if not mouser_parts and enable_digikey_fallback and DIGIKEY_ENABLED:
            if digikey_parts is None:
                dk_keyword = line.mpn or _build_keyword(line)
                try:
                    digikey_parts = query_digikey_keyword(dk_keyword)
                    entry["digikey"] = digikey_parts
                    entry["digikey_fetched_at"] = today
                    entry_dirty = True
                    if digikey_parts:
                        digikey_used += 1
                        print(f"  [mouser_lookup] {key!r} Mouser 查無結果,DigiKey 找到 {len(digikey_parts)} 筆候選")
                except VendorQueryError as e:
                    print(f"  [warn] {key!r} DigiKey fallback 查詢失敗(不影響 Mouser 結果): {e}")
                except Exception as e:
                    print(f"  [warn] {key!r} DigiKey fallback 發生非預期例外: {e.__class__.__name__}: {e}")

        final_parts = mouser_parts or digikey_parts or []
        # 把「這批候選是哪一天查的」蓋在候選 dict 上,讓它跟著 vendor_candidates.json
        # 一路流到報表(做法跟 _normalize_digikey_part 的 "_source" 標記一致)。
        # 快取沒有時效,不標日期的話,使用者打開報表完全看不出手上這份報價是
        # 今天查的還是半年前查的——而價格/MOQ 都是會變的。
        if mouser_parts:
            fetched_at = entry.get("mouser_fetched_at")
        elif digikey_parts:
            fetched_at = entry.get("digikey_fetched_at")
        else:
            fetched_at = entry.get("mouser_fetched_at")
        for part in final_parts:
            part["_fetched_at"] = fetched_at
        results[key] = final_parts
        if entry_dirty:
            queried_count += 1

        if use_cache:
            cache[key] = entry
            if entry_dirty:
                new_since_save += 1
                if new_since_save >= save_every:
                    _save_cache(cache_path, cache)
                    new_since_save = 0

        if total_lines >= 20 and i % 20 == 0:
            print(f"  [mouser_lookup] 進度 {i}/{total_lines}")

    if total_lines:
        print(f"[mouser_lookup] 共處理 {total_lines} 筆,快取命中 {total_lines - queried_count} 筆、"
              f"實際發出新查詢 {queried_count} 筆;DigiKey fallback 找到結果 {digikey_used} 筆")

    if use_cache and new_since_save > 0:
        _save_cache(cache_path, cache)
    if failed_keys:
        print(f"[mouser_lookup] {len(failed_keys)} 筆因網路問題查詢失敗(非「查無此料件」),"
              f"報表中會標記為 query_failed;重新執行同一條指令即可自動只重試這些筆")
    return results


# =====================================================================
# 節點 C: 比對驗證(match_validation)
# =====================================================================
#
# 信心分數 = 0.5 * 廠商相似度 + 0.5 * 描述相似度 + MPN 精確相符加成(封頂 1.0)
#
# - 廠商相似度:標準化後(去除大小寫、Inc/Corp/Ltd 等常見字尾)做完全比對 /
#   包含比對 / 字串相似度(difflib)。
# - 描述相似度:BOM 端把 description + value + package + tolerance 組成一段
#   文字,與 Mouser 回傳的 Description 做字串相似度比對 —— 這樣被動元件
#   (電阻/電容常無明確 MPN,只有 Value/Package/Tolerance)也能被合理評分。
# - 兩者任一邊資訊不足(空字串)時給中性分數 0.5,避免因缺欄位就被判定為
#   完全不相符或完全相符。
# - 信心分數低於門檻(預設 0.7,可用環境變數 MATCH_CONFIDENCE_THRESHOLD 調整)
#   標記為 needs_human_review。
#
# 這是規則式(rule-based)的近似判斷,不呼叫任何 LLM API。若之後要接真正的
# LLM 語意判斷,建議的介接點是 match_validation() —— 保留 bom_line 與
# candidates 兩個純本機/純 Mouser 回傳資料的參數,不需額外傳遞其他內部脈絡。

def _normalize_text(s: str) -> str:
    # 只留英數字,中文(或其他非 ASCII 字元)會被整個濾掉——這是刻意的,不是
    # bug。BOM 端組出的技術屬性(1uF/10V/X5R/0402 這類規格 token)本來就是
    # 英數字,濾掉中文之後兩邊比對的雜訊反而變少;純中文的描述會變成空字串,
    # 這時 _description_score() 會退回中性的 0.5 分,不會誤判成負分。
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


_MFR_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|ltd|llc|gmbh|sa|ag|kg|company|"
    r"technologies|technology|electronics|electronic|semiconductor|semi)\b\.?"
)

MANUFACTURER_ALIASES_PATH = Path(__file__).with_name("manufacturer_aliases.md")


def _load_manufacturer_aliases(path: Path = MANUFACTURER_ALIASES_PATH) -> dict[str, str]:
    """
    解析 manufacturer_aliases.md 的 markdown 表格,回傳 {normalize後的縮寫: normalize後的完整名稱}。
    這個檔案是給人編輯的參考資料,不是程式碼——新增/修改廠商縮寫不需要碰這支 .py。
    檔案不存在或格式跑掉,就當作沒有任何縮寫對照(退回原本只靠字尾去除+相似度的比對)。
    """
    aliases: dict[str, str] = {}
    if not path.exists():
        return aliases
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        if len(cells) != 2:
            continue
        abbr, full = cells
        if not abbr or not full:
            continue
        if re.fullmatch(r"[-:]+", abbr) or re.fullmatch(r"[-:]+", full):
            continue  # markdown 表格的分隔列,如 |---|---|
        if _normalize_text(abbr) in ("", "縮寫") or _normalize_text(full) in ("", "完整名稱"):
            continue  # 表格標題列
        aliases[_normalize_text(abbr)] = _normalize_text(full)
    return aliases


_MANUFACTURER_ALIASES = _load_manufacturer_aliases()


def _normalize_manufacturer(s: str) -> str:
    s = _normalize_text(s)
    s = _MFR_SUFFIXES.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 如果整串剛好命中 manufacturer_aliases.md 裡的已知縮寫,展開成完整名稱關鍵字,
    # 這樣後面的子字串/相似度比對才有機會對上(例如 "ti" -> "texas instruments"）。
    return _MANUFACTURER_ALIASES.get(s, s)


def _manufacturer_score(bom_mfr: str, mouser_mfr: str) -> float:
    a, b = _normalize_manufacturer(bom_mfr), _normalize_manufacturer(mouser_mfr)
    if not a or not b:
        return 0.5
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


def _local_description(line: BOMLine) -> str:
    """組合本機可用的比對文字:description + 被動元件的 value/package/tolerance。"""
    parts = [line.description, line.value, line.package, line.tolerance]
    return " ".join(p for p in parts if p)


def _tokenize(s: str) -> set[str]:
    return {t for t in s.split() if t}


def _description_score(bom_line: BOMLine, mouser_part: dict) -> float:
    """
    描述相似度,取兩種算法的較大值:
    - seq_ratio:字元層級序列相似度(difflib),用字/語言/順序一致時準,
      但 BOM 跟 Mouser 常常用不同語言/格式描述同一顆料,這種情況下容易偏低估。
    - token_containment:把兩邊(normalize 後只剩英數字,中文字會被濾掉,
      正好只留下 1uF/10V/X5R/0402 這類規格 token)拆成 token 集合,算 BOM
      這邊組出來的技術屬性有多少比例真的出現在 Mouser 描述裡。不看詞序/語言,
      同一顆料只要規格數值對得上就能拿高分,即使兩邊描述用字完全不同。
    """
    local_desc = _normalize_text(_local_description(bom_line))
    mouser_desc = _normalize_text(mouser_part.get("Description", ""))
    if not local_desc or not mouser_desc:
        return 0.5

    seq_ratio = difflib.SequenceMatcher(None, local_desc, mouser_desc).ratio()

    local_tokens = _tokenize(local_desc)
    mouser_tokens = _tokenize(mouser_desc)
    token_containment = 0.0
    if local_tokens:
        token_containment = len(local_tokens & mouser_tokens) / len(local_tokens)

    return max(seq_ratio, token_containment)


def _mpn_exact_bonus(bom_line: BOMLine, mouser_part: dict) -> float:
    """query_mouser 用 partSearchOptions=Exact;若回傳料號與查詢字串相符,給予加成。"""
    queried = _normalize_text(bom_line.mpn)
    returned = _normalize_text(mouser_part.get("ManufacturerPartNumber", ""))
    return 0.15 if queried and queried == returned else 0.0


@dataclass
class MatchResult:
    mpn: str
    match_status: str                       # matched | needs_human_review | no_candidates
    confidence: float
    matched_part: Optional[dict] = None
    candidates_considered: int = 0
    # 以下兩個欄位由 bom-match-review skill(Claude 在 session 裡親自判斷,不是付費
    # LLM API)事後回填,不是 match_validation() 自己填的。build_report() 只是原樣
    # 透出成報表欄位——真正把 claude_verdict 轉成 match_status 覆寫(matched_by_claude
    # / rejected_by_claude)的邏輯在 bom-report-finalize skill 的合併腳本裡。
    claude_verdict: Optional[str] = None    # "CONFIRM" | "REJECT" | "UNCERTAIN" | None
    claude_reasoning: Optional[str] = None


def match_validation(
    bom_line: BOMLine,
    candidates: list[dict],
    threshold: float = MATCH_CONFIDENCE_THRESHOLD,
) -> MatchResult:
    """比對 Mouser 候選結果與原始 BOM 是否為同一料件,產出信心分數。"""
    if not candidates:
        return MatchResult(mpn=bom_line.mpn, match_status="no_candidates", confidence=0.0)

    best_part = None
    best_score = -1.0
    for part in candidates:
        mfr_score = _manufacturer_score(bom_line.manufacturer, part.get("Manufacturer", ""))
        desc_score = _description_score(bom_line, part)
        bonus = _mpn_exact_bonus(bom_line, part)
        score = min(1.0, 0.5 * mfr_score + 0.5 * desc_score + bonus)

        # 身分覆寫:MPN 完全相符 + 廠商高度相符,在電子料件的世界裡幾乎等於
        # 確認是同一顆料(MPN 是廠商唯一識別碼)。Mouser 的 Description 欄位
        # 有時很簡略(甚至不含任何規格數字,如某些薄膜電阻只寫「薄膜電阻器
        # - SMD」),導致 desc_score 算不出東西、被拖累到門檻以下 —— 但這不該
        # 推翻 MPN+廠商已經給出的強力身分證據,所以在這個條件下把分數墊到 0.92。
        if bonus > 0 and mfr_score >= 0.85:
            score = max(score, 0.92)

        if score > best_score:
            best_score = score
            best_part = part

    status = "matched" if best_score >= threshold else "needs_human_review"
    return MatchResult(
        mpn=bom_line.mpn,
        match_status=status,
        confidence=round(best_score, 3),
        matched_part=best_part,
        candidates_considered=len(candidates),
    )


def match_validation_all(
    bom_lines: list[BOMLine],
    vendor_results: dict[str, list[dict]],
    threshold: float = MATCH_CONFIDENCE_THRESHOLD,
) -> dict[str, MatchResult]:
    """
    只對「有出現在 vendor_results 裡的 key」建立 MatchResult ——key 不在
    vendor_results 代表這筆查詢失敗(見 lookup_all 的 VendorQueryError 處理),
    刻意不當成「查無此料件」,讓節點 D 標記為 query_failed 而不是 no_candidates。
    """
    matches: dict[str, MatchResult] = {}
    for line in bom_lines:
        key = _lookup_key(line)
        if key is None or key in matches or key not in vendor_results:
            continue
        matches[key] = match_validation(line, vendor_results.get(key, []), threshold)
    return matches


# =====================================================================
# 節點 D: 本機端重新合併與報表產出
# =====================================================================

def _select_price_for_qty(price_breaks: list[dict], qty: int) -> tuple[Optional[float], Optional[str]]:
    """
    依數量挑選對應的 Mouser 價格階梯,回傳 (單價, 貨幣代碼) tuple。
    取所有 Quantity <= qty 的階梯中,Quantity 最大的那筆;
    若 qty 小於最低階梯數量,則取最低階梯的單價。

    貨幣由 Mouser 帳號/API Key 綁定的地區決定(例如台灣帳號常回傳 TWD),
    不一定跟 BOM 原始單價(bom_unit_price)同貨幣,所以另外回傳貨幣代碼,
    讓節點 D 把它放進報表,不要默默假設同貨幣就拿來算 price_delta_pct。
    """
    if not price_breaks:
        return None, None

    def _to_float(pb: dict) -> Optional[float]:
        try:
            # Mouser 價格字串可能含貨幣符號 / 千分位逗號,如 "NT$0.0123"
            return float(re.sub(r"[^0-9.]", "", str(pb.get("Price", ""))))
        except (ValueError, TypeError):
            return None

    parsed = []
    for pb in price_breaks:
        try:
            q = int(pb.get("Quantity", 0))
        except (ValueError, TypeError):
            continue
        price = _to_float(pb)
        if price is not None:
            parsed.append((q, price, pb.get("Currency")))
    if not parsed:
        return None, None

    parsed.sort(key=lambda x: x[0])
    applicable = [(price, cur) for q, price, cur in parsed if q <= qty]
    if applicable:
        return applicable[-1]
    return parsed[0][1], parsed[0][2]


def _parse_int_field(v, default: int = 1) -> int:
    """把 Mouser 回傳的 Min/Mult 這類欄位轉成正整數,轉不出來就用 default。"""
    try:
        n = int(re.sub(r"[^0-9]", "", str(v)))
        return n if n > 0 else default
    except (ValueError, TypeError):
        return default


def _compute_order_qty(needed_qty: int, moq: int, mult: int) -> int:
    """
    依 Mouser 的 Min(最小訂購量)/ Mult(訂購倍數)規則,算出能滿足需求的
    最小合法下單量。合法下單量序列為 moq, moq+mult, moq+2*mult, ...
    """
    moq = max(moq, 1)
    mult = max(mult, 1)
    if needed_qty <= moq:
        return moq
    remaining = needed_qty - moq
    steps = -(-remaining // mult)  # ceil division,不依賴 math.ceil
    return moq + steps * mult


def _mouser_part_number_lead_availability(mouser_part: dict) -> tuple[str, str, str]:
    """
    回傳 (Mouser 編號, 交期, 現貨狀況) 三元組。

    「Mouser 編號」跟 manufacturer_part_number(製造商編號,如
    B39941B8048P810)不是同一件事——它是 Mouser 自己網站上用來查詢/下單的
    內部號碼,長得像 "871-B39941B8048P810" 這種格式,前面那組字首是 Mouser
    自己加的。原本的欄位只抓了製造商編號,沒有這個號碼,朋友想直接到 Mouser
    網站核對或下單時反而要自己重新搜尋。

    **這個號碼只有 Mouser 有。** DigiKey 用的是它自己一套完全不同的編號
    系統(概念上等價,但不是同一個號碼,也不能拿去 Mouser 網站搜尋),所以
    `mouser_part.get("_source") == "DigiKey"` 時 Mouser 編號故意留空,不硬拿
    DigiKey 的號碼填進來冒充。

    lead_time/availability 這兩個不受此限制,兩個供應商都可能有值——
    Mouser 候選是 API 回應的原始 dict,直接讀 Mouser 自己的欄位名稱
    (MouserPartNumber/LeadTime/Availability);DigiKey 候選在
    _normalize_digikey_part() 裡已經轉成中立鍵名(LeadTime/Availability)。

    ⚠ Mouser 這幾個欄位名稱是依公開 API 文件寫的,**沒有拿真實帳號驗證過
    完整回應**——跟專案裡其他所有 Mouser 欄位處理原則一致,第一次用之前
    務必 `--limit 3` 小批量測試,打開報表確認 mouser_part_number/lead_time/
    availability 有抓到值,不是整欄空白。抓不到的話最可能是 Mouser 實際
    回傳的欄位名稱跟這裡假設的不同,把那筆候選的完整 JSON 印出來對一下。
    """
    is_digikey = mouser_part.get("_source") == "DigiKey"
    mouser_part_number = "" if is_digikey else (mouser_part.get("MouserPartNumber") or "")
    return (
        mouser_part_number,
        mouser_part.get("LeadTime") or "",
        mouser_part.get("Availability") or "",
    )


def build_report(
    bom_lines: list[BOMLine],
    vendor_results: dict[str, list[dict]],
    matches: dict[str, MatchResult],
    excluded_lines: Optional[list[BOMLine]] = None,
) -> list[dict]:
    """
    在本機把 ref_des / manufacturer(從未離開本機)與節點 B/C 的結果合併。
    excluded_lines(節點 A 的 filter_non_purchasable() 濾掉的品項)一樣會列進報表,
    但標記 match_status = "excluded_non_purchasable",不會有 Mouser 查詢結果。

    每筆料件同時算兩種金額(單位:mouser_currency,通常不等於 BOM 原始貨幣):
    - mouser_line_total:單價 × BOM 需求數量(qty_per_unit),忽略 MOQ,
      代表「理論上剛好買需要的量」的金額。
    - moq_order_qty / moq_unit_price / moq_line_total:套用 Mouser 的
      Min(最小訂購量)/ Mult(訂購倍數)規則後,實際下單量與對應金額 ——
      這才是比較貼近真實採購預算的數字(MOQ 常常會逼你買比需求量更多)。
    """
    excluded_ids = {id(line) for line in (excluded_lines or [])}
    report = []
    for line in bom_lines:
        if id(line) in excluded_ids:
            report.append({
                "ref_des": line.ref_des,
                "mpn": line.mpn,
                "manufacturer": line.manufacturer,
                "qty_per_unit": line.qty_per_unit,
                "match_status": "excluded_non_purchasable",
                "match_confidence": None,
                "bom_unit_price": line.bom_unit_price,
                "mouser_unit_price": None,
                "mouser_currency": None,
                "mouser_line_total": None,
                "moq": None,
                "order_multiple": None,
                "moq_order_qty": None,
                "moq_unit_price": None,
                "moq_line_total": None,
                "manufacturer_part_number": None,
                "mouser_part_number": None,
                "mouser_manufacturer": None,
                "mouser_description": None,
                "lead_time": None,
                "availability": None,
                "price_source": None,
                "data_fetched_at": None,
                "price_delta_pct": None,
                "claude_verdict": None,
                "claude_reasoning": None,
            })
            continue

        key = _lookup_key(line)
        match = matches.get(key) if key else None
        mouser_part = match.matched_part if match else None

        mouser_unit_price, mouser_currency = (None, None)
        moq = order_multiple = moq_order_qty = moq_unit_price = None
        mouser_line_total = moq_line_total = None
        mouser_part_number = lead_time = availability = None

        if mouser_part:
            price_breaks = mouser_part.get("PriceBreaks", [])
            mouser_unit_price, mouser_currency = _select_price_for_qty(price_breaks, line.qty_per_unit)
            if mouser_unit_price is not None:
                mouser_line_total = round(mouser_unit_price * line.qty_per_unit, 4)

            moq = _parse_int_field(mouser_part.get("Min"), default=1)
            order_multiple = _parse_int_field(mouser_part.get("Mult"), default=1)
            moq_order_qty = _compute_order_qty(line.qty_per_unit, moq, order_multiple)
            moq_unit_price, _ = _select_price_for_qty(price_breaks, moq_order_qty)
            if moq_unit_price is not None:
                moq_line_total = round(moq_unit_price * moq_order_qty, 4)

            mouser_part_number, lead_time, availability = _mouser_part_number_lead_availability(mouser_part)

        # 注意:price_delta_pct 假設 bom_unit_price 跟 mouser_unit_price 同貨幣。
        # Mouser 回傳的貨幣依帳號地區而定(常見 TWD),若 BOM 原始單價是其他貨幣
        # (如 USD/CNY),這裡不會自動換算,務必對照 mouser_currency 欄位確認。
        price_delta_pct = None
        if mouser_unit_price is not None and line.bom_unit_price:
            price_delta_pct = round(
                (mouser_unit_price - line.bom_unit_price) / line.bom_unit_price * 100, 2
            )

        if key is None:
            match_status = "missing_mpn"  # 沒有 MPN 也湊不出關鍵字(value/package/tolerance 都是空的)
        elif key not in vendor_results:
            match_status = "query_failed"  # 查詢失敗(網路問題),不是「Mouser 查無此料件」,重跑會自動重試
        elif match is None:
            match_status = "no_candidates"  # 理論上不會發生(key 在 vendor_results 就會有 match),保留防呆
        else:
            match_status = match.match_status

        report.append({
            "ref_des": line.ref_des,
            "mpn": line.mpn,
            "manufacturer": line.manufacturer,
            "qty_per_unit": line.qty_per_unit,
            "match_status": match_status,
            "match_confidence": match.confidence if match else None,
            "bom_unit_price": line.bom_unit_price,
            "mouser_unit_price": mouser_unit_price,
            "mouser_currency": mouser_currency,
            "mouser_line_total": mouser_line_total,
            "moq": moq,
            "order_multiple": order_multiple,
            "moq_order_qty": moq_order_qty,
            "moq_unit_price": moq_unit_price,
            "moq_line_total": moq_line_total,
            # BOM 沒填 mpn 的被動元件(靠 keyword 查詢配對)在 "mpn" 欄位一定是空的,
            # 這欄補上供應商實際回傳的型號,不然使用者完全不知道該去買哪顆料。
            "manufacturer_part_number": mouser_part.get("ManufacturerPartNumber") if mouser_part else None,
            # Mouser 自己網站上的內部料號("871-B39941B8048P810" 這種格式),不是
            # 上面的製造商編號。DigiKey 沒有這個號碼(DigiKey 用自己的一套編號,
            # 概念上等價但不是同一個號碼),來源是 DigiKey 時故意留空,見
            # _mouser_part_number_lead_availability() 的說明。
            "mouser_part_number": mouser_part_number,
            "mouser_manufacturer": mouser_part.get("Manufacturer") if mouser_part else None,
            "mouser_description": mouser_part.get("Description") if mouser_part else None,
            "lead_time": lead_time,
            "availability": availability,
            "price_source": mouser_part.get("_source", "Mouser") if mouser_part else None,
            # 這筆報價是哪一天查回來的。空白代表資料來自加入這個欄位之前的舊快取,
            # 日期不詳——不是「今天查的」。快取沒有時效,見 _load_cache 的說明。
            "data_fetched_at": mouser_part.get("_fetched_at") if mouser_part else None,
            "price_delta_pct": price_delta_pct,
            "claude_verdict": match.claude_verdict if match else None,
            "claude_reasoning": match.claude_reasoning if match else None,
        })
    return report


_REPORT_COLUMNS = [
    "ref_des", "mpn", "manufacturer", "qty_per_unit", "match_status",
    "match_confidence", "bom_unit_price", "mouser_unit_price", "mouser_currency",
    "mouser_line_total", "moq", "order_multiple", "moq_order_qty",
    "moq_unit_price", "moq_line_total",
    "manufacturer_part_number", "mouser_part_number", "mouser_manufacturer", "mouser_description",
    "lead_time", "availability",
    "price_source", "data_fetched_at", "price_delta_pct",
    "claude_verdict", "claude_reasoning",
]


# 只有這些狀態代表「配對確認無誤」,才該被算進總價/MOQ 預算。其餘狀態
# (needs_human_review 還沒確認、no_candidates/missing_mpn 沒配對、
# rejected_by_claude 已判定配錯、query_failed 查詢失敗)即使候選剛好帶了
# 價格,也不代表那是這個料件真正該買的東西,不能算進總價——不然總價會被
# 明知是錯的配對(甚至完全不同類別的料件)灌水。這些行仍然完整保留在
# BOM_vs_Vendors 明細裡,只是不計入 Summary 的加總。
_CONFIRMED_MATCH_STATUSES = {"matched", "matched_by_claude"}


def compute_summary(report: list[dict]) -> dict:
    """
    彙總整份報表:
    - status_counts:各 match_status 的筆數
    - totals_by_currency:{currency: {"unit_price_total": ..., "moq_budget_total": ...,
      "priced_lines": N, "moq_priced_lines": N}}
      分貨幣加總,因為不同貨幣不能直接相加。只加總 match_status 屬於
      _CONFIRMED_MATCH_STATUSES 的列,見上方常數的說明。priced_lines/
      moq_priced_lines 分開算是因為兩者可能不同:配對成功但候選沒帶完整
      PriceBreaks 時,可能算得出其中一種價格、算不出另一種。
    - multi_currency:True 表示這份報表混到一種以上的貨幣,加總時要注意。
    """
    status_counts: dict[str, int] = {}
    totals_by_currency: dict[str, dict] = {}
    total_lines = len(report)
    unique_mpns = len({row["mpn"] for row in report if row.get("mpn")})

    for row in report:
        status_counts[row["match_status"]] = status_counts.get(row["match_status"], 0) + 1

        currency = row.get("mouser_currency")
        if currency is None:
            # 沒有貨幣代表這列完全沒有價格可算(excluded_non_purchasable、
            # missing_mpn、query_failed、no_candidates,或雖然 matched 但候選
            # 沒帶 PriceBreaks)——沒有貨幣就無法歸類進下面按貨幣分桶的加總,
            # 只能跳過,不計入 totals_by_currency。
            continue

        if row["match_status"] not in _CONFIRMED_MATCH_STATUSES:
            continue

        bucket = totals_by_currency.setdefault(currency, {
            "unit_price_total": 0.0, "moq_budget_total": 0.0,
            "priced_lines": 0, "moq_priced_lines": 0,
        })
        if row.get("mouser_line_total") is not None:
            bucket["unit_price_total"] += row["mouser_line_total"]
            bucket["priced_lines"] += 1
        if row.get("moq_line_total") is not None:
            bucket["moq_budget_total"] += row["moq_line_total"]
            bucket["moq_priced_lines"] += 1

    for bucket in totals_by_currency.values():
        bucket["unit_price_total"] = round(bucket["unit_price_total"], 2)
        bucket["moq_budget_total"] = round(bucket["moq_budget_total"], 2)

    # Top 10 高價料件,三種排序各列一份,因為三個數字代表的意義不同:
    # - top10_by_unit_price:單純「單價」最高,不看數量(哪顆零件本身最貴)
    # - top10_by_line_total:忽略 MOQ,「單價 × 需求數量」貢獻最大的料件
    # - top10_by_moq_line_total:套用 MOQ 規則後,實際預算貢獻最大的料件
    #   (常常是因為 MOQ 遠高於需求量,把預算撐大的料件,跟上面兩份排名可能差很多)
    # 跟上面的總價加總同一個原則:只從 _CONFIRMED_MATCH_STATUSES 裡面選,
    # 不然「Claude 判定配錯」或「還沒確認」的候選價格一樣會混進榜單,誤導
    # 使用者以為某個高價項目是真的要花這筆錢。
    confirmed_rows = [r for r in report if r["match_status"] in _CONFIRMED_MATCH_STATUSES]
    top10_by_unit_price = sorted(
        (r for r in confirmed_rows if r.get("mouser_unit_price") is not None),
        key=lambda r: r["mouser_unit_price"], reverse=True,
    )[:10]
    top10_by_line_total = sorted(
        (r for r in confirmed_rows if r.get("mouser_line_total") is not None),
        key=lambda r: r["mouser_line_total"], reverse=True,
    )[:10]
    top10_by_moq_line_total = sorted(
        (r for r in confirmed_rows if r.get("moq_line_total") is not None),
        key=lambda r: r["moq_line_total"], reverse=True,
    )[:10]

    # 報價新鮮度:快取沒有時效,同一份報表裡的料件可能是不同時間查的
    # (上週查了一半、今天補查剩下的,或者半年前查過的直接命中快取)。
    # 列出最舊/最新兩個日期,使用者一眼就能看出「這份報價的資料橫跨多久」。
    priced_rows = [r for r in report if r.get("price_source")]
    fetch_dates = sorted({r["data_fetched_at"] for r in priced_rows if r.get("data_fetched_at")})
    unknown_fetch_lines = sum(1 for r in priced_rows if not r.get("data_fetched_at"))

    return {
        "total_lines": total_lines,
        "unique_mpns": unique_mpns,
        "status_counts": status_counts,
        "totals_by_currency": totals_by_currency,
        "multi_currency": len(totals_by_currency) > 1,
        "data_fetched_earliest": fetch_dates[0] if fetch_dates else None,
        "data_fetched_latest": fetch_dates[-1] if fetch_dates else None,
        "data_fetched_unknown_lines": unknown_fetch_lines,
        "top10_by_unit_price": top10_by_unit_price,
        "top10_by_line_total": top10_by_line_total,
        "top10_by_moq_line_total": top10_by_moq_line_total,
    }


def _autosize_columns(ws, df: pd.DataFrame) -> None:
    _autosize_columns_at(ws, df, header_row=1)


def _autosize_columns_at(ws, df: pd.DataFrame, header_row: int) -> None:
    """跟 _autosize_columns 一樣,但用於不是從試算表第一列開始的表格(如 Summary
    分頁裡接在總覽數字後面的 Top 10 表格)。"""
    for i, col in enumerate(df.columns, start=1):
        # pandas 3.x 的 .astype(str) 對 NA 值不一定轉成 "nan" 字串(可能仍是
        # NA/float),用 apply + pd.notna 檢查比較保險,避免 .map(len) 對 NA 炸掉。
        cell_lens = df[col].apply(lambda v: len(str(v)) if pd.notna(v) else 0)
        max_len = cell_lens.max() if len(df) else len(col)
        width = max(12, min(60, int(max_len or 10) + 2))
        col_letter = ws.cell(row=header_row, column=i).column_letter
        current = ws.column_dimensions[col_letter].width or 0
        ws.column_dimensions[col_letter].width = max(current, width)


#  "mpn" 是 BOM 自己填的型號,被動元件常是空的(靠 keyword 查詢配對)——
#  Top 10 一定要帶 "manufacturer_part_number"(供應商實際回傳的型號)跟 "mouser_description"
#  (供應商回傳的品名),不然 BOM 沒填型號的那幾列在榜單裡完全看不出是哪顆料。
#  不放 ref_des:Top 10 是「哪顆料最貴」的視角,不是「哪個位號」,位號要對照
#  細節要看 BOM_vs_Vendors 分頁。
_TOP10_UNIT_PRICE_COLUMNS = [
    "mpn", "manufacturer_part_number", "manufacturer", "mouser_description", "qty_per_unit",
    "mouser_unit_price", "mouser_currency",
    "match_status", "price_source",
]
_TOP10_LINE_TOTAL_COLUMNS = [
    "mpn", "manufacturer_part_number", "manufacturer", "mouser_description", "qty_per_unit",
    "mouser_unit_price", "mouser_currency", "mouser_line_total",
    "match_status", "price_source",
]
_TOP10_MOQ_COLUMNS = [
    "mpn", "manufacturer_part_number", "manufacturer", "mouser_description", "qty_per_unit",
    "moq", "moq_order_qty", "moq_unit_price", "mouser_currency", "moq_line_total",
    "match_status", "price_source",
]


def write_report_xlsx(report: list[dict], out_path: str) -> dict:
    """輸出主表(BOM_vs_Vendors)+ 彙總表(Summary,含理論總價、含 MOQ 預算、Top 10 高價料件)。回傳 summary dict。"""
    df = pd.DataFrame(report, columns=_REPORT_COLUMNS)
    summary = compute_summary(report)

    summary_rows = [
        {"項目": "報表產生日期", "數值": date.today().isoformat()},
        {"項目": "料件總數(BOM 列數)", "數值": summary["total_lines"]},
        {"項目": "唯一 MPN 數量", "數值": summary["unique_mpns"]},
        {"項目": "", "數值": ""},
    ]

    # 報價新鮮度。快取沒有時效,重跑 pipeline 不會自動更新價格/MOQ,所以
    # 一定要在使用者第一眼看得到的地方講清楚這份報價的資料是哪天查的。
    earliest, latest = summary["data_fetched_earliest"], summary["data_fetched_latest"]
    if earliest and latest and earliest != latest:
        summary_rows.append({"項目": "供應商報價查詢日期", "數值": f"{earliest} ~ {latest}"})
    elif latest:
        summary_rows.append({"項目": "供應商報價查詢日期", "數值": latest})
    if summary["data_fetched_unknown_lines"]:
        summary_rows.append({
            "項目": f"⚠ 其中 {summary['data_fetched_unknown_lines']} 筆查詢日期不詳(來自舊快取)",
            "數值": "",
        })
    if earliest or summary["data_fetched_unknown_lines"]:
        summary_rows.append({
            "項目": "⚠ 價格/MOQ 為查詢當下的快照,快取無時效;要最新報價請刪掉 .mouser_cache.json 重查",
            "數值": "",
        })
        summary_rows.append({"項目": "", "數值": ""})
    summary_rows += [{"項目": "match_status: " + status, "數值": count}
                      for status, count in sorted(summary["status_counts"].items())]
    summary_rows.append({"項目": "", "數值": ""})
    if summary["multi_currency"]:
        summary_rows.append({
            "項目": "⚠ 注意:本次查詢結果含多種貨幣,以下總價分貨幣列出,不可直接相加",
            "數值": "",
        })
    for currency, bucket in sorted(summary["totals_by_currency"].items()):
        summary_rows.extend([
            {"項目": f"[{currency}] 單價總價(忽略 MOQ,{bucket['priced_lines']} 筆有價格)",
             "數值": bucket["unit_price_total"]},
            {"項目": f"[{currency}] 含 MOQ 的預算評估({bucket['moq_priced_lines']} 筆有價格)",
             "數值": bucket["moq_budget_total"]},
        ])
    summary_df = pd.DataFrame(summary_rows, columns=["項目", "數值"])

    # 三份 Top 10 表格依序接在總覽下面,各自標題文字 + 內容,中間空一列。
    top10_tables = [
        ("Top 10 高價料件(依單價排序,單價最高排到最低,不看數量)",
         pd.DataFrame(summary["top10_by_unit_price"], columns=_TOP10_UNIT_PRICE_COLUMNS)),
        ("Top 10 高價料件(依單價總價排序,忽略 MOQ)",
         pd.DataFrame(summary["top10_by_line_total"], columns=_TOP10_LINE_TOTAL_COLUMNS)),
        ("Top 10 高價料件(依含 MOQ 預算排序)",
         pd.DataFrame(summary["top10_by_moq_line_total"], columns=_TOP10_MOQ_COLUMNS)),
    ]

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        ws = writer.sheets["Summary"]
        _autosize_columns(ws, summary_df)

        row = len(summary_df) + 3
        for title, table_df in top10_tables:
            ws.cell(row=row, column=1, value=title)
            table_df.to_excel(writer, index=False, sheet_name="Summary", startrow=row)
            _autosize_columns_at(ws, table_df, header_row=row + 1)
            row = row + len(table_df) + 4

        df.to_excel(writer, index=False, sheet_name="BOM_vs_Vendors")
        _autosize_columns(writer.sheets["BOM_vs_Vendors"], df)

    print(f"已輸出報表: {out_path}")
    print(f"  料件總數: {summary['total_lines']}(唯一 MPN {summary['unique_mpns']} 個)")
    for currency, bucket in sorted(summary["totals_by_currency"].items()):
        print(f"  [{currency}] 單價總價(忽略 MOQ): {bucket['unit_price_total']:,.2f}"
              f" | 含 MOQ 預算: {bucket['moq_budget_total']:,.2f}")
    if summary["multi_currency"]:
        print("  ⚠ 查詢結果含多種貨幣,以上總價分開列出,請勿直接相加")
    return summary


# =====================================================================
# 節點 D(選用):HTML 版報表(給人看的視覺化摘要,跟 xlsx 共用
# compute_summary() 的計算結果,兩份輸出的數字保證一致)
# =====================================================================

_HTML_REPORT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / ".claude" / "skills" / "bom-report-finalize" / "assets" / "report_template.html"
)

# (顯示文字, comp-bar 顏色分組) —— "good"/"warning"/"critical" 三組是「有查到
# 候選、有實際報價」的狀態,任何不在這個字典裡的未知狀態一律歸進下面的
# _NO_PRICE_STATUS_ORDER(比誤判成「有報價」安全)。
_PRICED_STATUS_ORDER = [
    ("matched", "good", "規則式評分信心足夠,直接判定配對正確"),
    ("matched_by_claude", "good", "分數卡在門檻附近,Claude 複核後確認正確"),
    ("rejected_by_claude", "critical", "Claude 複核後判定配對錯誤(如型號、類別不符)"),
    ("needs_human_review", "warning", "規則式評分卡在門檻附近,尚待確認"),
]
_NO_PRICE_STATUS_ORDER = [
    ("no_candidates", "Mouser / DigiKey 皆查無此料"),
    ("missing_mpn", "湊不出查詢關鍵字,沒有送出查詢"),
    ("excluded_non_purchasable", "PCB 本身或包材等非採購項"),
    ("query_failed", "查詢時網路/API 失敗,重跑指令會自動只重試這些筆"),
]


def _html_escape(s) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _report_row_to_item(row: dict, include_line_total: bool) -> dict:
    """把一列報表 dict 轉成 HTML 樣板 JS 認得的欄位形狀(見 report_template.html)。"""
    bom_mpn = row.get("mpn") or "—"
    item = {
        "mpn": bom_mpn,
        "mouserMpn": row.get("manufacturer_part_number") or bom_mpn,
        "mfr": row.get("mouser_manufacturer") or row.get("manufacturer") or "—",
        "desc": row.get("mouser_description") or "",
        "qty": row.get("qty_per_unit"),
        "price": row.get("mouser_unit_price"),
        "status": row.get("match_status"),
    }
    if include_line_total:
        item["lineTotal"] = row.get("mouser_line_total")
    return item


def render_html_report(report: list[dict], summary: dict, source_bom_path: str, generated_date: Optional[str] = None) -> str:
    """
    產生 HTML 版報表(視覺化摘要,給人看,不是給程式讀的中繼檔)。
    直接吃 build_report() 的 report 跟 compute_summary() 的 summary,
    不重新計算任何金額——跟 write_report_xlsx() 輸出的數字保證一致。
    """
    template = _HTML_REPORT_TEMPLATE_PATH.read_text(encoding="utf-8")
    generated_date = generated_date or date.today().isoformat()
    source_name = Path(source_bom_path).name
    page_heading = Path(source_bom_path).stem

    total_lines = summary["total_lines"]
    unique_mpns = summary["unique_mpns"]
    status_counts = summary["status_counts"]

    totals_by_currency = summary["totals_by_currency"]
    if totals_by_currency:
        currency = max(totals_by_currency, key=lambda c: totals_by_currency[c]["priced_lines"])
        bucket = totals_by_currency[currency]
        unit_total, moq_total, priced_lines = bucket["unit_price_total"], bucket["moq_budget_total"], bucket["priced_lines"]
    else:
        currency, unit_total, moq_total, priced_lines = "—", 0.0, 0.0, 0
    multi_currency_note = (
        f"(此報表另有 {len(totals_by_currency) - 1} 種其他幣別,詳見明細分頁,不會自動加總換算)"
        if summary["multi_currency"] else ""
    )

    confirmed_count = sum(status_counts.get(s, 0) for s in _CONFIRMED_MATCH_STATUSES)
    match_rate = round(confirmed_count / total_lines * 100) if total_lines else 0
    review_count = status_counts.get("needs_human_review", 0)

    confirm_n = sum(1 for r in report if r.get("claude_verdict") == "CONFIRM")
    reject_n = sum(1 for r in report if r.get("claude_verdict") == "REJECT")
    uncertain_n = sum(1 for r in report if r.get("claude_verdict") == "UNCERTAIN")
    reviewed_total = confirm_n + reject_n + uncertain_n
    has_review = reviewed_total > 0
    review_hint_suffix = " + Claude 複核(bom-match-review)" if has_review else ""

    # ---------- header meta ----------
    fetch_note = ""
    earliest, latest = summary.get("data_fetched_earliest"), summary.get("data_fetched_latest")
    if earliest and latest and earliest != latest:
        fetch_note = f'<span class="dot">供應商報價查詢日期:<strong> {_html_escape(earliest)} ~ {_html_escape(latest)}</strong></span>'
    elif latest:
        fetch_note = f'<span class="dot">供應商報價查詢日期:<strong> {_html_escape(latest)}</strong></span>'
    header_meta = (
        f'<span>來源:<strong> {_html_escape(source_name)}</strong></span>'
        f'<span class="dot"><strong>{total_lines}</strong> 列 / <strong>{unique_mpns}</strong> 個唯一 MPN</span>'
        f'<span class="dot">主要資料源:<strong> Mouser</strong>(查無結果改查 DigiKey)</span>'
        f'{fetch_note}'
        f'<span class="dot">產出日期:<strong> {_html_escape(generated_date)}</strong></span>'
    )

    # ---------- hero stats ----------
    hero_stats = f'''<div class="stat-grid">
      <div class="stat-tile hero">
        <p class="label">單價總價(忽略 MOQ)</p>
        <p class="value">{unit_total:,.0f}<span class="unit">{_html_escape(currency)}</span></p>
        <p class="sub">僅計入確認無誤的配對 · {priced_lines} 筆有價格</p>
      </div>
      <div class="stat-tile">
        <p class="label">含 MOQ 的預算評估</p>
        <p class="value">{moq_total:,.0f}<span class="unit">{_html_escape(currency)}</span></p>
        <p class="sub">依供應商最小訂購量估算</p>
      </div>
      <div class="stat-tile">
        <p class="label">配對確認率</p>
        <p class="value">{match_rate}<span class="unit">%</span></p>
        <p class="sub">{confirmed_count} / {total_lines} 列判定 matched</p>
      </div>
      <div class="stat-tile warn-tile">
        <p class="label">待人工複核</p>
        <p class="value">{review_count}<span class="unit">列</span></p>
        <p class="sub">{"Claude 複核後仍無把握,見下方" if review_count else "目前沒有待複核項目"}</p>
      </div>
    </div>'''

    # ---------- composition bar + legend ----------
    priced_rows_by_status = {s: status_counts.get(s, 0) for s, _, _ in _PRICED_STATUS_ORDER}
    priced_total = sum(priced_rows_by_status.values())
    known_statuses = {s for s, _, _ in _PRICED_STATUS_ORDER} | {s for s, _ in _NO_PRICE_STATUS_ORDER}
    other_total = sum(v for k, v in status_counts.items() if k not in known_statuses)
    no_price_total = sum(status_counts.get(s, 0) for s, _ in _NO_PRICE_STATUS_ORDER) + other_total

    color_var = {"good": "var(--good)", "warning": "var(--warning)", "critical": "var(--critical)"}
    seg_totals = {"good": 0, "warning": 0, "critical": 0}
    for s, grp, _ in _PRICED_STATUS_ORDER:
        seg_totals[grp] += status_counts.get(s, 0)
    segments = []
    for grp in ("good", "warning", "critical"):
        if seg_totals[grp]:
            segments.append(f'<div class="comp-seg" style="flex-grow:{seg_totals[grp]}; background:{color_var[grp]};"></div>')
    if no_price_total:
        segments.append(f'<div class="comp-seg" style="flex-grow:{no_price_total}; background:var(--neutral-mark);"></div>')
    aria_label = (f"{total_lines} 列中,{confirmed_count} 列配對確認、{review_count} 列待複核、"
                  f"{status_counts.get('rejected_by_claude', 0)} 列配對錯誤、{no_price_total} 列無資料")
    comp_bar_block = (
        f'<div class="comp-bar" role="img" aria-label="{_html_escape(aria_label)}">\n        '
        + "\n        ".join(segments) + "\n      </div>"
    )

    legend_parts = []
    if priced_total:
        legend_parts.append(f'<div class="comp-group-label">↓ 以下 {priced_total} 列都有查到候選、有實際報價</div>')
        for s, grp, desc in _PRICED_STATUS_ORDER:
            n = status_counts.get(s, 0)
            if not n:
                continue
            legend_parts.append(
                f'<div class="comp-row"><span class="comp-dot" style="background:{color_var[grp]};"></span>'
                f'<div class="rt"><div class="rt-top"><span class="name">{s}</span><span class="count">{n}</span></div>'
                f'<div class="rt-desc">{_html_escape(desc)}</div></div></div>'
            )
    if no_price_total:
        legend_parts.append(f'<div class="comp-group-label">↓ 以下 {no_price_total} 列從一開始就沒有價格可算,不是被排除,是根本沒有數字</div>')
        for s, desc in _NO_PRICE_STATUS_ORDER:
            n = status_counts.get(s, 0)
            if not n:
                continue
            legend_parts.append(
                f'<div class="comp-row"><span class="comp-dot" style="background:var(--neutral-mark);"></span>'
                f'<div class="rt"><div class="rt-top"><span class="name">{s}</span><span class="count">{n}</span></div>'
                f'<div class="rt-desc">{_html_escape(desc)}</div></div></div>'
            )
        if other_total:
            legend_parts.append(
                f'<div class="comp-row"><span class="comp-dot" style="background:var(--neutral-mark);"></span>'
                f'<div class="rt"><div class="rt-top"><span class="name">其他</span><span class="count">{other_total}</span></div>'
                f'<div class="rt-desc">未列在上面分類裡的狀態</div></div></div>'
            )
    comp_legend = "\n        ".join(legend_parts)

    verdict_row = ""
    if has_review:
        verdict_row = (
            '<div class="verdict-row">'
            f'<span class="pill good"><span class="n">{confirm_n}</span> CONFIRM</span>'
            f'<span class="pill critical"><span class="n">{reject_n}</span> REJECT</span>'
            f'<span class="pill warning"><span class="n">{uncertain_n}</span> UNCERTAIN</span>'
            f'<span style="font-size:12.5px; color:var(--ink-muted); align-self:center;">— Claude 對 {reviewed_total} 筆模糊配對的複核結果</span>'
            '</div>'
        )

    # ---------- callout ----------
    excluded_priced_n = status_counts.get("rejected_by_claude", 0) + status_counts.get("needs_human_review", 0)
    if has_review:
        callout_body = (
            f'<b>總價只算確認過的配對,{confirmed_count} / {total_lines} 列。</b>'
            f'另外 {total_lines - confirmed_count} 列沒算進去,原因分兩種:'
            f'<b>{excluded_priced_n} 列「有價格但不可信」</b>——Mouser/DigiKey 有查到候選、有報價,但被 Claude 判定配錯'
            f'({status_counts.get("rejected_by_claude", 0)} 列)或還沒確認({status_counts.get("needs_human_review", 0)} 列),故意排除避免灌水;'
            f'<b>{no_price_total} 列「根本沒有價格」</b>——查無此料、湊不出查詢關鍵字、或是非採購項,這些從一開始就沒有數字可算,不是「排除」的問題。'
        )
    else:
        callout_body = (
            f'<b>總價只算確認過的配對,{confirmed_count} / {total_lines} 列。</b>'
            f'其餘 {total_lines - confirmed_count} 列沒算進去——{review_count} 列信心不足還沒複核、{no_price_total} 列根本沒查到候選或沒有價格。'
            f'還沒跑 bom-match-review,{review_count} 列 needs_human_review 建議複核後再看一次這份報表。'
        )

    # ---------- excluded (priced but not counted) ----------
    excluded_rows = [
        r for r in report
        if r.get("match_status") in ("rejected_by_claude", "needs_human_review")
        and (r.get("mouser_line_total") is not None or r.get("mouser_unit_price") is not None)
    ]
    excluded_section = ""
    if excluded_rows:
        excluded_rows = sorted(excluded_rows, key=lambda r: r.get("mouser_line_total") or r.get("mouser_unit_price") or 0, reverse=True)
        excluded_amount = sum((r.get("mouser_line_total") or r.get("mouser_unit_price") or 0) for r in excluded_rows)
        excluded_section = f'''
  <section class="section">
    <div class="section-head">
      <h2>不算進總價的 {len(excluded_rows)} 筆</h2>
      <span class="hint">candidate 有查到價格,但配對被判定錯誤或不確定,合計 {excluded_amount:,.2f} {_html_escape(currency)} 未計入總價</span>
    </div>
    <div class="card chart-card">
      <div id="list-excluded"></div>
    </div>
  </section>'''

    excluded_items = []
    for r in excluded_rows:
        excluded_items.append({
            "refdes": r.get("ref_des") or "—",
            "bomMpn": r.get("mpn") or "（無型號）",
            "bomMfr": r.get("manufacturer") or "—",
            "mouserMpn": r.get("manufacturer_part_number") or "—",
            "mfr": r.get("mouser_manufacturer") or "—",
            "desc": r.get("mouser_description") or "",
            "price": r.get("mouser_line_total") if r.get("mouser_line_total") is not None else r.get("mouser_unit_price"),
            "status": "rejected" if r.get("match_status") == "rejected_by_claude" else "uncertain",
            "reason": r.get("claude_reasoning") or "（沒有記錄複核理由)",
        })

    # ---------- top 10 lists (already confirmed-only + sorted by compute_summary) ----------
    unit_price_top10 = summary.get("top10_by_unit_price", [])
    line_total_top10 = summary.get("top10_by_line_total", [])
    unit_price_items = [_report_row_to_item(r, include_line_total=False) for r in unit_price_top10]
    unit_price_keys = {(r.get("manufacturer_part_number") or r.get("mpn") or r.get("ref_des")) for r in unit_price_top10}
    line_total_items = []
    for r in line_total_top10:
        item = _report_row_to_item(r, include_line_total=True)
        key = r.get("manufacturer_part_number") or r.get("mpn") or r.get("ref_des")
        item["diffOnly"] = key not in unit_price_keys
        line_total_items.append(item)

    report_data_json = json.dumps(
        {"unitPriceTop10": unit_price_items, "lineTotalTop10": line_total_items, "excluded": excluded_items},
        ensure_ascii=False,
    ).replace("</", "<\\/")  # 保險:描述文字裡萬一出現 "</script" 這種字串,避免提早關閉 <script> 標籤

    html = template
    replacements = {
        "{{TITLE}}": f"{page_heading} 報價分析",
        "{{PAGE_HEADING}}": page_heading,
        "{{HEADER_META}}": header_meta,
        "{{HERO_STATS}}": hero_stats,
        "{{CALLOUT_BODY}}": callout_body,
        "{{TOTAL_LINES}}": str(total_lines),
        "{{REVIEW_HINT_SUFFIX}}": review_hint_suffix,
        "{{COMP_BAR_BLOCK}}": comp_bar_block,
        "{{COMP_LEGEND}}": comp_legend,
        "{{VERDICT_ROW}}": verdict_row,
        "{{EXCLUDED_SECTION}}": excluded_section,
        "{{CURRENCY}}": _html_escape(currency),
        "{{META_SOURCE_FILE}}": _html_escape(source_name),
        "{{MULTI_CURRENCY_NOTE}}": multi_currency_note,
        "{{REPORT_DATA_JSON}}": report_data_json,
    }
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html


def write_html_report(report: list[dict], summary: dict, source_bom_path: str, out_path: str) -> None:
    html = render_html_report(report, summary, source_bom_path)
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"已輸出 HTML 報表: {out_path}")


# =====================================================================
# Pipeline 進入點
# =====================================================================

def run_pipeline(
    bom_path: str,
    out_path: str,
    use_cache: bool = True,
    enable_keyword_fallback: bool = True,
    enable_digikey_fallback: bool = True,
    limit: Optional[int] = None,
    write_html: bool = True,
) -> list[dict]:
    """
    一次跑完整條 pipeline:A(load_bom_file → parse_bom → filter_non_purchasable)
    → B(lookup_all)→ C(match_validation_all)→ D(build_report →
    write_report_xlsx)。等同於分開跑 bom-parse/bom-vendor-lookup/
    bom-match-score/bom-report-finalize 四個 skill,只是中間不落地成 JSON、
    一次呼叫做完(代價是拿不到 bom-match-review 那步的 Claude 語意複核——
    那一步沒有對應的函式,只存在於 Claude Code skill 裡)。

    跟 `bom-report-finalize` skill 的 `run.py` 一樣,預設除了 xlsx 也會在
    同路徑輸出一份同檔名的 `.html` 視覺化摘要(`write_html=False` 關掉)——
    兩個入口(這支 CLI 跟 skill 版)產出的東西要一致,不然使用者從
    README「五分鐘走完全流程」那條路徑跑,會少一份 skill 版本才有的 html。

    `api_key = _get_api_key()` 刻意放在最前面:沒設定 MOUSER_SEARCH_API_KEY
    的話,寧可在讀檔、解析 BOM 之前就先失敗,不要讓使用者等到查詢階段
    才發現憑證沒設好。
    """
    api_key = _get_api_key()
    if enable_digikey_fallback and DIGIKEY_ENABLED:
        print("[mouser_lookup] DigiKey fallback 已啟用(Mouser 查無結果時會自動改查 DigiKey)")
    elif enable_digikey_fallback and not DIGIKEY_ENABLED:
        print("[mouser_lookup] 未設定 DIGIKEY_CLIENT_ID/SECRET,DigiKey fallback 停用中")

    rows = load_bom_file(bom_path)
    bom_lines = parse_bom(rows)
    purchasable, excluded = filter_non_purchasable(bom_lines)
    print(f"[bom_parsing] 共 {len(bom_lines)} 列,濾掉 {len(excluded)} 列非採購項"
          f"(PCB/包材等),{len(purchasable)} 列待查價")

    if limit is not None:
        purchasable = purchasable[:limit]
        print(f"[dry-run] --limit 生效,只對前 {len(purchasable)} 筆料件呼叫 API")

    vendor_results = lookup_all(
        purchasable, api_key=api_key, use_cache=use_cache,
        enable_keyword_fallback=enable_keyword_fallback,
        enable_digikey_fallback=enable_digikey_fallback,
    )
    matches = match_validation_all(purchasable, vendor_results)

    # 報表列出「本次實際查詢範圍」內的品項 + 被濾掉的非採購項(PCB/包材等,
    # 這些不佔查詢額度,不受 --limit 影響,一定會出現在報表裡標記
    # excluded_non_purchasable);不含 limit 之外、本次沒查的品項,避免報表出現
    # 看似「查無結果」卻其實根本沒查的列。用原始 bom_lines 的順序輸出,
    # 這樣報表跟原始 BOM 檔案的列順一致,比較好對照。
    used_ids = {id(line) for line in purchasable} | {id(line) for line in excluded}
    report_lines = [line for line in bom_lines if id(line) in used_ids]
    report = build_report(report_lines, vendor_results, matches, excluded_lines=excluded)
    summary = write_report_xlsx(report, out_path)  # 內部會印出各貨幣的總價 / MOQ 預算摘要
    if write_html:
        write_html_report(report, summary, bom_path, str(Path(out_path).with_suffix(".html")))
    return report


def main():
    """CLI 入口:解析參數後直接呼叫 run_pipeline()。單獨測試/從別的腳本呼叫
    pipeline 邏輯時,請直接 import run_pipeline,不需要經過這層 argparse。"""
    parser = argparse.ArgumentParser(description="BOM → Mouser 比價 Pipeline")
    parser.add_argument("bom_path", help="BOM 檔案路徑(.xlsx / .csv)")
    parser.add_argument("-o", "--out", default="bom_mouser_report.xlsx", help="輸出報表路徑")
    parser.add_argument("--no-cache", action="store_true", help="停用本機查詢快取(.mouser_cache.json)")
    parser.add_argument("--no-keyword-fallback", action="store_true",
                         help="停用缺 MPN 料件的 keyword 搜尋 fallback")
    parser.add_argument("--no-digikey-fallback", action="store_true",
                         help="停用 Mouser 查無結果時的 DigiKey fallback(即使有設定 DIGIKEY_CLIENT_ID/SECRET)")
    parser.add_argument("--limit", type=int, default=None,
                         help="只處理前 N 筆 BOM 列(小批量測試用,避免一次跑完整份 BOM;"
                              "注意是切 BOM 列不是切唯一料件,同一 MPN 出現多次時實際查詢數會比 N 少)")
    parser.add_argument("--no-html", action="store_true",
                         help="停用預設會一起輸出的 HTML 視覺化摘要網頁(只出 xlsx)")
    args = parser.parse_args()
    run_pipeline(
        args.bom_path, args.out,
        use_cache=not args.no_cache,
        enable_keyword_fallback=not args.no_keyword_fallback,
        enable_digikey_fallback=not args.no_digikey_fallback,
        limit=args.limit,
        write_html=not args.no_html,
    )


if __name__ == "__main__":
    main()
