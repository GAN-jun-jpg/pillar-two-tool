# -*- coding: utf-8 -*-
"""汇率获取模块：外币报表 -> 人民币折算。

实时优先走新浪（国内可达），失败回退 open.er-api.com / frankfurter.app（欧央行每日汇率）。
所有来源都失败时抛异常，由界面提示用户手动输入汇率。
"""
import json
import urllib.parse
import urllib.request

CURRENCIES: dict[str, str] = {
    "CNY": "人民币",
    "USD": "美元",
    "EUR": "欧元",
    "HKD": "港币",
    "SGD": "新加坡元",
    "GBP": "英镑",
    "JPY": "日元",
    "AUD": "澳元",
    "CAD": "加元",
    "CHF": "瑞士法郎",
}

SINA_SYMBOLS = {
    "USD": "fx_susdcny",
    "EUR": "fx_seurcny",
    "HKD": "fx_shkdcny",
    "SGD": "fx_ssgdcny",
    "GBP": "fx_sgbpcny",
    "JPY": "fx_sjpycny",
    "CHF": "fx_schfcny",
    "CAD": "fx_scadcny",
}


# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（data_ingestion -> currency / exchange_rate_sources）
# ═══════════════════════════════════════════════════════════════
FX_RULES_SOURCE = "fallback"
_FX_SOURCE_PRIORITY = ["sina", "open.er-api.com", "frankfurter.app"]
try:
    from rules_registry import get_registry
    _ingestion_rules = get_registry().data_ingestion
    _currency_rules = _ingestion_rules.get("currency", {}) if isinstance(_ingestion_rules, dict) else {}
    _fx_rules = _ingestion_rules.get("exchange_rate_sources", {}) if isinstance(_ingestion_rules, dict) else {}
    _loaded_currencies = _currency_rules.get("supported_currencies") if isinstance(_currency_rules, dict) else None
    _loaded_symbols = _fx_rules.get("sina_symbols") if isinstance(_fx_rules, dict) else None
    _loaded_priority = _fx_rules.get("priority") if isinstance(_fx_rules, dict) else None

    if isinstance(_loaded_currencies, dict) and _loaded_currencies:
        CURRENCIES = {str(k): str(v) for k, v in _loaded_currencies.items()}
    if isinstance(_loaded_symbols, dict) and _loaded_symbols:
        SINA_SYMBOLS = {str(k): str(v) for k, v in _loaded_symbols.items()}
    if isinstance(_loaded_priority, list) and _loaded_priority:
        _FX_SOURCE_PRIORITY = [str(x) for x in _loaded_priority]
    FX_RULES_SOURCE = "rules_registry"
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def _http_get(url: str, timeout: float = 8, referer: str | None = None) -> str:
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _from_sina(symbol: str) -> float | None:
    raw = _http_get(
        "https://hq.sinajs.cn/list=" + symbol,
        referer="https://finance.sina.com.cn",
    )
    m = raw.split('"')
    if len(m) < 2:
        return None
    fields = m[1].split(",")
    for idx in (1, 3):
        if len(fields) > idx:
            try:
                v = float(fields[idx])
                if v > 0:
                    return v
            except ValueError:
                continue
    return None


def _from_er_api(code: str) -> float | None:
    raw = _http_get("https://open.er-api.com/v6/latest/CNY")
    data = json.loads(raw)
    rates = data.get("rates", {})
    v = rates.get(code)
    if v:
        return 1.0 / float(v)  # 1 外币 = ? 人民币
    return None


def _from_frankfurter(code: str) -> float | None:
    raw = _http_get("https://frankfurter.app/latest?from=" + code + "&to=CNY")
    data = json.loads(raw)
    v = data.get("rates", {}).get("CNY")
    if v:
        return float(v)
    return None


def fetch_exchange_rate(source: str = "USD", target: str = "CNY",
                        loader=None) -> float:
    """返回 1 单位 source 货币折合 target 货币的汇率。

    loader 供测试注入（callable(url) -> str）；真实调用时忽略。
    """
    if loader is not None:
        raw = loader("https://open.er-api.com/v6/latest/CNY")
        data = json.loads(raw)
        v = data.get("rates", {}).get(source)
        if v:
            return 1.0 / float(v)
        raise RuntimeError("模拟数据中未找到该币种")

    errors: list[str] = []
    for source_name in _FX_SOURCE_PRIORITY:
        try:
            if source_name == "sina":
                if source not in SINA_SYMBOLS:
                    continue
                v = _from_sina(SINA_SYMBOLS[source])
            elif source_name in ("open.er-api.com", "er-api", "er_api"):
                v = _from_er_api(source)
            elif source_name in ("frankfurter.app", "frankfurter"):
                v = _from_frankfurter(source)
            else:
                continue
            if v:
                return v
        except Exception as e:
            errors.append(f"{source_name}: {e}")
    raise RuntimeError("实时/每日汇率获取失败，请手动填写汇率。详情: " + "; ".join(errors))
