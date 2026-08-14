"""
節點 A:讀 BOM 檔案,標準化欄位,濾掉 PCB/包材等非採購項。
純本機、純腳本,不打任何外部 API。

用法:
    python run.py <bom_path> [-o parsed_bom.json] [--header-row N]

    不指定 -o 時,會自動建立「今天日期_BOM檔名」資料夾(例如
    20260814_bom_2024Q1_projectX/),parsed_bom.json 寫進裡面——下游
    節點 B/C/D 預設也會沿用同一個資料夾,讓一次 pipeline run 的所有中繼
    檔跟最終報表都收在一起,不同 BOM(=不同 session)不會共用檔名互相
    覆蓋。手動指定 -o 時完全尊重使用者選的路徑,不套用這個資料夾邏輯。

輸出格式(parsed_bom.json):
    {
      "source_bom": "<原始檔案路徑>",
      "lines": [
        {..BOMLine 的所有欄位.., "excluded_non_purchasable": false},
        ...
      ]
    }

    "lines" 保留原始 BOM 檔案的列順序(不管有沒有被濾掉),下游步驟用
    "excluded_non_purchasable" 這個旗標分辨哪些要送去查價、哪些不用。
"""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

# mouser_lookup.py 在專案根目錄,不在這個 skill 資料夾裡,把它加進 import 路徑。
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import mouser_lookup as ml

# 這支腳本的進度訊息全是中文。Windows 主控台預設編碼(cp950 等)印不出簡體字
# 或 emoji 時會直接拋 UnicodeEncodeError 讓整支腳本掛掉——不是印成亂碼而已,
# 是真的中斷。強制 stdout 走 UTF-8 就不會有這個問題。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def run(bom_path: str, out_path: str = None, header_row: int = None) -> dict:
    if out_path is None:
        folder = ml.default_run_folder(bom_path)
        out_path = str(folder / "parsed_bom.json")

    rows = ml.load_bom_file(bom_path, header_row=header_row)
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
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[bom-parse] 共 {len(lines_payload)} 列,濾掉 {len(excluded)} 列非採購項,"
          f"{len(purchasable)} 列待查價 -> {out_path}")
    return payload


def main():
    parser = argparse.ArgumentParser(description="節點 A:BOM 解析")
    parser.add_argument("bom_path", help="BOM 檔案路徑(.xlsx / .csv)")
    parser.add_argument("-o", "--out", default=None,
                         help="輸出 JSON 路徑;不指定則自動建立「今天日期_BOM檔名」資料夾,存到裡面的 parsed_bom.json")
    parser.add_argument("--header-row", type=int, default=None,
                         help="手動指定標題列(0-indexed),不指定則自動偵測")
    args = parser.parse_args()
    run(args.bom_path, args.out, header_row=args.header_row)


if __name__ == "__main__":
    main()
