"""
本機自測腳本(不呼叫真實 Mouser API,不需要 MOUSER_SEARCH_API_KEY)。
用來驗證節點 A(欄位映射)/ C(比對信心分數)/ D(xlsx 輸出)的邏輯是否正確。

執行方式:
    python test_pipeline.py
"""

import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

import mouser_lookup as ml

# Windows 終端機預設編碼(cp950/cp1252 等)可能無法印出 emoji,強制 stdout 用 UTF-8。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def test_parse_bom_column_mapping():
    # 模擬一份實際 BOM 檔案,欄位命名故意跟骨架程式碼原本猜測的不完全一樣,
    # 用來驗證別名對照表可以正確辨識。
    rows = [
        {
            "Reference Designator": "R1,R2",
            "Manufacturer Part Number": "RC0603FR-0710KL",
            "Mfr": "Yageo",
            "Quantity": "2",
            "Description": "RES SMD 10K OHM 1% 1/10W 0603",
            "Value": "10K",
            "Package/Case": "0603",
            "Tolerance": "1%",
            "Unit Cost": "0.008",
        },
        {
            "Reference Designator": "U1",
            "Manufacturer Part Number": "ATMEGA328P-AU",
            "Mfr": "Microchip",
            "Quantity": 1,
            "Description": "IC MCU 8BIT 32KB FLASH 32TQFP",
            "Value": "",
            "Package/Case": "TQFP-32",
            "Tolerance": "",
            "Unit Cost": "2.10",
        },
    ]
    bom_lines = ml.parse_bom(rows)
    assert len(bom_lines) == 2
    assert bom_lines[0].mpn == "RC0603FR-0710KL"
    assert bom_lines[0].manufacturer == "Yageo"
    assert bom_lines[0].qty_per_unit == 2
    assert bom_lines[0].bom_unit_price == 0.008
    assert bom_lines[1].mpn == "ATMEGA328P-AU"
    assert bom_lines[1].bom_unit_price == 2.10
    print("[OK] test_parse_bom_column_mapping")
    return bom_lines


def test_match_validation():
    bom_lines = test_parse_bom_column_mapping()
    resistor, mcu = bom_lines

    # 案例 1: 廠商 + 描述都高度相符 -> 應該 matched,信心分數高
    good_candidates = [
        {
            "ManufacturerPartNumber": "RC0603FR-0710KL",
            "Manufacturer": "Yageo",
            "Description": "RES SMD 10K OHM 1% 1/10W 0603",
            "PriceBreaks": [
                {"Quantity": 1, "Price": "$0.0090", "Currency": "USD"},
                {"Quantity": 10, "Price": "$0.0075", "Currency": "USD"},
                {"Quantity": 100, "Price": "$0.0060", "Currency": "USD"},
            ],
            "Min": "1",
            "Mult": "1",
        },
        {
            "ManufacturerPartNumber": "RC0603FR-0710KL-XYZ",  # 干擾候選,不應被選中
            "Manufacturer": "SomeOtherVendor",
            "Description": "unrelated part",
            "PriceBreaks": [{"Quantity": 1, "Price": "$5.00", "Currency": "USD"}],
            "Min": "1",
            "Mult": "1",
        },
    ]
    result = ml.match_validation(resistor, good_candidates)
    assert result.match_status == "matched", result
    assert result.confidence >= ml.MATCH_CONFIDENCE_THRESHOLD
    assert result.matched_part["Manufacturer"] == "Yageo"
    print(f"[OK] passive component matched, confidence={result.confidence}")

    # 案例 2: 廠商不符 + 描述完全不相關 -> 應該 needs_human_review
    bad_candidates = [
        {
            "ManufacturerPartNumber": "ATMEGA328P-AU",
            "Manufacturer": "TotallyDifferentCorp",
            "Description": "completely unrelated widget",
            "PriceBreaks": [{"Quantity": 1, "Price": "$9.99", "Currency": "USD"}],
            "Min": "1",
            "Mult": "1",
        }
    ]
    result2 = ml.match_validation(mcu, bad_candidates)
    assert result2.match_status == "needs_human_review", result2
    assert result2.confidence < ml.MATCH_CONFIDENCE_THRESHOLD
    print(f"[OK] mismatched part flagged for review, confidence={result2.confidence}")

    # 案例 3: 沒有任何候選 -> no_candidates
    result3 = ml.match_validation(mcu, [])
    assert result3.match_status == "no_candidates"
    print("[OK] empty candidates -> no_candidates")

    return bom_lines, {resistor.mpn: good_candidates, mcu.mpn: bad_candidates}


