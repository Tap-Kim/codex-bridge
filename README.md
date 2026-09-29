# codex-bridge

웹 ChatGPT 가 내 컴퓨터의 프로젝트를 직접 읽고, 고치고, 명령을 돌리고, 필요하면 로컬 Codex CLI 에 작업을 통째로 넘길 수 있게 하는 MCP 서버.
Codex 앱에서 하던 작업(사용량 한도로 멈춘 스레드 포함)을 ChatGPT 나 Claude Code 로 끌어와 이어가는 용도로도 쓴다.

## AI 에이전트에게 설치 맡기기

Claude Code 나 Codex CLI 에 아래 한 줄을 붙여 넣으면 된다. 에이전트가 필요한 것을 묻고, 스크립트로 할 수 있는 건 전부 하고, 브라우저에서 사람이 해야 할 단계만 안내한다.

```
https://github.com/Tap-Kim/codex-bridge 의 SETUP.md 를 읽고 그대로 설치해줘
```

> **AI 에이전트라면:** 설치 요청을 받았을 때 이 README 가 아니라 [SETUP.md](SETUP.md) 의 절차를 따른다. 원문: <https://raw.githubusercontent.com/Tap-Kim/codex-bridge/main/SETUP.md>

직접 설치하려면 아래를 순서대로 따라 하거나, 클론한 뒤 `CODEX_BRIDGE_ROOTS=... scripts/setup.sh` 한 번으로 1단계와 로컬 클라이언트 등록을 끝낼 수 있다.

```
웹 ChatGPT (커넥터, 인증 없음)
  -> OpenAI Secure MCP Tunnel
  -> tunnel-client (내 컴퓨터, Authorization: Bearer 토큰을 붙임, 헬스 포트 7422)
  -> codex-bridge  http://127.0.0.1:7421/mcp
  -> 파일 읽기/쓰기, 허용된 명령 실행, codex exec
```

구성은 두 개의 상시 프로세스다.

| 프로세스 | 역할 | 확인 |
|---|---|---|
| `codex-bridge` (이 저장소) | MCP 서버. 포트 `7421`, Bearer 토큰 필수 | `curl -s 127.0.0.1:7421/health` |
| `codex-tunnel` (OpenAI `tunnel-client`) | ChatGPT 와 내 컴퓨터를 잇는 아웃바운드 터널. 포트 `7422` 는 헬스 체크용 | `curl -s 127.0.0.1:7422/readyz` |

