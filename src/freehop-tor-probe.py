#!/usr/bin/env python3
"""Probe the Tor daemon FreeHop uses for the tor egress method.
Checks the control port (AUTHENTICATE, no password) and the SOCKS port
(a TCP connect). Exit 0 if both answer, 1 otherwise. No secrets.
Usage: freehop-tor-probe.py [control-host:port] [socks-host:port]
Defaults: 127.0.0.1:9051 and 127.0.0.1:9050.
"""
import socket, sys

def check_control(host, port):
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(b"AUTHENTICATE\r\n")
    data = s.recv(64)
    s.close()
    return data.startswith(b"250")

def check_socks(host, port):
    s = socket.create_connection((host, port), timeout=5)
    s.close()
    return True

if __name__ == "__main__":
    control = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:9051"
    socks = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1:9050"
    ch, _, cp = control.rpartition(":")
    sh, _, sp = socks.rpartition(":")
    try:
        ok_c = check_control(ch, int(cp or 9051))
    except Exception as e:
        print(f"control port failed: {e}")
        ok_c = False
    try:
        ok_s = check_socks(sh, int(sp or 9050))
    except Exception as e:
        print(f"socks port failed: {e}")
        ok_s = False
    print(f"control: {'ok' if ok_c else 'FAIL'}, socks: {'ok' if ok_s else 'FAIL'}")
    sys.exit(0 if (ok_c and ok_s) else 1)
