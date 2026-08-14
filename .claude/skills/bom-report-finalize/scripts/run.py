"""
節點 D:合併節點 A/B/C(以及選用的 bom-match-review 語意判斷結果)產出最終 xlsx。
純本機、純腳本,不打任何外部 API。

用法:
    python run.py <parsed_bom.json> <vendor_candidates.json> <match_scores.json> \
        [-o report.xlsx] [--review review_verdicts.json]

review_verdicts.json(選用,bom-match-review skill 產出)格式:
    {
      "verdicts": [
        {"lookup_key": "...", "verdict": "CONFIRM"|"REJECT"|"UNCERTAIN", "reasoning": "..."},
        ...
      ]
    }

    CONFIRM -> match_status 覆寫成 "matched_by_claude"
    REJECT  -> match_status 覆寫成 "rejected_by_claude"(明確標記「Claude 認為這是
               錯誤配對」,跟「還沒判斷過」的 needs_human_review 分開)
    UNCERTAIN -> match_status 維持不變,但 claude_verdict/claude_reasoning 照樣
                 寫進報表,讓人工複核時看得到 Claude 已經想過但沒把握。

    沒有 --review 參數時,單純用節點 C 的規則式結果出報表,不受影響——這個
    步驟本來就該獨立能用。

    輸出檔名:不指定 -o 的話,預設寫進「輸入的 parsed_bom.json 所在的
    資料夾」,檔名用該資料夾的名字(bom-parse 一開始就用「今天日期_來源
    BOM 檔名」規則建好的,例如 20260814_bom_2024Q1_projectX/)+「_報價.xlsx」
    ——這樣每份 BOM 從第一步到最終報表都收在同一個資料夾,檔名也跟來源
    對得上、知道是哪天出的。如果算出來的檔名已經存在且寫入時被鎖住(常見
    於使用者在 Excel 裡開著同名舊報表),會自動改用版本化檔名(`_v2`、
    `_v3`...)重試,不會要求使用者先關檔案。

    除了 xlsx,預設還會在同一個路徑(同檔名、副檔名改 .html)輸出一份
    視覺化摘要網頁(給人看的 infographic,不是給下游步驟讀的中繼檔)——
    直接吃同一份 report/summary,數字保證跟 xlsx 一致,不用另外手動做。
    不想要的話加 `--no-html`。
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

_VALID_VERDICTS = {"CONFIRM", "REJECT", "UNCERTAIN"}
_VERDICT_TO_STATUS = {"CONFIRM": "matched_by_claude", "REJECT": "rejected_by_claude"}


def _default_out_path(parsed_bom_path: str) -> str:
    """
    沒指定 -o 時的預設輸出路徑:xlsx 跟 HTML(write_html_report 那邊用
    .with_suffix() 算出同資料夾的路徑,見 run())都放進「輸入的
    parsed_bom.json 所在的資料夾」——這個資料夾在 pipeline 一開始
    (bom-parse)就已經用「今天日期_來源 BOM 檔名」規則建好了(見
    mouser_lookup.default_run_folder),這裡直接沿用該資料夾的名稱當
    xlsx 檔名前綴,不重新用「今天」算一次——如果 parse 是昨天做的、
    report-finalize 是今天重跑,重算「今天日期」會跟資料夾原本的名字對
    不起來,變成同一個 run 卻散在兩個不同名稱的地方。

    同一份來源 BOM 重跑這一步(例如改完 manufacturer_aliases.md 重算
    分數、或套用新的 review 判決),會重用同一個資料夾、直接覆蓋裡面的
    xlsx/html——這是預期行為,「重跑這一步很便宜、隨時可以重跑」是這整條
    pipeline 的設計原則。xlsx 本身若被使用者在 Excel 裡開著鎖住,
    `_write_with_retry()` 仍會在檔名層級加 `_v2`/`_v3` 重試,不受這裡
    的資料夾邏輯影響。

    使用者用 -o 明確指定路徑時完全不套用這個資料夾邏輯,尊重使用者自己
    選的位置。相容舊用法:如果 parsed_bom.json 是直接放在專案根目錄
    (不是放在 run 資料夾裡,例如手動用 -o 指定過路徑的舊產物),
    folder.name 會是空字串或 "."——這種情況直接退回專案根目錄輸出,
    不會產生奇怪的檔名。
    """
    folder = Path(parsed_bom_path).resolve().parent
    base_name = folder.name or Path(parsed_bom_path).stem

    return str(folder / f"{base_name}_報價.xlsx")


def _write_with_retry(report: list[dict], out_path: str) -> tuple[dict, str]:
    """
    輸出檔名被鎖住(PermissionError,通常是使用者在 Excel 裡開著同名舊報表)
    時,自動改用版本化檔名(_v2、_v3...)重試,不要求使用者先關檔案。
    """
    p = Path(out_path)
    stem, suffix = p.stem, p.suffix
    attempt_path = out_path
    version = 1
    while True:
        try:
            summary = ml.write_report_xlsx(report, attempt_path)
            return summary, attempt_path
        except PermissionError:
            version += 1
            attempt_path = str(p.with_name(f"{stem}_v{version}{suffix}"))
            print(f"  [warn] 輸出檔案被鎖住(可能在 Excel 裡開著),改用 {attempt_path} 重試")


def run(
    parsed_bom_path: str,
    vendor_candidates_path: str,
    match_scores_path: str,
    out_path: str = None,
    review_path: str = None,
    write_html: bool = True,
) -> dict:
    bom_data = json.loads(Path(parsed_bom_path).read_text(encoding="utf-8"))
    vendor_data = json.loads(Path(vendor_candidates_path).read_text(encoding="utf-8"))
    match_data = json.loads(Path(match_scores_path).read_text(encoding="utf-8"))

    if out_path is None:
        out_path = _default_out_path(parsed_bom_path)

    all_lines = [
        ml.BOMLine(**{k: v for k, v in item.items() if k != "excluded_non_purchasable"})
        for item in bom_data["lines"]
    ]
    excluded_lines = [
        line for line, item in zip(all_lines, bom_data["lines"])
        if item["excluded_non_purchasable"]
    ]

    vendor_results = vendor_data["results"]
    matches = {k: ml.MatchResult(**v) for k, v in match_data["matches"].items()}

    if review_path:
        review_data = json.loads(Path(review_path).read_text(encoding="utf-8"))
        applied = 0
        skipped = 0
        for verdict in review_data.get("verdicts", []):
            key = verdict.get("lookup_key")
            v = verdict.get("verdict")
            if v not in _VALID_VERDICTS:
                print(f"  [warn] 略過不合法的 verdict: {verdict!r}")
                skipped += 1
                continue
            if key not in matches:
                print(f"  [warn] review 裡的 lookup_key {key!r} 不在 match_scores.json 裡,略過")
                skipped += 1
                continue
            m = matches[key]
            m.claude_verdict = v
            m.claude_reasoning = verdict.get("reasoning")
            if v in _VERDICT_TO_STATUS:
                m.match_status = _VERDICT_TO_STATUS[v]
            applied += 1
        print(f"[bom-report-finalize] 套用 {applied} 筆 Claude 判決" +
              (f",{skipped} 筆略過" if skipped else ""))

    report = ml.build_report(all_lines, vendor_results, matches, excluded_lines=excluded_lines)
    summary, actual_out_path = _write_with_retry(report, out_path)
    summary["_out_path"] = actual_out_path

    if write_html:
        html_out_path = str(Path(actual_out_path).with_suffix(".html"))
        ml.write_html_report(report, summary, bom_data["source_bom"], html_out_path)
        summary["_html_out_path"] = html_out_path

    return summary


def main():
    parser = argparse.ArgumentParser(description="節點 D:合併結果輸出最終 xlsx")
    parser.add_argument("parsed_bom_json", help="bom-parse 產出的 parsed_bom.json")
    parser.add_argument("vendor_candidates_json", help="bom-vendor-lookup 產出的 vendor_candidates.json")
    parser.add_argument("match_scores_json", help="bom-match-score 產出的 match_scores.json")
    parser.add_argument("-o", "--out", default=None,
                         help="輸出 xlsx 路徑;不指定則用來源 BOM 檔名 + _報價.xlsx")
    parser.add_argument("--review", default=None, help="bom-match-review 產出的 review_verdicts.json(選用)")
    parser.add_argument("--no-html", action="store_true",
                         help="停用預設會一起輸出的 HTML 視覺化摘要網頁(只出 xlsx)")
    args = parser.parse_args()
    run(args.parsed_bom_json, args.vendor_candidates_json, args.match_scores_json,
        args.out, review_path=args.review, write_html=not args.no_html)


if __name__ == "__main__":
    main()