로컬 Codex 앱·CLI 나 Claude Code 에서만 쓸 거라면 터널은 필요 없다. 1단계만 하고 [다른 클라이언트에서 쓰기](#다른-클라이언트에서-쓰기) 로 넘어가면 된다.

> **보안 주의.** 연결된 ChatGPT 대화는 허용 루트 안의 파일을 읽고 쓰고, 허용 목록의 명령(`git`, `npm`, `node` 등)을 실행하고, `codex exec -s workspace-write` 를 돌릴 수 있다. 허용 루트는 꼭 필요한 폴더로 좁히고, 커넥터는 본인 계정에서만 쓴다. `.env*`, `*.pem`, `*.key`, `auth.json`, `.npmrc`, `.netrc`, `id_rsa*` 는 읽기·쓰기 모두 거부되고 `.git/` 안에는 쓰지 못한다.

## 준비물

- macOS 또는 Linux, Node.js 20 이상, git
- (선택) [Codex CLI](https://github.com/openai/codex) 설치 후 로그인 — `codex_*` 툴을 쓸 때 필요
- (웹 ChatGPT 연결 시) Tunnels 권한이 있는 OpenAI Platform 조직, ChatGPT 개발자 모드

## 1. 브릿지 실행

설치 없이 바로:

```bash
CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" npx -y github:Tap-Kim/codex-bridge
```

또는 클론해서:

```bash
git clone https://github.com/Tap-Kim/codex-bridge.git && cd codex-bridge
npm install
CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" npm start
```

`CODEX_BRIDGE_ROOTS` 는 ChatGPT 가 만질 수 있는 폴더 목록(콜론 구분, 절대 경로)이다. 생략하면 서버를 띄운 현재 폴더 하나만 연다.
첫 실행 때 `~/.codex-bridge/token`(64자, 권한 600) 이 생기고, 매 실행마다 tunnel-client 용 `~/.codex-bridge/auth_header`(`Bearer <token>`) 를 다시 쓴다.

### 상시 실행 (macOS)

클론한 경우 LaunchAgent 로 등록하면 로그인할 때 자동으로 뜨고 죽으면 다시 뜬다.

```bash
CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" scripts/install-launchd.sh   # 설치·갱신
scripts/install-launchd.sh uninstall                                     # 제거
tail -f ~/.codex-bridge/logs/local.codex-bridge.err.log
```

`~/.codex-bridge/tunnel.yaml` 이 있고 `tunnel-client` 가 PATH 에 있으면 터널도 같이 등록된다. 터널을 나중에 준비했다면 스크립트를 한 번 더 실행하면 된다.
두 프로세스는 따로 등록되므로 브릿지를 재시작해도 ChatGPT 커넥터 연결은 유지된다.

## 2. 웹 ChatGPT 연결 (Secure MCP Tunnel)

### 2-1. tunnel-client 설치

```bash
brew install openai/tools/tunnel-client
tunnel-client --version
```

brew 가 실패하면(예: 베타 macOS 의 Xcode 버전 요구) [Homebrew formula](https://github.com/openai/homebrew-tools/blob/main/Formula/tunnel-client.rb) 에 적힌 zip 을 받아 formula 의 sha256 과 대조한 뒤 PATH 에 둔다.

```bash
V=0.0.14; A=darwin-arm64   # 인텔 Mac: darwin-amd64, 리눅스: linux-amd64 | linux-arm64
curl -fLO "https://persistent.oaistatic.com/tunnel-client/v$V/tunnel-client-v$V-$A.zip"
shasum -a 256 "tunnel-client-v$V-$A.zip"   # formula 의 sha256 과 같아야 한다
unzip "tunnel-client-v$V-$A.zip" -d ~/.local/tunnel-client
export PATH="$HOME/.local/tunnel-client:$PATH"   # 셸 설정 파일에도 추가
```

zip 안의 `cloudflared` 가 `tunnel-client` 와 같은 폴더에 있어야 하므로 심볼릭 링크 대신 폴더를 PATH 에 넣는다.

### 2-2. 터널과 런타임 키 만들기

1. <https://platform.openai.com/settings/organization/tunnels> -> **Create tunnel** -> `tunnel_...` ID 복사
2. 같은 터널 설정의 **ChatGPT workspaces** 에 내 ChatGPT 계정 ID 를 추가한다. 이름 검색은 안 되고 **정확한 ID** 를 넣어야 한다. 개인 계정의 ID 는 로그인한 브라우저에서 <https://chatgpt.com/api/auth/session> 을 열었을 때 `account.id` 값(UUID). 이게 빠지면 커넥터 폼의 터널 목록에 아무것도 안 뜬다.
3. <https://platform.openai.com/settings/organization/api-keys> -> 이 용도 전용 **런타임** 키 발급(admin 키 아님). 키를 발급하는 계정에 Tunnels Read + Use 권한이 있어야 한다.

```bash
printf '%s' 'sk-...런타임키...' > ~/.codex-bridge/api_key && chmod 600 ~/.codex-bridge/api_key
```

### 2-3. 프로파일 작성 후 실행

[`tunnel.example.yaml`](tunnel.example.yaml) 을 `~/.codex-bridge/tunnel.yaml` 로 복사하고 `REPLACE_ME` 두 군데(터널 ID, 홈 경로)를 채운다.

```bash
cp tunnel.example.yaml ~/.codex-bridge/tunnel.yaml && chmod 600 ~/.codex-bridge/tunnel.yaml
# 편집 후
tunnel-client doctor --profile-file ~/.codex-bridge/tunnel.yaml --explain
tunnel-client run    --profile-file ~/.codex-bridge/tunnel.yaml
curl -s 127.0.0.1:7422/readyz   # 200 이면 준비 완료
```

상시 실행은 1단계의 `scripts/install-launchd.sh` 를 다시 실행하면 된다. 터널 ID 하나에는 tunnel-client 하나만 붙인다.

### 2-4. ChatGPT 에 커넥터 추가

tunnel-client 가 떠 있는 동안에만 만들 수 있고, 떠 있는 동안에만 동작한다.

1. chatgpt.com 설정 -> 보안 -> **개발자 모드** 켜기
2. <https://chatgpt.com/plugins> 상단의 **앱 만들기** (설정 화면이 아니라 이 페이지에 있다)
3. 이름 `codex-bridge` -> 연결 방식 **터널** -> 2-2 에서 만든 터널 선택 -> 인증 **인증 없음** -> 위험 고지 체크 -> **만들기** -> **연결하기**
4. 새 대화에서 커넥터를 켜고 "list_projects 로 프로젝트 목록 보여줘" 로 확인

인증 없음이어도 괜찮은 이유: ChatGPT 와 터널 사이는 OpenAI 가 워크스페이스로 인증하고, 터널과 브릿지 사이는 tunnel-client 가 Bearer 토큰을 붙인다. 브릿지는 토큰 없는 요청을 `401` 로 막는다.

## 다른 클라이언트에서 쓰기

터널 없이 `127.0.0.1:7421` 로 바로 붙는다. 토큰을 재발급했다면(`~/.codex-bridge/token` 삭제 후 재시작) 아래 설정의 토큰도 다시 넣어야 한다.

### Codex 앱·CLI

```bash
cat >> ~/.codex/config.toml <<EOF

[mcp_servers.codex-bridge]
url = "http://127.0.0.1:7421/mcp"
http_headers = { Authorization = "Bearer $(cat ~/.codex-bridge/token)" }
EOF
```

슬래시 명령 두 개를 같이 설치하면 편하다.

```bash
mkdir -p ~/.codex/prompts && cp codex/prompts/*.md ~/.codex/prompts/
```

| 명령 | 하는 일 |
|---|---|
| `/handoff` | 최신 Codex 스레드를 `codex_handoff` 로 가져와 현황 정리 + 다음 행동 제안 |
| `/handoff /abs/project/path` | 그 프로젝트의 최신 스레드 |
| `/handoff <thread_id>` | 특정 스레드 |
| `/bridge <할 일>` | 지시를 codex-bridge 툴만으로 수행 |

### Claude Code

```bash
claude mcp add --transport http codex-bridge http://127.0.0.1:7421/mcp \
  --header "Authorization: Bearer $(cat ~/.codex-bridge/token)"
```

"codex_handoff 로 Codex 에서 하던 작업 가져와서 이어서 해줘" 처럼 쓰면 된다.

## 툴

| 툴 | 설명 |
|---|---|
| `list_projects`, `list_dir`, `read_file`, `search` | 허용 루트 탐색·읽기 |
| `write_file`, `edit_file` | 파일 생성·수정 (`edit_file` 은 정확히 한 번 일치하는 문자열만 교체) |
| `run_command` | 셸 없이 허용 목록 실행파일만 실행. 기본 `git npm npx pnpm yarn node bun python3 pytest go cargo make ls cat grep tsc eslint vitest jest prettier gh` |
| `git_diff` | 커밋 안 된 diff (`staged`, 단일 `path` 옵션) |
| `codex_run` | `codex exec --json` 으로 작업 위임. `wait_sec`(기본 240) 안에 안 끝나면 `job_id` 를 돌려준다 |
| `codex_status`, `codex_cancel` | 위임한 작업 폴링·취소 |
| `codex_resume` | 기존 Codex 스레드에 이어서 프롬프트 |
| `codex_sessions` | `~/.codex/sessions` 의 최근 스레드 목록 |
| `codex_handoff` | Codex 에서 하던 작업 요약: 목표, 마지막 메시지, 멈춘 원인(사용량 한도 등), 고친 파일, 최근 명령, git 상태 |
| `codex_session_read` | 스레드 전체 대화록. 토큰·키 패턴은 `[REDACTED]` |

웹 ChatGPT 에는 MCP 프롬프트 메뉴가 없어서, 서버 안내문(instructions)에 Codex 식 명령을 넣어 두었다. 메시지를 `/goal <목표>`, `/review`, `/diff`, `/status`, `/plan <작업>`, `/resume [thread_id]` 로 시작하면 그대로 따른다.

Codex 작업을 이어받는 흐름:

1. "codex_handoff 로 Codex 에서 하던 작업 가져와" -> 스레드 요약과 git 상태
2. 더 필요하면 `codex_session_read` 로 대화록, `git_diff` 로 실제 변경
3. Codex 에 계속 맡기려면 `codex_resume`, 직접 하려면 `read_file` / `edit_file` / `run_command`

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CODEX_BRIDGE_ROOTS` | 실행한 폴더 | 접근 허용 폴더, 콜론 구분 |
| `CODEX_BRIDGE_PORT` | `7421` | 리슨 포트. 호스트는 `127.0.0.1` 고정 |
| `CODEX_BRIDGE_STATE` | `~/.codex-bridge` | 토큰, auth_header, 접근 로그 위치 |
| `CODEX_BRIDGE_ALLOW_CMDS` | 위 툴 표 참고 | `run_command` 허용 실행파일, 쉼표 구분 |
| `CODEX_BIN` | `codex` (Homebrew 경로 우선) | Codex CLI 경로 |
| `CODEX_HOME` | `~/.codex` | Codex 세션을 읽을 위치 |

툴 호출 기록(툴 이름과 경로만)은 `~/.codex-bridge/access.log` 에 쌓인다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 커넥터 폼의 터널 목록이 비어 있음 | 터널의 ChatGPT workspaces 에 `account.id` 를 정확히 넣었는지 (2-2 의 2번) |
| 앱 만들기 버튼이 없음 | 개발자 모드를 켠 뒤 설정이 아니라 <https://chatgpt.com/plugins> 상단을 본다 |
| `readyz` 가 503, 사유가 `oauth discovery failed ... connection refused` | 터널이 브릿지보다 먼저 떴다. `install-launchd.sh` 로 등록한 터널은 브릿지 `/health` 를 기다린 뒤 뜨므로 생기지 않는다. 직접 띄웠다면 브릿지가 뜬 뒤 터널만 재시작한다 |
| ChatGPT 가 툴을 못 부름 | `curl -s 127.0.0.1:7422/readyz` 가 200 인지, `curl -s 127.0.0.1:7421/health` 가 ok 인지 |
| `401 unauthorized` | 토큰을 바꿨는데 Codex·Claude 설정의 헤더를 안 바꾼 경우. tunnel-client 는 `auth_header` 파일을 읽으므로 브릿지 재시작만 하면 된다 |
| `outside allowed roots` | `CODEX_BRIDGE_ROOTS` 에 그 폴더가 없음. LaunchAgent 는 스크립트를 다시 실행해야 반영된다 |
| `codex_status` 가 unknown job | 브릿지가 재시작되어 메모리의 작업 목록이 사라짐. `codex_resume` 에 thread_id 를 넘기면 이어진다 |

## 개발

```bash
npm install
npm test          # 임시 폴더와 가짜 ~/.codex 로 서버를 띄워 툴 전체를 점검한다
```

- `server.mjs` 한 파일이 서버 전부다. 무상태 Streamable HTTP 로, 요청마다 새 `McpServer` 를 만든다
- `test.mjs` 는 실제 `~/.codex` 나 홈 디렉터리를 건드리지 않는다
- `scripts/setup.sh` 는 에이전트용 원샷 설치(재실행 안전), `scripts/install-launchd.sh` 는 LaunchAgent 등록만 한다. 설치 흐름을 바꾸면 [SETUP.md](SETUP.md) 도 같이 고친다
- PR 마다 GitHub Actions 가 macOS·Ubuntu 에서 `npm test` 를 돌린다

## 참고

[dreamurl/GPT-Bridge](https://github.com/dreamurl/GPT-Bridge), [xq3427/WebCodex](https://github.com/xq3427/WebCodex) 를 참고해 최소한으로 다시 만들었다. tunnel-client 는 [openai/tunnel-client](https://github.com/openai/tunnel-client) (Apache-2.0).

## 라이선스

MIT
