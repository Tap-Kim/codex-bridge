---
description: "Codex에서 하던 작업을 codex-bridge로 끌어와 현황 정리 (인자: 프로젝트 경로 또는 thread_id, 생략 시 최신)"
argument-hint: "[프로젝트 경로 | thread_id]"
---
반드시 codex-bridge MCP 툴만 사용한다. 자체 셸이나 다른 도구로 대체하지 않는다.

인자: `$ARGUMENTS`

1. 인자가 `/`로 시작하면 `codex_handoff`를 `cwd`로, UUID 형태면 `thread_id`로, 비어 있으면 인자 없이 호출한다.
2. 결과를 아래 형식으로 한국어로 정리한다. 추측하지 말고 툴 결과에 있는 것만 쓴다.
   - 스레드: title / thread_id / cwd / 시작 시각
   - 목표: goal 한 줄 요약
   - 마지막 상태: latest_agent_message 요약, stopped_with_error 가 있으면 굵게 표시
   - Codex 가 바꾼 파일: files_changed_by_codex (10개 넘으면 10개 + 나머지 개수)
   - git: branch, status 요약, diff_stat
3. 멈춘 지점부터 이어가기 위한 다음 행동 2~3개를 제안한다. 선택지는 항상 두 갈래로 적는다:
   - Codex 에 계속 맡기기: `codex_resume thread_id=...` 에 넣을 프롬프트 초안
   - 여기서 직접 이어가기: 먼저 읽어야 할 파일 (read_file / git_diff)
4. 사용자가 고르기 전에는 파일을 수정하거나 codex_resume 을 실행하지 않는다.
