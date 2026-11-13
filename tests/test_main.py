"""Offline end-to-end strategy tests: never contact Alpaca or Google."""
import os
import sys
import unittest
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace,ModuleType
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main

NOW=datetime(2026,11,9,12,0,tzinfo=main.DENVER)


def fake_settings(**change):
    args=dict(alpaca_key="dummy",alpaca_secret="dummy",
        google_credentials_json="{}",sheet_id="v2-test",
        paper=True,dry_run=True,threshold=Decimal("95"),
        fraction=Decimal(".02"),addition=Decimal("5"))
    args.update(change)
    return main.Settings(**args)


class SheetStub:
    def __init__(self,rows):
        self.rows=rows
        self.kwargs=None
    def spreadsheets(self):return self
    def values(self):return self
    def get(self,**kwargs):
        self.kwargs=kwargs
        return self
    def execute(self,**kwargs):return {"values":self.rows}


def good_rows(now=NOW):
    header=["symbol","close","sma_200","sma_50","pos_52w",
            "dollar_vol_m","score","",
            "last_scan_utc","completed_session_date"]
    rows=[header]
    # 101 valid unique stocks. Score 95+ on two to demonstrate behavior.
    valid=["AAPL","MSFT"]+[f"SYM{i}" for i in range(99)]
    for i,sym in enumerate(valid):
        row=[sym,100,95,97,.9,100,
             99 if i==0 else 95 if i==1 else 0]
        if i==0:row+=["",now.replace(hour=9).isoformat(),"2026-11-06"]
        rows.append(row)
    return rows


def stub_modules():
    types={name:ModuleType(name) for name in (
        "alpaca","alpaca.trading","alpaca.trading.enums",
        "alpaca.trading.requests","alpaca.common","alpaca.common.exceptions")}
    class Request:
        def __init__(self,**kwargs):self.kwargs=kwargs
    class APIError(Exception):
        status_code=404
    types["alpaca.trading.requests"].MarketOrderRequest=Request
    types["alpaca.trading.requests"].GetOrdersRequest=Request
    types["alpaca.trading.enums"].AssetClass=SimpleNamespace(US_EQUITY="us_equity")
    types["alpaca.trading.enums"].OrderSide=SimpleNamespace(BUY="buy")
    types["alpaca.trading.enums"].TimeInForce=SimpleNamespace(DAY="day")
    types["alpaca.trading.enums"].QueryOrderStatus=SimpleNamespace(OPEN="open")
    types["alpaca.common.exceptions"].APIError=APIError
    return types,APIError


class BrokerStub:
    def __init__(self,cash="1000",held=None,pending=None):
        self.cash=cash
        self.nmbp=cash
        self.margin_power="10000"
        self.held=held or {}
        self.pending=pending or []
        self.sent=[]
        self.attempted=set()
        self.open=True
        self.account_calls=0
    def get_account(self):
        self.account_calls+=1
        return SimpleNamespace(cash=self.cash,non_marginable_buying_power=self.nmbp,
            buying_power=self.margin_power,status="ACTIVE",
            account_blocked=False,trading_blocked=False,trade_suspended_by_user=False)
    def get_clock(self):return SimpleNamespace(is_open=self.open)
    def get_all_positions(self):
        return [SimpleNamespace(symbol=k,qty=str(v)) for k,v in self.held.items()]
    def get_orders(self,filter=None):
        return [SimpleNamespace(symbol=x) for x in self.pending]
    def get_order_by_client_id(self,id):
        from alpaca.common.exceptions import APIError
        if id in self.attempted:return SimpleNamespace(id=id)
        raise APIError("not found")
    def get_asset(self,sym):
        return SimpleNamespace(symbol=sym,tradable=True,
                               fractionable=True,asset_class="us_equity")
    def submit_order(self,order_data):
        self.sent.append(order_data.kwargs)
        self.attempted.add(order_data.kwargs["client_order_id"])
        return SimpleNamespace(id="broker-test-"+str(len(self.sent)))


