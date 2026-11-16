"""Persistent six-step S&P100 low-water buying ladder.

The sole writer is V2-Buyer, observing once per market session.
Qualified lows are >=1% below the last *filled* step's price and strictly
lower than all previous observations in the current held episode.
Steps: 5%, 10%, 20%, 40%, 80%, 100% of frozen initial cost basis.
Only broker-confirmed filled steps advance the counter.
"""
from __future__ import annotations
import logging
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN

LOG=logging.getLogger("v2-lowwater")
DB_PATH="/data/v2-lowwater.sqlite3"
DROP_FACTOR=Decimal(".99")
FRACTIONS=tuple(Decimal(x) for x in (".05",".10",".20",".40",".80","1.00"))
CENT=Decimal(".01")
TICKER=re.compile(r"^[A-Z][A-Z0-9.\-]{0,10}$")
TERMINAL={"filled","canceled","expired","rejected","done_for_day"}
OPEN={"new","accepted","pending_new","partially_filled","accepted_for_bidding",
      "pending_cancel","pending_replace","calculated","stopped","suspended"}

def num(value):
    try:
        n=Decimal(str(value))
        return n if n.is_finite() else None
    except (TypeError,ValueError,InvalidOperation):
        return None

def position_info(position):
    symbol=str(getattr(position,"symbol","") or "").upper()
    cls=getattr(position,"asset_class",None)
    side=getattr(position,"side",None)
    if (not TICKER.fullmatch(symbol) or
        str(getattr(cls,"value",cls)).lower()!="us_equity" or
        str(getattr(side,"value",side)).lower()!="long"):
        return None
    entry=num(getattr(position,"avg_entry_price",None))
    price=num(getattr(position,"current_price",None))
    qty=num(getattr(position,"qty",None))
    broker=num(getattr(position,"unrealized_plpc",None))
    if None in (entry,price,qty,broker) or min(entry,price,qty)<=0:
        return None
    if abs((price/entry-1)-broker)>Decimal(".01"):
        return None
    return {"symbol":symbol,"price":price,"entry":entry,"qty":qty}

def connect(path=DB_PATH):
    folder=os.path.dirname(os.path.abspath(path))
    os.makedirs(folder,exist_ok=True)
    conn=sqlite3.connect(path,timeout=15)
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("""CREATE TABLE IF NOT EXISTS ladder(
        symbol TEXT PRIMARY KEY, episode TEXT NOT NULL,
        initial_notional TEXT NOT NULL, initial_entry TEXT NOT NULL,
        initial_qty TEXT NOT NULL, last_qty TEXT NOT NULL,
        observed_low TEXT NOT NULL, last_trigger TEXT NOT NULL,
        last_session TEXT NOT NULL, steps INTEGER NOT NULL,
        pending_id TEXT NOT NULL, pending_price TEXT NOT NULL,
        pending_step INTEGER NOT NULL, flag TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ladder_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        episode TEXT NOT NULL, session TEXT NOT NULL, event TEXT NOT NULL,
        price TEXT NOT NULL, step INTEGER NOT NULL,
        amount TEXT NOT NULL, details TEXT NOT NULL)""")
    conn.commit()
    return conn

def event(conn,row,session,kind,price,step=0,amount="0",details=""):
    conn.execute("""INSERT INTO ladder_events(
        symbol,episode,session,event,price,step,amount,details)
        VALUES(?,?,?,?,?,?,?,?)""",
        (row["symbol"],row["episode"],session,kind,str(price),
         int(step),str(amount),str(details)))

def safe_order_id(episode,step,symbol):
    if not TICKER.fullmatch(symbol) or not re.fullmatch(r"[0-9a-f]{12}",episode):
        raise ValueError("Invalid ladder order identity")
    return "v2lw-"+episode+"-"+str(step)+"-"+symbol.replace(".","-")

def order_reconciliation(order,row):
    """Do not advance until broker order is terminal and has positive fill."""
    if order is None:
        return ("UNKNOWN",Decimal(0))
    cid=str(getattr(order,"client_order_id","") or "")
    side=getattr(order,"side",None)
    if cid!=row["pending_id"] or str(getattr(side,"value",side)).lower()!="buy":
        return ("UNKNOWN",Decimal(0))
    filled=num(getattr(order,"filled_qty",None))
    state=getattr(order,"status",None)
    state=str(getattr(state,"value",state)).lower()
    if filled is None or filled<0:
        return ("UNKNOWN",Decimal(0))
    if state in OPEN:
        return ("OPEN",filled)
    if state not in TERMINAL:
        return ("UNKNOWN",filled)
    return ("FILLED" if filled>0 else "NO_FILL",filled)

