#!/usr/bin/env python3
"""Minimal HTTP forward proxy. Runs INSIDE the FreeHop netns and only
forwards traffic for clients that point at it (your gateway), so the
rest of the machine keeps its normal connection.
Supports CONNECT (https) and absolute-URI http. No logging of content.
Usage: freehop-proxy.py [bind-host] [port]
Defaults: $FREEHOP_PROXY_HOST or 0.0.0.0, $FREEHOP_PROXY_PORT or 18080.
"""
import os, socket, sys, threading
HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FREEHOP_PROXY_HOST", "0.0.0.0")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("FREEHOP_PROXY_PORT", "18080"))
def relay(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data: break
            b.sendall(data)
    except Exception:
        pass
    finally:
        try: b.shutdown(socket.SHUT_WR)
        except Exception: pass
def handle(client):
    try:
        req = b""
        while b"\r\n\r\n" not in req and len(req) < 65536:
            chunk = client.recv(4096)
            if not chunk: return
            req += chunk
        head, _, rest = req.partition(b"\r\n\r\n")
        line = head.split(b"\r\n")[0].decode(errors="ignore")
        parts = line.split(" ")
        if len(parts) < 2: return
        method, target = parts[0], parts[1]
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
            upstream = socket.create_connection((host, port), timeout=30)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            t1 = threading.Thread(target=relay, args=(client, upstream), daemon=True)
            t2 = threading.Thread(target=relay, args=(upstream, client), daemon=True)
            t1.start(); t2.start(); t1.join(); t2.join()
            upstream.close()
        else:
            # absolute URI form: http://host:port/path (port may be omitted)
            if target.startswith("http://"):
                u = target[7:]
                hostport, _, path = u.partition("/")
                if ":" in hostport:
                    host, _, port = hostport.rpartition(":")
                    port = int(port or 80)
                else:
                    host, port = hostport, 80
                path = "/" + path
            else:
                return
            upstream = socket.create_connection((host, port), timeout=30)
            newhead = head.replace(target.encode(), path.encode(), 1)
            upstream.sendall(newhead + b"\r\n\r\n" + rest)
            t1 = threading.Thread(target=relay, args=(client, upstream), daemon=True)
            t2 = threading.Thread(target=relay, args=(upstream, client), daemon=True)
            t1.start(); t2.start(); t1.join(); t2.join()
            upstream.close()
    except Exception as e:
        print(f"proxy err: {e}", flush=True)
    finally:
        try: client.close()
        except Exception: pass
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((HOST, PORT)); srv.listen(50)
print(f"freehop forward proxy listening on {HOST}:{PORT}", flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
