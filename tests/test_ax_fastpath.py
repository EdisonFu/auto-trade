"""AX 快路径与安全护栏回归: 不碰真实同花顺。"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_trade.ax as A                                            # noqa: E402
from auto_trade import Config                                        # noqa: E402
from auto_trade.trader import Status, Trader                         # noqa: E402


logging.getLogger("auto_trade").setLevel(logging.CRITICAL)


class FakeElement:
    def __init__(self, value: str):
        self._value = value

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


class FakePanel:
    def __init__(self, direction="买入", values=None, reset=None):
        self.direction = direction
        self.code = FakeElement("")
        self.price = FakeElement("")
        self.amount = FakeElement("")
        self.submit = object()
        self.reset = reset
        for el, val in (values or {}).items():
            setattr(el, "_value", val)


class TestConfirmDialogNoGeom(unittest.TestCase):
    def test_confirm_dialog_detected_without_geom(self):
        class Child:
            def __init__(self, role, text=None):
                self.role, self.text = role, text
                self.children = []

            def __hash__(self):
                return id(self)

            def __eq__(self, other):
                return self is other

        confirm = Child("AXButton", "确认")
        detail = Child("AXStaticText",
                       "买入委托 证券代码:600303 当前价格:2.70 数量:100")
        root = Child("AXWindow")
        root.children = [confirm, detail]

        real_geom = A.geom
        real_attr = A.attr
        A.geom = lambda _el: (_ for _ in ()).throw(AssertionError("geom called"))
        A.attr = lambda el, name: {
            "AXRole": el.role,
            "AXValue": el.text,
            "AXTitle": el.text,
            "AXDescription": el.text,
            "AXChildren": el.children,
        }.get(name)
        try:
            ax = A.ThsAX()
            ax.window = lambda: root
            dlg = ax.find_confirm_dialog()
        finally:
            A.geom = real_geom
            A.attr = real_attr
        self.assertIsNotNone(dlg)
        self.assertIs(dlg["confirm"], confirm)
        self.assertIn("证券代码:600303", dlg["text"])

    def test_confirm_order_reuses_dialog(self):
        ax = A.ThsAX()
        button = object()
        dlg = {"text": "ok", "confirm": button, "cancel": object()}
        pressed = []

        ax.press = lambda el: pressed.append(el)
        ax.find_confirm_dialog = lambda: (_ for _ in ()).throw(
            AssertionError("should not rescan"))
        ax.wait_until = lambda *a, **k: True
        self.assertEqual(ax.confirm_order(dlg), "ok")
        self.assertEqual(pressed, [button])


class TestFastPathPanel(unittest.TestCase):
    def test_blank_panel_skips_reset(self):
        ax = A.ThsAX()
        panel = FakePanel(reset=object())
        ax.find_panel = lambda: panel
        ax.find_confirm_dialog = lambda: None
        ax.read = lambda el: el._value
        ax.reset = lambda _panel: (_ for _ in ()).throw(
            AssertionError("reset should be skipped"))

        out = ax.reset_to_order_page("buy")
        self.assertIs(out, panel)

    def test_residual_panel_uses_reset(self):
        ax = A.ThsAX()
        panel = FakePanel(reset=object())
        panel.price._value = "12.30"
        ax.find_panel = lambda: panel
        ax.find_confirm_dialog = lambda: None
        ax.read = lambda el: el._value
        calls = []
        ax.reset = lambda p: calls.append(p) or True

        out = ax.reset_to_order_page("buy")
        self.assertIs(out, panel)
        self.assertEqual(calls, [panel])

    def test_residual_panel_goes_full_reset(self):
        ax = A.ThsAX()
        panel = FakePanel(reset=object())
        panel.amount._value = "100"
        ax.find_panel = lambda: panel
        ax.find_confirm_dialog = lambda: None
        ax.read = lambda el: el._value
        calls = []
        ax.reset = lambda p: calls.append("reset") or False
        ax.dismiss_dialogs = lambda: calls.append("dismiss")
        ax.goto_trade_page = lambda: (_ for _ in ()).throw(
            AssertionError("direction already matches"))
        ax.focus = lambda el: None
        ax._key = lambda key: setattr(panel.amount, "_value", "")
        ax.window = lambda: type("W", (), {})()
        ax.reset_to_order_page("buy")
        self.assertIn("reset", calls)
        self.assertIn("dismiss", calls)


class TestFastFillGuards(unittest.TestCase):
    def test_fast_fill_uses_set_value_only_for_amount_price(self):
        tr = Trader(Config(dry_run=True, fast_fill=True, trade_password="x",
                           state_file="/tmp/at_ff_state.json",
                           log_file="/tmp/at_ff.log"))
        panel = FakePanel()
        panel.code = FakeElement("600303")
        panel.amount = FakeElement("")
        panel.price = FakeElement("")
        panel.direction = "买入"
        tr.switch = lambda _side: panel
        st = A.PanelState(code="600303", price="2.70", amount="100",
                          stock_name="演练股", limit_up="2.86",
                          limit_down="2.34", available="可买:300股")
        tr.ax.read_state = lambda _panel: st
        calls = []
        tr.ax.type_text = lambda el, text: calls.append(("type", el, text))
        tr.ax.set_value = lambda el, text: calls.append(("set", el, text))
        tr.ax.read = lambda el: el._value
        tr.ax.dismiss_error_dialog = lambda: None

        r = tr.place("buy", "600303", 2.70, 100)
        self.assertEqual(r.status, Status.FILLED_ONLY)
        kinds = {c[0] for c in calls}
        self.assertEqual(kinds, {"type", "set"})
        typed = [c for c in calls if c[0] == "type"]
        self.assertEqual(len(typed), 1)
        self.assertIs(typed[0][1], panel.code)
        set_targets = {id(c[1]) for c in calls if c[0] == "set"}
        self.assertEqual(set_targets, {id(panel.amount), id(panel.price)})

    def test_confirm_guard_still_present(self):
        src = (Path(A.__file__).with_name("trader.py")
               .read_text(encoding="utf-8"))
        self.assertIn('checks.append(("数量", amount_str))', src)
        self.assertIn("self.ax.confirm_order(dlg)", src)


if __name__ == "__main__":
    unittest.main()
