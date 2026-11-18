"""Deposit-funded 50/50 core allocation: 25% VT, 25% SGOV, no margin.

Each new ALPACA CSD (positive cash deposit) contributes at most 50% of
its gross amount, split equally between VT and SGOV. Original position
quantities must be initially zero. Cash and non-marginable buying power
MUST cover each buy. Historical deposits are baselined without trades.

SQLite is hosted on V2-Buyer's existing persistent Railway /data volume.
All broker writes are at most once per deterministic client order ID.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

LOG=logging.getLogger("deposit-compounder")
DB_PATH="/data/v2-core.sqlite3"
CENT=Decimal(".01")
CORE_SYMBOLS=("VT","SGOV")
MIN_ORDER=Decimal("1.00")
PAGE_MAX=15
OPEN={"new","accepted","pending_new","partially_filled","accepted_for_bidding",
      "pending_cancel","pending_replace","calculated","stopped","suspended"}
FINISHED={"filled","canceled","expired","rejected","done_for_day"}


def number(value):
    try:
        n=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):
        return None
    return n if n.is_finite() else None


def money(value):
    value=number(value)
    if value is None:raise RuntimeError("CORE_BROKER_MONEY_UNVERIFIABLE")
    return value


def shares(value):
    value=number(value)
    if value is None or value<0:raise RuntimeError("CORE_BROKER_SHARES_UNVERIFIABLE")
    return value


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
    c=sqlite3.connect(path,timeout=20)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,val TEXT NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS deposits(
        id TEXT PRIMARY KEY,posted_day TEXT NOT NULL,
        gross TEXT NOT NULL,vt TEXT NOT NULL,sgov TEXT NOT NULL,
        state TEXT NOT NULL,observed TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS legs(
        deposit_id TEXT NOT NULL,symbol TEXT NOT NULL,
        cid TEXT NOT NULL UNIQUE,notional TEXT NOT NULL,
        state TEXT NOT NULL,order_id TEXT NOT NULL DEFAULT '',
        filled_qty TEXT NOT NULL DEFAULT '0',
        fill_price TEXT NOT NULL DEFAULT '0',
        PRIMARY KEY(deposit_id,symbol))""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,
        kind TEXT NOT NULL,detail TEXT NOT NULL)""")
    c.commit()
    return c


def log_event(c,kind,detail):
    c.execute("INSERT INTO audit(at,kind,detail) VALUES(?,?,?)",
              (datetime.now(timezone.utc).isoformat(),kind,str(detail)[:550]))


def meta(c,key):
    r=c.execute("SELECT val FROM meta WHERE key=?",(key,)).fetchone()
    return r["val"] if r else None


def store_meta(c,key,val):
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",(key,str(val)))


def order_cid(deposit_id,symbol):
    if symbol not in CORE_SYMBOLS:raise ValueError("CORE_SYMBOL_NOT_ALLOWED")
    return "v2core-"+hashlib.sha256(deposit_id.encode()).hexdigest()[:20]+"-"+symbol


def legs_amounts(gross):
    gross=money(gross)
    if gross<=0:raise ValueError("DEPOSIT_MUST_BE_POSITIVE")
    each=(gross*Decimal(".25")).quantize(CENT,rounding=ROUND_DOWN)
    assert each*2 <= gross*Decimal(".5")
    return {"VT":each,"SGOV":each}


def validate_deposits(raw):
    """Reject mixed, malformed or duplicate activities; don't infer deposits."""
    if not isinstance(raw,list):raise RuntimeError("CORE_CSD_RESPONSE_NOT_LIST")
    seen=set()
    items=[]
    for item in raw:
        if not isinstance(item,dict) or item.get("activity_type")!="CSD":
            raise RuntimeError("CORE_NON_CSD_ACTIVITY_IN_FILTERED_HISTORY")
        ident=str(item.get("id") or "")
        posted=str(item.get("date") or "")[:10]
        gross=number(item.get("net_amount"))
        if (not ident or len(ident)>140 or ident in seen or gross is None
            or gross<=0 or len(posted)!=10):
            raise RuntimeError("CORE_CSD_ACTIVITY_INCOMPLETE_OR_DUPLICATE")
        try:
            datetime.strptime(posted,"%Y-%m-%d")
        except ValueError:
            raise RuntimeError("CORE_CSD_DATE_UNVERIFIABLE") from None
        seen.add(ident)
        items.append({"id":ident,"date":posted,"amount":gross})
    return sorted(items,key=lambda x:(x["date"],x["id"]))


