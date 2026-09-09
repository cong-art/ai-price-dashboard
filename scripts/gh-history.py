#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 私有仓库历史持久化后端 —— 把「价格历史采样」从易失的托管沙箱本地盘，
迁移到一个外部 GitHub 私有仓库的单一文件上，使累积跨实例重启 / 跨重部署不丢。

设计动机
--------
线上 server.py 部署在「单 HTTP 端口 + 可写但不可靠的本地 data/」环境：每次冷启动/重部署
都可能把本地 data/history.json 回滚到部署快照，导致「较昨日/上周/上月」的累积历史丢失。
本模块用 GitHub Contents API 把 history.json 的权威副本存到私有仓库，线上进程每次刷新
前先拉远程最新副本、追加后乐观锁写回，从而让历史真正跨生命周期持久。

用法（由 fetch-prices.py / server.py 调用，也可单独 CLI 测试）
------------------------------------------------------------
环境变量（均可省，缺失则本模块自动“不可用”，调用方降级回本地 data/）：
    GH_HISTORY_TOKEN   GitHub PAT，需目标私有仓库的 contents 读写权限
    GH_HISTORY_REPO    "owner/repo"
    GH_HISTORY_PATH    仓库内文件路径，默认 "history.json"

读写 API
--------
    fetch()                     -> (payload: dict|None, sha: str|None)
    push(payload, base_sha)     -> bool   （base_sha 为 None 表示远程不存在=新建）
    put_latest(payload)         -> bool   （乐观锁重试：push 409 冲突时自动重拉合并再试）

约定
----
- 仅用 Python 标准库（urllib / base64），不引入第三方依赖，便于沙箱离线启动。
- 任何失败都不抛异常给上层抓取逻辑：网络错误 / 4xx / 无 token 一律视为“后端不可用”，
  返回 None / False，由 fetch-prices.py 决定回退到本地 data/history.json。
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error

API = "https://api.github.com"


def _conf():
    token = os.environ.get("GH_HISTORY_TOKEN", "").strip()
    repo = os.environ.get("GH_HISTORY_REPO", "").strip()
    path = os.environ.get("GH_HISTORY_PATH", "history.json").strip()
    ok = bool(token and repo and "/" in repo and not repo.startswith("/"))
    return {"token": token, "repo": repo, "path": path, "ok": ok}


def _headers(token, extra=None):
    h = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "User-Agent": "model-price-board/1.0",
    }
    if extra:
        h.update(extra)
    return h


def _req(method, url, token, body=None, payload_is_json=True):
    """执行一次 GitHub API 请求，返回 (http_code, response_dict)。不抛网络异常由上层吞。"""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8") if payload_is_json else body
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return resp.status, {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return e.code, {}
    except Exception:  # 网络不可达 / 超时 等
        return None, {}


def fetch():
    """拉取远程 history 文件。返回 (payload, sha)；后端不可用或文件不存在返回 (None, None)。"""
    c = _conf()
    if not c["ok"]:
        return None, None
    url = "{}/repos/{}/contents/{}".format(API, c["repo"], c["path"])
    code, j = _req("GET", url, c["token"])
    if code != 200:
        return None, None  # 404=文件尚未建(视为 None,None)；其他错误同样按不可用/不存在处理
    try:
        content = base64.b64decode(j["content"]).decode("utf-8")
        payload = json.loads(content)
    except Exception:
        return None, None
    return payload, j.get("sha")


def push(payload, base_sha):
    """把最终 payload 写回远程。base_sha 为 None=远程尚无该文件（新建）。返回是否成功。"""
    c = _conf()
    if not c["ok"] or payload is None:
        return False
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = {
        "message": "chore(model-price-board): update price history sample",
        "content": base64.b64encode(content).decode("utf-8"),
    }
    if base_sha is not None:
        body["sha"] = base_sha
    url = "{}/repos/{}/contents/{}".format(API, c["repo"], c["path"])
    code, _ = _req("PUT", url, c["token"], body=body)
    return code in (200, 201)


def _push_status(payload, base_sha):
    """push 的状态化版本：返回 'ok' / 'conflict'(409, 值得重试) / 'fail'(其它，放弃重试)。"""
    c = _conf()
    if not c["ok"] or payload is None:
        return "fail"
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = {
        "message": "chore(model-price-board): update price history sample",
        "content": base64.b64encode(content).decode("utf-8"),
    }
    if base_sha is not None:
        body["sha"] = base_sha
    url = "{}/repos/{}/contents/{}".format(API, c["repo"], c["path"])
    code, _ = _req("PUT", url, c["token"], body=body)
    if code in (200, 201):
        return "ok"
    if code == 409:  # 并发 sha 冲突：另一次写入抢先，重拉合并再试有意义
        return "conflict"
    return "fail"


def put_latest(payload, max_tries=4):
    """带乐观锁的整文件写回：以「此刻远程最新 sha」为基准重写并合并 payload。

    仅对 409 并发冲突做有限重试（重拉最新 → 合并 → 再写）；网络错误或其它 4xx 属
    “后端暂不可用”，直接放弃重试立即返回，避免拖慢线上抓取/请求线程。
    """
    for attempt in range(max_tries):
        remote, sha = fetch()
        if remote is not None and sha is not None and _same_history(remote, payload):
            return True  # 远程已是该内容，无需再写
        merged = _merge_histories(remote, payload) if remote is not None else payload
        status = _push_status(merged, sha)
        if status == "ok":
            return True
        if status == "fail":
            return False  # 网络/权限等非冲突问题：重试也大概率失败，不空耗
        time.sleep(0.6 * (attempt + 1))  # 冲突：退避后重拉再试
    return False


def _same_history(a, b):
    return (a or {}).get("samples") == (b or {}).get("samples")


def _merge_histories(remote, local):
    """把两份 {samples:[...]} 按时间戳去重合并，时间正序。remote 为权威基线。"""
    base = (remote or {}).get("samples") or []
    extra = (local or {}).get("samples") or []
    by_t = {}
    for s in base + extra:
        key = s.get("t")
        if key:
            by_t[key] = s  # 同时间戳，后者(local 新合并的)覆盖，保证本机刚抓的样本在列
    merged = sorted(by_t.values(), key=lambda s: s.get("t", ""))
    return {"samples": merged}


if __name__ == "__main__":
    # 简易 CLI：python scripts/gh-history.py fetch  |  push
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if action == "fetch":
        p, sha = fetch()
        if p is None:
            print("（后端不可用 / 文件不存在）")
        else:
            print("sha =", sha)
            print("samples =", len((p or {}).get("samples") or []))
    elif action == "push":
        # 从本地 data/history.json 读入并整体上推
        here = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(os.path.dirname(here), "data", "history.json")
        with open(local_path, "r", encoding="utf-8") as f:
            local = json.load(f)
        print("push 结果 =", put_latest(local))
