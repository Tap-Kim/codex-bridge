# codex-bridge 설치 절차 (AI 에이전트용)

이 문서는 사용자가 이 저장소 링크를 주며 "설치해줘" 라고 했을 때 **셸을 실행할 수 있는 에이전트**(Claude Code, Codex CLI 등)가 그대로 따라 하는 절차다.
셸이 없는 챗봇이라면 아래 명령을 사용자에게 순서대로 안내하고, 각 명령의 출력을 받아 다음 단계를 판단한다.

원리와 수동 설치는 [README.md](README.md) 에 있다. 여기서는 순서와 판단 기준만 다룬다.

## 에이전트가 지킬 규칙

1. **브라우저 로그인이 필요한 단계는 대신하지 않는다.** 정확한 URL 과 할 일을 알려 주고 사용자가 끝냈다고 할 때까지 기다린다. 해당 단계: 터널 만들기, 워크스페이스 ID 추가, 런타임 API 키 발급, ChatGPT 커넥터 만들기.
2. **API 키를 채팅에 붙여 넣게 하지 않는다.** 사용자가 직접 파일에 쓰는 명령(`pbpaste > ...`)을 준다.
3. **허용 루트는 반드시 사용자에게 묻는다.** 홈 전체(`~`)는 권하지 않는다. 절대 경로로 받는다.
4. 판단은 `scripts/setup.sh` 출력의 `[ok]` `[skip]` `[todo]` `[fail]` 줄과 검증 명령 결과로만 한다. 확인하지 않은 단계를 완료라고 보고하지 않는다.
5. 에이전트의 권한 정책이 터널 프로파일 작성이나 `tunnel-client` 실행을 막으면 **우회하지 않는다.** 같은 명령을 사용자가 직접 실행하게 안내한다 (Claude Code 에서는 프롬프트 앞에 `!` 를 붙여 실행).

## 0. 사용자에게 물을 것 (한 번에)

먼저 설치가 바꾸는 것을 알려 준다: `~/codex-bridge` (코드), `~/.codex-bridge/` (토큰·키·로그), macOS LaunchAgent `local.codex-bridge`(+ `.tunnel`), 고른 경우 `~/.codex/config.toml` 에 한 절 추가와 `~/.codex/prompts/` 에 두 파일, Claude Code user scope MCP 등록. 사용자가 원하면 실행 전에 `scripts/setup.sh` 를 같이 읽는다.

- ChatGPT·Codex 가 읽고 쓸 수 있게 할 폴더 (예: `/Users/me/work:/Users/me/side`, 콜론 구분)
- 어디서 쓸지 (복수 선택)
  - **웹 ChatGPT** -> 터널 필요. OpenAI Platform 조직에서 Tunnels 권한이 있어야 한다
  - **Codex 앱·CLI**, **Claude Code** -> 터널 없이 로컬 연결

## 1. 사전 점검

```bash
curl -s 127.0.0.1:7421/health   # {"status":"ok"} 가 나오면 이미 무언가 7421 에서 돌고 있다 -> 멈추고 사용자에게 확인
node -v                          # v20 이상
python3 --version                 # v3.9 이상 (Loop Engineering supervisor)
git --version
uname                            # Darwin 이면 LaunchAgent 로 상시 실행까지 자동
```

## 2. 설치 (로컬 클라이언트까지)

```bash
git clone https://github.com/Tap-Kim/codex-bridge.git ~/codex-bridge   # 이미 있으면 git -C ~/codex-bridge pull
cd ~/codex-bridge
CODEX_BRIDGE_ROOTS="<0단계에서 받은 폴더>" CODEX_BRIDGE_CLIENTS="codex claude" scripts/setup.sh
```

- `CODEX_BRIDGE_CLIENTS` 는 0단계에서 고른 로컬 클라이언트만 넣는다 (`codex`, `claude`, 둘 다, 또는 `""`)
- 스크립트는 여러 번 실행해도 안전하다. 끝난 단계는 건너뛴다
- `[skip]` 줄은 고르지 않았거나 설치되지 않은 항목이라 정상이다. `[fail]` 은 멈추고 메시지대로 고친다 (허용 루트가 홈 전체나 `/` 이거나, 없는 폴더면 거부된다)
- 끝에 `LEFT FOR THE HUMAN:` 목록이 나오면 그 항목을 사용자에게 전달한다. `DONE` 이면 이 단계 완료

검증:

```bash
curl -s 127.0.0.1:7421/health                                              # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:7421/mcp -d '{}'  # 401 (토큰 없는 요청 차단)
```

웹 ChatGPT 를 쓰지 않는다면 여기서 끝이다. Codex 앱은 재시작해야 새 MCP 서버를 읽는다. 사용자에게 "Codex 나 Claude Code 에서 `codex_handoff` 로 Codex 에서 하던 작업을 가져와 봐" 라고 안내한다.

## 3. 웹 ChatGPT: 사용자가 브라우저에서 할 일

아래 안내를 사용자에게 그대로 보내고 끝났다는 답을 기다린다.

> 1. <https://platform.openai.com/settings/organization/tunnels> 에서 **Create tunnel** 을 누르고, 만들어진 `tunnel_...` ID 를 알려 주세요.
> 2. 같은 터널 설정의 **ChatGPT workspaces** 에 본인 계정 ID 를 추가해 주세요. ChatGPT 에 로그인한 브라우저에서 <https://chatgpt.com/api/auth/session> 을 열면 나오는 `account.id` 값(UUID)입니다. 이름 검색은 안 되고 ID 를 그대로 넣어야 합니다.
> 3. <https://platform.openai.com/settings/organization/api-keys> 에서 이 용도 전용 **런타임** API 키를 만들고(admin 키 아님), 키를 복사한 상태에서 터미널에 아래를 실행해 주세요. 키는 채팅에 붙여 넣지 마세요.
>    ```bash
>    pbpaste | tr -d '\n' > ~/.codex-bridge/api_key && chmod 600 ~/.codex-bridge/api_key
>    ```
>    (Linux 는 `pbpaste` 대신 `xclip -o -selection clipboard` 또는 편집기로 한 줄 저장)

