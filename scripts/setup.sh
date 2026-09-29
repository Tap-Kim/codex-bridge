#!/bin/sh
# One-shot, re-runnable setup. Does every step a script can do, then prints what only the human can do.
# Run it again after the human finishes a step; finished steps are skipped.
#
#   CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" scripts/setup.sh                    # bridge + local clients
#   CODEX_BRIDGE_ROOTS="$HOME/work" TUNNEL_ID=tunnel_... scripts/setup.sh          # + web ChatGPT tunnel
#
# Optional: CODEX_BRIDGE_CLIENTS="codex claude" (local clients to register; "" for none)
set -eu
DIR=$(cd "$(dirname "$0")/.." && pwd)
STATE=${CODEX_BRIDGE_STATE:-$HOME/.codex-bridge}
PORT=${CODEX_BRIDGE_PORT:-7421}
URL=http://127.0.0.1:$PORT/mcp
CODEX_DIR=${CODEX_HOME:-$HOME/.codex}
CLIENTS=${CODEX_BRIDGE_CLIENTS-codex claude}
TODO=""
ok()   { printf '[ok]   %s\n' "$*"; }
skip() { printf '[skip] %s\n' "$*"; }
fail() { printf '[fail] %s\n' "$*"; exit 1; }
todo() { printf '[todo] %s\n' "$*"; TODO="$TODO
- $*"; }
up()   { n=0; until curl -fs -o /dev/null "$1"; do n=$((n + 1)); [ "$n" -ge "$2" ] && return 1; sleep 1; done; }

: "${CODEX_BRIDGE_ROOTS:?ask the user which folders ChatGPT may touch, then set CODEX_BRIDGE_ROOTS=/abs/a:/abs/b}"
export CODEX_BRIDGE_ROOTS CODEX_BRIDGE_STATE="$STATE" CODEX_BRIDGE_PORT="$PORT"

# 1. prerequisites, dependencies, token
command -v node >/dev/null || fail "node not found (need 20+)"
[ "$(node -p 'process.versions.node.split(".")[0]')" -ge 20 ] || fail "node 20+ required, found $(node -v)"
command -v git >/dev/null || fail "git not found"
(cd "$DIR" && npm ci --omit=dev --no-audit --no-fund --silent) || fail "npm ci failed in $DIR"
node -e "import('$DIR/server.mjs')" # importing (not running) creates $STATE/token and auth_header
TOKEN=$(cat "$STATE/token")
ok "dependencies, token ($STATE/token)"

# 2. bridge as a service
if [ "$(uname)" = Darwin ]; then
  "$DIR/scripts/install-launchd.sh" >/dev/null
  up "http://127.0.0.1:$PORT/health" 20 || fail "bridge not healthy on :$PORT, see $STATE/logs/local.codex-bridge.err.log"
  ok "bridge running at $URL (LaunchAgent local.codex-bridge, roots: $CODEX_BRIDGE_ROOTS)"
elif up "http://127.0.0.1:$PORT/health" 1; then
  ok "bridge already running at $URL"
else
  todo "no launchd here: keep 'CODEX_BRIDGE_ROOTS=$CODEX_BRIDGE_ROOTS node $DIR/server.mjs' running (systemd --user, tmux...), then re-run"
fi

# 3. local clients (connect straight to 127.0.0.1, no tunnel)
for c in $CLIENTS; do
  case $c in
    codex)
      if ! command -v codex >/dev/null; then skip "Codex CLI not installed"; continue; fi
      CFG=$CODEX_DIR/config.toml
      if grep -q '^\[mcp_servers\.codex-bridge\]' "$CFG" 2>/dev/null; then
        if grep -q "Bearer $TOKEN" "$CFG"; then ok "Codex already registered"
        else todo "$CFG has [mcp_servers.codex-bridge] with an old token: set http_headers Authorization to \"Bearer \$(cat $STATE/token)\""; fi
      else
        mkdir -p "$CODEX_DIR"
        printf '\n[mcp_servers.codex-bridge]\nurl = "%s"\nhttp_headers = { Authorization = "Bearer %s" }\n' "$URL" "$TOKEN" >> "$CFG"
        ok "Codex registered in $CFG (restart the Codex app to load it)"
      fi
      mkdir -p "$CODEX_DIR/prompts"
      for f in "$DIR"/codex/prompts/*.md; do [ -e "$CODEX_DIR/prompts/${f##*/}" ] || cp "$f" "$CODEX_DIR/prompts/"; done
      ok "Codex slash commands /handoff /bridge ($CODEX_DIR/prompts)"
      ;;
    claude)
      if ! command -v claude >/dev/null; then skip "Claude Code not installed"; continue; fi
      if claude mcp get codex-bridge >/dev/null 2>&1; then ok "Claude Code already registered"
      else
        claude mcp add --scope user --transport http codex-bridge "$URL" --header "Authorization: Bearer $TOKEN" >/dev/null
        ok "Claude Code registered (user scope)"
      fi
      ;;
  esac
