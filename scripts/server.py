#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板线上服务：静态文件服务 + 新闻 / 价格「每日自更新」。

为什么需要它（而不是直接把静态目录发出去）：
  本机的 fetch-news.py（每天 08:00）与 fetch-prices.py（每 6 小时）由 Windows 计划任务
  在本机跑、写本地 data/*.json。但部署出去的静态快照只是「上传那一刻」的拷贝，不会自己变
  ——因此公网链接上的新闻/价格会一直停在发布那天。

  本服务把「定时抓取」搬进线上进程：
    - 进程内记录各类数据最后一次刷新的日期。
    - 每次收到请求时做一次惰性检查：若「现在已过该类数据的每日刷新时刻」且「today != 该类
      last_refresh_date」，则调用对应 fetch 模块的 refresh() 抓一次，原子写 data/*.json。
    - 无需外部定时器 / cron，只要有人访问（或服务常驻），到达刷新时刻后的第一个请求即自动
      补齐，之后当天其余请求直接命中缓存，不再重复抓。

  默认时刻：新闻 08:00、价格 09:00（均为北京时间）。可用 --news-hh / --price-hh 覆盖。

运行（本地/线上一致）：
    python scripts/server.py            # 监听 $PORT 或 8000，绑定 0.0.0.0
    PORT=8712 python scripts/server.py  # 指定端口
"""

import argparse
import http.server
import importlib.util
import json
import os
import socketserver
import sys
import threading
from datetime import datetime

# 加载同目录 fetch-news.py / fetch-prices.py（文件名带连字符，非合法模块名，用 importlib 按路径加载）
_HERE = os.path.dirname(os.path.abspath(__file__))
_modules = {}
for _fname, _mod in (("fetch-news.py", "fetch_news"), ("fetch-prices.py", "fetch_prices")):
    _path = os.path.join(_HERE, _fname)
    _spec = importlib.util.spec_from_file_location(_mod, _path)
    _m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    _modules[_mod] = _m

ROOT = os.path.dirname(_HERE)          # 仓库根（index.html / data/ 所在）
DATA_DIR = os.path.join(ROOT, "data")
INDEX = os.path.join(ROOT, "index.html")

# 每日刷新时刻（时:分，24h），北京时间
NEWS_HH, NEWS_MM = 8, 0
PRICE_HH, PRICE_MM = 9, 0
# 进程内「今天是否已刷新」各类别
_lock = threading.Lock()
_last_refresh = {}  # {'news': 'YYYY-MM-DD'|None, 'prices': ...}


def _today_str():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _due(kind, hh, mm):
    """当前是否已过每日刷新时刻，且今天进程内还没刷过。"""
    now = datetime.now().astimezone()
    if now.hour < hh or (now.hour == hh and now.minute < mm):
        return False
    return _last_refresh.get(kind) != _today_str()


def _refresh_one(kind, mod, hh, mm, force=False):
    """惰性/强制刷新某一类：kind∈{'news','prices'}。达时刻且当天未刷（或 force）时抓一次。"""
    global _last_refresh
    with _lock:
        if not force and not _due(kind, hh, mm):
            return
        try:
            if kind == "news":
                mod.refresh(days=2, write=True, quiet=True)
            else:
                mod.refresh(now=None, write=True, verbose=False)
            _last_refresh[kind] = _today_str()
            print(f"[server] {_today_str()} {kind} 已刷新", flush=True)
        except Exception as e:
            # 抓取失败不应让页面 500——保留旧数据，下个请求再试
            print(f"[server] {kind} 刷新失败（保留旧数据）：{type(e).__name__}: {e}", flush=True)


def maybe_refresh_all(force_news=False, force_prices=False):
    """对 news 与 prices 做惰性/强制刷新。force_* 用于启动时立即刷。"""
    # news 刷新
    if force_news:
        _refresh_one("news", _modules["fetch_news"], NEWS_HH, NEWS_MM, force=True)
    else:
        _refresh_one("news", _modules["fetch_news"], NEWS_HH, NEWS_MM)
    # prices 刷新
    if force_prices:
        _refresh_one("prices", _modules["fetch_prices"], PRICE_HH, PRICE_MM, force=True)
    else:
        _refresh_one("prices", _modules["fetch_prices"], PRICE_HH, PRICE_MM)


def _mime(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".md": "text/plain; charset=utf-8",
    }.get(ext, "application/octet-stream")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _no_cache(self):
        # 关键：news.json / prices.json 每次都要最新，禁止浏览器 HTTP 强缓存。
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def do_GET(self):
        # 每次请求都先尝试惰性刷新新闻与价格（达到每日时刻才真正抓，命中缓存则零开销）
        maybe_refresh_all()

        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            target = INDEX
            if not os.path.exists(target):
                self.send_error(404, "index.html not found")
                return
            self.send_response(200)
            self._no_cache()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(target, "rb") as f:
                self.wfile.write(f.read())
            return

        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path in ("/data/news.json", "/data/prices.json", "/data/history.json"):
            # 数据文件：读当前磁盘内容返回，且禁用缓存
            fp = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
            if not fp.startswith(ROOT) or not os.path.exists(fp):
                self.send_error(404)
                return
            self.send_response(200)
            self._no_cache()
            self.send_header("Content-Type", _mime(fp))
            self.end_headers()
            with open(fp, "rb") as f:
                self.wfile.write(f.read())
            return

        # 其余静态资源走 SimpleHTTPRequestHandler（含 304 支持；静态可缓存，无妨）
        return super().do_GET()

    # 禁用目录列表（避免泄露 data 结构）
    def list_directory(self, path):
        self.send_error(403, "Directory listing denied")
        return None


def main():
    ap = argparse.ArgumentParser(description="AI 价格资讯看板 · 线上自更新服务")
    ap.add_argument("--refresh-once", action="store_true",
                    help="启动时先立即刷新一次新闻与价格")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()

    # 服务启动就绪前先刷一次，保证首访即有当日/前一日最新数据（失败不阻塞启动）
    if args.refresh_once:
        maybe_refresh_all(force_news=True, force_prices=True)
    else:
        # 未显式要求，也尝试在启动瞬间补刷（若当前已在各自刷新时刻后且当天未刷）
        try:
            maybe_refresh_all()
        except Exception as e:
            print(f"[server] 启动补刷失败：{e}", flush=True)

    os.chdir(ROOT)
    handler = Handler
    # ThreadingHTTPServer 支持并发，避免惰性抓取期间阻塞其他请求太久
    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = args.port
    try:
        httpd = Server(("0.0.0.0", port), handler)
    except OSError as e:
        print(f"[server] 端口 {port} 不可用：{e}", file=sys.stderr)
        sys.exit(1)
    print(f"[server] 看板服务运行于 http://0.0.0.0:{port} "
          f"（新闻每日 {NEWS_HH:02d}:{NEWS_MM:02d}、价格每日 {PRICE_HH:02d}:{PRICE_MM:02d} 起自动刷新）",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
