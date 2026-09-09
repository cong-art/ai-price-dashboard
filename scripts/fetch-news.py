#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取中文 AI 资讯（IT之家 + 量子位），过滤出 AI 相关条目，写入 data/news.json。

设计要点：
- 来源：IT之家 https://www.ithome.com/rss/ 、量子位 https://www.qbitai.com/feed
  （原 36氪 /feed 被反爬拦截返回 HTML，已弃用；量子位为垂直 AI 媒体作主力源）
- 每个来源各自返回若干条，脚本做：相关性过滤 → 全局去重 → 时间倒序 → 自动分类打标 → 截取 N 条
- 保留「前一天 06:00 起」的条目（即每次运行往前回溯约 26h，避免刚好跨过当日零点丢漏），
  也兼容当日早上刚发的；按 pubDate 倒序取最新。
- 输出 schema 对齐 index.html 新闻卡片：{category,date,title,desc,source,link}
  category 由标题/摘要关键词自动推断（模型发布/融资并购/产业动态/工具应用/监管政策/算力硬件等）。

用法：
    python scripts/fetch-news.py              # 正常抓取一次，写 data/news.json
    python scripts/fetch-news.py --dry-run    # 只打印，不写文件
    python scripts/fetch-news.py --days=3     # 回溯天数（默认 2，含前一日+当日）
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
NEWS_JSON = os.path.join(DATA_DIR, "news.json")

CST = timezone(timedelta(hours=8))
TIMEOUT = 30
MAX_ITEMS = 9

# ---- 来源 ----
SOURCES = [
    {"key": "ithome", "label": "IT之家", "url": "https://www.ithome.com/rss/"},
    {"key": "qbitai", "label": "量子位", "url": "https://www.qbitai.com/feed"},
]

# ---- AI 相关性过滤 ----
# 判断只基于「标题」：RSS 正文（description）里"AI/机器人"常作泛背景词出现，
# 若混入会淹没信号（手机/汽车/硬件新闻几乎都会提一句 AI）。标题是作者提炼的核心，
# AI 相关的新闻标题必然直接含 AI 实体或主题词，用它做保守白名单判断最稳。
AI_KEYWORDS = [
    "AI", "人工智能", "大模型", "ChatGPT", "OpenAI", "Gemini", "Gemma",
    "Claude", "Anthropic", "Copilot", "通义", "千问", "Qwen", "豆包", "智谱",
    "GLM", "Kimi", "MiniMax", "DeepSeek", "文心", "讯飞", "元象", "阶跃", "零一万物",
    "混元", "GPT-", "Llama", "Stable Diffusion", "Hugging Face",
    "智能体", "具身智能", "人形机器人", "多模态", "推理模型",
    "Agent", "AIGC", "生成式", "文生图", "文生视频", "Sora", "算法",
    "AI芯片", "AI应用", "AI助手", "AI眼镜", "机器学习", "深度学习",
    "自动驾驶", "机器人", "神经网络", "Neural", "Physical AI",
]

# 排除词：标题命中排除词则直接剔除（消费电子 / 汽车 / 泛金融硬件噪声，即便含 AI 字眼）。
AI_BLOCK = [
    "手机", "平板", "电视", "耳机", "智能手表", "笔记本", "电脑", "显示器",
    "汽车", "电动车", "电动", "电池", "折叠屏", "样张", "众筹", "显卡",
    "芯片发布", "新车", "上市开售", "助听器", "剃须刀", "暖风机", "洗衣机",
    "扫地机器人", "游戏机",
]