done

# 4. web ChatGPT: tunnel-client + profile + LaunchAgent
if [ -z "${TUNNEL_ID:-}" ]; then
  skip "web ChatGPT tunnel (re-run with TUNNEL_ID=tunnel_... once SETUP.md step 3 is done)"
else
  TC=${TUNNEL_CLIENT:-$(command -v tunnel-client || true)}
  if [ -z "$TC" ] && command -v brew >/dev/null; then
    brew install openai/tools/tunnel-client >/dev/null 2>&1 || true
    TC=$(command -v tunnel-client || true)
  fi
  if [ -z "$TC" ]; then
    todo "install tunnel-client (README 2-1, zip + sha256), then re-run"
  elif [ ! -s "$STATE/api_key" ]; then
    todo "save the runtime API key: copy it in the browser, then run  pbpaste | tr -d '\\n' > $STATE/api_key && chmod 600 $STATE/api_key"
  else
    chmod 600 "$STATE/api_key"
    if [ ! -f "$STATE/tunnel.yaml" ]; then
      sed -e "s#tunnel_REPLACE_ME#$TUNNEL_ID#" -e "s#/Users/REPLACE_ME/.codex-bridge#$STATE#g" -e "s#127.0.0.1:7421#127.0.0.1:$PORT#" \
        "$DIR/tunnel.example.yaml" > "$STATE/tunnel.yaml.new"
      chmod 600 "$STATE/tunnel.yaml.new"
      if "$TC" doctor --profile-file "$STATE/tunnel.yaml.new" --explain > "$STATE/doctor.log" 2>&1; then
        mv "$STATE/tunnel.yaml.new" "$STATE/tunnel.yaml"; ok "tunnel profile $STATE/tunnel.yaml (doctor passed)"
      else
        todo "tunnel-client doctor failed ($(grep '^FAILED_CHECKS' "$STATE/doctor.log" || echo 'see log')), details in $STATE/doctor.log; fix, then re-run"
      fi
    elif grep -q "$TUNNEL_ID" "$STATE/tunnel.yaml"; then ok "tunnel profile $STATE/tunnel.yaml"
    else todo "$STATE/tunnel.yaml exists with a different tunnel id; move it aside to regenerate, then re-run"; fi

    if [ -f "$STATE/tunnel.yaml" ] && [ "$(uname)" = Darwin ]; then
      TUNNEL_CLIENT=$TC "$DIR/scripts/install-launchd.sh" >/dev/null
      if up http://127.0.0.1:7422/readyz 45; then
        ok "tunnel ready (LaunchAgent local.codex-bridge.tunnel, readyz 200)"
        todo "ChatGPT: add your account.id (https://chatgpt.com/api/auth/session) to the tunnel's ChatGPT workspaces, if not done yet"
        todo "ChatGPT: Settings -> Security -> Developer mode ON, then https://chatgpt.com/plugins -> Create app -> connection Tunnel -> pick this tunnel -> auth None -> Create -> Connect"
      else
        todo "tunnel not ready: $(curl -s http://127.0.0.1:7422/readyz | head -c 300); log: $STATE/logs/local.codex-bridge.tunnel.out.log"
      fi
    elif [ -f "$STATE/tunnel.yaml" ]; then
      todo "keep '$TC run --profile-file $STATE/tunnel.yaml' running after the bridge is up"
    fi
  fi
fi

if [ -n "$TODO" ]; then printf '\nLEFT FOR THE HUMAN:%s\n' "$TODO"; else printf '\nDONE\n'; fi
