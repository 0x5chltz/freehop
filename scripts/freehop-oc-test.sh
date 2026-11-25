#!/usr/bin/env bash
# FreeHop post-rotate verification for the OpenCode free tier.
#
# The OpenCode Console free quota is applied per public IP. Proton *free*
# servers are shared, so a freshly rotated exit is often already burned.
# This probe asks the free model a one-word question through the current
# exit and reports whether the exit is usable:
#   exit 0 -> usable (watcher stops rotating)
#   exit 1 -> still limited (watcher rotates again, up to its attempt cap)
#
# Costs one free message per probe; cheap next to a dead exit.
set -u
PROXY="${FREEHOP_PROXY:-http://10.200.0.2:18080}"
REAL="${FREEHOP_OC_REAL:-/usr/bin/opencode}"
MODEL="${FREEHOP_OC_MODEL:-opencode/deepseek-v4.1-flash-free}"

out=$(HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" ALL_PROXY="$PROXY" \
      NO_PROXY=127.0.0.1,localhost,::1 \
      timeout 70 "$REAL" run --standalone --archived -m "$MODEL" "reply with exactly: ok" 2>&1)
rc=$?

if printf '%s' "$out" | grep -qi "free usage limit"; then
  echo "exit still limited"
  exit 1
fi
if printf '%s' "$out" | grep -qiE "econnrefused|transport error|proxy"; then
  echo "transport problem via exit"
  exit 1
fi
if [ "$rc" -eq 124 ]; then
  echo "probe timed out"
  exit 1
fi
echo "exit usable"
exit 0
