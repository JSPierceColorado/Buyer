"""V2 live/paper BUYER: 12:00 America/Denver every NYSE trading weekday.

Reads original V2 Screener!G, requiring a same-day verified 9AM scan marker.
For each score >=95: buy 2% of remaining margin buying power if unheld.
S&P100 held positions use a persistent six-step once-daily low-water ladder:
5/10/20/40/80/100% of original basis on >=1% successive observed new lows.
No held additions for non-S&P100; filled stops block buys until next Monday.
One deterministic broker order ID per symbol/day; no blind submission retries.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, date, time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from zoneinfo import ZoneInfo

import lowwater_tracker
import deposit_compounder

DENVER=ZoneInfo("America/Denver")
NEW_YORK=ZoneInfo("America/New_York")
SHEET_ID="14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ"
LOG=logging.getLogger("v2-buyer")
CENT=Decimal("0.01")
SCORE_MIN=Decimal("95")
MIN_ORDER=Decimal("1.00")
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
        raise ValueError("Missing broker buying-power field")
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
        addition=Decimal("0")  # Legacy settings field, no longer used for held additions.
    )
    if not all((s.alpaca_key,s.alpaca_secret,s.google_credentials_json,s.sheet_id)):
        raise ValueError("Missing broker or Sheet credentials")
    if not SCORE_MIN<=s.threshold<=100:
        raise ValueError("Score threshold must be between 95 and 100")
    if not 0<s.fraction<=Decimal(".02"):
        raise ValueError("Buying-power fraction must be 0-2%")
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


def verified_screener_snapshot(sheets,s:Settings,now:datetime)->tuple[list[tuple[str,Decimal]],set[str]]:
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
    return ordered,valid


def verified_screener_signals(sheets,s:Settings,now:datetime)->list[tuple[str,Decimal]]:
    return verified_screener_snapshot(sheets,s,now)[0]


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
    """Inspect equity positions and open orders safely.

    A broker account may also hold options, crypto, and other instrument symbols
    that are not S&P 100 equity tickers. Those should never block V2's equity
    buyer. The verified screener only supplies normalized US-equity tickers.
    """
    from alpaca.trading.enums import QueryOrderStatus, AssetClass
    from alpaca.trading.requests import GetOrdersRequest
    positions=trader.get_all_positions()
    if positions is None:
        raise RuntimeError("PAPER_OR_LIVE_POSITIONS_NOT_VERIFIED")
    held={}
    ignored_positions=0
    for position in positions:
        asset_class=getattr(position,"asset_class",None)
        if asset_class is not None and asset_class!=AssetClass.US_EQUITY:
            ignored_positions+=1
            continue
        try:
            symbol=normalize_symbol(position.symbol)
        except ValueError:
            # Invalid/unsupported symbols cannot match validated screener rows.
            ignored_positions+=1
            LOG.warning("V2_POSITION_SKIPPED_UNSUPPORTED_SYMBOL asset_class=%s",asset_class)
            continue
        try:
            qty=Decimal(str(position.qty))
        except (InvalidOperation,ValueError,TypeError):
            raise RuntimeError("V2_INVALID_EQUITY_POSITION_QUANTITY") from None
        if not qty.is_finite():
            raise RuntimeError("V2_NONFINITE_EQUITY_POSITION_QUANTITY")
        if qty==0:
            continue
        held[symbol]=qty
    orders=trader.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,limit=500))
    if orders is None or len(orders)>=500:
        raise RuntimeError("OPEN_BROKER_ORDERS_UNVERIFIED")
    pending=set()
    ignored_orders=0
    for order in orders:
        try:
            pending.add(normalize_symbol(order.symbol))
        except ValueError:
            ignored_orders+=1
            continue
    LOG.info("V2_BROKER_POSITION_CHECK held_equities=%d open_equity_orders=%d ignored_positions=%d ignored_orders=%d",
             len(held),len(pending),ignored_positions,ignored_orders)
    return held,pending


def available_buying_power(account)->Decimal:
    if any(bool(getattr(account,k,False)) for k in (
            "account_blocked","trading_blocked","trade_suspended_by_user")):
        raise RuntimeError("BROKER_ACCOUNT_RESTRICTED")
    status=str(getattr(account,"status",""))
    if not status.upper().endswith("ACTIVE"):
        raise RuntimeError("BROKER_ACCOUNT_NOT_ACTIVE")
    # Margin buying power, as reported by Alpaca; individual assets may have stricter requirements.
    return money(getattr(account,"buying_power",None))


def buying_power_order_amount(remaining:Decimal,fraction:Decimal,held:bool)->Decimal:
    if held:
        return Decimal(0)  # Additions need fresh validated broker price and entry basis.
    amount=(remaining*fraction).quantize(CENT,rounding=ROUND_DOWN)
    return amount if amount>=MIN_ORDER else Decimal(0)


def stopped_this_week(trader,day:str)->set[str]:
    """Only filled V2 stop sells; Monday ET begins a fresh eligible week.

    No local state sharing or inferred reasons: v2sl is the stop-only ID.
    Any incomplete order-history response fails closed rather than re-buying.
    """
    from alpaca.trading.enums import QueryOrderStatus,OrderSide
    from alpaca.trading.requests import GetOrdersRequest
    current_day=date.fromisoformat(day)
    monday=current_day-timedelta(days=current_day.weekday())
    start=datetime.combine(monday,time.min,tzinfo=NEW_YORK)
    orders=trader.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.ALL,limit=500,after=start,
        side=OrderSide.SELL,nested=False))
    if orders is None or len(orders)>=500:
        raise RuntimeError("WEEKLY_STOP_ORDER_HISTORY_UNVERIFIABLE")
    stopped=set()
    for order in orders:
        cid=str(getattr(order,"client_order_id","") or "").lower()
        if not re.fullmatch(r"v2sl-\d{8}-[a-z0-9-]+",cid):
            continue
        filled=Decimal(str(getattr(order,"filled_qty",None)))
        if not filled.is_finite() or filled<0:
            raise RuntimeError("WEEKLY_STOP_FILL_QUANTITY_INVALID")
        if filled==0:
            continue
        submitted=getattr(order,"submitted_at",None)
        if submitted is None or submitted.tzinfo is None or submitted<start:
            raise RuntimeError("WEEKLY_STOP_ORDER_DATE_UNVERIFIABLE")
        stopped.add(normalize_symbol(order.symbol))
    return stopped


def buy_signals(trader,signals:list[tuple[str,Decimal]],s:Settings,day:str)->dict:
    from alpaca.trading.enums import OrderSide,TimeInForce,AssetClass
    from alpaca.trading.requests import MarketOrderRequest
    results={"qualified":len(signals),"submitted":[],"preview":[],"skipped":[]}
    if not signals:return results
    account=trader.get_account()
    remaining=available_buying_power(account)
    _,pending=held_and_pending(trader)
    stopped=stopped_this_week(trader,day)
    if stopped:
        LOG.info("WEEKLY_STOP_COOLDOWN blocked=%s week_start_ny=%s",
                 sorted(stopped),day)
    for symbol,score in signals:
        if not trader.get_clock().is_open:
            LOG.warning("MARKET_CLOSED_DURING_CYCLE; stopping")
            break
        # Every symbol must pass fresh broker checks, not assumptions from a sheet.
        held,current_pending=held_and_pending(trader)
        if symbol in stopped:
            results["skipped"].append((symbol,"STOPPED_THIS_WEEK"))
            continue
        if symbol in pending or symbol in current_pending:
            results["skipped"].append((symbol,"OPEN_ORDER"))
            continue
        if symbol in held and held[symbol]<0:
            results["skipped"].append((symbol,"SHORT_POSITION_NOT_ELIGIBLE"))
            continue
        account=trader.get_account()
        remaining=min(remaining,available_buying_power(account))
        if remaining<MIN_ORDER:
            results["skipped"].append((symbol,"NO_BUYING_POWER"))
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
        if symbol in held and held[symbol]>0:
            results["skipped"].append((symbol,"HELD_HANDLED_BY_LOWWATER"))
            continue
        amount=buying_power_order_amount(remaining,s.fraction,False)
        if amount<MIN_ORDER:
            results["skipped"].append((symbol,"INSUFFICIENT_BUYING_POWER_FOR_RULE"))
            continue
        if s.dry_run:
            results["preview"].append((symbol,str(score),str(amount),
                                       "NEW_ENTRY"))
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
                                        "NEW_ENTRY"))
            LOG.warning("SUBMITTED_%s symbol=%s score=%s dollars=%s client_id=%s",
                "LIVE" if not s.paper else "PAPER",symbol,score,amount,cid)
        remaining-=amount  # Reserve locally even if Alpaca buying power has not updated.
    LOG.info("V2_BUY_CYCLE qualified=%s ordered=%d preview=%d skipped=%d mode=%s",
             len(signals),len(results["submitted"]),len(results["preview"]),
             len(results["skipped"]),"DRY_RUN" if s.dry_run else ("PAPER" if s.paper else "LIVE"))
    return results



def run_lowwater_sweep(trader,members:set[str],s:Settings,session:str)->dict:
    """Daily low-water samples and S&P100-only held additions; no score gate."""
    from alpaca.trading.enums import OrderSide,TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    positions=trader.get_all_positions()
    if positions is None:raise RuntimeError("LOWWATER_POSITIONS_UNVERIFIABLE")
    if not positions:
        # Soft double-check: a single empty broker result is not proof that
        # all holdings vanished. A second empty result still requires a
        # different trading session before the persistent ledger is cleared.
        retry=trader.get_all_positions()
        if retry is None:raise RuntimeError("LOWWATER_EMPTY_PORTFOLIO_RECHECK_FAILED")
        if retry:
            LOG.warning("LOWWATER_TRANSIENT_EMPTY_PORTFOLIO recovered_after_immediate_recheck=TRUE")
            positions=retry
        else:
            ready=(lowwater_tracker.confirm_portfolio_snapshot(
                session,empty=True,path=lowwater_tracker.DB_PATH)
                if not s.dry_run else False)
            if ready:
                lowwater_tracker.drop_absent(set(),path=lowwater_tracker.DB_PATH,session=session)
            LOG.warning("LOWWATER_PORTFOLIO_EMPTY_DOUBLECHECK empty_twice=TRUE retired=%s no_buys=TRUE",
                        ready)
            return {"sampled":0,"seeded":0,"submitted":0,"preview":0,
                    "review_required":0,"skipped":0,
                    "portfolio_empty":True,"records_retired":bool(ready)}
    active=set(); eligible={}
    for p in positions:
        info=lowwater_tracker.position_info(p)
        if info:
            active.add(info["symbol"])
            if info["symbol"] in members:eligible[info["symbol"]]=p
        else:
            sym=str(getattr(p,"symbol","") or "").upper()
            if SYMBOL_RE.fullmatch(sym) and sym in members:
                active.add(sym)  # Invalid quote must not delete its episode.
    if not s.dry_run:
        lowwater_tracker.confirm_portfolio_snapshot(
            session,empty=False,path=lowwater_tracker.DB_PATH)
        lowwater_tracker.drop_absent(active,path=lowwater_tracker.DB_PATH,session=session)
    _,pending=held_and_pending(trader)
    stopped=stopped_this_week(trader,session)
    report={"sampled":0,"seeded":0,"submitted":0,"preview":0,"review_required":0,"skipped":0}
    def lookup(cid):
        from alpaca.common.exceptions import APIError
        try:return trader.get_order_by_client_id(cid)
        except APIError as exc:
            if str(getattr(exc,"status_code",""))=="404":return None
            raise
    for symbol in sorted(eligible):
        position=eligible[symbol]
        report["sampled"]+=1
        plan=lowwater_tracker.observe(position,session,lookup=lookup,
                path=lowwater_tracker.DB_PATH,readonly=s.dry_run)
        status=plan["status"]
        if status=="SEEDED":
            report["seeded"]+=1
            continue
        if status=="REVIEW_REQUIRED":
            report["review_required"]+=1
            LOG.warning("LOWWATER_REVIEW_REQUIRED symbol=%s reason=%s",
                        symbol,plan.get("reason"))
            continue
        if status!="READY":
            report["skipped"]+=1
            continue
        if symbol in stopped or symbol in pending:
            LOG.info("LOWWATER_SKIP_ORDER_GUARD symbol=%s stopped=%s pending=%s",
                     symbol,symbol in stopped,symbol in pending)
            report["skipped"]+=1
            continue
        amount=plan["amount"]
        if available_buying_power(trader.get_account())<amount or not trader.get_clock().is_open:
            report["skipped"]+=1
            continue
        fresh=lowwater_tracker.position_info(trader.get_open_position(symbol))
        initial=lowwater_tracker.position_info(position)
        if (fresh is None or initial is None or fresh["symbol"]!=symbol or
            fresh["price"]>plan["price"] or fresh["qty"]!=initial["qty"]):
            LOG.info("LOWWATER_SKIP_QUOTE_MOVED symbol=%s",symbol)
            report["skipped"]+=1
            continue
        _,pending_now=held_and_pending(trader)
        if symbol in pending_now:
            report["skipped"]+=1
            continue
        if order_already_attempted(trader,plan["cid"]):
            LOG.warning("LOWWATER_EXISTING_ORDER_REVIEW symbol=%s",symbol)
            report["review_required"]+=1
            continue
        if s.dry_run:
            report["preview"]+=1
            continue
        lowwater_tracker.reserve(plan,session,path=lowwater_tracker.DB_PATH)
        request=MarketOrderRequest(symbol=symbol,notional=float(amount),
                    side=OrderSide.BUY,time_in_force=TimeInForce.DAY,
                    client_order_id=plan["cid"])
        try:
            response=trader.submit_order(order_data=request)
        except Exception:
            LOG.error("LOWWATER_ORDER_UNCERTAIN symbol=%s cid=%s",
                      symbol,plan["cid"])
            raise RuntimeError("LOWWATER_ORDER_STATUS_UNCERTAIN_NO_RETRY") from None
        report["submitted"]+=1
        LOG.warning("LOWWATER_LIVE_ORDER_SUBMITTED symbol=%s step=%d/6 amount=%s cid=%s order_id=%s",
                    symbol,plan["step"],amount,plan["cid"],getattr(response,"id","ACK_PENDING"))
    if not s.dry_run:
        overview=lowwater_tracker.audit(path=lowwater_tracker.DB_PATH)
        LOG.info("LOWWATER_DAILY_AUDIT tracked=%d step_counts=%s flagged=%s pending=%s",
                 overview["tracked"],overview["step_counts"],
                 overview["flagged"],overview["pending"])
    LOG.info("LOWWATER_CYCLE_COMPLETE report=%s",report)
    return report


def run_cycle(sheets,trader,s:Settings,now:datetime)->dict:
    if now.astimezone(DENVER).weekday()>=5 or not trader.get_clock().is_open:
        return {"status":"MARKET_CLOSED"}
    signals,members=verified_screener_snapshot(sheets,s,now)
    session=now.astimezone(NEW_YORK).date().isoformat()
    low=run_lowwater_sweep(trader,members,s,session)
    new=buy_signals(trader,signals,s,session)
    return {"status":"OK","lowwater":low,"new_entries":new}


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


def run_core_cycle(s:Settings,trader=None):
    """Cash deposit watcher; uses existing V2 credentials and /data volume.

    Never posts when DRY_RUN is enabled; historical CSD is baselined only.
    This is independent of Screener score and the low-water strategy.
    """
    if trader is None:
        _,trader=clients(s)
    result=deposit_compounder.run(
        trader,path=deposit_compounder.DB_PATH,dry_run=s.dry_run,
        get_csd=lambda:deposit_compounder.fetch_csd(
            s.alpaca_key,s.alpaca_secret,s.paper))
    LOG.info("CORE_DEPOSIT_CYCLE_COMPLETE report=%s",result)
    return result


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
    # Read-only startup check: verify Alpaca buying power and mixed-instrument holdings
    # without placing orders. This catches schema/account failures before noon.
    _,preflight_trader=clients(s)
    preflight_power=available_buying_power(preflight_trader.get_account())
    preflight_held,preflight_pending=held_and_pending(preflight_trader)
    database=lowwater_tracker.connect()
    database.close()
    LOG.info("LOWWATER_STORAGE_READY path=%s steps=6 observation_once_daily=TRUE",
             lowwater_tracker.DB_PATH)
    try:
        # The first successful run seeds the entire historical CSD list
        # without buying; this is vital before any future deposit can trade.
        report=run_core_cycle(s,preflight_trader)
        LOG.info("CORE_DEPOSIT_PREFLIGHT_OK status=%s",report["status"])
    except Exception as exc:
        # The unrelated V2 noon buyer remains operational; core purchases
        # will NOT happen until positive funding/ledger checks succeed.
        LOG.error("CORE_DEPOSIT_PREFLIGHT_FAILED type=%s core_no_trades_until_verified=TRUE",
                  type(exc).__name__)
    LOG.info("V2_BUYER_PREFLIGHT_OK buying_power_positive=%s held_equities=%d pending_equity_orders=%d mode=%s",
             preflight_power>0,len(preflight_held),len(preflight_pending),
             "PAPER" if s.paper else "LIVE")
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler=BlockingScheduler(timezone=DENVER,
        job_defaults={"coalesce":True,"max_instances":1,"misfire_grace_time":1800})
    def scheduled():
        try:
            sheets,trader=clients(s)
            report=run_cycle(sheets,trader,s,datetime.now(DENVER))
            LOG.info("DAILY_NOON_BUYER_COMPLETED report=%s",report)
        except Exception:
            LOG.exception("DAILY_NOON_BUYER_FAILED")
    scheduler.add_job(scheduled,"cron",day_of_week="mon-fri",
        hour=12,minute=0,id="v2_noon_buyer",replace_existing=True)
    def core_scheduled():
        try:run_core_cycle(s)
        except Exception as exc:
            LOG.error("CORE_DEPOSIT_CYCLE_FAILED error_type=%s no_orders_on_uncertainty=TRUE",
                      type(exc).__name__)
    # Monitor during NY market hours, independent of Denver noon score buys.
    # After-hours/weekend deposits await the next market session.
    scheduler.add_job(core_scheduled,"cron",day_of_week="mon-fri",
        hour="9-15",minute="5,15,25,35,45,55",timezone=NEW_YORK,
        id="v2_cash_deposit_compounder",replace_existing=True,
        max_instances=1,coalesce=True)
    LOG.info("CORE_COMPOUNDER_STARTED schedule=every_10min_NY_market_hours "
             "deposit_source=CSD investment=25pct_VT+25pct_SGOV "
             "cash_only=TRUE margin_power_not_funding=TRUE protected_seller=VT,SGOV")
    LOG.info("V2_BUYER_STARTED schedule=12:00 America/Denver mode=%s dry_run=%s "
             "new_entry=2pct_margin_power sp100_lowwater_steps=5_10_20_40_80_100pct_original nonsp100_no_additions=TRUE weekly_stop_cooldown=MONDAY",
             "PAPER" if s.paper else "LIVE",s.dry_run)
    scheduler.start()


if __name__=="__main__":
    main()
