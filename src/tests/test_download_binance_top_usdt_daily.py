import importlib.util
from datetime import date
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "download_binance_top_usdt_daily.py"
)
SPEC = importlib.util.spec_from_file_location("download_binance_top_usdt_daily", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_select_top_usdt_spot_symbols_filters_and_ranks():
    exchange_info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "OLDUSDT",
                "status": "BREAK",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "BTCUSDC",
                "status": "TRADING",
                "quoteAsset": "USDC",
                "isSpotTradingAllowed": True,
            },
        ]
    }
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "100"},
        {"symbol": "ETHUSDT", "quoteVolume": "200"},
        {"symbol": "OLDUSDT", "quoteVolume": "999"},
        {"symbol": "BTCUSDC", "quoteVolume": "888"},
    ]

    result = MODULE.select_top_usdt_spot_symbols(exchange_info, tickers, top=2)

    assert [item[0] for item in result] == ["ETHUSDT", "BTCUSDT"]


def test_fetch_hourly_klines_paginates(monkeypatch):
    hour_ms = MODULE.INTERVAL_MILLISECONDS["1h"]
    first_batch = [
        [index * hour_ms, "1", "2", "0", "1", "3", 1, "4", 5, "6", "7"]
        for index in range(MODULE.KLINE_LIMIT)
    ]
    second_batch = [
        [
            MODULE.KLINE_LIMIT * hour_ms,
            "1",
            "2",
            "0",
            "1",
            "3",
            1,
            "4",
            5,
            "6",
            "7",
        ]
    ]
    calls = []

    def fake_get_json(session, base_url, endpoint, params=None):
        calls.append(params)
        return first_batch if len(calls) == 1 else second_batch

    monkeypatch.setattr(MODULE, "get_json", fake_get_json)
    rows = MODULE.fetch_klines(
        session=object(),
        base_url="https://example.test",
        symbol="BTCUSDT",
        interval="1h",
        start_ms=0,
        end_ms=(MODULE.KLINE_LIMIT + 1) * hour_ms,
        request_interval=0,
    )

    assert len(rows) == MODULE.KLINE_LIMIT + 1
    assert calls[0]["interval"] == "1h"
    assert calls[1]["startTime"] == MODULE.KLINE_LIMIT * hour_ms


def test_write_csv_uses_utc_timestamps(tmp_path):
    output = tmp_path / "BTCUSDT_1d.csv"
    MODULE.write_csv(
        output,
        [[0, "1", "2", "0.5", "1.5", "10", 86_399_999, "12", 8, "4", "5", "0"]],
    )

    content = output.read_text(encoding="utf-8")
    assert "1970-01-01T00:00:00.000Z" in content
    assert "1970-01-01T23:59:59.999Z" in content
    assert not output.with_suffix(".csv.tmp").exists()


def test_utc_day_start_ms():
    assert MODULE.utc_day_start_ms(date(1970, 1, 2)) == MODULE.DAY_MS
