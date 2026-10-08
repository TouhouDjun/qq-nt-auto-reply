# -*- coding: utf-8 -*-
"""
QQ 自动回复机器人 - 可视化控制台

功能:
  - 开关: 大按钮一键启动/停止机器人
  - 暂停: 暂停/恢复按钮(与 F12 等价), 状态实时显示
  - 设置: API Key(可显示/隐藏)、模型、目标聊天、防抖/冷却/防刷屏参数, 运行中修改即时生效
  - 日志: 彩色日志面板(收到消息/已发送/警告/错误)
  - 工具: 测试 API 连通性、检测 QQ 环境(窗口/控件定位)
  - 配置自动保存到 config.json

用法:
    py qq_gui.py
    py qq_auto_reply.py   (默认即启动本控制台)
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from qq_auto_reply import (
    AutoReplyBot, ChatAPIClient, QQController, DEFAULTS,
    CONFIG_PATH, load_config, save_config, rebuild_corpus,
)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class ControlPanel:
    LOG_COLORS = {
        "info": "#333333",
        "ok": "#1a7f37",
        "warn": "#b35900",
        "error": "#c62828",
        "state": "#1565c0",
    }
    # 显示名 <-> 内部值
    MODE_TO_DISPLAY = {"button": "点击发送按钮", "enter": "回车发送", "ctrl_enter": "Ctrl+回车发送"}
    DISPLAY_TO_MODE = {v: k for k, v in MODE_TO_DISPLAY.items()}
    KIND_TO_DISPLAY = {"auto": "自动判断", "group": "群聊(仅@我回复)", "private": "私聊(全部回复)"}
    DISPLAY_TO_KIND = {v: k for k, v in KIND_TO_DISPLAY.items()}

    def __init__(self):
        self.cfg = load_config()
        self.bot = None
        self.thread = None
        self.stop_event = threading.Event()
        self.msg_queue = queue.Queue()
        self.running = False

        self.root = tk.Tk()
        self.root.title("QQ 自动回复机器人 - 控制台")
        self.root.geometry("1060x800")
        self.root.minsize(960, 700)

        self._build_vars()
        self._build_ui()
        self._apply_cfg_to_vars()
        self._update_controls()
        self._log("state", "控制台已启动。填写设置后点击「▶ 启动」开始自动回复。")
        self._log("info", f"配置文件: {CONFIG_PATH}")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._poll)

    # ---------- 变量 ----------

    def _build_vars(self):
        self.vars = {}
        self.str_keys = ["api_key", "base_url", "model", "chat_name",
                         "self_nickname", "trigger"]
        for k in self.str_keys:
            self.vars[k] = tk.StringVar()
        self.num_keys = ["debounce", "poll_interval", "reply_cooldown",
                         "max_burst", "history_len", "auto_refresh_interval",
                         "few_shot_count", "memory_max", "temperature",
                         "knowledge_topk"]
        for k in self.num_keys:
            self.vars[k] = tk.StringVar()
        self.vars["send_mode"] = tk.StringVar()
        self.vars["chat_kind"] = tk.StringVar()
        self.bool_keys = ["skip_image_only", "dry_run", "tree_poke",
                          "scroll_to_bottom", "group_at_only",
                          "knowledge_retrieval", "web_search_enabled"]
        for k in self.bool_keys:
            self.vars[k] = tk.BooleanVar()
        self.show_key_var = tk.BooleanVar(value=False)

    def _apply_cfg_to_vars(self):
        for k, v in self.vars.items():
            if k in self.bool_keys or k in ("show_key_var", "chat_kind"):
                continue
            v.set(str(self.cfg.get(k, "")))
        for k in self.bool_keys:
            if k == "dry_run":
                self.vars[k].set(False)
            else:
                self.vars[k].set(bool(self.cfg.get(k, True)))
        self.vars["send_mode"].set(
            self.MODE_TO_DISPLAY.get(self.cfg.get("send_mode", "button"), "点击发送按钮"))
        self.vars["chat_kind"].set(
            self.KIND_TO_DISPLAY.get(self.cfg.get("chat_kind", "auto"), "自动判断"))
        self.prompt_widget.delete("1.0", "end")
        self.prompt_widget.insert("1.0", self.cfg.get("system_prompt", ""))
        self._update_key_show()

    def _collect_cfg(self):
        """从界面读取设置到 self.cfg (不落盘)。返回错误信息列表。"""
        errors = []
        for k in self.str_keys:
            self.cfg[k] = self.vars[k].get().strip()
        for k in self.num_keys:
            raw = self.vars[k].get().strip()
            try:
                v = float(raw)
                if v < 0:
                    raise ValueError
                self.cfg[k] = int(v) if k in ("max_burst", "history_len") else v
            except ValueError:
                errors.append(f"'{k}' 需要非负数字")
        self.cfg["send_mode"] = self.DISPLAY_TO_MODE.get(self.vars["send_mode"].get(), "button")
        self.cfg["chat_kind"] = self.DISPLAY_TO_KIND.get(self.vars["chat_kind"].get(), "auto")
        for k in self.bool_keys:
            self.cfg[k] = bool(self.vars[k].get())
        self.cfg["system_prompt"] = self.prompt_widget.get("1.0", "end").strip()
        return errors

    # ---------- UI ----------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)

        # ===== 顶部状态栏 =====
        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.lamp = tk.Label(top, text="●", font=("Segoe UI", 16), fg="#9e9e9e")
        self.lamp.pack(side="left")
        self.status_var = tk.StringVar(value="已停止")
        ttk.Label(top, textvariable=self.status_var, font=("Microsoft YaHei UI", 12, "bold")) \
            .pack(side="left", padx=(6, 20))
        self.pause_state_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.pause_state_var, foreground="#1565c0") \
            .pack(side="left", padx=10)
        ttk.Label(top, text="快捷键: F12 紧急暂停/恢复", foreground="#777") \
            .pack(side="right")

        # ===== 左侧: 设置 =====
        left = ttk.Frame(self.root, padding=8)
        left.grid(row=1, column=0, sticky="n")

        # --- API 设置 ---
        api = ttk.LabelFrame(left, text=" API 设置 ", padding=8)
        api.pack(fill="x", **pad)
        self._entry(api, "API Key", "api_key", 0, secret=True, key_toggle=True)
        self._entry(api, "接口地址(base_url)", "base_url", 1)
        ttk.Label(api, text="模型名").grid(row=2, column=0, sticky="w", pady=2)
        model_combo = ttk.Combobox(api, textvariable=self.vars["model"],
                                   values=(), width=23)  # 可编辑, 手填服务商提供的模型名
        model_combo.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(api, foreground="#777",
                  text="兼容 OpenAI 格式的任意服务").grid(
            row=2, column=2, sticky="w", padx=(4, 0))
        ttk.Button(api, text="🧪 测试 API 连接", command=self._test_api) \
            .grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- 聊天设置 ---
        chat = ttk.LabelFrame(left, text=" 聊天设置 ", padding=8)
        chat.pack(fill="x", **pad)
        self._entry(chat, "目标聊天(群名/好友昵称)", "chat_name", 0)
        self._entry(chat, "我的昵称(留空自动检测)", "self_nickname", 1)
        ttk.Label(chat, text="聊天类型").grid(row=2, column=0, sticky="w")
        kind_combo = ttk.Combobox(chat, textvariable=self.vars["chat_kind"],
                                  values=list(self.KIND_TO_DISPLAY.values()),
                                  state="readonly", width=22)
        kind_combo.grid(row=2, column=1, sticky="w")
        row3 = ttk.Frame(chat)
        row3.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.detect_btn = ttk.Button(row3, text="🔍 检测 QQ 环境", command=self._detect_qq)
        self.detect_btn.pack(side="left")
        ttk.Button(row3, text="🔄 立即刷新聊天", command=self._request_refresh) \
            .pack(side="left", padx=(8, 0))

        # --- 回复策略 ---
        policy = ttk.LabelFrame(left, text=" 回复策略 ", padding=8)
        policy.pack(fill="x", **pad)
        self._entry(policy, "防抖秒数", "debounce", 0, width=10)
        self._entry(policy, "轮询间隔秒", "poll_interval", 0, width=10, col=2)
        self._entry(policy, "回复冷却秒", "reply_cooldown", 1, width=10)
        self._entry(policy, "单波回复上限", "max_burst", 1, width=10, col=2)
        self._entry(policy, "上下文条数", "history_len", 2, width=10)
        ttk.Label(policy, text="发送方式").grid(row=2, column=2, sticky="w")
        mode_combo = ttk.Combobox(policy, textvariable=self.vars["send_mode"],
                                  values=list(self.MODE_TO_DISPLAY.values()),
                                  state="readonly", width=12)
        mode_combo.grid(row=2, column=3, sticky="w")
        self._entry(policy, "触发词(空=全部,分号分隔)", "trigger", 3)
        ttk.Checkbutton(policy, text="跳过纯图片消息", variable=self.vars["skip_image_only"]) \
            .grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(policy, text="群聊仅@我时回复(私聊全部回复)", variable=self.vars["group_at_only"]) \
            .grid(row=4, column=2, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(policy, text="Dry-Run(只读不发送, 测试用)", variable=self.vars["dry_run"]) \
            .grid(row=5, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(policy, text="强制刷新UI树(防冻结, 推荐)", variable=self.vars["tree_poke"]) \
            .grid(row=6, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(policy, text="抓取前滚到底(防冻结, 会动鼠标)", variable=self.vars["scroll_to_bottom"]) \
            .grid(row=7, column=0, columnspan=3, sticky="w", pady=2)
        self._entry(policy, "自动重进聊天间隔秒(0=关, 冻结兜底)", "auto_refresh_interval", 8, width=10)

        # --- 自我学习 ---
        learn = ttk.LabelFrame(left, text=" 🧠 自我学习(从群聊记录学人设) ", padding=8)
        learn.pack(fill="x", **pad)
        self._entry(learn, "学习样本条数/次", "few_shot_count", 0, width=10)
        self._entry(learn, "记忆上限条数", "memory_max", 0, width=10, col=2)
        self._entry(learn, "生成温度(0.8-1.0更活泼)", "temperature", 1, width=10)
        self._entry(learn, "知识检索注入对数", "knowledge_topk", 1, width=10, col=2)
        ttk.Checkbutton(learn, text="语料知识检索(从聊天记录搜梗/知识)",
                        variable=self.vars["knowledge_retrieval"]) \
            .grid(row=2, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Checkbutton(learn, text="后台联网搜索(提问自动上网查最新)",
                        variable=self.vars["web_search_enabled"]) \
            .grid(row=3, column=0, columnspan=4, sticky="w", pady=2)
        learn_row = ttk.Frame(learn)
        learn_row.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Button(learn_row, text="🧠 重建学习库(解析群聊表格)", command=self._rebuild_learn) \
            .pack(side="left")
        ttk.Label(learn, foreground="#777", wraplength=330, justify="left",
                  text="提问类消息(带?/吗/什么/版本/最新等)会先查群聊记录, "
                       "再抓 Bing 搜索结果注入上下文——群里没聊过的新版本新梗也能答。") \
            .grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # --- 系统提示词/附加要求 ---
        prompt = ttk.LabelFrame(left, text=" 附加要求(可选) ", padding=8)
        prompt.pack(fill="x", **pad)
        ttk.Label(prompt, foreground="#777", wraplength=330, justify="left",
                  text="人设已由群聊学习库自动生成(你是人、禁政治、禁AI话题)。"
                       "这里可追加要求, 留空即纯学习模式。") \
            .grid(row=0, column=0, columnspan=2, sticky="w")
        self.prompt_widget = tk.Text(prompt, width=46, height=3, wrap="word",
                                     font=("Microsoft YaHei UI", 9))
        self.prompt_widget.grid(row=1, column=0, columnspan=2, sticky="w")

        # --- 底部按钮 ---
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="💾 保存配置", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="恢复默认", command=self._reset_defaults).pack(side="left", padx=4)
        ttk.Label(btns, text="运行中修改设置 → 点保存即时生效", foreground="#777") \
            .pack(side="left", padx=8)

        # ===== 右侧: 控制与日志 =====
        right = ttk.Frame(self.root, padding=8)
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        ctrl = ttk.Frame(right)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.start_btn = tk.Button(ctrl, text="▶ 启动机器人", font=("Microsoft YaHei UI", 13, "bold"),
                                   bg="#2e7d32", fg="white", activebackground="#1b5e20",
                                   activeforeground="white", width=16, height=2,
                                   command=self._toggle_run)
        self.start_btn.pack(side="left", padx=6)
        self.pause_btn = ttk.Button(ctrl, text="⏸ 暂停", width=10, command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=6)

        hint = ttk.Frame(right)
        hint.grid(row=1, column=0, sticky="ew")
        ttk.Label(hint, text="运行期间请保持 QQ 窗口可见且在前台(请勿最小化), "
                             "机器人需要 QQ 窗口在前台才能读取消息。",
                  foreground="#777", wraplength=560, justify="left").pack(anchor="w")

        logfrm = ttk.LabelFrame(right, text=" 运行日志 ", padding=4)
        logfrm.grid(row=2, column=0, sticky="nsew")
        logfrm.rowconfigure(0, weight=1)
        logfrm.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logfrm, state="disabled", wrap="word",
                                font=("Consolas", 9), bg="#fafafa")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logfrm, orient="vertical", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)
        for tag, color in self.LOG_COLORS.items():
            self.log_text.tag_configure(tag, foreground=color)
        ttk.Button(logfrm, text="🗑 清空日志", command=self._clear_log) \
            .grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _entry(self, parent, label, key, row, width=26, col=0, secret=False, key_toggle=False):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", pady=2)
        ent = ttk.Entry(parent, textvariable=self.vars[key],
                        show="●" if secret else "", width=width)
        ent.grid(row=row, column=col + 1, sticky="w", pady=2)
        if key_toggle:
            ttk.Checkbutton(parent, text="显示", variable=self.show_key_var,
                            command=self._update_key_show) \
                .grid(row=row, column=col + 2, sticky="w", padx=(4, 0))
        if key == "api_key":
            self.api_entry = ent
        return ent

    def _update_key_show(self):
        self.api_entry.configure(show="" if self.show_key_var.get() else "●")

    # ---------- 日志 ----------

    def _log(self, level, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] ", "info")
        self.log_text.insert("end", msg + "\n", level)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 3000:  # 防止日志无限增长
            self.log_text.delete("1.0", "200.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _queue(self, level, msg):
        self.msg_queue.put((level, msg))

    def _poll(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                if item[0] == "__stopped__":
                    self.running = False
                    self._update_controls()
                    continue
                self._log(*item)
        except queue.Empty:
            pass
        if self.bot and self.running:
            self.pause_btn.configure(
                text="▶ 恢复" if self.bot.paused else "⏸ 暂停")
            self.pause_state_var.set("(已暂停)" if self.bot.paused else "")
        self.root.after(120, self._poll)

    def _update_controls(self):
        self.start_btn.configure(state="normal")
        if self.running:
            self.start_btn.configure(text="■ 停止机器人", bg="#c62828",
                                     activebackground="#8e1d1d")
            self.lamp.configure(fg="#2e7d32")
            self.status_var.set("运行中")
            self.pause_btn.configure(state="normal")
            self.detect_btn.configure(state="disabled")
        else:
            self.start_btn.configure(text="▶ 启动机器人", bg="#2e7d32",
                                     activebackground="#1b5e20")
            self.lamp.configure(fg="#9e9e9e")
            self.status_var.set("已停止")
            self.pause_btn.configure(state="disabled")
            self.pause_state_var.set("")
            self.detect_btn.configure(state="normal")

    # ---------- 动作 ----------

    def _toggle_run(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        errors = self._collect_cfg()
        if errors:
            messagebox.showerror("设置错误", "\n".join(errors))
            return
        if not self.cfg.get("chat_name"):
            messagebox.showerror("缺少设置", "请填写「目标聊天」后再启动")
            return
        if not self.cfg.get("api_key") and not self.vars["dry_run"].get():
            if not messagebox.askyesno(
                    "未填写 API Key",
                    "还没有填写 API Key, 启动后只会读取消息、不会回复。\n仍然启动吗?"):
                return
        self._save(quiet=True)
        self.stop_event = threading.Event()
        self.bot = AutoReplyBot(self.cfg, dry_run=bool(self.vars["dry_run"].get()),
                                log_cb=self._queue)
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        self.running = True
        self._update_controls()
        self._log("state", "已启动机器人线程")

    def _stop(self):
        self._log("warn", "正在停止 (等当前操作结束, 最多约半分钟) ...")
        self.stop_event.set()
        # 线程退出后会通过 __stopped__ 哨兵更新界面; 期间禁用按钮防重复点击
        self.start_btn.configure(state="disabled")

    def _worker(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            self.bot.run(stop_event=self.stop_event)
        except Exception as e:
            self._queue("error", f"机器人运行异常: {e}")
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
            self._queue("__stopped__", None)

    def _toggle_pause(self):
        if self.bot and self.running:
            self.bot.set_paused(not self.bot.paused)
            if not self.bot.paused:
                # 恢复运行后重新建立基线, 暂停期间的消息不回复(避免刷屏)
                self.bot.seen_ids.clear()
                self.bot.baselined = False
                self.bot.pending.clear()

    def _request_refresh(self):
        if self.bot and self.running:
            self.bot.request_refresh = True
            self._log("info", "已请求强制刷新消息列表(切换会话后切回)")
        else:
            messagebox.showinfo("提示", "请先启动机器人, 再使用立即刷新")

    def _save(self, quiet=False):
        errors = self._collect_cfg()
        if errors:
            messagebox.showerror("设置错误", "\n".join(errors))
            return
        try:
            save_config(self.cfg)
        except OSError as e:
            messagebox.showerror("保存失败", str(e))
            return
        # 运行中即时生效
        if self.bot and self.running:
            chat_changed = self.vars["chat_name"].get().strip() != getattr(self, "_running_chat", None)
            self.bot._build_client()
            self.bot.resize_context(self.cfg["history_len"])
            if chat_changed:
                self.bot.seen_ids.clear()
                self.bot.baselined = False
                self.bot.pending.clear()
                self.bot.chat_kind = None  # 重新判断群聊/私聊
                self._log("info", f"目标聊天已改为 '{self.cfg['chat_name']}', 重新建立基线")
            self._log("ok", "设置已保存并即时生效")
        else:
            self._log("ok", "设置已保存到 config.json")
        self._running_chat = self.cfg["chat_name"]

    def _reset_defaults(self):
        if messagebox.askyesno("恢复默认", "确认恢复全部默认设置? (API Key 将被清空)"):
            self.cfg = dict(DEFAULTS)
            self._apply_cfg_to_vars()
            self._log("warn", "已恢复默认设置(未落盘), 点「保存配置」后生效")

    def _test_api(self):
        key = self.vars["api_key"].get().strip()
        base = self.vars["base_url"].get().strip()
        model = self.vars["model"].get().strip()
        if not key:
            messagebox.showwarning("缺少 API Key", "请先填写 API Key")
            return
        if not base:
            messagebox.showwarning("缺少接口地址", "请先填写接口地址(base_url)")
            return
        self._log("info", f"正在测试接口连接: {base} ...")

        def worker():
            try:
                ok, msg = ChatAPIClient(key, base, model).test_connection()
                self._queue("ok" if ok else "error", f"接口测试: {msg}")
            except Exception as e:
                self._queue("error", f"接口测试异常: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _rebuild_learn(self):
        """后台重建学习库(解析群聊表格), 完成后热重载。"""
        self._log("info", "开始重建学习库(解析 4 个群聊表格, 约1-2分钟) ...")

        def worker():
            ok = rebuild_corpus()
            if ok:
                if self.bot and self.bot.learner:
                    self.bot.learner.reload()
                self._queue("ok", "学习库重建完成并已热重载")
            else:
                self._queue("error", "学习库重建失败, 详见 build_learn.log")

        threading.Thread(target=worker, daemon=True).start()

    def _detect_qq(self):
        if self.running:
            messagebox.showinfo("提示", "机器人运行中无法检测, 请先停止")
            return
        self._log("info", "正在检测 QQ 环境(会把 QQ 窗口置前) ...")

        def worker():
            import comtypes
            import uiautomation as auto
            comtypes.CoInitialize()
            try:
                qq = QQController(self.cfg)
                found = qq.find_qq_window()
                if not found:
                    self._queue("error", "未找到 QQ 窗口, 请先登录 QQ")
                    return
                hwnd, title = found
                self._queue("info", f"找到 QQ 窗口: HWND={hwnd} 标题={title!r}")
                qq.hwnd = hwnd
                if not qq.force_foreground():
                    self._queue("warn", "QQ 窗口置前失败(可能被前台锁拦截)")
                win = auto.ControlFromHandle(hwnd)
                qq.win = win
                for name, ok in (
                    ("消息列表", win.WindowControl(Name="消息列表").Exists(1, 0.1)),
                    ("发送按钮", win.ButtonControl(Name="发送").Exists(1, 0.1)),
                    ("会话列表", win.WindowControl(Name="会话列表").Exists(1, 0.1)),
                ):
                    self._queue("ok" if ok else "error", f"控件 {name}: {'OK' if ok else '未找到'}")
                nick = qq.detect_self_nickname()
                if nick:
                    self._queue("ok", f"检测到自己的昵称: {nick}")
            except Exception as e:
                self._queue("error", f"检测异常: {e}")
            finally:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        try:
            if self.running:
                self.stop_event.set()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    panel = ControlPanel()
    panel.run()


if __name__ == "__main__":
    main()
