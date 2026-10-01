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

- macOS 또는 Linux, Node.js 20 이상, Python 3.9 이상, git
- (선택) [Codex CLI](https://github.com/openai/codex) 설치 후 로그인 — `codex_*` 툴을 쓸 때 필요
- (웹 ChatGPT 연결 시) Tunnels 권한이 있는 OpenAI Platform 조직, ChatGPT 개발자 모드

## 1. 브릿지 실행

설치 없이 바로:

```bash
CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" npx -y github:Tap-Kim/codex-bridge
```

또는 클론해서:

```bash
git clone https://github.com/Tap-Kim/codex-bridge.git ~/codex-bridge && cd ~/codex-bridge
npm install
CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" npm start
```

`CODEX_BRIDGE_ROOTS` 는 ChatGPT 가 만질 수 있는 폴더 목록(콜론 구분, 절대 경로)이다. 생략하면 서버를 띄운 현재 폴더 하나만 연다.
첫 실행 때 `~/.codex-bridge/token`(64자, 권한 600) 이 생기고, 매 실행마다 tunnel-client 용 `~/.codex-bridge/auth_header`(`Bearer <token>`) 를 다시 쓴다.

### 상시 실행 (macOS)

클론한 경우 LaunchAgent 로 등록하면 로그인할 때 자동으로 뜨고 죽으면 다시 뜬다.

```bash
CODEX_BRIDGE_ROOTS="$HOME/work:$HOME/side" scripts/install-launchd.sh   # 설치·갱신
scripts/install-launchd.sh restart                                       # 브릿지만 안전하게 재시작
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
| `codex_loop_start` | 목표 + 검증 명령으로 영속적인 Loop Engineering 작업 시작. detached Python worker가 Codex 실행→외부 검증→실패 시 같은 스레드 resume 반복 |
| `codex_loop_status` | 디스크 상태와 heartbeat/activity/progress, worker/child PID, watchdog 이력을 조회·폴링 |
| `codex_loop_resume`, `codex_loop_cancel` | 중단된 loop 재개 또는 활성 loop 취소. flock으로 중복 worker를 막고 취소 상태는 재시작되지 않게 영속화 |
| `codex_graph_start` | dependency DAG를 받아 독립 task들을 격리된 git worktree에서 병렬 실행하고 integration worktree로 합침 |
| `codex_graph_status` | task별 loop/worktree/commit/integration 상태, peak 병렬도, 최종 검증 상태 조회·폴링 |
| `codex_graph_resume`, `codex_graph_cancel` | 중단/integration-ready graph 재개 또는 모든 활성 child loop와 graph 취소 |
| `codex_sessions` | `~/.codex/sessions` 의 최근 스레드 목록 |
| `codex_handoff` | Codex 에서 하던 작업 요약: 목표, 마지막 메시지, 멈춘 원인(사용량 한도 등), 고친 파일, 최근 명령, git 상태 |
| `codex_session_read` | 스레드 전체 대화록. 토큰·키 패턴은 `[REDACTED]` |
| `orca_daemon_status` | 현재 실행 중인 Orca terminal daemon(`Orca Helper ... daemon-entry.js`) PID와 상태 조회 |
| `orca_daemon_restart` | `confirm=true`일 때만 정확히 일치한 Orca terminal daemon PID를 TERM→필요시 KILL 순서로 재시작하고 새 PID 확인. 실행 중인 Orca 터미널/에이전트 세션이 끊길 수 있음 |

웹 ChatGPT 에는 MCP 프롬프트 메뉴가 없어서, 서버 안내문(instructions)에 Codex 식 명령을 넣어 두었다. 메시지를 `/goal <목표>`, `/review`, `/diff`, `/status`, `/plan <작업>`, `/resume [thread_id]` 로 시작하면 그대로 따른다.

### Loop Engineering

긴 작업은 `codex_loop_start` 에 목표와 **명시적인 검증 명령**을 함께 넘긴다. MCP 서버는 실행만 요청하고 실제 loop는 별도 Python worker가 맡는다. worker는 Codex 실행 → 검증 → 실패 원인 요약 → 같은 `thread_id` resume 을 `max_attempts` 범위에서 반복한다. 검증 명령은 `run_command` 와 같은 allowlist를 사용하고 셸을 거치지 않는다.

상태는 `CODEX_BRIDGE_STATE/loops/<loop_id>.json` 에 원자적으로 저장되고 worker는 loop별 flock을 잡는다. 그래서 ChatGPT 연결이나 Node 브릿지가 재시작돼도 이미 실행 중인 worker는 계속 동작한다. worker 자체가 죽은 경우에는 다음 브릿지 시작 시 stale active loop를 찾아 checkpoint와 저장된 `thread_id` 로 자동 복구한다. thread가 만들어지기 전에 끊긴 경우에도 같은 `loop_id` 를 유지한 채 새 Codex 실행으로 이어간다.

worker는 기본 5초마다 heartbeat를 기록하고, Codex stdout/stderr activity가 기본 300초 동안 없으면 hang으로 판정한다. hang이면 먼저 SIGTERM, 종료되지 않으면 SIGKILL로 정리한 뒤 같은 thread를 다시 resume한다. `watchdog_interval_sec`, `hang_timeout_sec`, `terminate_grace_sec` 은 loop 시작 시 조정할 수 있다. 취소는 별도 cancel marker로 먼저 영속화해 watchdog과 race가 나도 다시 실행되지 않는다.

검증 명령을 생략한 loop는 Codex의 자체 완료 메시지만으로 성공 처리하지 않고 `verification_required` 에서 멈춘다. 이후 `codex_loop_resume` 에 `verify_commands` 를 전달해 외부 검증을 추가할 수 있다. 검증 실패나 hang/process failure가 `max_attempts` 에 도달하면 무한 반복하지 않고 `blocked` 로 종료한다.

같은 오류 fingerprint가 반복되면 단순 retry 대신 전략을 단계적으로 바꾼다. 2회째에는 이전 수정 방식 재사용을 금지하고 다른 구현 접근을 요구하고, 3회째에는 가정과 실제 런타임 증거를 다시 확인하는 root-cause reset, 4회째부터는 구현 경계·의존성·제어 흐름까지 재검토하는 architecture escalation 지시를 다음 Codex resume prompt에 넣는다. 현재 단계와 반복 횟수는 loop state의 `strategy_level`, `strategy_repeat_count`, `failure_fingerprints` 에 남는다.

### MCP 응답 대기와 실제 작업 시간

`codex_run/resume/status`, Loop/Graph의 시작·재개·상태 조회는 요청한 `wait_sec`가 길어도 한 호출에서 최대 20초만 기다린다. 완료되지 않았으면 job/loop/graph ID와 현재 상태를 먼저 반환하고, 작업은 계속 진행한다. 해당 status 도구로 짧게 폴링한다. `CODEX_BRIDGE_MAX_WAIT_SEC`로 이 응답 대기를 더 짧게 조정할 수 있다 (0.1~20초).

`run_command`도 서버를 차단하지 않고 실행한다. 빠른 명령은 기존 출력 문자열을 반환하고, 긴 명령은 `kind=command`, `job_id`, `status=running`을 반환한다. `codex_status(job_id)`로 완료 출력(`final_message`)을 확인하거나 `codex_cancel(job_id)`로 취소한다. `timeout_sec`는 실제 명령 실행 제한이며 짧은 MCP 응답 대기와 별개다.

ChatGPT에서 호출 시간이 초과돼도 작업이 반드시 실패한 것은 아니다. 이미 받은 ID로 상태를 먼저 확인하고, 동일 목표를 새 작업으로 중복 실행하지 않는다. 장기 작업은 상태가 저장되는 Loop/Graph를 사용한다. 일반 job/command ID와 완료 결과는 재시작 후에도 조회할 수 있지만 중단된 실행은 자동 재실행하지 않는다.

### PC 자동 복구 감시

기존 브릿지·터널을 설치한 macOS에서 `python3 scripts/install-recovery.py`를 실행하면 PC에서 30초마다 점검하는 독립 LaunchAgent를 등록한다. daimon 사용 시 `bridge-recovery` scheduled job으로 나타난다. ChatGPT 연결이 없어도 실행된다. macOS의 Documents TCC 제한을 피하기 위해 watchdog과 supervisor 런타임 복사본은 `~/.codex-bridge/recovery-runtime`에서 실행하며, 설치 스크립트를 다시 실행하면 현재 저장소 버전으로 갱신된다.

- 3회 연속 readiness 실패 시 해당 서비스만 복구한다. 브릿지가 정상화되기 전에 터널을 재시작하지 않는다.
- 재시작은 최소 5분 간격, backoff 최대 30분, 전체 시간당 최대 3회로 제한한다. 반복 실패는 `attention_required`로 남긴다.
- 브릿지의 진행 작업·새 작업 유입을 확인한다. 재시작 준비 시 새 요청을 잠깐 차단하며, 살아 있는 작업 또는 확인할 수 없는 상태에서는 강제 재시작하지 않는다.
- 완료 결과와 job ID는 private `CODEX_BRIDGE_STATE/jobs`에 저장한다. 응답을 놓쳤으면 `codex_jobs`로 ID를 찾고 `codex_status`로 조회한다. 중단된 일반 job은 `interrupted`로 보고하고 자동 재실행하지 않는다. 저장된 Loop/Graph는 기존 ID와 flock·attempt 제한·cancel marker에 따라 복구한다.
- `codex_recovery_status`로 상태를 조회한다. 건강한 서비스를 단일 502나 ChatGPT 화면 오류만으로 재시작하지 않는다. ChatGPT 자체 스트림 복구는 로컬 감시가 보장하지 않는다.

설정과 상태는 `recovery-config.json`, `recovery-state.json`에 저장된다. 종료하려면 daimon에서 `bridge-recovery`를 Disable한다. 기존 토큰, 터널 프로파일, 허용 루트는 변경하지 않는다.

### Task Graph / Multi-worker

서로 독립적으로 진행 가능한 작업은 `codex_graph_start` 로 DAG를 전달할 수 있다. Graph supervisor는 원본 working tree가 clean인지 확인한 뒤 task마다 별도 git worktree와 `codex_loop_*` worker를 만들고, dependency가 모두 통합된 task만 시작한다. `max_parallel` 범위에서 독립 task는 실제로 동시에 실행된다.

각 task가 자체 verifier를 통과하면 임시 branch에 commit하고, private integration worktree에서 topological order로 cherry-pick한다. 병렬 task가 같은 코드를 충돌되게 수정하면 원본에는 아무것도 적용하지 않고 graph를 `blocked / merge_conflict` 로 멈춘다. integration worktree에는 충돌 파일과 index를 남겨 수동으로 해결할 수 있다. dependency task는 선행 task가 integration된 commit을 기준으로 새 worktree를 만들기 때문에 선행 결과를 볼 수 있다.

모든 task가 통합되면 `final_verify_commands` 를 integration worktree에서 실행한다. 최종 verifier가 없으면 `verification_required` 로 멈추며 `codex_graph_resume` 에 verifier를 추가해 재개할 수 있다. 최종 검증이 통과한 뒤에도 원본 working tree의 HEAD나 파일이 graph 시작 이후 바뀌었다면 자동 덮어쓰지 않고 `integration_ready` 로 멈추고 patch를 보존한다.

원본이 그대로 clean이면 integration branch 전체 diff를 원본 working tree에 **커밋 없이** 적용한다. 따라서 graph 내부 병렬 작업용 임시 commit은 사용자 브랜치 history에 들어가지 않는다. Graph 상태는 `CODEX_BRIDGE_STATE/graphs`, 격리 worktree는 `CODEX_BRIDGE_STATE/graph-worktrees` 아래에 저장되며 graph worker도 flock과 startup recovery를 사용한다.

시작 예시 (MCP tool arguments):

```json
{
  "cwd": "/absolute/path/to/clean-repo",
  "max_parallel": 2,
  "tasks": [
    {"task_id": "api", "goal": "Implement the API change", "write_paths": ["src/api"], "verify_commands": [{"command": "npm", "args": ["test", "--", "api"]}]},
    {"task_id": "ui", "goal": "Implement the UI change", "write_paths": ["src/ui"], "verify_commands": [{"command": "npm", "args": ["test", "--", "ui"]}]},
    {"task_id": "integration", "goal": "Add integration coverage", "depends_on": ["api", "ui"], "write_paths": ["test"], "verify_commands": [{"command": "npm", "args": ["test"]}]}
  ],
  "final_verify_commands": [{"command": "npm", "args": ["test"]}],
  "apply_to_base": true
}
```

제약과 종료 상태:

- `cwd`는 clean git working tree의 루트여야 한다. staged/unstaged/untracked 변경이 있으면 시작을 거부한다. submodule 저장소와 reftable reference storage는 지원하지 않는다.
- task의 `write_paths`는 수정 가능한 상대 파일/디렉터리 목록이다. 범위 밖 변경은 통합 전에 제거하고 task verifier를 다시 실행한다. `[]`는 변경 없는 task에 사용한다. worktree는 파일 격리이며 보안 sandbox를 대체하지 않는다.
- 각 task와 graph에 명시적 verifier를 제공한다. final verifier는 검증 대상 tracked/untracked 파일이나 HEAD를 변경하면 안 된다 (ignored 빌드 산출물은 가능).
- `apply_to_base=false`이면 검증된 patch와 integration worktree를 보존하고 `integration_ready`에서 멈춘다. resume도 이 설정을 유지한다. base 변경 시에는 `integration_ready / base_changed`로 멈추며 강제 적용 옵션은 없다.
- apply 도중 worker가 중단되면 자동 재적용하지 않고 `integration_ready`로 남긴다. patch와 base diff를 비교한 뒤 직접 처리한다. 최종 재검증과 적용 중에는 base의 Git `index.lock`, `HEAD.lock`, 현재 branch ref lock을 잡아 다른 graph와 Git 변경을 차단한다. 잠금 파일은 기존 파일을 덮어쓰지 않으며, `base_locked`면 해당 작업이 끝난 뒤 resume한다. 재시작 시 graph가 직접 만든 잠금만 회수한다. 일반 editor는 Git 잠금을 따르지 않으므로 최종 적용 중 base를 편집하지 않는다.
- 완료·취소 시 private worktree/branch를 정리한다. 충돌·검증 실패·integration-ready 상태는 조사용으로 보존하며 `codex_graph_cancel`로 정리할 수 있다. merge conflict는 명시적인 수동 해결 재개가 필요하다.

충돌 수동 해결:

1. `codex_graph_status`의 `conflict_resolution.worktree`에서 충돌 파일을 수정한다. base working tree는 편집하지 않는다.
2. 해당 integration worktree에서 `git add -- <해결한 파일>`로 변경을 stage한다. 직접 commit하지 않는다.
3. `codex_graph_resume`에 `{"graph_id":"...", "resolve_conflict":true}`를 전달한다. 미해결 index, unstaged/untracked 변경, task의 `write_paths` 밖 변경은 `resolution_error`와 함께 거부된다.
4. supervisor가 충돌 task의 verifier를 실행하고 private 해결 commit을 만든다. 충돌로 중지된 다른 task는 새 child loop로 다시 시작하고, dependent task는 해결된 integration 결과를 기준으로 실행한다. 마지막으로 `final_verify_commands`를 다시 통과해야 base에 적용한다.

수동 해결 checkpoint가 저장된 뒤 worker가 중단돼도 검증된 동일 결과로 이어간다. 재개 중에는 integration worktree도 편집하지 않는다. `integration_base` checkpoint가 있는 이전 graph는 충돌이 abort됐더라도 같은 integration HEAD에서 해결 내용을 stage해 재개할 수 있다. checkpoint가 없는 legacy graph는 새 graph로 시작한다.

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

- `server.mjs` 는 무상태 Streamable HTTP MCP adapter이고, 요청마다 새 `McpServer` 를 만든다
- `loop_supervisor.py` 는 장기 loop의 프로세스 소유권, heartbeat/watchdog, checkpoint/recovery, 반복 오류 전략 전환을 담당한다
- `graph_supervisor.py` 는 dependency scheduling, 격리 worktree 병렬 실행, deterministic integration, 최종 검증과 base patch 적용을 담당한다
- `test.mjs` 는 fake Codex로 loop 복구뿐 아니라 병렬 graph, dependency 순서, 최종 verifier attach/resume, merge conflict, 취소, base 변경, stale recovery와 중단 checkpoint 복구까지 검증하며 실제 `~/.codex` 나 홈 디렉터리를 건드리지 않는다
- `scripts/setup.sh` 는 에이전트용 원샷 설치(재실행 안전), `scripts/install-launchd.sh` 는 LaunchAgent 등록만 한다. 설치 흐름을 바꾸면 [SETUP.md](SETUP.md) 도 같이 고친다
- PR 마다 GitHub Actions 가 macOS·Ubuntu 에서 `npm test` 를 돌린다

## 참고

[dreamurl/GPT-Bridge](https://github.com/dreamurl/GPT-Bridge), [xq3427/WebCodex](https://github.com/xq3427/WebCodex) 를 참고해 최소한으로 다시 만들었다. tunnel-client 는 [openai/tunnel-client](https://github.com/openai/tunnel-client) (Apache-2.0).

## 라이선스

MIT
