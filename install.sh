#!/bin/bash
# Ollama Monitor — ติดตั้งทั้งระบบในคำสั่งเดียว
#   1) host agent (launchd, auto-start) สำหรับ metrics + control บริการบนเครื่อง
#   2) dashboard ใน Docker (compose)
set -e
cd "$(dirname "$0")"

LABEL="com.ollama-monitor.host-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"
DIR="$PWD"

echo ">> [1/3] ติดตั้ง host agent (launchd)..."
mkdir -p "$HOME/Library/LaunchAgents"
# เผื่อมี agent เวอร์ชันเก่ารันอยู่ (เช่น start ด้วยมือ) ให้ปิดก่อน ไม่งั้น bind port ซ้ำ
pkill -f ollama_host_agent.py 2>/dev/null || true
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/host-agent/ollama_host_agent.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/ollama-monitor-host-agent.log</string>
  <key>StandardErrorPath</key><string>/tmp/ollama-monitor-host-agent.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# รอ agent พร้อมตอบ (polling สูงสุด ~20 วิ) — launchd spawn อาจช้ากว่า 1 วินาที
AGENT_OK=""
for i in $(seq 1 40); do
  if curl -sf --noproxy '*' --max-time 1 http://127.0.0.1:9021/health >/dev/null 2>&1; then
    AGENT_OK=1; break
  fi
  sleep 0.5
done
[ -n "$AGENT_OK" ] && echo "   host agent OK (http://127.0.0.1:9021)" \
  || { echo "   host agent ยังไม่ตอบ — ดู log ที่ /tmp/ollama-monitor-host-agent.log"; exit 1; }

echo ">> [2/3] Build + รัน dashboard ใน Docker..."
docker compose build
docker compose up -d

echo ">> [3/3] ตรวจสอบ..."
DASH_OK=""
for i in $(seq 1 30); do
  if curl -sf --noproxy '*' --max-time 2 http://127.0.0.1:9020/api/overview >/dev/null 2>&1; then
    DASH_OK=1; break
  fi
  sleep 1
done
[ -n "$DASH_OK" ] && echo "   dashboard OK (http://localhost:9020)" \
  || { echo "   dashboard ยังไม่ตอบ — ดู log: docker compose logs"; exit 1; }

echo ""
echo "เสร็จแล้ว ✅  เปิด http://localhost:9020"
