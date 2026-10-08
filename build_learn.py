# -*- coding: utf-8 -*-
"""
离线建库: 解析 D:\\bot 下 group_*.xlsx 群聊记录表, 生成学习语料。

产物:
  learn_corpus.json   对话对语料: {"pairs": [[对方说, 群友接], ...], "meta": {...}}
  learn_profile.json  风格画像: 平均长度/常用表情/口头禅/开头习惯

用法(建议用带 openpyxl 的 python):
  <python> build_learn.py
"""
import glob
import json
import os
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from learn_common import clean_text, is_usable_text, is_sensitive, EMOJI_RE

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_PATH = os.path.join(BOT_DIR, "learn_corpus.json")
PROFILE_PATH = os.path.join(BOT_DIR, "learn_profile.json")

# 同一发送者消息间隔超过该秒数视为新一段(群聊冷场后另起话题)
SAME_SPEAKER_MERGE_SEC = 300


def parse_workbook(fp):
    """解析一个群聊 xlsx, 返回按时间排序的 [(发送者, 清洗后文本, 序号时间), ...]。"""
    import openpyxl
    wb = openpyxl.load_workbook(fp, read_only=True)
    ws = wb["聊天记录"] if "聊天记录" in wb.sheetnames else wb.worksheets[0]
    header = None
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if header is None:
            header = [str(c).strip() if c is not None else "" for c in row]
            try:
                idx_sender = header.index("发送者")
                idx_type = header.index("消息类型")
                idx_content = header.index("消息内容")
                idx_recall = header.index("是否撤回") if "是否撤回" in header else None
            except ValueError:
                wb.close()
                raise ValueError(f"表头无法识别: {header}")
            continue
        try:
            sender = str(row[idx_sender] or "").strip()
            mtype = str(row[idx_type] or "").strip()
            content = str(row[idx_content] or "").strip()
            recalled = str(row[idx_recall] or "否").strip() if idx_recall is not None else "否"
        except IndexError:
            continue
        if mtype != "文本" or recalled == "是" or not sender:
            continue
        text = clean_text(content)
        if not is_usable_text(text):
            continue
        rows.append((sender, text))
    wb.close()
    return rows


def build_pairs(rows):
    """把单条消息流合并成对话对: 相邻不同发送者 -> (A说, B接)。"""
    merged = []  # [sender, text]
    for sender, text in rows:
        if merged and merged[-1][0] == sender:
            prev = merged[-1][1]
            if len(prev) + len(text) + 1 <= 120:
                merged[-1][1] = prev + " " + text
                continue
        merged.append([sender, text])
    pairs = []
    for i in range(1, len(merged)):
        a_sender, a_text = merged[i - 1]
        b_sender, b_text = merged[i]
        if a_sender != b_sender:
            pairs.append((a_text, b_text))
    return pairs


def build_profile(all_texts):
    """风格画像: 长度分布、emoji、口头禅(高频短句)、开头习惯。"""
    lens = [len(t) for t in all_texts]
    emoji_counter = Counter()
    phrase_counter = Counter()
    opener_counter = Counter()
    for t in all_texts:
        for e in EMOJI_RE.findall(t):
            emoji_counter[e] += 1
        if 2 <= len(t) <= 30:
            phrase_counter[t] += 1
        opener = t[:2]
        if opener and not re.match(r"^[\[(【@0-9A-Za-z]", opener):
            opener_counter[opener] += 1
    n = len(all_texts) or 1
    return {
        "sample_count": n,
        "avg_len": round(sum(lens) / n, 1),
        "median_len": int(sorted(lens)[len(lens) // 2]),
        "top_emojis": [e for e, _ in emoji_counter.most_common(20)],
        "top_phrases": [p for p, c in phrase_counter.most_common(25) if c >= 5],
        "top_openers": [o for o, c in opener_counter.most_common(20) if c >= 20],
    }


def main():
    files = sorted(glob.glob(os.path.join(BOT_DIR, "group_*.xlsx")))
    if not files:
        print("未找到 group_*.xlsx 文件")
        return 1
    all_pairs = []
    all_texts = []
    meta = {"groups": [], "total_raw_msgs": 0}
    for fp in files:
        name = os.path.basename(fp)
        print(f"解析 {name} ...", flush=True)
        rows = parse_workbook(fp)
        pairs = build_pairs(rows)
        texts = [t for _, t in rows]
        all_pairs.extend(pairs)
        all_texts.extend(texts)
        meta["groups"].append({"file": name, "usable_msgs": len(texts), "pairs": len(pairs)})
        meta["total_raw_msgs"] += len(rows)
        print(f"  可用消息 {len(texts)} 条, 对话对 {len(pairs)} 个", flush=True)

    # 去重
    seen = set()
    dedup = []
    for a, b in all_pairs:
        key = (a, b)
        if key not in seen:
            seen.add(key)
            dedup.append([a, b])
    meta["total_pairs"] = len(dedup)
    meta["total_usable_msgs"] = len(all_texts)

    with open(CORPUS_PATH, "w", encoding="utf-8") as f:
        json.dump({"pairs": dedup, "meta": meta}, f, ensure_ascii=False)
    profile = build_profile(all_texts)
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    print(f"\n=== 完成 ===")
    print(f"对话对总数(去重): {meta['total_pairs']}")
    print(f"可用消息总数: {meta['total_usable_msgs']}")
    print(f"画像: 平均长度 {profile['avg_len']} 字, 中位 {profile['median_len']}")
    print(f"常用表情: {''.join(profile['top_emojis'][:10])}")
    print(f"口头禅: {profile['top_phrases'][:10]}")
    print(f"开头习惯: {profile['top_openers'][:10]}")
    print(f"语料库: {CORPUS_PATH} ({os.path.getsize(CORPUS_PATH)/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
