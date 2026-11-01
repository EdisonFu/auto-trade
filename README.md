# auto-trade

同花顺 **Mac 版** 自动下单，基于 macOS Accessibility API。

无需 Windows 虚拟机、无需 OCR、无需模拟坐标点击。

---

> ## ⚠️ 使用前必读
>
> **这个工具会向你的真实交易账户提交委托，可能造成资金损失。**
>
> - **不是券商授权通道。** GUI 自动化属于模拟人工操作，多数券商的用户协议
>   对此有限制，A 股程序化交易另有报备要求。实盘前请自行确认券商态度与
>   合规要求，由此产生的账户风险由使用者承担。
> - **作者不对任何损失负责。** 本项目按 MIT 许可证"按原样"提供，不含任何
>   明示或默示担保，详见 [LICENSE](LICENSE)。
> - **界面会变。** 同花顺改版会让控件定位失效，可能导致填错值或点错按钮。
>   每次同花顺升级后，务必重跑 `tools/scan_panel.py` 核对。
> - **首次使用务必按这个顺序**：`dry_run=True` 空跑 → 用 100 股最小单
>   实测 → 确认委托表记录无误 → 再接策略。
> - **`submitted` 之外的任何状态都不代表下单成功。** 交易时段出现
>   `no_record` 或 `error` 必须立即人工核对委托列表。

---

## 30 秒上手

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 授权一次：系统设置 → 隐私与安全性 → 辅助功能 → 勾选你的终端（见下文）
.venv/bin/python tools/probe_ax.py          # 验证授权 + 同花顺可读
```

```python
from auto_trade import Config, Runner

runner = Runner(Config(dry_run=True))        # 先空跑，确认无误再改 False
report = runner.run([
    {"side": "buy",  "code": "600303", "price": 2.48, "amount": 100},
    {"side": "sell", "code": "002284", "price_mode": "limit_down", "amount": 100},
])
print(report.summary())
```

空跑会把值填进同花顺但不提交，你能直接在界面上核对。确认无误后
`Config(dry_run=False, auto_confirm=True)` 即为全自动。

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

## 授权设置

本项目通过 macOS 辅助功能 API 读写同花顺界面，**必须手动授权一次**，否则任何操作都会失败。

### 需要授权什么

只需要一项：**辅助功能（Accessibility）**。

授权对象是**运行脚本的那个程序**，不是同花顺本身，也不是 Python 解释器：

| 你怎么跑脚本 | 就授权谁 |
|---|---|
| iTerm2 里跑 | iTerm |
| 系统自带终端里跑 | 终端 / Terminal |
| VS Code 的终端里跑 | Visual Studio Code |
| PyCharm / GoLand 里跑 | 对应 IDE |
| launchd / cron 定时任务 | 同样是发起它的那个程序（通常也是终端） |

换一个终端跑，就要给那个终端再授权一次。

### 怎么授权

1. 打开 **系统设置 → 隐私与安全性 → 辅助功能**

   也可以直接在终端执行：
   ```bash
   open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
   ```

2. 点 **＋**，把你的终端 / IDE 加进列表（在「应用程序」里找），**把开关打开**

3. **重启那个终端 / IDE** —— 授权对已经运行的进程不生效

### 验证授权是否生效

```bash
.venv/bin/python tools/probe_ax.py
```

正常应输出：

```
AXAPIEnabled      = True
AXIsProcessTrusted = True