class BuyerTests(unittest.TestCase):
    def test_noon_and_dst(self):
        self.assertEqual(main.next_noon(datetime(2026,7,6,11,tzinfo=main.DENVER)).isoformat(),
                         "2026-07-06T12:00:00-06:00")
        self.assertEqual(main.next_noon(datetime(2026,11,9,11,tzinfo=main.DENVER)).isoformat(),
                         "2026-11-09T12:00:00-07:00")
        self.assertEqual(main.next_noon(NOW).date().isoformat(),"2026-11-10")

    def test_scan_requires_same_day_and_completed_session(self):
        sheet=SheetStub(good_rows())
        signals=main.verified_screener_signals(sheet,fake_settings(),NOW)
        self.assertEqual(signals,[("AAPL",Decimal("99")),("MSFT",Decimal("95"))])
        self.assertEqual(sheet.kwargs["range"],"'Screener'!A1:J150")
        stale=good_rows()
        stale[1][8]="2026-11-06T09:00:00-07:00"
        with self.assertRaisesRegex(RuntimeError,"NOT_REFRESHED"):
            main.verified_screener_signals(SheetStub(stale),fake_settings(),NOW)
        incomplete=good_rows()
        incomplete[0][8]=""
        with self.assertRaisesRegex(RuntimeError,"VERIFIED_SCAN_STAMP"):
            main.verified_screener_signals(SheetStub(incomplete),fake_settings(),NOW)
        old=good_rows()
        old[1][9]="2026-10-20"
        with self.assertRaisesRegex(RuntimeError,"COMPLETED_DATA_STALE"):
            main.verified_screener_signals(SheetStub(old),fake_settings(),NOW)

    def test_too_few_valid_symbols_aborts_before_broker_trade(self):
        rows=good_rows()[:4]
        with self.assertRaisesRegex(RuntimeError,"UNIVERSE_INCOMPLETE"):
            main.verified_screener_signals(SheetStub(rows),fake_settings(),NOW)

    def test_ignore_empty_tick_and_formula_error(self):
        rows=good_rows()
        rows.append(["",100,95,97,.9,100,100])
        rows.append(["BAD!",100,95,97,.9,100,100])
        rows.append(["FRAUD",0,95,97,.9,100,100])
        self.assertEqual(len(main.verified_screener_signals(
            SheetStub(rows),fake_settings(),NOW)),2)

    def test_margin_buying_power_and_five_dollar_held_add(self):
        modules,_=stub_modules()
        trader=BrokerStub("1000",held={"MSFT":"0.33333"})
        with patch.dict(sys.modules,modules):
            result=main.buy_signals(trader,[("AAPL",Decimal(99)),("MSFT",Decimal(95))],
                fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual(len(trader.sent),2)
        self.assertEqual(trader.sent[0]["notional"],200.0)
        self.assertEqual(trader.sent[1]["notional"],5.0)
        self.assertEqual(trader.sent[1]["symbol"],"MSFT")
        self.assertEqual(trader.sent[1]["side"],"buy")
        self.assertEqual(result["submitted"][1][4],"ADD_HELD")

    def test_remaining_margin_buying_power_reduced_for_sequential_new_entries(self):
        modules,_=stub_modules()
        trader=BrokerStub("1000")
        with patch.dict(sys.modules,modules):
            main.buy_signals(trader,[("AAPL",Decimal(99)),("MSFT",Decimal(98))],
                fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual([x["notional"] for x in trader.sent],[200.0,196.0])

    def test_order_dedupes_after_restarts(self):
        modules,_=stub_modules()
        trader=BrokerStub("1000")
        with patch.dict(sys.modules,modules):
            for _ in range(2):
                main.buy_signals(trader,[("AAPL",Decimal(99))],
                    fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual(len(trader.sent),1)

    def test_pending_and_short_positions_never_bought(self):
        modules,_=stub_modules()
        trader=BrokerStub("1000",held={"AAPL":"-3"},pending=["MSFT"])
        with patch.dict(sys.modules,modules):
            r=main.buy_signals(trader,[("AAPL",Decimal(99)),("MSFT",Decimal(98))],
                fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual(len(trader.sent),0)
        self.assertEqual(len(r["skipped"]),2)

    def test_missing_buying_power_fails_closed(self):
        class NoBuyingPower:
            buying_power=None
            status="ACTIVE"
        with self.assertRaises(ValueError):
            main.available_buying_power(NoBuyingPower())
        self.assertEqual(main.buying_power_order_amount(Decimal("4"),Decimal(".02"),True),0)
        self.assertEqual(main.buying_power_order_amount(Decimal("49"),Decimal(".02"),False),0)

    def test_margin_power_not_cash_drives_new_entry(self):
        modules,_=stub_modules()
        trader=BrokerStub("0")
        trader.margin_power="5000"
        with patch.dict(sys.modules,modules):
            result=main.buy_signals(trader,[("AAPL",Decimal("99"))],
                fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual(result["qualified"],1)
        self.assertEqual([x["notional"] for x in trader.sent],[100.0])

    def test_fresh_margin_power_limits_subsequent_entries(self):
        modules,_=stub_modules()
        trader=BrokerStub("0")
        trader.margin_power="10000"
        original=trader.get_account
        def account():
            result=original()
            if trader.sent:
                result.buying_power="150"
            return result
        trader.get_account=account
        with patch.dict(sys.modules,modules):
            main.buy_signals(trader,[("AAPL",Decimal("99")),("MSFT",Decimal("98"))],
                fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual([x["notional"] for x in trader.sent],[200.0,3.0])

    def test_insufficient_margin_power_aborts_before_order(self):
        modules,_=stub_modules()
        trader=BrokerStub("1000")
        trader.margin_power="0"
        with patch.dict(sys.modules,modules):
            r=main.buy_signals(trader,[("AAPL",Decimal("99"))],
                fake_settings(dry_run=False),NOW.date().isoformat())
        self.assertEqual(trader.sent,[])
        self.assertEqual(r["skipped"],[("AAPL","NO_BUYING_POWER")])

    def test_submissions_never_retried_after_unknown_response(self):
        modules,_=stub_modules()
        trader=BrokerStub()
        def ambiguous(order_data):
            raise TimeoutError("connection lost after POST")
        trader.submit_order=ambiguous
        with patch.dict(sys.modules,modules):
            with self.assertRaises(TimeoutError):
                main.buy_signals(trader,[("AAPL",Decimal(99)),("MSFT",Decimal(98))],
                    fake_settings(dry_run=False),NOW.date().isoformat())

    def test_market_closed_does_not_read_sheet(self):
        trader=BrokerStub()
        trader.open=False
        self.assertEqual(main.run_cycle(None,trader,fake_settings(),NOW),
                         {"status":"MARKET_CLOSED"})

    def test_live_credential_ack_required_for_real_orders(self):
        env={"APCA_API_KEY_ID":"dummy","APCA_API_SECRET_KEY":"dummy",
             "GOOGLE_SERVICE_ACCOUNT_JSON":"{}",
             "ALPACA_PAPER":"false","DRY_RUN":"false"}
        with patch.dict(os.environ,env,clear=True):
            with self.assertRaisesRegex(ValueError,"LIVE_TRADING_ACK"):
                main.settings_from_env()
            os.environ["LIVE_TRADING_ACK"]="I_ACCEPT_REAL_MONEY_BUYS"
            s=main.settings_from_env()
            self.assertFalse(s.paper)
            self.assertFalse(s.dry_run)

    def test_safest_default_is_paper_and_dry_run(self):
        env={"APCA_API_KEY_ID":"dummy","APCA_API_SECRET_KEY":"dummy",
             "GOOGLE_SERVICE_ACCOUNT_JSON":"{}"}
        with patch.dict(os.environ,env,clear=True):
            s=main.settings_from_env()
        self.assertTrue(s.paper)
        self.assertTrue(s.dry_run)

    def test_scheduler_noon_denver_without_railway_cron(self):
        from types import ModuleType
        scheduled={}
        class Stub:
            def __init__(self,**kwargs):scheduled["init"]=kwargs
            def add_job(self,callback,kind,**kwargs):
                scheduled["job"]=(callback,kind,kwargs)
            def start(self):scheduled["started"]=True
        module=ModuleType("apscheduler.schedulers.blocking")
        module.BlockingScheduler=Stub
        modules={"apscheduler":ModuleType("apscheduler"),
                 "apscheduler.schedulers":ModuleType("apscheduler.schedulers"),
                 "apscheduler.schedulers.blocking":module}
        trader=BrokerStub("0")
        trader.margin_power="10000"
        with patch.dict(sys.modules,{**stub_modules()[0],**modules}), patch("sys.argv",["main.py"]):
            with patch.object(main,"settings_from_env",return_value=fake_settings()), patch.object(main,"clients",return_value=(SheetStub(good_rows()),trader)):
                main.main()
        self.assertTrue(scheduled["started"])
        self.assertEqual(trader.sent,[],"Scheduler startup must not send orders")
        self.assertEqual(scheduled["init"]["timezone"],main.DENVER)
        callback,kind,params=scheduled["job"]
        self.assertEqual(kind,"cron")
        self.assertEqual((params["day_of_week"],params["hour"],params["minute"]),("mon-fri",12,0))


if __name__=="__main__":
    unittest.main()
