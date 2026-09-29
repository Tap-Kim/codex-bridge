#!/bin/sh
# Keep codex-bridge running on macOS as a LaunchAgent (restarts on crash and login).
# The tunnel-client agent is added too once ~/.codex-bridge/tunnel.yaml exists.
#
#   CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" scripts/install-launchd.sh
#   scripts/install-launchd.sh uninstall
set -eu
DIR=$(cd "$(dirname "$0")/.." && pwd)
STATE=${CODEX_BRIDGE_STATE:-$HOME/.codex-bridge}
AGENTS=$HOME/Library/LaunchAgents
DOMAIN=gui/$(id -u)
BRIDGE=local.codex-bridge
TUNNEL=local.codex-bridge.tunnel

remove() { launchctl bootout "$DOMAIN/$1" 2>/dev/null || true; rm -f "$AGENTS/$1.plist"; }

if [ "${1:-}" = uninstall ]; then
  remove "$BRIDGE"; remove "$TUNNEL"; echo "removed $BRIDGE, $TUNNEL"; exit 0
fi

: "${CODEX_BRIDGE_ROOTS:?set CODEX_BRIDGE_ROOTS to the folders ChatGPT may touch, e.g. \$HOME/work:\$HOME/side}"
mkdir -p "$AGENTS" "$STATE/logs"

# agent <label> <program> [args...]: write the plist, then (re)load it.
# PATH is copied from this shell so launchd finds node, git and codex.
agent() {
  label=$1; shift
  args=""; for a in "$@"; do args="$args<string>$a</string>"; done
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEX_BRIDGE_ROOTS</key><string>$CODEX_BRIDGE_ROOTS</string>
    <key>CODEX_BRIDGE_STATE</key><string>$STATE</string>
    <key>PATH</key><string>$PATH</string>
  </dict>
  <key>WorkingDirectory</key><string>$STATE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$STATE/logs/$label.out.log</string>
  <key>StandardErrorPath</key><string>$STATE/logs/$label.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
  echo "loaded $label (logs: $STATE/logs/$label.*.log)"
}

agent "$BRIDGE" "$(command -v node)" "$DIR/server.mjs"

TC=${TUNNEL_CLIENT:-$(command -v tunnel-client || true)}
if [ -n "$TC" ] && [ -f "$STATE/tunnel.yaml" ]; then
  agent "$TUNNEL" "$TC" run --profile-file "$STATE/tunnel.yaml"
else
  echo "skipped tunnel: needs tunnel-client on PATH and $STATE/tunnel.yaml (README, ChatGPT 연결 절)"
fi