找到进程: 同花顺 (pid=xxxxx)
...
===== 合计可见控件节点: 1518 =====
```

### 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `AXIsProcessTrusted = False` | 没授权成功 | 确认加的是**运行脚本的终端/IDE**；授权后**重启**该程序 |
| 节点数 0，且屏幕上没有同花顺窗口 | Mac 上关闭窗口不退出进程，窗口被关掉了 | 点 Dock 图标，或菜单栏「窗口 → 主窗口」调出主界面 |
| 节点数 0，但窗口在屏幕上 | 窗口被最小化/完全遮挡，或仍在加载 | 把主界面显示到最前后重试 |
| 能读到行情但找不到下单面板 | 未登录交易账号 | 在同花顺里登录后重试 |

`probe_ax.py` 会自动区分这几种情况并给出对应提示（成功退出码 0，控件树为空退出码 4）。

### 不需要授权的项目

以下都**不需要**，看到相关提示不用理会：

- **自动化（Automation / Apple 事件）** —— 代码只用 `osascript ... to activate` 激活窗口，不发送 AppleEvent 指令，实测不触发此授权。
- **屏幕录制（Screen Recording）** —— 本方案不截屏、不做 OCR，纯 AX 读取。
- **输入监控（Input Monitoring）** —— 只发送按键（`CGEventPost`），不监听按键。
- **完全磁盘访问** —— 只读写项目自己的 `state/` 目录。
- **给同花顺本身授权辅助功能** —— 不需要。同花顺可能因自身功能出现在该列表里，与本项目无关（实测它的 `AXEnhancedUserInterface=False` 时控件树照样可读）。

### 前置条件

授权之外，运行时还需要：

- 同花顺已启动，**窗口未最小化、未隐藏**
- 已登录交易账号（未登录时读不到下单面板）
- 下单操作会把同花顺切到前台并抢占键盘焦点 —— **执行期间不要用这台机器做别的事**，你的输入会打断下单


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
| `error` | 点击后未出现二次确认框等异常 —— **状态不明，必须人工核对** |

`Result.ok` 为真仅包含 `filled_only` 与 `submitted`。批量执行后用 `report.needs_attention` 取出所有状态不确定的信号。

## 踩过的坑（都已在代码中处理）

1. **窗口失焦后 AX 树塌缩为 0 节点** → 每次操作前 activate + 重试取窗口。

2. **`AXValue` 直接写入曾是假阳性** —— 早期实测：控件显示会变、读回"一致"，
   但同花顺业务层毫无反应（股票名/涨跌停/可买全是 `- -`）。据此提交会下出
   内部状态为空的单。
   → 主路径用 `CGEvent` 发**真实键盘事件**。

   但后来在连续下单压测中发现另一面：页面重建后键盘路径会**静默失败**
   （退格清不掉旧值，新值也追加不进去），而此时 `AXValue` 直写反而能被
   同花顺收到并正常触发联动。
   → 现在是「键盘优先，写入未生效则回退 `AXValue`」，**两条路径都以
   `read_state()` 的联动字段作为最终判据** —— 显示对了不算数，
   同花顺查出名称和涨跌停才算数。

3. **填代码会被同花顺自动改写价格为现价** → 价格放在最后填，提交前重读校验（`price_drift`）。

4. **买入面板显示「可买:N股」，卖出面板显示「可用:N股」** → 两个词都要匹配，否则会误判为无持仓。

5. **点[确定买入]不会报单，只弹二次确认框** → 必须处理确认框，并核对框内代码/价格与意图一致才点确认。

6. **左侧导航栏按钮的 `AXTitle` 都读作"按钮"、`AXPosition.x` 报 -1** —— 只能靠「x<0 且 y 在 100~600」识别，按 y 排序后与视觉顺序一致。实测第 6 项是交易页。
   → `goto_trade_page()` 先试该位置，失败则逐个点击探测，用户停在任何页面都能回到下单页。

7. **委托查询会把界面带离下单页，下单框还会残留上一单的值** —— 残留值会让下一单误用旧数量/旧价格。
   → 每单之前调 `reset_to_order_page(side)`：取消遗留确认框 → 确保在交易页 → 切方向 → 点[重填]清空（清不干净则逐框退格兜底，仍失败就报错拒绝执行）。

8. **输入框靠标签 y 坐标配对，不能用子节点索引** —— 实测索引顺序是 `/41`(代码) `/37`(价格) `/42`(数量)，与视觉顺序不符。

## 批量下单：延后回查

每单下完都去委托表核对一次，要「开委托表 → 读表 → 切回下单页」，约
0.9 秒/单。连续下 3 单就是 2.7 秒的纯开销。

`verify=False` 跳过逐单回查，批次末统一核对：

```python
expect = []
for sig in signals:
    trader.place(sig["side"], sig["code"], sig["price"], sig["amount"],
                 verify=False)
    expect.append({"side": sig["side"], "code": sig["code"],
                   "amount": sig["amount"]})

for r in trader.verify_orders(expect):
    print(r["code"], "命中" if r["found"] else "缺失", r["order"])
