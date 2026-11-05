"""Trader.place 的重试/恢复/防重复下单逻辑 —— 不碰真实同花顺。

用替身替换 _place_once / 就绪检查 / 委托表计数, 只验证编排逻辑。
真实界面的故障态演练见 second-day 仓库的 live/drill.py。

运行: python -m unittest discover -s tests  (在 auto-trade 目录下)
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_trade import Config                                   # noqa: E402
from auto_trade.trader import Result, Status, Trader            # noqa: E402

logging.getLogger("auto_trade").setLevel(logging.CRITICAL)


def R(st, msg="", sent=False):
    return Result(st, "buy", "600303", "2.70", "100", msg=msg, maybe_sent=sent)


class Base(unittest.TestCase):
    def make(self, *, dry=True, **kw):
        tr = Trader(Config(dry_run=dry, trade_password="x",
                           state_file="/tmp/at_test_state.json",
                           log_file="/tmp/at_test.log", **kw))
        self.seq = []
        tr.ensure_ready = lambda: (self.seq.append("quick") or (True, ""))
        tr.ax.ready_to_trade = lambda pw=None: (self.seq.append("full") or (True, ""))
        tr.restart_app = lambda: (self.seq.append("restart") or (True, ""))
        tr._count_orders = lambda *a: 0
        return tr

    def script(self, tr, results, side_effect=None):
        self.calls = 0

        def fake(*a, **k):
            r = results[min(self.calls, len(results) - 1)]
            self.calls += 1
            if side_effect:
                side_effect()
            return r
        tr._place_once = fake

    def place(self, tr, amount=100):
        return tr.place("buy", "600303", 2.70, amount)


class TestEscalation(Base):
    def test_success_first_try(self):
        tr = self.make()
        self.script(tr, [R(Status.FILLED_ONLY)])
        self.assertTrue(self.place(tr).ok)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.seq, ["quick"])

    def test_escalates_quick_full_restart(self):
        tr = self.make()
        self.script(tr, [R(Status.ERROR), R(Status.ERROR), R(Status.FILLED_ONLY)])
        r = self.place(tr)
        self.assertTrue(r.ok)
        self.assertEqual(self.seq, ["quick", "full", "restart"])

    def test_gives_up_after_max_attempts(self):
        tr = self.make()
        self.script(tr, [R(Status.ERROR)])
        r = self.place(tr)
        self.assertEqual(r.status, Status.ERROR)
        self.assertEqual(self.calls, 3)
        self.assertIn("3次尝试均未完成", r.msg)

    def test_not_ready_moves_to_next_level(self):
        tr = self.make()
        tr.ensure_ready = lambda: (self.seq.append("quick") or (False, ""))
        tr.ax.ready_to_trade = lambda pw=None: (self.seq.append("full") or (False, ""))
        self.script(tr, [R(Status.FILLED_ONLY)])
        self.assertTrue(self.place(tr).ok)
        self.assertEqual(self.seq, ["quick", "full", "restart"])
        self.assertEqual(self.calls, 1)

    def test_unrecognized_is_retried(self):
        tr = self.make()
        self.script(tr, [R(Status.UNRECOGNIZED), R(Status.FILLED_ONLY)])
        self.assertTrue(self.place(tr).ok)
        self.assertEqual(self.calls, 2)

    def test_final_statuses_not_retried(self):
        for st in (Status.BLOCKED, Status.PRICE_DRIFT, Status.AWAITING_CONFIRM):
            tr = self.make()
            self.script(tr, [R(st)])
            self.assertEqual(self.place(tr).status, st)
            self.assertEqual(self.calls, 1, st)

    def test_risk_block_touches_nothing(self):
        tr = self.make()
        self.script(tr, [R(Status.FILLED_ONLY)])
        r = self.place(tr, amount=150)
        self.assertEqual(r.status, Status.BLOCKED)
        self.assertEqual(self.seq, [])
        self.assertEqual(self.calls, 0)

    def test_self_heal_off_single_attempt(self):
        tr = self.make(self_heal=False)
        self.script(tr, [R(Status.ERROR)])
        self.place(tr)
        self.assertEqual(self.calls, 1)


class TestNoDuplicateOrders(Base):
    """真实提交时绝不能因为重试而下出两张单。"""

    def test_sent_but_unconfirmed_detected(self):
        tr = self.make(dry=False)
        table = {"n": 1}
        tr._count_orders = lambda *a: table["n"]
        self.script(tr, [R(Status.NO_RECORD, sent=True)],
                    side_effect=lambda: table.__setitem__("n", table["n"] + 1))
        r = self.place(tr)
        self.assertEqual(r.status, Status.SUBMITTED)
        self.assertEqual(self.calls, 1)

    def test_really_not_sent_is_resent(self):
        tr = self.make(dry=False)
        self.script(tr, [R(Status.NO_RECORD, sent=True), R(Status.SUBMITTED)])
        self.assertEqual(self.place(tr).status, Status.SUBMITTED)
        self.assertEqual(self.calls, 2)

    def test_final_recheck_catches_late_order(self):
        tr = self.make(dry=False)
        reads = {"k": 0}

        def cnt(*a):
            reads["k"] += 1
            return 0 if reads["k"] < 4 else 1
        tr._count_orders = cnt
        self.script(tr, [R(Status.ERROR)])
        self.assertEqual(self.place(tr).status, Status.SUBMITTED)

    def test_maybe_sent_without_baseline_stops(self):
        tr = self.make(dry=False)
        tr._count_orders = lambda *a: None
        self.script(tr, [R(Status.ERROR, sent=True)])
        r = self.place(tr)
        self.assertEqual(r.status, Status.ERROR)
        self.assertEqual(self.calls, 1)
        self.assertIn("无法排除重复下单", r.msg)

    def test_not_sent_without_baseline_still_retries(self):
        tr = self.make(dry=False)
        tr._count_orders = lambda *a: None
        self.script(tr, [R(Status.ERROR, sent=False)])
        self.place(tr)
        self.assertEqual(self.calls, 3)

    def test_dry_run_ignores_dedupe(self):
        tr = self.make(dry=True)
        tr._count_orders = lambda *a: None
        self.script(tr, [R(Status.ERROR, sent=True), R(Status.FILLED_ONLY)])
        self.assertTrue(self.place(tr).ok)
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
