"""V2 buyer bot: once daily at noon America/Denver while Railway stays online.

Read only: does not write to Google Sheets or modify score formulas in column G.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from zoneinfo import ZoneInfo

DENVER = ZoneInfo("America/Denver")
SHEET_ID = "14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ"
LOG = logging.getLogger("v2-buyer")


@dataclass(frozen=True)
class Settings:
    alpaca_key: str
    alpaca_secret: str
    google_credentials_json: str
    sheet_id: str
    sheet_tab: str
    paper: bool
    dry_run: bool
    threshold: Decimal
    fraction: Decimal


def flag(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower().strip()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


def settings_from_env() -> Settings:
    settings = Settings(
        alpaca_key=os.getenv("APCA_API_KEY_ID", "").strip(),
        alpaca_secret=os.getenv("APCA_API_SECRET_KEY", "").strip(),
        google_credentials_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        sheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", SHEET_ID).strip(),
        sheet_tab=os.getenv("GOOGLE_SHEET_TAB", "Screener").strip(),
        paper=flag("ALPACA_PAPER", True),
        dry_run=flag("DRY_RUN", True),
        threshold=Decimal(os.getenv("MINIMUM_SCORE", "95")),
        fraction=Decimal(os.getenv("BUY_FRACTION", "0.02")),
    )
    if not all([settings.alpaca_key, settings.alpaca_secret, settings.google_credentials_json]):
        raise ValueError("Missing Alpaca keys or GOOGLE_SERVICE_ACCOUNT_JSON")
    if not settings.threshold.is_finite() or not 0 <= settings.threshold <= 100:
        raise ValueError("MINIMUM_SCORE must be between 0 and 100")
    if not settings.fraction.is_finite() or not 0 < settings.fraction <= Decimal("0.02"):
        raise ValueError("BUY_FRACTION must be > 0 and <= 0.02")
    if not settings.paper and not settings.dry_run and os.getenv("LIVE_TRADING_ACK") != "I_ACCEPT_LIVE_MARGIN_RISK":
        raise ValueError("Live orders need LIVE_TRADING_ACK=I_ACCEPT_LIVE_MARGIN_RISK")
    return settings


def next_noon(now: datetime) -> datetime:
    """Next weekday at 12:00 in Denver, adjusting automatically for DST."""
    local_now = now.astimezone(DENVER)
    for days in range(8):
        day = local_now.date() + timedelta(days=days)
        if day.weekday() >= 5:
            continue
        target = datetime.combine(day, clock_time(12, 0), tzinfo=DENVER)
        if target > local_now:
            return target
    raise RuntimeError("Could not find next weekday")


def symbol_key(value: object) -> str:
    return str(value or "").upper().strip().replace("/", ".")


def read_signals(sheets, settings: Settings) -> list[tuple[str, Decimal]]:
    """Read A (ticker) and G (score); ignore other indicators and unused rows."""
    tab = "'" + settings.sheet_tab.replace("'", "''") + "'"
    response = sheets.spreadsheets().values().get(
        spreadsheetId=settings.sheet_id,
        range=f"{tab}!A1:G500",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    rows = response.get("values", [])
    if not rows or len(rows[0]) < 7 or symbol_key(rows[0][0]) != "SYMBOL" or symbol_key(rows[0][6]) != "SCORE":
        raise ValueError("Expected column A header 'symbol' and column G header 'score'")
    qualified: dict[str, Decimal] = {}
    for row in rows[1:]:
        if len(row) < 7 or not row[0] or row[6] in (None, ""):
            continue
        symbol = symbol_key(row[0])
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", symbol):
            LOG.warning("Skipping invalid symbol: %r", row[0])
            continue
        try:
            score = Decimal(str(row[6]))
        except (InvalidOperation, ValueError):
            continue
        if score.is_finite() and settings.threshold <= score <= 100:
            qualified[symbol] = max(score, qualified.get(symbol, Decimal("0")))
    return sorted(qualified.items(), key=lambda item: (-item[1], item[0]))


def size_from_buying_power(buying_power: object, fraction: Decimal) -> Decimal:
    value = Decimal(str(buying_power))
    if not value.is_finite() or value <= 0:
        return Decimal("0")
    return (value * fraction).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def client_order_id(day: str, symbol: str) -> str:
    return f"v2sp100-{day.replace('-', '')}-{symbol.replace('.', '-')}"[:48]


def order_already_attempted(trader, client_id: str) -> bool:
    from alpaca.common.exceptions import APIError
    try:
        trader.get_order_by_client_id(client_id)
        return True
    except APIError as exc:
        if exc.status_code == 404:
            return False
        raise


def held_and_pending(trader) -> tuple[set[str], set[str]]:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    held = {symbol_key(p.symbol) for p in trader.get_all_positions()}
    orders = trader.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
    if len(orders) >= 500:
        raise RuntimeError("Too many pending orders to verify safely")
    pending = {symbol_key(o.symbol) for o in orders}
    return held, pending


def buy_signals(trader, signals: list[tuple[str, Decimal]], settings: Settings, date: str) -> dict:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    results = {"qualified": len(signals), "ordered": [], "dry_run": [], "skipped": []}
    for symbol, score in signals:
        try:
            if not trader.get_clock().is_open:
                LOG.warning("Market closed while processing; stopping")
                break
            held, pending = held_and_pending(trader)
            cid = client_order_id(date, symbol)
            if symbol in held or symbol in pending or order_already_attempted(trader, cid):
                results["skipped"].append((symbol, "held, pending, or already attempted"))
                continue
            asset = trader.get_asset(symbol)
            if not asset.tradable or not asset.fractionable:
                results["skipped"].append((symbol, "not fractionable/tradable"))
                continue
            account = trader.get_account()
            if any(getattr(account, key, False) for key in
                   ("account_blocked", "trading_blocked", "trade_suspended_by_user")):
                raise RuntimeError("Alpaca account restricted from trading")
            amount = size_from_buying_power(account.buying_power, settings.fraction)
            if amount < Decimal("1"):
                results["skipped"].append((symbol, "buying power too low"))
                continue
            if settings.dry_run:
                LOG.info("DRY RUN: %s score=%s buy=$%s", symbol, score, amount)
                results["dry_run"].append((symbol, str(amount)))
                continue
            order = trader.submit_order(order_data=MarketOrderRequest(
                symbol=symbol, notional=float(amount), side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, client_order_id=cid,
            ))
            LOG.info("BUY %s score=%s amount=$%s order_id=%s", symbol, score, amount, order.id)
            results["ordered"].append((symbol, str(amount), str(order.id)))
        except Exception:
            # Never blindly retry an ambiguous order submission.
            LOG.exception("Order processing stopped at %s", symbol)
            break
    return results


def run_cycle(sheets, trader, settings: Settings, now: datetime) -> dict:
    if now.astimezone(DENVER).weekday() >= 5 or not trader.get_clock().is_open:
        LOG.info("Market closed today, skipping")
        return {"status": "market-closed"}
    signals = read_signals(sheets, settings)
    LOG.info("Noon check: %d stocks scored >= %s", len(signals), settings.threshold)
    return buy_signals(trader, signals, settings, now.astimezone(DENVER).date().isoformat())


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    settings = settings_from_env()
    from alpaca.trading.client import TradingClient
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credential_info = json.loads(settings.google_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credential_info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    trader = TradingClient(settings.alpaca_key, settings.alpaca_secret, paper=settings.paper)
    LOG.info("Buyer running perpetually; daily at noon Denver; paper=%s dry_run=%s", settings.paper, settings.dry_run)
    while True:
        due = next_noon(datetime.now(DENVER))
        LOG.info("Next cycle at %s", due.isoformat())
        while True:
            remaining = (due - datetime.now(DENVER)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 60))
        try:
            LOG.info("Starting scheduled buyer cycle")
            report = run_cycle(sheets, trader, settings, datetime.now(DENVER))
            LOG.info("Cycle finished: %s", report)
        except Exception:
            LOG.exception("Cycle failed; next attempt will be at the next scheduled noon")


if __name__ == "__main__":
    main()