def fetch_csd(key,secret,paper):
    """Read paginated Alpaca Trading API CSD activities; never post transfers."""
    base=("https://paper-api.alpaca.markets" if paper
          else "https://api.alpaca.markets")
    out=[]
    token=""
    seen_tokens=set()
    for _ in range(PAGE_MAX):
        q={"activity_types":"CSD","page_size":"100","direction":"desc"}
        if token:q["page_token"]=token
        url=base+"/v2/account/activities?"+urllib.parse.urlencode(q)
        request=urllib.request.Request(url,headers={
            "APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,
            "Accept":"application/json"})
        with urllib.request.urlopen(request,timeout=20) as response:
            batch=json.load(response)
        if not isinstance(batch,list):raise RuntimeError("CORE_CSD_BAD_RESPONSE")
        out.extend(batch)
        if len(batch)<100:return validate_deposits(out)
        token=str(batch[-1].get("id") or "")
        if not token or token in seen_tokens:
            raise RuntimeError("CORE_CSD_PAGINATION_UNVERIFIABLE")
        seen_tokens.add(token)
    raise RuntimeError("CORE_CSD_PAGINATION_TRUNCATED_NO_BUYS")


def broker_status(value):
    return str(getattr(value,"value",value)).lower()


def seed(c,account,deposits,now,positions):
    """Initial boundary: never spend previously posted cash/deposits."""
    if meta(c,"armed_at"):return False
    ident=str(getattr(account,"id",None) or "")
    if not ident:raise RuntimeError("CORE_ACCOUNT_ID_MISSING")
    if positions is None:raise RuntimeError("CORE_POSITIONS_UNVERIFIABLE")
    # Can't attribute an already-held core ETF to this new deposit program.
    held=[p for p in positions
          if str(getattr(p,"symbol","")).upper() in CORE_SYMBOLS
          and (number(getattr(p,"qty",None)) or Decimal(0))!=0]
    if held:
        LOG.error("CORE_EXISTING_ETFS_REVIEW_REQUIRED symbols=%s; no new core orders",
                  sorted(str(p.symbol) for p in held))
        with c:
            store_meta(c,"existing_core_unattributable","1")
            log_event(c,"EXISTING_CORE","VT_OR_SGOV_HELD_BEFORE_BASELINE")
        return False
    with c:
        if meta(c,"armed_at"):return False
        for d in deposits:
            allocation=legs_amounts(d["amount"])
            c.execute("INSERT OR IGNORE INTO deposits VALUES(?,?,?,?,?,?,?)",
                      (d["id"],d["date"],str(d["amount"]),str(allocation["VT"]),
                       str(allocation["SGOV"]),"HISTORICAL",now.isoformat()))
        store_meta(c,"account_id",ident)
        store_meta(c,"armed_at",now.isoformat())
        store_meta(c,"armed_day",now.date().isoformat())
        log_event(c,"ARMED","historical_deposits="+str(len(deposits)))
    LOG.warning("CORE_BASELINE_ARMED historical_deposits=%d no_historical_buys=TRUE account_verified=TRUE",
                len(deposits))
    return True


