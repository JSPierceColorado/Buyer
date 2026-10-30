
"""V2 Buyer Bot — enhanced financial and asset logging.

Runs once daily at noon America/Denver on weekdays while Railway
stays online.

Trading behavior:
- Reads qualifying symbols from Google Sheets, columns A and G.
- Does not edit Google Sheets or score formulas.
- Uses Alpaca buying power to size each purchase.
- Default minimum score: 95.
- Default purchase size: 2% of available buying power.
- Never intentionally submits duplicate same-day symbol orders.
- Skips assets already held or with open orders.
- Only purchases tradable, fractionable assets.
- Preserves PAPER, DRY_RUN, and live-trading safeguards.

Logging improvements:
- Company name, asset class, and exchange.
- Signal score.
- Account buying power, cash, and equity.
- Order dollars and percentage of buying power.
- Estimated buying power after order.
- Detailed skip reasons.
- Order identifiers and initial submission status.
- Structured BUYER_EVENT logs.
- End-of-cycle financial and activity summary.

Important:
A submitted order is not necessarily filled.
Execution results must be checked separately.
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


# ============================================================
# CONFIGURATION
# ============================================================

DENVER = ZoneInfo("America/Denver")

SHEET_ID = "14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ"

LOG = logging.getLogger("v2-buyer")

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


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
        alpaca_key=os.getenv(
            "APCA_API_KEY_ID", ""
        ).strip(),

        alpaca_secret=os.getenv(
            "APCA_API_SECRET_KEY", ""
        ).strip(),

        google_credentials_json=os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_JSON", ""
        ).strip(),

        sheet_id=os.getenv(
            "GOOGLE_SPREADSHEET_ID", SHEET_ID
        ).strip(),

        sheet_tab=os.getenv(
            "GOOGLE_SHEET_TAB", "Screener"
        ).strip(),

        paper=flag("ALPACA_PAPER", True),

        dry_run=flag("DRY_RUN", True),

        threshold=Decimal(
            os.getenv("MINIMUM_SCORE", "95")
        ),

        fraction=Decimal(
            os.getenv("BUY_FRACTION", "0.02")
        ),
    )

    if not all([
        settings.alpaca_key,
        settings.alpaca_secret,
        settings.google_credentials_json,
    ]):
        raise ValueError(
            "Missing Alpaca keys or "
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        )

    if (
        not settings.threshold.is_finite()
        or not 0 <= settings.threshold <= 100
    ):
        raise ValueError(
            "MINIMUM_SCORE must be between 0 and 100"
        )

    if (
        not settings.fraction.is_finite()
        or not 0 < settings.fraction <= Decimal("0.02")
    ):
        raise ValueError(
            "BUY_FRACTION must be > 0 and <= 0.02"
        )

    if (
        not settings.paper
        and not settings.dry_run
        and os.getenv("LIVE_TRADING_ACK")
        != "I_ACCEPT_LIVE_MARGIN_RISK"
    ):
        raise ValueError(
            "Live orders need "
            "LIVE_TRADING_ACK=I_ACCEPT_LIVE_MARGIN_RISK"
        )

    return settings


# ============================================================
# LOGGING HELPERS
# ============================================================

def money(value: object) -> str:
    """Format a dollar amount to two decimal places."""
    amount = Decimal(str(value))

    if not amount.is_finite():
        raise ValueError("Non-finite monetary value")

    return str(
        amount.quantize(CENT, rounding=ROUND_DOWN)
    )


def percentage(
    numerator: Decimal,
    denominator: Decimal,
) -> str:
    """Return a percentage formatted to four decimals."""
    if denominator <= 0:
        return "0.0000"

    result = (
        numerator / denominator * Decimal("100")
    )

    return str(
        result.quantize(
            Decimal("0.0001"),
            rounding=ROUND_DOWN,
        )
    )


def enum_value(value: object) -> str:
    """Return readable values from Alpaca enums."""
    if value is None:
        return "unknown"

    return str(getattr(value, "value", value))


def log_event(event: str, **fields) -> None:
    """Produce searchable, structured Railway logs."""
    payload = {
        "event": event,
        "bot": "V2_BUYER",
        "timestamp_denver": datetime.now(
            DENVER
        ).isoformat(),
        **fields,
    }

    LOG.info(
        "BUYER_EVENT %s",
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
        ),
    )


def account_snapshot(account) -> dict:
    """Capture Alpaca account balances before a decision."""
    return {
        "buying_power": Decimal(
            str(account.buying_power)
        ),
        "cash": Decimal(
            str(account.cash)
        ),
        "equity": Decimal(
            str(account.equity)
        ),
    }


def asset_snapshot(asset, symbol: str) -> dict:
    """Capture basic security details from Alpaca."""
    return {
        "symbol": symbol,
        "asset_name": getattr(
            asset, "name", None
        ) or symbol,
        "asset_class": enum_value(
            getattr(asset, "asset_class", None)
        ),
        "exchange": enum_value(
            getattr(asset, "exchange", None)
        ),
    }


# ============================================================
# SCHEDULER
# ============================================================

def next_noon(now: datetime) -> datetime:
    """Find next weekday at noon in Denver, including DST."""
    local_now = now.astimezone(DENVER)

    for days in range(8):
        day = local_now.date() + timedelta(days=days)

        if day.weekday() >= 5:
            continue

        target = datetime.combine(
            day,
            clock_time(12, 0),
            tzinfo=DENVER,
        )

        if target > local_now:
            return target

    raise RuntimeError("Could not find next weekday")


# ============================================================
# GOOGLE SHEETS SIGNAL READER
# ============================================================

def symbol_key(value: object) -> str:
    return (
        str(value or "")
        .upper()
        .strip()
        .replace("/", ".")
    )


def read_signals(
    sheets,
    settings: Settings,
) -> list[tuple[str, Decimal]]:
    """Read column A ticker and column G score."""

    tab = (
        "'"
        + settings.sheet_tab.replace("'", "''")
        + "'"
    )

    response = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=settings.sheet_id,
            range=f"{tab}!A1:G500",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )

    rows = response.get("values", [])

    if (
        not rows
        or len(rows[0]) < 7
        or symbol_key(rows[0][0]) != "SYMBOL"
        or symbol_key(rows[0][6]) != "SCORE"
    ):
        raise ValueError(
            "Expected column A header 'symbol' "
            "and column G header 'score'"
        )

    qualified: dict[str, Decimal] = {}

    for row in rows[1:]:
        if (
            len(row) < 7
            or not row[0]
            or row[6] in (None, "")
        ):
            continue

        symbol = symbol_key(row[0])

        if not re.fullmatch(
            r"[A-Z][A-Z0-9.\-]{0,11}",
            symbol,
        ):
            LOG.warning(
                "Skipping invalid symbol: %r",
                row[0],
            )
            continue

        try:
            score = Decimal(str(row[6]))
        except (InvalidOperation, ValueError):
            continue

        if (
            score.is_finite()
            and settings.threshold <= score <= 100
        ):
            qualified[symbol] = max(
                score,
                qualified.get(symbol, Decimal("0")),
            )

    signals = sorted(
        qualified.items(),
        key=lambda item: (-item[1], item[0]),
    )

    log_event(
        "SIGNALS_LOADED",
        qualified_count=len(signals),
        minimum_score=str(settings.threshold),
        symbols=[
            {"symbol": s, "score": str(score)}
            for s, score in signals
        ],
    )

    return signals


# ============================================================
# ORDER SIZING
# ============================================================

def size_from_buying_power(
    buying_power: object,
    fraction: Decimal,
) -> Decimal:
    value = Decimal(str(buying_power))

    if not value.is_finite() or value <= 0:
        return ZERO

    return (
        value * fraction
    ).quantize(
        CENT,
        rounding=ROUND_DOWN,
    )


# ============================================================
# DUPLICATE ORDER PROTECTION
# ============================================================

def client_order_id(
    day: str,
    symbol: str,
) -> str:
    return (
        f"v2sp100-"
        f"{day.replace('-', '')}-"
        f"{symbol.replace('.', '-')}"
    )[:48]


def order_already_attempted(
    trader,
    client_id: str,
) -> bool:
    from alpaca.common.exceptions import APIError

    try:
        trader.get_order_by_client_id(client_id)
        return True

    except APIError as exc:
        if exc.status_code == 404:
            return False

        raise


def held_and_pending(
    trader,
) -> tuple[set[str], set[str]]:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    held = {
        symbol_key(p.symbol)
        for p in trader.get_all_positions()
    }

    orders = trader.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            limit=500,
        )
    )

    if len(orders) >= 500:
        raise RuntimeError(
            "Too many pending orders to verify safely"
        )

    pending = {
        symbol_key(o.symbol)
        for o in orders
    }

    return held, pending


# ============================================================
# BUYING ENGINE
# ============================================================

def buy_signals(
    trader,
    signals: list[tuple[str, Decimal]],
    settings: Settings,
    date: str,
) -> dict:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    results = {
        "qualified": len(signals),
        "ordered": [],
        "dry_run": [],
        "skipped": [],
        "stopped_early": False,
        "stop_reason": None,
    }

    total_submitted = ZERO
    total_simulated = ZERO

    for symbol, score in signals:
        order_stage = "PRECHECK"

        try:
            # ------------------------------------------------
            # MARKET STATUS
            # ------------------------------------------------

            if not trader.get_clock().is_open:
                results["stopped_early"] = True
                results["stop_reason"] = "Market closed"

                log_event(
                    "MARKET_CLOSED",
                    symbol=symbol,
                    reason="Market closed during processing",
                )
                break

            # ------------------------------------------------
            # EXISTING POSITIONS AND ORDERS
            # ------------------------------------------------

            held, pending = held_and_pending(trader)
            cid = client_order_id(date, symbol)

            if symbol in held:
                reason = "Already held"

            elif symbol in pending:
                reason = "Open order already exists"

            elif order_already_attempted(trader, cid):
                reason = "Already attempted today"

            else:
                reason = None

            if reason:
                log_event(
                    "BUY_SKIPPED",
                    symbol=symbol,
                    score=str(score),
                    reason=reason,
                )

                results["skipped"].append(
                    (symbol, reason)
                )
                continue

            # ------------------------------------------------
            # ASSET INFORMATION
            # ------------------------------------------------

            asset = trader.get_asset(symbol)
            info = asset_snapshot(asset, symbol)

            info["score"] = str(score)

            if not asset.tradable or not asset.fractionable:
                reason = "Not tradable or not fractionable"

                log_event(
                    "BUY_SKIPPED",
                    **info,
                    tradable=asset.tradable,
                    fractionable=asset.fractionable,
                    reason=reason,
                )

                results["skipped"].append(
                    (symbol, reason)
                )
                continue

            # ------------------------------------------------
            # ACCOUNT BALANCES
            # ------------------------------------------------

            account = trader.get_account()

            if any(
                getattr(account, key, False)
                for key in (
                    "account_blocked",
                    "trading_blocked",
                    "trade_suspended_by_user",
                )
            ):
                raise RuntimeError(
                    "Alpaca account restricted from trading"
                )

            snapshot = account_snapshot(account)

            buying_power = snapshot["buying_power"]
            cash = snapshot["cash"]
            equity = snapshot["equity"]

            # ------------------------------------------------
            # PURCHASE CALCULATION
            # ------------------------------------------------

            amount = size_from_buying_power(
                buying_power,
                settings.fraction,
            )

            bp_used_pct = percentage(
                amount,
                buying_power,
            )

            cash_used_pct = percentage(
                amount,
                cash,
            )

            equity_used_pct = percentage(
                amount,
                equity,
            )

            estimated_bp_after = buying_power - amount

            financials = {
                "buying_power_before_usd": money(
                    buying_power
                ),
                "cash_before_usd": money(cash),
                "account_equity_usd": money(equity),
                "order_amount_usd": money(amount),
                "pct_buying_power_used": bp_used_pct,
                "pct_cash_equivalent": cash_used_pct,
                "pct_account_equity": equity_used_pct,
                "estimated_buying_power_after_usd": money(
                    estimated_bp_after
                ),
                "configured_buy_fraction": str(
                    settings.fraction
                ),
            }

            # ------------------------------------------------
            # INSUFFICIENT BUYING POWER
            # ------------------------------------------------

            if amount < Decimal("1"):
                reason = "Buying power too low"

                log_event(
                    "BUY_SKIPPED",
                    **info,
                    **financials,
                    reason=reason,
                )

                results["skipped"].append(
                    (symbol, reason)
                )
                continue

            # ------------------------------------------------
            # HUMAN-READABLE DECISION LOG
            # ------------------------------------------------

            LOG.info(
                "BUY DECISION | %s (%s) | "
                "score=%s | "
                "amount=$%s | "
                "buying_power_used=%s%% | "
                "buying_power=$%s | "
                "cash=$%s | "
                "equity=$%s | "
                "mode=%s",
                symbol,
                info["asset_name"],
                score,
                money(amount),
                bp_used_pct,
                money(buying_power),
                money(cash),
                money(equity),
                "DRY_RUN" if settings.dry_run else "LIVE_SUBMISSION",
            )

            # ------------------------------------------------
            # DRY RUN
            # ------------------------------------------------

            if settings.dry_run:
                total_simulated += amount

                log_event(
                    "BUY_DRY_RUN",
                    **info,
                    **financials,
                    client_order_id=cid,
                    status="SIMULATED_NOT_SUBMITTED",
                    paper=settings.paper,
                )

                results["dry_run"].append(
                    (symbol, str(amount))
                )

                continue

            # ------------------------------------------------
            # SUBMIT ORDER
            # ------------------------------------------------

            order_stage = "SUBMITTING"

            order = trader.submit_order(
                order_data=MarketOrderRequest(
                    symbol=symbol,
                    notional=float(amount),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=cid,
                )
            )

            order_stage = "SUBMITTED"

            total_submitted += amount

            order_status = enum_value(
                getattr(order, "status", None)
            )

            filled_qty = getattr(
                order, "filled_qty", None
            )

            filled_avg_price = getattr(
                order, "filled_avg_price", None
            )

            log_event(
                "BUY_SUBMITTED",
                **info,
                **financials,
                order_id=str(order.id),
                client_order_id=cid,
                order_status=order_status,
                submitted_notional_usd=money(amount),
                filled_qty=(
                    str(filled_qty)
                    if filled_qty is not None
                    else "0"
                ),
                filled_avg_price=(
                    str(filled_avg_price)
                    if filled_avg_price is not None
                    else None
                ),
                fill_confirmed=(
                    order_status == "filled"
                ),
                paper=settings.paper,
            )

            LOG.info(
                "ORDER SUBMITTED | %s | "
                "$%s | %s%% of buying power | "
                "order_id=%s | status=%s",
                symbol,
                money(amount),
                bp_used_pct,
                order.id,
                order_status,
            )

            results["ordered"].append(
                (symbol, str(amount), str(order.id))
            )

        except Exception as exc:
            # An ambiguous submission must NEVER be
            # blindly retried.

            results["stopped_early"] = True
            results["stop_reason"] = (
                f"{type(exc).__name__}: {exc}"
            )

            log_event(
                "BUY_PROCESSING_STOPPED",
                symbol=symbol,
                score=str(score),
                order_stage=order_stage,
                error_type=type(exc).__name__,
                reason=str(exc),
                manual_reconciliation_required=(
                    order_stage in (
                        "SUBMITTING",
                        "SUBMITTED",
                    )
                ),
            )

            LOG.exception(
                "Order processing stopped at %s",
                symbol,
            )
            break

    # --------------------------------------------------------
    # END-OF-CYCLE SUMMARY
    # --------------------------------------------------------

    results["total_submitted_usd"] = money(
        total_submitted
    )

    results["total_simulated_usd"] = money(
        total_simulated
    )

    log_event(
        "BUY_CYCLE_SUMMARY",
        qualified_assets=results["qualified"],
        orders_submitted=len(results["ordered"]),
        simulated_orders=len(results["dry_run"]),
        skipped_assets=len(results["skipped"]),
        total_submitted_usd=money(total_submitted),
        total_simulated_usd=money(total_simulated),
        stopped_early=results["stopped_early"],
        stop_reason=results["stop_reason"],
        submitted_symbols=[
            item[0] for item in results["ordered"]
        ],
        simulated_symbols=[
            item[0] for item in results["dry_run"]
        ],
        skipped_details=[
            {
                "symbol": item[0],
                "reason": item[1],
            }
            for item in results["skipped"]
        ],
        note=(
            "Submitted dollar amounts are not "
            "confirmed executions or realized P&L."
        ),
    )

    LOG.info(
        "CYCLE COMPLETE | "
        "qualified=%d | "
        "submitted=%d | "
        "simulated=%d | "
        "skipped=%d | "
        "submitted_notional=$%s | "
        "simulated_notional=$%s | "
        "stopped_early=%s",
        results["qualified"],
        len(results["ordered"]),
        len(results["dry_run"]),
        len(results["skipped"]),
        money(total_submitted),
        money(total_simulated),
        results["stopped_early"],
    )

    return results


# ============================================================
# DAILY CYCLE
# ============================================================

def run_cycle(
    sheets,
    trader,
    settings: Settings,
    now: datetime,
) -> dict:

    local_now = now.astimezone(DENVER)

    if (
        local_now.weekday() >= 5
        or not trader.get_clock().is_open
    ):
        log_event(
            "CYCLE_SKIPPED",
            reason="Market closed",
            date=local_now.date().isoformat(),
        )

        return {
            "status": "market-closed"
        }

    log_event(
        "CYCLE_STARTED",
        date=local_now.date().isoformat(),
        paper=settings.paper,
        dry_run=settings.dry_run,
        minimum_score=str(settings.threshold),
        buy_fraction=str(settings.fraction),
    )

    signals = read_signals(
        sheets,
        settings,
    )

    LOG.info(
        "Noon check: %d assets scored >= %s",
        len(signals),
        settings.threshold,
    )

    return buy_signals(
        trader,
        signals,
        settings,
        local_now.date().isoformat(),
    )


# ============================================================
# MAIN APPLICATION
# ============================================================

def main() -> None:

    logging.basicConfig(
        level="INFO",
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(name)s "
            "%(message)s"
        ),
    )

    settings = settings_from_env()

    from alpaca.trading.client import TradingClient
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    # --------------------------------------------------------
    # GOOGLE SHEETS CONNECTION
    # --------------------------------------------------------

    credential_info = json.loads(
        settings.google_credentials_json
    )

    credentials = (
        service_account.Credentials.from_service_account_info(
            credential_info,
            scopes=[
                "https://www.googleapis.com/"
                "auth/spreadsheets.readonly"
            ],
        )
    )

    sheets = build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )

    # --------------------------------------------------------
    # ALPACA CONNECTION
    # --------------------------------------------------------

    trader = TradingClient(
        settings.alpaca_key,
        settings.alpaca_secret,
        paper=settings.paper,
    )

    log_event(
        "BOT_STARTED",
        schedule="Weekdays 12:00 America/Denver",
        paper=settings.paper,
        dry_run=settings.dry_run,
        minimum_score=str(settings.threshold),
        buy_fraction=str(settings.fraction),
        sheet_tab=settings.sheet_tab,
        logging_version="2.0",
    )

    LOG.info(
        "V2 Buyer running perpetually | "
        "daily noon Denver | "
        "paper=%s | dry_run=%s",
        settings.paper,
        settings.dry_run,
    )

    # --------------------------------------------------------
    # PERPETUAL SCHEDULER
    # --------------------------------------------------------

    while True:

        due = next_noon(
            datetime.now(DENVER)
        )

        LOG.info(
            "Next buyer cycle scheduled for %s",
            due.isoformat(),
        )

        while True:
            remaining = (
                due - datetime.now(DENVER)
            ).total_seconds()

            if remaining <= 0:
                break

            time.sleep(
                min(remaining, 60)
            )

        try:
            LOG.info(
                "Starting scheduled V2 buyer cycle"
            )

            report = run_cycle(
                sheets,
                trader,
                settings,
                datetime.now(DENVER),
            )

            LOG.info(
                "Cycle finished: %s",
                json.dumps(
                    report,
                    default=str,
                ),
            )

        except Exception as exc:

            log_event(
                "CYCLE_FAILED",
                error_type=type(exc).__name__,
                reason=str(exc),
                next_attempt="Next scheduled noon",
            )

            LOG.exception(
                "Cycle failed; next attempt will "
                "be at the next scheduled noon"
            )


if __name__ == "__main__":
    main()
