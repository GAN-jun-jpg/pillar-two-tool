# -*- coding: utf-8 -*-
"""剩余四个规则文件的只读接入回归测试。"""
import calculator
import exchange_rate
import financial_parser
import unit_convert
import utils


def test_parser_keywords_source():
    assert financial_parser.PARSER_KEYWORDS_SOURCE == "rules_registry"
    assert "支付给职工以及为职工支付的现金" in financial_parser.CASH_FLOW_SUBJECTS["cash_to_employees"]


def test_unit_conversion_source_and_behavior():
    assert unit_convert.UNIT_RULES_SOURCE == "rules_registry"
    assert unit_convert.to_wan("5.22亿") == 52200.0
    assert unit_convert.to_wan("8194.39万") == 8194.39


def test_exchange_rate_source():
    assert exchange_rate.FX_RULES_SOURCE == "rules_registry"
    assert "USD" in exchange_rate.CURRENCIES
    assert exchange_rate.CURRENCIES["USD"] == "美元"


def test_data_ingestion_source():
    assert utils.DATA_INGESTION_SOURCE == "rules_registry"
    assert utils.DTL_TYPE_FROM_CN["固定资产"] == "Fixed Asset"


def test_dtl_recapture_rule_source():
    assert calculator.DTL_RULES_SOURCE == "rules_registry"
    assert calculator.RECAPTURE_OFFSET == 5
    assert calculator.EXPIRY_OFFSET == 4
    assert len(calculator.DTL_TYPES) == 8


def test_allocation_rule_source():
    assert calculator.ALLOCATION_RULES_SOURCE == "rules_registry"
    assert calculator.ALLOCATION_RULE_ORDER == ["QDMTT", "IIR", "UTPR"]
    assert calculator.UTPR_ASSET_WEIGHT == 0.5
    assert calculator.UTPR_PAYROLL_WEIGHT == 0.5
    assert calculator.ALLOCATION_ROUND_DECIMALS == 2
