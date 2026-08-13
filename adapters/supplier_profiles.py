#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
adapters/supplier_profiles.py

登記每一家「一格塞多屬性」供應商 BOM 的排版規則,給
normalize_compound_spec_bom.py 用。**新增一家新供應商,只要在下面
SUPPLIER_PROFILES 加一個 SupplierProfile,不用碰
normalize_compound_spec_bom.py 本身的解析邏輯**——封裝/公差/數值怎麼判斷、
「哪個 token 才是型號」這些是通用邏輯,跟供應商排版習慣無關,所有 profile
共用,不用每家供應商重寫一次。

每個 profile 描述的是「這家供應商的 BOM 有什麼排版特徵」:

- `detect_columns`:標題列裡只要出現任何一個,就代表是這種格式(目前用
  「有沒有規格型號這種塞多屬性的欄位」當判斷依據)。`normalize_compound_spec_bom.py`
  掃描前幾列時,會依序拿每個 profile 的 `detect_columns` 去比對,找到第一個
  命中的就當作這份 BOM 用的 profile。
- `columns`:各邏輯欄位(類別描述/規格型號/位號/數量)在原始檔案裡可能
  出現的欄位標題候選清單,同時列簡繁兩種寫法保險(比對原始檔案標題用的
  字串,原始檔案是簡體就要留著簡體候選,不能只留繁體——供應商 BOM 用簡體
  還是繁體看的是他們自己的排版習慣,不是這份文件寫給誰看,兩者無關,
  所以這一點不會因為多了 profile 機制而改變)。
- `brand_pattern` / `remark_pattern`:「品牌」「備注」這類標籤在規格文字
  裡長什麼樣子的 regex(要抓全形/半形括號、冒號可有可無這種變體)。

## 新增新供應商 profile 的流程

把該供應商實際的原始 BOM 標題列,跟幾筆規格型號欄位的文字範例貼給
Claude 看,讓它照這個檔案現有 profile 的格式提議一個新的 SupplierProfile
(包括判斷這家是不是真的需要「一格塞多屬性」拆解,還是其實欄位本來就是
分開的,直接用 bom-parse skill 就夠、根本不需要加 profile)。**你確認欄位
候選、regex 沒問題之後才貼進來**——不要讓它自己猜完直接動手改檔案,規則
是不是通用、會不會誤傷其他供應商的資料,需要人確認過(跟 `manufacturer_aliases.md`
的處理原則一致)。
"""

from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(frozen=True)
class SupplierProfile:
    id: str
    description: str
    detect_columns: tuple[str, ...]
    columns: dict[str, tuple[str, ...]]
    brand_pattern: str
    remark_pattern: str

    @property
    def brand_re(self) -> re.Pattern:
        return re.compile(self.brand_pattern)

    @property
    def remark_re(self) -> re.Pattern:
        return re.compile(self.remark_pattern)


SUPPLIER_PROFILES: tuple[SupplierProfile, ...] = (
    SupplierProfile(
        id="bracket_labeled_spec",
        description=(
            "「名称描述」+「规格型号」欄位組合,規格型號欄位裡用"
            "（品牌：X）（备注：Y）括號標籤夾帶廠牌/備注(順序可能顛倒、"
            "冒號可能省略)。這是部分供應商/EMS 廠常見的排版慣例,不限特定"
            "客戶或專案。"
        ),
        detect_columns=("规格型号", "規格型號"),
        columns={
            "category": ("名称描述", "名稱描述", "名称", "名稱"),
            "spec": ("规格型号", "規格型號"),
            "ref_des": ("位号", "位號"),
            "qty": ("组成用量", "組成用量", "数量", "數量"),
        },
        brand_pattern=r"[（(]\s*品牌\s*[:：]?\s*(?P<mfr>[^）)]*?)\s*[）)]",
        remark_pattern=r"[（(]\s*备注\s*[:：]?\s*(?P<remark>[^）)]*?)\s*[）)]",
    ),
)


def detect_profile(cell_values) -> SupplierProfile | None:
    """
    傳入原始檔案某一列的儲存格內容(iterable),依序比對每個已知 profile 的
    detect_columns,回傳第一個命中的 profile;都沒命中回傳 None。
    """
    cells = {str(c).strip() for c in cell_values if c is not None}
    for profile in SUPPLIER_PROFILES:
        if any(marker in cells for marker in profile.detect_columns):
            return profile
    return None


def get_profile(profile_id: str) -> SupplierProfile:
    for profile in SUPPLIER_PROFILES:
        if profile.id == profile_id:
            return profile
    known = ", ".join(p.id for p in SUPPLIER_PROFILES)
    raise KeyError(f"找不到 profile id={profile_id!r},目前已知的 profile: {known}")
