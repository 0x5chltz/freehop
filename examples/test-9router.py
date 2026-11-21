#!/usr/bin/env python3
"""Example TEST_CMD for freehop-watch.py when your gateway is 9Router.
Reads the active API key from the 9Router SQLite DB (read-only) and makes
one small chat completion against the local gateway. Exit 0 on HTTP 200.
The key is used transiently and never printed.
Env: FREEHOP_9ROUTER_DB, FREEHOP_TEST_URL, FREEHOP_TEST_MODEL.
"""
import json, os, sqlite3, sys, urllib.error, urllib.request

DB = os.environ.get("FREEHOP_9ROUTER_DB", os.path.expanduser("~/.9router/db/data.sqlite"))
URL = os.environ.get("FREEHOP_TEST_URL", "http://127.0.0.1:20129/v1/chat/completions")
MODEL = os.environ.get("FREEHOP_TEST_MODEL", "oc/deepseek-v4.1-flash-free")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = list(con.execute("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1"))
con.close()
if not rows:
    print("no active api key")
    sys.exit(1)
key = str(rows[0][0])
payload = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "2+2?"}], "max_tokens": 10}).encode()
req = urllib.request.Request(URL, data=payload,
                            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                            method="POST")
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        print(f"HTTP {resp.status}")
        sys.exit(0 if resp.status == 200 else 1)
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}")
    sys.exit(1)