def test_match_validation_price_tie_break():
    # 同一個 MPN 有兩筆候選,分數只差在描述字面相似度,但分數較高的那筆
    # 缺報價(PriceBreaks 空)。分數差距在 MATCH_PRICE_TIE_MARGIN(預設 0.1)
    # 以內時,應該優先選有報價的那筆——不要因為描述字面比較像就選中一個
    # 查得到料、卻查無報價的候選。
    line = ml.BOMLine(
        ref_des="D1", mpn="ZD-TEST", manufacturer="UN", qty_per_unit=1,
        description="zener diode", value="ZD-TEST", package="SOD-323",
    )
    priceless_better_desc = {
        "ManufacturerPartNumber": "ZD-TEST",
        "Manufacturer": "VendorA",
        "Description": "zener diode sod",  # 描述字面比對分數較高
        "PriceBreaks": [],
        "Min": "1",
        "Mult": "1",
    }
    priced_slightly_lower = {
        "ManufacturerPartNumber": "ZD-TEST",
        "Manufacturer": "VendorB",
        "Description": "zener diode",  # 描述分數略低(差距約 0.07,在容許範圍內)
        "PriceBreaks": [{"Quantity": 1, "Price": "$0.10", "Currency": "USD"}],
        "Min": "1",
        "Mult": "1",
    }
    result = ml.match_validation(line, [priceless_better_desc, priced_slightly_lower])
    assert result.matched_part["Manufacturer"] == "VendorB", result.matched_part
    assert result.matched_part["PriceBreaks"], "應該選到有報價的候選"
    print(f"[OK] price tie-break prefers priced candidate, confidence={result.confidence}")

    # 分數差距明顯超過容許範圍時,即使另一筆有報價,還是要尊重分數較高的
    # 那個——不是「有報價就無條件優先」。
    clearly_better = {
        "ManufacturerPartNumber": "ZD-TEST",
        "Manufacturer": "UN",  # 廠商完全相符,分數明顯領先
        "Description": "zener diode SOD-323",
        "PriceBreaks": [],
        "Min": "1",
        "Mult": "1",
    }
    clearly_worse_but_priced = {
        "ManufacturerPartNumber": "totally-different-part",
        "Manufacturer": "SomeOtherCorp",
        "Description": "unrelated widget",
        "PriceBreaks": [{"Quantity": 1, "Price": "$1.00", "Currency": "USD"}],
        "Min": "1",
        "Mult": "1",
    }
    result2 = ml.match_validation(line, [clearly_better, clearly_worse_but_priced])
    assert result2.matched_part is clearly_better, result2.matched_part
    print("[OK] price tie-break does not override a clearly higher score")


def test_price_break_selection():
    price_breaks = [
        {"Quantity": 1, "Price": "$0.0090", "Currency": "USD"},
        {"Quantity": 10, "Price": "$0.0075", "Currency": "USD"},
        {"Quantity": 100, "Price": "$0.0060", "Currency": "USD"},
    ]
    assert ml._select_price_for_qty(price_breaks, 1) == (0.0090, "USD")
    assert ml._select_price_for_qty(price_breaks, 5) == (0.0090, "USD")
    assert ml._select_price_for_qty(price_breaks, 10) == (0.0075, "USD")
    assert ml._select_price_for_qty(price_breaks, 500) == (0.0060, "USD")
    assert ml._select_price_for_qty([], 5) == (None, None)
    print("[OK] test_price_break_selection")


def test_build_report_and_xlsx_output():
    bom_lines, mouser_results = test_match_validation()
    matches = ml.match_validation_all(bom_lines, mouser_results)
    report = ml.build_report(bom_lines, mouser_results, matches)

    assert len(report) == 2
    r_resistor = next(r for r in report if r["mpn"] == "RC0603FR-0710KL")
    assert r_resistor["match_status"] == "matched"
    assert r_resistor["mouser_unit_price"] == 0.009
    assert r_resistor["bom_unit_price"] == 0.008
    assert r_resistor["price_delta_pct"] is not None
    print(f"[OK] report row: {r_resistor}")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "test_report.xlsx"
        summary = ml.write_report_xlsx(report, str(out_path))
        assert out_path.exists()

        df = pd.read_excel(out_path, sheet_name="BOM_vs_Vendors")
        assert list(df.columns) == ml._REPORT_COLUMNS
        assert len(df) == 2
        print(f"[OK] xlsx written and re-read, shape={df.shape}")

        # 兩筆料件都是 USD(mock 資料),理論總價 = 0.009*2(電阻) + mcu 的 needs_human_review
        # 仍有價格(候選被選中,只是信心不足),所以兩筆都會被計入。
        assert "USD" in summary["totals_by_currency"]
        usd = summary["totals_by_currency"]["USD"]
        assert usd["unit_price_total"] > 0
        assert usd["moq_budget_total"] > 0
        print(f"[OK] summary totals: {summary['totals_by_currency']}")

        summary_df = pd.read_excel(out_path, sheet_name="Summary")
        assert "項目" in summary_df.columns and "數值" in summary_df.columns
        print("[OK] Summary sheet present")


