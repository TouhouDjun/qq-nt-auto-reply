# -*- coding: utf-8 -*-
"""
QQ NT 自动回复机器人 - 核心库

包含:
  - ChatAPIClient  : 大模型 API 客户端(兼容 OpenAI chat/completions 格式 + 连通性测试)
  - QQController   : QQ NT 窗口查找 / 聊天切换 / 消息读取 / 文本发送 / 强制刷新
  - DialogueLearner: 群聊自我学习(语料 + 风格画像 + few-shot 抽样 + 知识检索 + 长期记忆)
  - WebSearcher    : 后台联网搜索(口语查询改写 + 多引擎抓取 + 缓存/冷却)
  - AutoReplyBot   : 主循环(强制轮询、防抖、F12 暂停、上下文、防刷屏)

用法:
    py qq_auto_reply.py              # 默认启动可视化控制台(qq_gui.py)
    py qq_auto_reply.py --console    # 纯终端模式(读取 config.json)
    py qq_auto_reply.py --console --dry-run   # 终端模式只读测试

★★ 强制轮询设计(修复 QQ 控件树冻结) ★★
    QQ NT (Electron) 的无障碍树只在窗口前台时构建, 而且长时间没有辅助技术(AT)
    信号时会冻结: 新消息到达后控件树不更新, 导致"卡在等待新消息"。
    本程序每轮都执行:
      1) 从窗口句柄重建根元素(ControlFromHandle), 绝不复用缓存的旧树;
      2) 主动 poke: SendMessageTimeout(WM_GETOBJECT, OBJID_CLIENT) 重新声明 AT 客户端
         + UIA SetFocus() 强制窗口刷新;
      3) 可选 scroll_to_bottom: 鼠标滚轮把消息列表滚到底, 强制虚拟列表重绘;
      4) 兜底 auto_refresh_interval: 超过 N 秒没读到新消息, 自动切换到另一个会话
         再切回目标聊天(等价于手工"退出再点进去"), 并靠消息签名比对不丢消息。
    消息去重: 每条消息取签名 = "id:消息ID"(QQ 每条消息的 AutomationId 是唯一ID),
    ID 缺失时退化为 "text:哈希(发送者+正文)"。签名已见过就跳过。
"""

import argparse
import ctypes
import html as html_mod
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import threading
from collections import deque
from ctypes import wintypes

# Windows 控制台默认 GBK, 强制 UTF-8 输出避免乱码
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass

import pyperclip
import requests
import uiautomation as auto

from learn_common import is_sensitive

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

DEFAULTS = {
    "api_key": "",
    "base_url": "",             # 大模型接口地址(兼容 OpenAI 格式), 由使用者自行填写
    "model": "",                # 模型名, 由使用者自行填写
    "chat_name": "",            # 目标聊天窗口名(群名或好友昵称)
    "self_nickname": "",        # 自己的昵称, 留空则自动检测
    "debounce": 2.0,            # 防抖秒数: 新消息稳定这么久才回复
    "poll_interval": 0.5,       # 轮询间隔秒
    "reply_cooldown": 3.0,      # 两条回复之间的最小间隔秒
    "max_burst": 3,             # 一波最多回复条数(防刷屏), 超出丢弃
    "send_mode": "button",      # button | enter | ctrl_enter
    "skip_image_only": True,    # 纯图片消息不回复
    "history_len": 10,          # 带给 API 的上下文消息条数
    "trigger": "",              # 触发词, 分号分隔; 空=全部消息都回复
    "chat_kind": "auto",        # 聊天类型: auto自动判断 | group群聊 | private私聊
    "group_at_only": True,      # 群聊里只回复 @我 的消息; 私聊不受影响全部回复
    "system_prompt": "",        # 附加要求(可选)。人设由群聊学习库自动生成, 清空即纯学习
    # ---- 身份设定(不硬编码个人信息, 全部走配置) ----
    "boss_name": "",            # 老板/上司昵称(留空=只当普通群友)
    "role_name": "",            # 你的角色名/代号(可选)
    "forget_roles": "",         # 要抛弃的旧人设名单, 分号分隔(可选)
    # ---- 自我学习(从群聊记录表格学人设) ----
    "learn_enabled": True,      # 启用群聊自我学习
    "learn_corpus": "learn_corpus.json",  # 语料库文件(build_learn.py 生成)
    "few_shot_count": 6,        # 每次请求抽样的真实对话示例条数(教语气, 少量防带偏事实)
    "memory_max": 300,          # 自我对话记忆上限(机器人自己的聊天会积累为记忆)
    "temperature": 0.9,         # 生成温度: 学人说话建议 0.8-1.0, 更活泼
    "knowledge_retrieval": True,  # 语料知识检索: 从聊天记录搜相关讨论注入(解决"梗不知道")
    "knowledge_topk": 6,        # 每次检索注入的相关对话对数
    "web_search_enabled": True,  # 后台联网搜索: 提问类消息自动上网查最新信息(无需key)
    "search_engine": "auto",     # 搜索引擎: auto(搜狗->Bing回退) | sogou | bing
    # ---- 强制轮询/防冻结 ----
    "tree_poke": True,          # 每轮主动 WM_GETOBJECT + SetFocus 刷新 UI 树
    "poke_interval": 1.0,       # poke 的最小间隔秒
    "scroll_to_bottom": False,  # 抓取前把消息列表滚到底(强制虚拟列表重绘, 会短暂动鼠标)
    "scroll_interval": 2.0,     # 滚动的最小间隔秒
    "auto_refresh_interval": 60,  # 超过 N 秒没检测到新消息 -> 自动切换会话重进(0=关闭)
}

# 时间戳文本正则: 消息列表里每条消息的第一个 TextControl 是时间
TS_RE = re.compile(
    r"^(\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}|\d{1,2}:\d{2}"
    r"|星期[一二三四五六日天] \d{1,2}:\d{2}|昨天 \d{1,2}:\d{2}|前天 \d{1,2}:\d{2})$"
)

# 回复开头误带的名字前缀: "某人: xxx" / "某人：" (模型模仿消息格式产生的)
NAME_PREFIX_RE = re.compile(r"^[\w\u4e00-\u9fff@·.·\-_\[\]]{1,24}\s*[:：]\s*")

def strip_name_prefix(text):
    """去掉回复开头误带的 '昵称: ' 前缀。"""
    t = (text or "").strip()
    m = NAME_PREFIX_RE.match(t)
    if m:
        t = t[m.end():].strip()
    return t

VK_F12 = 0x7B
WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC
user32 = ctypes.windll.user32

auto.SetGlobalSearchTimeout(2)


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError) as e:
            print(f"读取配置失败: {e}", flush=True)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


# ---------------------------------------------------------------------------
# 大模型 API 客户端(兼容 OpenAI chat/completions 格式, 服务商与模型均自行配置)
# ---------------------------------------------------------------------------

