# -*- coding: utf-8 -*-
"""一次性工具：本机直连 github.com 被阻断时，经 api.github.com Contents API 上传站点文件。"""
import base64, json, os, sys, time, urllib.request, urllib.error

TOKEN = os.environ["GH_UPLOAD_TOKEN"]
REPO = "cong-art/ai-price-dashboard"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # scripts/ 的上级
BRANCH = "main"

SKIP_DIRS = {".git", "__pycache__", ".workbuddy", "node_modules"}
SKIP_FILES = {".env", ".DS_Store", "Thumbs.db"}
SKIP_EXT = {".pyc", ".pyo", ".tmp"}

API = "https://api.github.com"


def req(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + TOKEN)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "ai-price-dashboard-uploader")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body) if body else {}
        except Exception:
            return e.code, {}


def put_file(path, content_bytes, message):
    """单文件上传：先查 sha（存在则更新），再 PUT。"""
    api_path = API + "/repos/%s/contents/%s" % (REPO, path)
    sha = None
    st, cur = req("GET", api_path + "?ref=" + BRANCH)
    if st == 200:
        sha = cur.get("sha")
    elif st != 404:
        print("  !! GET %s -> %s %s" % (path, st, cur.get("message")))
        return False
    body = {
        "message": message,
        "branch": BRANCH,
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        body["sha"] = sha
    st, out = req("PUT", api_path, body)
    if st in (200, 201):
        return True
    print("  !! PUT %s -> %s %s" % (path, st, out.get("message")))
    return False


def main():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            files.append((rel, full))

    files.sort()
    ok = fail = 0
    for i, (rel, full) in enumerate(files, 1):
        with open(full, "rb") as f:
            data = f.read()
        if put_file(rel, data, "upload %s (via contents api)" % rel):
            ok += 1
            print("[%2d/%2d] ok  %s (%d B)" % (i, len(files), rel, len(data)))
        else:
            fail += 1
        time.sleep(0.3)
    print("\n完成: %d 成功, %d 失败, 共 %d 文件" % (ok, fail, len(files)))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
