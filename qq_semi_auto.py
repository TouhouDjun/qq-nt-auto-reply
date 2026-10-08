# -*- coding: utf-8 -*-
"""
QQ 半自动回复机器人 (键盘钩子模式) - UI 轮询冻结时的备选方案

设计思路:
    不再持续轮询 UI 树(避免 QQ 控件树冻结问题), 改为"你按一下, 我干一次":
    用全局低级键盘钩子 (WH_KEYBOARD_LL) 监听热键, 触发时一次性读取并回复。

热键:
    Ctrl+Shift+R   读取当前聊天最后一条消息 -> 调用大模型 -> 自动填入并发送
                   (剪贴板模式 --clipboard: 读取你刚复制的文本作为消息, 完全不做 UIA 读取)
    Ctrl+Shift+P   暂停/恢复 (防止误触发)

用法:
    py qq_semi_auto.py                    # UIA 一次性读取模式
    py qq_semi_auto.py --clipboard        # 剪贴板模式(不读 UI, 最稳)
    py qq_semi_auto.py --dry-run          # 只打印将要回复的内容, 不发送

说明:
    每条消息只回复一次(按消息ID/文本哈希去重)。Ctrl+C 退出。
"""

import argparse
import ctypes
import re
import sys
import threading
import time
from ctypes import wintypes

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass

import pyperclip

from qq_auto_reply import (
    AutoReplyBot, ChatAPIClient, DialogueLearner, QQController, WebSearcher,
    load_config, CONFIG_PATH, strip_name_prefix,
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_CONTROL, VK_SHIFT, VK_R, VK_P = 0x11, 0x10, 0x52, 0x50


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


def _key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyWatcher:
    """全局低级键盘钩子: 监听组合键, 触发时置 threading.Event。"""

    def __init__(self, combos, on_fire):
        # combos: [(修饰键vks tuple, 触发键vk, 标识id), ...]
        self.combos = combos
        self.on_fire = on_fire
        self.hook = None
        self._stop = threading.Event()
        self._last_fire = {}

    def _cb(self, nCode, wParam, lParam):
        if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            now = time.time()
            for mods, trig, cid in self.combos:
                if kb.vkCode == trig and all(_key_down(m) for m in mods):
                    if now - self._last_fire.get(cid, 0) > 1.0:  # 防抖 1 秒
                        self._last_fire[cid] = now
                        self.on_fire(cid)
        return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)

    def _pump(self):
        msg = wintypes.MSG()
        while not self._stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)

    def start(self):
        CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                                     wintypes.WPARAM, wintypes.LPARAM)
        self._cb_ref = CMPFUNC(self._cb)  # 必须保持引用防止被GC
        self.hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._cb_ref, kernel32.GetModuleHandleW(None), 0)
        if not self.hook:
            raise OSError("键盘钩子注册失败")
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None


