"""Offline checks; never contact Google Sheets or Alpaca."""
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.requested = None
    def spreadsheets(self):
        return self
    def values(self):
        return self
    def get(self, **kw):
        self.requested = kw
        return self
    def execute(self):
        return {"values": self.rows}


def fake_settings(**kwargs):
    base = dict(alpaca_key="k", alpaca_secret="s", google_credentials_json="{}",
                sheet_id="spreadsheet", sheet_tab="Screener", paper=True,
                dry_run=True, threshold=Decimal("95"), fraction=Decimal("0.02"))
    base.update(kwargs)
    return main.Settings(**base)


def with_alpaca_modules():
    modules = {n: ModuleType(n) for n in (
        "alpaca", "alpaca.trading", "alpaca.trading.requests", "alpaca.trading.enums",
        "alpaca.common", "alpaca.common.exceptions")}
    class Dummy:
        def __init__(self, **kw): self.kwargs = kw
    class APIError(Exception):
        status_code = 404
    modules["alpaca.trading.requests"].GetOrdersRequest = Dummy
    modules["alpaca.trading.requests"].MarketOrderRequest = Dummy
    modules["alpaca.trading.enums"].QueryOrderStatus = SimpleNamespace(OPEN="open")
    modules["alpaca.trading.enums"].OrderSide = SimpleNamespace(BUY="buy")
    modules["alpaca.trading.enums"].TimeInForce = SimpleNamespace(DAY="day")
    modules["alpaca.common.exceptions"].APIError = APIError
    return modules, APIError