```

`verify_orders()` 按 **代码 + 方向 + 数量** 三项匹配，并轮询到全部命中或
超时 5 秒 —— 委托表刷新有延迟（实测可达 2 秒），刚提交就读会误报缺失。

> 只按代码匹配会撞上同一标的的历史委托而误报成功。这个 bug 真实发生过：
> 买单返回 `submitted`，回查到的却是 20 分钟前的卖单。

## 界面异常的自愈

同花顺的界面状态会以几种方式干扰自动化，这些都已在代码中处理：

| 现象 | 后果 | 处理 |
|---|---|---|
| 券商拒单弹「警告」框（只有[确认]，正文是错误码） | 框滞留在屏幕上，**挡住后续所有操作** | `dismiss_error_dialog()` 识别并关闭，返回 `blocked` + 券商原文 |
| 顶部搜索框的下拉面板展开 | 遮挡 AX 遍历，读不到左侧导航（ESC 关不掉） | `dismiss_overlay()` 点窗口空白处 |
| 切换买卖方向 / 从委托表返回 | 控件被重建，旧引用写入**静默失败**（值进不去也不报错） | `type_text` 检测到写入未生效时回退 `AXValue` 直写（见「踩过的坑」第 2 条的完整来龙去脉） |
| 填完代码立刻读状态 | 联动是异步的（要回服务端查名称/涨跌停），必然读到空值 | 轮询等待，上限 `recognize_timeout`（默认 3 秒，实测 0.12 秒就绪） |

这些不是假想场景 —— 每一条都是连续下单压测时暴露出来的。

## 风控

`Config` 集中管理，默认值偏保守：

- `dry_run=True` / `auto_confirm=False` —— 默认不会真实下单
- `max_orders_per_day` / `max_amount_per_order` / `max_turnover_per_day` —— 日内计数**落盘**（`state/daily_state.json`），进程重启绕不过
- `stock_whitelist` —— 标的白名单
- `require_recognized` —— 同花顺未识别标的则拒绝提交
- `strict_params=True` —— **严格按入参执行**：代码、价格、数量、方向一律以策略给的为准，确认框里逐项核对，任何一项不符即放弃本单，绝不自行替换价格
- 数量须为 100 整数倍，且不超过面板显示的可买/可用量

## 风险提示

见文首的「使用前必读」。几条容易被忽略的补充：

- **风控是本地的，不是券商的。** `config.py` 里的 `max_amount_per_order`
  等阈值只拦本程序发出的单，拦不住你手动下的单，也不是券商风控。
- **`strict_params=True`（默认）才安全。** 它要求确认框里的代码/价格/数量
  与入参逐一相符，不符即放弃本单。关掉它意味着放弃最后一道核对。
- **券商拒单会弹警告框**（如"可用资金不足"）。程序会识别、关闭并返回
  `blocked` + 券商原文；若你自己扩展了下单流程，注意别让这类框滞留在
  屏幕上——它会挡住后续所有操作。

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

## 配置项

```python
Config(
    dry_run=True,              # True=只填值不提交(安全默认)
    auto_confirm=False,        # 是否自动点二次确认框
    strict_params=True,        # 确认框内容须与入参逐一相符, 否则放弃本单
    require_recognized=True,   # 必须确认同花顺已识别标的
    check_price_limit=True,    # 校验价格在涨跌停区间内
    allow_price_override=False,# 同花顺改写价格时是否容忍

    max_orders_per_day=50,
    max_amount_per_order=50000.0,
    max_turnover_per_day=200000.0,
    stock_whitelist=[],        # 非空时只允许这些代码

    recognize_timeout=3.0,     # 等同花顺联动出名称/涨跌停的上限
    key_delay=0.03,            # 按键间隔, 过小会丢键
    post_submit_wait=1.5,
)
```

> `max_amount_per_order` 默认 5 万是**示例值，不是推荐值**。首次使用请按
> 自己的实际规模调小。

## 目录

```
auto_trade/
  ax.py        AX 驱动:窗口/控件定位、CGEvent 输入、确认框、委托表
  trader.py    编排:风控 → 输入 → 联动校验 → 确认框核对 → 回查
  config.py    风控与行为配置
examples/      可直接运行的示例
tools/         控件树探测(改版后排查用)
```

## 许可证

[MIT](LICENSE)。按"原样"提供，不含任何担保 —— 包括但不限于适销性、
特定用途适用性。使用本软件造成的任何损失，作者不承担责任。
