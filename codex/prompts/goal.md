---
description: "검증 가능한 목표를 persistent loop 또는 병렬 task graph로 수행"
argument-hint: "<목표와 프로젝트 경로>"
---
목표: `$ARGUMENTS`

codex-bridge MCP 도구로 아래 작업을 수행한다.

1. 사용자 요청과 현재 프로젝트 문맥으로 cwd를 정한다. 불명확하면 list_projects로 후보를 확인하고, 경로를 임의로 선택하지 않는다.
2. read_file로 프로젝트 지침과 package/test 설정을 읽고 run_command로 git 상태를 확인한다. 기존 수정은 보존한다.
3. 목표에 맞는 실제 verifier 명령을 고른다. 근거 없이 성공만 반환하는 명령을 verifier로 사용하지 않는다. 검증 기준이 불명확하면 사용자에게 필요한 기준을 묻는다.
4. 단일 작업이나 dirty working tree에서는 codex_loop_start에 goal과 verify_commands를 전달한다. max_attempts 기본값은 5다.
5. clean git root에서 독립/의존 작업을 안전하게 분리할 수 있으면 codex_graph_start를 사용한다. task_id, goal, 명시적인 상대 write_paths, depends_on, task별 verify_commands와 graph의 final_verify_commands를 제공한다. max_parallel 기본값은 2다. submodule/reftable 저장소는 graph 대신 loop를 사용한다.
6. start/resume/status 호출에는 wait_sec를 명시하고 20초 이하로 유지한다. codex_loop_status 또는 codex_graph_status로 상태를 확인한다. worker는 연결과 별개로 실행된다. recoverable interruption은 같은 id의 resume 도구로 이어간다. cancelled 작업을 자동 재시작하지 않는다.
7. merge_conflict에서는 integration worktree 경로를 보고한다. 사용자가 해당 worktree에서 수정·stage한 결과를 명시적으로 재개하도록 요청한 경우 codex_graph_resume(resolve_conflict=true)를 사용한다. base_changed/base_locked/apply_interrupted를 강제로 우회하지 않는다.
8. 원본 저장소의 commit/push는 사용자 요청이 있을 때만 한다. graph 내부의 격리된 임시 commit은 supervisor가 관리한다.
9. 완료 보고에는 상태, 실제 변경, 외부 verifier 결과를 적는다. Codex 자체 완료 메시지만으로 검증 성공을 주장하지 않는다.
