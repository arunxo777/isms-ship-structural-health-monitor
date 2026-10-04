#!/bin/bash
echo "=== ollama ps ==="
ollama ps
echo
echo "=== llama threads ==="
LPID=$(pgrep -n llama-server)
if [ -n "$LPID" ]; then
  echo "pid=$LPID"
  ps -L -p "$LPID" -o pid,tid,pcpu,stat,comm | head -40
  echo "thread count: $(ps -L -p "$LPID" --no-headers | wc -l)"
fi
echo
echo "=== ollama journal ==="
journalctl -u ollama -n 25 --no-pager
echo
echo "=== renderer pids ==="
ps -eo pid,pcpu,rss,args | grep 'type=renderer' | grep -v grep
echo
echo "=== who ==="
who
echo
echo "=== ollama env ==="
systemctl show ollama -p Environment --value
echo
echo "=== top cpu now ==="
ps -eo pid,pcpu,pmem,rss,comm --sort=-pcpu | head -12
