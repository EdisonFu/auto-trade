"""同花顺 Mac 版自动下单 (Accessibility 驱动)。"""
from .ax import AXError, Panel, PanelState, ThsAX
from .config import Config
from .runner import BatchReport, Execution, Runner
from .signal import Signal, SignalError, dedupe, load_signals
from .trader import Result, Status, Trader

__all__ = [
    "ThsAX", "Panel", "PanelState", "AXError",
    "Config", "Trader", "Result", "Status",
    "Signal", "SignalError", "load_signals", "dedupe",
    "Runner", "BatchReport", "Execution",
]
