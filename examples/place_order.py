#!/usr/bin/env python3
"""下单示例。

默认 dry_run=True 只填值不提交。确认无误后再改参数。

    python examples/place_order.py                 # 空跑
    python examples/place_order.py --submit        # 提交到二次确认框为止
    python examples/place_order.py --submit --yes  # 连二次确认一起点
"""
import argparse
import sys

sys.path.insert(0, ".")
from auto_trade import Config, Trader


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--code", default="600303")
    ap.add_argument("--price", type=float, default=2.48)
    ap.add_argument("--amount", type=int, default=100)
    ap.add_argument("--submit", action="store_true", help="真实提交")
    ap.add_argument("--yes", action="store_true", help="自动点二次确认")
    args = ap.parse_args()

    trader = Trader(Config(dry_run=not args.submit, auto_confirm=args.yes))
    result = trader.place(
        args.side, args.code, args.price, args.amount, submit=args.submit
    )

    print()
    print(f"status : {result.status.value}")
    print(f"msg    : {result.msg}")
    if result.state:
        print(f"state  : {result.state.summary()}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
