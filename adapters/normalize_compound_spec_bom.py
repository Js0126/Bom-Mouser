#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
adapters/normalize_compound_spec_bom.py

有些供應商/EMS 廠給的 BOM,會把 MPN、廠牌、備註全部塞進同一個「規格型號」
欄位,例如(範例來自實際處理過的一份 BOM,「品牌」「备注」括號標籤排版):

    STM32F030CCT6（品牌：ST）（备注：LQFP-48）
    RJE60-188-51A1（备注：万兆 DIP90度 带灯 带弹8P8C）（品牌：安费诺）   <- 品牌/备注順序也可能顛倒

被動元件(電阻/電容/電感等)常常完全沒有型號,只有一段規格描述文字,例如:

    厚膜电阻 0Ω ±5% 1/16W SMD 0402(公制1005)
    多层陶瓷电容 MLCC 10PF ±5% 50V C0G SMD 0402(公制1005)

這種「一格塞多個屬性」的結構,沒辦法只靠幫 mouser_lookup.py 的
_COLUMN_ALIASES 補別名解決(別名比對是整欄一對一對應,不會拆解儲存格內容)。
這支腳本把原始 BOM 拆解成 bom-parse 認得的標準欄位(名称/型号/厂家/位置/
数量/值/封装/公差/特性参数),直接在記憶體裡呼叫 mouser_lookup.parse_bom()
產生 parsed_bom.json —— 格式跟正常走 bom-parse skill 產生的一模一樣,下游
bom-vendor-lookup 等步驟完全不需要改。不需要更動 mouser_lookup.py 的
parse_bom() 邏輯本身,也不透過任何中間 .xlsx(理由見 build_rows() 的
docstring:pandas 讀寫 Excel 對純數字字串欄位有前導 0 丟失的問題)。

適用判斷依據:原始 BOM 的欄位標題命中 `adapters/supplier_profiles.py` 裡
某個已知 profile 的標記欄位(目前只有一個 profile:
`bracket_labeled_spec`,標記是「规格型号」/「規格型號」欄位)。如果原始
BOM 本來就是分開的欄位(像名称/型号/厂家分開,或英文 MFG_NAME/MFG_PN/
Reference),不需要用這支腳本,直接跑 bom-parse skill 就好。

**這支腳本本身不寫死任何特定供應商的排版規則**——欄位標題候選清單、
品牌/備注標籤的 regex 都定義在 `adapters/supplier_profiles.py`。合作到新
供應商、格式又是「一格塞多屬性」時,去那份檔案加一個新的 SupplierProfile
就好,不用改這支腳本(那份檔案開頭有寫新增流程)。

使用方式(取代 bom-parse skill 的 run.py,直接產生 parsed_bom.json):
    python adapters/normalize_compound_spec_bom.py <原始BOM.xlsx> \
        [-o parsed_bom.json] [--debug-xlsx normalized_debug.xlsx] [--profile bracket_labeled_spec]

    不指定 -o 時,跟 bom-parse 一樣自動建立「今天日期_BOM檔名」資料夾,
    parsed_bom.json 存到裡面(見 ml.default_run_folder)。

`--profile` 平常不用填,腳本會自動掃描前幾列標題去比對
`supplier_profiles.SUPPLIER_PROFILES` 猜是哪家供應商的格式;只有在自動
偵測失敗、或你想強制用某個 profile 除錯時才需要手動指定。

之後接著跑 bom-vendor-lookup / bom-match-score / bom-match-review /
bom-report-finalize,跟正常流程完全一樣。

限制與已知不完美之處(拆解用的是規則式 regex,不是 100% 準確),尤其:
  - 完全沒有「品牌」標籤、也沒有 EIA 封裝代碼(0402/0603/...)可辨識的自由
    文字(例如純連接器描述「8P8C 90度 无灯带屏蔽」),value/package 可能
    抓不到,mpn 會是空的 —— 這類料件會在後續 vendor-lookup 用 keyword
    fallback 查詢,查不到就會落成 missing_mpn / no_candidates,屬於預期
    行為,不是腳本錯誤。
  - 拆出來的 mpn/manufacturer/package/value/tolerance 建議跑完
    bom-parse 後抽樣檢查幾筆,尤其型號帶有特殊符號(斜線、頓號)的料件。