def test_data_fetched_at_flows_to_report():
    # 供應商候選上的 _fetched_at(由 lookup_all 蓋章)要一路流到報表的
    # data_fetched_at 欄位,並在 Summary 彙總出最舊/最新日期與「日期不詳」筆數。
    # 這是使用者唯一能看出「手上這份報價有多舊」的線索——快取沒有時效,
    # 重跑 pipeline 不會自動更新價格,所以這條路徑不能斷。
    line_new = ml.BOMLine(ref_des="R1", mpn="NEW-PART", manufacturer="Yageo",
                          qty_per_unit=1, description="RES 10K")
    line_old = ml.BOMLine(ref_des="R2", mpn="OLD-PART", manufacturer="Yageo",
                          qty_per_unit=1, description="RES 20K")

    def _part(mpn, desc, fetched_at):
        return {"ManufacturerPartNumber": mpn, "Manufacturer": "Yageo", "Description": desc,
                "Min": "1", "Mult": "1",
                "PriceBreaks": [{"Quantity": 1, "Price": "0.01", "Currency": "USD"}],
                "_fetched_at": fetched_at}

    vendor_results = {
        "NEW-PART": [_part("NEW-PART", "RES 10K", "2026-08-13")],
        "OLD-PART": [_part("OLD-PART", "RES 20K", None)],   # 舊快取,日期不詳
    }
    lines = [line_new, line_old]
    matches = ml.match_validation_all(lines, vendor_results)
    report = ml.build_report(lines, vendor_results, matches)

    by_ref = {r["ref_des"]: r for r in report}
    assert by_ref["R1"]["data_fetched_at"] == "2026-08-13"
    assert by_ref["R2"]["data_fetched_at"] is None
    # 供應商實際回傳的型號也要出現在報表裡(缺 MPN 的被動元件靠這欄才知道買哪顆)
    assert by_ref["R1"]["manufacturer_part_number"] == "NEW-PART"
    assert "data_fetched_at" in ml._REPORT_COLUMNS and "manufacturer_part_number" in ml._REPORT_COLUMNS

    summary = ml.compute_summary(report)
    assert summary["data_fetched_earliest"] == "2026-08-13"
    assert summary["data_fetched_latest"] == "2026-08-13"
    assert summary["data_fetched_unknown_lines"] == 1
    print("[OK] test_data_fetched_at_flows_to_report")


