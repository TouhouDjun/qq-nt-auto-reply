# -*- coding: utf-8 -*-
"""
QQ NT UIA 控件检查工具

用途: 当 QQ 更新导致控件定位失效时, 用本工具 dump 当前 QQ 窗口的
UIA 控件树, 找出新的输入框/发送按钮/消息列表定位方式。

用法:
    py qq_inspect.py              # 列出 QQ 窗口并 dump 主窗口控件树
    py qq_inspect.py --dump       # 同时把完整树保存到 _qq_tree.txt
    py qq_inspect.py --check      # 只检查关键控件(消息列表/输入框/发送按钮)是否可定位
"""
import argparse
import ctypes
import re
import subprocess
import sys
import time

import uiautomation as auto

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
user32 = ctypes.windll.user32
auto.SetGlobalSearchTimeout(2)


def qq_pids():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id) -join ','"],
            capture_output=True, text=True, timeout=20)
        return {int(x) for x in out.stdout.strip().split(",") if x.strip().isdigit()}
    except Exception:
        return set()


def list_qq_windows():
    """列出 QQ.exe 的所有可见 Chrome_WidgetWin_1 顶层窗口。"""
    pids = qq_pids()
    wins = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "Chrome_WidgetWin_1":
            wins.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return wins


def force_foreground(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return user32.GetForegroundWindow() == hwnd


def dump_tree(win, max_depth=14, out_lines=None, depth=0):
    out_lines = out_lines if out_lines is not None else []
    if depth > max_depth:
        return out_lines
    try:
        name = win.Name
    except Exception:
        name = "<err>"
    try:
        line = ("  " * depth + f"{win.ControlTypeName} | Name={str(name)[:70]!r} "
                f"| Id={str(win.AutomationId)[:40]!r} | Class={str(win.ClassName)[:30]!r}")
        out_lines.append(line)
    except Exception:
        return out_lines
    try:
        children = win.GetChildren()
    except Exception:
        children = []
    for ch in children[:80]:
        dump_tree(ch, max_depth, out_lines, depth + 1)
    return out_lines


def check_key_controls(win):
    print("--- 关键控件检查 ---")
    # 消息列表
    ml = win.WindowControl(Name="消息列表")
    print(f"消息列表 WindowControl(Name='消息列表'): {ml.Exists(1, 0.1)}")
    if ml.Exists(1, 0.1):
        items = [g for g in _desc(ml, "GroupControl") if re.fullmatch(r"\d+", g.AutomationId or "")]
        print(f"  消息条目数(可见): {len(items)}")
        if items:
            print(f"  最后一条 id={items[-1].AutomationId}")

    # 发送按钮
    btn = win.ButtonControl(Name="发送")
    ok = btn.Exists(1, 0.1)
    print(f"发送按钮 ButtonControl(Name='发送'): {ok}")
    if ok:
        print(f"  rect={btn.BoundingRectangle} enabled={btn.IsEnabled}")

    # 输入框 (CustomControl 富文本)
    edit = win.CustomControl(searchDepth=12)
    print(f"输入框 CustomControl(searchDepth=12): {edit.Exists(1, 0.1)}")
    if edit.Exists(1, 0.1):
        print(f"  Name={edit.Name!r} rect={edit.BoundingRectangle}")

    # 会话列表
    sess = win.WindowControl(Name="会话列表")
    print(f"会话列表 WindowControl(Name='会话列表'): {sess.Exists(1, 0.1)}")

    # 自己的昵称
    nick_btn = win.ButtonControl(RegexName=r"(.+)的头像$")
    if nick_btn.Exists(1, 0.1):
        print(f"自己的昵称(头像按钮): {nick_btn.Name[:-3]}")


def _desc(root, ctype, max_depth=10, max_nodes=4000):
    out = []
    stack = [(root, 0)]
    while stack and len(out) < max_nodes:
        c, d = stack.pop()
        if d > max_depth:
            continue
        if c.ControlTypeName == ctype:
            out.append(c)
        try:
            stack.extend((ch, d + 1) for ch in reversed(c.GetChildren()))
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="保存完整控件树到 _qq_tree.txt")
    ap.add_argument("--check", action="store_true", help="只检查关键控件")
    args = ap.parse_args()

    wins = list_qq_windows()
    if not wins:
        print("未找到 QQ 窗口, 请先登录并打开 QQ 主窗口")
        return
    print("QQ 窗口:")
    for h, t in wins:
        print(f"  HWND={h} title={t!r}")

    # 优先主窗口, 否则第一个
    hwnd, title = next(((h, t) for h, t in wins if t == "QQ"), wins[0])
    print(f"\n使用窗口: HWND={hwnd} title={title!r}")
    if not force_foreground(hwnd):
        print("警告: 无法置前 QQ 窗口, UIA 树可能为空(QQ NT 只在窗口前台时构建无障碍树)")
        time.sleep(1)

    win = auto.ControlFromHandle(hwnd)
    print(f"顶层控件: {win.ControlTypeName} Name={win.Name!r}")

    if args.check:
        check_key_controls(win)
        return

    lines = dump_tree(win)
    text = "\n".join(lines)
    if args.dump:
        with open("_qq_tree.txt", "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已保存 {len(lines)} 行到 _qq_tree.txt")
    else:
        print(text[:8000])
        if len(text) > 8000:
            print(f"\n... (共 {len(lines)} 行, 用 --dump 保存完整树)")


if __name__ == "__main__":
    main()
