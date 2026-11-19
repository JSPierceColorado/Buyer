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
        fraction=Decimal(".02"))
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
    types["alpaca.trading.enums"].OrderSide=SimpleNamespace(BUY="buy",SELL="sell")
    types["alpaca.trading.enums"].TimeInForce=SimpleNamespace(DAY="day")
    types["alpaca.trading.enums"].QueryOrderStatus=SimpleNamespace(OPEN="open",ALL="all")
    types["alpaca.common.exceptions"].APIError=APIError
    return types,APIError


class BrokerStub:
    def __init__(self,cash="1000",held=None,pending=None):
        self.cash=cash
        self.nmbp=cash
        self.margin_power="10000"
        self.held=held or {}
        self.pending=pending or []
        self.weekly_orders=[]
        self.held_prices={}
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
        status=filter.kwargs.get("status") if filter else None
        if status=="all":
            after=filter.kwargs.get("after")
            return [x for x in self.weekly_orders if x.submitted_at>after]
        return [SimpleNamespace(symbol=x) for x in self.pending]
    def get_open_position(self,symbol):
        if symbol not in self.held:
            raise ValueError("not held")
        price=self.held_prices.get(symbol,"97")
        entry="100"
        return SimpleNamespace(symbol=symbol,qty=self.held[symbol],
            avg_entry_price=entry,current_price=price,
            unrealized_plpc=str(Decimal(price)/Decimal(entry)-1))
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
    """Offline checks for the lean, entry-only S&P100 momentum buyer."""

    def test_verified_sp100_scan_and_score_threshold(self):
        signals,members=main.verified_screener_snapshot(
            SheetStub(good_rows()),fake_settings(),NOW)
        self.assertEqual(len(members),101)
        self.assertEqual(signals,[("AAPL",Decimal("99")),("MSFT",Decimal("95"))])

    def test_stale_scan_fails_closed(self):
        rows=good_rows()
        rows[1][8]=NOW.replace(day=6).isoformat()
        with self.assertRaises(RuntimeError):
            main.verified_screener_snapshot(SheetStub(rows),fake_settings(),NOW)

    def test_incomplete_sp100_universe_fails_closed(self):
        with self.assertRaises(RuntimeError):
            main.verified_screener_snapshot(
                SheetStub(good_rows()[:40]),fake_settings(),NOW)

    def test_invalid_score_and_below_threshold_never_eligible(self):
        rows=good_rows()
        rows[1][6]=94
        rows[2][6]="INVALID_FORMULA"
        signals,_=main.verified_screener_snapshot(
            SheetStub(rows),fake_settings(),NOW)
        self.assertEqual(signals,[])

    def test_margin_buying_power_drives_each_sequential_order(self):
        broker=BrokerStub(cash="100")
        broker.margin_power="10000"
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99)),("MSFT",Decimal(95))],
                fake_settings(dry_run=False),"2026-11-09")
        self.assertEqual(len(result["submitted"]),2)
        self.assertEqual([x["notional"] for x in broker.sent],[200.0,196.0])
        self.assertEqual([x["symbol"] for x in broker.sent],["AAPL","MSFT"])
        self.assertEqual([x["side"] for x in broker.sent],["buy","buy"])

    def test_does_not_add_to_held_positions(self):
        broker=BrokerStub(held={"AAPL":"10"})
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99)),("MSFT",Decimal(95))],
                fake_settings(dry_run=False),"2026-11-09")
        self.assertEqual([x["symbol"] for x in broker.sent],["MSFT"])
        self.assertIn(("AAPL","ALREADY_HELD"),result["skipped"])

    def test_open_orders_block_duplicate_entries(self):
        broker=BrokerStub(pending=["AAPL"])
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99)),("MSFT",Decimal(95))],
                fake_settings(dry_run=False),"2026-11-09")
        self.assertEqual([x["symbol"] for x in broker.sent],["MSFT"])
        self.assertIn(("AAPL","OPEN_ORDER"),result["skipped"])

    def test_client_ids_prevent_repeat_attempts(self):
        broker=BrokerStub()
        broker.attempted.add(main.client_order_id("2026-11-09","AAPL"))
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99)),("MSFT",Decimal(95))],
                fake_settings(dry_run=False),"2026-11-09")
        self.assertEqual([x["symbol"] for x in broker.sent],["MSFT"])
        self.assertIn(("AAPL","BUY_ORDER_ALREADY_ATTEMPTED_TODAY"),result["skipped"])

    def test_short_positions_never_averaged_into(self):
        broker=BrokerStub(held={"AAPL":"-3"})
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99)),("MSFT",Decimal(95))],
                fake_settings(dry_run=False),"2026-11-09")
        self.assertEqual([x["symbol"] for x in broker.sent],["MSFT"])
        self.assertIn(("AAPL","SHORT_POSITION_NOT_ELIGIBLE"),result["skipped"])

    def test_dry_run_never_places_orders(self):
        broker=BrokerStub()
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99))],fake_settings(),"2026-11-09")
        self.assertEqual(len(result["preview"]),1)
        self.assertEqual(broker.sent,[])

    def test_market_closed_prevents_sheet_access(self):
        broker=BrokerStub()
        broker.open=False
        self.assertEqual(main.run_cycle(None,broker,fake_settings(),NOW),
                         {"status":"MARKET_CLOSED"})

    def test_tiny_buying_power_prevents_fractional_order(self):
        broker=BrokerStub()
        broker.margin_power="20"
        with patch.dict(sys.modules,stub_modules()[0]):
            result=main.buy_signals(
                broker,[("AAPL",Decimal(99))],fake_settings(dry_run=False),
                "2026-11-09")
        self.assertEqual(result["submitted"],[])
        self.assertEqual(broker.sent,[])

    def test_missing_margin_field_blocks_trading(self):
        with self.assertRaises(ValueError):
            main.available_buying_power(
                SimpleNamespace(status="ACTIVE",buying_power=None))

    def test_account_blocked_prevents_orders(self):
        with self.assertRaises(RuntimeError):
            main.available_buying_power(SimpleNamespace(
                status="ACTIVE",buying_power="1000",trading_blocked=True))

    def test_broker_order_uncertainty_not_retried(self):
        class Uncertain(BrokerStub):
            def submit_order(self,order_data):
                self.sent.append(order_data.kwargs)
                raise RuntimeError("unknown broker response")
        broker=Uncertain()
        with patch.dict(sys.modules,stub_modules()[0]):
            with self.assertRaises(RuntimeError):
                main.buy_signals(broker,[("AAPL",Decimal(99))],
                                 fake_settings(dry_run=False),"2026-11-09")
        self.assertEqual(len(broker.sent),1)

    def test_only_one_scheduled_job_no_compounder_or_ladder(self):
        import inspect
        code=inspect.getsource(main.main)
        self.assertEqual(code.count("scheduler.add_job("),1)
        self.assertNotIn("run_core_cycle",code)
        self.assertFalse(hasattr(main,"run_lowwater_sweep"))
        self.assertFalse(hasattr(main,"run_core_cycle"))

    def test_sizing_rounding_and_fraction_cap(self):
        self.assertEqual(main.buying_power_order_amount(
            Decimal("1234.56"),Decimal(".02")),Decimal("24.69"))
        self.assertEqual(main.buying_power_order_amount(
            Decimal("20"),Decimal(".02")),Decimal(0))
        self.assertEqual(main.next_noon(
            NOW.replace(hour=13)),NOW.replace(day=10,hour=12))

    def test_default_safe_mode_and_live_ack(self):
        env={"APCA_API_KEY_ID":"dummy","APCA_API_SECRET_KEY":"dummy",
             "GOOGLE_SERVICE_ACCOUNT_JSON":"{}","GOOGLE_SPREADSHEET_ID":"v2-test",
             "ALPACA_PAPER":"true","DRY_RUN":"true",
             "MINIMUM_SCORE":"95","BUY_FRACTION":"0.02"}
        with patch.dict(os.environ,env,clear=True):
            s=main.settings_from_env()
            self.assertTrue(s.paper)
            self.assertTrue(s.dry_run)
        env.update({"ALPACA_PAPER":"false","DRY_RUN":"false"})
        with patch.dict(os.environ,env,clear=True):
            with self.assertRaises(ValueError):
                main.settings_from_env()
