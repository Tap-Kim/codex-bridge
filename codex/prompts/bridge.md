---
description: "지시를 codex-bridge MCP 툴로만 수행 (로컬 파일 읽기/편집, 명령 실행, Codex 위임)"
argument-hint: "<할 일>"
---
다음 지시를 수행할 때 반드시 codex-bridge MCP 툴(list_projects, list_dir, read_file, search, edit_file, write_file, run_command, git_diff, codex_run, codex_resume, codex_status, codex_sessions, codex_session_read, codex_handoff)만 사용한다. 자체 셸·파일 도구는 쓰지 않는다.

지시: `$ARGUMENTS`

규칙:
- 경로가 불명확하면 먼저 `list_projects` 로 후보를 확인하고 절대 경로를 쓴다.
- 수정 전에 `read_file` 로 읽고, `edit_file` 은 정확히 한 번 일치하는 old_text 를 쓴다.
- 여러 파일에 걸친 구현이나 테스트 반복은 `codex_run`(sandbox workspace-write) 에 맡기고, status 가 running 이면 `codex_status` 로 폴링한다. 사용량 한도 오류가 나오면 직접 read_file/edit_file/run_command 로 이어간다.
- 끝나면 실제로 바꾼 파일과 실행한 명령·결과만 보고한다. 검증하지 않은 것을 완료라고 말하지 않는다.
