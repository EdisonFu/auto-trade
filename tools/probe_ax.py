#!/usr/bin/env python3
"""探测同花顺 Mac 版的辅助功能(Accessibility)控件树。

用途: 判断能否通过 AX API 读取/填写下单窗口的输入框与按钮。
运行前需在 系统设置 > 隐私与安全性 > 辅助功能 中授权运行它的终端程序。
"""
import subprocess
import sys
import time

from ApplicationServices import (
    AXAPIEnabled,
    AXIsProcessTrusted,
    AXUIElementCopyAttributeNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    kAXChildrenAttribute,
    kAXDescriptionAttribute,
    kAXRoleAttribute,
    kAXTitleAttribute,
    kAXValueAttribute,
)
from Cocoa import NSWorkspace

BUNDLE_ID = "cn.com.10jqka.macstockPro"
MAX_DEPTH = 14


def _onscreen_windows() -> int:
    """数一下同花顺在屏窗口。窗口被关掉时 AX 树必然为空。"""
    try:
        import Quartz
    except ImportError:
        return -1
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    return sum(1 for w in wins if "同花顺" in str(w.get("kCGWindowOwnerName", "")))


def find_app_pid(bundle_id):
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == bundle_id:
            return app.processIdentifier(), app.localizedName()
    return None, None


def attr(element, name):
    err, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def describe(element):
    role = attr(element, kAXRoleAttribute) or "?"
    bits = [str(role)]
    for key in (kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute):
        val = attr(element, key)
        if val is None:
            continue
        text = str(val).strip().replace("\n", " ")
        if text and len(text) < 60:
            bits.append(f"{key[2:]}={text!r}")
    return "  ".join(bits)


def walk(element, depth=0, counter=None):
    if counter is None:
        counter = {"n": 0}
    counter["n"] += 1
    print("  " * depth + describe(element))
    if depth >= MAX_DEPTH:
        return counter
    children = attr(element, kAXChildrenAttribute) or []
    for child in children:
        walk(child, depth + 1, counter)
    return counter


def main():
    print(f"AXAPIEnabled      = {AXAPIEnabled()}")
    trusted = AXIsProcessTrusted()
    print(f"AXIsProcessTrusted = {trusted}")
    if not trusted:
        print(
            "\n[!] 当前进程没有辅助功能权限。\n"
            "    请到 系统设置 > 隐私与安全性 > 辅助功能，\n"
            "    为运行本脚本的终端(如 iTerm/Terminal)打开开关后重试。"
        )
        return 2

    pid, name = find_app_pid(BUNDLE_ID)
    if pid is None:
        print(f"\n[!] 没找到正在运行的 {BUNDLE_ID}，请先启动同花顺。")
        return 3
    print(f"\n找到进程: {name} (pid={pid})")

    # 同花顺窗口失焦时 AX 树会塌缩为 0 节点，先激活再读
    subprocess.run(
        ["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'],
        capture_output=True,
    )
    time.sleep(1.5)
    print()

    app = AXUIElementCreateApplication(pid)
    err, names = AXUIElementCopyAttributeNames(app, None)
    print(f"应用级可读属性: {list(names) if err == 0 else '读取失败'}\n")

    windows = attr(app, "AXWindows") or []
    for _ in range(8):
        if windows and (attr(windows[0], kAXChildrenAttribute) or []):
            break
        time.sleep(0.5)
        windows = attr(app, "AXWindows") or []
    print(f"窗口数量: {len(windows)}\n")
    total = 0
    for i, win in enumerate(windows):
        title = attr(win, kAXTitleAttribute)
        print(f"===== 窗口 {i}: {title!r} =====")
        counter = walk(win)
        total += counter["n"]
        print()

    print(f"===== 合计可见控件节点: {total} =====")
    if total <= max(len(windows) * 2, 2):
        onscreen = _onscreen_windows()
        print("\n[!] 控件树是空的。这几乎不是权限问题 —— 请按以下顺序排查：")
        if onscreen == 0:
            print(
                "    >>> 同花顺进程在运行，但屏幕上没有任何窗口。"
                "\n        Mac 上关闭窗口不会退出应用，窗口已被关掉。"
                "\n        解决：点 Dock 里的同花顺图标，或菜单栏「窗口 → 主窗口」"
                "把主界面调出来，然后重新运行本脚本。"
            )
        else:
            print(
                f"    屏幕上有 {onscreen} 个窗口，但 AX 读不到内容："
                "\n      1. 窗口可能被最小化或被其他窗口完全遮挡；"
                "\n      2. 同花顺可能还停在启动/加载阶段。"
                "\n    把主界面显示到最前后重试。"
            )
        print("    正常应能读到 1000+ 个节点。")
        return 4
    print(
        "\n[OK] 控件树可读，AX 自动化可用。"
        "\n     若要查看下单面板控件，请先在同花顺中登录并打开买入/卖出界面，"
        "\n     然后运行: python tools/scan_panel.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
