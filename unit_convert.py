# -*- coding: utf-8 -*-
"""单位换算模块：把带中文单位后缀的金额（"5.22亿"、"8194.39万"、"8300.00"）统一换算成【万元】数值。

用法：
    from unit_convert import to_wan
    to_wan("5.22亿")            # -> 52200.0
    to_wan("8300.00")           # -> 0.83   （裸数字默认按元）
    to_wan("8300", default_unit="万")  # -> 8300.0

    python unit_convert.py --test                # 跑内置测试
    python unit_convert.py 600396_资产负债表.csv  # 转换 CSV -> 600396_资产负债表_万元.csv
"""
import csv
import sys
from decimal import Decimal, InvalidOperation

# 各单位后缀 -> 换算到万元的系数（长后缀在前，保证 "万亿" 先于 "万"、"亿元" 先于 "元" 匹配）
_FACTOR = {
    "万亿元": Decimal(10 ** 8),
    "万亿":   Decimal(10 ** 8),
    "亿元":   Decimal(10 ** 4),
    "亿":     Decimal(10 ** 4),
    "万元":   Decimal(1),
    "万":     Decimal(1),
    "元":     Decimal("0.0001"),
}
_MISSING = {"", "-", "—", "--"}

# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（data_ingestion -> unit_conversion）
# ═══════════════════════════════════════════════════════════════
UNIT_RULES_SOURCE = "fallback"
_UNIT_REVISION: int | None = None


def _load_unit_rules() -> bool:
    """重新读取单位换算规则并写回模块级缓存。"""
    global _FACTOR, _MISSING, UNIT_RULES_SOURCE
    try:
        from rules_registry import get_registry
        _ingestion_rules = get_registry().data_ingestion
    except Exception:
        return False
    _unit_rules = _ingestion_rules.get("unit_conversion", {}) if isinstance(_ingestion_rules, dict) else {}
    _loaded_factors = _unit_rules.get("factors_to_wan", {}) if isinstance(_unit_rules, dict) else {}
    _loaded_missing = _unit_rules.get("missing_tokens") if isinstance(_unit_rules, dict) else None
    if isinstance(_loaded_factors, dict) and _loaded_factors:
        _FACTOR = {str(k): Decimal(str(v)) for k, v in _loaded_factors.items()}
    if isinstance(_loaded_missing, list):
        _MISSING = {str(x) for x in _loaded_missing}
    UNIT_RULES_SOURCE = "rules_registry"
    return True


def sync_unit_rules() -> None:
    """规则库发布后按需重新读取单位换算规则；未变化时仅一次修订号比较。"""
    global _UNIT_REVISION
    try:
        from rules_registry import sync_on_revision
        _UNIT_REVISION = sync_on_revision(_load_unit_rules, _UNIT_REVISION)
    except Exception:
        _UNIT_REVISION = None


# 导入时先按规则库初始化一次，行为与改造前一致
sync_unit_rules()


def to_wan(value, default_unit: str = "元") -> float | None:
    """把任意带单位的金额转成万元数值；空值/缺失返回 None。

    value: 字符串（可带 万亿/亿/万/元 后缀、千分位逗号、括号负数）或 int/float
    default_unit: 裸数字的默认单位，"元" 或 "万"（同花顺 CSV 裸数字是元）
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(Decimal(str(value)) * _FACTOR[default_unit if default_unit != "万" else "万元"])
    s = str(value).strip().replace(",", "").replace(" ", "")
    if s in _MISSING:
        return None
    # 括号负数："(123.45)" -> "-123.45"
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    num, unit = s, default_unit if default_unit != "万" else "万元"
    for suffix in _FACTOR:  # 长后缀优先
        if s.endswith(suffix):
            num, unit = s[: -len(suffix)], suffix
            break
    try:
        return float(Decimal(num) * _FACTOR[unit])
    except InvalidOperation:
        return None


def convert_csv(in_path: str, out_path: str | None = None) -> str:
    """把同花顺 CSV（首行=科目+年份表头，首列=科目名）的值全部转成万元数值。"""
    out_path = out_path or in_path.replace(".csv", "_万元.csv")
    with open(in_path, encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.reader(fp))
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(rows[0])  # 表头原样保留
        for row in rows[1:]:
            vals = [to_wan(v) for v in row[1:]]
            w.writerow([row[0]] + [_fmt(v) for v in vals])
    return out_path


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _test():
    cases = [
        ("5.22亿", 52200.0),          # 资产负债表实际值
        ("8194.39万", 8194.39),       # 现金流量表实际值
        ("-8939.47万", -8939.47),     # 负数变动量
        ("8300.00", 0.83),            # 裸数字按元
        ("1.27万", 1.27),
        ("4.46万亿", 446000000.0),    # 大型银行量级
        ("1,154.85亿", 11548500.0),   # 千分位逗号
        ("409.81亿元", 4098100.0),    # 复合后缀 亿元
        ("(123.45)", -0.012345),      # 括号负数（元）
        ("", None),
        ("--", None),
        (None, None),
        (52200, 5.22),                # int 裸数字按元
    ]
    for raw, expect in cases:
        got = to_wan(raw)
        assert got == expect, f"to_wan({raw!r}) = {got!r}, 期望 {expect!r}"
    assert to_wan("8300", default_unit="万") == 8300.0
    assert to_wan("5.22亿", default_unit="万") == 52200.0  # 有后缀时忽略默认单位
    print(f"全部 {len(cases) + 2} 个测试通过")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        _test()
    elif len(sys.argv) > 1:
        print("已生成:", convert_csv(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        print(__doc__)
