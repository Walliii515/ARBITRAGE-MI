#!/usr/bin/env python3
"""下载 Binance 成交额靠前或指定的 USDT 现货交易对 K 线行情。

无需 API Key。交易对排名使用运行脚本时的 24 小时 USDT 成交额
（ticker/24hr 的 quoteVolume），K 线时间使用 UTC。
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, datetime, time as datetime_time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_BASE_URL = "https://api.binance.com"
DAY_MS = 24 * 60 * 60 * 1000
INTERVAL_MILLISECONDS = {
    "1h": 60 * 60 * 1000,
    "1d": DAY_MS,
}
KLINE_LIMIT = 1000
CSV_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "按当前 24 小时成交额选出 Binance 前 N 个 USDT 现货交易对，"
            "或下载指定交易对，并将 K 线行情分别保存为 CSV。"
        )
    )
    parser.add_argument(
        "--interval",
        choices=tuple(INTERVAL_MILLISECONDS),
        default="1d",
        help="K 线周期（默认：1d）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="下载成交额前多少个交易对（默认：30）",
    )
    parser.add_argument(
        "--symbols",
        help=(
            "逗号分隔的交易对名单，例如 BTCUSDT,ETHUSDT；"
            "指定后不再重新选取成交额前 N 名"
        ),
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        help="起始日期，UTC，格式 YYYY-MM-DD（默认：从交易对最早 K 线开始）",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        help="结束日期，UTC，格式 YYYY-MM-DD，包含该日（默认：最新已收盘 K 线）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "CSV 输出目录"
            "（默认：1d 使用 data/binance_spot_daily，其他周期使用 "
            "data/binance_spot_<周期>）"
        ),
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.1,
        help="每次 K 线请求后的等待秒数（默认：0.1）",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Binance REST API 地址（默认：{DEFAULT_BASE_URL}）",
    )
    args = parser.parse_args()

    if args.top <= 0:
        parser.error("--top 必须大于 0")
    if args.request_interval < 0:
        parser.error("--request-interval 不能小于 0")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date 不能晚于 --end-date")
    if args.symbols:
        args.symbols = parse_symbols(args.symbols, parser)
    if args.output_dir is None:
        suffix = "daily" if args.interval == "1d" else args.interval
        args.output_dir = Path(f"data/binance_spot_{suffix}")
    return args


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"无效日期 {value!r}，请使用 YYYY-MM-DD 格式"
        ) from exc


def utc_day_start_ms(value: date) -> int:
    moment = datetime.combine(value, datetime_time.min, tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def parse_symbols(value: str, parser: argparse.ArgumentParser) -> list[str]:
    symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",")))
    if not symbols or any(not item or not item.endswith("USDT") for item in symbols):
        parser.error("--symbols 必须是逗号分隔的 USDT 交易对，例如 BTCUSDT,ETHUSDT")
    return symbols


def create_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=(418, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": "Arbitrage-Mi-market-data/1.0"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def get_json(
    session: requests.Session,
    base_url: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> Any:
    response = session.get(
        f"{base_url.rstrip('/')}{endpoint}",
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def select_top_usdt_spot_symbols(
    exchange_info: dict[str, Any],
    tickers: list[dict[str, Any]],
    top: int,
) -> list[tuple[str, Decimal]]:
    """返回按 24h USDT 成交额降序排列的 (symbol, quoteVolume)。"""
    tradable_symbols = {
        item["symbol"]
        for item in exchange_info.get("symbols", [])
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("isSpotTradingAllowed", False)
    }

    ranked: list[tuple[str, Decimal]] = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if symbol not in tradable_symbols:
            continue
        try:
            quote_volume = Decimal(str(ticker.get("quoteVolume", "0")))
        except (InvalidOperation, ValueError):
            continue
        ranked.append((symbol, quote_volume))

    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:top]


def fetch_klines(
    session: requests.Session,
    base_url: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    request_interval: float,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    cursor = start_ms
    interval_ms = INTERVAL_MILLISECONDS[interval]

    while cursor <= end_ms:
        batch = get_json(
            session,
            base_url,
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": KLINE_LIMIT,
            },
        )
        if not batch:
            break

        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError(f"{symbol} 的 K 线分页游标未向前移动")
        cursor = next_cursor

        if len(batch) < KLINE_LIMIT:
            break
        if request_interval:
            time.sleep(request_interval)

    return rows


def iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def csv_rows(klines: Iterable[list[Any]]) -> Iterable[dict[str, Any]]:
    for row in klines:
        if len(row) < 11:
            raise ValueError(f"Binance 返回了无效的 K 线记录：{row!r}")
        yield {
            "open_time": iso_utc(int(row[0])),
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "close_time": iso_utc(int(row[6])),
            "quote_asset_volume": row[7],
            "number_of_trades": row[8],
            "taker_buy_base_asset_volume": row[9],
            "taker_buy_quote_asset_volume": row[10],
        }


def write_csv(path: Path, klines: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(csv_rows(klines))
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def completed_date_range(args: argparse.Namespace) -> tuple[int, int]:
    interval_ms = INTERVAL_MILLISECONDS[args.interval]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    latest_completed_end_ms = now_ms - (now_ms % interval_ms) - 1
    start_ms = utc_day_start_ms(args.start_date) if args.start_date else 0
    if args.end_date:
        requested_end_ms = utc_day_start_ms(args.end_date) + DAY_MS - 1
        end_ms = min(requested_end_ms, latest_completed_end_ms)
    else:
        end_ms = latest_completed_end_ms
    if start_ms > end_ms:
        raise ValueError("指定范围内没有已收盘 K 线")
    return start_ms, end_ms


def main() -> int:
    args = parse_args()
    try:
        start_ms, end_ms = completed_date_range(args)
        session = create_session()
        if args.symbols:
            symbols = [(symbol, None) for symbol in args.symbols]
        else:
            exchange_info = get_json(session, args.base_url, "/api/v3/exchangeInfo")
            tickers = get_json(session, args.base_url, "/api/v3/ticker/24hr")
            symbols = select_top_usdt_spot_symbols(exchange_info, tickers, args.top)
        if not symbols:
            raise RuntimeError("没有找到可交易的 USDT 现货交易对")

        print(
            f"将下载 {len(symbols)} 个交易对到 {args.output_dir.resolve()}",
            flush=True,
        )
        for rank, (symbol, quote_volume) in enumerate(symbols, start=1):
            volume_label = (
                f" (24h quoteVolume={quote_volume})"
                if quote_volume is not None
                else ""
            )
            print(f"[{rank:02d}/{len(symbols):02d}] {symbol}{volume_label}", flush=True)
            klines = fetch_klines(
                session=session,
                base_url=args.base_url,
                symbol=symbol,
                interval=args.interval,
                start_ms=start_ms,
                end_ms=end_ms,
                request_interval=args.request_interval,
            )
            output_path = args.output_dir / f"{symbol}_{args.interval}.csv"
            write_csv(output_path, klines)
            print(f"  已保存 {len(klines)} 行 -> {output_path}", flush=True)

        print("全部下载完成。", flush=True)
        return 0
    except (requests.RequestException, OSError, RuntimeError, ValueError) as exc:
        print(f"下载失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
