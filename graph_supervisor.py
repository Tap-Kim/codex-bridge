#!/usr/bin/env python3
"""Dependency-aware multi-worker orchestration for codex-bridge Loop Engineering.

A graph worker schedules task loops into isolated git worktrees. Completed task
branches are cherry-picked into a private integration worktree. After explicit
final verification, the integration diff is applied to the user's original
working tree without creating a commit there.
"""
import argparse
import datetime as dt
from contextlib import contextmanager
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ACTIVE = {"starting", "running", "integrating", "verifying", "resolving", "applying"}
TERMINAL = {"completed", "blocked", "failed", "cancelled", "verification_required", "integration_ready"}
DEFAULT_ALLOW = "git,npm,npx,pnpm,yarn,node,bun,python3,pytest,go,cargo,make,ls,cat,grep,tsc,eslint,vitest,jest,prettier,gh"
MAX_HISTORY = 120
MAX_TASKS = 32
HERE = Path(__file__).resolve().parent
LOOP_SUPERVISOR = HERE / "loop_supervisor.py"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def short(value, limit=3000):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def state_root():
    return Path(os.environ.get("CODEX_BRIDGE_STATE", str(Path.home() / ".codex-bridge")))


def graphs_dir():
    p = state_root() / "graphs"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        p.chmod(0o700)
    except OSError:
        pass
    return p


def worktrees_root():
    p = state_root() / "graph-worktrees"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def valid_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(value or "")))


def state_path(graph_id):
    if not valid_id(graph_id):
        raise ValueError("invalid graph_id")
    return graphs_dir() / (graph_id + ".json")


def lock_path(graph_id):
    return state_path(graph_id).with_suffix(".lock")


def cancel_path(graph_id):
    return state_path(graph_id).with_suffix(".cancel")


def patch_path(graph_id):
    return state_path(graph_id).with_suffix(".patch")


def history(state, event, **detail):
    state.setdefault("history", []).append({"at": now(), "type": event, **detail})
    state["history"] = state["history"][-MAX_HISTORY:]


def write_state(state):
    if cancel_path(state["graph_id"]).exists():
        state["status"] = "cancelled"
    state["updated_at"] = now()
    state["history"] = state.get("history", [])[-MAX_HISTORY:]
    target = state_path(state["graph_id"])
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return state


def load_state(graph_id):
    return json.loads(state_path(graph_id).read_text())


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def read_input():
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def try_lock(graph_id):
    f = open(lock_path(graph_id), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def lock_held(graph_id):
    f = try_lock(graph_id)
    if f is None:
        return True
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    f.close()
    return False


def run(args, cwd=None, input_text=None, check=True, timeout=120):
    r = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        errors="surrogateescape",
        input=input_text,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")},
    )
    if check and r.returncode != 0:
        raise RuntimeError(short(r.stderr or r.stdout or ("command failed: " + " ".join(args)), 4000))
    return r


def git(cwd, *args, check=True, input_text=None):
    return run(["git", *args], cwd=cwd, check=check, input_text=input_text)


def loop_call(command, loop_id=None, payload=None):
    args = [sys.executable, str(LOOP_SUPERVISOR), command]
    if loop_id:
        args.append(loop_id)
    r = run(args, input_text=json.dumps(payload or {}), timeout=30)
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError("loop supervisor returned no output")
    data = json.loads(lines[-1])
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def spawn_worker(graph_id):
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "worker", graph_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=os.environ.copy(),
    )


