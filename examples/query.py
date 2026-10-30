#!/usr/bin/env python3
"""查询当前下单面板状态与当日委托。只读,不下单。"""
import sys

sys.path.insert(0, ".")
from auto_trade import Trader
from auto_trade.ax import AXError


def main():
    trader = Trader()
    print("=== 下单面板 ===")
    print(trader.snapshot().summary())
    print()
    print("=== 当日委托 ===")
    try:
        trader.ax.open_orders_tab()
        orders = trader.ax.read_orders()
    except AXError as exc:
        print(f"读取失败: {exc}")
        return 1
    if not orders:
        print("(无委托记录)")
    for o in orders:
        print(" ", {k: v for k, v in o.items() if v not in ("", "--")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
