"""下单编排层:风控 -> 输入 -> 联动校验 -> 提交 -> 结果判定。

核心原则:提交前必须确认同花顺**业务层**已接收输入(联动字段已填充),
而不是只确认控件文本"看起来对"。参见 ax.py 顶部的实测结论 2/3。
"""
from __future__ import annotations

import json
import logging
import time
import os
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .ax import (AXError, LoginRefused, Panel, PanelState, ThsAX,
                 redact, wait_until)
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
    # 已点过[确定买入/卖出]: 这之后出的任何异常, 单都可能已经报出去了
    maybe_sent: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (Status.FILLED_ONLY, Status.SUBMITTED)


class Trader:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config()
        self.ax = ThsAX(settle=self.cfg.settle, key_delay=self.cfg.key_delay)
        self.log = self._logger()
        self._state = self._load()
        self._ready_at = 0.0
        # 委托表快照: (读取时刻, 委托列表)。批量下单时由 snapshot_orders()
        # 先读一次, 各笔从中算防重复基线, 免得每笔都开委托表再切回
        # (实测每笔 1~2.5s)。只在第一次尝试时使用, 重试仍然现读。
        self._orders_snap: tuple[float, list] | None = None
        # 预热刚确认过界面干净: 仅下一笔可跳过报错框扫描, 用后即清
        self._last_clean = False

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

    def ensure_ready(self) -> tuple[bool, str]:
        """确保同花顺可下单: 没开就拉起、窗口被关就唤回、掉登录就重登。

        Trader 自己负责, 不指望调用方先跑好自检 —— 9:25 下单那一刻的状态
        才算数, 9:18 的检查通过不代表 9:25 还行(2026-11-05 实盘即如此)。
        """
        if not self.cfg.self_heal:
            return True, "self_heal 关闭"
        # 刚就绪过(默认30s内)就直接放行: 连续下3单时每单都做一遍
        # login_sheet+nav+funds 三次全树遍历约0.3s, 纯属浪费关键路径时间。
        if self._ready_at and time.time() - self._ready_at < self.cfg.ready_cache_s:
            return True, "近期已就绪"
        # 锁屏下快路径会读到空树然后误入完整自检, 先点亮
        self.ax.wake_screen()
        # 每批首次确保前台(缓存命中不再激活): 后台时CGEvent会落到别的App
        self.ax.ensure_frontmost()
        # 已就绪的快路径: 不把每单都拖慢到完整自检(约 2s)
        try:
            if (self.ax.login_sheet() is None and self.ax.nav_buttons()
                    and "可用金额" in self.ax.read_funds()):
                self._ready_at = time.time()
                return True, "已就绪"
        except AXError:
            pass
        ok, why = self.ax.ready_to_trade(self.cfg.trade_password or None)
        (self.log.info if ok else self.log.error)("就绪自检: %s", why)
        if ok:
            self._ready_at = time.time()
        return ok, why

    # 换环境重试也解决不了的结果: 风控拦截/券商拒单/价格被改写/等人工确认。
    # 其余(AX 异常、未识别、点了确认却查不到)都可能是界面状态问题, 值得重来。
    _FINAL = {Status.FILLED_ONLY, Status.SUBMITTED, Status.BLOCKED,
              Status.PRICE_DRIFT, Status.AWAITING_CONFIRM}

    def snapshot_orders(self, side: str = "buy") -> bool:
        """批量下单前读一次委托表作为防重复基线, 读完回到下单页。

        返回是否读到。读不到时各笔退回逐笔现读(旧行为)。
        """
        try:
            self.ax.open_orders_tab()
            orders = self.ax.read_orders()
        except AXError as exc:
            self.log.warning("委托表快照失败, 改为逐笔读取: %s", exc)
            self._orders_snap = None
            return False
        self._orders_snap = (time.time(), orders)
        try:
            self.ax.reset_to_order_page(side)
        except AXError as exc:
            self.log.warning("快照后归位失败(下单时会重新归位): %s", exc)
        return True

    @staticmethod
    def _match_count(orders: list, side: str, code: str, amount: int) -> int:
        want = "买入" if side == "buy" else "卖出"
        return sum(1 for o in orders
                   if str(o.get("证券代码", "")).strip() == code
                   and want in str(o.get("操作", ""))
                   and str(o.get("委托数量", "")).strip() == str(amount))

    def _count_orders(self, side: str, code: str, amount: int,
                      use_snapshot: bool = False) -> int | None:
        """委托表里 同代码+同方向+同数量 的条数; 读不到返回 None。

        用"下单前后条数是否增加"判断这单报没报出去, 不怕撞上当天同一只票
        的历史委托(含废单)。use_snapshot=True 且有 5 分钟内的快照时用快照,
        否则现读。快照只适合做"第一次尝试前"的基线: 若快照之后本程序已对
        同一只票下过单, 基线会偏低 —— 这种情况由批次内去重(同代码合并)排除。
        """
        if use_snapshot and self._orders_snap is not None \
                and time.time() - self._orders_snap[0] < 300:
            return self._match_count(self._orders_snap[1], side, code, amount)
        try:
            self.ax.open_orders_tab()
            orders = self.ax.read_orders()
        except AXError as exc:
            self.log.warning("读委托表失败: %s", exc)
            return None
        return self._match_count(orders, side, code, amount)

    def restart_app(self) -> tuple[bool, str]:
        """退出同花顺 → 冷启动 → 登录 → 自检。最重的一级恢复。"""
        self.log.warning("重启同花顺...")
        if not self.ax.quit_app():
            return False, "同花顺退出失败"
        return self.ax.ready_to_trade(self.cfg.trade_password or None)

    def place(
        self, side: str, code: str, price: float, amount: int,
        submit: bool | None = None, verify: bool = True,
    ) -> Result:
        """下单, 失败则逐级恢复后重来, 直到委托表里出现这一单。

        恢复分级(第 n 次尝试前):
          1. 快速就绪检查(已就绪约 0.3s, 不就绪才走完整自检)
          2. 完整自检: 拉起/唤回窗口/重登
          3+. 退出同花顺重启 —— 界面卡死、弹出不认识的框时只有这招管用

        防重复下单: 真实提交时, 第一次尝试前记下委托表里同代码同方向同数量
        的条数作为基线; 之后每次重试前先复查, 条数已增加说明上一次其实报
        出去了(只是没确认到), 直接返回成功而不再下单。
        """
        do_submit = (not self.cfg.dry_run) if submit is None else submit
        price_str = f"{price:.2f}"
        amount_str = str(amount)
        err = self._risk(code, price, amount)
        if err:
            self.log.warning("风控拦截 %s %s: %s", side, code, err)
            return Result(Status.BLOCKED, side, code, price_str, amount_str, msg=err)

        attempts = max(1, self.cfg.max_attempts if self.cfg.self_heal else 1)
        baseline: int | None = None
        res: Result | None = None
        for n in range(1, attempts + 1):
            _step_t0 = time.perf_counter()
            if n == 1:
                ok, why = self.ensure_ready()
            elif n == 2:
                ok, why = self.ax.ready_to_trade(self.cfg.trade_password or None)
            else:
                ok, why = self.restart_app()
            if not ok:
                self.log.error("第%d次: 同花顺未就绪: %s", n, why)
                res = Result(Status.ERROR, side, code, price_str, amount_str,
                             msg=f"同花顺未就绪: {why}")
                if why.startswith("券商拒绝登录"):
                    # 券商明确拒绝(系统测试/密码错/冻结): 重试和重启同花顺都
                    # 只会再打登录接口, 有锁户风险。立即停下交人工。
                    res.msg = f"[已停止: {why}, 请人工处理]"
                    return res
                continue

            self.log.info("下单计时 %s 第%d次 就绪检查 %.3fs (%s)",
                          code, n, time.perf_counter() - _step_t0, why)
            if do_submit:
                _cnt_t0 = time.perf_counter()
                cnt = self._count_orders(side, code, amount,
                                         use_snapshot=(n == 1))
                _snap_used = (self._orders_snap is not None
                              and time.time() - self._orders_snap[0] < 300)
                self.log.info("下单计时 %s 第%d次 防重复基线 %.3fs (%s)",
                              code, n, time.perf_counter() - _cnt_t0,
                              "快照" if (_snap_used and n == 1) else "现读")
                if baseline is None:
                    baseline = cnt
                elif cnt is not None and cnt > baseline:
                    self.log.warning("第%d次前复查: 委托表已有 %s 这一单"
                                     "(上次其实已报出), 不再重复下单", n, code)
                    return Result(Status.SUBMITTED, side, code, price_str,
                                  amount_str, None,
                                  f"重试前复查到委托(第{n-1}次已报出)")

            res = self._place_once(side, code, price, amount, submit, verify)
            if res.status in self._FINAL:
                if n > 1:
                    res.msg = f"[第{n}次尝试] {res.msg}"
                return res
            self.log.warning("第%d/%d次未完成 (%s): %s",
                             n, attempts, res.status.value, res.msg)
            # 单可能已报出, 却又没有可信的委托表基线去核对 —— 此时重下有
            # 重复成交的风险, 宁可停下交给人工(告警邮件会带上这条)。
            if do_submit and res.maybe_sent and baseline is None:
                res.msg = ("[已停止重试: 单可能已报出但委托表读不到, 无法排除"
                           f"重复下单, 请人工核对] {res.msg}")
                return res

        # 所有尝试用尽: 最后再查一次, 免得把"其实报出去了"误报为失败
        if do_submit and baseline is not None:
            cnt = self._count_orders(side, code, amount)
            if cnt is not None and cnt > baseline:
                return Result(Status.SUBMITTED, side, code, price_str,
                              amount_str, None, "末次复查到委托")
        assert res is not None
        res.msg = f"[{attempts}次尝试均未完成] {res.msg}"
        return res

    def _place_once(
        self, side: str, code: str, price: float, amount: int,
        submit: bool | None, verify: bool,
    ) -> Result:
        do_submit = (not self.cfg.dry_run) if submit is None else submit
        price_str = f"{price:.2f}"
        amount_str = str(amount)
        maybe_sent = False
        _t0 = time.perf_counter()

        def _mark(label: str) -> None:
            self.log.info("下单计时 %s %s %.3fs", code, label,
                          time.perf_counter() - _t0)

        try:
            # 上一单可能留下警告框/确认框, 不清掉会让本单读到别人的确认框
            # 而报"证券代码与预期不符"。只有预热刚确认界面干净时才跳过这次整树
            # 扫描(约 0.25s)。点确认之后不能跳: 券商拒单的警告框正是确认后才弹,
            # 不关掉会挡住下一笔。
            if not self._last_clean:
                self.ax.dismiss_error_dialog()
            self._last_clean = False
            _mark("清错误框")
            panel = self.switch(side)
            _mark("归位下单页")

            # 1) 代码 —— 必须先填,同花顺据此联动
            # 联动是异步的(要回服务端查名称/涨跌停),填完立刻读会得到空值,
            # 故轮询等到 recognized 或超时。
            self.ax.type_text(panel.code, code)
            _mark("填代码")
            # 页面重建后旧引用写入会静默失败(值进不去也不报错),
            # 检测到没写进去就重新定位面板再试一次。
            if (self.ax.read(panel.code) or "").strip() != code:
                self.log.debug("代码未写入, 重新定位面板")
                panel = self.ax.reset_to_order_page(side)
                self.ax.type_text(panel.code, code)
            st = wait_until(
                lambda: (lambda x: x if x.recognized else None)(
                    self.ax.read_state(panel)),
                timeout=self.cfg.recognize_timeout, interval=0.08,
            )
            if st is None:
                st = self.ax.read_state(panel)
                self.log.warning("识别股票等待超时 %.3fs", time.perf_counter() - _t0)
            _mark("识别股票")
            if self.cfg.require_recognized and not st.recognized:
                msg = f"同花顺未识别 {code}(联动字段为空),拒绝提交"
                self.log.error(msg + " | " + st.summary())
                return Result(Status.UNRECOGNIZED, side, code, price_str,
                              amount_str, st, msg)

            # 2) 数量  3) 价格 —— 价格放最后, 因为填代码会被同花顺改写成现价
            if self.cfg.fast_fill:
                self.ax.set_value(panel.amount, amount_str)
                self.ax.set_value(panel.price, price_str)
            else:
                self.ax.type_text(panel.amount, amount_str)
                self.ax.type_text(panel.price, price_str)
            _mark("填数量价格")

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
            self.log.info("点击[%s]", panel.submit_title)
            maybe_sent = True
            self.ax.press(panel.submit)
            _mark("点击确定")
            # 轮询确认框出现,不盲等 —— 通常 200~400ms 就弹出来了
            dlg = wait_until(self.ax.find_confirm_dialog,
                             timeout=max(self.cfg.post_submit_wait * 2, 3.0),
                             interval=0.05)
            if dlg is None:
                msg = "点击后未出现二次确认框,状态不明,请人工核对"
                self.log.error(msg)
                return Result(Status.ERROR, side, code, price_str,
                              amount_str, self.ax.read_state(panel), msg,
                              maybe_sent=True)

            # 券商拒单会弹只有[确认]的警告框(如"可用资金不足")。
            # 必须关掉并如实报错, 否则它会一直挡住后续所有操作。
            err_text = self.ax.dismiss_error_dialog()
            if err_text:
                msg = f"券商拒单: {redact(err_text)[:120]}"
                self.log.error(msg)
                return Result(Status.BLOCKED, side, code, price_str,
                              amount_str, None, msg)

            self.log.info("二次确认框: %s", redact(dlg["text"].replace("\n", " "))[:120])

            _mark("确认框出现")
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

            self.ax.confirm_order(dlg)
            _mark("确认报出")
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
                          None, maybe_sent=True, msg=
                          f"已点确认但委托表无 {code} 记录"
                          f"(当前共 {len(orders)} 条);非交易时段属正常,"
                          "交易时段出现此状态须立即人工核对")

        except AXError as exc:
            self.log.error("AX 异常: %s", exc)
            return Result(Status.ERROR, side, code, price_str, amount_str,
                          msg=str(exc), maybe_sent=maybe_sent)

    @staticmethod
    def _match_order(orders: list[dict], e: dict):
        want = "买入" if e["side"] == "buy" else "卖出"
        return next((o for o in orders
                     if str(o.get("证券代码", "")).strip() == e["code"]
                     and want in str(o.get("操作", ""))
                     and str(o.get("委托数量", "")).strip() == str(e["amount"])),
                    None)

    def verify_orders(self, expect: list[dict]) -> list[dict]:
        """批次末统一回查委托表。expect: [{side,code,amount}]。

        返回每项的核对结果 {side,code,amount,found,order}。
        """
        # 委托表刷新有延迟(实测可达2s), 轮询到全部命中或超时
        orders = []
        deadline = time.time() + 5.0
        while True:
            try:
                self.ax.open_orders_tab()
                orders = self.ax.read_orders()
            except AXError as exc:
                self.log.warning("委托表回查失败: %s", exc)
                orders = []
            missing = [e for e in expect
                       if not self._match_order(orders, e)]
            if not missing or time.time() >= deadline:
                break
            time.sleep(0.5)
        out = []
        for e in expect:
            hit = self._match_order(orders, e)
            out.append({**e, "found": hit is not None, "order": hit})
        return out

    def buy(self, code: str, price: float, amount: int, submit: bool | None = None):
        return self.place("buy", code, price, amount, submit)

    def sell(self, code: str, price: float, amount: int, submit: bool | None = None):
        return self.place("sell", code, price, amount, submit)

    def snapshot(self) -> PanelState:
        return self.ax.read_state(self.ax.find_panel())