def test_mouser_part_number_lead_availability():
    # mouser_part_number 是 Mouser 自己網站上的內部料號(例如網站顯示成
    # "871-B39941B8048P810"),跟 manufacturer_part_number(製造商編號)是
    # 兩件不同的事,之前完全沒有這個欄位。這裡驗證 Mouser 候選(欄位名稱是
    # Mouser 自己的 MouserPartNumber/LeadTime/Availability,原始 dict 直接讀)
    # 能正確拆開這兩者。
    line = ml.BOMLine(ref_des="U1", mpn="B39941B8048P810", manufacturer="RF360",
                      qty_per_unit=1, description="Duplexer")

    mouser_candidate = {
        "ManufacturerPartNumber": "B39941B8048P810",
        "MouserPartNumber": "871-B39941B8048P810",
        "Manufacturer": "RF360", "Description": "Duplexer",
        "Min": "1", "Mult": "1", "LeadTime": "8 Weeks", "Availability": "1234 In Stock",
        "PriceBreaks": [{"Quantity": 1, "Price": "1.00", "Currency": "USD"}],
    }
    vendor_results = {"B39941B8048P810": [mouser_candidate]}
    matches = ml.match_validation_all([line], vendor_results)
    report = ml.build_report([line], vendor_results, matches)
    row = report[0]
    assert row["mouser_part_number"] == "871-B39941B8048P810"
    assert row["mouser_part_number"] != row["manufacturer_part_number"]  # 使用者回報的那個混淆
    assert row["lead_time"] == "8 Weeks"
    assert row["availability"] == "1234 In Stock"

    # DigiKey 路徑:DigiKey 沒有「Mouser 編號」這個概念,故意留空,不拿
    # DigiKey 自己的內部料號(DigiKeyProductNumber)冒充填進去。
    # availability/lead_time 不受此限制,兩個供應商都可能有值。
    digikey_product = {
        "Description": {"ProductDescription": "Duplexer"},
        "Manufacturer": {"Name": "RF360"},
        "ManufacturerProductNumber": "B39941B8048P810",
        "QuantityAvailable": 500,
        "ProductVariations": [{
            "DigiKeyProductNumber": "RF360-B39941B8048P810DKR-ND",
            "MinimumOrderQuantity": 1, "StandardPackage": 1,
            "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 1.10}],
        }],
    }
    dk_part = ml._normalize_digikey_part(digikey_product, currency="USD")
    assert "VendorPartNumber" not in dk_part  # 沒有這個概念,不該存在
    assert dk_part["Availability"] == "500 In Stock"

    mouser_part_number, lead_time, availability = ml._mouser_part_number_lead_availability(dk_part)
    assert mouser_part_number == ""  # DigiKey 候選:Mouser 編號故意留空
    assert availability == "500 In Stock"

    for col in ("mouser_part_number", "lead_time", "availability"):
        assert col in ml._REPORT_COLUMNS
    print("[OK] test_mouser_part_number_lead_availability")


def test_normalize_digikey_part():
    # 依 DigiKey Product Information V4 官方 swagger 規格(ProductSearch.json)
    # 組一個接近真實形狀的 Product 物件,驗證轉換成 Mouser 形狀後欄位正確,
    # 且挑的是 MinimumOrderQuantity 最小的 ProductVariation。
    product = {
        "Description": {"ProductDescription": "RES SMD 10K OHM 1% 1/10W 0603", "DetailedDescription": "..."},
        "Manufacturer": {"Id": 1, "Name": "Yageo"},
        "ManufacturerProductNumber": "RC0603FR-0710KL",
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "311-10.0KHRCT-ND",
                "MinimumOrderQuantity": 1,
                "StandardPackage": 1,
                "StandardPricing": [
                    {"BreakQuantity": 1, "UnitPrice": 0.10, "TotalPrice": 0.10},
                    {"BreakQuantity": 10, "UnitPrice": 0.08, "TotalPrice": 0.80},
                ],
            },
            {
                "DigiKeyProductNumber": "311-10.0KHRDKR-ND",
                "MinimumOrderQuantity": 5000,
                "StandardPackage": 5000,
                "StandardPricing": [{"BreakQuantity": 5000, "UnitPrice": 0.03, "TotalPrice": 150.0}],
            },
        ],
    }
    part = ml._normalize_digikey_part(product, currency="USD")
    assert part["Manufacturer"] == "Yageo"
    assert part["ManufacturerPartNumber"] == "RC0603FR-0710KL"
    assert part["Description"] == "RES SMD 10K OHM 1% 1/10W 0603"
    assert part["Min"] == 1  # 應該選 MOQ=1 那個 variation,不是 MOQ=5000 那個
    assert part["Mult"] == 1
    assert part["PriceBreaks"] == [
        {"Quantity": 1, "Price": 0.10, "Currency": "USD"},
        {"Quantity": 10, "Price": 0.08, "Currency": "USD"},
    ]
    assert part["_source"] == "DigiKey"
    print("[OK] test_normalize_digikey_part")


