"""Offline regression tests for the six-step low-water ladder."""
import os
import tempfile
import unittest
from datetime import date,timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import lowwater_tracker as lw


def pos(price="100",qty="1",entry="100",symbol="AAPL"):
    gain=Decimal(price)/Decimal(entry)-1
    return SimpleNamespace(symbol=symbol,current_price=price,
        avg_entry_price=entry,unrealized_plpc=str(gain),qty=qty,
        asset_class="us_equity",side="long")


def filled(cid,qty="0.1",state="filled",side="buy"):
    return SimpleNamespace(client_order_id=cid,filled_qty=qty,
                           status=state,side=side)


class LowWaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=os.path.join(self.tmp.name,"lowwater.sqlite")
        self.orders={}
        self.day=date(2026,11,9)
    def nextday(self):
        self.day+=timedelta(days=1)
        return self.day.isoformat()
    def observe(self,p,day=None,readonly=False):
        return lw.observe(p,day or self.day.isoformat(),
               lookup=lambda cid:self.orders.get(cid),
               path=self.path,readonly=readonly)
    def state(self):
        with lw.connect(self.path) as db:
            row=db.execute("SELECT * FROM ladder WHERE symbol='AAPL'").fetchone()
            return dict(row) if row else None
    def reserve_and_fill(self,plan):
        self.assertEqual(plan["status"],"READY")
        lw.reserve(plan,self.day.isoformat(),path=self.path)
        self.orders[plan["cid"]]=filled(plan["cid"])
    def test_tiny_initial_position_scales_to_minimum_notional(self):
        # $5 entry yields original raw rungs $0.25 and $0.50.
        self.observe(pos("100",qty=".05"))
        self.nextday()
        first=self.observe(pos("99",qty=".05"))
        self.assertEqual(first["status"],"READY")
        self.assertEqual(first["initial_notional"],Decimal("5.00"))
        self.assertEqual(first["amount"],Decimal("1.00"))
        self.reserve_and_fill(first)
        self.nextday()
        second=self.observe(pos("98",qty=".06"))
        self.assertEqual(second["status"],"READY")
        self.assertEqual(second["step"],2)
        self.assertEqual(second["amount"],Decimal("1.00"))
        # Later steps retain their normal fractions; no single rung
        # may exceed the initial $5 cost basis.
        self.assertLessEqual(second["amount"],second["initial_notional"])

    def test_portfolio_empty_needs_two_different_sessions(self):
        self.observe(pos("100"))
        monday="2026-11-09"
        tuesday="2026-11-10"
        self.assertFalse(lw.confirm_portfolio_snapshot(monday,empty=True,path=self.path))
        self.assertFalse(lw.confirm_portfolio_snapshot(monday,empty=True,path=self.path))
        self.assertIsNotNone(self.state())
        self.assertTrue(lw.confirm_portfolio_snapshot(tuesday,empty=True,path=self.path))
        lw.drop_absent(set(),path=self.path,session=tuesday)
        self.assertIsNone(self.state())

    def test_real_portfolio_resets_empty_counter(self):
        self.observe(pos("100"))
        self.assertFalse(lw.confirm_portfolio_snapshot("2026-11-09",empty=True,path=self.path))
        self.assertTrue(lw.confirm_portfolio_snapshot("2026-11-10",empty=False,path=self.path))
        self.assertFalse(lw.confirm_portfolio_snapshot("2026-11-11",empty=True,path=self.path))
        self.assertIsNotNone(self.state())

    def test_health_audit_summarizes_steps_and_pending_orders(self):
        self.observe(pos("100"))
        self.nextday()
        plan=self.observe(pos("99"))
        lw.reserve(plan,self.day.isoformat(),path=self.path)
        audit=lw.audit(path=self.path)
        self.assertEqual(audit["tracked"],1)
        self.assertEqual(audit["step_counts"],[1,0,0,0,0,0,0])
        self.assertEqual(audit["pending"],["AAPL"])
        self.assertEqual(audit["flagged"],[])

    def test_first_observation_only_seeds_and_no_buy(self):
        p=pos("95")
        self.assertEqual(self.observe(p)["status"],"SEEDED")
        s=self.state()
        self.assertEqual(s["initial_notional"],"100.00")
        self.assertEqual(s["observed_low"],"95")
        self.assertEqual(s["steps"],0)
        self.assertEqual(self.observe(p)["status"],"ALREADY_OBSERVED")
    def test_one_percent_increment_and_all_time_daily_low(self):
        self.observe(pos("100"))
        self.nextday()
        self.assertEqual(self.observe(pos("99.5"))["status"],"NO_QUALIFYING_LOW")
        self.nextday()
        self.assertEqual(self.observe(pos("99.2"))["status"],"NO_QUALIFYING_LOW")
        self.nextday()
        plan=self.observe(pos("98.99"))
        self.assertEqual(plan["step"],1)
        self.assertEqual(plan["amount"],Decimal("5.00"))
        self.assertEqual(self.state()["observed_low"],"98.99")
    def test_rebound_then_old_low_does_not_rebuy(self):
        self.observe(pos("100"))
        self.nextday();self.reserve_and_fill(self.observe(pos("99")))
        self.nextday()
        self.assertEqual(self.observe(pos("101",qty="1.05"))["status"],
                         "NO_QUALIFYING_LOW")
        self.nextday()
        self.assertEqual(self.observe(pos("99.5",qty="1.05"))["status"],
                         "NO_QUALIFYING_LOW")
        self.nextday()
        self.assertEqual(self.observe(pos("99.0",qty="1.05"))["status"],
                         "NO_QUALIFYING_LOW")
        self.nextday()
        plan=self.observe(pos("97.99",qty="1.05"))
        self.assertEqual(plan["step"],2)
        self.assertEqual(plan["amount"],Decimal("10.00"))
    def test_six_filled_steps_capped_and_review_flag_persistent(self):
        self.observe(pos("100"))
        prices=["99","98","97","96","95","94"]
        amounts=["5.00","10.00","20.00","40.00","80.00","100.00"]
        qty=Decimal("1")
        for i,(price,expected) in enumerate(zip(prices,amounts),start=1):
            self.nextday()
            plan=self.observe(pos(price,qty=str(qty)))
            self.assertEqual(plan["status"],"READY")
            self.assertEqual(plan["step"],i)
            self.assertEqual(plan["amount"],Decimal(expected))
            self.reserve_and_fill(plan)
            qty+=Decimal(expected)/Decimal(price)
        self.nextday()
        last=self.observe(pos("93",qty=str(qty)))
        self.assertEqual(last["status"],"REVIEW_REQUIRED")
        self.assertEqual(last["reason"],"CAP_REACHED_REVIEW_REQUIRED")
        self.assertEqual(self.state()["steps"],6)
        with lw.connect(self.path) as db:
            statuses=[r["event"] for r in db.execute(
                "SELECT event FROM ladder_events WHERE symbol='AAPL'")]
        self.assertEqual(statuses.count("STEP_FILL_CONFIRMED"),6)
        self.assertEqual(statuses.count("ORDER_RESERVED"),6)
        self.assertEqual(self.state()["initial_notional"],"100.00")
        self.assertEqual(sum(Decimal(x) for x in amounts),Decimal("255.00"))
        self.nextday()
        self.assertEqual(self.observe(pos("92",qty=str(qty)))["status"],
                         "REVIEW_REQUIRED")
    def test_unknown_order_does_not_advance_or_retry(self):
        self.observe(pos("100"))
        self.nextday();plan=self.observe(pos("99"))
        lw.reserve(plan,self.day.isoformat(),path=self.path)
        self.nextday()
        status=self.observe(pos("98"))  # broker can't find submitted ID
        self.assertEqual(status["status"],"REVIEW_REQUIRED")
        self.assertEqual(self.state()["steps"],0)
        self.assertEqual(self.state()["flag"],"BROKER_ORDER_UNVERIFIABLE")
    def test_open_order_does_not_advance(self):
        self.observe(pos("100"))
        self.nextday();plan=self.observe(pos("99"))
        lw.reserve(plan,self.day.isoformat(),path=self.path)
        self.orders[plan["cid"]]=filled(plan["cid"],qty="0",state="new")
        self.nextday();x=self.observe(pos("98"))
        self.assertEqual(x["status"],"PENDING_ORDER")
        self.assertEqual(self.state()["steps"],0)
    def test_rejected_order_flags_review(self):
        self.observe(pos("100"))
        self.nextday();plan=self.observe(pos("99"))
        lw.reserve(plan,self.day.isoformat(),path=self.path)
        self.orders[plan["cid"]]=filled(plan["cid"],qty="0",state="rejected")
        self.nextday()
        self.assertEqual(self.observe(pos("98"))["reason"],"UNFILLED_ORDER_REVIEW")
    def test_held_position_addition_outside_ladder_requires_review(self):
        self.observe(pos("100",qty="1"))
        self.nextday()
        x=self.observe(pos("99",qty="1.5"))
        self.assertEqual(x["reason"],"UNTRACKED_POSITION_ADDITION")
    def test_average_cost_changes_do_not_reset_original_notional(self):
        self.observe(pos("100",qty="1"))
        self.nextday();plan=self.observe(pos("99"))
        self.reserve_and_fill(plan)
        self.nextday()
        p=pos("98",qty="1.05",entry="99.95")
        out=self.observe(p)
        self.assertEqual(out["status"],"READY")
        self.assertEqual(out["amount"],Decimal("10.00"))
        self.assertEqual(self.state()["initial_notional"],"100.00")
    def test_full_position_close_reentry_is_new_episode(self):
        self.observe(pos("100"))
        old=self.state()["episode"]
        self.nextday()
        lw.drop_absent(set(),path=self.path,session=self.day.isoformat())
        self.assertIsNone(self.state())
        self.nextday()
        self.assertEqual(self.observe(pos("94"))["status"],"SEEDED")
        self.assertNotEqual(self.state()["episode"],old)
        self.assertEqual(self.state()["steps"],0)
    def test_broker_quote_mismatch_is_rejected(self):
        bad=pos("94")
        bad.unrealized_plpc="0.02"
        self.assertEqual(self.observe(bad)["status"],"INVALID_POSITION")
        self.assertIsNone(self.state())
    def test_read_only_preview_never_writes(self):
        self.assertEqual(self.observe(pos("100"),readonly=True)["status"],
                         "NEW_POSITION_SEED_ONLY")
        self.assertIsNone(self.state())
        self.observe(pos("100"))
        self.nextday()
        self.assertEqual(self.observe(pos("99"),readonly=True)["status"],
                         "PREVIEW_NEW_LOW")
        self.assertEqual(self.state()["last_session"],"2026-11-09")
    def test_reservation_idempotency(self):
        self.observe(pos("100"))
        self.nextday();plan=self.observe(pos("99"))
        lw.reserve(plan,self.day.isoformat(),path=self.path)
        with self.assertRaisesRegex(RuntimeError,"RESERVATION_CONFLICT"):
            lw.reserve(plan,self.day.isoformat(),path=self.path)


if __name__=="__main__":
    unittest.main()