확인 (키 내용은 출력하지 않는다):

```bash
test -s ~/.codex-bridge/api_key && echo "api_key saved"
```

## 4. 웹 ChatGPT: 터널 설치

```bash
cd ~/codex-bridge
CODEX_BRIDGE_ROOTS="<같은 폴더>" CODEX_BRIDGE_CLIENTS="<2단계와 같게>" TUNNEL_ID=<3단계의 tunnel_...> scripts/setup.sh
```

스크립트가 하는 일: `tunnel-client` 가 없으면 `brew install openai/tools/tunnel-client`, 템플릿으로 `~/.codex-bridge/tunnel.yaml` 생성, `tunnel-client doctor` 로 검증, 브릿지가 뜬 뒤에 터널이 뜨도록 LaunchAgent 등록, `readyz` 대기.

`[todo]` 가 나오면 그 내용대로 처리하고 같은 명령을 다시 실행한다. 자주 나오는 경우:

| `[todo]` 내용 | 조치 |
|---|---|
| install tunnel-client | brew 실패. README 의 2-1 절대로 zip 을 받아 sha256 을 확인하고 PATH 에 추가, 또는 `TUNNEL_CLIENT=/abs/path/tunnel-client` 로 재실행 |
| save the runtime API key | 3단계의 3번이 안 됨 |
| doctor failed (`FAILED_CHECKS ...`) | `~/.codex-bridge/doctor.log` 를 읽고 원인 해결. `tunnel_id` 면 ID 오타, `control_plane_api_key` 면 키 파일, `health_listener` 면 7422 포트를 다른 프로세스가 사용 중 |
| different tunnel id | 기존 `tunnel.yaml` 을 사용자 확인 후 옮기고 재실행 |
| tunnel not ready | 출력된 readyz 사유와 `~/.codex-bridge/logs/local.codex-bridge.tunnel.out.log` 확인 |

검증:

```bash
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:7422/readyz   # 200
```

## 5. 웹 ChatGPT: 커넥터 만들기 (사용자가 브라우저에서)

readyz 가 200 인 상태에서 사용자에게 보낸다.

> 1. chatgpt.com 설정 -> 보안 -> **개발자 모드** 를 켜 주세요.
> 2. <https://chatgpt.com/plugins> 페이지 상단의 **앱 만들기** 를 눌러 주세요 (설정 화면이 아니라 이 페이지에 있습니다).
> 3. 이름 `codex-bridge`, 연결 방식 **터널**, 방금 만든 터널 선택, 인증 **인증 없음**, 위험 고지 체크 -> **만들기** -> **연결하기**.
> 4. 새 대화에서 이 앱을 켜고 "list_projects 로 프로젝트 목록 보여줘" 라고 보내 보세요.

터널 목록이 비어 있으면 3단계의 2번(워크스페이스 ID)이 빠진 것이다.

## 6. 최종 확인

```bash
tail -5 ~/.codex-bridge/access.log   # ChatGPT 가 부른 툴 이름이 찍히면 연결 완료
```

사용자에게 보고할 것: 허용 루트, 등록한 클라이언트, 터널 readyz 상태, 그리고 README 의 보안 주의 한 줄 (연결된 대화는 허용 루트 안의 파일을 쓰고 허용 명령을 실행할 수 있다).

## Phase 4 Task Graph 사용

설치된 브릿지의 MCP tool 목록에서 `codex_graph_start`, `codex_graph_status`, `codex_graph_resume`, `codex_graph_cancel`을 확인한다. Python 3 supervisor는 Node adapter와 별개로 실행되어 브릿지 재시작 후에도 child 작업을 유지·복구한다.

clean git 저장소 루트에서 task DAG, task별 `write_paths`와 `verify_commands`, graph의 `final_verify_commands`를 제공한다. 각 task는 독립 worktree에서 실행되고 선행 task 통합 이후에 dependent task가 시작된다. 최종 적용 전에 base HEAD와 dirty 상태를 다시 확인하며, 원본에는 commit 없이 diff만 적용한다. `apply_to_base=false` 또는 base 변경이면 `integration_ready`로 멈춘다. 수정 중인 저장소에서는 시작하지 말고, 최초 점검은 별도 임시 git 저장소에서 수행한다. 최종 적용은 Git 잠금 안에서 수행하며 외부 잠금이 있으면 `base_locked`로 멈춘다. 충돌은 반환된 integration worktree에서 수정·stage하고 `codex_graph_resume(resolve_conflict=true)`로 검증 후 재개한다. 직접 commit하지 않는다. submodule은 지원하지 않는다. 예시와 보존/정리 규칙은 [README Task Graph 절](README.md#task-graph--multi-worker)을 참고한다.

## 제거

```bash
~/codex-bridge/scripts/install-launchd.sh uninstall   # 두 LaunchAgent 제거
claude mcp remove codex-bridge -s user                 # Claude Code 등록 해제
# Codex: ~/.codex/config.toml 에서 [mcp_servers.codex-bridge] 절 삭제, ~/.codex/prompts/{handoff,bridge}.md 삭제
# Linux: 직접 띄운 브릿지·tunnel-client 프로세스(systemd --user 유닛, tmux 등)를 멈춘다
# 토큰·키·프로파일까지 지우려면: rm -rf ~/.codex-bridge
```
