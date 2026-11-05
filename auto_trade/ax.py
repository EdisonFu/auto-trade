"""同花顺 Mac 版 Accessibility 驱动层。

实测结论(2026-10-30,同花顺 5.3.5 / macOS 26.6):
  1. 主窗口失焦后 AX 树塌缩为 0 节点 -> 每次操作前必须 activate + 重试取窗口;
  2. **AXValue 直接写入无效**: 控件显示会变、读回"一致",但同花顺业务层
     完全不响应(股票名/涨跌停/可买 全是 '- -')。这是假阳性,据此提交
     会下出一张内部状态为空的单,比直接失败更危险;
  3. 必须用 CGEvent 发真实键盘事件,应用层才会联动;
  4. 下单区三个输入框靠**标签 y 坐标**配对,不能依赖子节点索引;
  5. 填入代码后同花顺会**自动把价格改写成现价**,务必在提交前重读价格框。
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

import Quartz
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from Cocoa import NSRunningApplication

BUNDLE_ID = "cn.com.10jqka.macstockPro"
APP_PATH = "/Applications/同花顺.app"


def running_apps():
    """同花顺的 NSRunningApplication 列表(实时)。

    2026-11-05 的教训: 不要用 NSWorkspace.sharedWorkspace().runningApplications()。
    在不跑 runloop 的脚本进程里它是一份**永不刷新的快照** —— 实测同花顺退出
    7 秒后它仍报"在运行", 启动 10 秒后仍报"没运行"。ths_login 的冷启动因此
    空等 25 秒后报"启动超时", 而同花顺其实早已起来。
    NSRunningApplication.runningApplicationsWithBundleIdentifier_ 每次都现查。
    """
    return list(
        NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
        or []
    )


def app_running() -> bool:
    return bool(running_apps())

# CGEvent 虚拟键码
_KEYCODE = {
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
    "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
    ".": 47, "\b": 51, "\r": 36, "esc": 53,
}


class AXError(Exception):
    pass


def wait_until(cond, timeout: float = 3.0, interval: float = 0.04,
               settle: float = 0.0):
    """轮询直到 cond() 返回真值,返回该值;超时返回 None。

    替代固定 sleep: 条件达成就立刻继续(通常几十毫秒),
    机器卡顿时自动多等,比盲等既快又稳。
    """
    deadline = time.time() + timeout
    while True:
        try:
            val = cond()
        except Exception:
            val = None
        if val:
            if settle:
                time.sleep(settle)
            return val
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def redact(text: str) -> str:
    """脱敏:股东账号等账户标识不应进入日志或报告。"""
    import re
    text = re.sub(r"(股东账号[:：]\s*)([A-Za-z0-9]+)",
                  lambda m: m.group(1) + "***", text)
    text = re.sub(r"(资金账号|客户号|账户)([:：]\s*)([A-Za-z0-9]+)",
                  lambda m: m.group(1) + m.group(2) + "***", text)
    return text


def attr(element, name):
    err, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def geom(element) -> tuple[float, float, float, float]:
    x = y = w = h = 0.0
    pos = attr(element, "AXPosition")
    if pos is not None:
        ok, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
        if ok:
            x, y = pt.x, pt.y
    size = attr(element, "AXSize")
    if size is not None:
        ok, sz = AXValueGetValue(size, kAXValueCGSizeType, None)
        if ok:
            w, h = sz.width, sz.height
    return x, y, w, h


@dataclass
class Panel:
    """下单面板控件引用。"""
    code: object
    price: object
    amount: object
    submit: object
    reset: object | None
    submit_title: str
    direction: str
    order_type: str


@dataclass
class PanelState:
    """下单面板的业务状态 —— 判断输入是否真被应用层接收的唯一依据。"""
    code: str = ""
    price: str = ""
    amount: str = ""
    stock_name: str = ""
    limit_up: str = ""
    limit_down: str = ""
    available: str = ""      # 可买/可卖 文本
    ref_amount: str = ""     # 参考金额
    raw: dict = field(default_factory=dict)

    @property
    def recognized(self) -> bool:
        """同花顺是否真正识别了这只票。"""
        return bool(self.stock_name) and self.limit_up not in ("", "- -")

    @property
    def available_qty(self) -> int | None:
        """从 '可买:200股' / '可用:1600股' 解析出数量;解析不出返回 None。"""
        import re
        m = re.search(r"(\d+)\s*股", self.available)
        return int(m.group(1)) if m else None

    def summary(self) -> str:
        return (
            f"代码={self.code!r} 名称={self.stock_name!r} 价格={self.price!r} "
            f"数量={self.amount!r} 涨停={self.limit_up} 跌停={self.limit_down} "
            f"{self.available} 参考金额={self.ref_amount}"
        )


class ThsAX:
    def __init__(self, settle: float = 0.3, key_delay: float = 0.08):
        self.settle = settle
        self.key_delay = key_delay
        self._src = Quartz.CGEventSourceCreate(
            Quartz.kCGEventSourceStateHIDSystemState
        )

    # ------------------------------------------------------------------
    # 进程 / 窗口
    # ------------------------------------------------------------------
    def quit_app(self, timeout: float = 15.0) -> bool:
        """退出同花顺。主要给测试/演练造冷启动态用。

        osascript quit 会被挂起的确认框挡住并报 -128(用户已取消), 所以逐级
        加力: quit → terminate → kill -9。注意进程名是"同花顺"而非 bundle id,
        pkill -f 10jqka 匹配不到。
        """
        subprocess.run(
            ["osascript", "-e", f'tell application id "{BUNDLE_ID}" to quit'],
            capture_output=True)
        if wait_until(lambda: not app_running() or None, timeout=5.0,
                      interval=0.3):
            return True
        for app in running_apps():
            app.terminate()
        if wait_until(lambda: not app_running() or None, timeout=5.0,
                      interval=0.3):
            return True
        for app in running_apps():
            app.forceTerminate()
        return bool(wait_until(lambda: not app_running() or None,
                               timeout=timeout, interval=0.3))

    def launch_app(self, timeout: float = 40.0) -> bool:
        """同花顺没在跑就拉起来, 等到 AX 主窗口真的有内容。

        冷启动(App 完全退出)与"窗口被关掉"是两回事, 后者进程还活着, 由
        _reopen_main_window 处理。这里只负责"进程都没有"的情况。

        实测要点(2026-11-05, 同花顺 5.3.5):
          - open -b 之后进程立刻出现, 但 AXWindows 仍为 0, 必须 activate
            一次窗口才会真正上屏;
          - 冷启动到登录框可操作约需 10~20s, 故默认等 40s。
        返回是否已就绪(进程在且主窗口 AX 树非空)。
        """
        # 锁屏下窗口不上屏, 下面的就绪判断必然超时; 所有入口都经过这里, 先点亮
        self.wake_screen()
        if not app_running():
            subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True)
            if not wait_until(app_running, timeout=timeout, interval=0.3):
                return False

        def tree_ready():
            """必须是**真窗口**: AXWindows 非空且有子节点, 且已上屏。

            不能拿 AXMainWindow 兜底 —— 刚 open 出来的进程 AXWindows 为 0
            但 AXMainWindow 已能读出 83 个子节点, 据此判定"就绪"会跳过
            activate, 窗口始终不上屏, 后续一切点击都落空。
            """
            if self._onscreen_windows() <= 0:
                return False
            try:
                app = AXUIElementCreateApplication(self._pid())
            except AXError:
                return False
            return self._main_of(app) is not None

        if tree_ready():
            return True
        # 刚 open 出来的进程 AXWindows 为 0, activate 一下窗口才上屏
        self.activate(timeout=6.0)
        return bool(wait_until(tree_ready, timeout=timeout, interval=0.3,
                               settle=0.5))

    def activate(self, timeout: float = 3.0) -> None:
        """激活同花顺并等它真正到前台(轮询,不盲等)。"""
        subprocess.run(
            ["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'],
            capture_output=True,
        )

        def is_front():
            return any(app.isActive() for app in running_apps())

        wait_until(is_front, timeout=timeout, interval=0.03, settle=0.08)

    def _pid(self) -> int:
        apps = running_apps()
        if not apps:
            raise AXError("同花顺未运行")
        return apps[0].processIdentifier()

    @staticmethod
    def _onscreen_windows() -> int:
        """同花顺在屏窗口数。为 0 说明窗口被关掉了(Mac 关窗不退进程)。"""
        try:
            import Quartz
        except ImportError:
            return -1
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        return sum(1 for w in wins
                   if "同花顺" in str(w.get("kCGWindowOwnerName", "")))

    def _reopen_main_window(self) -> None:
        """窗口被关时重新唤出主窗口。

        Mac 上关闭窗口不会退出应用,此时 AX 树恒为空。再怎么 activate
        也没用,必须让应用重新开窗 —— 等价于点 Dock 图标。

        2026-11-05 的教训: 原实现只做 activate + "AXRaise of window 1",
        但窗口全关时根本没有 window 1, 这条必然失败; activate 对已运行却
        无窗口的 App 也不会开新窗。于是实盘 3 笔全废而唤回毫无作用。
        改为三级递进, 每级都验证是否已开窗:
          1. open -b  —— 真正等价于点 Dock 图标, 会让 App 重新开窗
          2. 菜单「窗口 → ...」第一项
          3. AXRaise(窗口只是被最小化/遮挡时有效)
        """
        def opened() -> bool:
            return self._onscreen_windows() > 0

        # 1) open -b: 对已运行且无窗口的 App, 这是唯一能让它重新开窗的办法
        subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True)
        if wait_until(opened, timeout=4.0, interval=0.1, settle=0.3):
            return

        # 2) 菜单栏「窗口」第一项 —— 多数 Mac App 在此提供"主窗口"
        subprocess.run(
            ["osascript", "-e",
             f'tell application id "{BUNDLE_ID}" to activate',
             "-e", 'tell application "System Events" to tell process "同花顺" '
                   'to click menu item 1 of menu 1 of menu bar item "窗口" '
                   'of menu bar 1'],
            capture_output=True,
        )
        if wait_until(opened, timeout=4.0, interval=0.1, settle=0.3):
            return

        # 3) 窗口只是被最小化或遮挡时, AXRaise 足够
        subprocess.run(
            ["osascript", "-e",
             f'tell application id "{BUNDLE_ID}" to activate',
             "-e", 'tell application "System Events" to tell process "同花顺" '
                   'to perform action "AXRaise" of window 1'],
            capture_output=True,
        )
        wait_until(opened, timeout=4.0, interval=0.1, settle=0.3)

    @staticmethod
    def _main_of(app):
        """取真正的主窗口, 不取 AXWindows[0]。

        2026-11-05 实测: 交易密码框获得焦点时, macOS「密码」自动填充会在
        同花顺进程内弹出一个小浮窗(只含一格「密码…」), 它排在 AXWindows[0]。
        原实现取 [0] 就把浮窗当成主窗口 —— 登录框"凭空消失", 后续点击落在
        「密码…」上, 弹出「"密码"已锁定」的 Touch ID 解锁框, 自动登录全线失败。
        只认 AXStandardWindow, 并取子节点最多的那个。
        """
        wins = [w for w in (attr(app, "AXWindows") or [])
                if str(attr(w, "AXSubrole") or "") == "AXStandardWindow"
                and (attr(w, "AXChildren") or [])]
        if not wins:
            return None
        return max(wins, key=lambda w: len(attr(w, "AXChildren") or []))

    def window(self, timeout: float = 5.0):
        """取主窗口。失焦时 AX 树会塌缩,故轮询而非盲等。"""
        if not app_running():
            # 冷启动: 进程都没有, 先拉起来(launchd 重启、手动退出都会到这)
            self.launch_app()
        app = AXUIElementCreateApplication(self._pid())

        def ready():
            return self._main_of(app)

        win = ready()
        if win is not None:
            return win

        self.activate()
        win = wait_until(ready, timeout=timeout)
        if win is not None:
            return win

        # 还是空 —— 一律尝试唤回主窗口, 不再只在"在屏窗口数为 0"时才试。
        # 2026-11-05 的教训: 窗口计数与 AX 树是否可用并不等价(窗口可能已
        # 在屏但 AX 仍塌缩, 或计数本身读失败返回 -1), 用它当唤回的前置条件
        # 会让真正需要唤回的情况被跳过。唤回本身是幂等的, 多做一次无害。
        self._reopen_main_window()
        win = wait_until(ready, timeout=timeout)
        if win is not None:
            return win
        raise AXError(
            f"同花顺 AX 树为空且自动唤回失败(在屏窗口 {self._onscreen_windows()} 个)。"
            "请点 Dock 里的同花顺图标, 或用菜单「窗口 → 主窗口」打开主界面。"
        )

    # ------------------------------------------------------------------
    # 面板定位
    # ------------------------------------------------------------------
    def _try_panel(self):
        try:
            return self.find_panel()
        except AXError:
            return None

    def _panel_direction(self) -> str | None:
        p = self._try_panel()
        return p.direction if p else None

    def find_panel(self) -> Panel:
        win = self.window()
        kids = attr(win, "AXChildren") or []

        labels: dict[str, float] = {}
        fields: list[tuple[float, object]] = []
        buttons: dict[str, object] = {}
        texts: list[tuple[float, str]] = []

        for el in kids:
            role = str(attr(el, "AXRole") or "")
            _x, y, _w, _h = geom(el)
            if role == "AXStaticText":
                val = str(attr(el, "AXValue") or "").strip()
                if val in ("代码", "价格", "数量"):
                    labels[val] = y
                elif val:
                    texts.append((y, val))
            elif role == "AXTextField":
                fields.append((y, el))
            elif role == "AXButton":
                title = str(attr(el, "AXTitle") or "").strip()
                if title:
                    buttons.setdefault(title, el)

        if not labels or not fields:
            raise AXError("未找到下单区,请确认已切换到买入/卖出界面")

        def nearest(label: str):
            if label not in labels:
                raise AXError(f"缺少标签: {label}")
            ly = labels[label]
            y, el = min(fields, key=lambda f: abs(f[0] - ly))
            if abs(y - ly) > 12:
                raise AXError(f"标签[{label}]附近无输入框(最近 {abs(y-ly):.0f}px)")
            return el

        submit = submit_title = None
        for title in ("确定买入", "确定卖出"):
            if title in buttons:
                submit, submit_title = buttons[title], title
                break
        if submit is None:
            raise AXError(f"未找到提交按钮;现有按钮: {list(buttons)[:12]}")

        return Panel(
            code=nearest("代码"),
            price=nearest("价格"),
            amount=nearest("数量"),
            submit=submit,
            reset=buttons.get("重填"),
            submit_title=submit_title,
            direction="买入" if "买入" in submit_title else "卖出",
            order_type=next((t for _y, t in texts if "委托" in t), "?"),
        )

    # ------------------------------------------------------------------
    # 读状态
    # ------------------------------------------------------------------
    def read(self, element) -> str:
        return str(attr(element, "AXValue") or "")

    def read_state(self, panel: Panel) -> PanelState:
        """读下单区全部业务字段。联动字段是输入被接收的唯一证据。"""
        win = self.window()
        st = PanelState(
            code=self.read(panel.code),
            price=self.read(panel.price),
            amount=self.read(panel.amount),
        )
        items: list[tuple[float, float, str, str]] = []
        for el in attr(win, "AXChildren") or []:
            role = str(attr(el, "AXRole") or "")
            if role not in ("AXStaticText", "AXButton"):
                continue
            x, y, _w, _h = geom(el)
            if not (100 <= y <= 340 and 260 <= x <= 660):
                continue
            text = str(attr(el, "AXValue") or attr(el, "AXTitle") or "").strip()
            if text:
                items.append((y, x, role, text))
        items.sort()
        st.raw = {f"{y:.0f}:{x:.0f}": t for y, x, _r, t in items}

        for i, (y, x, _role, text) in enumerate(items):
            # 买入面板显示"可买:N股",卖出面板显示"可用:N股"
            if any(k in text for k in ("可买", "可卖", "可用")):
                st.available = text
            elif text == "涨停:":
                nxt = [t for yy, xx, _r, t in items if abs(yy - y) < 4 and xx > x]
                if nxt:
                    st.limit_up = nxt[0]
            elif text == "跌停:":
                nxt = [t for yy, xx, _r, t in items if abs(yy - y) < 4 and xx > x]
                if nxt:
                    st.limit_down = nxt[0]
            elif "参考金额" in text:
                nxt = [t for yy, xx, _r, t in items if abs(yy - y) < 4 and xx > x]
                if nxt:
                    st.ref_amount = nxt[0]
            # 股票名: 代码框下方 20px 左右的纯文本
            elif 195 <= y <= 215 and not any(c.isdigit() for c in text):
                st.stock_name = text
        return st

    # ------------------------------------------------------------------
    # 输入 (CGEvent 真实键盘)
    # ------------------------------------------------------------------
    def _key(self, keycode: int) -> None:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(self._src, keycode, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        if self.key_delay:
            time.sleep(self.key_delay)

    def focus(self, element) -> None:
        err = AXUIElementSetAttributeValue(element, "AXFocused", True)
        if err != 0:
            raise AXError(f"聚焦失败 (AXError={err})")
        wait_until(lambda: attr(element, "AXFocused"),
                   timeout=1.0, interval=0.02, settle=0.03)

    def type_text(self, element, text: str, clear_first: bool = True,
                  verify: bool = True) -> None:
        """聚焦后用真实键盘事件输入。不要用 AXValue —— 应用层收不到。

        清空与落值都用轮询确认,不盲等:
          * 退格按到框空为止,而不是固定敲 12 次;
          * 输入后轮询读回,值对了立刻返回。
        """
        self.focus(element)

        if clear_first and self.read(element):
            for _ in range(16):
                self._key(_KEYCODE["\b"])
                if not self.read(element):
                    break

        for ch in text:
            if ch not in _KEYCODE:
                raise AXError(f"不支持的字符: {ch!r}")
            self._key(_KEYCODE[ch])

        if verify:
            # 读回只能证明控件显示对了,不能证明应用层收到 —— 后者由
            # read_state() 的联动字段判断。这里只用来省掉固定 settle。
            if wait_until(lambda: self.read(element) == text,
                          timeout=max(self.settle * 4, 1.0),
                          interval=0.02) is None:
                # 键盘路径失败(多见于退格没清干净导致新值追加不进去):
                # 回退到 AXValue 直写。实测同花顺能收到并触发联动,
                # 仍以 read_state() 的联动字段作为最终判据。
                AXUIElementSetAttributeValue(element, "AXValue", text)
                if wait_until(lambda: self.read(element) == text,
                              timeout=1.0, interval=0.02) is None:
                    time.sleep(self.settle)
        else:
            time.sleep(self.settle)

    def press(self, element) -> None:
        err = AXUIElementPerformAction(element, "AXPress")
        if err != 0:
            raise AXError(f"AXPress 失败 (AXError={err})")

    def reset(self, panel: Panel) -> bool:
        """点[重填]清空表单。"""
        if panel.reset is None:
            return False
        self.press(panel.reset)
        wait_until(lambda: not (self.read(panel.code)
                                or self.read(panel.price)
                                or self.read(panel.amount)),
                   timeout=1.5, interval=0.03)
        return True

    # ------------------------------------------------------------------
    # 二次确认框
    # ------------------------------------------------------------------
    # 券商侧拒单时弹的是"警告"框: 只有[确认]一个按钮, 正文带错误码。
    # 它必须被识别并关掉 —— 否则一直挂在屏幕上, 挡住后续所有操作。
    ERROR_MARKS = ("可用资金不足", "可用股份不足", "超出涨跌幅", "警告",
                   "不足", "失败", "错误", "拒绝", "非交易时间", "停牌")

    def find_error_dialog(self) -> dict | None:
        """查找券商拒单的警告框。返回 {'text','confirm'} 或 None。

        不能复用 find_confirm_dialog —— 后者要求正文含"委托/证券代码"
        才算数, 而警告框正文是错误码(如 [260200][可用资金不足]), 会被判空。
        这里独立扫描: 只有[确认]没有[取消], 且正文命中错误关键词。
        """
        win = self.window()
        texts, confirm, cancel = [], None, None

        def walk(el, depth=0):
            nonlocal confirm, cancel
            if depth > 18:
                return
            role = str(attr(el, "AXRole") or "")
            t = str(attr(el, "AXValue") or attr(el, "AXTitle") or "").strip()
            if role == "AXButton" and t in ("确认", "确定"):
                confirm = confirm or el
            elif role == "AXButton" and t == "取消":
                cancel = cancel or el
            elif role == "AXStaticText" and t:
                texts.append(t)
            for c in attr(el, "AXChildren") or []:
                walk(c, depth + 1)

        walk(win)
        if confirm is None or cancel is not None:
            return None            # 有[取消]的是正常下单确认框
        blob = " ".join(texts)
        hit = [t for t in texts if any(m in t for m in self.ERROR_MARKS)]
        if not hit:
            return None
        return {"text": " ".join(hit)[:200], "confirm": confirm}

    def dismiss_error_dialog(self) -> str | None:
        """关掉警告框并返回其正文, 没有则 None。"""
        dlg = self.find_error_dialog()
        if dlg is None:
            return None
        text = str(dlg.get("text", ""))
        btn = dlg.get("confirm")
        if btn is not None:
            try:
                self.press(btn)
                time.sleep(0.4)
            except AXError:
                pass
        return text

    def _click_at(self, x: float, y: float) -> None:
        for ev in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateMouseEvent(
                                   None, ev, (x, y), Quartz.kCGMouseButtonLeft))
            time.sleep(0.08)

    def find_password_unlock(self) -> tuple[float, float, float, float] | None:
        """「"密码"已锁定」解锁框的位置, 没有返回 None。

        它由 macOS 自动填充弹在同花顺**进程内**, 但不进 AX 树(AXWindows 看不到,
        按钮也遍历不到), 只能靠窗口服务器的窗口尺寸(约 440x515)识别。
        """
        try:
            pid = self._pid()
        except AXError:
            return None
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        for w in wins:
            if w.get("kCGWindowOwnerPID") != pid:
                continue
            b = w.get("kCGWindowBounds") or {}
            if 400 <= b.get("Width", 0) <= 480 and 480 <= b.get("Height", 0) <= 550:
                return b["X"], b["Y"], b["Width"], b["Height"]
        return None

    def dismiss_password_unlock(self) -> bool:
        """误弹「"密码"已锁定」时点[取消]。无人值守时没人按 Touch ID, 不取消
        它会一直挡在登录框上。返回是否取消过。"""
        box = self.find_password_unlock()
        if box is None:
            return False
        self.activate(timeout=3.0)
        x, y, w, h = box
        # 不在 AX 树里, 只能按坐标点。[取消]在框内右下, 实测偏移 (315, 483)
        self._click_at(x + w * 315 / 440, y + h * 483 / 515)
        wait_until(lambda: self.find_password_unlock() is None,
                   timeout=3.0, interval=0.1, settle=0.3)
        return self.find_password_unlock() is None

    @staticmethod
    def dismiss_system_prompt() -> str | None:
        """取消系统弹出的凭据对话框(钥匙串解锁 / Touch ID / 授权)。

        这类框由 SecurityAgent / CoreAuthUI 弹出, **不属于同花顺的 AX 树**,
        所以 dismiss_dialogs 看不见它, 却会抢走焦点让后续键盘事件全部落空。
        无人值守时它永远等不到人按 Touch ID, 必须主动点[取消]继续。

        正常路径下不该出现 —— 取密码已不走钥匙串。这里是兜底。
        返回被取消的对话框所属进程名, 没有则 None。
        """
        # 先用 pgrep 判断有没有这类进程: 直接跑 osascript 遍历要 6s+
        if not any(subprocess.run(["pgrep", "-x", name],
                                  capture_output=True).returncode == 0
                   for name in ("SecurityAgent", "CoreAuthUI")):
            return None
        script = '''
        tell application "System Events"
          repeat with procName in {"SecurityAgent", "CoreAuthUI"}
            try
              if exists (process procName) then
                tell process procName
                  repeat with w in windows
                    try
                      if exists (button "取消" of w) then
                        click button "取消" of w
                        return procName as text
                      else if exists (button "Cancel" of w) then
                        click button "Cancel" of w
                        return procName as text
                      end if
                    end try
                  end repeat
                end tell
              end if
            end try
          end repeat
        end tell
        return ""
        '''
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True)
        name = (out.stdout or "").strip()
        if name:
            time.sleep(0.5)
            return name
        return None

    def find_confirm_dialog(self) -> dict | None:
        """查找下单二次确认框。

        同花顺点[确定买入/卖出]后不会直接报单,而是弹出一个内嵌确认框
        (不是独立 AXWindow,也不是 AXSheet,而是主窗口内的一组控件)。
        返回 {'text':摘要, 'confirm':按钮, 'cancel':按钮} 或 None。
        """
        win = self.window()
        found: dict = {}
        detail = []

        def walk(el, depth=0):
            if depth > 16:
                return
            role = str(attr(el, "AXRole") or "")
            x, y, _w, _h = geom(el)
            text = str(
                attr(el, "AXValue") or attr(el, "AXTitle")
                or attr(el, "AXDescription") or ""
            ).strip()
            if role == "AXButton" and text in ("确认", "确定"):
                found.setdefault("confirm", el)
            elif role == "AXButton" and text in ("取消",):
                found.setdefault("cancel", el)
            elif role == "AXStaticText" and (
                "委托" in text and ("证券代码" in text or "股东账号" in text)
            ):
                detail.append(text)
            elif role == "AXStaticText" and text in ("买入委托", "卖出委托"):
                detail.append(text)
            for c in attr(el, "AXChildren") or []:
                walk(c, depth + 1)

        walk(win)
        if "confirm" not in found or not detail:
            return None
        found["text"] = " | ".join(detail)
        return found

    def confirm_order(self) -> str:
        """点击确认框的[确认]。返回确认框摘要文本。"""
        dlg = self.find_confirm_dialog()
        if dlg is None:
            raise AXError("未找到二次确认框")
        text = dlg["text"]
        self.press(dlg["confirm"])
        # 确认框消失即视为该步完成,不必盲等满 1.2s
        wait_until(lambda: self.find_confirm_dialog() is None,
                   timeout=3.0, interval=0.05, settle=0.1)
        return text

    def cancel_order_dialog(self) -> str:
        """点击确认框的[取消],放弃本次下单。"""
        dlg = self.find_confirm_dialog()
        if dlg is None:
            raise AXError("未找到二次确认框")
        text = dlg["text"]
        if "cancel" not in dlg:
            raise AXError("确认框无[取消]按钮")
        self.press(dlg["cancel"])
        wait_until(lambda: self.find_confirm_dialog() is None,
                   timeout=2.0, interval=0.05)
        return text

    # ------------------------------------------------------------------
    # 资金
    # ------------------------------------------------------------------
    def read_funds(self) -> dict[str, float]:
        """读左侧资金区。返回 {可用金额, 总资产, 总市值, 资金余额, ...}。

        键为界面原文, 值为 float。读不到的键不会出现 —— 调用方必须自己
        判断 "可用金额" 在不在结果里, 不要默认它存在。

        这是券商的权威口径: 可用金额已扣掉在途委托冻结的钱, 比配置里
        写死的 CAP 准确, 且随成交实时变化, 故须在下单前现读。
        """
        win = self.window()
        items: list[tuple[float, float, str]] = []

        def walk(el, depth=0):
            if depth > 14:
                return
            if str(attr(el, "AXRole") or "") == "AXStaticText":
                text = str(attr(el, "AXValue") or attr(el, "AXTitle") or "").strip()
                if text:
                    x, y, _w, _h = geom(el)
                    # 左侧资金区: x<260 排除中部下单面板, y 圈定资金块
                    if x < 260 and 100 <= y <= 300:
                        items.append((y, x, text))
            for c in attr(el, "AXChildren") or []:
                walk(c, depth + 1)

        walk(win)
        items.sort()

        # 版面是"标签 | 数值"两列: 同一行(y 差 <6)取标签右侧的数字
        out: dict[str, float] = {}
        for y, x, text in items:
            if not text.endswith(("额", "产", "值", "亏")):
                continue
            for yy, xx, val in items:
                if abs(yy - y) < 6 and xx > x:
                    try:
                        out[text] = float(val.replace(",", ""))
                    except ValueError:
                        pass
                    break
        return out

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def login_sheet(self):
        """交易登录框(AXSheet)。不在登录界面返回 None。

        按 **role + placeholder** 定位, 不写死子节点索引。实测该 sheet 恰好
        排在主窗口子节点末位(/83), 但那只是"最后添加的节点排最后"的巧合 ——
        主窗口子节点数会随行情页面变化(实测 83/84 都出现过), 写死必然漂移。
        """
        try:
            win = self.window()
        except AXError:
            return None
        for el in attr(win, "AXChildren") or []:
            if str(attr(el, "AXRole") or "") != "AXSheet":
                continue
            sub = attr(el, "AXChildren") or []
            has_pw = any(
                str(attr(c, "AXPlaceholderValue") or "") == "请输入交易密码"
                for c in sub)
            has_btn = any(
                str(attr(c, "AXTitle") or "").strip() == "登录" for c in sub)
            if has_pw and has_btn:
                return el
        return None

    @staticmethod
    def _sheet_field(sheet, placeholder: str):
        for c in attr(sheet, "AXChildren") or []:
            if str(attr(c, "AXPlaceholderValue") or "") == placeholder:
                return c
        return None

    @staticmethod
    def _sheet_button(sheet, title: str):
        for c in attr(sheet, "AXChildren") or []:
            if str(attr(c, "AXTitle") or "").strip() == title:
                return c
        return None

    def read_captcha(self, sheet) -> str:
        """读验证码。

        同花顺把 5 位验证码以 AXStaticText **明文**暴露, 无需 OCR。它们紧挨
        验证码输入框右侧, 故取"在输入框右边、y 与之同排"的 StaticText, 按 x
        排序拼接 —— 不按索引取, sheet 里还有别的 StaticText(标签文字)。
        """
        box = self._sheet_field(sheet, "请输入验证码")
        if box is None:
            return ""
        bx, by, bw, _bh = geom(box)
        digits = []
        for c in attr(sheet, "AXChildren") or []:
            if str(attr(c, "AXRole") or "") != "AXStaticText":
                continue
            x, y, _w, _h = geom(c)
            if x < bx + bw or abs(y - by) > 8:
                continue
            v = str(attr(c, "AXValue") or "").strip()
            if v.isdigit():
                digits.append((x, v))
        return "".join(v for _x, v in sorted(digits))

    def logged_in(self, probe_code: str = "600303",
                  timeout: float = 4.0) -> bool:
        """是否已登录交易。

        判据分两级, 先快后慢:
          1. 资金区读到「可用金额」—— 未登录时资金区恒为空, 这一条又快
             (约 0.2s) 又不触碰界面;
          2. 资金区读不到时, 再用下单面板的业务联动兜底(填代码看股票名/
             涨跌停是否联动)。未登录则恒为 '- -'。

        不能只用第 2 条: 它会调 reset_to_order_page 点击界面, 刚登录成功
        那几秒界面还在切换, 联动探测会超时而误判"未登录" —— 实测冷启动
        登录其实已成功, 却被判失败并报"登录框消失但未登录"。
        """
        if self.login_sheet() is not None:
            return False
        try:
            if "可用金额" in self.read_funds():
                return True
        except AXError:
            pass
        try:
            p = self.reset_to_order_page("buy")
            self.type_text(p.code, probe_code)
            return bool(wait_until(
                lambda: self.read_state(p).recognized or None, timeout=timeout))
        except AXError:
            return False

    def login(self, password: str, *, retries: int = 3) -> tuple[bool, str]:
        """填密码+验证码并登录。密码由调用方传入, 本模块不碰钥匙串。

        返回 (是否已登录, 说明)。已登录则直接返回 True。
        验证码偶尔读不全(渲染未完成), 故重试。
        """
        if not self.launch_app():
            return False, "同花顺拉起失败"
        if not password:
            return False, "未提供交易密码"
        self.activate(timeout=6.0)

        sheet = self.login_sheet()
        if sheet is None:
            if self.logged_in():
                return True, "已登录"
            # 冷启动时登录框是**自动弹出**的, 只是渲染比窗口慢几秒。先等它
            # 自己出来, 别急着点[立即登录] —— 那会在框已弹出时再点一次,
            # 把框关掉或叠出第二个, 实测因此失败("登录框消失但未登录")。
            sheet = wait_until(self.login_sheet, timeout=12.0, interval=0.3,
                               settle=0.3)
        if sheet is None:
            # 等不来才主动点入口
            btn = self._find_button("立即登录")
            if btn is None:
                return False, "既未登录, 也找不到登录入口"
            self.press(btn)
            sheet = wait_until(self.login_sheet, timeout=10.0, interval=0.3,
                               settle=0.3)
            if sheet is None:
                return False, "点[立即登录]后登录框未出现"

        last_err = ""
        for n in range(1, retries + 1):
            # 每轮都先激活: 窗口离屏/失焦时 press 会落空而填字仍"看着成功",
            # 于是出现"密码填进去了但登录没点动"的假象(实测 onscreen=0 时
            # 必然如此)。另外顺手取消误弹的系统凭据框, 它会抢焦点。
            self.dismiss_system_prompt()
            self.activate(timeout=6.0)
            self.dismiss_password_unlock()
            sheet = self.login_sheet()
            if sheet is None:
                return (True, "登录成功") if self.logged_in() else \
                       (False, "登录框消失但未登录")
            cap = self.read_captcha(sheet)
            if len(cap) < 4:
                time.sleep(1.5)
                continue
            pw_box = self._sheet_field(sheet, "请输入交易密码")
            cap_box = self._sheet_field(sheet, "请输入验证码")
            btn = self._sheet_button(sheet, "登录")
            if not (pw_box and cap_box and btn):
                return False, "登录框控件不全"
            self.type_text(pw_box, password)
            time.sleep(0.2)
            self.type_text(cap_box, cap)
            time.sleep(0.2)
            self.press(btn)
            # 登录完成的信号是**资金区可读**, 不是登录框消失: 实测按下后
            # 0.1s 框就没了, 但资金要再过 1~数秒才加载(冷启动更慢)。若此时
            # 就判定, 资金为空会退到联动探测, 界面切换中探测超时 → 误判失败。
            def done():
                if self.login_sheet() is not None:
                    return None
                return "可用金额" in self.read_funds() or None
            if wait_until(done, timeout=20.0, interval=0.5):
                return True, f"登录成功(第{n}次)"
            if self.login_sheet() is None and self.logged_in():
                return True, f"登录成功(第{n}次, 联动确认)"
            err = self.dismiss_error_dialog()
            if err:
                last_err = err
            time.sleep(1.5)
        return False, f"登录 {retries} 次均失败" + (
            f"(同花顺提示: {last_err[:60]})" if last_err else "")

    def _find_button(self, title: str):
        try:
            win = self.window()
        except AXError:
            return None
        for el in attr(win, "AXChildren") or []:
            if str(attr(el, "AXTitle") or "").strip() == title:
                return el
        return None

    @staticmethod
    def screen_locked() -> bool:
        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked"))

    @classmethod
    def wake_screen(cls, timeout: float = 8.0) -> bool:
        """点亮屏幕并等锁屏解除。返回是否已解锁。

        熄屏(手动或 pmset displaysleepnow)会进入锁屏, 锁屏下 AX 树塌缩,
        所有操作必败 —— 2026-11-06 凌晨演练即因此 3 笔全失败。在未设锁屏
        密码的前提下, caffeinate -u 会点亮并自动解锁。这里放在自检/下单
        的最前面, 不依赖 09:18 的点亮任务一定跑过。
        """
        if not cls.screen_locked():
            return True
        subprocess.run(["caffeinate", "-u", "-t", "3"], capture_output=True)
        return bool(wait_until(lambda: not cls.screen_locked() or None,
                               timeout=timeout, interval=0.3, settle=1.0))

    def ready_to_trade(self, password: str | None = None) -> tuple[bool, str]:
        """下单前就绪自检: 逐项验证真正要用到的能力, 而非只看"能取到窗口"。

        2026-11-05 的教训: screen.sh 在 09:18 只检查 window() 能否返回,
        报告"AX 树可用, 可以交易"; 7 分钟后 09:25 下单时读资金为空、导航栏
        找不到、委托页签打不开, 3 笔全废。取窗口是弱条件, 下单需要的是读得
        到资金区、点得到导航、翻得开委托表 —— 必须逐项真测。

        返回 (是否就绪, 说明)。失败时说明写清卡在哪一步, 便于直接动手。

        password 不为空时, 发现未登录会自动登录 —— 冷启动(App 被退出)和
        会话失效都会落到未登录态, 此时窗口/导航全都正常, 只有下单必废,
        所以必须在自检里真查登录态, 而不是等到下单才发现。
        """
        from ApplicationServices import AXIsProcessTrusted
        if not AXIsProcessTrusted():
            return False, "辅助功能未授权"
        if not self.wake_screen():
            return False, "屏幕处于锁屏且无法自动解锁(是否重新设了锁屏密码?)"

        if not app_running():
            if not self.launch_app():
                return False, "同花顺未运行且拉起失败"

        onscreen = self._onscreen_windows()
        try:
            win = self.window(timeout=10.0)
        except AXError as e:
            return False, f"取窗口失败(在屏窗口 {onscreen} 个): {e}"
        if not (attr(win, "AXChildren") or []):
            return False, "窗口 AX 树为空"

        # 登录态要在读资金之前查 —— 未登录时资金区是空的, 若不先查登录,
        # 报出来的会是"读不到可用金额"这种误导性原因。
        if self.login_sheet() is not None or not self.logged_in():
            if not password:
                return False, "交易未登录(未提供密码, 无法自动登录)"
            ok, why = self.login(password)
            if not ok:
                return False, f"交易未登录且自动登录失败: {why}"

        if not self.nav_buttons():
            return False, f"读不到左侧导航栏(在屏窗口 {onscreen} 个)"

        # 资金区是逐行加载的: 刚登录完常常只出来 总资产/总市值/总盈亏 三行,
        # 可用金额还要再等几秒(实测冷启动 drill 在这里误报失败)。轮询等它。
        funds = wait_until(lambda: (f := self.read_funds()) and
                           "可用金额" in f and f or None,
                           timeout=20.0, interval=0.5) or self.read_funds()
        if "可用金额" not in funds:
            return False, f"读不到可用金额(资金区字段: {sorted(funds) or '空'})"

        try:
            self.open_orders_tab()
        except AXError as e:
            return False, f"打不开委托表: {e}"
        return True, f"可用金额 {funds['可用金额']:,.2f}, 导航与委托表均可读"

    def available_cash(self, timeout: float = 10.0) -> float | None:
        """可用金额(可立即用于买入的现金)。读不到返回 None。

        返回 None 时调用方应中止下单, 而不是退回某个默认值 —— 拿不准
        账户有多少钱就不该下单。
        """
        f = wait_until(lambda: (d := self.read_funds()) and
                       "可用金额" in d and d or None,
                       timeout=timeout, interval=0.5)
        return (f or {}).get("可用金额")

    # ------------------------------------------------------------------
    # 委托查询
    # ------------------------------------------------------------------
    def open_orders_tab(self) -> None:
        """点击下方[委托]页签, 并确保「只显示可撤委托」未勾选。"""
        win = self.window()
        for el in attr(win, "AXChildren") or []:
            if str(attr(el, "AXRole") or "") != "AXButton":
                continue
            if str(attr(el, "AXTitle") or "").strip() != "委托":
                continue
            _x, y, _w, _h = geom(el)
            if 360 <= y <= 390:
                self.press(el)
                wait_until(lambda: any(
                    "委托" in str(attr(c, "AXTitle") or "")
                    for c in (attr(self.window(), "AXChildren") or [])
                ), timeout=2.5, interval=0.05, settle=0.2)
                self.show_all_orders()
                return
        raise AXError("未找到[委托]页签")

    def show_all_orders(self) -> bool:
        """取消「只显示可撤委托」, 否则废单/已成交根本不在表里。

        2026-11-04 实测: 该复选框勾选时 read_orders() 返回 0 笔, 而当天
        实际有 5 笔(3 已成 + 2 废单)。它会同时毁掉两件事 ——
          * 下单后核对: 报成功的单被判成 "委托表中无此笔, 可能未成功报单"
          * 废单重挂:   看不到废单, 以为没有, 该补的不补
        返回 True 表示本次动过它(原来是勾选状态)。
        """
        win = self.window()
        box = None

        def walk(el, depth=0):
            nonlocal box
            if box is not None or depth > 18:
                return
            if str(attr(el, "AXRole") or "") == "AXCheckBox":
                x, y, _w, _h = geom(el)
                # 复选框本身无标题, 靠紧邻右侧的「只显示可撤委托」文本定位
                if 395 <= y <= 420 and 760 <= x <= 860:
                    box = el
                    return
            for c in attr(el, "AXChildren") or []:
                walk(c, depth + 1)

        walk(win)
        if box is None:
            return False                      # 版面变了, 交给调用方从行数判断
        if str(attr(box, "AXValue") or "0").strip() not in ("1", "true", "True"):
            return False                      # 本来就没勾
        self.press(box)
        wait_until(lambda: str(attr(box, "AXValue") or "0").strip()
                   not in ("1", "true", "True"),
                   timeout=2.0, interval=0.05, settle=0.2)
        return True

    def read_orders(self) -> list[dict]:
        """读当日委托表。空列表表示没有任何委托记录。"""
        win = self.window()
        cells: list[tuple[float, float, str]] = []

        def walk(el, depth=0):
            if depth > 18:
                return
            text = str(attr(el, "AXValue") or attr(el, "AXTitle") or "").strip()
            x, y, _w, _h = geom(el)
            if text and 430 <= y <= 860:
                cells.append((y, x, text))
            for c in attr(el, "AXChildren") or []:
                walk(c, depth + 1)

        walk(win)
        if not cells:
            return []

        from collections import defaultdict
        rows: dict[int, list[tuple[float, str]]] = defaultdict(list)
        for y, x, t in cells:
            rows[round(y)].append((x, t))

        ys = sorted(rows)
        header = [t for _x, t in sorted(rows[ys[0]])]
        out = []
        for y in ys[1:]:
            vals = [t for _x, t in sorted(rows[y])]
            if len(vals) < 3 or vals == ["按钮"]:
                continue
            out.append(dict(zip(header, vals)))
        return out

    # ------------------------------------------------------------------
    # 页面归位 (连续下单的前提)
    # ------------------------------------------------------------------
    def dismiss_dialogs(self, max_rounds: int = 3) -> list[str]:
        """清掉可能悬在界面上的二次确认框(点[取消],不提交)。

        连续下单前必须确保没有遗留确认框,否则下一单的点击会落到旧框上。
        返回被取消的确认框摘要列表。
        """
        dismissed = []
        for _ in range(max_rounds):
            dlg = self.find_confirm_dialog()
            if dlg is None:
                break
            text = dlg.get("text", "")
            target = dlg.get("cancel") or dlg.get("confirm")
            if target is None:
                break
            # 优先点[取消] —— 绝不因为"清理界面"而提交一张单
            if "cancel" not in dlg:
                raise AXError(
                    "发现确认框但无[取消]按钮,拒绝自动处理(可能会误提交);"
                    f"请人工处理: {redact(text)[:120]}"
                )
            self.press(dlg["cancel"])
            wait_until(lambda: self.find_confirm_dialog() is None,
                       timeout=2.0, interval=0.05)
            dismissed.append(text)
        return dismissed

    def reset_to_order_page(self, side: str = "buy") -> Panel:
        """把界面恢复成「干净的下单页」,供下一单使用。

        连续下单时每单之前都该调用。做三件事:
          1. 取消遗留的二次确认框(不提交);
          2. 切到指定买卖方向;
          3. 点[重填]清空代码/价格/数量 —— 残留值会让下一单
             误用上一单的数量或价格。
        """
        # dismiss_dialogs 要遍历全部 AX 节点(约0.25s, 首次冷启动3.5s),
        # 连续下单时绝大多数情况没有遗留对话框; 故先看是否已在干净的
        # 目标方向下单页, 是则跳过清理与导航, 直接重填。
        try:
            cur = self.find_panel()
            want0 = "买入" if side == "buy" else "卖出"
            if cur.direction == want0 and self.find_confirm_dialog() is None:
                # reset 返回 False 说明[重填]没清干净(上一单残留),
                # 此时必须走完整归位, 否则下一单会填进脏面板导致联动失败。
                if self.reset(cur):
                    return cur
        except AXError:
            pass

        self.dismiss_dialogs()

        # 用户可能停在行情/涨停/基金等任意页面,先确保回到交易页
        try:
            self.find_panel()
        except AXError:
            self.goto_trade_page()

        want = "买入" if side == "buy" else "卖出"
        win = self.window()
        for el in attr(win, "AXChildren") or []:
            if str(attr(el, "AXRole") or "") != "AXButton":
                continue
            if str(attr(el, "AXTitle") or "").strip() != want:
                continue
            _x, y, _w, _h = geom(el)
            if 100 <= y <= 130:          # 方向 tab 行
                self.press(el)
                # 轮询到面板方向真的切过去
                wait_until(lambda: self._panel_direction() == want,
                           timeout=2.5, interval=0.04)
                break

        panel = self.find_panel()
        if panel.direction != want:
            raise AXError(f"切换到[{want}]失败,当前为[{panel.direction}]")

        self.reset(panel)
        residue = {
            "代码": self.read(panel.code),
            "价格": self.read(panel.price),
            "数量": self.read(panel.amount),
        }
        left = {k: v for k, v in residue.items() if v.strip()}
        if left:
            # [重填]没清干净,逐框退格兜底
            for el in (panel.code, panel.price, panel.amount):
                self.focus(el)
                for _ in range(12):
                    self._key(_KEYCODE["\b"])
            time.sleep(0.3)
            still = {
                k: v for k, v in {
                    "代码": self.read(panel.code),
                    "价格": self.read(panel.price),
                    "数量": self.read(panel.amount),
                }.items() if v.strip()
            }
            if still:
                raise AXError(f"下单框未能清空,残留 {still};拒绝继续以免误用旧值")
        return panel

    # ------------------------------------------------------------------
    # 左侧导航 (用户可能停在任意页面)
    # ------------------------------------------------------------------
    def dismiss_overlay(self) -> None:
        """点窗口空白处, 关掉搜索框下拉等遮挡 AX 遍历的浮层。"""
        try:
            x, y, w, h = geom(self.window())
        except AXError:
            return
        cx, cy = x + w * 0.45, y + h * 0.75      # 下半部空白区
        for ev in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            e = Quartz.CGEventCreateMouseEvent(
                None, ev, (cx, cy), Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
            time.sleep(0.05)

    def nav_buttons(self) -> list[tuple[int, float, object]]:
        """左侧导航栏按钮,返回 [(子节点索引, y坐标, 元素)]。

        这些按钮的 AXTitle 都读作'按钮'、AXPosition.x 报 -1,
        只能靠「x<0 且 y 在 100~600」这个几何特征识别,按 y 排序后
        与视觉顺序一致。
        """
        win = self.window()
        out = []
        for i, el in enumerate(attr(win, "AXChildren") or []):
            if str(attr(el, "AXRole") or "") != "AXButton":
                continue
            x, y, _w, _h = geom(el)
            if x < 0 and 100 <= y <= 600:
                out.append((i, y, el))
        return sorted(out, key=lambda t: t[1])

    def goto_trade_page(self, max_tries: int = 3) -> Panel:
        """从任意页面进入交易下单页。

        实测(同花顺 5.3.5)左侧导航第 6 项(按 y 排序,索引从1计)是交易页。
        为避免版本差异写死序号,先试该位置,失败则逐个点击探测。
        """
        # 已经在下单页就直接返回
        try:
            return self.find_panel()
        except AXError:
            pass

        for _try in range(max_tries):
            navs = self.nav_buttons()
            if not navs:
                # 读不到导航栏有两种原因, 必须分开处理:
                #   窗口不存在 —— Mac 关窗不退进程, AX 树恒空。2026-11-05
                #     实盘 3 笔全废就是这样: 进程活着但在屏窗口 0 个, 代码
                #     却一直去 dismiss_overlay(), 点空白处对没有的窗口毫无
                #     作用, 3 次重试后报"未找到左侧导航栏按钮"。
                #   浮层遮挡 —— 顶部搜索框下拉面板会挡住 AX 遍历, 这时才
                #     需要点空白处把它关掉(ESC 关不掉, 它是独立浮层)。
                if _try < max_tries - 1:
                    if self._onscreen_windows() == 0:
                        self._reopen_main_window()
                    else:
                        self.dismiss_overlay()
                    time.sleep(0.6)
                    continue
                raise AXError(
                    "未找到左侧导航栏按钮"
                    + ("(同花顺无在屏窗口, 自动唤回失败)"
                       if self._onscreen_windows() == 0 else "(疑有浮层遮挡)"))

            # 优先试已知位置(第6项),再回退到全量探测
            order = []
            if len(navs) >= 6:
                order.append(navs[5])
            order.extend(n for n in navs if n not in order)

            for _idx, _y, el in order:
                try:
                    self.press(el)
                except AXError:
                    continue
                panel = wait_until(self._try_panel, timeout=2.5, interval=0.06)
                if panel is not None:
                    return panel
                continue
        raise AXError("逐个点击左侧导航后仍未进入下单页,请手动打开交易界面")
