"""
節點 B:對每個唯一 MPN/關鍵字查 Mouser,查無結果時 fallback 查 DigiKey。
整條 pipeline 裡**唯一**會打外部 API 的一步(Mouser/DigiKey 都是免費 API,
但仍然是對外連線,節流/快取都在這裡處理)。

用法:
    python run.py <parsed_bom.json> -o vendor_candidates.json
        [--limit N] [--no-cache] [--no-keyword-fallback] [--no-digikey-fallback]

輸出格式(vendor_candidates.json):
    {
      "results": {
        "<lookup_key>": [ <候選料件 dict>, ... ],
        ...
      }
    }

    lookup_key 是 MPN 本身,或是缺 MPN 時的 "__keyword__:xxx" 合成 key
    (跟 mouser_lookup.py 的 _lookup_key() 定義一致)。空 list 代表供應商
    明確回應查無此料件;key 完全不存在代表查詢失敗(網路問題),不是
    「查無結果」——下游步驟要能分辨這兩種狀態。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import mouser_lookup as ml
# 這支腳本的進度訊息全是中文。Windows 主控台預設編碼(cp950 等)印不出簡體字
# 或 emoji 時會直接拋 UnicodeEncodeError 讓整支腳本掛掉——不是印成亂碼而已,
# 是真的中斷。強制 stdout 走 UTF-8 就不會有這個問題。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def run(
    parsed_bom_path: str,
    out_path: str,
    limit: int = None,
    use_cache: bool = True,
    enable_keyword_fallback: bool = True,
    enable_digikey_fallback: bool = True,
) -> dict:
    api_key = ml._get_api_key()
    data = json.loads(Path(parsed_bom_path).read_text(encoding="utf-8"))

    purchasable = [
        ml.BOMLine(**{k: v for k, v in item.items() if k != "excluded_non_purchasable"})
        for item in data["lines"]
        if not item["excluded_non_purchasable"]
    ]
    if limit is not None:
        purchasable = purchasable[:limit]
        print(f"[bom-vendor-lookup] --limit 生效,只對前 {len(purchasable)} 筆料件呼叫 API")

    if enable_digikey_fallback and ml.DIGIKEY_ENABLED:
        print("[bom-vendor-lookup] DigiKey fallback 已啟用")
    elif enable_digikey_fallback and not ml.DIGIKEY_ENABLED:
        print("[bom-vendor-lookup] 未設定 DIGIKEY_CLIENT_ID/SECRET,DigiKey fallback 停用中")

    results = ml.lookup_all(
        purchasable, api_key=api_key, use_cache=use_cache,
        enable_keyword_fallback=enable_keyword_fallback,
        enable_digikey_fallback=enable_digikey_fallback,
    )

    payload = {"results": results}
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[bom-vendor-lookup] 共 {len(results)} 個唯一 key 有查詢結果 -> {out_path}")
    return payload


def main():
    parser = argparse.ArgumentParser(description="節點 B:Mouser/DigiKey 查詢")
    parser.add_argument("parsed_bom_json", help="bom-parse 產出的 parsed_bom.json")
    parser.add_argument("-o", "--out", default="vendor_candidates.json", help="輸出 JSON 路徑")
    parser.add_argument("--limit", type=int, default=None, help="只查前 N 筆(小批量測試用)")
    parser.add_argument("--no-cache", action="store_true", help="停用本機查詢快取")
    parser.add_argument("--no-keyword-fallback", action="store_true", help="停用 Mouser keyword 搜尋")
    parser.add_argument("--no-digikey-fallback", action="store_true", help="停用 DigiKey fallback")
    args = parser.parse_args()
    run(
        args.parsed_bom_json, args.out,
        limit=args.limit,
        use_cache=not args.no_cache,
        enable_keyword_fallback=not args.no_keyword_fallback,
        enable_digikey_fallback=not args.no_digikey_fallback,
    )


if __name__ == "__main__":
    main()
