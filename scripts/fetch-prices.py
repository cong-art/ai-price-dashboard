#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 OpenRouter 拉取模型实时价格，写入 data/prices.json，并把本次结果追加进 data/history.json。

用法：
    python scripts/fetch-prices.py              # 正常抓取一次
    python scripts/fetch-prices.py --dry-run    # 只打印，不写文件

设计要点：
- 价格单位换算：OpenRouter 返回 per-token，本脚本统一乘 1e6 转为 $/1M tokens
- 展示列：输入(in) / 输出(out) / 缓存输入(cache_in)；`off_peak` 为该模型官方「闲时/高峰」折算价（如有）
- `mix`（混合成本）仍内部计算，仅用于历史 delta 对比，不再作为展示列输出语义
- 三档对比（较昨日/较上周/较上月）从历史样本中取「时间上最接近该基准点」的样本，
  而不是要求恰好命中；历史不足时该档返回 null，前端显示「—」
- 变体歧义：每个模型可配 or_id + fallbacks，按顺序取第一个在源里存在的
- 历史持久化：历史采样权威源为「GitHub 私有仓库文件」（见 gh-history.py），本地
  data/history.json 仅作缓存副本。配置 GH_HISTORY_TOKEN / GH_HISTORY_REPO 后自动启用
  GitHub 后端；未配置或不可达时回退本地，行为与旧版一致。
"""

import importlib.util
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
MODELS_JSON = os.path.join(DATA_DIR, "models.json")
PRICES_JSON = os.path.join(DATA_DIR, "prices.json")
HISTORY_JSON = os.path.join(DATA_DIR, "history.json")

# 加载同目录 gh-history.py（文件名带连字符，非合法模块名，用 importlib 按路径加载）。
# 它提供「把历史采样持久化到 GitHub 私有仓库」的能力；未配置 token 时自动不可用，
# 本脚本随之回退到本地 data/history.json（与改造前行为等价）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_gh_history = None
try:
    _spec = importlib.util.spec_from_file_location("gh_history", os.path.join(_HERE, "gh-history.py"))
    _gh_history = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_gh_history)
except Exception:
    _gh_history = None  # 后端模块缺失/损坏不影响价格主流程，历史回退本地

API_URL = "https://openrouter.ai/api/v1/models"
CST = timezone(timedelta(hours=8))
HISTORY_KEEP_DAYS = 400
TIMEOUT = 30

# —— 新模型发现 ——
# 每次抓取除价格外，还比对 OpenRouter 全目录：最近 DISCOVERY_WINDOW_DAYS 天内新上架、
# 且属于重点厂商（VENDOR_PREFIX 白名单）的模型，自动写入 models.json 进入价格表。
# 已见过的模型 id 登记在 data/discovered.json，避免重复判定。
DISCOVERED_JSON = os.path.join(DATA_DIR, "discovered.json")
DISCOVERY_WINDOW_DAYS = 21
DISCOVERY_MAX_PER_RUN = 5
VENDOR_PREFIX = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "deepseek": "深度求索", "moonshotai": "月之暗面", "z-ai": "智谱",
    "qwen": "阿里", "alibaba": "阿里", "x-ai": "xAI", "minimax": "MiniMax",
}
_SKIP_SUFFIX = {"free", "extended", "beta", "online", "thinking", "search", "plugins", "vision-exp", "batch"}
_SKIP_SUBSTR = ("preview", "exp-", "-exp", "embed", "whisper", "tts", "imagen",
                "veo", "dall-e", "sora", "moderation", "voice", "video")


def log(msg):
    print(msg, flush=True)


def fetch_api():
    """拉取 OpenRouter 模型列表，返回 {model_id: model} 字典。"""
    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": "model-price-board/1.0 (local dashboard)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return {m["id"]: m for m in payload.get("data", [])}


def resolve(api, model_cfg):
    """按 or_id -> fallbacks 顺序，返回第一个存在的 (model_id, model)。"""
    candidates = [model_cfg["or_id"]] + list(model_cfg.get("fallbacks") or [])
    for cid in candidates:
        if cid in api:
            return cid, api[cid]
    return None, None


def per_million(raw):
    try:
        return round(float(raw) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def mix_cost(inp, out):
    if inp is None or out is None:
        return None
    return round((inp * 3 + out) / 4, 4)


def pct_change(now, before):
    """相对变化百分比；基准为 0 或缺失时返回 None。"""
    if now is None or before in (None, 0):
        return None
    return round((now - before) / before * 100, 1)


def load_history():
    """加载历史采样。优先从 GitHub 私有仓库拉取权威副本；后端不可用则回退本地。

    返回 dict（含 samples 列表）。若远程拉取成功，会顺带把该副本写成本地缓存，
    供前端读取 / 离线降级 / 回滚使用。
    """
    if _gh_history is not None:
        try:
            remote, _sha = _gh_history.fetch()
        except Exception as e:
            remote, _sha = None, None
            log(f"  ! GitHub 历史读取异常，回退本地：{type(e).__name__}: {e}")
        if isinstance(remote, dict) and isinstance(remote.get("samples"), list):
            # 远程为权威：刷新本地缓存副本
            _write_local_history({"samples": remote["samples"]})
            return {"samples": remote["samples"]}

    if not os.path.exists(HISTORY_JSON):
        return {"samples": []}
    try:
        with open(HISTORY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("samples"), list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        log(f"  ! history.json 读取失败，将重建：{e}")
    return {"samples": []}


def _write_local_history(history):
    """原子写本地 history.json 缓存副本。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HISTORY_JSON, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    except OSError as e:
        log(f"  ! 本地 history.json 缓存写入失败：{e}")


