# auto-trade

同花顺 **Mac 版** 自动下单，基于 macOS Accessibility API。

无需 Windows 虚拟机、无需 OCR、无需模拟坐标点击。

## 为什么是这个方案

调研过的三条路，实测后的结论：

| 方案 | 结论 |
|---|---|
| GitHub 现成工具（thsauto 等） | ❌ 全部 Windows 专用，驱动 `xiadan.exe`；easytrader 已下架 |
| Windows 虚拟机 | ⚠️ 可行但重：10G 内存 + x86 模拟，且依赖 OCR 读状态栏 |
| **Mac 原生 AX** | ✅ 控件可定位、状态可结构化读取、输入可校验 |

关键实测数据：同花顺 Mac 版（5.3.5）界面是**原生 Cocoa**，AX 可读到 1518 个控件节点。

## 环境

- macOS（实测 26.6.2 / Apple M2 Pro）
- 同花顺 Mac 版（实测 5.3.5，bundle id `cn.com.10jqka.macstockPro`）
- Python 3.9+

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**必须授权辅助功能**：系统设置 → 隐私与安全性 → 辅助功能 → 加入运行脚本的程序（终端 / VS Code）。

## 使用

```bash
# 只读:看面板状态和当日委托
.venv/bin/python examples/query.py

# 空跑:填值+校验,不提交
.venv/bin/python examples/place_order.py --code 600303 --price 2.48 --amount 100

# 提交到二次确认框为止(需手动点确认)
.venv/bin/python examples/place_order.py --code 600303 --price 2.48 --amount 100 --submit

# 全自动(连二次确认一起点)
.venv/bin/python examples/place_order.py --code 600303 --price 2.48 --amount 100 --submit --yes
```

## 接入你自己的策略信号

### 单笔下单

```python
from auto_trade import Trader, Config

trader = Trader(Config(dry_run=False, auto_confirm=True,
                       max_amount_per_order=10000))
r = trader.buy("600303", price=2.48, amount=100, submit=True)
print(r.status.value, r.msg)
```

### 批量信号（推荐）

策略程序只需产出 `dict` 或 `Signal`，`Runner` 负责校验、定价、风控、执行、审计：

```python
from auto_trade import Runner, Config

signals = [
    {"side": "buy",  "code": "600303", "price": 2.48, "amount": 100, "tag": "mom-1"},
    {"side": "sell", "code": "600519", "price_mode": "limit_up", "amount": 100},
]

runner = Runner(Config(dry_run=False, auto_confirm=True))
report = runner.run(signals)

print(report.summary())            # 共 2 条, 执行 2 条 [submitted=2]
for e in report.needs_attention:   # 状态不确定的,必须人工核对
    print(e.signal, e.status, e.msg)
```

直接跑示例：

```bash
.venv/bin/python examples/from_signals.py              # 空跑
.venv/bin/python examples/from_signals.py --submit --yes  # 真实下单
```

### 信号字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `side` | ✓ | `buy` / `sell` |
| `code` | ✓ | 6 位数字证券代码 |
| `amount` | ✓ | 股数，须为 100 整数倍 |
| `price` | 条件 | `price_mode='fixed'`（默认）时必填 |
| `price_mode` | | `fixed` / `last`（现价）/ `limit_up`（涨停）/ `limit_down`（跌停） |
| `tag` | | 策略名或信号 ID，写入日志便于追溯 |
| `meta` | | 任意附加字段，原样留在审计报告里 |

`price_mode` 非 `fixed` 时**不要再传 `price`** —— 会直接报错，避免"以为指定了价格其实被忽略"。

### 批量执行的行为约定

- **整批校验**：任一条信号格式非法，整批拒绝执行，不会"跳过坏的继续下好的"——部分执行会让策略端难以对账。
- **失败即停**（默认 `halt_on_error=True`）：某条返回 `no_record` / `error` 时立即停止后续下单。这两种状态意味着"下单意图已发出但结果不明"，继续执行会叠加风险。要改成继续，传 `halt_on_error=False`。
- **自动合并**：同方向、同标的、同价的信号会合并数量，避免重复下单。传 `merge_duplicates=False` 关闭。
- **审计落盘**：每批执行写一份 JSON 到 `state/batches/`，含每条信号的原始内容、解析后价格、执行状态与时间。

### 实时回调

```python
def on_result(sig, result):
    if not result.ok:
        notify(f"{sig.describe()} 失败: {result.msg}")

runner.run(signals, on_result=on_result)
```

回调里抛异常不会中断交易主流程（会记一条 warning）。

## 状态语义

