"""
節點 C(規則式部分):對每個查到候選的料件,套用規則式評分
(廠商相似度 + 描述 token 命中率 + MPN 精確相符加成 + manufacturer_aliases.md
縮寫對照),產出信心分數與 matched/needs_human_review/no_candidates 判定。
純本機、純腳本,不打任何外部 API,也不叫任何 LLM。

用法:
    python run.py <parsed_bom.json> <vendor_candidates.json> [-o match_scores.json]

    不指定 -o 時,預設寫進「輸入的 parsed_bom.json 所在的資料夾」,跟同一
    次 pipeline run 的其他中繼檔收在一起。手動指定 -o 時完全尊重使用者
    選的路徑。

輸出格式(match_scores.json):
    {
      "matches": {
        "<lookup_key>": {
          "mpn": "...", "match_status": "matched"|"needs_human_review"|"no_candidates",
          "confidence": 0.0-1.0, "matched_part": {...}|null,
          "candidates_considered": N,
          "claude_verdict": null, "claude_reasoning": null   // 留給 bom-match-review 填
        },
        ...
      }
    }

    key 不在 vendor_candidates.json 裡的料件(查詢失敗)不會出現在這裡——
    這是刻意的,下游 bom-report-finalize 步驟看 vendor_candidates.json 有沒有
    這個 key 就能分辨 query_failed 跟 no_candidates。
"""

import argparse
import dataclasses
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


def run(parsed_bom_path: str, vendor_candidates_path: str, out_path: str = None) -> dict:
    if out_path is None:
        out_path = str(Path(parsed_bom_path).resolve().parent / "match_scores.json")

    bom_data = json.loads(Path(parsed_bom_path).read_text(encoding="utf-8"))
    vendor_data = json.loads(Path(vendor_candidates_path).read_text(encoding="utf-8"))

    purchasable = [
        ml.BOMLine(**{k: v for k, v in item.items() if k != "excluded_non_purchasable"})
        for item in bom_data["lines"]
        if not item["excluded_non_purchasable"]
    ]
    vendor_results = vendor_data["results"]

    matches = ml.match_validation_all(purchasable, vendor_results)

    payload = {"matches": {k: dataclasses.asdict(v) for k, v in matches.items()}}
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = {}
    for m in matches.values():
        counts[m.match_status] = counts.get(m.match_status, 0) + 1
    print(f"[bom-match-score] 共 {len(matches)} 筆評分完成 -> {out_path}")
    print(f"  狀態分布: {counts}")
    return payload


def main():
    parser = argparse.ArgumentParser(description="節點 C(規則式):比對信心分數")
    parser.add_argument("parsed_bom_json", help="bom-parse 產出的 parsed_bom.json")
    parser.add_argument("vendor_candidates_json", help="bom-vendor-lookup 產出的 vendor_candidates.json")
    parser.add_argument("-o", "--out", default=None,
                         help="輸出 JSON 路徑;不指定則存到 parsed_bom.json 所在的資料夾")
    args = parser.parse_args()
    run(args.parsed_bom_json, args.vendor_candidates_json, args.out)


if __name__ == "__main__":
    main()