"""

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path

import openpyxl
import pandas as pd

# mouser_lookup.py 在專案根目錄,supplier_profiles.py 跟這支腳本同目錄。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mouser_lookup as ml  # noqa: E402
import supplier_profiles as sp  # noqa: E402

# 這支腳本印出的 profile 描述含簡體字(「名称描述」「规格型号」)。Windows 主控台
# 預設編碼(cp950 等)印不出來時會直接拋 UnicodeEncodeError 讓整支腳本掛掉——
# 不是印成亂碼而已,是真的中斷在還沒寫出 parsed_bom.json 之前。強制 stdout
# 走 UTF-8 就不會有這個問題。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# 品牌/備注標籤的 regex(全形/半形括號、冒號可有可無)是「供應商排版習慣」,
# 因供應商而異,已經搬進 supplier_profiles.py 的 SupplierProfile.brand_re /
# remark_re,不在這裡寫死。下面留著的都是跟供應商排版無關的通用 regex
# (封裝代碼、公差、數值格式),所有 profile 共用。
_TOLERANCE_RE = re.compile(r"±\s*[\d.]+\s*(?:%|ppm)", re.IGNORECASE)
# 常見 EIA 被動元件封裝代碼 + 常見晶振/模組封裝代碼
_PACKAGE_RE = re.compile(
    r"\b(0201|0402|0603|0805|1206|1210|1808|2010|2512"
    r"|1612|2016|2520|3215|3225|5032|6035|7050)\b"
)
# 抓「數字+單位」的粗略 value pattern,例如 10PF、4.7K、100mΩ、50MHz
_VALUE_RE = re.compile(r"[\d.]+\s*[a-zA-ZΩµμ]+")

# 判斷「品牌標籤外的那段文字」裡,單一 token 是不是「規格數值」而不是型號本身。
# 用於晶振這類品項:主文字常常是「頻率 封裝 誤差」三個 token 並列(例如
# "48MHz 3225 ±30ppm"),不是型號 —— 真正型號反而寫在備注欄裡。
_FREQ_UNIT_TOKEN_RE = re.compile(r"^[\d.]+\s*(?:MHz|KHz|GHz|Hz|ppm)$", re.IGNORECASE)
_BARE_UNIT_RE = re.compile(r"^(?:MHz|KHz|GHz|Hz|ppm)$", re.IGNORECASE)
_DECIMAL_NUMBER_RE = re.compile(r"^[\d.]+$")


def _is_spec_token(tok: str) -> bool:
    return bool(
        _PACKAGE_RE.fullmatch(tok)
        or _DECIMAL_NUMBER_RE.fullmatch(tok)
        or "±" in tok or tok.endswith("%")
        or _FREQ_UNIT_TOKEN_RE.match(tok)
        or _BARE_UNIT_RE.match(tok)
    )


def split_spec(spec: str, category: str, profile) -> dict:
    """
    拆解「规格型号」欄位文字,回傳 dict:
        mpn / manufacturer / package / value / tolerance / remark
    只有偵測到「品牌」標籤時才會填 mpn/manufacturer(比較有把握是真的型號);
    沒有「品牌」標籤的一律當被動元件自由格式規格文字處理,不猜 mpn。

    品牌/備注標籤怎麼認(profile.brand_re / profile.remark_re)因供應商而
    異,由呼叫端傳入的 profile 決定;拆解完之後怎麼判斷哪個 token 是型號、
    哪個是規格數值,是通用邏輯,所有 profile 共用,不放進 profile 裡。
    """
    text = (spec or "").strip()
    result = {"mpn": "", "manufacturer": "", "package": "", "value": "", "tolerance": "", "remark": ""}
    if not text:
        return result

    brand_re = profile.brand_re
    remark_re = profile.remark_re
    m_brand = brand_re.search(text)
    m_remark = remark_re.search(text)
    remark_text = m_remark.group("remark").strip() if m_remark else ""
    result["remark"] = remark_text

    if m_brand:
        result["manufacturer"] = m_brand.group("mfr").strip()
        mpn_candidate = brand_re.sub("", text)
        mpn_candidate = remark_re.sub("", mpn_candidate).strip(" 　,，")

        tokens = mpn_candidate.split()
        if len(tokens) <= 1:
            # 常見情況:主文字就是型號本身(例如 "STM32F030CCT6")
            result["mpn"] = mpn_candidate
        else:
            mpn_like = [t for t in tokens if not _is_spec_token(t)]
            spec_like = [t for t in tokens if _is_spec_token(t)]
            if len(mpn_like) == 1:
                # 例如 "U8528LF 38.88MHz" -> 型號是 U8528LF,38.88MHz 是頻率
                # 數值,不該黏進型號裡,移到 value 供 keyword fallback 用
                result["mpn"] = mpn_like[0]
                result["value"] = " ".join(spec_like)
            elif len(mpn_like) == 0:
                # 例如 "48MHz 3225 ±30ppm" -> 整段都是規格數值,不是型號。
                # 這種情況常見於晶振類:真正型號被寫在備注欄裡而不是主文字,
                # 用「備注是單一 token 且看起來像型號(英數混合、無空白、
                # 夠長)」當判斷依據。
                if remark_text and " " not in remark_text and len(remark_text) >= 6:
                    result["mpn"] = remark_text
                result["value"] = " ".join(spec_like)
            else:
                # 無法判斷哪個 token 才是型號,保守地整段當型號(維持舊行為)
                result["mpn"] = mpn_candidate
        search_text = remark_text or text
    else:
        # 沒有品牌標籤 -> 視為被動元件自由格式規格文字(即使有備注標籤,
        # 備注內容通常是「暫無」之類,不代表這段文字就是型號)
        search_text = text

    tol_m = _TOLERANCE_RE.search(search_text)
    if tol_m:
        result["tolerance"] = tol_m.group(0)

    pkg_m = _PACKAGE_RE.search(search_text) or _PACKAGE_RE.search(category or "")
    if pkg_m:
        result["package"] = pkg_m.group(0)

    if not m_brand:
        val_m = _VALUE_RE.search(text)
        if val_m:
            result["value"] = val_m.group(0)

    return result


def find_header_row(path: Path, max_scan: int = 8, profile_id: str | None = None):
    """
    掃描前幾列,找出標題列在哪一列、對應哪個 supplier profile。

    profile_id 指定時只比對該 profile 的標記欄位(強制模式,跳過自動判斷);
    否則依序比對 supplier_profiles.SUPPLIER_PROFILES 裡每一個已知 profile,
    回傳第一個命中的。回傳 (header_row_index, SupplierProfile)。
    """
    candidates = [sp.get_profile(profile_id)] if profile_id else list(sp.SUPPLIER_PROFILES)
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    seen_columns: set[str] = set()
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan, values_only=True)):
        cells = {str(c).strip() for c in row if c is not None}
        seen_columns |= cells
        for profile in candidates:
            if any(marker in cells for marker in profile.detect_columns):
                return i, profile
    known = ", ".join(p.id for p in candidates)
    raise ValueError(
        f"在前 {max_scan} 列都找不到任何已知 supplier profile 的標記欄位"
        f"(已比對:{known})。可能是新供應商的格式——參考 "
        f"adapters/supplier_profiles.py 開頭的說明新增一個 profile,或者這份"
        f"BOM 其實欄位是分開的一般格式,不需要這支 adapter,直接用 bom-parse "
        f"skill 即可。掃描到的欄位標題:{sorted(seen_columns)}"
    )


def build_rows(in_path: str, profile_id: str | None = None) -> list[dict]:
    """
    讀原始 BOM,回傳 list of dict,欄位鍵名直接用 mouser_lookup._COLUMN_ALIASES
    認得的標準別名(名称/型号/厂家/位置/数量/值/封装/公差/特性参数),可以
    直接餵給 ml.parse_bom() —— 不透過任何中間 .xlsx 檔案。

    這樣做是刻意的:如果先把拆解結果寫成 .xlsx 再讓 bom-parse 重新讀一次,
    pandas/openpyxl 對「整欄都是純數字字串」的欄位(例如封裝代碼 "0402")
    會自動轉型成數字,讀回來變成 402、丟失前導 0(可用
    `pd.DataFrame({"x": ["0402"]}).to_excel(...)` 再 `pd.read_excel()` 重現
    這個問題,跟這支腳本自己寫入時用的格式無關,是 pandas 讀取階段的型別
    推斷)。直接在記憶體裡把 dict 傳給 parse_bom(),完全不繞經 Excel 數值
    型別推斷,就不會有這個問題。
    """
    p = Path(in_path)
    header_row, profile = find_header_row(p, profile_id=profile_id)
    print(f"[normalize] 偵測到標題列在第 {header_row + 1} 列,使用 supplier profile: "
          f"{profile.id}({profile.description})")
    df = pd.read_excel(p, header=header_row)
    df = df.dropna(how="all")

    def col(field: str) -> str:
        for n in profile.columns[field]:
            if n in df.columns:
                return n
        raise KeyError(
            f"profile {profile.id!r} 找不到欄位 {field}"
            f"(候選:{profile.columns[field]}),實際欄位: {list(df.columns)}"
        )

    c_category = col("category")
    c_spec = col("spec")
    c_refdes = col("ref_des")
    c_qty = col("qty")

    rows = []
    for _, row in df.iterrows():
        category = str(row.get(c_category, "") or "").strip()
        spec = str(row.get(c_spec, "") or "").strip()
        refdes = row.get(c_refdes, "")
        refdes = "" if pd.isna(refdes) else str(refdes).strip()
        qty = row.get(c_qty, 1)
        try:
            qty = int(float(qty))
        except (ValueError, TypeError):
            qty = 1

        parts = split_spec(spec, category, profile)

        rows.append({
            "名称": category,
            "型号": parts["mpn"],
            "厂家": parts["manufacturer"],
            "位置": refdes,
            "数量": qty,
            "值": parts["value"],
            "封装": parts["package"],
            "公差": parts["tolerance"],
            "特性参数": parts["remark"],
            "原始规格型号": spec,  # 不在 _COLUMN_ALIASES 裡,parse_bom 會忽略;保留供除錯
        })
    return rows


def write_debug_xlsx(rows: list[dict], out_path: str) -> None:
    """
    把 build_rows() 的結果另存一份 .xlsx,方便人工核對拆解結果對不對。
    只給人看、用 Excel 開 —— 不要再讓 pandas 讀回這個檔案(見 build_rows()
    docstring 提到的前導 0 問題,只在「pandas 又讀一次」時才會發生;人眼
    在 Excel 裡開,儲存格本身仍是正確的文字型別,不會有問題)。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    if not rows:
        wb.save(out_path)
        return
    columns = list(rows[0].keys())
    ws.append(columns)
    for r in rows:
        ws.append([r[c] for c in columns])
    for col_idx, col_name in enumerate(columns, start=1):
        if col_name != "数量":
            letter = openpyxl.utils.get_column_letter(col_idx)
            for cell in ws[letter][1:]:
                cell.number_format = "@"
    wb.save(out_path)


