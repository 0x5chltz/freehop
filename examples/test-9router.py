#!/usr/bin/env python3
"""Example TEST_CMD for freehop-watch.py when your gateway is 9Router.
Reads the active API key from the 9Router SQLite DB (read-only) and makes
one small chat completion against the local gateway. Exit 0 on HTTP 200.
The key is used transiently and never printed.
Env: FREEHOP_9ROUTER_DB, FREEHOP_TEST_URL, FREEHOP_TEST_MODEL.

Probe mode (--probe): prints only the numeric HTTP status (200, 429, ...)
and always exits 0 (000 when the gateway cannot be reached at all).
That is what the watcher uses as PROBE_CMD: it needs the status code,
not just pass/fail, so it can tell a 429 limit from a 502 or a refused
connection. Default mode is unchanged (prints "HTTP <code>", exit 0
only on 200) and stays the TEST_CMD post-rotate check.
"""
import json, os, sqlite3, sys, urllib.error, urllib.request

DB = os.environ.get("FREEHOP_9ROUTER_DB", os.path.expanduser("~/.9router/db/data.sqlite"))
URL = os.environ.get("FREEHOP_TEST_URL", "http://127.0.0.1:20129/v1/chat/completions")
MODEL = os.environ.get("FREEHOP_TEST_MODEL", "oc/deepseek-v4.1-flash-free")

PROBE = "--probe" in sys.argv

try:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = list(con.execute("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1"))
    con.close()
except Exception:
    rows = []
if not rows:
    if PROBE:
        print("000")
        sys.exit(0)
    print("no active api key")
    sys.exit(1)
key = str(rows[0][0])
payload = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "2+2?"}], "max_tokens": 10}).encode()
req = urllib.request.Request(URL, data=payload,
                            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                            method="POST")
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        status = resp.status
except urllib.error.HTTPError as e:
    status = e.code
except Exception:
    status = 0
if PROBE:
    print(status if status else "000")
    sys.exit(0)
print(f"HTTP {status}")
sys.exit(0 if status == 200 else 1)