def new_deposits(c,deposits,now):
    armed=meta(c,"armed_at")
    if not armed:raise RuntimeError("CORE_NOT_BASELINED")
    if meta(c,"existing_core_unattributable")=="1":
        raise RuntimeError("CORE_PREEXISTING_HOLDINGS_REVIEW_REQUIRED")
    count=0
    with c:
        for d in deposits:
            if c.execute("SELECT 1 FROM deposits WHERE id=?",(d["id"],)).fetchone():
                continue
            # CSD dates have only daily granularity, not intra-day booking.
            # Never treat a backdated record as a new post-arm deposit.
            if d["date"] < meta(c,"armed_day"):
                c.execute("INSERT INTO deposits VALUES(?,?,?,?,?,?,?)",
                   (d["id"],d["date"],str(d["amount"]),"0","0",
                    "LATE_HISTORICAL",now.isoformat()))
                log_event(c,"BACKDATED_CSD_SKIPPED",d["id"])
                continue
            allocation=legs_amounts(d["amount"])
            c.execute("INSERT INTO deposits VALUES(?,?,?,?,?,?,?)",
                   (d["id"],d["date"],str(d["amount"]),
                    str(allocation["VT"]),str(allocation["SGOV"]),"NEW",
                    now.isoformat()))
            for symbol,amt in allocation.items():
                state="PLANNED" if amt>=MIN_ORDER else "DEFERRED_BELOW_MINIMUM"
                c.execute("""INSERT INTO legs(
                    deposit_id,symbol,cid,notional,state)
                    VALUES(?,?,?,?,?)""",
                    (d["id"],symbol,order_cid(d["id"],symbol),str(amt),state))
            count+=1
            LOG.warning("CORE_NEW_CASH_DEPOSIT posted=%s amount=%s core_target=%s VT=%s SGOV=%s",
                        d["date"],d["amount"],sum(allocation.values()),
                        allocation["VT"],allocation["SGOV"])
            log_event(c,"NEW_DEPOSIT",d["date"]+" amount="+str(d["amount"]))
    return count


def order_check(order,row):
    if order is None:return ("UNKNOWN",Decimal(0),Decimal(0),"")
    if (str(getattr(order,"client_order_id",""))!=row["cid"] or
        broker_status(getattr(order,"side",None))!="buy" or
        str(getattr(order,"symbol","")).upper()!=row["symbol"]):
        return ("UNKNOWN",Decimal(0),Decimal(0),"")
    state=broker_status(getattr(order,"status",None))
    filled=number(getattr(order,"filled_qty",None))
    price=number(getattr(order,"filled_avg_price",None))
    if filled is None or filled<0 or (filled>0 and (price is None or price<=0)):
        return ("UNKNOWN",Decimal(0),Decimal(0),"")
    if state in OPEN:return ("OPEN",filled,price or Decimal(0),str(getattr(order,"id","")))
    if state not in FINISHED:return ("UNKNOWN",filled,price or Decimal(0),"")
    if state=="filled" and filled>0:
        return ("FILLED",filled,price,str(getattr(order,"id","")))
    return ("REVIEW",filled,price or Decimal(0),str(getattr(order,"id","")))


def reconcile(c,trader):
    pending=c.execute("SELECT * FROM legs WHERE state IN ('RESERVED','ORDER_PENDING')").fetchall()
    revised=0
    for row in pending:
        try: order=trader.get_order_by_client_id(row["cid"])
        except Exception: order=None
        result,qty,price,oid=order_check(order,row)
        if result=="OPEN":
            with c:
                c.execute("""UPDATE legs SET state='ORDER_PENDING',
                    order_id=?,filled_qty=?,fill_price=? WHERE cid=?""",
                    (oid,str(qty),str(price),row["cid"]))
            continue
        if result=="UNKNOWN":
            # A crash after reserving, or API/network failure, is NOT a safe retry.
            LOG.warning("CORE_ORDER_UNVERIFIABLE cid=%s held_for_review=TRUE",row["cid"])
            continue
        with c:
            c.execute("""UPDATE legs SET state=?,order_id=?,filled_qty=?,
                fill_price=? WHERE cid=?""",
                (result,oid,str(qty),str(price),row["cid"]))
            log_event(c,"LEG_"+result,row["cid"]+" qty="+str(qty))
        revised+=1
        LOG.warning("CORE_ORDER_%s symbol=%s notional=%s qty=%s cid=%s",
                    result,row["symbol"],row["notional"],qty,row["cid"])
    return revised


