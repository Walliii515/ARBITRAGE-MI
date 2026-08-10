# coding: utf-8
"""
Verify strategy-isolated exchange API keys from .env.

The checks are read-only:
- Binance spot account for forward/reverse
- Binance cross-margin account for reverse
- Gate USDT futures account for forward/reverse

No orders, borrows, repayments, or transfers are submitted.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


BINANCE_BASE_URL = "https://api1.binance.com"
GATE_BASE_URL = "https://api.gateio.ws"
TIMEOUT_SEC = 10


@dataclass
class CheckResult:
    ok: bool
    msg: str


@dataclass
class StrategyKeys:
    name: str
    binance_key: str
    binance_secret: str
    gate_key: str
    gate_secret: str


def mask(value: str) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return value[0:2] + "***"
    return value[:4] + "..." + value[-4:]


def binance_sign(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def binance_signed_get(api_key: str, secret: str, path: str, params: Optional[Dict] = None) -> requests.Response:
    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    payload.setdefault("recvWindow", 10000)
    query = urlencode(payload)
    payload["signature"] = binance_sign(secret, query)
    return requests.get(
        BINANCE_BASE_URL + path,
        params=payload,
        headers={"X-MBX-APIKEY": api_key},
        timeout=TIMEOUT_SEC,
    )


def gate_sign(secret: str, method: str, api_path: str, query_string: str = "", body: str = "") -> Dict[str, str]:
    timestamp = str(int(time.time()))
    hashed_payload = hashlib.sha512(body.encode("utf-8")).hexdigest()
    sign_string = f"{method}\n{api_path}\n{query_string}\n{hashed_payload}\n{timestamp}"
    signature = hmac.new(
        secret.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return {
        "KEY": "",
        "SIGN": signature,
        "Timestamp": timestamp,
        "Content-Type": "application/json",
    }


def gate_signed_get(api_key: str, secret: str, api_path: str, query_string: str = "") -> requests.Response:
    headers = gate_sign(secret, "GET", api_path, query_string, "")
    headers["KEY"] = api_key
    url = GATE_BASE_URL + api_path
    if query_string:
        url += "?" + query_string
    return requests.get(url, headers=headers, timeout=TIMEOUT_SEC)


def short_error(resp: requests.Response) -> str:
    text = (resp.text or "").replace("\n", " ")[:240]
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("label")
            msg = payload.get("msg") or payload.get("message")
            if code or msg:
                return f"HTTP {resp.status_code}: code={code}, msg={msg}"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {text}"


def check_binance_spot(api_key: str, secret: str) -> CheckResult:
    if not api_key or not secret:
        return CheckResult(False, "missing key/secret")
    try:
        resp = binance_signed_get(api_key, secret, "/api/v3/account")
        if resp.status_code != 200:
            return CheckResult(False, short_error(resp))
        data = resp.json()
        balances = data.get("balances") if isinstance(data, dict) else []
        nonzero = 0
        for item in balances or []:
            try:
                if float(item.get("free") or 0) + float(item.get("locked") or 0) > 0:
                    nonzero += 1
            except Exception:
                continue
        can_trade = data.get("canTrade")
        return CheckResult(True, f"spot account ok, canTrade={can_trade}, nonzero_assets={nonzero}")
    except Exception as exc:
        return CheckResult(False, str(exc)[:240])


def check_binance_cross_margin(api_key: str, secret: str) -> CheckResult:
    if not api_key or not secret:
        return CheckResult(False, "missing key/secret")
    try:
        resp = binance_signed_get(api_key, secret, "/sapi/v1/margin/account")
        if resp.status_code != 200:
            return CheckResult(False, short_error(resp))
        data = resp.json()
        margin_level = data.get("marginLevel") if isinstance(data, dict) else None
        borrow_enabled = data.get("borrowEnabled") if isinstance(data, dict) else None
        trade_enabled = data.get("tradeEnabled") if isinstance(data, dict) else None
        assets = data.get("userAssets") if isinstance(data, dict) else []
        if borrow_enabled is not True or trade_enabled is not True:
            return CheckResult(
                False,
                f"cross-margin account readable but not trade/borrow ready, "
                f"borrowEnabled={borrow_enabled}, tradeEnabled={trade_enabled}, "
                f"marginLevel={margin_level}, assets={len(assets or [])}",
            )
        return CheckResult(
            True,
            f"cross-margin account ok, borrowEnabled={borrow_enabled}, "
            f"tradeEnabled={trade_enabled}, marginLevel={margin_level}, assets={len(assets or [])}",
        )
    except Exception as exc:
        return CheckResult(False, str(exc)[:240])


def check_gate_futures(api_key: str, secret: str) -> CheckResult:
    if not api_key or not secret:
        return CheckResult(False, "missing key/secret")
    try:
        resp = gate_signed_get(api_key, secret, "/api/v4/futures/usdt/accounts")
        if resp.status_code != 200:
            return CheckResult(False, short_error(resp))
        data = resp.json()
        available = data.get("available") if isinstance(data, dict) else None
        total = data.get("total") if isinstance(data, dict) else None
        return CheckResult(True, f"futures account ok, available={available}, total={total}")
    except Exception as exc:
        return CheckResult(False, str(exc)[:240])


def load_strategy_keys() -> list[StrategyKeys]:
    return [
        StrategyKeys(
            name="forward",
            binance_key=os.getenv("FORWARD_BINANCE_API_KEY", ""),
            binance_secret=os.getenv("FORWARD_BINANCE_API_SECRET", ""),
            gate_key=os.getenv("FORWARD_GATE_FUTURES_API_KEY", ""),
            gate_secret=os.getenv("FORWARD_GATE_FUTURES_API_SECRET", ""),
        ),
        StrategyKeys(
            name="reverse",
            binance_key=os.getenv("REVERSE_BINANCE_API_KEY", ""),
            binance_secret=os.getenv("REVERSE_BINANCE_API_SECRET", ""),
            gate_key=os.getenv("REVERSE_GATE_FUTURES_API_KEY", ""),
            gate_secret=os.getenv("REVERSE_GATE_FUTURES_API_SECRET", ""),
        ),
    ]


def print_result(label: str, result: CheckResult) -> None:
    mark = "OK" if result.ok else "FAIL"
    print(f"  [{mark}] {label}: {result.msg}")


def main() -> int:
    load_dotenv(ROOT / ".env")
    print("Verifying strategy API keys from .env")
    print("No order/borrow/repay/transfer requests will be sent.\n")

    all_ok = True
    for item in load_strategy_keys():
        print(f"{item.name.upper()}")
        print(f"  Binance key: {mask(item.binance_key)}")
        print(f"  Gate key:    {mask(item.gate_key)}")
        spot = check_binance_spot(item.binance_key, item.binance_secret)
        gate = check_gate_futures(item.gate_key, item.gate_secret)
        print_result("Binance spot account", spot)
        if item.name == "reverse":
            margin = check_binance_cross_margin(item.binance_key, item.binance_secret)
            print_result("Binance cross-margin account", margin)
            all_ok = all_ok and margin.ok
        print_result("Gate USDT futures account", gate)
        all_ok = all_ok and spot.ok and gate.ok
        print()

    if not all_ok:
        print("One or more checks failed. Fix key permissions/IP whitelist/sub-account wallet activation first.")
        return 1
    print("All configured strategy API read checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
