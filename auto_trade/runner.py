"""批量执行交易信号。

单笔下单的安全性由 Trader 保证;本层解决批量特有的问题:
  1. 一批信号中途失败,是停还是继续 —— 默认 **停**(halt_on_error);
  2. price_mode 非 fixed 时,需要先读面板拿到现价/涨跌停再定价;
  3. 整批执行的审计记录(谁、何时、下了什么、结果如何)。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from .ax import AXError
from .config import Config
from .signal import Signal, SignalError, dedupe, load_signals
from .trader import Result, Status, Trader

# 这些状态说明"没有真正下出去",可安全继续下一条
_SAFE_TO_CONTINUE = {
    Status.BLOCKED, Status.UNRECOGNIZED, Status.PRICE_DRIFT,
    Status.FILLED_ONLY, Status.AWAITING_CONFIRM,
}
# 这些状态下单意图已经发出但结果不确定,继续执行有风险
_MUST_STOP = {Status.NO_RECORD, Status.ERROR}


@dataclass
class Execution:
    """一条信号的执行记录。"""
    signal: dict
    status: str
    msg: str
    resolved_price: float | None = None
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class BatchReport:
    started_at: str
    finished_at: str
    total: int
    executions: list[Execution]
    stopped_early: bool = False
    stop_reason: str = ""

    @property
    def submitted(self) -> list[Execution]:
        return [e for e in self.executions if e.status == Status.SUBMITTED.value]

    @property
    def needs_attention(self) -> list[Execution]:
        return [e for e in self.executions if e.status in
                (Status.NO_RECORD.value, Status.ERROR.value)]

    def summary(self) -> str:
        from collections import Counter
        counts = Counter(e.status for e in self.executions)
        parts = [f"{k}={v}" for k, v in sorted(counts.items())]
        tail = f" | 提前终止: {self.stop_reason}" if self.stopped_early else ""
        return f"共 {self.total} 条, 执行 {len(self.executions)} 条 [{', '.join(parts)}]{tail}"

    def to_json(self) -> str:
        return json.dumps({
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.total,
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
            "executions": [asdict(e) for e in self.executions],
        }, ensure_ascii=False, indent=2)


class Runner:
    """把信号批量喂给 Trader。

        runner = Runner(Config(dry_run=False, auto_confirm=True))
        report = runner.run([
            {"side": "buy", "code": "600303", "price": 2.48, "amount": 100},
            {"side": "sell", "code": "600519", "price_mode": "limit_up", "amount": 100},
        ])
        print(report.summary())
    """

    def __init__(self, config: Config | None = None, trader: Trader | None = None):
        self.cfg = config or Config()
        self.trader = trader or Trader(self.cfg)
        self.log = self.trader.log

    # ------------------------------------------------------------------
    def _resolve_price(self, sig: Signal) -> float:
        """price_mode 非 fixed 时,读面板拿实时价格。

        做法:切到对应方向、填入代码,让同花顺联动出现价与涨跌停,再取值。
        """
        if sig.price_mode == "fixed":
            assert sig.price is not None
            return sig.price

        panel = self.trader.switch(sig.side)
        self.trader.ax.type_text(panel.code, sig.code)
        time.sleep(0.6)
        st = self.trader.ax.read_state(panel)
        if not st.recognized:
            raise AXError(f"同花顺未识别 {sig.code},无法解析 {sig.price_mode} 价格")

        source = {
            "last": st.price,
            "limit_up": st.limit_up,
            "limit_down": st.limit_down,
        }[sig.price_mode]
        try:
            price = float(str(source).strip())
        except (TypeError, ValueError):
            raise AXError(
                f"{sig.price_mode} 价格读取失败: {source!r} | {st.summary()}"
            )
        if price <= 0:
            raise AXError(f"{sig.price_mode} 解析出非正价格 {price}")
        return price

    # ------------------------------------------------------------------
    def run(
        self,
        signals: Iterable[Any],
        *,
        halt_on_error: bool = True,
        merge_duplicates: bool = True,
        interval: float = 1.0,
        on_result: Callable[[Signal, Result], None] | None = None,
        report_dir: str | None = "state/batches",
    ) -> BatchReport:
        """执行一批信号。

        halt_on_error=True(默认): 遇到 no_record/error 立即停止后续下单。
            这类状态表示"下单意图已发出但结果不明",继续执行可能叠加风险。
        merge_duplicates=True: 同方向同标的同价合并数量。
        """
        started = datetime.now().isoformat(timespec="seconds")
        try:
            parsed = load_signals(signals)
        except SignalError as exc:
            self.log.error("信号校验失败,整批未执行: %s", exc)
            raise

        if merge_duplicates:
            before = len(parsed)
            parsed = dedupe(parsed)
            if len(parsed) != before:
                self.log.info("信号合并: %d -> %d 条", before, len(parsed))

        self.log.info("开始执行 %d 条信号 (dry_run=%s, auto_confirm=%s)",
                      len(parsed), self.cfg.dry_run, self.cfg.auto_confirm)

        executions: list[Execution] = []
        stopped, reason = False, ""

        for i, sig in enumerate(parsed):
            self.log.info("[%d/%d] %s", i + 1, len(parsed), sig.describe())

            try:
                price = self._resolve_price(sig)
            except AXError as exc:
                self.log.error("定价失败: %s", exc)
                executions.append(Execution(asdict(sig), Status.ERROR.value, str(exc)))
                if halt_on_error:
                    stopped, reason = True, f"第 {i+1} 条定价失败"
                    break
                continue

            result = self.trader.place(sig.side, sig.code, price, sig.amount,
                                       verify=self.cfg.verify_orders)
            executions.append(Execution(
                asdict(sig), result.status.value, result.msg, resolved_price=price
            ))

            if on_result is not None:
                try:
                    on_result(sig, result)
                except Exception as exc:          # 回调不该影响交易主流程
                    self.log.warning("on_result 回调异常(已忽略): %s", exc)

            if result.status in _MUST_STOP and halt_on_error:
                stopped = True
                reason = f"第 {i+1} 条返回 {result.status.value},停止后续"
                self.log.error("!!! %s —— 剩余 %d 条未执行",
                               reason, len(parsed) - i - 1)
                break

            if i < len(parsed) - 1:
                time.sleep(interval)

        report = BatchReport(
            started_at=started,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            total=len(parsed),
            executions=executions,
            stopped_early=stopped,
            stop_reason=reason,
        )
        self.log.info("批次结束: %s", report.summary())

        if report_dir:
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(
                report_dir, f"{datetime.now():%Y%m%d_%H%M%S}.json"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(report.to_json())
            self.log.info("批次报告: %s", path)

        return report