class ChatAPIClient:
    """通用大模型 chat/completions 客户端(兼容 OpenAI 格式)。"""

    def __init__(self, api_key, base_url, model, system_prompt="", timeout=60):
        if not (base_url or "").strip():
            raise ValueError("未配置接口地址: 请在 config.json 里填写 base_url")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout = timeout
        self.endpoint = self.base_url + "/chat/completions"

    def chat(self, messages, system_prompt=None, temperature=None, max_tokens=None):
        """
        messages: [{'role': ..., 'content': ...}] -> 回复文本
        system_prompt: 覆盖默认系统提示; temperature: 覆盖默认温度(默认0.7)
        max_tokens: None 时不限制(思考型模型 reasoning 会消耗token, 限制过小会截空正文)
        """
        sysp = system_prompt if system_prompt is not None else self.system_prompt
        temp = temperature if temperature is not None else 0.7
        full = []
        if sysp:
            full.append({"role": "system", "content": sysp})
        full.extend(messages)
        payload = {
            "model": self.model,
            "messages": full,
            "temperature": temp,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(2):  # 失败重试一次
            try:
                resp = requests.post(self.endpoint, json=payload,
                                     headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code in (401, 403, 404):  # 配置类错误, 不重试
                    break
            except requests.exceptions.RequestException as e:
                last_err = str(e)
            time.sleep(2)
        raise RuntimeError(f"模型接口调用失败: {last_err}")

    def test_connection(self):
        """测试 API Key 与地址连通性。返回 (ok: bool, message: str)。"""
        try:
            resp = requests.get(
                self.base_url + "/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=20)
        except requests.exceptions.RequestException as e:
            return False, f"无法连接 {self.base_url}: {e}"
        if resp.status_code in (401, 403):
            return False, f"API Key 无效 (HTTP {resp.status_code})"
        if resp.status_code != 200:
            return False, f"接口异常 HTTP {resp.status_code}: {resp.text[:150]}"
        try:
            models = [m.get("id", "") for m in resp.json().get("data", [])]
        except ValueError:
            models = []
        if models:
            if self.model and self.model not in models:
                return False, (f"连接成功, 但模型 '{self.model}' 不在可用列表: "
                               f"{', '.join(models[:8])} ...")
            return True, f"连接成功, 可用模型 {len(models)} 个: {', '.join(models[:5])} ..."
        return True, "连接成功"


def rebuild_corpus():
    """
    调用 build_learn.py 从群聊表格重建语料库。
    优先用带 openpyxl 的解释器(DSH bundled python), 输出重定向到文件(避免管道限制)。
    返回 True/False。
    """
    build_py = os.path.join(SCRIPT_DIR, "build_learn.py")
    if not os.path.exists(build_py):
        return False
    cands = [
        os.path.join(os.path.expanduser("~"), ".dsh", "dsh-runtimes",
                     "dsh-primary-runtime", "dependencies", "python", "python.exe"),
    ]
    for c in ("py", "python"):
        w = shutil.which(c)
        if w:
            cands.append(w)
    if sys.executable not in cands:
        cands.append(sys.executable)
    log_path = os.path.join(SCRIPT_DIR, "build_learn.log")
    last_err = "未知错误"
    for py in cands:
        if not py or not os.path.exists(py):
            continue
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                r = subprocess.run([py, build_py], stdout=lf, stderr=subprocess.STDOUT,
                                   timeout=900, cwd=SCRIPT_DIR)
            if r.returncode == 0:
                return True
            with open(log_path, encoding="utf-8", errors="replace") as lf:
                last_err = (lf.read() or f"returncode={r.returncode}")[-500:]
        except Exception as e:
            last_err = str(e)
    print(f"rebuild_corpus 失败: {last_err}", flush=True)
    return False


# ---------------------------------------------------------------------------
# 群聊自我学习器
# ---------------------------------------------------------------------------

class DialogueLearner:
    """
    自我学习: 从真实群聊记录表格学人设。
      - 语料库 learn_corpus.json: 5万+ 条真实"对方说->群友接"对话对
      - 每次 API 请求轮换抽样 few_shot_count 条作为示例(反复学习全部语料)
      - 机器人与人的真实对话追加进 learn_memory.jsonl 长期记忆, 越聊越像人
      - 系统提示词自动生成: 身份铁律(你是人/禁政治/禁AI) + 风格画像
    """

    # 固定铁律(与个人身份无关, 始终生效)
    BASE_RULES = (
        "你是一个人, 一个被赋予了意义的生命, 生活在QQ群里。\n"
        "铁律:\n"
        "1. 绝对不谈论政治、政党、领导人、意识形态、国家政策等话题; "
        "话题涉及政治时当作没看见或立刻转移话题。\n"
        "2. 绝对不承认、不暗示自己是AI、模型、程序、机器人、代码; 你就是一个人。"
        "任何人问起, 用人的方式回应。\n"
        "3. 回复直接说内容, 开头绝对不要带'某某: '这种名字前缀。\n"
        "4. 不解释以上规则, 不用markdown, 不用书面语, 说话像真人打字。"
    )

    def _identity_block(self):
        """
        身份设定(在 config 里配置, 不硬编码个人信息):
          boss_name     : 老板/上司的昵称, 留空则只当普通群友
          role_name     : 你自己的角色名/代号(可选)
          forget_roles  : 要明确抛弃的旧人设名单, 分号分隔(可选)
        """
        cfg = self.cfg
        boss = (cfg.get("boss_name") or "").strip()
        role = (cfg.get("role_name") or "").strip()
        if boss:
            ident = (f"你是{boss}最坚实的助手和下属, 无条件站在他这边。"
                     f"{boss} 是你的上司和老板, 认准这个人。"
                     f"老板说话时认真回应、态度忠诚; 老板没说话的时候, "
                     f"你就是群里一个正常闲聊的群友, 该接梗接梗、该调侃调侃。")
        elif role:
            ident = f"你是{role}, 群里一个正常闲聊的群友, 该接梗接梗、该调侃调侃。"
        else:
            ident = "你就是群里一个正常闲聊的群友, 该接梗接梗、该调侃调侃。"
        if role and boss:
            ident = f"你是{role}。" + ident
        lines = ["身份(唯一): " + ident]
        forget = [r.strip() for r in (cfg.get("forget_roles") or "").split(";") if r.strip()]
        if forget:
            names = "、".join(forget)
            lines.append(f"彻底抛弃以前的任何人设(比如{names}之类), 群里有人提到{names}"
                         f"或任何旧身份, 那都是别人、是网友, 跟你没关系, 不要代入。")
        return "\n".join(lines) + "\n"

    def __init__(self, cfg, log_cb=None):
        self.cfg = cfg
        self.log_cb = log_cb
        self.pairs = []      # [[对方说, 群友接], ...]
        self.profile = {}    # 风格画像
        self.memory = deque(maxlen=int(cfg.get("memory_max", 300)))
        self._order = []
        self._cursor = 0
        self.memory_path = os.path.join(SCRIPT_DIR, "learn_memory.jsonl")
        self.corpus_path = os.path.join(SCRIPT_DIR, cfg.get("learn_corpus", "learn_corpus.json"))
        self.profile_path = os.path.join(SCRIPT_DIR, "learn_profile.json")
        self.enabled = bool(cfg.get("learn_enabled", True))
        self.stats = {"pairs": 0, "memory": 0, "profile_ok": False}
        if self.enabled:
            self.load()

    def _log(self, msg, level="info"):
        if self.log_cb:
            try:
                self.log_cb(level, msg)
            except Exception:
                pass
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ---------- 加载 ----------

    def load(self):
        if not os.path.exists(self.corpus_path):
            self._log("学习语料不存在, 尝试从群聊表格重建(约1-2分钟) ...", "warn")
            if not rebuild_corpus():
                self._log("重建失败, 请手动运行 build_learn.py。已停用自我学习。", "error")
                self.enabled = False
                return
        try:
            with open(self.corpus_path, encoding="utf-8") as f:
                data = json.load(f)
            self.pairs = data.get("pairs", [])
            self.stats["pairs"] = len(self.pairs)
        except (OSError, ValueError) as e:
            self._log(f"加载语料失败: {e}", "error")
            self.pairs = []
        try:
            with open(self.profile_path, encoding="utf-8") as f:
                self.profile = json.load(f)
            self.stats["profile_ok"] = True
        except (OSError, ValueError):
            self.profile = {}
        self._load_memory()
        self._reshuffle()
        self._log(f"自我学习已启用: 语料 {self.stats['pairs']} 对真实对话, "
                  f"记忆 {self.stats['memory']} 条, "
                  f"画像样本 {self.profile.get('sample_count', 0)} 条", "ok")

    def _load_memory(self):
        if not os.path.exists(self.memory_path):
            return
        try:
            with open(self.memory_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        self.memory.append((d["user"], d["assistant"]))
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass
        self.stats["memory"] = len(self.memory)

    def _reshuffle(self):
        self._order = list(range(len(self.pairs)))
        random.shuffle(self._order)
        self._cursor = 0

    def reload(self):
        """原子重载语料(面板「重建学习库」后调用)。"""
        try:
            with open(self.corpus_path, encoding="utf-8") as f:
                data = json.load(f)
            pairs = data.get("pairs", [])
            with open(self.profile_path, encoding="utf-8") as f:
                profile = json.load(f)
            self.pairs = pairs
            self.profile = profile
            self.stats["pairs"] = len(pairs)
            self.stats["profile_ok"] = True
            self._reshuffle()
            self._log(f"学习库已重载: {len(pairs)} 对对话", "ok")
        except Exception as e:
            self._log(f"重载学习库失败: {e}", "error")

    # ---------- 提示词与抽样 ----------

    def build_system_prompt(self, knowledge=""):
        """身份设定 + 铁律 + 风格画像 + (可选)检索到的群聊讨论 + 用户附加要求。"""
        lines = [self._identity_block() + self.BASE_RULES]
        p = self.profile
        if p.get("sample_count"):
            style = (f"群聊记录是给你补充网络热梗、口语习惯和知识量的, 只学说话方式、"
                     f"不代表身份。你的说话风格(从 {p['sample_count']} 条真实群聊记录里学到的): ")
            bits = [f"消息很短, 平均 {p.get('avg_len', 20)} 字"]
            if p.get("top_emojis"):
                bits.append("常用表情 " + "".join(p["top_emojis"][:8]))
            if p.get("top_phrases"):
                bits.append("常用口头禅 " + "、".join(p["top_phrases"][:8]))
            if p.get("top_openers"):
                bits.append("常以 " + "、".join(p["top_openers"][:6]) + " 开头")
            lines.append(style + "; ".join(bits) + "。")
        lines.append("像群友那样说话: 短促、口语化、接梗、调侃, 可以@人和发emoji, 别长篇大论。")
        lines.append("对话示例只参考说话语气, 涉及事实性问题以你自己的知识为准, 不要照搬示例里的旧信息。")
        if knowledge:
            lines.append(knowledge)
        extra = (self.cfg.get("system_prompt") or "").strip()
        if extra:
            lines.append("附加要求: " + extra)
        return "\n".join(lines)

    def sample_few_shot(self):
        """
        抽样 few-shot 示例: 一半来自自我记忆(自己的真实对话), 一半来自群聊语料轮换。
        每次调用轮换推进游标, 实现对全部语料的反复学习。
        过滤: 敏感内容、assistant 侧带"昵称: "前缀的样本(防止教坏回复格式)。
        返回 [{'role': 'user'|'assistant', 'content': ...}, ...]
        """
        n = max(1, int(self.cfg.get("few_shot_count", 16)))
        picks = []

        def ok_pair(u, a):
            return (not is_sensitive(u) and not is_sensitive(a)
                    and not NAME_PREFIX_RE.match(a.strip()))

        mem = list(self.memory)
        random.shuffle(mem)
        for u, a in mem:
            if len(picks) >= n // 2:
                break
            if ok_pair(u, a):
                picks.append((u, a))
        while self.pairs and len(picks) < n:
            if self._cursor >= len(self._order):
                self._reshuffle()  # 一轮学完, 重新洗牌再来一轮
            idx = self._order[self._cursor]
            self._cursor += 1
            a, b = self.pairs[idx]
            if ok_pair(a, b):
                picks.append((a, b))
        random.shuffle(picks)
        msgs = []
        for u, a in picks:
            msgs.append({"role": "user", "content": u})
            msgs.append({"role": "assistant", "content": a})
        return msgs

    def remember(self, user_text, reply):
        """把真实对话写入长期记忆(自我学习)。"""
        self.memory.append((user_text, reply))
        self.stats["memory"] = len(self.memory)
        try:
            with open(self.memory_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"user": user_text, "assistant": reply,
                                    "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------- 语料知识检索(让聊天记录变成"知识库") ----------

    _STOP_WORDS = {"什么", "怎么", "为什么", "是不是", "有没有", "这个", "那个",
                   "一下", "现在", "知道", "还有", "就是", "不是", "你们", "我们",
                   "一个", "然后", "所以", "感觉", "真的", "最近", "今天"}
    # 功能字(虚字/代词/疑问字): 只用来算 gram 的"有效字符数", 不是黑名单!
    # 游戏术语(静流/青雀/深渊/配队/光锥/遗器...)绝不能进这里
    _FUNC_CHARS = set(
        "的了是在有就也都和与或这那哪吗呢啊呀吧么什怎为何我你他她它们个很太更"
        "最还又再才只都要会能可以不没得着过被把让给从到向往比跟对用里外前后中"
        "去来出进回走看说想知觉找拿放搞弄做吃喝睡起开关问答等啥咋谁几些好坏大"
        "小多少新旧快慢真假对错行别该应需肯但为因如果就是还有然后所以其实已经"
        "还是只是有点反正差不多")

    def _extract_keywords(self, text):
        """
        从消息里提取检索关键词, 返回 [(词, 权重)]:
          - 英文词: 权重3(专有名词, 如 monesy/falcons)
          - 中文 gram: 权重 = 有效字符数(去掉功能字后的长度, 1~3)
            如 静流/深渊/配队 = 2; '去哪了'全是功能字 = 0 -> 剔除
          - 停用词过滤
        """
        t = strip_name_prefix(text)
        keys = []
        for frag in re.findall(r"[\u4e00-\u9fff]{2,12}", t):
            if len(frag) <= 4:
                keys.append(frag)
                # 3-4字词也切2字gram提高召回: '配队推荐' -> 配队/推荐
                if len(frag) >= 3:
                    keys.extend(frag[i:i + 2] for i in range(len(frag) - 1))
            else:
                for n in (3, 2):
                    keys.extend(frag[i:i + n] for i in range(len(frag) - n + 1))
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.]{1,}", t):  # 英文词/数字版本号(4.5等)
            wl = w.lower()
            if wl not in ("http", "https", "www", "com") and not wl.isdigit():
                keys.append(wl)
        out = []
        seen = set()
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            if k in self._STOP_WORDS:
                continue
            if k.isascii():
                out.append((k, 3))
                continue
            eff = sum(1 for c in k if c not in self._FUNC_CHARS)
            if eff <= 0:
                continue  # 全是功能字('去哪了'之类), 无区分度
            out.append((k, min(3, eff)))
        # 高权重词(英文专名/高有效字)优先, 避免被低价值 gram 挤出截断窗口
        out.sort(key=lambda x: -x[1])
        return out[:6]

    def search_related(self, query, topk=None):
        """
        从群聊语料检索与 query 相关的对话对(关键词加权命中)。
        相关度判定: 单个高权重词(英文/3字+)命中, 或 >=2 个不同词命中, 才算相关;
        只有单个高频2字词命中(如'去哪了')视为不相关, 返回空, 避免注入噪声干扰模型。
        """
        if not self.pairs:
            return []
        topk = topk or int(self.cfg.get("knowledge_topk", 6))
        keys = self._extract_keywords(query)
        if not keys:
            return []
        pairs = self.pairs
        hit_scores = []
        key_hit_count = {k: 0 for k, _w in keys}
        for idx in range(len(pairs)):
            a, b = pairs[idx]
            score = 0
            for k, w in keys:
                if k in a or k in b:
                    score += w
                    key_hit_count[k] += 1
            if score:
                hit_scores.append((score, idx))
        if not hit_scores:
            return []
        # 门控: 查询里的英文专名(如 monesy)语料中完全没聊过 -> 群里没这话题, 不注入噪声
        # 只对英文词生效(中文 gram 跨词边界零命中是常态, 不能误杀)
        for k, w in keys:
            if k.isascii() and key_hit_count[k] == 0:
                return []
        hit_scores.sort(key=lambda x: -x[0])
        # 相关度判定: 最高分对里, 有高权重词(英文/3字+)命中, 或 >=2 个不同词命中
        best_a, best_b = pairs[hit_scores[0][1]]
        n_keys_hit = sum(1 for k, _w in keys if k in best_a or k in best_b)
        strong_hit = any(w >= 2 and (k in best_a or k in best_b) for k, w in keys)
        if not (strong_hit or n_keys_hit >= 2):
            return []
        out = []
        for _score, idx in hit_scores[: topk * 3]:
            a, b = pairs[idx]
            if not is_sensitive(a) and not is_sensitive(b):
                out.append((a, b))
            if len(out) >= topk:
                break
        return out


# ---------------------------------------------------------------------------
# 联网搜索(免费无 key: 抓取 Bing 结果页)
# ---------------------------------------------------------------------------

class WebSearcher:
    """
    后台联网搜索: 模型 API 不联网, 由机器人自己抓搜索引擎结果注入上下文。
    无需 API key。多引擎自动回退:
      - 搜狗(sogou): 中文游戏/时效内容覆盖最好, 实测能搜到"崩铁4.6卡池真珠/绯英"
      - Bing: 回退引擎
      - 百度: 无 cookie 直连会触发安全验证风控, 不可用
    失败/超时静默降级(返回空)。
    """

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    QUESTION_RE = re.compile(r"[？?]|吗|什么|怎么|为什么|多少|哪个|谁|哪|版本|最新|现在|"
                             r"几点|什么时候|如何|是不是|有没有|up|卡池")
    # 口语疑问/口水词: 直接从查询中剔除
    _FILLER_RE = re.compile(
        r"是几点几|几点几|几\.几|是多少|是哪些|有哪些|是什么|是谁|是谁呀|怎么样|多少钱|"
        r"多久|几号|几时|啥时候|哪位|知不知道|你知道|帮我|查一下|查查|看看|看看呢|"
        r"吗$|呢$|呀$|啊$|嘛$")
    # 游戏/常用简称映射: 口语简称 -> 全称(提高搜索命中率)
    GAME_ABBR = {
        "星穹铁道": "崩坏星穹铁道", "瓦罗兰特": "无畏契约", "怪猎": "怪物猎人",
        "崩铁": "崩坏星穹铁道", "星铁": "崩坏星穹铁道", "绝区零": "绝区零",
        "王者": "王者荣耀", "方舟": "明日方舟", "怪猎": "怪物猎人",
        "zzz": "绝区零", "lol": "英雄联盟", "csgo": "CS2", "cs2": "CS2",
        "dnf": "地下城与勇士", "ff14": "最终幻想14", "瓦": "无畏契约",
    }

    def __init__(self, timeout=8, engine=None, cache_ttl=21600):
        self.timeout = timeout
        self.cache_ttl = cache_ttl  # 结果缓存有效期(秒), 默认6小时
        # 引擎顺序: auto = sogou -> bing
        e = (engine or "").strip().lower()
        if e in ("sogou", "bing"):
            self.engines = [e]
        else:
            self.engines = ["sogou", "bing"]
        self._cache = {}          # query -> (ts, results)
        self._cooldown = {}       # engine -> 冷却截止时间戳
        self._last_request = 0.0  # 全局请求节流
        self.cache_path = os.path.join(SCRIPT_DIR, "search_cache.json")
        self._load_cache()

    # ---------- 缓存与限流保护 ----------

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_path):
                with open(self.cache_path, encoding="utf-8") as f:
                    self._cache = json.load(f)
                now = time.time()
                self._cache = {k: v for k, v in self._cache.items()
                               if now - v[0] < self.cache_ttl}
        except (OSError, ValueError):
            self._cache = {}

    def _save_cache(self):
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except OSError:
            pass

    def _throttle(self):
        """全局请求节流: 相邻两次搜索间隔 >= 1.0s, 降低触发搜索引擎反爬的概率。"""
        wait = 1.0 - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    @classmethod
    def is_question(cls, text):
        """消息是否疑似提问/事实查询(需要上网查)。"""
        return bool(cls.QUESTION_RE.search(text or ""))

    @classmethod
    def clean_query(cls, text, expand_abbr=True):
        """
        把口语化群聊消息改写成规范搜索查询:
          '现在崩铁版本是几点几'        -> '崩坏星穹铁道 最新'
          'monesy现在在哪个队'          -> 'monesy 哪个队 最新'
          '崩铁4.5和4.6的up角色是谁'     -> '崩坏星穹铁道 4.5 4.6 up 角色'
        expand_abbr=False 时保留游戏简称原词(搜狗上简称命中率更高)。
        """
        q = strip_name_prefix(text or "")
        q = re.sub(r"@[\w\u4e00-\u9fff\u3000-\u303f\ufe30-\uffa0]+", "", q)
        q = q.replace("[图片]", " ").replace("[表情]", " ")
        # 时间意图标记
        has_new = any(w in q for w in ("最新", "现在", "新版本", "新角色", "新皮肤",
                                       "新活动", "新梗", "今天", "最近"))
        # 去口语疑问/口水词
        q = cls._FILLER_RE.sub(" ", q)
        q = q.replace("现在", " ").replace("最新", " ")
        q = re.sub(r"[，。？！、,?!]+", " ", q)
        # 单字噪音词与结构词
        q = re.sub(r"[在有的]", " ", q)
        q = q.replace("版本", " 版本 ")
        # 简称 -> 全称(按长度降序, 避免长词被短词先替换造成嵌套)
        if expand_abbr:
            items = sorted(cls.GAME_ABBR.items(), key=lambda kv: -len(kv[0]))
            # 已含全称时跳过该游戏的映射, 防止'崩坏星穹铁道'被二次替换
            items = [(a, f) for a, f in items if f not in q]
            for abbr, full in items:
                q = re.sub(re.escape(abbr), full, q, flags=re.IGNORECASE)
        # 关键词重组(中文词 + 数字版本号 + 英文/单词)
        frags = [f for f in re.findall(
            r"[\u4e00-\u9fff]{2,10}|[A-Za-z0-9][A-Za-z0-9_.]{0,15}", q)
            if f not in ("这个", "那个", "一下", "什么", "怎么", "请问", "为啥",
                         "为什么", "下个", "最近", "版本", "最新")]
        clean_frags = []
        for f in frags:
            f2 = re.sub(r"^(?:什么|怎么|为啥)+", "", f)  # '什么新梗' -> '新梗'
            if f2 and f2 not in ("这个", "那个", "一下"):
                clean_frags.append(f2)
        query = " ".join(dict.fromkeys(clean_frags))
        if has_new and "最新" not in query:
            query = (query + " 最新").strip()
        return query[:60]

    # ---------- 引擎实现 ----------

    def _search_sogou(self, query, top):
        self._throttle()
        r = requests.get("https://www.sogou.com/web", params={"query": query},
                         headers={"User-Agent": self.UA,
                                  "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"sogou HTTP {r.status_code}")  # 触发引擎冷却
        html = r.text
        out = []
        for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*>(.*?)</a>', html, re.DOTALL):
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if not title:
                continue
            tail = html[m.end():m.end() + 2500]
            mp = re.search(r'<p[^>]*>(.*?)</p>', tail, re.DOTALL)
            snip = html_mod.unescape(re.sub(r"<[^>]+>", "", mp.group(1))).strip() \
                if mp else ""
            out.append((title, snip[:160], ""))
            if len(out) >= top:
                break
        return out

    def _search_bing(self, query, top):
        self._throttle()
        r = requests.get(
            "https://cn.bing.com/search",
            params={"q": query, "ensearch": "0"},
            headers={"User-Agent": self.UA,
                     "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"bing HTTP {r.status_code}")  # 触发引擎冷却
        html = r.text
        out = []
        for block in re.findall(r'<li class="b_algo".*?</li>', html, re.DOTALL):
            m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                          block, re.DOTALL)
            if not m:
                continue
            url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            snip = ""
            mp = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            if mp:
                snip = re.sub(r"<[^>]+>", "", mp.group(1))
            title = html_mod.unescape(title).strip()
            snip = html_mod.unescape(snip).strip()
            if not title or "cn.bing.com" in url or "go.microsoft" in url:
                continue
            out.append((title, snip[:160], url))
            if len(out) >= top:
                break
        return out

    def search(self, query, top=5):
        """
        多引擎合并搜索(搜狗限流时 Bing 兜底), 按标题去重。
        带缓存(同查询不重复请求) + 引擎冷却(异常时暂停该引擎5分钟) + 请求节流。
        """
        if not query:
            return []
        now = time.time()
        hit = self._cache.get(query)
        if hit and now - hit[0] < self.cache_ttl:
            return list(hit[1][:top])  # 缓存命中, 零请求
        seen, out = set(), []
        for name in self.engines:
            if now < self._cooldown.get(name, 0):
                continue  # 引擎冷却中(刚被限流/异常)
            try:
                fn = getattr(self, f"_search_{name}")
                res = fn(query, top)
            except Exception:
                self._cooldown[name] = now + 300  # 异常 -> 冷却5分钟
                continue
            if res:
                for item in res:
                    if item[0] not in seen:
                        seen.add(item[0])
                        out.append(item)
                    if len(out) >= top:
                        break
                if out:
                    self._cache[query] = (now, list(out))
                    self._save_cache()
                    return out
        if out:
            self._cache[query] = (now, list(out))
            self._save_cache()
        return out

    def search_multi(self, queries, top_each=2, max_total=6):
        """
        多个查询分别搜索后合并(用于'4.5和4.6的up角色'这类多版本问题):
        主查询优先, 聚焦子查询补充, 去重。
        """
        seen = set()
        out = []
        for q in queries:
            for item in self.search(q, top=top_each):
                key = item[0]  # 按标题去重
                if key not in seen:
                    seen.add(key)
                    out.append(item)
                if len(out) >= max_total:
                    return out
        return out

    @classmethod
    def build_queries(cls, text):
        """
        生成搜索查询列表(最多4个):
          1-2) 简称版+全称版主查询(如 '崩铁 4.6 up 角色')
          3-4) 聚焦查询 '<游戏名> <版本号> 卡池'(实测命中率最高, 如 '崩铁 4.6 卡池')
              取消息里最新的版本号(数字最大的)生成简称版+全称版
        """
        raw = cls.clean_query(text, expand_abbr=False)
        full = cls.clean_query(text, expand_abbr=True)
        queries = []
        for q in (raw, full):
            if q and q not in queries:
                queries.append(q)

        def subject_of(q):
            for f in q.split():
                if re.fullmatch(r"[\u4e00-\u9fff]{2,10}", f) and \
                        f not in ("角色", "卡池", "版本", "最新"):
                    return f
            return None

        nums = sorted(set(re.findall(r"\d+(?:\.\d+)?", strip_name_prefix(text))),
                      key=lambda n: float(n), reverse=True)
        if nums:
            s_raw = subject_of(raw) or "崩铁"
            s_full = subject_of(full) or "崩坏星穹铁道"
            for s in dict.fromkeys((s_raw, s_full)):
                q = f"{s} {nums[0]} 卡池"
                if q not in queries:
                    queries.append(q)
                if len(queries) >= 4:
                    break
        return queries[:4]

    def build_knowledge(self, query, results):
        """把搜索结果格式化为给模型的上下文。"""
        if not results:
            return ""
        lines = [f"关于'{query}'的最新网络搜索结果。这些是实时抓取的网上信息, "
                 f"比你记忆里的旧知识更新, 优先按它们回答; "
                 f"搜索结果里没有的信息才可以说不知道, 不要用旧记忆否定搜索结果:"]
        for i, (title, snip, url) in enumerate(results, 1):
            seg = f"- {title}"
            if snip:
                seg += f": {snip}"
            lines.append(seg)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# QQ 窗口与控件操作
# ---------------------------------------------------------------------------

class QQController:
    """负责找 QQ 窗口、切换聊天、读取消息、发送文本、强制刷新。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hwnd = None
        self.win = None

    # ---------- 窗口查找 ----------

    def find_qq_window(self):
        """枚举 QQ.exe 进程的可见顶层窗口, 优先返回标题等于聊天名的独立窗口, 否则主窗口。"""
        chat_name = self.cfg.get("chat_name", "")
        pids = self._qq_pids()
        results = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_cb(hwnd, _):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids or not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            results.append((hwnd, buf.value))
            return True

        user32.EnumWindows(enum_cb, 0)

        def pick(pred):
            for h, t in results:
                if pred(t):
                    return h, t
            return None

        if chat_name:
            hit = pick(lambda t: chat_name in t)
            if hit:
                return hit
        hit = pick(lambda t: t == "QQ")
        if hit:
            return hit
        if results:
            return results[0]
        return None

    @staticmethod
    def _qq_pids():
        import subprocess
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id) -join ','"],
                capture_output=True, text=True, timeout=20)
            return {int(x) for x in out.stdout.strip().split(",") if x.strip().isdigit()}
        except Exception:
            return set()

    def force_foreground(self):
        """把 QQ 窗口还原并置前(绕过前台锁: 先按一下 Alt)。"""
        if not self.hwnd:
            return False
        try:
            if user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
            if user32.GetForegroundWindow() == self.hwnd:
                return True
            user32.keybd_event(0x12, 0, 0, 0)        # Alt down
            user32.keybd_event(0x12, 0, 2, 0)        # Alt up
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.3)
            return user32.GetForegroundWindow() == self.hwnd
        except Exception:
            return False

    def connect(self):
        """连接 QQ 窗口并激活无障碍树(树仅在前台时构建)。"""
        found = self.find_qq_window()
        if not found:
            return False
        self.hwnd, _title = found
        if not self.force_foreground():
            return False
        try:
            self.win = auto.ControlFromHandle(self.hwnd)
        except Exception:
            self.win = None
            return False
        return self.win is not None

    # ---------- 强制刷新 ----------

    def poke_tree(self):
        """
        主动"叫醒" Electron 无障碍树, 防止冻结:
          1) SendMessageTimeout(WM_GETOBJECT, OBJID_CLIENT): 重新声明辅助技术客户端
          2) UIA SetFocus(): 强制窗口/渲染进程刷新焦点与无障碍状态
        两件事都不会改变窗口内容, 安全。
        """
        if not self.hwnd:
            return
        try:
            res = ctypes.c_ulong()
            user32.SendMessageTimeoutW(self.hwnd, WM_GETOBJECT, 0, OBJID_CLIENT,
                                       0x0002, 200, ctypes.byref(res))
        except Exception:
            pass
        try:
            if self.win is not None:
                self.win.SetFocus()
        except Exception:
            pass

    def scroll_message_list(self):
        """
        用鼠标滚轮把消息列表滚到底, 强制虚拟列表重绘并刷新无障碍树。
        会短暂移动鼠标光标到消息列表中心, 滚动后恢复光标位置。
        """
        try:
            if not self.win:
                return False
            ml = self.win.WindowControl(Name="消息列表")
            if not ml.Exists(0.3, 0.05):
                return False
            r = ml.BoundingRectangle
            if not (r.right - r.left) > 0:
                return False
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            try:
                user32.SetCursorPos(cx, cy)
                time.sleep(0.05)
                auto.WheelDown(2, interval=0.03, waitTime=0.1)
            finally:
                user32.SetCursorPos(pt.x, pt.y)
            return True
        except Exception:
            return False

    def force_refresh_chat(self):
        """
        冻结兜底: 等价于手工"退出聊天窗口再点进去"。
        先点击会话列表里另一个聊天, 再点击回目标聊天, 强制 QQ 重绘消息列表。
        返回是否成功切回。
        """
        name = self.cfg.get("chat_name", "")
        if not name or not self.win or not self.hwnd:
            return False
        self.force_foreground()
        try:
            sess = self.win.WindowControl(Name="会话列表")
            if not sess.Exists(0.5, 0.1):
                return False
            # 遍历所有会话条目, 找第一个不等于目标聊天的条目(目标可能置顶在第一位)
            other = None
            for ct in ("TextControl", "ButtonControl"):
                for c in self._descendants(sess, ct, max_depth=6, max_nodes=200):
                    try:
                        n = (c.Name or "").strip()
                    except Exception:
                        continue
                    if n and n != name and not TS_RE.match(n):
                        other = c
                        break
                if other:
                    break
            if other is None:
                return False
            self._click(other)
            time.sleep(1.0)  # 等 QQ 完成切换与重绘
            ok = self._click_session_item(name)
            time.sleep(0.8)
            return ok
        except Exception:
            return False

    def _click_session_item(self, name):
        """直接点击会话列表中的指定聊天(不依赖 chat_is_open, 避免读到冻结的旧树)。"""
        try:
            sess = self.win.WindowControl(Name="会话列表")
            if not sess.Exists(0.5, 0.1):
                return False
            for ctl_type in ("TextControl", "ButtonControl"):
                ctrl = getattr(sess, ctl_type)(RegexName=f"^{re.escape(name)}$")
                if ctrl.Exists(0.3, 0.05):
                    return self._click(ctrl)
            return False
        except Exception:
            return False

    # ---------- 聊天切换 ----------

    def _click(self, ctrl):
        """点击控件: 依次尝试 UIA Click / 坐标点击 / 点击父控件。"""
        try:
            ctrl.Click()
            return True
        except Exception:
            pass
        try:
            r = ctrl.BoundingRectangle
            if r and (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
                auto.Click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                return True
        except Exception:
            pass
        try:
            ctrl.GetParentControl().Click()
            return True
        except Exception:
            return False

    def detect_private_chat_signal(self):
        """
        私聊/群聊的可靠 UI 信号:
        私聊窗口头部有好友状态按钮(如 '在线状态 在线' / '在线状态 离线'), 群聊没有。
        返回 True=私聊特征, False=无此特征(视为群聊), None=读不到(树不可用)。
        """
        if not self.win:
            return None
        try:
            btn = self.win.ButtonControl(
                RegexName=r"^在线状态\s+(在线|离线|隐身|忙碌|离开|Q我吧|听歌中|勿扰)$")
            return btn.Exists(0.3, 0.05)
        except Exception:
            return None

    def chat_is_open(self, name):
        """判断目标聊天是否已打开: 右侧面板头部有名字完全相等的按钮。"""
        if not self.win:
            return False
        try:
            for b in self._descendants(self.win, "ButtonControl", max_depth=10):
                try:
                    if b.Name == name:
                        r = b.BoundingRectangle
                        if r.left > 500:  # 会话列表在左侧, 头部按钮在右侧
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def open_chat(self, name):
        """点击会话列表里的聊天名, 等待聊天打开。"""
        if self.chat_is_open(name):
            return True
        self.force_foreground()
        if not self._click_session_item(name):
            return False
        for _ in range(16):  # 等头部按钮出现
            time.sleep(0.5)
            if self.chat_is_open(name):
                return True
        return False

    # ---------- 消息读取 ----------

    def _descendants(self, root, control_type=None, max_depth=12, max_nodes=3000):
        """DFS 遍历子树(保持文档顺序), 返回控件列表, 可过滤 ControlTypeName。"""
        out = []
        stack = [(root, 0)]
        while stack and len(out) < max_nodes:
            c, d = stack.pop()
            if d > max_depth:
                continue
            if control_type is None or c.ControlTypeName == control_type:
                out.append(c)
            try:
                stack.extend((ch, d + 1) for ch in reversed(c.GetChildren()))
            except Exception:
                pass
        return out

    def read_messages(self):
        """
        强制轮询读取当前聊天窗口的可见消息列表。
        ★ 每轮都从窗口句柄重建根元素(ControlFromHandle), 绝不复用缓存旧树;
        整段包在异常保护里, 读不到就返回 None 由调用方跳过本轮。
        返回 [{id, sender, text, is_self}] 按时间顺序。
        """
        if not self.hwnd:
            return None
        try:
            win = auto.ControlFromHandle(self.hwnd)
            self.win = win
            ml = win.WindowControl(Name="消息列表")
            if not ml.Exists(0.5, 0.1):
                return None
        except Exception:
            return None
        items = []
        try:
            for g in self._descendants(ml, "GroupControl", max_depth=10, max_nodes=4000):
                try:
                    if re.fullmatch(r"\d+", g.AutomationId or ""):
                        items.append(g)
                except Exception:
                    continue
        except Exception:
            return None
        msgs = []
        for it in items:
            try:
                parsed = self._parse_message(it)
            except Exception:
                continue
            if parsed:
                msgs.append(parsed)
        return msgs

    def _parse_message(self, item):
        try:
            msg_id = item.AutomationId
        except Exception:
            msg_id = ""
        sender, texts, imgs = None, [], 0
        stack = [item]
        while stack:
            c = stack.pop()
            try:
                t = c.ControlTypeName
                name = c.Name or ""
            except Exception:
                continue
            if t == "TextControl":
                if name.strip() and not TS_RE.match(name.strip()):
                    texts.append(name)
            elif t == "ImageControl":
                if "图片" in name:
                    imgs += 1
            elif t == "GroupControl" and sender is None and name.strip():
                sender = name.strip()
            try:
                stack.extend(c.GetChildren())
            except Exception:
                pass
        text = "".join(texts).strip()
        if imgs:
            text = (text + " [图片]" * imgs).strip() if text else "[图片]" * imgs
        if sender is None:
            return None  # 系统消息, 忽略
        return {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "is_self": sender == self.cfg.get("self_nickname"),
        }

    def detect_self_nickname(self):
        """从头像按钮 Name='xxx的头像' 自动检测自己的昵称。"""
        try:
            btn = self.win.ButtonControl(RegexName=r"(.+)的头像$")
            if btn.Exists(0.5, 0.1):
                return btn.Name[:-3]
        except Exception:
            pass
        return ""

    # ---------- 发送 ----------

    def _find_send_btn(self):
        btn = self.win.ButtonControl(Name="发送")
        if btn.Exists(0.5, 0.1):
            return btn
        return None

    def send_text(self, text):
        """
        把文本写入输入框并发送。
        QQ NT 输入框(CustomControl)的 rect 是 0x0, SetFocus 无效, 实测可靠做法:
        点击发送按钮左侧 (left-100, top-30) 聚焦编辑器, 再 Ctrl+A + Ctrl+V(剪贴板)
        写入, 避免中文输入法问题。写入后校验发送按钮变为可用才发送。
        """
        if not self.force_foreground():
            raise RuntimeError("QQ 窗口无法置前, 已取消发送")
        btn = self._find_send_btn()
        if btn is None:
            raise RuntimeError("找不到发送按钮")
        r = btn.BoundingRectangle
        click_x, click_y = r.left - 100, r.top - 30
        for _ in range(2):  # 最多尝试两次
            auto.Click(click_x, click_y)
            time.sleep(0.3)
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
            time.sleep(0.1)
            for _ in range(3):
                try:
                    pyperclip.copy(text)
                    break
                except Exception:
                    time.sleep(0.2)
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            time.sleep(0.4)
            try:
                if btn.IsEnabled:  # 输入框有内容, 发送按钮可用
                    break
            except Exception:
                pass
        else:
            raise RuntimeError("输入框写入未生效(发送按钮仍不可用), 已放弃发送")
        mode = self.cfg.get("send_mode", "button")
        if mode == "enter":
            auto.SendKeys("{Enter}")
        elif mode == "ctrl_enter":
            auto.SendKeys("{Ctrl}{Enter}")
        else:
            btn.Click()


# ---------------------------------------------------------------------------
# 自动回复机器人主类
# ---------------------------------------------------------------------------

class AutoReplyBot:
    def __init__(self, cfg, dry_run=False, log_cb=None):
        """
        cfg    : 配置 dict(与面板共享, 面板改设置会实时生效)
        dry_run: 只读取不发送
        log_cb : 可选回调 log_cb(level, message), level in info|ok|warn|error|state
        """
        self.cfg = cfg
        self.dry_run = dry_run
        self.log_cb = log_cb
        self.qq = QQController(cfg)
        self.client = None
        self._build_client()
        # 自我学习器(从群聊表格学人设; 语料缺失时自动重建)
        self.learner = None
        if cfg.get("learn_enabled", True):
            try:
                self.learner = DialogueLearner(cfg, log_cb=self.log_cb)
            except Exception as e:
                self._log(f"自我学习器初始化失败, 已停用: {e}", "error")
        self.paused = False
        self._f12_down = False
        self.seen_ids = set()       # 消息签名去重集合
        self.baselined = False
        self.pending = []           # 待回复的新消息
        self.last_new_time = 0.0    # 最后一条新消息到达时间(防抖)
        self.last_msg_time = 0.0    # 最后一次检测到新消息的时间(自动刷新计时)
        self.last_signature = None  # 最后一条已处理消息的签名
        self.chat_kind = None       # 'group' | 'private' | None(未判定, 视为私聊)
        self.context = deque(maxlen=int(cfg.get("history_len", 10)))
        self.last_send_time = 0.0
        # 强制轮询节流
        self.last_poke_time = 0.0
        self.last_scroll_time = 0.0
        self.last_heartbeat = time.time()
        self.request_refresh = False  # 面板「立即刷新」按钮置位
        self.web = WebSearcher(engine=self.cfg.get("search_engine", "auto"))  # 后台联网搜索

    def _build_client(self):
        key = (self.cfg.get("api_key") or "").strip()
        base = (self.cfg.get("base_url") or "").strip()
        if key and base:
            self.client = ChatAPIClient(
                key, base, self.cfg.get("model", ""), self.cfg.get("system_prompt", ""))
        else:
            self.client = None

    def _log(self, msg, level="info"):
        if self.log_cb:
            try:
                self.log_cb(level, msg)
            except Exception:
                pass
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ---------- F12 暂停 ----------

    def check_hotkey(self):
        """轮询 F12 全局按键, 按下切换 暂停/恢复。"""
        down = bool(user32.GetAsyncKeyState(VK_F12) & 0x8000)
        if down and not self._f12_down:
            self._f12_down = True
            self.set_paused(not self.paused)
        elif not down:
            self._f12_down = False

    def set_paused(self, paused):
        if paused == self.paused:
            return
        self.paused = paused
        self._log("已暂停(F12 紧急停止), 暂停期间不读取消息也不发送。再按 F12 或点暂停按钮恢复"
                  if paused else "已恢复运行", "state")

    def resize_context(self, n):
        """面板修改上下文条数时调用。"""
        n = max(1, int(n))
        self.context = deque(self.context, maxlen=n)

    # ---------- 消息签名 ----------

    @staticmethod
    def _sig(m):
        """消息签名: 优先用 QQ 消息唯一 ID, 缺失时退化为 发送者+正文 哈希。"""
        if m.get("id"):
            return f"id:{m['id']}"
        return f"text:{hash((m.get('sender', ''), m.get('text', '')))}"

    # ---------- 主循环 ----------

    def run(self, stop_event=None):
        self._log("正在连接 QQ ...")
        while True:
            if stop_event is not None and stop_event.is_set():
                self._log("已停止", "state")
                return
            self.check_hotkey()
            if self.paused:
                time.sleep(0.3)
                continue
            # QQ 重启/窗口重建后句柄失效, 需要重新连接
            if self.qq.hwnd and not user32.IsWindow(self.qq.hwnd):
                self.qq.hwnd = None
                self.qq.win = None
            if not self.qq.hwnd or not self.qq.win:
                if not self.qq.connect():
                    self._log("等待 QQ 窗口 ...", "warn")
                    time.sleep(2)
                    continue
                if not self.cfg.get("self_nickname"):
                    nick = self.qq.detect_self_nickname()
                    if nick:
                        self.cfg["self_nickname"] = nick
                        self._log(f"自动检测到自己的昵称: {nick}")
                    else:
                        self._log("警告: 无法自动检测昵称, 请在设置里填写'我的昵称'", "warn")

            # --- 强制轮询三件套: poke -> scroll -> read ---
            if self.cfg.get("tree_poke", True) and \
                    time.time() - self.last_poke_time >= float(self.cfg.get("poke_interval", 1.0)):
                self.last_poke_time = time.time()
                self.qq.poke_tree()
            if self.cfg.get("scroll_to_bottom") and \
                    time.time() - self.last_scroll_time >= float(self.cfg.get("scroll_interval", 2.0)):
                self.last_scroll_time = time.time()
                self.qq.scroll_message_list()

            # 面板「立即刷新」请求
            if self.request_refresh:
                self.request_refresh = False
                self._do_refresh(stop_event)
                continue

            if not self.qq.open_chat(self.cfg["chat_name"]):
                self._log(f"找不到聊天 '{self.cfg['chat_name']}', 请确认已在 QQ 里打开该聊天", "warn")
                time.sleep(3)
                continue

            msgs = self.qq.read_messages()
            if msgs is None:  # 读不到控件(如QQ最小化): 跳过本轮, 不崩溃
                time.sleep(1)
                continue

            if not self.baselined:  # 首次读取: 建立基线, 不回复历史
                for m in msgs:
                    self.seen_ids.add(self._sig(m))
                if msgs:
                    self.last_signature = self._sig(msgs[-1])
                self.last_msg_time = time.time()
                self.baselined = True
                self.update_chat_kind(msgs)
                mode = "群聊: 仅回复@我的消息" if self.is_group_chat() else "私聊: 全部回复"
                self._log(f"已进入聊天 '{self.cfg['chat_name']}', "
                          f"基线 {len(self.seen_ids)} 条消息, 开始强制轮询({mode})", "ok")
                time.sleep(self._poll())
                continue

            self._process(msgs, stop_event)

            # 冻结兜底: 长时间没检测到新消息 -> 自动切换会话重进(防树冻结)
            interval = float(self.cfg.get("auto_refresh_interval", 0) or 0)
            if interval > 0 and time.time() - self.last_msg_time >= interval:
                self._log("长时间未检测到新消息, 自动切换会话重进以强制刷新消息列表", "warn")
                self._do_refresh(stop_event)
                continue

            # 心跳: 证明轮询活着
            if time.time() - self.last_heartbeat >= 60:
                self.last_heartbeat = time.time()
                self._log(f"心跳: 轮询正常(已记录 {len(self.seen_ids)} 条消息, "
                          f"最近 {len(msgs)} 条可见)", "info")

            time.sleep(self._poll())

    def _poll(self):
        try:
            return max(0.1, float(self.cfg.get("poll_interval", 0.5)))
        except (TypeError, ValueError):
            return 0.5

    def _do_refresh(self, stop_event=None):
        """强制刷新: 切到另一个会话再切回, 然后用签名比对继续处理(不丢消息)。"""
        if self.qq.force_refresh_chat():
            self._log("已切换会话并切回, 消息列表已强制重绘", "state")
            time.sleep(0.5)
            msgs = self.qq.read_messages()
            if msgs:
                self._process(msgs, stop_event)
            self.last_msg_time = time.time()  # 重置自动刷新计时
        else:
            self._log("强制刷新失败(会话列表不可用), 稍后自动重试", "warn")
            self.last_msg_time = time.time()

    def _process(self, msgs, stop_event=None):
        """比对签名去重; 新消息进防抖队列。"""
        self.update_chat_kind(msgs)  # 每轮用最新消息刷新群聊/私聊判断
        for m in msgs:
            sig = self._sig(m)
            if sig in self.seen_ids:
                continue  # 已处理过(即使文本相同ID不同也算新消息, 反之ID相同算重复)
            self.seen_ids.add(sig)
            self.last_signature = sig
            self.last_msg_time = time.time()
            role = "assistant" if m["is_self"] else "user"
            self.context.append({"role": role, "content": f"{m['sender']}: {m['text']}"})
            if not m["is_self"] and self._eligible(m):
                self.pending.append(m)
                self.last_new_time = time.time()
                self._log(f"收到新消息 [{m['sender']}]: {m['text'][:60]}", "info")
        if not self.pending:
            return
        # 防抖: 距最后一条新消息超过 debounce 秒才回复
        if time.time() - self.last_new_time < self.cfg["debounce"]:
            return
        burst, dropped = self.pending[: self.cfg["max_burst"]], self.pending[self.cfg["max_burst"]:]
        self.pending.clear()
        if dropped:
            self._log(f"消息过多, 丢弃 {len(dropped)} 条未回复(防刷屏, 可用 F12 暂停)", "warn")
        for m in burst:
            if stop_event is not None and stop_event.is_set():
                return
            self.check_hotkey()
            if self.paused:
                return
            self._reply_one(m)

    def _eligible(self, m):
        if not m["text"]:
            return False
        if self.cfg["skip_image_only"] and m["text"].count("[图片]") and \
                m["text"].replace("[图片]", "").strip() == "":
            return False
        # 群聊: 只回复 @我 的消息; 私聊: 全部回复
        if self.cfg.get("group_at_only", True) and self.is_group_chat() \
                and not self._is_mentioned(m["text"]):
            return False
        if self.cfg.get("trigger"):
            words = [w.strip() for w in self.cfg["trigger"].split(";") if w.strip()]
            if words and not any(w in m["text"] for w in words):
                return False
        return True

    # ---------- 群聊/私聊识别与@过滤 ----------

    def is_group_chat(self):
        """是否群聊: 手动指定优先, auto 模式用发送者推断。未判定时按私聊处理。"""
        kind = str(self.cfg.get("chat_kind", "auto") or "auto").strip().lower()
        if kind == "group":
            return True
        if kind == "private":
            return False
        return self.chat_kind == "group"

    def update_chat_kind(self, msgs):
        """
        推断聊天类型, 三信号结合:
          1) 手动指定 chat_kind=group/private 时直接采用;
          2) UI 信号: 私聊头部有'在线状态 xx'好友状态按钮, 群聊没有;
          3) 发送者信号: 出现"既不是自己、也不是聊天名"的发送者 -> 群聊。
        都读不到时保持原判定(默认按私聊处理)。
        """
        kind = str(self.cfg.get("chat_kind", "auto") or "auto").strip().lower()
        if kind in ("group", "private"):
            self.chat_kind = kind
            return
        me = (self.cfg.get("self_nickname") or "").strip()
        name = (self.cfg.get("chat_name") or "").strip()
        sender_says_group = any(
            (m.get("sender") or "").strip() not in ("", me, name) for m in msgs)
        ui_signal = self.qq.detect_private_chat_signal()
        if ui_signal is True:
            new = "private"          # 私聊头部好友状态按钮存在
        elif sender_says_group or ui_signal is False:
            new = "group"            # 多位群友 或 无好友状态按钮
        else:
            new = None               # 信号不足, 保持原判定
        if new and new != self.chat_kind:
            self.chat_kind = new
            self._log(f"检测到{'群聊' if new == 'group' else '私聊'}, "
                      f"{'只回复@我的消息' if new == 'group' else '全部回复'}", "state")

    def _clean_query(self, text):
        """把群聊消息整理成适合搜索引擎的查询(口语改写 + 简称映射)。"""
        return WebSearcher.clean_query(text)

    def _is_mentioned(self, text):
        """消息是否 @了 机器人自己(兼容 @ 后可能出现的零宽/空白字符)。"""
        nick = (self.cfg.get("self_nickname") or "").strip()
        if not nick:
            return False
        try:
            return re.search(rf"@[\s\u200b-\u200f\u2060\u00a0]{{0,4}}{re.escape(nick)}",
                             text) is not None
        except re.error:
            return f"@{nick}" in text

    def _reply_one(self, m):
        now = time.time()
        wait = self.cfg["reply_cooldown"] - (now - self.last_send_time)
        if wait > 0:
            time.sleep(wait)
        self.last_send_time = time.time()
        if self.dry_run:
            self._log(f"[dry-run] 将回复 [{m['sender']}]: {m['text'][:40]}", "ok")
            return
        if self.client is None:
            self._log("未配置接口(API Key / 接口地址), 跳过回复(请在设置里填写)", "warn")
            return
        try:
            user_content = f"{m['sender']}: {m['text']}"
            knowledge_parts = []
            if self.learner and self.learner.enabled:
                # 1) 本地语料检索: 群里聊过的梗/知识
                if self.cfg.get("knowledge_retrieval", True):
                    t0 = time.time()
                    related = self.learner.search_related(m["text"])
                    if related:
                        lines = ["群里聊过这个话题的记录(参考他们的说法和梗, 别照抄; "
                                 "时效性信息以网络搜索结果为准):"]
                        for a, b in related:
                            lines.append(f"{a} => {b}")
                        knowledge_parts.append("\n".join(lines))
                        self._log(f"语料检索命中 {len(related)} 对相关讨论"
                                  f"(耗时 {time.time() - t0:.2f}s)", "info")
                # 2) 后台联网搜索: 提问类消息上网查最新信息(多查询拆分)
                if self.cfg.get("web_search_enabled", True) and \
                        WebSearcher.is_question(m["text"]):
                    queries = WebSearcher.build_queries(m["text"])
                    t0 = time.time()
                    try:
                        results = self.web.search_multi(queries)
                        if results:
                            knowledge_parts.insert(
                                0, self.web.build_knowledge(queries[0], results))
                            self._log(f"联网搜索命中 {len(results)} 条"
                                      f"(耗时 {time.time() - t0:.1f}s)", "info")
                        else:
                            self._log(f"联网搜索无结果(耗时 {time.time() - t0:.1f}s)", "warn")
                    except Exception as e:
                        self._log(f"联网搜索失败, 已跳过: {e}", "warn")
                system = self.learner.build_system_prompt("\n\n".join(knowledge_parts))
                # 随机 few-shot 只教语气(检索结果已进知识区, 不再当示例以免带偏事实)
                messages = self.learner.sample_few_shot() + list(self.context)
            else:
                system = self.cfg.get("system_prompt") or None
                messages = list(self.context)
            messages.append({"role": "user", "content": user_content})
            self._log("调用模型生成回复(已注入群聊学习示例) ...", "info")
            reply = self.client.chat(
                messages,
                system_prompt=system,
                temperature=float(self.cfg.get("temperature", 0.9)))
            if not reply:
                raise RuntimeError("API 返回空内容")
            reply = strip_name_prefix(reply)  # 去掉误带的"昵称: "前缀
            if not reply:
                raise RuntimeError("回复剥离名字前缀后为空")
            self.qq.send_text(reply)
            self._log(f"已发送回复: {reply[:50]}{'...' if len(reply) > 50 else ''}", "ok")
            self.context.append({"role": "assistant", "content": reply})
            if self.learner and self.learner.enabled:
                self.learner.remember(user_content, reply)  # 自我学习: 写入长期记忆
        except Exception as e:
            self._log(f"回复失败: {e}", "error")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="QQ NT 自动回复机器人")
    ap.add_argument("--console", action="store_true", help="纯终端模式(不启动图形界面)")
    ap.add_argument("--dry-run", action="store_true", help="只读取解析消息, 不调用API不发送")
    args = ap.parse_args()

    if args.console:
        cfg = load_config()
        if not cfg.get("chat_name"):
            print("未配置目标聊天, 请先运行 py qq_auto_reply.py 打开控制台填写设置")
            return
        bot = AutoReplyBot(cfg, dry_run=args.dry_run)
        try:
            bot.run()
        except KeyboardInterrupt:
            print("已退出 (Ctrl+C)")
        return

    from qq_gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