def run(
    bom_path: str,
    out_json_path: str = None,
    debug_xlsx_path: str | None = None,
    profile_id: str | None = None,
) -> dict:
    """
    對應 bom-parse skill 的 run.py,但用 build_rows() 取代 ml.load_bom_file(),
    其餘(parse_bom / filter_non_purchasable / 輸出 JSON schema)完全共用,
    產出的 parsed_bom.json 跟正常走 bom-parse 產生的格式一模一樣,下游
    bom-vendor-lookup 等步驟不需要知道這份 BOM 是走哪條路徑解析的——包含
    不指定 out_json_path 時,同樣用 ml.default_run_folder() 建「今天日期_
    來源 BOM 檔名」資料夾的預設行為,跟 bom-parse 一致。

    profile_id 不填時自動偵測(見 find_header_row());填了就強制用該
    profile,略過自動判斷。
    """
    if out_json_path is None:
        folder = ml.default_run_folder(bom_path)
        out_json_path = str(folder / "parsed_bom.json")

    rows = build_rows(bom_path, profile_id=profile_id)
    if debug_xlsx_path:
        write_debug_xlsx(rows, debug_xlsx_path)

    bom_lines = ml.parse_bom(rows)
    purchasable, excluded = ml.filter_non_purchasable(bom_lines)
    excluded_ids = {id(l) for l in excluded}

    lines_payload = [
        {**dataclasses.asdict(line), "excluded_non_purchasable": id(line) in excluded_ids}
        for line in bom_lines
    ]
    payload = {
        "source_bom": str(Path(bom_path).resolve()),
        "total_lines": len(lines_payload),
        "purchasable_count": len(purchasable),
        "excluded_count": len(excluded),
        "lines": lines_payload,
    }
    Path(out_json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    n_mpn = sum(1 for l in bom_lines if l.mpn.strip())
    print(f"[normalize] 共 {len(bom_lines)} 列,{len(excluded)} 列非採購項被濾掉,"
          f"其中 {n_mpn} 列拆出明確 MPN,{len(bom_lines) - n_mpn} 列為自由格式規格"
          f"(多半是被動元件,交給 keyword fallback 查詢)")
    print(f"[normalize] 已輸出: {out_json_path}")
    if debug_xlsx_path:
        print(f"[normalize] 除錯用正規化表格(僅供人工核對,不要再餵回 bom-parse): {debug_xlsx_path}")
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bom_path", nargs="?", help="原始 BOM 檔案路徑(.xlsx)")
    ap.add_argument("-o", "--out", default=None,
                     help="輸出 parsed_bom.json 路徑;不指定則自動建立「今天日期_BOM檔名」資料夾,存到裡面")
    ap.add_argument("--debug-xlsx", default=None, help="額外輸出一份正規化後的 .xlsx 供人工核對(選填)")
    ap.add_argument(
        "--profile", default=None,
        help="強制指定 supplier profile id(平常不用填,會自動偵測)。"
             "可用值見 --list-profiles。",
    )
    ap.add_argument(
        "--list-profiles", action="store_true",
        help="列出 adapters/supplier_profiles.py 裡已知的 supplier profile 後結束",
    )
    args = ap.parse_args()

    if args.list_profiles:
        for profile in sp.SUPPLIER_PROFILES:
            print(f"{profile.id}: {profile.description}")
        return

    if not args.bom_path:
        ap.error("bom_path 未指定(--list-profiles 以外的用法都需要這個參數)")

    run(args.bom_path, args.out, debug_xlsx_path=args.debug_xlsx, profile_id=args.profile)


if __name__ == "__main__":
    main()