def health(c):
    rows=c.execute("SELECT state,count(*) AS n FROM legs GROUP BY state").fetchall()
    states={x["state"]:x["n"] for x in rows}
    deposits=c.execute("""SELECT COUNT(*) n,COALESCE(SUM(CAST(gross AS REAL)),0) amount
        FROM deposits WHERE state='NEW'""").fetchone()
    spent=c.execute("""SELECT symbol,COALESCE(SUM(CAST(filled_qty AS REAL)),0) qty,
        COALESCE(SUM(CAST(filled_qty AS REAL)*CAST(fill_price AS REAL)),0) cost
        FROM legs WHERE state='FILLED' GROUP BY symbol""").fetchall()
    return {"new_deposits":deposits["n"],
            "gross_deposits":round(deposits["amount"],2),
            "legs":states,
            "invested":{r["symbol"]:{"qty":round(r["qty"],8),
                                     "cost":round(r["cost"],2)} for r in spent}}


def available_cash(account):
    if (any(bool(getattr(account,x,False)) for x in
         ("trading_blocked","account_blocked","trade_suspended_by_user"))
        or broker_status(getattr(account,"status",None))!="active"):
        raise RuntimeError("CORE_BROKER_ACCOUNT_RESTRICTED")
    # All three checks are mandatory. Margin buying power alone is NOT cash.
    amounts=[money(getattr(account,x,None)) for x in
             ("cash","non_marginable_buying_power","buying_power")]
    outgoing=money(getattr(account,"pending_transfer_out","0") or "0")
    return max(Decimal(0),min(amounts)-max(Decimal(0),outgoing))


def verify_positions(c,trader):
    positions=trader.get_all_positions()
    if positions is None:raise RuntimeError("CORE_POSITIONS_UNVERIFIABLE")
    for p in positions:
        symbol=str(getattr(p,"symbol","")).upper()
        if symbol not in CORE_SYMBOLS:continue
        actual=shares(getattr(p,"qty",None))
        observed=c.execute("""SELECT COALESCE(SUM(CAST(filled_qty AS REAL)),0) as q
            FROM legs WHERE symbol=?""",(symbol,)).fetchone()["q"]
        # Own submitted core orders may have filled since our last scan.
        pending=c.execute("""SELECT COUNT(*) n FROM legs
            WHERE symbol=? AND state IN ('RESERVED','ORDER_PENDING')""",
            (symbol,)).fetchone()["n"]
        if actual>Decimal(str(observed))+Decimal(".000001") and not pending:
            raise RuntimeError("CORE_UNATTRIBUTED_ETF_SHARES_"+symbol)
    return True


