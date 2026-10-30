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
from Cocoa import NSWorkspace

BUNDLE_ID = "cn.com.10jqka.macstockPro"

# CGEvent 虚拟键码
_KEYCODE = {
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
    "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
    ".": 47, "\b": 51, "\r": 36,
}


class AXError(Exception):
    pass


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
    def activate(self) -> None:
        subprocess.run(
            ["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'],
            capture_output=True,
        )
        time.sleep(1.2)

    def _pid(self) -> int:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.bundleIdentifier() == BUNDLE_ID:
                return app.processIdentifier()
        raise AXError("同花顺未运行")

    def window(self, retries: int = 12):
        app = AXUIElementCreateApplication(self._pid())
        for attempt in range(retries):
            wins = attr(app, "AXWindows") or []
            if wins and (attr(wins[0], "AXChildren") or []):
                return wins[0]
            if attempt == 0:
                self.activate()
            else:
                time.sleep(0.4)
        raise AXError("无法获取窗口(AX 树为空;请确认同花顺未最小化)")

    # ------------------------------------------------------------------
    # 面板定位
    # ------------------------------------------------------------------
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
        time.sleep(self.key_delay)

    def focus(self, element) -> None:
        err = AXUIElementSetAttributeValue(element, "AXFocused", True)
        if err != 0:
            raise AXError(f"聚焦失败 (AXError={err})")
        time.sleep(0.35)

    def type_text(self, element, text: str, clear_first: bool = True) -> None:
        """聚焦后用真实键盘事件输入。不要用 AXValue —— 应用层收不到。"""
        self.focus(element)
        if clear_first:
            for _ in range(12):
                self._key(_KEYCODE["\b"])
        for ch in text:
            if ch not in _KEYCODE:
                raise AXError(f"不支持的字符: {ch!r}")
            self._key(_KEYCODE[ch])
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
        time.sleep(0.8)
        return True

    # ------------------------------------------------------------------
    # 二次确认框
    # ------------------------------------------------------------------
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
        time.sleep(1.2)
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
        time.sleep(0.8)
        return text

    # ------------------------------------------------------------------
    # 委托查询
    # ------------------------------------------------------------------
    def open_orders_tab(self) -> None:
        """点击下方[委托]页签。"""
        win = self.window()
        for el in attr(win, "AXChildren") or []:
            if str(attr(el, "AXRole") or "") != "AXButton":
                continue
            if str(attr(el, "AXTitle") or "").strip() != "委托":
                continue
            _x, y, _w, _h = geom(el)
            if 360 <= y <= 390:
                self.press(el)
                time.sleep(1.5)
                return
        raise AXError("未找到[委托]页签")

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
            time.sleep(0.8)
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
                time.sleep(1.0)
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

        for _ in range(max_tries):
            navs = self.nav_buttons()
            if not navs:
                raise AXError("未找到左侧导航栏按钮")

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
                time.sleep(1.6)
                try:
                    return self.find_panel()
                except AXError:
                    continue
        raise AXError("逐个点击左侧导航后仍未进入下单页,请手动打开交易界面")