def log(msg, level="info"):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="QQ 半自动回复(键盘钩子模式)")
    ap.add_argument("--clipboard", action="store_true",
                    help="剪贴板模式: 热键触发时读取剪贴板内容作为消息(不做UIA读取)")
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不调用API不发送")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("chat_name"):
        print("未配置目标聊天, 请先运行 py qq_auto_reply.py 打开控制台填写设置")
        return
    if not cfg.get("api_key") and not args.dry_run:
        print("未配置 API Key, 请先打开控制台填写")
        return

    qq = QQController(cfg)
    client = ChatAPIClient(cfg["api_key"], cfg["base_url"], cfg["model"],
                           cfg["system_prompt"]) \
        if (cfg.get("api_key") and cfg.get("base_url")) else None
    # 自我学习器: 学群聊语料 + 积累自己的对话记忆
    learner = None
    if cfg.get("learn_enabled", True):
        try:
            learner = DialogueLearner(cfg)
        except Exception as e:
            log(f"自我学习器初始化失败: {e}", "warn")

    state = {"paused": False, "replied_sigs": set(), "busy": False}

    def on_fire(cid):
        if cid == "pause":
            state["paused"] = not state["paused"]
            log("已暂停(热键 Ctrl+Shift+P), 再按恢复" if state["paused"] else "已恢复",
                "state")
            return
        if state["paused"] or state["busy"]:
            return
        state["busy"] = True
        try:
            if args.clipboard:
                text = (pyperclip.paste() or "").strip()
                sender = "剪贴板"
            else:
                # 一次性 UIA 读取: 先叫醒树再读最后一条非自己消息
                if not qq.hwnd or not qq.win:
                    if not qq.connect():
                        log("找不到 QQ 窗口", "error")
                        return
                qq.force_foreground()
                qq.poke_tree()
                msgs = qq.read_messages() or []
                last = None
                for m in reversed(msgs):
                    if not m["is_self"] and m["text"]:
                        last = m
                        break
                if last is None:
                    log("当前聊天没有可回复的消息", "warn")
                    return
                sig = AutoReplyBot._sig(last)
                if sig in state["replied_sigs"]:
                    log(f"这条消息已回复过, 跳过: {last['text'][:40]}", "warn")
                    return
                state["replied_sigs"].add(sig)
                sender, text = last["sender"], last["text"]

            if not text:
                log("没有内容可回复", "warn")
                return
            log(f"触发回复 [{sender}]: {text[:60]}")
            if args.dry_run:
                log(f"[dry-run] 将生成回复(不发送)")
                return
            user_content = f"{sender}: {text}"
            knowledge_parts = []
            if learner and learner.enabled:
                if cfg.get("knowledge_retrieval", True):
                    related = learner.search_related(text)
                    if related:
                        lines = ["群里聊过这个话题的记录(参考他们的说法和梗, 别照抄):"]
                        for a, b in related:
                            lines.append(f"{a} => {b}")
                        knowledge_parts.append("\n".join(lines))
                if cfg.get("web_search_enabled", True) and WebSearcher.is_question(text):
                    searcher = WebSearcher(engine=cfg.get("search_engine", "auto"))
                    queries = WebSearcher.build_queries(text)
                    results = searcher.search_multi(queries)
                    if results:
                        knowledge_parts.insert(0, searcher.build_knowledge(queries[0], results))
                        log(f"联网搜索命中 {len(results)} 条", "info")
                system = learner.build_system_prompt("\n\n".join(knowledge_parts))
                messages = learner.sample_few_shot() + \
                    [{"role": "user", "content": user_content}]
            else:
                system = cfg.get("system_prompt") or None
                messages = [{"role": "user", "content": user_content}]
            reply = client.chat(messages, system_prompt=system,
                                temperature=float(cfg.get("temperature", 0.9)))
            if not reply:
                log("API 返回空内容", "error")
                return
            reply = strip_name_prefix(reply)  # 去掉误带的"昵称: "前缀
            qq.send_text(reply)
            log(f"已发送: {reply[:60]}", "ok")
            if learner and learner.enabled:
                learner.remember(user_content, reply)  # 自我学习: 写入长期记忆
        except Exception as e:
            log(f"回复失败: {e}", "error")
        finally:
            state["busy"] = False

    fire_event = threading.Event()
    fire_id = [None]

    def _on_fire_wrap(cid):
        fire_id[0] = cid
        fire_event.set()

    watcher = HotkeyWatcher(
        [((VK_CONTROL, VK_SHIFT), VK_R, "reply"),
         ((VK_CONTROL, VK_SHIFT), VK_P, "pause")],
        _on_fire_wrap)
    watcher.start()

    mode = "剪贴板模式" if args.clipboard else "UIA 一次性读取模式"
    log(f"半自动机器人已启动({mode}), 配置文件: {CONFIG_PATH}")
    log("热键: Ctrl+Shift+R 回复最后一条消息 | Ctrl+Shift+P 暂停/恢复 | Ctrl+C 退出")

    try:
        while True:
            fire_event.wait(0.2)
            if fire_event.is_set():
                fire_event.clear()
                on_fire(fire_id[0])
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        log("已退出")


if __name__ == "__main__":
    main()