# ---- 分类打标 ----
# 有序：返回第一个命中；先用 title、title 兜底时用 desc 补充。兜底「AI 产业」。
# 命名的厂商/应用类主题优先，宽泛的「模型动态」落最后，避免所有带「发布/上线」的标题都归它。
CATEGORY_RULES = [
    (["融资", "并购", "收购", "IPO", "上市", "投资", "获投", "招股", "估值"], "融资并购"),
    (["监管", "政策", "法规", "治理", "网信", "合规", "备案", "起诉", "诉讼", "反垄断"], "监管动态"),
    # 巨头 / 明星模型动态优先标出（点名了厂商或具体模型名）
    (["微软", "谷歌", "苹果", "Meta", "字节", "腾讯", "阿里", "百度", "亚马逊", "OpenAI", "Anthropic", "英伟达", "蚂蚁", "OpenRouter", "Hugging Face"], "巨头动态"),
    (["机器人", "具身", "自动驾驶", "智能驾驶", "Agent", "智能体", "仿生"], "智能应用"),
    (["芯片", "GPU", "算力", "服务器", "数据中心", "英伟达", "NVIDIA", "AMD", "摩尔线程", "澜起", "晶体管"], "算力硬件"),
    (["开源", "开发者", "SDK", "API", "编程", "代码", "框架", "论文", "研究", "炼丹"], "开发者研究"),
    (["合作", "接入", "联手", "生态", "战略", "联名", "入驻", "达成", "上线眼镜版"], "产业合作"),
    (["财报", "营收", "业绩", "裁员", "人事", "离职", "股价", "市值", "月活", "上榜", "贷款", "银团"], "行业数据"),
    # 通用发布/迭代类兜底
    (["发布", "上线", "推出", "亮相", "更新", "迭代", "开源", "登顶", "屠榜", "开售"], "模型动态"),
]
CATEGORY_FALLBACK = "AI 产业"


def log(msg):
    print(msg, flush=True)


def fetch(url):
    """抓取 RSS 文本；失败抛异常。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(raw):
    """去 HTML 标签 + 反转义 + 压缩空白。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def polish_desc(desc, max_len=150):
    """精炼 RSS 摘要，去掉来源引导语与段落残句，让卡片更像精编摘要。

    - 去掉开头的「IT之家 9 月 3 日消息，据 XX 报道/介绍/称 …」式引导
    - 去掉结尾「XX 从原报道获悉 / IT之家注 / 详见 …」这类残句
    - 若末尾残留截断段落（如“据官方 PPT 介绍，”）则一并清掉
    - 截断到 max_len 字符
    """
    if not desc:
        return ""
    d = desc
    # 开头：媒体 + 消息来源引导句
    d = re.sub(
        r"^【?[^，。；]{0,20}?(?:X月|0?\d月|月)\s?\d+\s?日[，,]?\s*消息[，,]\s*"
        r"(?:据[^，。]{2,30}[，,]\s*)?(?:报道|介绍|称|显示|表示|指出)?[，,：: ]?\s*",
        "", d,
    )
    d = re.sub(r"^(?:据|记者从|外媒|当地)[^，。]{2,24}[，,]\s*(?:报道|消息)?[，,：: ]?\s*", "", d)
    # 结尾残句：IT之家注/IT之家从原报道获悉/详见 …
    d = re.sub(r"[。；]\s*(?:IT之家[^。]*?(?:注|获悉|提醒|点评)[^。]*|详见[^。]*|原标题[^。]*|更多[^。]*)$", "", d)
    d = re.sub(r"[。；，,]\s*(?:据[^。]{0,30}介绍[^。]*|某官方[^。]*|另外|同时)[。；]?\s*$", "", d)
    d = re.sub(r"[。；，,][。；，,]+", "。", d)
    if len(d) > max_len:
        cut = d[:max_len]
        # 尽量在句子边界截断
        for sep in ["。", "；", "，", "！", "？", " "]:
            idx = cut.rfind(sep)
            if idx > max_len * 0.6:
                cut = cut[: idx + 1]
                break
        d = cut.rstrip("，。；、 ") + "…"
    return d.strip()


def parse_pubdate(s):
    """解析 RFC822 日期（GMT / +0000 / +0800 等），转 CST datetime。失败返回 None。"""
    if not s:
        return None
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(s.strip())
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except (ValueError, TypeError, OverflowError):
        return None


def classify(text):
    """根据文本关键词返回分类标签（中文）。优先 title，失败才用 desc 补充。"""
    for kws, cat in CATEGORY_RULES:
        for k in kws:
            if k in text:
                return cat
    return CATEGORY_FALLBACK


def best_category(title, desc):
    """先用 title 分类，title 兜底时再用 desc 补充一次。"""
    c = classify(title)
    if c != CATEGORY_FALLBACK:
        return c
    return classify(title + " " + desc)


def is_ai_related(title):
    """仅依据标题判断是否 AI 相关（保守白名单）。"""
    t = title.lower()
    if any(b.lower() in t for b in AI_BLOCK):
        return False
    for kw in AI_KEYWORDS:
        if kw.lower() in t:
            return True
    return False