class BuyerTests(unittest.TestCase):
    def test_noon_denver_weekday(self):
        now = datetime(2026, 10, 19, 11, 35, tzinfo=main.DENVER)
        self.assertEqual(main.next_noon(now).isoformat(), "2026-10-19T12:00:00-06:00")
        self.assertEqual(main.next_noon(datetime(2026, 10, 19, 12, 0, tzinfo=main.DENVER)).day, 20)

    def test_weekend_skip(self):
        now = datetime(2026, 10, 17, 9, 30, tzinfo=main.DENVER)
        self.assertEqual(main.next_noon(now).isoformat(), "2026-10-19T12:00:00-06:00")

    def test_daylight_saving(self):
        self.assertEqual(main.next_noon(datetime(2026, 11, 2, 7, tzinfo=main.DENVER)).isoformat(),
                         "2026-11-02T12:00:00-07:00")

    def test_score_filter_only_needs_a_and_g(self):
        rows = [["symbol", "close", "sma_200", "sma_50", "pos_52w", "dollar_vol_m", "score"],
                ["AAPL", None, None, None, None, None, 100],
                ["MSFT", 10, 9, 9, .9, 100, 94.999],
                ["V", "", "", "", "", "", "95.2"],
                ["", "", "", "", "", "", ""],
                ["META", 10, 9, 9, .9, 100, "FORMULA_ERROR"]]
        fake = FakeSheet(rows)
        signals = main.read_signals(fake, fake_settings())
        self.assertEqual(signals, [("AAPL", Decimal("100")), ("V", Decimal("95.2"))])
        self.assertIn("A1:G500", fake.requested["range"])

    def test_no_strict_universe_validation(self):
        rows = [["symbol", "close", "sma_200", "sma_50", "pos_52w", "dollar_vol_m", "score"],
                ["A", 100, 95, 90, .9, 20, 97.1]]
        self.assertEqual(main.read_signals(FakeSheet(rows), fake_settings()), [("A", Decimal("97.1"))])

    def test_dedupes_signals(self):
        rows = [["symbol", "close", "sma_200", "sma_50", "pos_52w", "dollar_vol_m", "score"],
                ["v", None, None, None, None, None, 98],
                ["V", None, None, None, None, None, 96]]
        self.assertEqual(main.read_signals(FakeSheet(rows), fake_settings()), [("V", Decimal("98"))])

    def test_invalid_header_rejected(self):
        with self.assertRaisesRegex(ValueError, "Expected column A"):
            main.read_signals(FakeSheet([["bad", "b", "c", "d", "e", "f", "G"]]), fake_settings())

    def test_two_percent_of_buying_power_not_equity(self):
        self.assertEqual(main.size_from_buying_power("50000", Decimal(".02")), Decimal("1000.00"))
        self.assertEqual(main.size_from_buying_power("48910.15", Decimal(".02")), Decimal("978.20"))
        self.assertEqual(main.size_from_buying_power("0", Decimal(".02")), Decimal("0"))

    def test_client_order_id_deterministic(self):
        self.assertEqual(main.client_order_id("2026-10-19", "BRK.B"), "v2sp100-20261019-BRK-B")

    def test_default_safe_paper_dry(self):
        env = {"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}
        with patch.dict(os.environ, env, clear=True):
            s = main.settings_from_env()
        self.assertTrue(s.paper)
        self.assertTrue(s.dry_run)
        self.assertEqual(s.threshold, Decimal("95"))

    def test_live_requires_ack(self):
        env = {"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
               "ALPACA_PAPER": "false", "DRY_RUN": "false"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "LIVE_TRADING_ACK"):
                main.settings_from_env()

    def test_existing_position_pending_and_daily_id_prevent_duplicates(self):
        modules, APIError = with_alpaca_modules()
        def get_id(cid):
            if "MSFT" in cid:
                return SimpleNamespace(id="already-filled")
            raise APIError("not found")
        trader = SimpleNamespace(
            get_clock=lambda: SimpleNamespace(is_open=True),
            get_all_positions=lambda: [SimpleNamespace(symbol="AAPL")],
            get_orders=lambda filter: [SimpleNamespace(symbol="V")],
            get_order_by_client_id=get_id,
            get_asset=lambda sym: SimpleNamespace(tradable=True, fractionable=True),
            get_account=lambda: SimpleNamespace(buying_power="10000"),
            submit_order=lambda order_data: self.fail("must not buy held or pending"),
        )
        with patch.dict(sys.modules, modules):
            result = main.buy_signals(trader, [("AAPL", Decimal(100)), ("V", Decimal(99)),
                                               ("MSFT", Decimal(97))], fake_settings(dry_run=False), "2026-10-19")
        self.assertEqual(result["ordered"], [])
        self.assertEqual(len(result["skipped"]), 3)

    def test_submit_unheld_using_buying_power(self):
        modules, APIError = with_alpaca_modules()
        sent = []
        def submit(order_data):
            sent.append(order_data.kwargs)
            return SimpleNamespace(id="test-1")
        trader = SimpleNamespace(
            get_clock=lambda: SimpleNamespace(is_open=True),
            get_all_positions=lambda: [],
            get_orders=lambda filter: [],
            get_order_by_client_id=lambda x: (_ for _ in ()).throw(APIError("404")),
            get_asset=lambda sym: SimpleNamespace(tradable=True, fractionable=True),
            get_account=lambda: SimpleNamespace(buying_power="25000", trading_blocked=False),
            submit_order=submit,
        )
        with patch.dict(sys.modules, modules):
            result = main.buy_signals(trader, [("AAPL", Decimal(100))], fake_settings(dry_run=False), "2026-10-19")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["notional"], 500.0)
        self.assertEqual(sent[0]["client_order_id"], "v2sp100-20261019-AAPL")
        self.assertEqual(result["ordered"][0][0], "AAPL")

    def test_market_closed_skips_all(self):
        trader = SimpleNamespace(get_clock=lambda: SimpleNamespace(is_open=False))
        self.assertEqual(main.run_cycle(None, trader, fake_settings(), datetime(2026, 10, 19, 12,
                                                                                  tzinfo=main.DENVER)),
                         {"status": "market-closed"})


if __name__ == "__main__":
    unittest.main()