def topo_order(tasks):
    ids = [t["task_id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("task_id values must be unique")
    known = set(ids)
    deps = {t["task_id"]: set(t.get("depends_on") or []) for t in tasks}
    for tid, values in deps.items():
        missing = values - known
        if missing:
            raise ValueError("unknown dependency for %s: %s" % (tid, ",".join(sorted(missing))))
        if tid in values:
            raise ValueError("task cannot depend on itself: " + tid)
    order = []
    remaining = set(ids)
    while remaining:
        ready = [tid for tid in ids if tid in remaining and deps[tid].issubset(set(order))]
        if not ready:
            raise ValueError("task graph contains a dependency cycle")
        for tid in ready:
            order.append(tid)
            remaining.remove(tid)
    return order


def validate_commands(commands, allow):
    if not isinstance(commands, list) or len(commands) > 20:
        raise ValueError("verify commands must be an array of at most 20 entries")
    for spec in commands:
        if not isinstance(spec, dict) or spec.get("command") not in allow:
            raise ValueError("verifier command not allowed")
        if not isinstance(spec.get("args", []), list) or not all(isinstance(arg, str) for arg in spec.get("args", [])):
            raise ValueError("verifier args must be an array of strings")


def validate_create(cfg):
    cwd = Path(cfg["cwd"]).resolve()
    if not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    if git(str(cwd), "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() != "true":
        raise ValueError("Phase 4 worktree orchestration requires a git repository")
    if Path(git(str(cwd), "rev-parse", "--show-toplevel").stdout.strip()).resolve() != cwd:
        raise ValueError("cwd must be the git working tree root")
    if git(str(cwd), "config", "--local", "--get", "extensions.refStorage", check=False).stdout.strip() not in {"", "files"}:
        raise ValueError("Phase 4 base locking requires Git files reference storage")
    if git(str(cwd), "submodule", "status").stdout.strip():
        raise ValueError("Phase 4 does not support repositories with submodules")
    if git(str(cwd), "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise ValueError("base working tree must be clean before starting a parallel graph")
    if "apply_to_base" in cfg and not isinstance(cfg["apply_to_base"], bool):
        raise ValueError("apply_to_base must be boolean")
    for key, default, lower, upper in [("max_parallel", 2, 1, 8), ("task_max_attempts", 5, 1, 20),
                                      ("watchdog_interval_sec", 5, .1, 300), ("hang_timeout_sec", 300, .5, 86400),
                                      ("terminate_grace_sec", 5, .1, 60)]:
        value = cfg.get(key, default)
        if not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise ValueError("invalid " + key)
    tasks = cfg.get("tasks") or []
    if not tasks or len(tasks) > MAX_TASKS:
        raise ValueError("tasks must contain 1-%d entries" % MAX_TASKS)
    allow = set(cfg.get("allow_commands") or DEFAULT_ALLOW.split(","))
    normalized = []
    for raw in tasks:
        tid = raw.get("task_id")
        if not valid_id(tid):
            raise ValueError("invalid task_id: " + str(tid))
        if not str(raw.get("goal") or "").strip():
            raise ValueError("task goal is required: " + tid)
        if not isinstance(raw.get("depends_on", []), list) or not all(isinstance(dep, str) for dep in raw.get("depends_on", [])):
            raise ValueError("depends_on must be an array of task ids")
        if not 1 <= int(raw.get("max_attempts", cfg.get("task_max_attempts", 5))) <= 20:
            raise ValueError("invalid task max_attempts")
        commands = raw.get("verify_commands") or []
        validate_commands(commands, allow)
        write_paths = []
        if "write_paths" not in raw:
            raise ValueError("write_paths must be explicitly provided for task: " + tid)
        for value in raw.get("write_paths") or []:
            raw_path = str(value).replace("\\", "/")
            rel = raw_path.strip("/")
            parts = [p for p in rel.split("/") if p]
            if not rel or raw_path.startswith("/") or rel.startswith(".git") or ".." in parts:
                raise ValueError("invalid write_paths entry for %s: %s" % (tid, value))
            write_paths.append(rel)
        normalized.append({
            "task_id": tid,
            "goal": raw["goal"],
            "depends_on": list(raw.get("depends_on") or []),
            "write_paths": write_paths,
            "verify_commands": commands,
            "max_attempts": int(raw.get("max_attempts", cfg.get("task_max_attempts", 5))),
            "status": "pending",
            "loop_id": None,
            "worktree": None,
            "branch": None,
            "commit": None,
            "error": None,
            "started_at": None,
            "completed_at": None,
            "integrated_at": None,
        })
    order = topo_order(normalized)
    final_commands = cfg.get("final_verify_commands") or []
    validate_commands(final_commands, allow)
    return str(cwd), normalized, order, sorted(allow), final_commands


def cmd_create():
    cfg = read_input()
    cwd, tasks, order, allow, final_commands = validate_create(cfg)
    graph_id = uuid.uuid4().hex[:12]
    stamp = now()
    state = {
        "schema_version": 1,
        "graph_id": graph_id,
        "cwd": cwd,
        "goal": cfg.get("goal") or "",
        "status": "starting",
        "base_head": git(cwd, "rev-parse", "HEAD").stdout.strip(),
        "base_branch": git(cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        "base_ref": git(cwd, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() or None,
        "integration_branch": "codex-graph-" + graph_id,
        "integration_worktree": str(worktrees_root() / graph_id / "integration"),
        "max_parallel": max(1, min(int(cfg.get("max_parallel", 2)), 8)),
        "peak_parallel": 0,
        "task_max_attempts": int(cfg.get("task_max_attempts", 5)),
        "watchdog_interval_sec": float(cfg.get("watchdog_interval_sec", 5)),
        "hang_timeout_sec": float(cfg.get("hang_timeout_sec", 300)),
        "terminate_grace_sec": float(cfg.get("terminate_grace_sec", 5)),
        "codex_bin": cfg.get("codex_bin", os.environ.get("CODEX_BIN", "codex")),
        "allow_commands": allow,
        "tasks": tasks,
        "topological_order": order,
        "final_verify_commands": final_commands,
        "final_verification": [],
        "apply_to_base": bool(cfg.get("apply_to_base", True)),
        "patch_file": str(patch_path(graph_id)),
        "owner_pid": None,
        "worker_spawned_at": stamp,
        "created_at": stamp,
        "updated_at": stamp,
        "history": [{"at": stamp, "type": "graph_created", "tasks": len(tasks)}],
    }
    write_state(state)
    spawn_worker(graph_id)
    emit(snapshot(state))


def snapshot(state):
    out = dict(state)
    if cancel_path(state["graph_id"]).exists():
        out["status"] = "cancelled"
    out["recoverable"] = out.get("status") in {"interrupted", "integration_ready"}
    if state.get("blocked_reason") == "merge_conflict":
        out["conflict_resolution"] = {
            "worktree": state["integration_worktree"],
            "task_id": state.get("resolution_task_id") or next((t["task_id"] for t in state["tasks"] if t["status"] == "conflict"), None),
            "instructions": "Resolve files and git add them in the integration worktree; do not commit. Then resume with resolve_conflict=true. Task and final verifiers run before base apply.",
        }
    return out


def age_seconds(stamp):
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return float("inf")


def stale_to_interrupted(state):
    if state.get("status") not in ACTIVE:
        return state
    lock = try_lock(state["graph_id"])
    if lock is None:
        return load_state(state["graph_id"])
    try:
        state = load_state(state["graph_id"])
        if state.get("status") in ACTIVE and age_seconds(state.get("worker_spawned_at")) >= 2:
            state["status"] = "interrupted"
            state["owner_pid"] = None
            history(state, "graph_interrupted", recoverable=True)
            write_state(state)
        return state
    finally:
        lock.close()


def cmd_status(graph_id):
    emit(snapshot(stale_to_interrupted(load_state(graph_id))))


def cmd_resume(graph_id):
    cfg = read_input()
    if "resolve_conflict" in cfg and not isinstance(cfg["resolve_conflict"], bool):
        raise ValueError("resolve_conflict must be boolean")
    lock = try_lock(graph_id)
    if lock is None:
        emit(snapshot(load_state(graph_id)))
        return
    try:
        state = load_state(graph_id)
        if cancel_path(graph_id).exists() or state["status"] in {"completed", "cancelled"}:
            emit(snapshot(state))
            return
        resolving = bool(cfg.get("resolve_conflict")) and state.get("blocked_reason") == "merge_conflict"
        if state.get("status") == "blocked" and state.get("blocked_reason") != "final_verification_failed" and not resolving:
            emit(snapshot(state))
            return
        if resolving:
            state["resolve_conflict_requested"] = True
            state["resolution_task_id"] = state.get("resolution_task_id") or next(t["task_id"] for t in state["tasks"] if t["status"] == "conflict")
        if "final_verify_commands" in cfg:
            commands = cfg.get("final_verify_commands") or []
            validate_commands(commands, set(state["allow_commands"]))
            state["final_verify_commands"] = commands
        if state.get("status") == "verification_required" and not state.get("final_verify_commands"):
            emit(snapshot(state))
            return
        state["status"] = "starting"
        state["worker_spawned_at"] = now()
        history(state, "graph_manual_resume")
        write_state(state)
    finally:
        lock.close()
    spawn_worker(graph_id)
    emit(snapshot(state))


def cancel_children(state):
    for task in state.get("tasks", []):
        if task.get("loop_id") and task.get("status") in {"launching", "running"}:
            # The marker also protects a child not yet created by launch_task.
            marker = state_root() / "loops" / (task["loop_id"] + ".cancel")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(mode=0o600, exist_ok=True)
            if state.get("blocked_reason") == "merge_conflict":
                task["stopped_by_graph"] = True
            try:
                loop_call("cancel", task["loop_id"])
            except Exception:
                pass


def cmd_cancel(graph_id):
    state = load_state(graph_id)
    if state["status"] == "completed":
        emit(snapshot(state))
        return
    cancel_path(graph_id).touch(mode=0o600, exist_ok=True)
    cancel_children(load_state(graph_id))
    lock = try_lock(graph_id)
    try:
        state = load_state(graph_id)
        if lock is not None:
            state["status"] = "cancelled"
            history(state, "graph_cancelled")
            release_base_locks(state)
            cleanup_graph_worktrees(state)
        # While an owner is live, the monotonic marker is authoritative; avoid
        # overwriting its freshly saved task checkpoints with a stale snapshot.
        emit(snapshot(state))
    finally:
        if lock is not None:
            lock.close()


def cmd_recover():
    recovered = []
    for p in graphs_dir().glob("*.json"):
        try:
            state = stale_to_interrupted(json.loads(p.read_text()))
            if state.get("status") == "interrupted":
                lock = try_lock(state["graph_id"])
                if lock is None:
                    continue
                try:
                    state = load_state(state["graph_id"])
                    if state["status"] != "interrupted":
                        continue
                    state["status"] = "starting"
                    state["worker_spawned_at"] = now()
                    history(state, "graph_startup_recovery")
                    write_state(state)
                finally:
                    lock.close()
                spawn_worker(state["graph_id"])
                recovered.append(state["graph_id"])
        except Exception:
            continue
    emit({"recovered": recovered})


def setup_integration(state):
    root = Path(state["integration_worktree"]).parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    integration = Path(state["integration_worktree"])
    if integration.exists() and (integration / ".git").exists():
        return state
    if any(t["status"] != "pending" for t in state["tasks"]):
        raise RuntimeError("integration worktree is missing; refusing to discard task checkpoints")
    if integration.exists():
        shutil.rmtree(integration, ignore_errors=True)
    # Clean up a stale private branch only when no integration worktree exists.
    git(state["cwd"], "branch", "-D", state["integration_branch"], check=False)
    r = git(state["cwd"], "worktree", "add", "-b", state["integration_branch"], str(integration), state["base_head"], check=False)
    if r.returncode != 0:
        raise RuntimeError("failed to create integration worktree: " + short(r.stderr or r.stdout, 3000))
    history(state, "integration_worktree_created")
    state["status"] = "running"
    return write_state(state)


def task_by_id(state, task_id):
    return next(t for t in state["tasks"] if t["task_id"] == task_id)


def launch_task(state, task):
    if not task.get("loop_id"):
        integration = state["integration_worktree"]
        task_root = Path(integration).parent / ("task-" + task["task_id"])
        branch = "codex-graph-%s-%s" % (state["graph_id"], task["task_id"])
        # Persist intent before any worktree/child process mutation.
        task.update(status="launching", loop_id="graph-%s-%s-%s" % (state["graph_id"], task["task_id"], task.get("generation", 0)),
                    worktree=str(task_root), branch=branch,
                    base_head=git(integration, "rev-parse", "HEAD").stdout.strip())
        write_state(state)
    task_root = Path(task["worktree"])
    if not (task_root / ".git").exists():
        git(state["cwd"], "worktree", "prune")
        git(state["cwd"], "branch", "-D", task["branch"], check=False)
        git(state["cwd"], "worktree", "add", "-b", task["branch"], str(task_root), task["base_head"])
    payload = {
        "_loop_id": task["loop_id"],
        "cwd": str(task_root),
        "goal": task["goal"],
        "verify_commands": task.get("verify_commands") or [],
        "max_attempts": task.get("max_attempts") or state["task_max_attempts"],
        "watchdog_interval_sec": state["watchdog_interval_sec"],
        "hang_timeout_sec": state["hang_timeout_sec"],
        "terminate_grace_sec": state["terminate_grace_sec"],
        "codex_bin": state["codex_bin"],
        "allow_commands": state["allow_commands"],
    }
    loop_call("create", payload=payload)
    task["status"] = "running"
    task["started_at"] = task.get("started_at") or now()
    history(state, "task_started", task_id=task["task_id"], loop_id=task["loop_id"])
    return write_state(state)


def changed_paths(wt, base):
    tracked = [x for x in git(wt, "diff", "--name-only", "--no-renames", "-z", base).stdout.split("\0") if x]
    untracked = [x for x in git(wt, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0") if x]
    return sorted(set(tracked + untracked))


def path_in_scope(rel, scopes):
    return any(rel == scope or rel.startswith(scope.rstrip("/") + "/") for scope in scopes)


def discard_out_of_scope(state, task):
    wt = task["worktree"]
    base = task.get("base_head") or git(wt, "rev-parse", "HEAD").stdout.strip()
    scopes = task.get("write_paths") or []
    discarded = set()
    while True:
        unauthorized = [p for p in changed_paths(wt, base) if not path_in_scope(p, scopes)]
        if not unauthorized:
            break
        if set(unauthorized).issubset(discarded):
            raise RuntimeError("scope cleanup did not remove excluded paths")
        for rel in unauthorized:
            exists_in_base = git(wt, "cat-file", "-e", base + ":" + rel, check=False).returncode == 0
            if exists_in_base:
                git(wt, "restore", "--source=" + base, "--staged", "--worktree", "--", ":(literal)" + rel)
            else:
                git(wt, "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", ":(literal)" + rel)
                target = Path(wt) / rel
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
        discarded.update(unauthorized)
        # Removing an agent-written .gitignore may expose more generated files.
    if not discarded:
        return state
    history(state, "out_of_scope_discarded", task_id=task["task_id"], paths=sorted(discarded)[:30])
    write_state(state)
    results = run_verifiers(wt, task.get("verify_commands") or [], state["allow_commands"])
    if not results or results[-1]["exit_code"] != 0 or any(not path_in_scope(p, scopes) for p in changed_paths(wt, base)):
        task["status"] = "failed"
        task["error"] = {"reason": "scope_cleanup_broke_verifier", "results": results}
        state["status"] = "blocked"
        state["blocked_reason"] = "scope_cleanup_broke_verifier"
        history(state, "task_failed_after_scope_cleanup", task_id=task["task_id"])
    return write_state(state)


def commit_task(state, task):
    wt = task["worktree"]
    state = discard_out_of_scope(state, task)
    if state.get("status") in {"blocked", "cancelled"}:
        return state
    base = task.get("base_head") or git(wt, "rev-parse", "HEAD").stdout.strip()
    # Restoring .gitignore can reveal previously ignored plugin artifacts.
    # Build the index from the task base and stage ONLY explicitly scoped paths;
    # never rely on a one-time untracked scan followed by a blanket git add.
    git(wt, "read-tree", base)
    allowed = [p for p in changed_paths(wt, base) if path_in_scope(p, task.get("write_paths") or [])]
    if allowed:
        git(wt, "add", "-A", "--", *[":(literal)" + p for p in allowed])
    tree = git(wt, "write-tree").stdout.strip()
    if tree == git(wt, "rev-parse", base + "^{tree}").stdout.strip():
        task["commit"] = None
    else:
        # Squash even commits created by Codex, without hooks. Persist the object
        # before updating its branch; repeating this after a crash is harmless.
        task["commit"] = git(wt, "-c", "user.name=codex-bridge", "-c", "user.email=codex-bridge@localhost",
                             "commit-tree", tree, "-p", base,
                             input_text="codex graph task " + task["task_id"] + "\n").stdout.strip()
    task["status"] = "completed"
    task["completed_at"] = now()
    history(state, "task_completed", task_id=task["task_id"], commit=task["commit"], no_changes=task["commit"] is None)
    write_state(state)
    if task["commit"]:
        git(wt, "update-ref", "refs/heads/" + task["branch"], task["commit"])
    return state


def cleanup_task_worktree(state, task):
    if task.get("worktree"):
        git(state["cwd"], "worktree", "remove", "--force", task["worktree"], check=False)
    if task.get("branch"):
        git(state["cwd"], "branch", "-D", task["branch"], check=False)


def integrate_task(state, task):
    integration = state["integration_worktree"]
    if task.get("integration_base"):
        git(integration, "cherry-pick", "--abort", check=False)
        git(integration, "reset", "--hard", task["integration_base"])
    else:
        task["integration_base"] = git(integration, "rev-parse", "HEAD").stdout.strip()
        write_state(state)
    if task.get("commit"):
        r = git(integration, "-c", "user.name=codex-bridge", "-c", "user.email=codex-bridge@localhost",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                "cherry-pick", task["commit"], check=False)
        if r.returncode != 0:
            # Keep the unmerged index/files for an explicit manual resolution.
            task["conflict_files"] = git(integration, "diff", "--name-only", "--diff-filter=U", "-z").stdout.strip("\0").split("\0")
            task["status"] = "conflict"
            task["error"] = short(r.stderr or r.stdout, 3000)
            state["status"] = "blocked"
            state["blocked_reason"] = "merge_conflict"
            history(state, "integration_conflict", task_id=task["task_id"])
            return write_state(state), False
    task["status"] = "integrated"
    task["integrated_at"] = now()
    task["integration_head"] = git(integration, "rev-parse", "HEAD").stdout.strip()
    history(state, "task_integrated", task_id=task["task_id"], commit=task.get("commit"))
    write_state(state)
    cleanup_task_worktree(state, task)
    return state, True


def run_verifiers(cwd, commands, allow):
    results = []
    for spec in commands:
        cmd = spec["command"]
        if cmd not in set(allow):
            item = {"command": cmd, "args": spec.get("args", []), "exit_code": None, "stdout": "", "stderr": "command not allowed"}
        else:
            try:
                r = run([cmd] + list(spec.get("args", [])), cwd=cwd, check=False, timeout=120)
                item = {
                    "command": cmd,
                    "args": spec.get("args", []),
                    "exit_code": r.returncode,
                    "stdout": short(r.stdout, 2500),
                    "stderr": short(r.stderr, 2500),
                }
            except Exception as exc:
                item = {"command": cmd, "args": spec.get("args", []), "exit_code": None, "stdout": "", "stderr": short(exc, 2500)}
        results.append(item)
        if item["exit_code"] != 0:
            break
    return results


def cleanup_graph_worktrees(state):
    root = Path(state["integration_worktree"]).parent
    for task in state.get("tasks", []):
        cleanup_task_worktree(state, task)
    git(state["cwd"], "worktree", "remove", "--force", state["integration_worktree"], check=False)
    git(state["cwd"], "branch", "-D", state["integration_branch"], check=False)
    shutil.rmtree(root, ignore_errors=True)
    git(state["cwd"], "worktree", "prune", check=False)


def resolution_blocked(state, reason, detail=None):
    state.update(status="blocked", blocked_reason="merge_conflict", resolve_conflict_requested=False)
    state["resolution_error"] = {"reason": reason, "detail": short(detail)}
    history(state, "conflict_resolution_rejected", reason=reason)
    return write_state(state)


def child_lock_held(loop_id):
    path = state_root() / "loops" / (loop_id + ".lock")
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def requeue_stopped_tasks(state):
    for task in state["tasks"]:
        if not task.get("stopped_by_graph"):
            continue
        deadline = time.monotonic() + 5
        while child_lock_held(task["loop_id"]) and time.monotonic() < deadline:
            time.sleep(.05)
        if child_lock_held(task["loop_id"]):
            return resolution_blocked(state, "child_shutdown_pending", task["task_id"])
        cleanup_task_worktree(state, task)
        generation = task.get("generation", 0) + 1
        task.update(status="pending", generation=generation, stopped_by_graph=False,
                    loop_id=None, worktree=None, branch=None, commit=None, error=None,
                    base_head=None, started_at=None, completed_at=None, integrated_at=None)
        history(state, "task_requeued_after_conflict", task_id=task["task_id"], generation=generation)
        write_state(state)
    return state


def resolve_conflict(state):
    task = task_by_id(state, state["resolution_task_id"])
    wt = state["integration_worktree"]
    head = git(wt, "rev-parse", "HEAD").stdout.strip()
    if git(wt, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() != "refs/heads/" + state["integration_branch"]:
        return resolution_blocked(state, "integration_branch_changed")
    if task["status"] != "integrated":
        base = task.get("integration_base")
        if not base:
            return resolution_blocked(state, "missing_integration_checkpoint", "This legacy graph cannot safely adopt a resolution; start a new graph.")
        resolution = task.get("resolution_commit")
        if head not in {base, resolution}:
            return resolution_blocked(state, "integration_head_changed")
        if git(wt, "ls-files", "-u", "-z").stdout:
            return resolution_blocked(state, "unresolved_files")
        if git(wt, "diff", "--quiet", check=False).returncode != 0 or git(wt, "ls-files", "--others", "--exclude-standard", "-z").stdout:
            return resolution_blocked(state, "stage_all_resolution_changes")
        pick = git_path(wt, "CHERRY_PICK_HEAD")
        if pick.exists() and pick.read_text().strip() != task["commit"]:
            return resolution_blocked(state, "unexpected_cherry_pick")
        if any(git_path(wt, name).exists() for name in ["MERGE_HEAD", "REVERT_HEAD"]):
            return resolution_blocked(state, "unexpected_git_operation")
        tree = git(wt, "write-tree").stdout.strip()
        if resolution:
            if tree != task["resolution_tree"]:
                return resolution_blocked(state, "resolution_checkpoint_changed")
        else:
            changed = [p for p in git(wt, "diff", "--cached", "--name-only", "--no-renames", "-z", base).stdout.split("\0") if p]
            unauthorized = [p for p in changed if not path_in_scope(p, task.get("write_paths") or [])]
            if unauthorized:
                return resolution_blocked(state, "resolution_out_of_scope", unauthorized)
            state["status"] = "resolving"
            history(state, "conflict_resolution_verification_started", task_id=task["task_id"])
            write_state(state)
            results = run_verifiers(wt, task.get("verify_commands") or [], state["allow_commands"])
            state = load_state(state["graph_id"])
            task = task_by_id(state, task["task_id"])
            if cancel_path(state["graph_id"]).exists():
                return write_state(state)
            task["resolution_verification"] = results
            if not results or results[-1]["exit_code"] != 0:
                return resolution_blocked(state, "resolution_verification_failed", results)
            if (git(wt, "rev-parse", "HEAD").stdout.strip() != head or git(wt, "write-tree").stdout.strip() != tree
                    or git(wt, "diff", "--quiet", check=False).returncode != 0
                    or git(wt, "ls-files", "--others", "--exclude-standard", "-z").stdout):
                return resolution_blocked(state, "resolution_changed_during_verification")
            resolution = git(wt, "-c", "user.name=codex-bridge", "-c", "user.email=codex-bridge@localhost",
                             "commit-tree", tree, "-p", base,
                             input_text="codex graph manual resolution " + task["task_id"] + "\n").stdout.strip()
            task.update(resolution_commit=resolution, resolution_tree=tree)
            history(state, "conflict_resolution_verified", task_id=task["task_id"], commit=resolution)
            # A restart can finish this exact verified commit, without rerunning
            # cherry-pick/reset against the user's staged resolution.
            write_state(state)
        if cancel_path(state["graph_id"]).exists():
            return write_state(state)
        if head != resolution:
            git(wt, "reset", "--hard", resolution)
        task.update(status="integrated", integration_head=resolution, integrated_at=now(), error=None)
        history(state, "task_integrated", task_id=task["task_id"], commit=task["commit"], manual_resolution=True)
        write_state(state)
        cleanup_task_worktree(state, task)
    state = requeue_stopped_tasks(state)
    if state["status"] == "blocked":
        return state
    state.update(status="running", blocked_reason=None, resolve_conflict_requested=False, resolution_error=None)
    history(state, "conflict_resolution_adopted", task_id=task["task_id"])
    return write_state(state)


class BaseLocked(RuntimeError):
    pass


def git_path(cwd, name):
    path = Path(git(cwd, "rev-parse", "--git-path", name).stdout.strip())
    return path if path.is_absolute() else Path(cwd) / path


def expected_base_ref(state):
    if "base_ref" in state:
        return state["base_ref"]
    branch = state.get("base_branch")
    return "refs/heads/" + branch if branch and branch != "HEAD" else None


def base_unchanged(state):
    cwd = state["cwd"]
    return (git(cwd, "rev-parse", "HEAD").stdout.strip() == state["base_head"]
            and (git(cwd, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() or None) == expected_base_ref(state)
            and not git(cwd, "status", "--porcelain", "--untracked-files=all").stdout.strip())


def release_base_locks(state):
    # Called only while holding this graph's flock. Never remove a foreign lock,
    # even if a Git user replaced one of ours after an interrupted transaction.
    for entry in state.get("base_apply_locks", []):
        for name in [entry["path"], entry.get("temporary")]:
            if not name:
                continue
            path = Path(name)
            try:
                if path.read_text() == entry["content"]:
                    path.unlink()
            except (OSError, UnicodeError):
                pass
    state["base_apply_locks"] = []
    write_state(state)


@contextmanager
def base_apply_lock(state):
    release_base_locks(state)
    cwd = state["cwd"]
    if git(cwd, "config", "--local", "--get", "extensions.refStorage", check=False).stdout.strip() not in {"", "files"}:
        raise BaseLocked("unsupported Git reference storage")
    names = ["index.lock", "HEAD.lock"]
    ref = git(cwd, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if ref:
        names.append(ref + ".lock")
    nonce = uuid.uuid4().hex
    content = json.dumps({"codex_graph": state["graph_id"], "nonce": nonce}) + "\n"
    entries = [{"path": str(git_path(cwd, name)), "content": content,
                "temporary": str(git_path(cwd, name).parent / (".codex-graph-lock-" + nonce + "-" + str(index)))}
               for index, name in enumerate(names)]
    # Persist ownership intent first. A fully written temporary inode is linked
    # atomically into Git's lock path, so even a kill at acquisition is recoverable.
    state["base_apply_locks"] = entries
    write_state(state)
    try:
        for entry in entries:
            target = Path(entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            name = entry["temporary"]
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(name, target)
                except FileExistsError:
                    raise BaseLocked("existing Git lock: " + str(target))
            finally:
                Path(name).unlink(missing_ok=True)
        history(state, "base_apply_locked")
        write_state(state)
        yield
    finally:
        release_base_locks(state)


def integration_ready(state, reason, error=None):
    state.update(status="integration_ready", blocked_reason=reason)
    if error:
        state["apply_error"] = short(error)
    history(state, "integration_ready", reason=reason)
    return write_state(state)


def apply_verified_patch(state, patch):
    if state.get("apply_started"):
        return integration_ready(state, "apply_interrupted")
    try:
        with base_apply_lock(state):
            if cancel_path(state["graph_id"]).exists():
                return write_state(state)
            if not base_unchanged(state):
                return integration_ready(state, "base_changed")
            if patch:
                check = git(state["cwd"], "apply", "--check", "-", check=False, input_text=patch)
                if check.returncode != 0:
                    return integration_ready(state, "patch_apply_check_failed", check.stderr or check.stdout)
                if cancel_path(state["graph_id"]).exists():
                    return write_state(state)
                if not base_unchanged(state):
                    return integration_ready(state, "base_changed")
                state.update(status="applying", apply_started=True)
                write_state(state)
                if cancel_path(state["graph_id"]).exists():
                    return write_state(state)
                apply = git(state["cwd"], "apply", "-", check=False, input_text=patch)
                if apply.returncode != 0:
                    state["apply_started"] = False
                    return integration_ready(state, "patch_apply_failed", apply.stderr or apply.stdout)
            state.update(status="completed", blocked_reason=None, apply_error=None)
            history(state, "graph_completed", integration_head=state["integration_head"])
            write_state(state)
    except BaseLocked as exc:
        return integration_ready(state, "base_locked", exc)
    cleanup_graph_worktrees(state)
    return load_state(state["graph_id"])


def finalize_graph(state):
    integration = state["integration_worktree"]
    commands = state.get("final_verify_commands") or []
    if not commands:
        state["status"] = "verification_required"
        history(state, "final_verification_required")
        return write_state(state)
    verified_head = git(integration, "rev-parse", "HEAD").stdout.strip()
    state["status"] = "verifying"
    write_state(state)
    results = run_verifiers(integration, commands, state["allow_commands"])
    state = load_state(state["graph_id"])
    if cancel_path(state["graph_id"]).exists():
        return write_state(state)
    state["final_verification"] = results
    if not results or results[-1]["exit_code"] != 0:
        state["status"] = "blocked"
        state["blocked_reason"] = "final_verification_failed"
        history(state, "final_verification_failed")
        return write_state(state)
    history(state, "final_verification_passed", commands=len(results))

    integration_head = git(integration, "rev-parse", "HEAD").stdout.strip()
    if integration_head != verified_head or git(integration, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        state["status"] = "blocked"
        state["blocked_reason"] = "final_verifier_modified_tree"
        return write_state(state)
    patch = git(integration, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", state["base_head"], integration_head).stdout
    patch_tmp = Path(state["patch_file"] + ".tmp")
    patch_tmp.write_text(patch, errors="surrogateescape")
    patch_tmp.chmod(0o600)
    os.replace(patch_tmp, state["patch_file"])
    try:
        Path(state["patch_file"]).chmod(0o600)
    except OSError:
        pass
    state["integration_head"] = integration_head

    if not state.get("apply_to_base", True):
        state["status"] = "integration_ready"
        history(state, "integration_ready", reason="apply_disabled")
        return write_state(state)

    return apply_verified_patch(state, patch)


def worker(graph_id):
    lock = try_lock(graph_id)
    if lock is None:
        return
    try:
        state = load_state(graph_id)
        if state.get("status") in {"completed", "cancelled"}:
            return
        release_base_locks(state)
        state["owner_pid"] = os.getpid()
        history(state, "graph_worker_started", owner_pid=os.getpid())
        write_state(state)
        state = setup_integration(state)
        if state.get("resolve_conflict_requested"):
            state = resolve_conflict(state)
            if state.get("status") in {"blocked", "cancelled"}:
                return

        while True:
            state = load_state(graph_id)
            if cancel_path(graph_id).exists() or state.get("status") == "cancelled":
                state["status"] = "cancelled"
                write_state(state)
                return

            # Recover launch intent and interrupted children without creating
            # duplicate task worktrees or losing a completed child result.
            for task in state["tasks"]:
                if task["status"] == "launching":
                    state = launch_task(state, task)
            # Reconcile running task loops.
            changed = False
            for task in state["tasks"]:
                if task["status"] != "running" or not task.get("loop_id"):
                    continue
                loop = loop_call("status", task["loop_id"])
                if loop["status"] == "interrupted":
                    loop = loop_call("resume", task["loop_id"])
                if loop["status"] == "completed":
                    state = commit_task(state, task)
                    if state.get("status") in {"blocked", "cancelled"}:
                        return
                    changed = True
                elif loop["status"] in {"blocked", "failed", "cancelled", "verification_required"}:
                    task["status"] = "failed"
                    task["error"] = loop.get("last_error") or {"status": loop["status"]}
                    state["status"] = "blocked"
                    state["blocked_reason"] = "task_" + loop["status"]
                    history(state, "task_failed", task_id=task["task_id"], loop_status=loop["status"])
                    write_state(state)
                    return

            # Integrate only the completed prefix of the topological order.
            # Later independent tasks may finish first, but they wait here so merge
            # ordering and conflict behavior remain deterministic across runs.
            for tid in state["topological_order"]:
                task = task_by_id(state, tid)
                if task["status"] == "integrated":
                    continue
                if task["status"] != "completed":
                    break
                if not all(task_by_id(state, dep)["status"] == "integrated" for dep in task["depends_on"]):
                    break
                state, ok = integrate_task(state, task)
                changed = True
                if not ok:
                    return

            if all(t["status"] == "integrated" for t in state["tasks"]):
                finalize_graph(state)
                return

            # Launch dependency-ready tasks up to concurrency limit.
            running = sum(1 for t in state["tasks"] if t["status"] == "running")
            slots = max(0, int(state["max_parallel"]) - running)
            if slots:
                for tid in state["topological_order"]:
                    if slots <= 0:
                        break
                    task = task_by_id(state, tid)
                    if cancel_path(graph_id).exists():
                        return
                    if task["status"] != "pending":
                        continue
                    if not all(task_by_id(state, dep)["status"] == "integrated" for dep in task["depends_on"]):
                        continue
                    state = launch_task(state, task)
                    slots -= 1
                    changed = True
                running = sum(1 for t in state["tasks"] if t["status"] == "running")
                if running > state.get("peak_parallel", 0):
                    state["peak_parallel"] = running
                    write_state(state)

            if not changed:
                pending = [t for t in state["tasks"] if t["status"] == "pending"]
                running = [t for t in state["tasks"] if t["status"] == "running"]
                if pending and not running:
                    state["status"] = "blocked"
                    state["blocked_reason"] = "scheduler_deadlock"
                    history(state, "scheduler_deadlock")
                    write_state(state)
                    return
            time.sleep(0.15)
    except Exception as exc:
        try:
            state = load_state(graph_id)
            if state.get("status") != "cancelled":
                state["status"] = "failed"
                state["error"] = short(exc, 4000)
                history(state, "graph_worker_failed", error=short(exc, 1000))
                write_state(state)
        except Exception:
            pass
    finally:
        try:
            state = load_state(graph_id)
            release_base_locks(state)
            if state.get("status") in {"cancelled", "blocked", "failed"}:
                cancel_children(state)
            if state.get("status") in {"cancelled", "completed"}:
                cleanup_graph_worktrees(state)
            state["owner_pid"] = None
            write_state(state)
        except Exception:
            pass
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create", "status", "resume", "cancel", "recover", "worker"])
    parser.add_argument("graph_id", nargs="?")
    args = parser.parse_args()
    if args.command == "create":
        cmd_create()
    elif args.command == "status":
        cmd_status(args.graph_id)
    elif args.command == "resume":
        cmd_resume(args.graph_id)
    elif args.command == "cancel":
        cmd_cancel(args.graph_id)
    elif args.command == "recover":
        cmd_recover()
    elif args.command == "worker":
        worker(args.graph_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"error": str(exc)})
        sys.exit(1)
