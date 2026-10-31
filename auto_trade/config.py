"""风控与行为配置。所有阈值集中此处。"""
from dataclasses import dataclass, field


@dataclass
class Config:
    # ---- 行为开关 ----
    dry_run: bool = True            # True=只填值不提交(安全默认)
    require_recognized: bool = True # 必须确认同花顺已识别股票才允许提交
    allow_price_override: bool = False  # 同花顺改写价格时是否容忍
    auto_confirm: bool = False      # 是否自动点二次确认框的[确认]
    check_price_limit: bool = True  # 下单前校验价格是否在涨跌停区间内
    strict_params: bool = True      # 严格模式:代码/价格/数量/方向一律按入参执行,
                                    # 任何一项与界面实际值不符即拒绝提交,绝不替换

    # ---- 风控 ----
    max_orders_per_day: int = 50
    max_amount_per_order: float = 50000.0
    max_turnover_per_day: float = 200000.0
    stock_whitelist: list[str] = field(default_factory=list)

    # ---- 时序 ----
    settle: float = 0.3      # 轮询失败时的兜底等待
    recognize_timeout: float = 3.0  # 等同花顺联动出名称/涨跌停的上限
    key_delay: float = 0.03  # 按键间隔;过小可能丢键,实测 0.03 稳定
    post_submit_wait: float = 1.5

    # ---- 落盘 ----
    state_file: str = "state/daily_state.json"
    log_file: str = "state/orders.log"
