#!/usr/bin/env python3
"""探测同花顺 Mac 版的辅助功能(Accessibility)控件树。

用途: 判断能否通过 AX API 读取/填写下单窗口的输入框与按钮。
运行前需在 系统设置 > 隐私与安全性 > 辅助功能 中授权运行它的终端程序。
"""
import sys

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
    print(f"\n找到进程: {name} (pid={pid})\n")

    app = AXUIElementCreateApplication(pid)
    err, names = AXUIElementCopyAttributeNames(app, None)
    print(f"应用级可读属性: {list(names) if err == 0 else '读取失败'}\n")

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
    if total <= len(windows) * 2:
        print(
            "\n[!] 控件树几乎是空的 —— 说明同花顺的界面是自绘(非原生NSView)，\n"
            "    辅助功能 API 读不到内部控件，AX 自动化路线走不通。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