def drop_absent(current_symbols,*,path=DB_PATH,session):
    """Retire episodes when a validated complete portfolio no longer holds them."""
    with closing(connect(path)) as conn,conn:
        for row in conn.execute("SELECT * FROM ladder").fetchall():
            if row["symbol"] not in current_symbols:
                event(conn,row,session,"EPISODE_ENDED",row["observed_low"],
                      row["steps"],details="POSITION_NOT_HELD")
                conn.execute("DELETE FROM ladder WHERE symbol=?",(row["symbol"],))

def observe(position,session,*,lookup,path=DB_PATH,readonly=False):
    """One daily snapshot and possible READY plan; never submits broker orders."""
    date.fromisoformat(session)
    info=position_info(position)
    if info is None:
        return {"status":"INVALID_POSITION"}
    sym=info["symbol"];price=info["price"]
    with closing(connect(path)) as conn,conn:
        rec=conn.execute("SELECT * FROM ladder WHERE symbol=?",(sym,)).fetchone()
        if rec is None:
            if readonly:return {"status":"NEW_POSITION_SEED_ONLY"}
            episode=uuid.uuid4().hex[:12]
            basis=(info["entry"]*info["qty"]).quantize(CENT,rounding=ROUND_DOWN)
            low=min(info["entry"],price)
            conn.execute("INSERT INTO ladder VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (sym,episode,str(basis),str(info["entry"]),str(info["qty"]),
               str(info["qty"]),str(low),str(low),session,0,"","",0,""))
            event(conn,{"symbol":sym,"episode":episode},session,
                  "INITIAL_SEED",price,details="basis="+str(basis))
            LOG.info("LOWWATER_SEEDED symbol=%s original_notional=%s baseline_price=%s",
                     sym,basis,low)
            return {"status":"SEEDED","original":basis}
        old=dict(rec)
        if old["last_session"]>=session:
            return {"status":"ALREADY_OBSERVED","steps":old["steps"],"flag":old["flag"]}
        if old["pending_id"]:
            try:
                result,filled=order_reconciliation(lookup(old["pending_id"]),old)
            except Exception:
                result,filled="UNKNOWN",Decimal(0)
            if result=="OPEN":
                if not readonly:
                    conn.execute("UPDATE ladder SET last_session=? WHERE symbol=?",
                                 (session,sym))
                return {"status":"PENDING_ORDER","id":old["pending_id"]}
            if result=="UNKNOWN":
                if not readonly:
                    conn.execute("UPDATE ladder SET flag=?,last_session=? WHERE symbol=?",
                                 ("BROKER_ORDER_UNVERIFIABLE",session,sym))
                return {"status":"REVIEW_REQUIRED","reason":"BROKER_ORDER_UNVERIFIABLE"}
            if result=="NO_FILL":
                if not readonly:
                    conn.execute("UPDATE ladder SET flag=?,last_session=? WHERE symbol=?",
                                 ("UNFILLED_ORDER_REVIEW",session,sym))
                    event(conn,old,session,"UNFILLED_REVIEW",price,old["pending_step"])
                return {"status":"REVIEW_REQUIRED","reason":"UNFILLED_ORDER_REVIEW"}
            if readonly:return {"status":"FILLED_PENDING_RECONCILIATION"}
            step=old["pending_step"]
            if step!=old["steps"]+1 or not 1<=step<=6:
                conn.execute("UPDATE ladder SET flag=? WHERE symbol=?",
                             ("INVALID_PENDING_STEP",sym))
                return {"status":"REVIEW_REQUIRED","reason":"INVALID_PENDING_STEP"}
            old["steps"]=step
            old["last_trigger"]=old["pending_price"]
            old["last_qty"]=str(info["qty"])
            old["pending_id"]="";old["pending_price"]="";old["pending_step"]=0
            if step==6:old["flag"]="CAP_REACHED_REVIEW_REQUIRED"
            conn.execute("""UPDATE ladder SET steps=?,last_trigger=?,last_qty=?,
                pending_id='',pending_price='',pending_step=0,flag=? WHERE symbol=?""",
                (step,old["last_trigger"],old["last_qty"],old["flag"],sym))
            event(conn,old,session,"STEP_FILL_CONFIRMED",
                  old["last_trigger"],step,details="shares_filled="+str(filled))
            LOG.warning("LOWWATER_STEP_FILLED symbol=%s step=%s/6",sym,step)
            if step==6:
                LOG.warning("LOWWATER_REVIEW_REQUIRED symbol=%s steps=6 reason=CAP_REACHED",sym)
        if old["flag"]:
            if not readonly:
                conn.execute("UPDATE ladder SET last_session=? WHERE symbol=?",
                             (session,sym))
            return {"status":"REVIEW_REQUIRED","reason":old["flag"],"steps":old["steps"]}
        if not readonly:
            prev_qty=num(old["last_qty"])
            # Unexpected independent manual or external-bot additions suspend
            # further escalating purchases to protect the initial exposure cap.
            if prev_qty is None or info["qty"]>prev_qty+Decimal(".0000001"):
                conn.execute("UPDATE ladder SET flag=?,last_session=? WHERE symbol=?",
                             ("UNTRACKED_POSITION_ADDITION",session,sym))
                LOG.warning("LOWWATER_REVIEW_REQUIRED symbol=%s reason=UNTRACKED_POSITION_ADDITION",sym)
                return {"status":"REVIEW_REQUIRED","reason":"UNTRACKED_POSITION_ADDITION"}
        prev_low=Decimal(old["observed_low"])
        new_low=price<prev_low
        if readonly:
            return {"status":"PREVIEW_NEW_LOW" if new_low else "PREVIEW_NO_NEW_LOW"}
        conn.execute("UPDATE ladder SET last_session=?,last_qty=?,observed_low=? WHERE symbol=?",
                     (session,str(info["qty"]),str(min(price,prev_low)),sym))
        if new_low:
            event(conn,old,session,"NEW_OBSERVED_LOW",price,old["steps"])
        if old["steps"]>=6:
            conn.execute("UPDATE ladder SET flag=? WHERE symbol=?",
                         ("CAP_REACHED_REVIEW_REQUIRED",sym))
            return {"status":"REVIEW_REQUIRED","reason":"CAP_REACHED_REVIEW_REQUIRED"}
        if (not new_low or price>=info["entry"] or
            price>Decimal(old["last_trigger"])*DROP_FACTOR):
            return {"status":"NO_QUALIFYING_LOW","steps":old["steps"],
                    "new_low":new_low}
        step=old["steps"]+1
        basis=Decimal(old["initial_notional"])
        amount=(basis*FRACTIONS[step-1]).quantize(CENT,rounding=ROUND_DOWN)
        if amount<Decimal("1"):
            return {"status":"BELOW_MINIMUM_ORDER","next_step":step}
        return {"status":"READY","symbol":sym,"episode":old["episode"],
                "step":step,"amount":amount,"price":price,
                "cid":safe_order_id(old["episode"],step,sym),
                "initial_notional":basis}

def reserve(plan,session,*,path=DB_PATH):
    """Persist deterministic client ID BEFORE the one permitted broker POST."""
    if plan.get("status")!="READY":raise ValueError("Cannot reserve non-ready plan")
    with closing(connect(path)) as conn,conn:
        row=conn.execute("SELECT * FROM ladder WHERE symbol=?",(plan["symbol"],)).fetchone()
        if (row is None or row["episode"]!=plan["episode"] or
            row["steps"]+1!=plan["step"] or row["pending_id"] or
            row["last_session"]!=session):
            raise RuntimeError("LADDER_RESERVATION_CONFLICT")
        conn.execute("""UPDATE ladder SET pending_id=?,pending_price=?,pending_step=?
            WHERE symbol=?""",(plan["cid"],str(plan["price"]),plan["step"],plan["symbol"]))
        event(conn,row,session,"ORDER_RESERVED",plan["price"],plan["step"],
              amount=plan["amount"],details=plan["cid"])
    LOG.warning("LOWWATER_ORDER_RESERVED symbol=%s step=%s amount=%s",
                plan["symbol"],plan["step"],plan["amount"])
