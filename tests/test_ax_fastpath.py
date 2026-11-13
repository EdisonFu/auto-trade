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


class TestFrontmost(unittest.TestCase):
    class _App:
        def __init__(self, active):
            self._active = active

        def isActive(self):
            return self._active

    def test_active_no_activate(self):
        ax = A.ThsAX()
        calls = []
        ax.activate = lambda: calls.append(1)
        real = A.running_apps
        A.running_apps = lambda: [self._App(True)]
        try:
            ax.ensure_frontmost()
        finally:
            A.running_apps = real
        self.assertEqual(calls, [])

    def test_background_activates(self):
        ax = A.ThsAX()
        calls = []
        ax.activate = lambda: calls.append(1)
        real = A.running_apps
        A.running_apps = lambda: [self._App(False)]
        try:
            ax.ensure_frontmost()
        finally:
            A.running_apps = real
        self.assertEqual(calls, [1])

    def test_type_text_checks_front_before_focus(self):
        ax = A.ThsAX()
        seq = []
        ax.ensure_frontmost = lambda: seq.append("front")
        ax.focus = lambda el: seq.append("focus")
        ax.read = lambda el: ""
        ax._key = lambda k: None
        ax.settle = 0.0
        ax.type_text(object(), "1", clear_first=False, verify=False)
        self.assertEqual(seq, ["front", "focus"])

    def test_ensure_ready_activates_once_per_batch(self):
        from auto_trade import Config
        from auto_trade.trader import Trader
        tr = Trader(Config(dry_run=True, state_file="/tmp/at_ff_state.json",
                           log_file="/tmp/at_ff.log"))
        seq = []
        tr.ax.wake_screen = lambda: None
        tr.ax.ensure_frontmost = lambda: seq.append("front")
        tr.ax.find_blocking_window = lambda: None
        tr.ax.login_sheet = lambda: None
        tr.ax.nav_buttons = lambda: [(1, 100.0, object())]
        tr.ax.read_funds = lambda: {"可用金额": 100.0}
        ok, _ = tr.ensure_ready()
        self.assertTrue(ok)
        self.assertEqual(seq, ["front"])
        ok, _ = tr.ensure_ready()
        self.assertTrue(ok)
        self.assertEqual(seq, ["front"])   # 缓存命中, 不再激活


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
        tr.ensure_ready = lambda: (True, "stub")

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