def norm_title(t):
    """标题归一化用于去重。"""
    t = t.lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
    return t


def parse_feed(xmltext, source_label):
    """解析 RSS XML → 原始条目 list[dict]。"""
    root = ET.fromstring(xmltext)
    channel = root.find("channel")
    node = channel if channel is not None else root
    out = []
    for item in node.iter("item"):
        def val(tag):
            e = item.find(tag)
            return clean_text(e.text) if (e is not None and e.text) else ""
        title = val("title")
        if not title:
            continue
        # description 可能为空（量子位），尝试 content:encoded
        desc = val("description")
        if not desc:
            for enc in item.iter():
                if enc.tag.endswith("}encoded") or enc.tag == "encoded":
                    desc = clean_text(enc.text)
                    break
        link = val("link")
        pub = item.findtext("pubDate")
        dt = parse_pubdate(pub) if pub else None
        out.append({
            "title": title,
            "desc": desc,
            "link": link,
            "source": source_label,
            "pub_iso": dt.isoformat() if dt else None,
            "pub_dt": dt,
        })
    return out


def refresh(days=2, write=True, quiet=False):
    """抓取一次并（可选）写出 data/news.json，返回 payload dict。

    供 CLI 与线上 HTTP 服务（server.py）共用，避免逻辑重复。
    任一源失败只降级该源，不会中断整体；全部失败时抛 RuntimeError。
    """
    log_fn = (lambda m: None) if quiet else log
    hours_back = days * 24
    now = datetime.now(CST)
    cutoff = now - timedelta(hours=hours_back)

    log_fn(f"[{now:%Y-%m-%d %H:%M:%S}] 抓取中文 AI 资讯（回溯 {hours_back}h）...")

    raw_items = []
    for src in SOURCES:
        try:
            xmltext = fetch(src["url"])
            items = parse_feed(xmltext, src["label"])
            log_fn(f"  {src['label']}: 抓到 {len(items)} 条")
            raw_items.extend(items)
        except Exception as e:
            log_fn(f"  ! {src['label']} 抓取失败：{type(e).__name__}: {e}")

    if not raw_items:
        raise RuntimeError("所有新闻源均失败或无条目")

    # 过滤：时间窗 + AI 相关（title-only）
    kept = []
    for it in raw_items:
        if it["pub_dt"] is None or it["pub_dt"] < cutoff:
            continue  # 跳过太旧
        if not is_ai_related(it["title"]):
            continue  # 跳过非 AI（依据标题）
        kept.append(it)
    log_fn(f"  时间窗+相关性过滤后剩 {len(kept)} 条")

    # 去重（同源标题归一化去重）
    seen = set()
    uniq = []
    for it in sorted(kept, key=lambda x: x["pub_dt"] or now, reverse=True):
        nk = norm_title(it["title"])
        if nk in seen:
            continue
        seen.add(nk)
        uniq.append(it)

    # 分类 + 组装 schema
    news = []
    for it in uniq[:MAX_ITEMS]:
        cat = best_category(it["title"], it["desc"])
        news.append({
            "category": cat,
            "date": it["pub_dt"].strftime("%m-%d"),
            "title": it["title"],
            "desc": polish_desc(it["desc"]),
            "source": it["source"],
            "link": it["link"],
        })

    payload = {
        "generated_at": now.isoformat(),
        "sources": [s["label"] for s in SOURCES],
        "window": f"回溯 {days} 天",
        "count": len(news),
        "news": news,
    }

    log_fn(f"\n汇总 {len(news)} 条新闻（去重、倒序、截前 {MAX_ITEMS}）:")
    for n in news:
        log_fn(f"  [{n['category']}·{n['date']}] {n['title'][:46]} ｜ {n['source']}")

    if write:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = NEWS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, NEWS_JSON)  # 原子写，避免半截文件被读到
        log_fn(f"\n✓ 已写入 {NEWS_JSON}")

    return payload


def main():
    dry_run = "--dry-run" in sys.argv
    days = 2
    for a in sys.argv:
        if a.startswith("--days="):
            try:
                days = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                pass
    refresh(days=days, write=not dry_run)
    if dry_run:
        print("\n--- DRY RUN（未写文件）---")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 新闻抓取失败：{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
