"""V2 live/paper BUYER: 12:00 America/Denver every NYSE trading weekday.

Reads original V2 Screener!G, requiring a same-day verified 9AM scan marker.
For each score >=95: buy 2% of remaining usable CASH if unheld, or $5 if
already held long. Never use margin buying power, leverage, shorts or options.
One deterministic broker order ID per symbol/day; no blind submission retries.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from zoneinfo import ZoneInfo

DENVER=ZoneInfo("America/Denver")
NEW_YORK=ZoneInfo("America/New_York")
SHEET_ID="14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ"
LOG=logging.getLogger("v2-buyer")
CENT=Decimal("0.01")
SCORE_MIN=Decimal("95")
MIN_ORDER=Decimal("1.00")
ADD_DOLLARS=Decimal("5.00")
SYMBOL_RE=re.compile(r"[A-Z][A-Z0-9.\-]{0,10}")


@dataclass(frozen=True)
class Settings:
    alpaca_key:str
    alpaca_secret:str
    google_credentials_json:str
    sheet_id:str
    paper:bool
    dry_run:bool
    threshold:Decimal
    fraction:Decimal
    addition:Decimal


def flag(name:str,default:bool)->bool:
    value=os.getenv(name,str(default)).lower().strip()
    if value not in ("true","false","1","0"):
        raise ValueError(name+" must be a boolean")
    return value in ("true","1")


def money(value)->Decimal:
    if value is None or value=="":
        raise ValueError("Missing broker cash or non-margin buying-power field")
    try:
        d=Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid cash figure") from None
    if not d.is_finite():
        raise ValueError("Nonfinite cash figure")
    return max(Decimal(0),d)


def settings_from_env()->Settings:
    s=Settings(
        alpaca_key=os.getenv("APCA_API_KEY_ID","").strip(),
        alpaca_secret=os.getenv("APCA_API_SECRET_KEY","").strip(),
        google_credentials_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON","").strip(),
        sheet_id=os.getenv("GOOGLE_SPREADSHEET_ID",SHEET_ID).strip(),
        paper=flag("ALPACA_PAPER",True),
        dry_run=flag("DRY_RUN",True),
        threshold=money(os.getenv("MINIMUM_SCORE","95")),
        fraction=money(os.getenv("BUY_FRACTION","0.02")),
        addition=money(os.getenv("HELD_ADDITION_USD","5.00"))
    )
    if not all((s.alpaca_key,s.alpaca_secret,s.google_credentials_json,s.sheet_id)):
        raise ValueError("Missing broker or Sheet credentials")
    if not SCORE_MIN<=s.threshold<=100:
        raise ValueError("Score threshold must be between 95 and 100")
    if not 0<s.fraction<=Decimal(".02"):
        raise ValueError("Cash fraction must be 0-2%")
    if s.addition!=ADD_DOLLARS:
        raise ValueError("Held addition must be exactly $5")
    if not s.paper and not s.dry_run:
        if os.getenv("LIVE_TRADING_ACK")!="I_ACCEPT_REAL_MONEY_BUYS":
            raise ValueError("Explicit LIVE_TRADING_ACK required for actual real-money orders")
    return s


def next_noon(now:datetime)->datetime:
    local=now.astimezone(DENVER)
    for days in range(8):
        day=local.date()+timedelta(days=days)
        if day.weekday()>=5:
            continue
        target=datetime(day.year,day.month,day.day,12,0,tzinfo=DENVER)
        if target>local:
            return target
    raise RuntimeError("Could not determine weekday noon")


def normalize_symbol(value)->str:
    text=str(value or "").strip().upper().replace("/","")
    if not SYMBOL_RE.fullmatch(text):
        raise ValueError("Invalid stock ticker")
    return text


def verified_screener_signals(sheets, s:Settings, now:datetime)->list[tuple[str,Decimal]]:
    """Read a single A:J values snapshot; fail closed if 9AM scan not current."""
    response=sheets.spreadsheets().values().get(
        spreadsheetId=s.sheet_id,range="'Screener'!A1:J150",
        valueRenderOption="UNFORMATTED_VALUE").execute(num_retries=3)
    rows=response.get("values",[])
    if not rows or len(rows[0])<10 or rows[0][:7]!=[
            "symbol","close","sma_200","sma_50","pos_52w","dollar_vol_m","score"]:
        raise RuntimeError("V2_SCREENER_SCHEMA_MISMATCH")
    if rows[0][8:]!=["last_scan_utc","completed_session_date"]:
        raise RuntimeError("V2_SCREENER_NO_VERIFIED_SCAN_STAMP")
    if len(rows)<2 or len(rows[1])<10:
        raise RuntimeError("V2_SCREENER_STAMP_EMPTY")
    try:
        scanned=datetime.fromisoformat(str(rows[1][8]).replace("Z","+00:00"))
        asof=datetime.fromisoformat(str(rows[1][9])).date()
    except ValueError:
        raise RuntimeError("V2_SCREENER_STAMP_INVALID") from None
    if scanned.tzinfo is None:
        raise RuntimeError("V2_SCREENER_STAMP_NAIVE")
    current=now.astimezone(DENVER)
    age=(now.astimezone(timezone.utc)-scanned.astimezone(timezone.utc)).total_seconds()
    if (scanned.astimezone(DENVER).date()!=current.date()
        or age<0 or age>6*3600):
        raise RuntimeError("V2_SCREENER_NOT_REFRESHED_TODAY")
    current_ny=now.astimezone(NEW_YORK).date()
    if not (asof<current_ny and 0<(current_ny-asof).days<=7):
        raise RuntimeError("V2_SCREENER_COMPLETED_DATA_STALE")

    candidates={}
    valid=set()
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        try:
            sym=normalize_symbol(row[0])
        except ValueError:
            continue
        if sym in valid:
            raise RuntimeError("V2_SCREENER_DUPLICATE_SYMBOL")
        if len(row)<7:
            continue
        try:
            metrics=[Decimal(str(x)) for x in row[1:7]]
        except (InvalidOperation,ValueError,TypeError):
            continue
        if not all(x.is_finite() for x in metrics):
            continue
        close,sma200,sma50,pos52,dollar_volume,score=metrics
        if not (close>0 and sma200>0 and sma50>0 and 0<=pos52<=1
                and dollar_volume>=0 and 0<=score<=100):
            continue
        valid.add(sym)
        if score>=s.threshold:
            candidates[sym]=score
    if not 95<=len(valid)<=125:
        raise RuntimeError("V2_SCREENER_UNIVERSE_INCOMPLETE")
    ordered=sorted(candidates.items(),key=lambda x:(-x[1],x[0]))
    LOG.info("V2_SCAN_VERIFIED date=%s completed_session=%s symbols=%d qualified=%d",
             current.date(),asof,len(valid),len(ordered))
    return ordered


def client_order_id(day:str,symbol:str)->str:
    sym=normalize_symbol(symbol).replace(".","-")
    return f"v2b-{day.replace('-','')}-{sym}"[:48]


def order_already_attempted(trader,client_id:str)->bool:
    from alpaca.common.exceptions import APIError
    try:
        return trader.get_order_by_client_id(client_id) is not None
    except APIError as exc:
        if str(getattr(exc,"status_code",""))=="404":
            return False
        raise


def held_and_pending(trader):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    positions=trader.get_all_positions()
    if positions is None:
        raise RuntimeError("PAPER_OR_LIVE_POSITIONS_NOT_VERIFIED")
    held={}
    for position in positions:
        symbol=normalize_symbol(position.symbol)
        qty=Decimal(str(position.qty))
        if not qty.is_finite() or qty==0:
            continue
        held[symbol]=qty
    orders=trader.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,limit=500))
    if orders is None or len(orders)>=500:
        raise RuntimeError("OPEN_BROKER_ORDERS_UNVERIFIED")
    pending={normalize_symbol(order.symbol) for order in orders}
    return held,pending


def available_cash(account)->Decimal:
    if any(bool(getattr(account,k,False)) for k in (
            "account_blocked","trading_blocked","trade_suspended_by_user")):
        raise RuntimeError("BROKER_ACCOUNT_RESTRICTED")
    status=str(getattr(account,"status",""))
    if not status.upper().endswith("ACTIVE"):
        raise RuntimeError("BROKER_ACCOUNT_NOT_ACTIVE")
    # Important: do NOT use buying_power; it may be 2x/4x margin leverage.
    cash=money(getattr(account,"cash",None))
    nonmargin=money(getattr(account,"non_marginable_buying_power",None))
    return min(cash,nonmargin)


def cash_order_amount(remaining:Decimal,fraction:Decimal,held:bool)->Decimal:
    if held:
        return ADD_DOLLARS if remaining>=ADD_DOLLARS else Decimal(0)
    amount=(remaining*fraction).quantize(CENT,rounding=ROUND_DOWN)
    return amount if amount>=MIN_ORDER else Decimal(0)


def buy_signals(trader,signals:list[tuple[str,Decimal]],s:Settings,day:str)->dict:
    from alpaca.trading.enums import OrderSide,TimeInForce,AssetClass
    from alpaca.trading.requests import MarketOrderRequest
    results={"qualified":len(signals),"submitted":[],"preview":[],"skipped":[]}
    if not signals:return results
    account=trader.get_account()
    remaining=available_cash(account)
    _,pending=held_and_pending(trader)
    for symbol,score in signals:
        if not trader.get_clock().is_open:
            LOG.warning("MARKET_CLOSED_DURING_CYCLE; stopping")
            break
        # Every symbol must pass fresh broker checks, not assumptions from a sheet.
        held,current_pending=held_and_pending(trader)
        if symbol in pending or symbol in current_pending:
            results["skipped"].append((symbol,"OPEN_ORDER"))
            continue
        if symbol in held and held[symbol]<0:
            results["skipped"].append((symbol,"SHORT_POSITION_NOT_ELIGIBLE"))
            continue
        account=trader.get_account()
        remaining=min(remaining,available_cash(account))
        if remaining<MIN_ORDER:
            results["skipped"].append((symbol,"NO_CASH"))
            break
        asset=trader.get_asset(symbol)
        if (not bool(getattr(asset,"tradable",False))
            or not bool(getattr(asset,"fractionable",False))
            or getattr(asset,"asset_class",None)!=AssetClass.US_EQUITY):
            results["skipped"].append((symbol,"NOT_FRACTIONABLE_US_EQUITY"))
            continue
        cid=client_order_id(day,symbol)
        if order_already_attempted(trader,cid):
            results["skipped"].append((symbol,"BUY_ORDER_ALREADY_ATTEMPTED_TODAY"))
            continue
        held_long=symbol in held and held[symbol]>0
        amount=cash_order_amount(remaining,s.fraction,held_long)
        if amount<MIN_ORDER:
            results["skipped"].append((symbol,"INSUFFICIENT_CASH_FOR_RULE"))
            continue
        if s.dry_run:
            results["preview"].append((symbol,str(score),str(amount),
                                       "ADD_HELD" if held_long else "NEW_ENTRY"))
        else:
            req=MarketOrderRequest(
                symbol=symbol,notional=float(amount),side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,client_order_id=cid)
            try:
                broker_order=trader.submit_order(order_data=req)
            except Exception:
                # Order might have reached Alpaca. Stop; never retry ambiguous POST.
                LOG.exception("BROKER_ORDER_UNCONFIRMED symbol=%s cid=%s; aborting",symbol,cid)
                raise
            results["submitted"].append((symbol,str(score),str(amount),
                                        str(broker_order.id),
                                        "ADD_HELD" if held_long else "NEW_ENTRY"))
            LOG.warning("SUBMITTED_%s symbol=%s score=%s dollars=%s client_id=%s",
                "LIVE" if not s.paper else "PAPER",symbol,score,amount,cid)
        remaining-=amount  # Reserve locally even if Alpaca cash has not updated.
    LOG.info("V2_BUY_CYCLE qualified=%s ordered=%d preview=%d skipped=%d mode=%s",
             len(signals),len(results["submitted"]),len(results["preview"]),
             len(results["skipped"]),"DRY_RUN" if s.dry_run else ("PAPER" if s.paper else "LIVE"))
    return results


def run_cycle(sheets,trader,s:Settings,now:datetime)->dict:
    if now.astimezone(DENVER).weekday()>=5 or not trader.get_clock().is_open:
        return {"status":"MARKET_CLOSED"}
    signals=verified_screener_signals(sheets,s,now)
    return buy_signals(trader,signals,s,now.astimezone(DENVER).date().isoformat())


def clients(s:Settings):
    from alpaca.trading.client import TradingClient
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials=service_account.Credentials.from_service_account_info(
        json.loads(s.google_credentials_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets=build("sheets","v4",credentials=credentials,cache_discovery=False)
    trader=TradingClient(s.alpaca_key,s.alpaca_secret,paper=s.paper)
    return sheets,trader


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-now",action="store_true",
        help="ONE immediate read-only dry-run using the configured account and score Sheet")
    args=parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO"),
        format="%(asctime)s %(levelname)s %(message)s")
    s=settings_from_env()
    if args.check_now:
        s=replace(s,dry_run=True)
        sheets,trader=clients(s)
        report=run_cycle(sheets,trader,s,datetime.now(DENVER))
        LOG.info("MANUAL_BUYER_PREVIEW status=%s",report)
        return
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler=BlockingScheduler(timezone=DENVER,
        job_defaults={"coalesce":True,"max_instances":1,"misfire_grace_time":1800})
    def scheduled():
        try:
            sheets,trader=clients(s)
            report=run_cycle(sheets,trader,s,datetime.now(DENVER))
            LOG.info("DAILY_NOON_BUYER_COMPLETED report=%s",report)
        except Exception as exc:
            LOG.error("DAILY_NOON_BUYER_FAILED type=%s",type(exc).__name__)
    scheduler.add_job(scheduled,"cron",day_of_week="mon-fri",
        hour=12,minute=0,id="v2_noon_buyer",replace_existing=True)
    LOG.info("V2_BUYER_STARTED schedule=12:00 America/Denver mode=%s dry_run=%s "
             "new_entry=2pct_remaining_cash existing_long_add=$5",
             "PAPER" if s.paper else "LIVE",s.dry_run)
    scheduler.start()


if __name__=="__main__":
    main()
