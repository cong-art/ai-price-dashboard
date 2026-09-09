#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 data/baseline.json（接入 OpenRouter 前的画布静态快照）作为历史种子注入 data/history.json。

为何需要：真实的三档对比（较昨日/较上周/较上月）依赖跨天历史样本，接入当天必然没有。
本脚本把接入前的官方口径快照拆成三个参考样本（约 1 / 7 / 30 天前），
让首日即有对比可看。每个样本带 "seed": true 标记。

何时应重跑：仅在你删除或重建 history.json 之后、需要重新引导一次。日常由 fetch-prices.py
的真实定时采样逐步覆盖即可，无需再跑。

用法：
    python scripts/seed-history.py            # 注入（已存在相同 seed 时间戳则跳过）
    python scripts/seed-history.py --reset    # 先清空全部历史再注入
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_JSON = os.path.join(DATA_DIR, "history.json")
BASELINE_JSON = os.path.join(DATA_DIR, "baseline.json")

CST = timezone(timedelta(hours=8))
# 种子相对接入日的参考时点：接入日 09:30 左右，往前推 N 天
SEED_OFFSET_DAYS = [1, 7, 30]
SEED_HOUR = 9
SEED_MINUTE = 30


def main():
    reset = "--reset" in sys.argv

    with open(BASELINE_JSON, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    prices = baseline["prices"]

    history = {"samples": []}
    if os.path.exists(HISTORY_JSON) and not reset:
        try:
            with open(HISTORY_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("samples"), list):
                history = data
        except (json.JSONDecodeError, OSError):
            history = {"samples": []}

    # 已有种子标记集合
    existing = {s.get("t") for s in history["samples"] if s.get("seed")}
    anchor = datetime(2026, 9, 3, SEED_HOUR, SEED_MINUTE, tzinfo=CST)
    added = 0
    for days in SEED_OFFSET_DAYS:
        ts = anchor - timedelta(days=days)
        iso = ts.isoformat()
        if iso in existing:
            print(f"  跳过已存在种子：{iso}")
            continue
        # 克隆一份 prices，仅保留该模型存在输入价的项（避免 None key）
        snap = {k: v for k, v in prices.items() if v.get("in") is not None}
        history["samples"].append({"t": iso, "prices": snap, "seed": True})
        added += 1

    history["samples"].sort(key=lambda s: s["t"])
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump({"samples": history["samples"]}, f, ensure_ascii=False, separators=(",", ":"))

    print(f"✓ 种子样本：本次新增 {added}，历史总样本 {len(history['samples'])}（reset={reset}）")
    for s in history["samples"]:
        tag = "seed" if s.get("seed") else "real"
        print(f"   [{tag}] {s['t']}  模型 {len(s.get('prices') or {})} 个")


if __name__ == "__main__":
    main()