def prune(samples, now):
    cutoff = now - timedelta(days=HISTORY_KEEP_DAYS)
    kept = [s for s in samples if parse_ts(s.get("t")) and parse_ts(s["t"]) >= cutoff]
    kept.sort(key=lambda s: s["t"])
    return kept


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def nearest_sample(samples, target):
    """取时间上最接近 target 的历史样本。"""
    best, best_gap = None, None
    for s in samples:
        ts = parse_ts(s.get("t"))
        if ts is None:
            continue
        gap = abs((ts - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = s, gap
    return best


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(CST)
    refresh(now=now, write=not dry_run, verbose=True)
    if dry_run:
        log("\n--- DRY RUN（未写文件）---")


def _load_prev_rows():
    """读取上一次 prices.json 的行（按模型名索引），供「源暂时失败时沿用上次快照」兜底。"""
    try:
        with open(PRICES_JSON, "r", encoding="utf-8") as f:
            prev = json.load(f)
        rows = prev.get("models") or []
        if isinstance(rows, list):
            return {r.get("name"): r for r in rows if isinstance(r, dict) and r.get("name")}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _atomic_write_json(path, payload):
    """原子写 JSON：先写 .tmp 再 os.replace，避免读端抓到半截文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _load_registry():
    """读取已见模型登记表（data/discovered.json），保证新模型判定只做一次。"""
    try:
        with open(DISCOVERED_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("seen"), dict):
            return d
    except (json.JSONDecodeError, OSError):
        pass
    return {"seen": {}}


def _pretty_model_name(mdl):
    """OpenRouter 的 name 形如 "OpenAI: GPT-5.7 Pro"，去掉作者前缀。"""
    name = mdl.get("name") or ""
    if ":" in name:
        name = name.split(":", 1)[1].strip()
    return name or mdl.get("id", "")


def _group_by_price(inp, out):
    """按混合成本分组：>=6 旗舰档 / >=2 主力档 / 其余性价比档。"""
    m = mix_cost(inp, out)
    if m is None:
        return "value"
    if m >= 6:
        return "flagship"
    if m >= 2:
        return "mainstream"
    return "value"


_RADAR_BASE = {  # 六维基线（编码/推理/长上下文/多模态/指令/性价比），按档位区分
    "flagship":   [86, 86, 82, 80, 85, 55],
    "mainstream": [78, 77, 73, 70, 78, 76],
    "value":      [68, 66, 65, 60, 72, 90],
}
_RADAR_COLORS = {  # 同厂商多款模型时按序取未占用的变体色
    "OpenAI":    ["#0A7D32", "#059669", "#065F46", "#16A34A", "#0284C7"],
    "Anthropic": ["#0066CC", "#1D4ED8", "#3B82F6", "#60A5FA"],
    "Google":    ["#FF9500", "#F59E0B", "#D97706"],
    "xAI":       ["#374151", "#4B5563"],
    "月之暗面":   ["#7C3AED", "#6D28D9"],
    "深度求索":  ["#5856D6", "#818CF8"],
    "智谱":      ["#2563EB", "#1D4ED8"],
    "阿里":      ["#0891B2", "#0E7490"],
    "MiniMax":   ["#DC2626", "#B91C1C"],
}


def _radar_for(group, vendor, out_price, used_colors):
    """为自动发现的新模型赋定性雷达分与颜色（启发式，页面口径：定性而非跑分）。

    高价档在上调编码/推理、下调性价比；OpenAI/Google/xAI 传统上多模态更强；
    颜色从同厂商变体色板中取第一个未被占用的。
    """
    v = list(_RADAR_BASE.get(group, _RADAR_BASE["mainstream"]))
    if (out_price or 0) >= 15:
        v[0] += 4; v[1] += 4; v[5] -= 6
    elif 0 < (out_price or 0) <= 1.5:
        v[0] -= 4; v[1] -= 4; v[5] += 4
    if vendor in ("OpenAI", "Google", "xAI"):
        v[3] += 6
    v = [max(20, min(98, x)) for x in v]
    palette = _RADAR_COLORS.get(vendor, ["#6B7280", "#4B5563", "#9CA3AF"])
    color = next((c for c in palette if c not in used_colors), palette[0])
    return {"v": v, "color": color}


def _base_id(cid):
    """归并键：去掉 :variant 后缀与 -0902 / -20260902 型日期快照尾巴。

    用于把「同一模型的新版本快照」归并进既有条目的 fallbacks，
    而不是当成一个新模型另立一行。
    """
    b = cid.split(":", 1)[0].lower()
    b = re.sub(r"-\d{6,8}$", "", b)
    b = re.sub(r"-\d{4}$", "", b)
    return b


def discover_new_models(api, models_cfg, now, verbose=True):
    """比对 OpenRouter 目录，发现新发布的重点厂商模型。

    返回 (待入库的新模型 cfg 列表, 更新后的登记表)；同时把本次见到的所有 id 登记为
    已见（调用方在 write=True 时落盘）。判定口径：
      - 最近 DISCOVERY_WINDOW_DAYS 天内上架（OpenRouter created 字段）
      - 作者前缀在 VENDOR_PREFIX 白名单内
      - 排除免费档、变体后缀（:free/:beta/:batch/…）与非对话模型（embed/tts/…）
      - 基名与既有模型相同（仅日期快照/变体差异）→ 归并为该模型的 fallback，不另立条目
      - 单次最多收录 DISCOVERY_MAX_PER_RUN 款，按上架时间新→旧
    """
    log_fn = log if verbose else (lambda m: None)
    reg = _load_registry()
    seen = reg["seen"]
    known = set(seen.keys())
    base_owner = {}  # base_id -> models_cfg 条目（供快照归并）
    for m in models_cfg:
        known.add(m["or_id"])
        base_owner.setdefault(_base_id(m["or_id"]), m)
        for fb in m.get("fallbacks") or []:
            known.add(fb)
            base_owner.setdefault(_base_id(fb), m)

    cutoff = now - timedelta(days=DISCOVERY_WINDOW_DAYS)
    fresh = []          # 真正的新模型
    snapshot_map = []   # (新快照 id, 归属条目) —— 作为 fallback 追加
    for cid, mdl in api.items():
        if cid in known:
            continue
        low = cid.lower()
        suffix = low.rsplit(":", 1)[1] if ":" in low else ""
        if suffix in _SKIP_SUFFIX or any(s in low for s in _SKIP_SUBSTR):
            seen[cid] = seen.get(cid) or now.isoformat()
            continue
        try:
            if float((mdl.get("pricing") or {}).get("prompt") or 0) == 0 \
                    and float((mdl.get("pricing") or {}).get("completion") or 0) == 0:
                seen[cid] = seen.get(cid) or now.isoformat()
                continue  # 免费档不算正式发布
        except (TypeError, ValueError):
            seen[cid] = seen.get(cid) or now.isoformat()
            continue
        try:
            created = datetime.fromtimestamp(int(mdl.get("created")), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            seen[cid] = seen.get(cid) or now.isoformat()
            continue
        author = cid.split("/", 1)[0].lower()
        vendor = VENDOR_PREFIX.get(author)
        owner = base_owner.get(_base_id(cid))
        if owner is not None:
            # 同一模型的新版本快照：归并进既有条目的 fallbacks
            snapshot_map.append((cid, owner))
            seen[cid] = seen.get(cid) or now.isoformat()
            continue
        if vendor is None or created < cutoff:
            seen[cid] = seen.get(cid) or now.isoformat()
            continue
        fresh.append((created, cid, mdl, vendor))

    for cid, owner in snapshot_map:
        fbs = owner.setdefault("fallbacks", [])
        if cid not in fbs:
            fbs.append(cid)
            log_fn(f"  ↳ 版本快照归并：{cid} -> {owner['name']} 的 fallback")

    fresh.sort(reverse=True, key=lambda x: x[0])
    used_colors = {m["radar"]["color"] for m in models_cfg if m.get("radar")}
    new_entries = []
    for created, cid, mdl, vendor in fresh[:DISCOVERY_MAX_PER_RUN]:
        inp = per_million((mdl.get("pricing") or {}).get("prompt"))
        out = per_million((mdl.get("pricing") or {}).get("completion"))
        group = _group_by_price(inp, out)
        entry = {
            "name": _pretty_model_name(mdl),
            "vendor": vendor,
            "group": group,
            "or_id": cid,
            "fallbacks": [],
            "discovered": now.strftime("%Y-%m-%d"),
            "note": "自动发现的新模型（%s 上架 OpenRouter）" % now.strftime("%Y-%m-%d"),
            "radar": _radar_for(group, vendor, out, used_colors),
        }
        used_colors.add(entry["radar"]["color"])
        new_entries.append(entry)
        log_fn(f"  ★ 发现新模型：{entry['name']}（{cid}，{vendor}，上架于 {created:%Y-%m-%d}）")
    for created, cid, mdl, vendor in fresh[DISCOVERY_MAX_PER_RUN:]:
        log_fn(f"  … 其余新模型暂缓（单次上限 {DISCOVERY_MAX_PER_RUN} 款）：{cid}")
        seen[cid] = seen.get(cid) or now.isoformat()  # 暂缓款也登记，下轮不再重复
    reg["seen"] = seen
    reg["updated_at"] = now.isoformat()
    return new_entries, snapshot_map, reg


def refresh(now=None, write=True, verbose=True):
    """抓取一次价格并（可选）写出 prices.json + 追加 history.json，返回 out_payload。

    供 CLI 与线上 HTTP 服务（server.py）共用。now 可注入便于测试；verbose 控日志。
    失败抛异常，由调用方决定是否保留旧数据。
    """
    if now is None:
        now = datetime.now(CST)
    log_fn = (lambda m: None) if not verbose else log

    with open(MODELS_JSON, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    vendors = cfg["vendors"]
    models_cfg = cfg["models"]

    log_fn(f"[{now:%Y-%m-%d %H:%M:%S}] 拉取 {API_URL} ...")
    # 抓取失败重试一次；两次都失败则进入「沿用上次快照」降级模式（仅真实写场景），
    # 绝不让一次网络抖动把 16 模型的好数据覆盖成残缺数据。
    api = None
    for attempt in (1, 2):
        try:
            api = fetch_api()
            break
        except Exception as e:
            log_fn(f"  ! 第 {attempt} 次拉取失败：{type(e).__name__}: {e}"
                   + ("，重试…" if attempt == 1 else "，改用上次快照兜底"))
    if api is not None:
        log_fn(f"  接口返回 {len(api)} 个模型")
        # —— 新模型发布探测 ——
        try:
            new_entries, snapshot_map, reg = discover_new_models(api, models_cfg, now, verbose)
        except Exception as e:
            new_entries, snapshot_map, reg = [], [], None
            log_fn(f"  ! 新模型发现失败（不影响主流程）：{type(e).__name__}: {e}")
        if new_entries or snapshot_map:
            models_cfg = models_cfg + new_entries
            if write and reg is not None:
                # 新模型 + 版本快照 fallback 持久写回 models.json（原子写），下轮起视为已知模型
                try:
                    cfg_new = dict(cfg)
                    cfg_new["models"] = models_cfg
                    _atomic_write_json(MODELS_JSON, cfg_new)
                    _atomic_write_json(DISCOVERED_JSON, reg)
                    log_fn(f"  ✓ 已收录 {len(new_entries)} 款新模型进 models.json")
                except OSError as e:
                    log_fn(f"  ! models.json/discovered.json 写入失败：{e}")
            elif not write and reg is not None:
                log_fn("  （dry-run：新模型未落盘）")

    prev_rows = _load_prev_rows() if write else {}
    rows, missing, carried, snapshot = [], [], [], {}
    for m in models_cfg:
        official = m.get("official")
        if official:
            # 官网官方价 override：不从 OpenRouter 拉，直接使用厂商官方标价。
            # snapshot 用独立 key（official:厂商:模型名），与 OpenRouter 采样历史隔离，
            # 避免「从聚合折扣价切到官网价」被误判成涨价/降价的跨口径假 delta。
            cid = "official:%s:%s" % (m["vendor"], m["name"])
            inp = round(float(official["in"]), 4)
            out = round(float(official["out"]), 4)
            cache_in = official.get("cache_in")
            if cache_in is not None:
                cache_in = round(float(cache_in), 4)
            # 闲时/高峰（off_peak）官方折算价（可选）
            off_peak = None
            op = official.get("off_peak")
            if op:
                off_peak = {
                    "in": round(float(op.get("in", inp)), 4),
                    "out": round(float(op.get("out", out)), 4),
                }
                if op.get("cache_in") is not None:
                    off_peak["cache_in"] = round(float(op["cache_in"]), 4)
            src = "official"
            note = official.get("note", "")
            ctx = m.get("ctx")  # 官方行的上下文长度来自 models.json（OpenRouter 无此数据）
            log_fn(f"  用官网官方价 {m['name']}: in={inp} out={out} cache={cache_in} off_peak={off_peak}（{note[:40]}…）")
        else:
            cid, model = resolve(api, m) if api is not None else (None, None)
            if model is None:
                # 源中找不到（接口失败或条目缺失）：沿用上次快照，避免残缺数据上线
                prev = prev_rows.get(m["name"])
                if prev is not None and prev.get("in") is not None and prev.get("out") is not None:
                    cid = prev.get("or_id") or m["or_id"]
                    inp = prev.get("in")
                    out = prev.get("out")
                    cache_in = prev.get("cache_in")
                    off_peak = prev.get("off_peak")
                    ctx = prev.get("ctx")
                    src = prev.get("src", "openrouter")
                    note = prev.get("note", "")
                    carried.append(m["name"])
                    log_fn(f"  ~ 源中未找到，沿用上次快照：{m['name']}（in={inp} out={out}）")
                else:
                    missing.append(m["name"])
                    log_fn(f"  ! 未在源中找到且无上次快照：{m['name']}（{m['or_id']}）")
                    continue
            else:
                pricing = model.get("pricing") or {}
                inp = per_million(pricing.get("prompt"))
                out = per_million(pricing.get("completion"))
                cache_in = per_million(pricing.get("input_cache_read") or pricing.get("cached"))
                off_peak = None
                if pricing.get("discount"):
                    # OpenRouter 部分模型有「闲时折扣」，折算为 off_peak 档
                    try:
                        d = float(pricing["discount"])
                        if 0 < d < 1:
                            off_peak = {
                                "in": round(inp * (1 - d), 4) if inp is not None else None,
                                "out": round(out * (1 - d), 4) if out is not None else None,
                            }
                    except (TypeError, ValueError):
                        pass
                ctx = model.get("context_length")
                src = "openrouter"
                note = ""

        snapshot[cid] = {"in": inp, "out": out}
        row = {
            "key": m["name"],
            "or_id": cid,
            "name": m["name"],
            "vendor": m["vendor"],
            "group": m["group"],
            "in": inp,
            "out": out,
            "cache_in": cache_in,
            "off_peak": off_peak,
            "tier": m.get("tier"),
            "mix": mix_cost(inp, out),   # 仅供 delta 历史对比用，不再作为展示列
            "ctx": ctx,
            "src": src,
            "note": note,
            "discovered": bool(m.get("discovered")),
            "pricing_url": vendors.get(m["vendor"], ""),
        }
        rows.append(row)

    # 历史采样与三档对比
    history = load_history()
    samples = history["samples"]

    # 避免同一分钟重复写入：若最后一条样本时间相同则替换
    if samples and samples[-1].get("t", "")[:16] == now.isoformat()[:16]:
        samples[-1] = {"t": now.isoformat(), "prices": snapshot}
    else:
        samples.append({"t": now.isoformat(), "prices": snapshot})

    # 基准只允许取「严格早于本次采样」的历史，避免拿自己当基准
    prior = [
        s for s in samples
        if parse_ts(s.get("t")) and parse_ts(s["t"]) < now - timedelta(minutes=1)
    ]
    baselines = {
        label: nearest_sample(prior, now - timedelta(days=days))
        for label, days in (("d1", 1), ("d7", 7), ("d30", 30))
    }

    for r in rows:
        r["delta"] = {}
        for label, baseline in baselines.items():
            if baseline is None:
                r["delta"][label] = None
                continue
            prev = (baseline.get("prices") or {}).get(r["or_id"]) or {}
            prev_mix = None
            if prev.get("in") is not None and prev.get("out") is not None:
                prev_mix = mix_cost(prev["in"], prev["out"])
            r["delta"][label] = pct_change(r["mix"], prev_mix)

    samples = prune(samples, now)

    out_payload = {
        "generated_at": now.isoformat(),
        "source": "openrouter",
        "source_url": API_URL,
        "model_count": len(rows),
        "history_samples": len(samples),
        "missing": missing,
        "carried": carried,
        "models": rows,
    }

    if not write:
        if verbose:
            for r in rows:
                d = r["delta"]
                op = r["off_peak"]
                log(f"  {r['name']:18s} in={r['in']:<7} out={r['out']:<7} cache={r['cache_in']} off={op and (op['in'],op['out'])} d1={d['d1']} d7={d['d7']} d30={d['d30']}")
        return out_payload

    os.makedirs(DATA_DIR, exist_ok=True)
    _atomic_write_json(PRICES_JSON, out_payload)

    # —— 历史样本写回 ——
    # 权威源优先写 GitHub 私有仓库（乐观锁，跨实例/跨重部署持久）；随后总把最新副本
    # 同步到本地 data/history.json（供前端、离线降级与回滚）。GitHub 不可用时仅保留本地，
    # 下次刷新若 GitHub 恢复会以远程为基线再合并，不会丢已累积样本。
    final_history = {"samples": samples}
    pushed = False
    if _gh_history is not None and samples:
        try:
            pushed = _gh_history.put_latest(final_history)
        except Exception as e:
            log(f"  ! GitHub 历史推送失败（保留本地）：{type(e).__name__}: {e}")
    _write_local_history(final_history)

    log_fn(f"\n✓ 已写入 {PRICES_JSON}")
    log_fn(f"✓ 历史样本数：{len(samples)}（保留 {HISTORY_KEEP_DAYS} 天）"
           + (" · 已持久化到 GitHub" if pushed else " · 存于本地"))
    if missing:
        log_fn(f"  注意：{len(missing)} 个模型未在源中找到 -> {missing}")
    if carried:
        log_fn(f"  兜底：{len(carried)} 个模型沿用上次快照 -> {carried}")
    if verbose:
        for r in rows:
            d = r["delta"]
            op = r["off_peak"]
            log(f"  {r['name']:18s} in={r['in']:<7} out={r['out']:<7} cache={r['cache_in']} off={op and (op['in'],op['out'])} d1={d['d1']} d7={d['d7']} d30={d['d30']}")

    return out_payload


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] 抓取失败：{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