| status | 含义 |
|---|---|
| `filled_only` | 已填值并校验通过，未提交（dry_run） |
| `awaiting_confirm` | 已弹出二次确认框，等待确认（`auto_confirm=False`） |
| `submitted` | 已确认**且委托表回查到记录** |
| `no_record` | 已点确认但委托表无记录 —— 非交易时段属正常；**交易时段出现须立即人工核对** |
| `blocked` | 风控/前置校验拦截，未提交 |
| `unrecognized` | 同花顺未识别该标的，拒绝提交 |
| `price_drift` | 价格被同花顺改写且不容忍，拒绝提交 |

`Result.ok` 为真仅包含 `filled_only` 与 `submitted`。批量执行后用 `report.needs_attention` 取出所有状态不确定的信号。

## 踩过的坑（都已在代码中处理）

1. **窗口失焦后 AX 树塌缩为 0 节点** → 每次操作前 activate + 重试取窗口。

2. **`AXValue` 直接写入是假阳性** —— 控件显示会变、读回"一致"，但同花顺业务层毫无反应（股票名/涨跌停/可买全是 `- -`）。据此提交会下出内部状态为空的单。
   → **必须用 `CGEvent` 发真实键盘事件**。

3. **填代码会被同花顺自动改写价格为现价** → 价格放在最后填，提交前重读校验（`price_drift`）。

4. **买入面板显示「可买:N股」，卖出面板显示「可用:N股」** → 两个词都要匹配，否则会误判为无持仓。

5. **点[确定买入]不会报单，只弹二次确认框** → 必须处理确认框，并核对框内代码/价格与意图一致才点确认。

6. **左侧导航栏按钮的 `AXTitle` 都读作"按钮"、`AXPosition.x` 报 -1** —— 只能靠「x<0 且 y 在 100~600」识别，按 y 排序后与视觉顺序一致。实测第 6 项是交易页。
   → `goto_trade_page()` 先试该位置，失败则逐个点击探测，用户停在任何页面都能回到下单页。

7. **委托查询会把界面带离下单页，下单框还会残留上一单的值** —— 残留值会让下一单误用旧数量/旧价格。
   → 每单之前调 `reset_to_order_page(side)`：取消遗留确认框 → 确保在交易页 → 切方向 → 点[重填]清空（清不干净则逐框退格兜底，仍失败就报错拒绝执行）。

8. **输入框靠标签 y 坐标配对，不能用子节点索引** —— 实测索引顺序是 `/41`(代码) `/37`(价格) `/42`(数量)，与视觉顺序不符。

## 风控

`Config` 集中管理，默认值偏保守：

- `dry_run=True` / `auto_confirm=False` —— 默认不会真实下单
- `max_orders_per_day` / `max_amount_per_order` / `max_turnover_per_day` —— 日内计数**落盘**（`state/daily_state.json`），进程重启绕不过
- `stock_whitelist` —— 标的白名单
- `require_recognized` —— 同花顺未识别标的则拒绝提交
- `strict_params=True` —— **严格按入参执行**：代码、价格、数量、方向一律以策略给的为准，确认框里逐项核对，任何一项不符即放弃本单，绝不自行替换价格
- 数量须为 100 整数倍，且不超过面板显示的可买/可用量

## 风险提示

- GUI 自动化属于**模拟人工操作**，不是券商授权的程序化交易通道。多数券商用户协议对此有限制，A 股程序化交易亦有报备要求 —— 实盘前请自行确认券商态度。
- 同花顺改版会导致控件定位失效。上线前跑 `tools/scan_panel.py` 核对。
- **`submitted` 之外的任何状态都不代表下单成功。** 交易时段出现 `no_record` 必须立即人工核对。

## 连续下单与页面导航

无需手动把同花顺停在交易页 —— 每单执行前会自动归位：

```python
trader.buy("600303", price=2.50, amount=100)   # 当前在涨停板页也能执行
```

归位流程：取消遗留确认框（点[取消]，绝不误提交）→ 不在交易页则走左侧导航 → 切到对应买卖方向 → 清空下单框残留值。

也可手动调用：

```python
from auto_trade import ThsAX
ax = ThsAX()
ax.goto_trade_page()           # 从任意页面进入交易页
panel = ax.reset_to_order_page("sell")   # 归位到干净的卖出页
ax.dismiss_dialogs()           # 单独清理遗留确认框
```

## 目录

```
auto_trade/
  ax.py        AX 驱动:窗口/控件定位、CGEvent 输入、确认框、委托表
  trader.py    编排:风控 → 输入 → 联动校验 → 确认框核对 → 回查
  config.py    风控与行为配置
examples/      可直接运行的示例
tools/         控件树探测(改版后排查用)
reference/     Windows 方案源码存档(已放弃,保留结论依据)
```
