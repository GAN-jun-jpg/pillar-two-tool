# -*- coding: utf-8 -*-
"""exchange_rate tests (injected fake source, no network)."""
import json

import pytest

from exchange_rate import fetch_exchange_rate


def _loader(url):
    # mimics open.er-api.com /v6/latest/CNY: rates per 1 CNY
    return json.dumps({"result": "success", "rates": {"CNY": 1.0, "USD": 0.1388889}})


def test_fetch_usd():
    rate = fetch_exchange_rate("USD", loader=_loader)
    assert abs(rate - 7.2) < 1e-4, rate


def test_fetch_unknown_currency():
    with pytest.raises(RuntimeError):
        fetch_exchange_rate("XXX", loader=_loader)