def _cg(wid, x, y, w, h, pid=4242, layer=0):
    return {"kCGWindowNumber": wid, "kCGWindowOwnerPID": pid,
            "kCGWindowLayer": layer,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


MAIN = (0.0, 33.0, 1512.0, 859.0)


class TestBlockingWindow(unittest.TestCase):
    """11-13: AX 看不见的券商重登弹窗锁住主窗口, 自检全过却下不了单。"""

    def setUp(self):
        A.pop_blocker_events()
        self._cg = A.Quartz.CGWindowListCopyWindowInfo
        self.cg = []
        A.Quartz.CGWindowListCopyWindowInfo = lambda *a: self.cg
        self.ax = A.ThsAX()
        self.ax._pid = lambda: 4242
        self.ax._ax_window_bounds = lambda: [MAIN]
        self.ax._onscreen_windows = lambda: 2
        self.ax.dismiss_error_dialog = lambda: None
        self.ax.activate = lambda timeout=3.0: (_ for _ in ()).throw(
            AssertionError("screen on: no activate"))

    def tearDown(self):
        A.Quartz.CGWindowListCopyWindowInfo = self._cg

    def test_main_window_sliding_not_blocking(self):
        # 切桌面动画中: 窗口服务器 x=-1532, AX 仍报 x=0
        self.cg = [_cg(74912, -1532, 33, 1512, 859)]
        self.assertIsNone(self.ax.find_blocking_window())

    def test_screen_off_wakes_before_scan(self):
        # 熄屏时窗口服务器报 0 个在屏窗口, 不点亮会漏判(11-13 22:43 实测)
        seq = []
        self.ax._onscreen_windows = lambda: 0
        self.ax.activate = lambda timeout=3.0: seq.append("activate")
        saved = A.subprocess.run, A.time.sleep
        A.subprocess.run = lambda cmd, **k: seq.append(cmd[0])
        A.time.sleep = lambda s: None
        self.cg = [_cg(1, *MAIN), _cg(74924, 530, 316, 453, 292)]
        try:
            blk = self.ax.find_blocking_window()
        finally:
            A.subprocess.run, A.time.sleep = saved
        self.assertEqual(seq, ["caffeinate", "activate"])
        self.assertEqual(blk["id"], 74924)

    def test_ax_invisible_popup_detected(self):
        self.cg = [_cg(1, *MAIN), _cg(74924, 530, 316, 453, 292)]
        blk = self.ax.find_blocking_window()
        self.assertEqual(blk["id"], 74924)
        self.assertEqual(blk["bounds"], (530, 316, 453, 292))

    def test_ax_visible_sheet_not_blocking(self):
        sheet = (530.0, 316.0, 453.0, 292.0)
        self.ax._ax_window_bounds = lambda: [MAIN, sheet]
        self.cg = [_cg(1, *MAIN), _cg(2, *sheet)]
        self.assertIsNone(self.ax.find_blocking_window())

    def test_non_login_sheet_is_blocking(self):
        # 23:39: 「当日盈亏」提示框是主窗口下的 AXSheet(只有[已知晓]),
        # AX 看得见但同样锁主窗口; 只有交易登录 sheet 放行
        class W:
            def __init__(self, g, kids=(), role="AXWindow", sub="AXStandardWindow"):
                self.g, self.kids, self.role, self.sub = g, list(kids), role, sub
        notice = W((529.0, 324.0, 454.0, 276.0), role="AXSheet", sub=None,
                   kids=[W((0, 0, 1, 1), role="AXButton")])
        main = W(MAIN, kids=[W((0, 0, 1, 1), role="AXButton")] * 5 + [notice])
        app = object()
        saved = A.attr, A.geom, A.AXUIElementCreateApplication
        A.AXUIElementCreateApplication = lambda pid: app
        A.attr = lambda el, n: ([main] if el is app and n == "AXWindows" else
                                (el.kids if n == "AXChildren" else
                                 el.role if n == "AXRole" else
                                 el.sub if n == "AXSubrole" else None))
        A.geom = lambda el: el.g
        try:
            ax = A.ThsAX()
            ax._pid = lambda: 4242
            ax._onscreen_windows = lambda: 2
            ax.login_sheet = lambda: None
            bounds = ax._ax_window_bounds()
            self.cg = [_cg(79343, *MAIN), _cg(79360, 529, 324, 454, 276)]
            blk = ax.find_blocking_window()
            ax.login_sheet = lambda: notice          # 若它是登录框则放行
            self.assertIsNone(ax.find_blocking_window())
        finally:
            A.attr, A.geom, A.AXUIElementCreateApplication = saved
        self.assertEqual(bounds, [MAIN])
        self.assertEqual(blk["id"], 79360)

    def test_small_toast_other_layer_and_unlock_ignored(self):
        self.cg = [_cg(1, *MAIN),
                   _cg(2, 910, 74, 269, 73, layer=25),     # 断线提示
                   _cg(3, 265, 175, 64, 64),               # 小图标
                   _cg(4, 500, 200, 440, 515),             # 密码解锁框
                   _cg(5, 530, 316, 453, 292, pid=999)]    # 别的进程
        self.assertIsNone(self.ax.find_blocking_window())

    def test_clear_restarts_and_relogins(self):
        seq = []
        states = iter([{"id": 7, "bounds": (1, 2, 3, 4)}, None])
        self.ax.find_blocking_window = lambda: next(states)
        self.ax.capture_window = lambda b: seq.append(("shot", b)) or "/tmp/x.png"
        self.ax.ocr_image = lambda p: [("请输入验证码", .5, .5, .1, .1)]
        self.ax.activate = lambda timeout=3.0: None
        self.ax._press_ax_safe_button = lambda blk: (None, "")
        self.ax.quit_app = lambda: seq.append("quit") or True
        self.ax.launch_app = lambda: seq.append("launch") or True
        self.ax.login = lambda pw: seq.append(("login", pw)) or (True, "ok")
        ok, why = self.ax.clear_blocking_window("pw")
        self.assertTrue(ok)
        self.assertEqual(seq, [("shot", (1, 2, 3, 4)), "quit", "launch", ("login", "pw")])
        self.assertIn("/tmp/x.png", why)

    def test_clear_clicks_safe_button_without_restart(self):
        # 11-13 截图 OCR 结果: 提示框只有[已知晓]
        clicks = []
        states = iter([{"id": 7, "bounds": (100, 200, 454, 276)}, None, None])
        self.ax.find_blocking_window = lambda: next(states)
        self.ax.capture_window = lambda b: "/tmp/x.png"
        self.ax._press_ax_safe_button = lambda blk: (None, "")
        self.ax.ocr_image = lambda p: [("提示", .5, .1, .1, .05),
                                       ("我公司于2016年9月5日起提供", .45, .37, .6, .05),
                                       ("已知晓", .5, .88, .1, .05)]
        self.ax.activate = lambda timeout=3.0: None
        self.ax._click_at = lambda x, y: clicks.append((round(x), round(y)))
        self.ax.quit_app = lambda: (_ for _ in ()).throw(AssertionError("no restart"))
        saved = A.wait_until
        A.wait_until = lambda cond, **k: cond()
        try:
            ok, why = self.ax.clear_blocking_window("pw")
        finally:
            A.wait_until = saved
        self.assertTrue(ok)
        self.assertEqual(clicks, [(round(100 + 454 * .5), round(200 + 276 * .88))])
        self.assertIn("已知晓", why)
        ev = A.pop_blocker_events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["action"], "点[已知晓]")
        self.assertIn("2016年9月5日", ev[0]["text"])
        self.assertEqual(A.pop_blocker_events(), [])     # 取走即清空

    def test_foreign_overlay_reported(self):
        # 窗口列表前→后: 系统更新弹窗压在同花顺主窗口上面
        self.cg = [_cg(10, 0, 0, 1512, 33, pid=1, layer=24) | {"kCGWindowOwnerName": "Window Server"},
                   _cg(11, 0, 0, 1512, 982, pid=2, layer=20) | {"kCGWindowOwnerName": "程序坞"},
                   _cg(12, 500, 300, 420, 180, pid=3) | {"kCGWindowOwnerName": "软件更新"},
                   _cg(1, *MAIN),
                   _cg(13, 100, 100, 600, 400, pid=4) | {"kCGWindowOwnerName": "iTerm2"}]
        over = self.ax.find_foreign_overlay()
        self.assertEqual([o["owner"] for o in over], ["软件更新"])   # 主窗口后面的不算

    def test_no_foreign_overlay_when_ths_on_top(self):
        self.cg = [_cg(1, *MAIN),
                   _cg(13, 100, 100, 600, 400, pid=4) | {"kCGWindowOwnerName": "iTerm2"}]
        self.assertEqual(self.ax.find_foreign_overlay(), [])

    def test_unsafe_buttons_never_clicked(self):
        # 只有[登录]/[确定]: 不点, 走重启
        self.ax.ocr_image = lambda p: [("登录", .7, .88, .1, .05),
                                       ("确定", .5, .88, .1, .05),
                                       ("请输入取消原因", .5, .5, .3, .05)]
        self.ax._click_at = lambda x, y: (_ for _ in ()).throw(AssertionError("clicked"))
        name, text = self.ax._click_safe_button(
            {"id": 7, "bounds": (0, 0, 100, 100)}, "/tmp/x.png")
        self.assertIsNone(name)
        self.assertIn("登录", text)

    def test_cancel_preferred_over_login(self):
        clicks = []
        self.ax.ocr_image = lambda p: [("登录", .78, .88, .1, .05),
                                       ("取消", .57, .88, .05, .05)]
        self.ax.activate = lambda timeout=3.0: None
        self.ax._click_at = lambda x, y: clicks.append((round(x), round(y)))
        name, _text = self.ax._click_safe_button(
            {"id": 7, "bounds": (0, 0, 100, 100)}, "/tmp/x.png")
        self.assertEqual(name, "取消")
        self.assertEqual(clicks, [(57, 88)])

    def test_ax_sheet_button_pressed_before_ocr(self):
        # 23:47: sheet 弹窗用坐标点 4 次没点中; AX 能看到就直接 AXPress
        class W:
            def __init__(self, g, kids=(), role="AXWindow", text="", sub="AXStandardWindow"):
                self.g, self.kids, self.role, self.text, self.sub = g, list(kids), role, text, sub
        ok_btn = W((0, 0, 1, 1), role="AXButton", text="已知晓")
        body = W((0, 0, 1, 1), role="AXStaticText", text="关于\"当日盈亏\"的提示")
        notice = W((529, 324, 454, 276), role="AXSheet", sub=None,
                   kids=[W((0, 0, 1, 1), role="AXScrollArea", kids=[body]), ok_btn])
        main = W(MAIN, kids=[W((0, 0, 1, 1), role="AXButton")] * 5 + [notice])
        app = object()
        pressed = []
        saved = A.attr, A.geom, A.AXUIElementCreateApplication
        A.AXUIElementCreateApplication = lambda pid: app
        A.attr = lambda el, n: ([main] if el is app and n == "AXWindows" else
                                (el.kids if n == "AXChildren" else
                                 el.role if n == "AXRole" else
                                 el.sub if n == "AXSubrole" else
                                 el.text if n in ("AXValue", "AXTitle") else None))
        A.geom = lambda el: el.g
        try:
            self.ax.press = lambda el: pressed.append(el)
            name, text = self.ax._press_ax_safe_button(
                {"id": 79360, "bounds": (529.0, 324.0, 454.0, 276.0)})
        finally:
            A.attr, A.geom, A.AXUIElementCreateApplication = saved
        self.assertEqual(name, "已知晓")
        self.assertEqual(pressed, [ok_btn])
        self.assertIn("当日盈亏", text)

    def test_clear_uses_ax_press_and_skips_ocr(self):
        states = iter([{"id": 7, "bounds": (529, 324, 454, 276)}, None, None])
        self.ax.find_blocking_window = lambda: next(states)
        self.ax.activate = lambda timeout=3.0: None
        self.ax.capture_window = lambda b: "/tmp/x.png"
        self.ax._press_ax_safe_button = lambda blk: ("已知晓", "当日盈亏提示")
        self.ax._click_safe_button = lambda *a: (_ for _ in ()).throw(
            AssertionError("OCR not needed"))
        saved = A.wait_until
        A.wait_until = lambda cond, **k: cond()
        try:
            ok, why = self.ax.clear_blocking_window("pw")
        finally:
            A.wait_until = saved
        self.assertTrue(ok)
        self.assertEqual(A.pop_blocker_events()[0]["text"], "当日盈亏提示")

    def test_login_popup_cancel_never_clicked(self):
        # 11-13 09:25 重登框 OCR 结果含[取消], 但点了只是关框、会话仍断
        self.ax.ocr_image = lambda p: [("交易密码", .5, .5, .1, .05),
                                       ("验证码", .5, .7, .1, .05),
                                       ("取消", .57, .88, .05, .05),
                                       ("登录", .78, .88, .1, .05)]
        self.ax._click_at = lambda x, y: (_ for _ in ()).throw(AssertionError("clicked"))
        name, text = self.ax._click_safe_button(
            {"id": 7, "bounds": (0, 0, 100, 100)}, "/tmp/x.png")
        self.assertIsNone(name)
        self.assertIn("验证码", text)

    def test_broker_error_dialog_closed_without_restart(self):
        # 券商拒单警告框(只有[确认])走 dismiss_error_dialog, 不重启
        states = iter([{"id": 7, "bounds": (529, 324, 300, 150)}, None, None])
        self.ax.find_blocking_window = lambda: next(states)
        self.ax.activate = lambda timeout=3.0: None
        self.ax.capture_window = lambda b: "/tmp/x.png"
        self.ax.dismiss_error_dialog = lambda: "[260200][可用资金不足]"
        self.ax._press_ax_safe_button = lambda blk: (_ for _ in ()).throw(
            AssertionError("error dialog handled first"))
        self.ax.quit_app = lambda: (_ for _ in ()).throw(AssertionError("no restart"))
        saved = A.wait_until
        A.wait_until = lambda cond, **k: cond()
        try:
            ok, _why = self.ax.clear_blocking_window("pw")
        finally:
            A.wait_until = saved
        self.assertTrue(ok)
        self.assertEqual(A.pop_blocker_events()[0]["action"], "点[确认]")

    def test_clear_reports_still_blocked(self):
        self.ax.find_blocking_window = lambda: {"id": 7, "bounds": (1, 2, 3, 4)}
        self.ax.capture_window = lambda b: None
        self.ax.activate = lambda timeout=3.0: None
        self.ax._press_ax_safe_button = lambda blk: (None, "")
        self.ax.quit_app = lambda: True
        self.ax.launch_app = lambda: True
        self.ax.login = lambda pw: (True, "ok")
        ok, why = self.ax.clear_blocking_window("pw")
        self.assertFalse(ok)
        self.assertIn("仍在", why)

    def test_clear_noop_without_blocker(self):
        self.ax.find_blocking_window = lambda: None
        self.ax.quit_app = lambda: (_ for _ in ()).throw(AssertionError("no quit"))
        self.assertEqual(self.ax.clear_blocking_window("pw"), (True, ""))

    def test_ensure_ready_skips_fast_path_when_blocked(self):
        tr = Trader(Config(dry_run=True, state_file="/tmp/at_ff_state.json",
                           log_file="/tmp/at_ff.log"))
        tr.ax.wake_screen = lambda: None
        tr.ax.ensure_frontmost = lambda: None
        tr.ax.find_blocking_window = lambda: {"id": 7, "bounds": (1, 2, 3, 4)}
        tr.ax.login_sheet = lambda: None
        tr.ax.nav_buttons = lambda: [(1, 100.0, object())]
        tr.ax.read_funds = lambda: {"可用金额": 100.0}
        full = []
        tr.ax.ready_to_trade = lambda pw: full.append(1) or (True, "full")
        ok, why = tr.ensure_ready()
        self.assertEqual((ok, why, full), (True, "full", [1]))

    def test_no_confirm_dialog_with_blocker_clears_ready_cache(self):
        tr = Trader(Config(dry_run=False, auto_confirm=True, trade_password="x",
                           state_file="/tmp/at_ff_state.json",
                           log_file="/tmp/at_ff.log"))
        panel = FakePanel()
        panel.code = FakeElement("600303")
        panel.submit_title = "确定买入"
        tr.switch = lambda _side: panel
        st = A.PanelState(code="600303", price="2.70", amount="100",
                          stock_name="演练股", limit_up="2.86",
                          limit_down="2.34", available="可买:300股")
        tr.ax.read_state = lambda _panel: st
        tr.ax.type_text = lambda el, text: None
        tr.ax.read = lambda el: el._value
        tr.ax.dismiss_error_dialog = lambda: None
        tr.ax.press = lambda el: None
        tr.ax.find_confirm_dialog = lambda: None
        tr.ax.find_blocking_window = lambda: {"id": 7, "bounds": (530, 316, 453, 292)}
        tr.cfg.post_submit_wait = 0.01
        tr._ready_at = 1e18
        real_wait = A.wait_until
        import auto_trade.trader as T
        T.wait_until = lambda *a, **k: None
        try:
            r = tr._place_once("buy", "600303", 2.70, 100, None, False)
        finally:
            T.wait_until = real_wait
        self.assertEqual(r.status, Status.ERROR)
        self.assertTrue(r.maybe_sent)
        self.assertIn("遮挡", r.msg)
        self.assertEqual(tr._ready_at, 0.0)


class TestQuitApp(unittest.TestCase):
    def test_sigkill_when_force_terminate_fails(self):
        class App:
            def terminate(self): pass
            def forceTerminate(self): pass
            def processIdentifier(self): return 4242

        alive = {"v": True}
        killed = []
        saved = (A.running_apps, A.app_running, A.wait_until, A.os.kill,
                 A.subprocess.run)
        A.running_apps = lambda: [App()] if alive["v"] else []
        A.app_running = lambda: alive["v"]
        A.wait_until = lambda cond, **k: cond()
        A.subprocess.run = lambda *a, **k: None

        def kill(pid, sig):
            killed.append((pid, sig))
            alive["v"] = False
        A.os.kill = kill
        try:
            self.assertTrue(A.ThsAX().quit_app())
        finally:
            (A.running_apps, A.app_running, A.wait_until, A.os.kill,
             A.subprocess.run) = saved
        self.assertEqual(killed, [(4242, A.signal.SIGKILL)])


if __name__ == "__main__":
    unittest.main()
