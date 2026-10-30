"""交易信号的标准形态与校验。

外部策略程序只需产出 Signal(或等价的 dict),不必关心下单细节。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal

Side = Literal["buy", "sell"]


class SignalError(ValueError):
    """信号本身不合法(格式/取值问题),在触及交易界面前就被拒绝。"""


@dataclass
class Signal:
    """一条交易指令。

    price=None 表示"跟随盘口",由 price_mode 决定最终价格:
      * 'last'       —— 用面板现价(同花顺填入代码后自动带出)
      * 'limit_up'   —— 涨停价
      * 'limit_down' —— 跌停价
    """
    side: Side
    code: str
    amount: int
    price: float | None = None
    price_mode: Literal["fixed", "last", "limit_up", "limit_down"] = "fixed"
    tag: str = ""                     # 策略名/信号ID,便于日志追溯
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise SignalError(f"side 必须是 buy/sell,得到 {self.side!r}")

        code = str(self.code).strip()
        if not (code.isdigit() and len(code) == 6):
            raise SignalError(f"证券代码须为6位数字,得到 {self.code!r}")
        self.code = code

        if not isinstance(self.amount, int) or self.amount <= 0:
            raise SignalError(f"数量须为正整数,得到 {self.amount!r}")
        if self.amount % 100:
            raise SignalError(f"A股数量须为100整数倍,得到 {self.amount}")

        if self.price_mode == "fixed":
            if self.price is None:
                raise SignalError("price_mode='fixed' 时必须给 price")
            if self.price <= 0:
                raise SignalError(f"价格须为正数,得到 {self.price}")
        elif self.price is not None:
            raise SignalError(
                f"price_mode={self.price_mode!r} 时不应再给 price(会被忽略,"
                "显式传入易产生误解)"
            )

    @property
    def notional(self) -> float:
        """名义金额;price_mode 非 fixed 时未知,返回 0。"""
        return (self.price or 0) * self.amount

    def describe(self) -> str:
        px = f"{self.price:.2f}" if self.price is not None else self.price_mode
        tag = f" [{self.tag}]" if self.tag else ""
        return f"{self.side} {self.code} x{self.amount} @{px}{tag}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        known = {"side", "code", "amount", "price", "price_mode", "tag", "meta"}
        kwargs = {k: v for k, v in d.items() if k in known}
        if "amount" in kwargs:
            try:
                kwargs["amount"] = int(kwargs["amount"])
            except (TypeError, ValueError):
                raise SignalError(f"数量无法解析为整数: {d.get('amount')!r}")
        if kwargs.get("price") is not None:
            try:
                kwargs["price"] = float(kwargs["price"])
            except (TypeError, ValueError):
                raise SignalError(f"价格无法解析: {d.get('price')!r}")
        extra = {k: v for k, v in d.items() if k not in known}
        if extra:
            kwargs.setdefault("meta", {}).update(extra)
        return cls(**kwargs)


def load_signals(items: Iterable[Any]) -> list[Signal]:
    """把 dict/Signal 混合的可迭代对象规整成 Signal 列表。

    任一条非法即整批抛错 —— 不做"跳过坏的继续下好的",
    因为部分执行会让策略端难以对账。
    """
    out: list[Signal] = []
    for i, item in enumerate(items):
        if isinstance(item, Signal):
            out.append(item)
            continue
        if isinstance(item, dict):
            try:
                out.append(Signal.from_dict(item))
            except SignalError as exc:
                raise SignalError(f"第 {i} 条信号非法: {exc}") from None
            continue
        raise SignalError(f"第 {i} 条既不是 Signal 也不是 dict: {type(item).__name__}")
    return out


def dedupe(signals: list[Signal]) -> list[Signal]:
    """同方向同标的合并数量,避免重复下单。"""
    merged: dict[tuple[str, str, str, float | None], Signal] = {}
    order: list[tuple] = []
    for s in signals:
        key = (s.side, s.code, s.price_mode, s.price)
        if key in merged:
            merged[key].amount += s.amount
        else:
            merged[key] = Signal(
                side=s.side, code=s.code, amount=s.amount, price=s.price,
                price_mode=s.price_mode, tag=s.tag, meta=dict(s.meta),
            )
            order.append(key)
    return [merged[k] for k in order]