def run(trader,*,get_csd,path=DB_PATH,dry_run=False,now=None):
    """No historic funding catch-up; order at most one ETF leg per poll.

    The buyer remains in the same margin account; mandatory account cash and
    non-marginable buying-power checks prevent choosing margin as funding.
    This cannot reserve/segregate cash against an unrelated concurrent bot.
    """
    from alpaca.trading.enums import AssetClass,OrderSide,QueryOrderStatus,TimeInForce
    from alpaca.trading.requests import GetOrdersRequest,MarketOrderRequest
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError("CORE_TIME_NAIVE")
    account=trader.get_account()
    ident=str(getattr(account,"id",None) or "")
    if not ident:raise RuntimeError("CORE_ACCOUNT_ID_MISSING")
    with closing(connect(path)) as c:
        stored_id=meta(c,"account_id")
        if stored_id and stored_id!=ident:
            raise RuntimeError("CORE_ACCOUNT_ID_CHANGED_STOP")
        cash_available=available_cash(account)
        ds=get_csd()
        if not meta(c,"armed_at"):
            # Confirm two stable reads BEFORE trusting the baseline boundary.
            ds2=get_csd()
            if ds!=ds2:raise RuntimeError("CORE_BASELINE_INCONSISTENT_DEPOSIT_HISTORY")
            initialized=seed(c,account,ds,now,trader.get_all_positions())
            return {"status":"BASELINED" if initialized else "REVIEW_REQUIRED",
                    "health":health(c),"orders":0}
        count=new_deposits(c,ds,now)
        revised=reconcile(c,trader)
        if not trader.get_clock().is_open:
            return {"status":"MARKET_CLOSED","new":count,
                    "reconciled":revised,"health":health(c),"orders":0}
        verify_positions(c,trader)
        unresolved=c.execute("""SELECT cid,state FROM legs
            WHERE state IN ('RESERVED','ORDER_PENDING','REVIEW')""").fetchall()
        if unresolved:
            # If one leg's POST or fill is unresolved, don't buy the other
            # ETF or move on to another deposit. Preserve a balanced core.
            LOG.warning("CORE_OUTSTANDING_ORDER_REVIEW count=%d no_new_orders=TRUE",
                        len(unresolved))
            return {"status":"RECONCILIATION_REQUIRED","new":count,
                    "orders":0,"health":health(c)}
        # One OPEN order, even for another strategy, may consume cash. Wait.
        orders=trader.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN,limit=500,nested=True))
        if orders is None or len(orders)>=500:
            raise RuntimeError("CORE_OPEN_ORDERS_UNVERIFIABLE")
        if orders:
            LOG.info("CORE_DEFER_OPEN_ORDERS count=%d",len(orders))
            return {"status":"DEFER_OPEN_ORDERS","new":count,"orders":0,"health":health(c)}
        row=c.execute("""SELECT * FROM legs WHERE state='PLANNED'
            ORDER BY deposit_id,CASE symbol WHEN 'VT' THEN 0 ELSE 1 END LIMIT 1""").fetchone()
        if row is None:
            return {"status":"NO_ACTION","new":count,"orders":0,"health":health(c)}
        amount=money(row["notional"])
        # Re-read before each order; do not spend half a deposit on margin.
        fresh=trader.get_account()
        if available_cash(fresh)<amount:
            LOG.warning("CORE_DEFER_INSUFFICIENT_CASH symbol=%s needed=%s verified_cash=%s",
                        row["symbol"],amount,available_cash(fresh))
            return {"status":"DEFER_CASH","new":count,"orders":0,"health":health(c)}
        asset=trader.get_asset(row["symbol"])
        if (not bool(getattr(asset,"tradable",False)) or
            not bool(getattr(asset,"fractionable",False)) or
            broker_status(getattr(asset,"asset_class",None))!="us_equity" or
            broker_status(getattr(asset,"status",None))!="active"):
            LOG.warning("CORE_ETF_ASSET_NOT_TRADABLE symbol=%s",row["symbol"])
            return {"status":"DEFER_ASSET","orders":0,"health":health(c)}
        if dry_run:
            LOG.info("CORE_PREVIEW symbol=%s amount=%s cash_available=%s",
                     row["symbol"],amount,cash_available)
            return {"status":"PREVIEW","symbol":row["symbol"],
                    "amount":str(amount),"orders":0,"health":health(c)}
        # Durable reservation BEFORE POST. Any network uncertainty is held for
        # broker reconciliation; no blind second order will occur.
        with c:
            cur=c.execute("""UPDATE legs SET state='RESERVED'
                WHERE cid=? AND state='PLANNED'""",(row["cid"],))
            if cur.rowcount!=1:raise RuntimeError("CORE_ORDER_RESERVATION_CONFLICT")
            log_event(c,"ORDER_RESERVED",row["cid"]+" "+str(amount))
        req=MarketOrderRequest(symbol=row["symbol"],notional=float(amount),
             side=OrderSide.BUY,time_in_force=TimeInForce.DAY,
             client_order_id=row["cid"])
        try:order=trader.submit_order(order_data=req)
        except Exception:
            LOG.error("CORE_ORDER_UNCERTAIN_NO_RETRY cid=%s",row["cid"])
            raise RuntimeError("CORE_ORDER_UNCERTAIN_NO_RETRY") from None
        with c:
            c.execute("UPDATE legs SET state='ORDER_PENDING',order_id=? WHERE cid=?",
                      (str(getattr(order,"id","")),row["cid"]))
        LOG.warning("CORE_BUY_SUBMITTED symbol=%s notional=%s cid=%s order_id=%s",
                    row["symbol"],amount,row["cid"],getattr(order,"id",""))
        return {"status":"SUBMITTED","symbol":row["symbol"],
                "amount":str(amount),"orders":1,"new":count,"health":health(c)}
