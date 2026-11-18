"""Offline core buyer tests: only CSD deposits, never margin and never duplicate."""
import os
import tempfile
import types
import sys
import unittest
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import deposit_compounder as core

DAY=datetime(2026,11,18,16,0,tzinfo=timezone.utc)


def csd(suffix,amount,day="2026-11-18"):
    return {"id":"20261118::"+suffix,"activity_type":"CSD",
            "date":day,"net_amount":str(amount)}


class Broker:
    def __init__(self,cash="1000",nonmargin="1000",power="3000",open_=True):
        self.account=SimpleNamespace(id="test-account",status="ACTIVE",
            account_blocked=False,trading_blocked=False,
            trade_suspended_by_user=False,cash=cash,
            non_marginable_buying_power=nonmargin,buying_power=power,
            pending_transfer_out="0")
        self.open_=open_
        self.positions=[]
        self.open_orders=[]
        self.orders={}
        self.sent=[]
        self.fail_post=False
    def get_account(self):return self.account
    def get_clock(self):return SimpleNamespace(is_open=self.open_)
    def get_all_positions(self):return self.positions
    def get_orders(self,filter=None):return self.open_orders
    def get_asset(self,symbol):
        return SimpleNamespace(symbol=symbol,tradable=True,
              fractionable=True,status="active",asset_class="us_equity")
    def get_order_by_client_id(self,cid):return self.orders.get(cid)
    def submit_order(self,order_data):
        if self.fail_post:raise ConnectionError("Broker timeout")
        info=order_data.kwargs
        self.sent.append(info)
        amt=Decimal(str(info["notional"]))
        self.account.cash=str(Decimal(self.account.cash)-amt)
        self.account.non_marginable_buying_power=str(
            Decimal(self.account.non_marginable_buying_power)-amt)
        self.account.buying_power=str(Decimal(self.account.buying_power)-amt)
        cid=info["client_order_id"]
        order=SimpleNamespace(id="order-"+str(len(self.sent)),
             client_order_id=cid,symbol=info["symbol"],status="new",
             side="buy",filled_qty="0",filled_avg_price=None)
        self.orders[cid]=order
        return order
    def fill(self):
        for o in self.orders.values():
            if o.status=="new":
                o.status="filled"
                o.filled_avg_price="100"
                amt=next((x["notional"] for x in self.sent
                          if x["client_order_id"]==o.client_order_id),0)
                o.filled_qty=str(Decimal(str(amt))/100)
                self.positions=[p for p in self.positions
                    if p.symbol!=o.symbol]+[
                    SimpleNamespace(symbol=o.symbol,qty=o.filled_qty)]


def sdk():
    modules={name:types.ModuleType(name) for name in (
       "alpaca","alpaca.trading","alpaca.trading.enums","alpaca.trading.requests")}
    class Request:
        def __init__(self,**kwargs):self.kwargs=kwargs
    enums=modules["alpaca.trading.enums"]
    enums.AssetClass=SimpleNamespace(US_EQUITY="us_equity")
    enums.OrderSide=SimpleNamespace(BUY="buy")
    enums.QueryOrderStatus=SimpleNamespace(OPEN="open")
    enums.TimeInForce=SimpleNamespace(DAY="day")
    req=modules["alpaca.trading.requests"]
    req.GetOrdersRequest=Request
    req.MarketOrderRequest=Request
    return modules


class CoreTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.path=os.path.join(t.name,"core.sqlite3")
        self.trader=Broker()
        self.deposits=[]
        self.now=DAY
    def run_cycle(self,dry_run=False):
        with patch.dict(sys.modules,sdk()):
            return core.run(self.trader,get_csd=lambda:core.validate_deposits(self.deposits),
                            path=self.path,dry_run=dry_run,now=self.now)
    def test_first_run_baselines_all_historical_deposits_without_orders(self):
        self.deposits=[csd("old","600",day="2026-11-15")]
        report=self.run_cycle()
        self.assertEqual(report["status"],"BASELINED")
        self.assertEqual(report["health"]["new_deposits"],0)
        self.assertEqual(self.trader.sent,[])
        self.assertEqual(self.run_cycle()["status"],"NO_ACTION")
    def test_new_deposit_quarter_each_then_no_duplicate(self):
        self.run_cycle()
        self.deposits=[csd("new","200")]
        a=self.run_cycle()
        self.assertEqual(a["status"],"SUBMITTED")
        self.assertEqual(a["symbol"],"VT")
        self.assertEqual(a["amount"],"50.00")
        self.assertEqual(self.trader.sent[0]["symbol"],"VT")
        self.assertEqual(len(self.trader.sent),1)
        self.trader.fill()
        b=self.run_cycle()
        self.assertEqual(b["status"],"SUBMITTED")
        self.assertEqual(b["symbol"],"SGOV")
        self.assertEqual(b["amount"],"50.00")
        self.trader.fill()
        c=self.run_cycle()
        self.assertEqual(c["status"],"NO_ACTION")
        self.assertEqual(c["health"]["legs"]["FILLED"],2)
        self.assertEqual(c["health"]["gross_deposits"],200)
        self.assertEqual(len(self.trader.sent),2)
        self.assertEqual(len({x["client_order_id"] for x in self.trader.sent}),2)
    def test_cash_not_margin_is_hard_requirement(self):
        self.run_cycle()
        self.deposits=[csd("margincheck","200")]
        self.trader.account.cash="10"
        self.trader.account.non_marginable_buying_power="5"
        self.trader.account.buying_power="10000"
        r=self.run_cycle()
        self.assertEqual(r["status"],"DEFER_CASH")
        self.assertEqual(self.trader.sent,[])
    def test_missing_nonmarginable_power_fails_closed(self):
        self.trader.account.non_marginable_buying_power=None
        with self.assertRaisesRegex(RuntimeError,"CORE_BROKER_MONEY_UNVERIFIABLE"):
            self.run_cycle()
        self.assertEqual(self.trader.sent,[])
    def test_broker_timeout_never_retries_reserved_cid(self):
        self.run_cycle()
        self.deposits=[csd("timeout","200")]
        self.trader.fail_post=True
        with self.assertRaisesRegex(RuntimeError,"NO_RETRY"):
            self.run_cycle()
        self.trader.fail_post=False
        out=self.run_cycle()
        self.assertEqual(out["status"],"NO_ACTION")
        self.assertEqual(len(self.trader.sent),0)
        with core.connect(self.path) as c:
            r=c.execute("SELECT state FROM legs WHERE symbol='VT'").fetchone()
            self.assertEqual(r["state"],"RESERVED")
    def test_old_backdated_post_arm_activity_not_invested(self):
        self.run_cycle()
        self.deposits=[csd("late","1000",day="2026-11-15")]
        self.assertEqual(self.run_cycle()["status"],"NO_ACTION")
        self.assertEqual(self.trader.sent,[])
    def test_small_deposits_defer_below_one_dollar(self):
        self.run_cycle()
        self.deposits=[csd("small","3")]
        r=self.run_cycle()
        self.assertEqual(r["status"],"NO_ACTION")
        self.assertEqual(r["health"]["legs"].get("DEFERRED_BELOW_MINIMUM"),2)
    def test_market_closed_saves_deposit_without_order(self):
        self.run_cycle()
        self.deposits=[csd("weekend","100")]
        self.trader.open_=False
        r=self.run_cycle()
        self.assertEqual(r["status"],"MARKET_CLOSED")
        self.assertEqual(self.trader.sent,[])
        self.trader.open_=True
        r=self.run_cycle()
        self.assertEqual(r["status"],"SUBMITTED")
        self.assertEqual(r["amount"],"25.00")
    def test_existing_core_position_blocks_baseline(self):
        self.trader.positions=[SimpleNamespace(symbol="VT",qty=".2")]
        r=self.run_cycle()
        self.assertEqual(r["status"],"REVIEW_REQUIRED")
        self.assertEqual(self.trader.sent,[])
    def test_wrong_account_identity_blocks(self):
        self.run_cycle()
        self.trader.account.id="different-account"
        with self.assertRaisesRegex(RuntimeError,"ACCOUNT_ID_CHANGED"):
            self.run_cycle()
    def test_invalid_csd_activity_and_amount_rejected(self):
        for bad in ([{"id":"a","activity_type":"DIV","net_amount":"100","date":"2026-11-18"}],
                    [csd("bad","-2")],[csd("dupe","10"),csd("dupe","10")]):
            with self.assertRaises(RuntimeError):
                core.validate_deposits(bad)
    def test_amount_rounding_and_cap(self):
        self.assertEqual(core.legs_amounts("101.01"),
                         {"VT":Decimal("25.25"),"SGOV":Decimal("25.25")})
        self.assertEqual(core.legs_amounts("0.03"),
                         {"VT":Decimal("0.00"),"SGOV":Decimal("0.00")})
    def test_open_order_blocks_new_cash_order(self):
        self.run_cycle();self.deposits=[csd("open","200")]
        self.trader.open_orders=[SimpleNamespace(symbol="AAPL")]
        self.assertEqual(self.run_cycle()["status"],"DEFER_OPEN_ORDERS")
        self.assertEqual(self.trader.sent,[])
    def test_existing_etf_added_externally_requires_review(self):
        self.run_cycle()
        self.trader.positions=[SimpleNamespace(symbol="SGOV",qty="1")]
        self.deposits=[csd("deposit","200")]
        with self.assertRaisesRegex(RuntimeError,"UNATTRIBUTED_ETF_SHARES"):
            self.run_cycle()
    def test_dry_run_does_not_submit(self):
        self.run_cycle();self.deposits=[csd("dry","200")]
        self.assertEqual(self.run_cycle(dry_run=True)["status"],"PREVIEW")
        self.assertEqual(self.trader.sent,[])


if __name__=="__main__":unittest.main()