def test_cache_migration():
    # 最舊格式(cache[key] = 候選 list,只代表 Mouser 結果)存檔後,重新讀取
    # 應該要自動轉成目前的格式,不遺失資料。查詢日期一律是 None:那個年代的
    # 快取根本沒存日期,**絕對不能拿今天的日期充數**——那會讓半年前的報價
    # 看起來像剛查的,比誠實承認「不詳」危險得多。
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "cache.json"
        old_format = {"SOME-MPN": [{"Manufacturer": "Yageo"}], "EMPTY-MPN": []}
        cache_path.write_text(__import__("json").dumps(old_format), encoding="utf-8")

        migrated = ml._load_cache(cache_path)
        assert migrated["SOME-MPN"] == {
            "mouser": [{"Manufacturer": "Yageo"}], "digikey": None,
            "mouser_fetched_at": None, "digikey_fetched_at": None,
        }
        assert migrated["EMPTY-MPN"] == {
            "mouser": [], "digikey": None,
            "mouser_fetched_at": None, "digikey_fetched_at": None,
        }

    # 中間格式(有 mouser/digikey 但還沒有查詢日期欄位)也要能無痛升級。
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "cache.json"
        mid_format = {"SOME-MPN": {"mouser": [{"Manufacturer": "Yageo"}], "digikey": None}}
        cache_path.write_text(__import__("json").dumps(mid_format), encoding="utf-8")

        migrated = ml._load_cache(cache_path)
        assert migrated["SOME-MPN"]["mouser"] == [{"Manufacturer": "Yageo"}]
        assert migrated["SOME-MPN"]["mouser_fetched_at"] is None

    # 目前格式:日期要原封不動保留下來(不能被今天的日期蓋掉)。
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "cache.json"
        cur_format = {"SOME-MPN": {"mouser": [{"Manufacturer": "Yageo"}], "digikey": None,
                                   "mouser_fetched_at": "2020-01-01", "digikey_fetched_at": None}}
        cache_path.write_text(__import__("json").dumps(cur_format), encoding="utf-8")

        migrated = ml._load_cache(cache_path)
        assert migrated["SOME-MPN"]["mouser_fetched_at"] == "2020-01-01"
    print("[OK] test_cache_migration")


