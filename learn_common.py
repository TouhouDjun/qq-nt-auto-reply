# -*- coding: utf-8 -*-
"""
学习系统公共模块: 消息清洗 + 政治敏感词过滤。
build_learn.py(离线建库) 与 qq_auto_reply.py(运行时) 共用。
"""
import re

# 政治敏感词过滤表(用于: 1.剔除语料样本 2.抽样时跳过)。
# 宁可错杀不可放过: 学习样本绝不含政治内容, 身份铁律也禁止谈论政治。
SENSITIVE_WORDS = [
    "共产党", "中共", "ccp", "gcd", "国民党", "民进党", "台独", "藏独", "疆独", "港独",
    "习近平", "习主席", "总书记", "国家主席", "主席", "总理", "李克强", "李强", "胡锦涛",
    "江泽民", "邓小平", "毛泽东", "毛主席", "蔡英文", "赖清德", "马英九", "陈水扁",
    "特朗普", "拜登", "哈里斯", "普京", "泽连斯基", "金正恩", "安倍", "尹锡悦",
    "六四", "天安门", "法轮功", "大法", "退党", "学运", "政变", "起义", "革命",
    "阶级斗争", "意识形态", "社会主义", "共产主义", "资本主义", "文革", "大跃进",
    "游行", "示威", "抗议", "上访", "信访", "政治", "体制", "政权", "统治",
    "政府", "官员", "领导", "公务员", "贪腐", "腐败", "反华", "辱华", "爱国",
    "新疆", "西藏", "台湾问题", "香港问题", "一国两制", "人quan", "人权",
]
_SENS_RE = re.compile("|".join(re.escape(w) for w in SENSITIVE_WORDS), re.IGNORECASE)

# 图片/表情/引用等占位符: QQ 导出有两种形式
#   闭合:   [图片:xxxx.jpg]
#   不闭合: [图片:xxxx.jpg ... 或 [image: xxxx (资源消息后半段)
_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情|动画表情|表情包|贴纸|视频|语音|文件|链接|小程序|卡片|音乐|image|video|file)"
    r"(?::\s?[^\]\s]*)?\]?"  # 注意 [image: xxx] 冒号后可能有空格
)
_HTTP_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@[\w\u4e00-\u9fff\u3000-\u303f\ufe30-\uffa0]+")

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF]"
)


def is_sensitive(text):
    return bool(_SENS_RE.search(text or ""))


def clean_text(raw):
    """
    清洗一条群聊消息:
      - 去掉 [图片:xxx] 等占位(图片统一为[图片], 表情统一为[表情]), 兼容不闭合形式
      - 去掉 http 链接
      - 去掉 QQ 导出标记前缀(可多个叠加: [内联键盘][Markdown消息])
      - 保留 @提及 与 emoji(人类聊天特征)
    返回清洗后的文本, 空则返回 ""。
    """
    if not raw:
        return ""
    t = str(raw).strip()
    if not t:
        return ""
    # QQ 导出标记前缀, 循环去除多个叠加的
    while True:
        m = re.match(r"^\[(?:内联键盘|Markdown消息|JSON消息|文本|表情)\]", t)
        if not m:
            break
        t = t[m.end():].strip()
    # 转发消息整段不要
    if t.startswith("[转发消息") or t.startswith("[合并转发"):
        return ""
    # 占位符统一
    t = _PLACEHOLDER_RE.sub(lambda m: "[图片]" if "图片" in m.group(0) else "[表情]", t)
    # 图片/表情文件名 hash 残留: [表情] 9417EC586C9558A67F29BC114F30E6 / xxx.jpg
    t = re.sub(r"(?:\s+|^)[0-9A-Fa-f]{24,}(?:\.(?:jpg|jpeg|png|gif|webp))?", "", t)
    # 链接去掉(保留文字部分)
    t = _HTTP_RE.sub("", t)
    t = t.strip()
    return t


def is_usable_text(text, min_len=2, max_len=120):
    """判断清洗后的文本是否适合作为学习样本。"""
    if not text:
        return False
    if text in ("[图片]", "[表情]") or text.replace("[图片]", "").replace("[表情]", "").strip() == "":
        return False
    if is_sensitive(text):
        return False
    # 群机器人指令不学: "/命令" 或 "@机器人 /命令"
    if text.startswith("/") or re.match(r"^@\S+\s+/", text):
        return False
    # 入群欢迎机器人语不学
    if "欢迎" in text and ("入群" in text or "加入" in text) and len(text) < 60:
        return False
    n = len(text)
    if n < min_len or n > max_len:
        return False
    return True
