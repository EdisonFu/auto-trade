#!/usr/bin/env python3
"""从外部策略接入信号的示例。

默认空跑。确认无误后加 --submit --yes 才会真实下单。

    python examples/from_signals.py
    python examples/from_signals.py --submit --yes
"""
import argparse
import sys

sys.path.insert(0, ".")
from auto_trade import Config, Runner


def my_strategy_signals():
    """替换成你自己的策略输出。

    返回 dict 或 Signal 均可;dict 的字段:
        side       'buy' | 'sell'          必填
        code       '600303'  6位数字        必填
        amount     100  (100的整数倍)       必填
        price      2.48                     price_mode='fixed' 时必填
        price_mode 'fixed'|'last'|'limit_up'|'limit_down'   默认 fixed
        tag        策略名/信号ID,便于日志追溯
    """
    return [
        {"side": "buy", "code": "600303", "price": 2.48, "amount": 100,
         "tag": "demo-limit"},
        # 跟随盘口定价的写法(不要同时给 price):
        # {"side": "sell", "code": "600303", "price_mode": "limit_up",
        #  "amount": 100, "tag": "demo-follow"},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="真实提交")
    ap.add_argument("--yes", action="store_true", help="自动点二次确认框")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="某条失败后继续执行剩余信号(默认停止)")
    args = ap.parse_args()

    runner = Runner(Config(
        dry_run=not args.submit,
        auto_confirm=args.yes,
        max_amount_per_order=10000,     # 示例用小额度
    ))

    def on_result(sig, result):
        mark = "OK" if result.ok else "!!"
        print(f"  [{mark}] {sig.describe()} -> {result.status.value}")

    report = runner.run(
        my_strategy_signals(),
        halt_on_error=not args.continue_on_error,
        on_result=on_result,
    )

    print()
    print(report.summary())
    if report.needs_attention:
        print()
        print("以下信号状态不确定,请人工核对委托列表:")
        for e in report.needs_attention:
            print(f"  {e.signal['side']} {e.signal['code']} "
                  f"x{e.signal['amount']} -> {e.status}: {e.msg[:80]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
