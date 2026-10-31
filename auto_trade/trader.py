"""下单编排层:风控 -> 输入 -> 联动校验 -> 提交 -> 结果判定。

核心原则:提交前必须确认同花顺**业务层**已接收输入(联动字段已填充),
而不是只确认控件文本"看起来对"。参见 ax.py 顶部的实测结论 2/3。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .ax import AXError, Panel, PanelState, ThsAX, redact, wait_until
from .config import Config


class Status(str, Enum):
    FILLED_ONLY = "filled_only"   # 已填值未提交(dry_run)
    AWAITING_CONFIRM = "awaiting_confirm"  # 已弹出二次确认框,等待确认
    SUBMITTED = "submitted"       # 已确认,且委托表已回查到
    NO_RECORD = "no_record"       # 已确认但委托表无记录(如非交易时段)
    BLOCKED = "blocked"           # 风控/前置检查拦截,未提交
    UNRECOGNIZED = "unrecognized" # 同花顺未识别标的,拒绝提交
    PRICE_DRIFT = "price_drift"   # 价格被改写且不容忍,拒绝提交
    ERROR = "error"


@dataclass
class Result:
    status: Status
    side: str
    code: str
    price: str
    amount: str
    state: PanelState | None = None
    msg: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (Status.FILLED_ONLY, Status.SUBMITTED)


class Trader:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config()
        self.ax = ThsAX(settle=self.cfg.settle, key_delay=self.cfg.key_delay)
        self.log = self._logger()
        self._state = self._load()

    def _logger(self) -> logging.Logger:
        log = logging.getLogger("auto_trade")
        if log.handlers:
            return log
        log.setLevel(logging.INFO)
        os.makedirs(os.path.dirname(self.cfg.log_file) or ".", exist_ok=True)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        for h in (logging.FileHandler(self.cfg.log_file, encoding="utf-8"),
                  logging.StreamHandler()):
            h.setFormatter(fmt)
            log.addHandler(h)
        return log

    def _load(self) -> dict:
        today = date.today().isoformat()
        blank = {"date": today, "count": 0, "turnover": 0.0}
        try:
            with open(self.cfg.state_file, encoding="utf-8") as f:
                st = json.load(f)
            return st if st.get("date") == today else blank
        except (FileNotFoundError, json.JSONDecodeError):
            return blank

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.state_file) or ".", exist_ok=True)
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    def _risk(self, code: str, price: float, amount: int) -> str | None:
        if amount <= 0 or amount % 100:
            return f"数量须为100整数倍: {amount}"
        if self.cfg.stock_whitelist and code not in self.cfg.stock_whitelist:
            return f"{code} 不在白名单"
        notional = price * amount
        if notional > self.cfg.max_amount_per_order:
            return f"单笔 {notional:.0f} 超限 {self.cfg.max_amount_per_order:.0f}"
        if self._state["count"] >= self.cfg.max_orders_per_day:
            return f"日内笔数达上限 {self.cfg.max_orders_per_day}"
        if self._state["turnover"] + notional > self.cfg.max_turnover_per_day:
            return f"日内成交额将超上限 {self.cfg.max_turnover_per_day:.0f}"
        return None

    # ------------------------------------------------------------------
    def switch(self, side: str) -> Panel:
        """切到干净的下单页。side: 'buy' | 'sell'

        委托查询等操作会改变页面状态,下单框里也可能残留上一单的值,
        因此每单之前都走一次归位,而不是只切 tab。
        """
        return self.ax.reset_to_order_page(side)

    def place(
        self, side: str, code: str, price: float, amount: int,
        submit: bool | None = None, verify: bool = True,
    ) -> Result:
        """下单。submit=None 时取 cfg.dry_run 的反值。"""
        do_submit = (not self.cfg.dry_run) if submit is None else submit
        price_str = f"{price:.2f}"
        amount_str = str(amount)

        err = self._risk(code, price, amount)
        if err:
            self.log.warning("风控拦截 %s %s: %s", side, code, err)
            return Result(Status.BLOCKED, side, code, price_str, amount_str, msg=err)

        try:
            panel = self.switch(side)

            # 1) 代码 —— 必须先填,同花顺据此联动
            # 联动是异步的(要回服务端查名称/涨跌停),填完立刻读会得到空值,
            # 故轮询等到 recognized 或超时。
            self.ax.type_text(panel.code, code)
            st = wait_until(
                lambda: (lambda x: x if x.recognized else None)(
                    self.ax.read_state(panel)),
                timeout=self.cfg.recognize_timeout, interval=0.08,
            )
            if st is None:
                st = self.ax.read_state(panel)
            if self.cfg.require_recognized and not st.recognized:
                msg = f"同花顺未识别 {code}(联动字段为空),拒绝提交"
                self.log.error(msg + " | " + st.summary())
                return Result(Status.UNRECOGNIZED, side, code, price_str,
                              amount_str, st, msg)

            # 2) 数量
            self.ax.type_text(panel.amount, amount_str)
            # 3) 价格 —— 放最后,因为填代码会被同花顺改写成现价
            self.ax.type_text(panel.price, price_str)

            st = self.ax.read_state(panel)

            # 涨跌停校验:超出范围会被交易所废单,提前拦住
            if self.cfg.check_price_limit:
                for label, bound in (("涨停", st.limit_up), ("跌停", st.limit_down)):
                    try:
                        limit = float(str(bound).strip())
                    except (TypeError, ValueError):
                        continue
                    if label == "涨停" and price > limit + 1e-9:
                        msg = f"价格 {price_str} 高于涨停价 {limit},交易所将废单"
                        self.log.error(msg)
                        return Result(Status.BLOCKED, side, code, price_str,
                                      amount_str, st, msg)
                    if label == "跌停" and price < limit - 1e-9:
                        msg = f"价格 {price_str} 低于跌停价 {limit},交易所将废单"
                        self.log.error(msg)
                        return Result(Status.BLOCKED, side, code, price_str,
                                      amount_str, st, msg)

            # 可用量校验:买入看"可买",卖出看"可用"
            avail = st.available_qty
            if avail is not None and amount > avail:
                msg = f"数量 {amount} 超过{st.available}"
                self.log.error(msg)
                return Result(Status.BLOCKED, side, code, price_str,
                              amount_str, st, msg)

            # 价格漂移检测:同花顺可能再次改写
            actual = st.price.strip()
            if actual and abs(float(actual) - price) > 1e-6:
                msg = f"价格被改写: 期望 {price_str} 实际 {actual}"
                if not self.cfg.allow_price_override:
                    self.log.error(msg + ",拒绝提交 | " + st.summary())
                    return Result(Status.PRICE_DRIFT, side, code, price_str,
                                  amount_str, st, msg)
                self.log.warning(msg + ",按配置继续")

            self.log.info("就绪 %s | %s", side, st.summary())

            if not do_submit:
                return Result(Status.FILLED_ONLY, side, code, price_str,
                              amount_str, st, "dry_run:已填值未提交")

            # 4) 点[确定买入/卖出] —— 这一步只会弹出二次确认框,并未报单
            import time
            self.log.info("点击[%s]", panel.submit_title)
            self.ax.press(panel.submit)
            # 轮询确认框出现,不盲等 —— 通常 200~400ms 就弹出来了
            dlg = wait_until(self.ax.find_confirm_dialog,
                             timeout=max(self.cfg.post_submit_wait * 2, 3.0),
                             interval=0.05)
            if dlg is None:
                msg = "点击后未出现二次确认框,状态不明,请人工核对"
                self.log.error(msg)
                return Result(Status.ERROR, side, code, price_str,
                              amount_str, self.ax.read_state(panel), msg)

            self.log.info("二次确认框: %s", redact(dlg["text"].replace("\n", " "))[:120])

            if not self.cfg.auto_confirm:
                return Result(Status.AWAITING_CONFIRM, side, code, price_str,
                              amount_str, None,
                              "已弹出确认框,未自动确认(auto_confirm=False);"
                              "调用 ax.confirm_order() 或 ax.cancel_order_dialog()")

            # 5) 确认框内容必须与意图一致,否则取消
            #    严格模式下代码/价格/数量三项逐一核对,不一致即放弃本单,
            #    绝不"换个价格再试" —— 策略给什么就下什么。
            text = dlg["text"]
            checks = [("证券代码", code), ("当前价格", price_str)]
            if self.cfg.strict_params:
                checks.append(("数量", amount_str))
            for field_name, expect in checks:
                needle = f"{field_name}:{expect}"
                flat = text.replace(" ", "")
                if field_name == "数量":
                    # 确认框里写作"买入数量:100"/"卖出数量:100"
                    if f"{panel.direction}数量:{expect}" in flat:
                        continue
                if needle not in flat:
                    self.ax.cancel_order_dialog()
                    msg = f"确认框 {field_name} 与预期不符(期望 {expect}),已取消"
                    self.log.error(msg + " | " + redact(text.replace("\n", " "))[:150])
                    return Result(Status.BLOCKED, side, code, price_str,
                                  amount_str, None, msg)

            self.ax.confirm_order()
            self._state["count"] += 1
            self._state["turnover"] += price * amount
            self._save()

            # 6) 回查委托表 —— 唯一可信的成功判据
            # verify=False 时跳过(批量下单由调用方在末尾统一回查): 开/读委托表
            # 再切回下单页约 0.9s/单, 逐单做会让 3 单多花近 3 秒。
            if not verify:
                return Result(Status.SUBMITTED, side, code, price_str,
                              amount_str, st, "已确认(未回查,待批次末核对)")
            try:
                self.ax.open_orders_tab()
                orders = self.ax.read_orders()
            except AXError as exc:
                self.log.warning("委托表回查失败: %s", exc)
                orders = []
            finally:
                # 回查会把界面带离下单页,这里还原,保证下一单可直接执行
                try:
                    self.ax.reset_to_order_page(side)
                except AXError as exc:
                    self.log.warning("页面归位失败(下一单前请人工检查): %s", exc)
            # 匹配须同时核对方向与数量, 否则会撞上同一代码的历史委托
            want_side = "买入" if side == "buy" else "卖出"
            matched = [o for o in orders
                       if str(o.get("证券代码", "")).strip() == code
                       and want_side in str(o.get("操作", ""))
                       and str(o.get("委托数量", "")).strip() == amount_str]
            if matched:
                return Result(Status.SUBMITTED, side, code, price_str,
                              amount_str, None,
                              f"已确认并回查到委托: {matched[0]}")
            return Result(Status.NO_RECORD, side, code, price_str, amount_str,
                          None,
                          f"已点确认但委托表无 {code} 记录"
                          f"(当前共 {len(orders)} 条);非交易时段属正常,"
                          "交易时段出现此状态须立即人工核对")

        except AXError as exc:
            self.log.error("AX 异常: %s", exc)
            return Result(Status.ERROR, side, code, price_str, amount_str,
                          msg=str(exc))

    def verify_orders(self, expect: list[dict]) -> list[dict]:
        """批次末统一回查委托表。expect: [{side,code,amount}]。

        返回每项的核对结果 {side,code,amount,found,order}。
        """
        try:
            self.ax.open_orders_tab()
            orders = self.ax.read_orders()
        except AXError as exc:
            self.log.warning("委托表回查失败: %s", exc)
            orders = []
        out = []
        for e in expect:
            want = "买入" if e["side"] == "buy" else "卖出"
            hit = next((o for o in orders
                        if str(o.get("证券代码", "")).strip() == e["code"]
                        and want in str(o.get("操作", ""))
                        and str(o.get("委托数量", "")).strip() == str(e["amount"])),
                       None)
            out.append({**e, "found": hit is not None, "order": hit})
        return out

    def buy(self, code: str, price: float, amount: int, submit: bool | None = None):
        return self.place("buy", code, price, amount, submit)

    def sell(self, code: str, price: float, amount: int, submit: bool | None = None):
        return self.place("sell", code, price, amount, submit)

    def snapshot(self) -> PanelState:
        return self.ax.read_state(self.ax.find_panel())