def test_digikey_fallback_in_lookup_all():
    # 模擬:Mouser 查無結果、DigiKey 找到候選,驗證 lookup_all 會正確 fallback,
    # 且轉換後的候選料件形狀跟 Mouser 一致(match_validation/build_report 不用
    # 另外處理)。全程不打真實網路 —— 手動替換函式/常數,跑完再還原
    # (這份測試檔是純腳本執行,不是用 pytest 跑,沒有 monkeypatch fixture 可用)。
    digikey_part = {
        "Manufacturer": "TestCorp",
        "ManufacturerPartNumber": "DK-ONLY-PART",
        "Description": "some part only on digikey",
        "PriceBreaks": [{"Quantity": 1, "Price": 1.23, "Currency": "USD"}],
        "Min": 1,
        "Mult": 1,
        "_source": "DigiKey",
    }

    orig_query_mouser = ml.query_mouser
    orig_query_digikey_keyword = ml.query_digikey_keyword
    orig_digikey_enabled = ml.DIGIKEY_ENABLED
    ml.query_mouser = lambda mpn, api_key, max_candidates=5: []
    ml.query_digikey_keyword = lambda keyword, max_candidates=5: [digikey_part]
    ml.DIGIKEY_ENABLED = True
    try:
        line = ml.BOMLine(ref_des="U9", mpn="DK-ONLY-PART", manufacturer="TestCorp", qty_per_unit=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            results = ml.lookup_all([line], api_key="dummy", cache_path=cache_path, use_cache=True)

        assert results["DK-ONLY-PART"] == [digikey_part]

        matches = ml.match_validation_all([line], results)
        report = ml.build_report([line], results, matches)
        assert report[0]["match_status"] == "matched"
        assert report[0]["price_source"] == "DigiKey"
        assert report[0]["mouser_unit_price"] == 1.23
    finally:
        ml.query_mouser = orig_query_mouser
        ml.query_digikey_keyword = orig_query_digikey_keyword
        ml.DIGIKEY_ENABLED = orig_digikey_enabled
    print("[OK] test_digikey_fallback_in_lookup_all")


def test_digikey_fallback_when_mouser_priceless():
    # 模擬:Mouser 有回傳候選(型號比對上了),但候選 PriceBreaks 是空的
    # (缺貨/停產/該地區不開賣)。驗證 lookup_all 仍然會觸發 DigiKey fallback
    # ——這是這次修正的重點,以前只有「Mouser 完全查無候選」才會觸發——
    # 而且 Mouser、DigiKey 兩邊的候選會合併成同一份 list,不是互斥二選一。
    mouser_priceless_part = {
        "Manufacturer": "VendorA",
        "ManufacturerPartNumber": "SHARED-PART",
        "Description": "part with no mouser price",
        "PriceBreaks": [],
        "Min": "1",
        "Mult": "1",
    }
    digikey_priced_part = {
        "Manufacturer": "VendorA",
        "ManufacturerPartNumber": "SHARED-PART",
        "Description": "part with digikey price",
        "PriceBreaks": [{"Quantity": 1, "Price": 2.50, "Currency": "USD"}],
        "Min": 1,
        "Mult": 1,
        "_source": "DigiKey",
    }

    orig_query_mouser = ml.query_mouser
    orig_query_digikey_keyword = ml.query_digikey_keyword
    orig_digikey_enabled = ml.DIGIKEY_ENABLED
    ml.query_mouser = lambda mpn, api_key, max_candidates=5: [dict(mouser_priceless_part)]
    ml.query_digikey_keyword = lambda keyword, max_candidates=5: [dict(digikey_priced_part)]
    ml.DIGIKEY_ENABLED = True
    try:
        line = ml.BOMLine(ref_des="U9", mpn="SHARED-PART", manufacturer="VendorA", qty_per_unit=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            results = ml.lookup_all([line], api_key="dummy", cache_path=cache_path, use_cache=True)

        candidates = results["SHARED-PART"]
        assert len(candidates) == 2, candidates
        sources = {c.get("_source") for c in candidates}
        assert sources == {None, "DigiKey"}, sources

        matches = ml.match_validation_all([line], results)
        report = ml.build_report([line], results, matches)
        assert report[0]["match_status"] == "matched"
        assert report[0]["price_source"] == "DigiKey", "應該選到有報價的 DigiKey 候選"
        assert report[0]["mouser_unit_price"] == 2.50
    finally:
        ml.query_mouser = orig_query_mouser
        ml.query_digikey_keyword = orig_query_digikey_keyword
        ml.DIGIKEY_ENABLED = orig_digikey_enabled
    print("[OK] test_digikey_fallback_when_mouser_priceless")


def test_digikey_not_queried_when_mouser_already_priced():
    # 反面案例:Mouser 候選已經有報價時,不應該多打一次 DigiKey——
    # 節省查詢額度,行為跟修正前一致。
    mouser_priced_part = {
        "Manufacturer": "VendorA",
        "ManufacturerPartNumber": "PRICED-PART",
        "Description": "already has a price",
        "PriceBreaks": [{"Quantity": 1, "Price": "$1.00", "Currency": "USD"}],
        "Min": "1",
        "Mult": "1",
    }

    def _digikey_should_not_be_called(keyword, max_candidates=5):
        raise AssertionError("DigiKey 不該被呼叫——Mouser 候選已經有報價")

    orig_query_mouser = ml.query_mouser
    orig_query_digikey_keyword = ml.query_digikey_keyword
    orig_digikey_enabled = ml.DIGIKEY_ENABLED
    ml.query_mouser = lambda mpn, api_key, max_candidates=5: [mouser_priced_part]
    ml.query_digikey_keyword = _digikey_should_not_be_called
    ml.DIGIKEY_ENABLED = True
    try:
        line = ml.BOMLine(ref_des="U10", mpn="PRICED-PART", manufacturer="VendorA", qty_per_unit=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            results = ml.lookup_all([line], api_key="dummy", cache_path=cache_path, use_cache=True)
        assert results["PRICED-PART"] == [mouser_priced_part]
    finally:
        ml.query_mouser = orig_query_mouser
        ml.query_digikey_keyword = orig_query_digikey_keyword
        ml.DIGIKEY_ENABLED = orig_digikey_enabled
    print("[OK] test_digikey_not_queried_when_mouser_already_priced")


def test_no_import_time_api_key_requirement():
    # 確保 import 這個模組不會因為沒有 MOUSER_SEARCH_API_KEY 而炸掉
    # (骨架程式碼原本在 import 時就檢查,已改為延遲到 lookup_all()/run_pipeline() 才檢查)
    saved = os.environ.pop("MOUSER_SEARCH_API_KEY", None)
    try:
        import importlib
        importlib.reload(ml)
    finally:
        if saved is not None:
            os.environ["MOUSER_SEARCH_API_KEY"] = saved
    print("[OK] module import does not require MOUSER_SEARCH_API_KEY")


if __name__ == "__main__":
    test_parse_bom_column_mapping()
    test_match_validation()
    test_match_validation_price_tie_break()
    test_price_break_selection()
    test_build_report_and_xlsx_output()
    test_data_fetched_at_flows_to_report()
    test_mouser_part_number_lead_availability()
    test_normalize_digikey_part()
    test_cache_migration()
    test_digikey_fallback_in_lookup_all()
    test_digikey_fallback_when_mouser_priceless()
    test_digikey_not_queried_when_mouser_already_priced()
    test_no_import_time_api_key_requirement()
    print("\n全部自測通過 ✅")
