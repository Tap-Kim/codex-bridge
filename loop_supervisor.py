#!/usr/bin/env python3
"""Persistent loop supervisor for codex-bridge.

Each loop runs in its own detached worker process. The worker owns a per-loop
flock for its lifetime, so the Node MCP bridge may restart without killing the
loop and duplicate workers cannot race the same state file.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ACTIVE = {"running", "verifying", "repairing"}
TERMINAL = {"completed", "blocked", "failed", "cancelled", "verification_required"}
DEFAULT_ALLOW = "git,npm,npx,pnpm,yarn,node,bun,python3,pytest,go,cargo,make,ls,cat,grep,tsc,eslint,vitest,jest,prettier,gh"
MAX_HISTORY = 80
MAX_ACTIVITY = 50
MAX_VERIFY = 20


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def age_seconds(timestamp):
    try:
        parsed = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())
    except Exception:
        return float("inf")


def short(value, limit=3000):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def fingerprint(category, message):
    normalized = " ".join(str(message or "").lower().split())
    return hashlib.sha256((category + ":" + normalized).encode()).hexdigest()[:16]


def error_obj(category, message):
    message = short(message, 3000)
    return {"category": category, "message": message, "fingerprint": fingerprint(category, message)}


def strategy_for_repeat(count):
    if count >= 4:
        return 3, (
            "Architecture escalation: the same failure has survived multiple repair attempts. "
            "Do not repeat prior edits. Reconsider the implementation boundary, dependency choice, "
            "or control flow while preserving the user goal and minimizing unrelated changes."
        )
    if count >= 3:
        return 2, (
            "Root-cause reset: the same failure repeated three times. Re-check assumptions against "
            "the actual code and runtime evidence, isolate the smallest failing path, and choose a "
            "materially different repair strategy."
        )
    if count >= 2:
        return 1, (
            "Strategy switch: the same failure repeated. Do not retry the previous fix. Inspect fresh "
            "evidence and choose a materially different implementation approach."
        )
    return 0, (
        "Normal repair: fix the observed failure using the current evidence, then let the external "
        "supervisor verify the result."
    )


def record_failure(state, error):
    fp = error.get("fingerprint")
    table = state.setdefault("failure_fingerprints", {})
    item = dict(table.get(fp) or {"count": 0, "category": error.get("category")})
    item["count"] = int(item.get("count", 0)) + 1
    item["category"] = error.get("category")
    item["last_at"] = now()
    table[fp] = item
    # Bound persisted fingerprint history. Keep the most recently updated entries.
    if len(table) > 20:
        ordered = sorted(table.items(), key=lambda kv: kv[1].get("last_at", ""), reverse=True)[:20]
        state["failure_fingerprints"] = dict(ordered)
        item = state["failure_fingerprints"].get(fp, item)

    level, directive = strategy_for_repeat(item["count"])
    previous = int(state.get("strategy_level", 0))
    state["strategy_level"] = level
    state["strategy_directive"] = directive
    state["strategy_fingerprint"] = fp
    state["strategy_repeat_count"] = item["count"]
    if level > previous:
        history(
            state,
            "strategy_escalated",
            level=level,
            fingerprint=fp,
            repeat_count=item["count"],
        )
    return state


def state_dir():
    return Path(os.environ.get("CODEX_BRIDGE_STATE", str(Path.home() / ".codex-bridge")))


def loops_dir():
    p = state_dir() / "loops"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        p.chmod(0o700)
    except OSError:
        pass
    return p


def state_path(loop_id):
    if not loop_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in loop_id):
        raise ValueError("invalid loop_id")
    return loops_dir() / (loop_id + ".json")


def lock_path(loop_id):
    return loops_dir() / (loop_id + ".lock")


def cancel_path(loop_id):
    return loops_dir() / (loop_id + ".cancel")


def load_state(loop_id):
    state = json.loads(state_path(loop_id).read_text())
    # Migrate older persisted loop state lazily so existing loops remain usable.
    version = int(state.get("schema_version", 1))
    if version < 2:
        state.setdefault("owner_pid", None)
        state.setdefault("worker_spawned_at", state.get("updated_at") or now())
        state.setdefault("child_pid", None)
        state.setdefault("child_pgid", None)
        state.setdefault("codex_bin", os.environ.get("CODEX_BIN", "codex"))
        state.setdefault("allow_commands", DEFAULT_ALLOW.split(","))
        state.setdefault("watchdog_interval_sec", 5.0)
        state.setdefault("hang_timeout_sec", 300.0)
        state.setdefault("terminate_grace_sec", 5.0)
        state.setdefault("last_heartbeat_at", state.get("updated_at") or now())
        state.setdefault("last_activity_at", state.get("updated_at") or now())
        state.setdefault("last_progress_at", state.get("updated_at") or now())
    if version < 3:
        state.setdefault("failure_fingerprints", {})
        state.setdefault("strategy_level", 0)
        state.setdefault("strategy_directive", strategy_for_repeat(0)[1])
        state.setdefault("strategy_fingerprint", None)
        state.setdefault("strategy_repeat_count", 0)
    state["schema_version"] = 3
    return state


def write_state(state):
    # Cancellation is monotonic across processes. A separate marker closes the
    # race where a worker with a stale in-memory state could overwrite
    # "cancelled" after the controller has already requested cancellation.
    if cancel_path(state["loop_id"]).exists():
        state["status"] = "cancelled"
    state["updated_at"] = now()
    state["history"] = state.get("history", [])[-MAX_HISTORY:]
    state["activity"] = state.get("activity", [])[-MAX_ACTIVITY:]
    if state.get("last_verification"):
        state["last_verification"] = state["last_verification"][-MAX_VERIFY:]
    target = state_path(state["loop_id"])
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


def history(state, event, **detail):
    state.setdefault("history", []).append({"at": now(), "type": event, **detail})
    state["history"] = state["history"][-MAX_HISTORY:]


def read_input():
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def try_lock(loop_id):
    lock = open(lock_path(loop_id), "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        lock.close()
        return None


def lock_held(loop_id):
    lock = try_lock(loop_id)
    if lock is None:
        return True
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()
    return False


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def spawn_worker(loop_id):
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "worker", loop_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=os.environ.copy(),
    )


def snapshot(state):
    out = dict(state)
    out["recoverable"] = out.get("status") == "interrupted"
    return out


def validate_config(cfg):
    cwd = Path(cfg["cwd"]).resolve()
    if not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    commands = cfg.get("verify_commands") or []
    if len(commands) > MAX_VERIFY:
        raise ValueError("too many verify_commands")
    allow = set(cfg.get("allow_commands") or DEFAULT_ALLOW.split(","))
    for spec in commands:
        if spec.get("command") not in allow:
            raise ValueError("verifier command not allowed: " + str(spec.get("command")))
        if not isinstance(spec.get("args", []), list):
            raise ValueError("verifier args must be an array")
    return str(cwd), commands, sorted(allow)


def cmd_create():
    cfg = read_input()
    cwd, commands, allow = validate_config(cfg)
    # Graph workers reserve the child id before creation, so a crash between
    # create and graph checkpoint cannot orphan an untracked loop.
    loop_id = cfg.get("_loop_id") or uuid.uuid4().hex[:12]
    if state_path(loop_id).exists():
        existing = load_state(loop_id)
        if existing["cwd"] != cwd or existing["goal"] != cfg["goal"]:
            raise ValueError("reserved loop id belongs to another task")
        emit(snapshot(existing))
        return
    timestamp = now()
    state = {
        "schema_version": 3,
        "loop_id": loop_id,
        "cwd": cwd,
        "goal": cfg["goal"],
        "status": "running",
        "attempts": 0,
        "max_attempts": int(cfg.get("max_attempts", 5)),
        "thread_id": None,
        "active_job_id": None,
        "owner_pid": None,
        "worker_spawned_at": timestamp,
        "child_pid": None,
        "child_pgid": None,
        "codex_bin": cfg.get("codex_bin", "codex"),
        "allow_commands": allow,
        "verify_commands": commands,
        "watchdog_interval_sec": float(cfg.get("watchdog_interval_sec", 5)),
        "hang_timeout_sec": float(cfg.get("hang_timeout_sec", 300)),
        "terminate_grace_sec": float(cfg.get("terminate_grace_sec", 5)),
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_heartbeat_at": timestamp,
        "last_activity_at": timestamp,
        "last_progress_at": timestamp,
        "last_error": None,
        "last_verification": [],
        "failure_fingerprints": {},
        "strategy_level": 0,
        "strategy_directive": strategy_for_repeat(0)[1],
        "strategy_fingerprint": None,
        "strategy_repeat_count": 0,
        "activity": [],
        "history": [{"at": timestamp, "type": "loop_created"}],
    }
    if state["max_attempts"] < 1 or state["max_attempts"] > 20:
        raise ValueError("max_attempts must be between 1 and 20")
    if state["watchdog_interval_sec"] < 0.1 or state["hang_timeout_sec"] < 0.5 or state["terminate_grace_sec"] < 0.1:
        raise ValueError("watchdog/timeout values are too small")
    write_state(state)
    spawn_worker(loop_id)
    emit(snapshot(state))


def stale_to_interrupted(state):
    if state.get("status") in ACTIVE and not lock_held(state["loop_id"]):
        # create/resume spawns the detached worker asynchronously. Give it a
        # short lease to acquire its flock before declaring the loop stale.
        spawned_at = state.get("worker_spawned_at") or state.get("updated_at")
        if age_seconds(spawned_at) < 2.0:
            return state
        state["status"] = "interrupted"
        state["owner_pid"] = None
        state["child_pid"] = None
        state["child_pgid"] = None
        state["active_job_id"] = None
        state["last_error"] = state.get("last_error") or error_obj("process", "Loop worker stopped while the loop was active.")
        history(state, "interrupted", recoverable=True)
        write_state(state)
    return state


def cmd_status(loop_id):
    state = stale_to_interrupted(load_state(loop_id))
    emit(snapshot(state))


def cmd_resume(loop_id):
    cfg = read_input()
    state = stale_to_interrupted(load_state(loop_id))
    if "verify_commands" in cfg:
        allow = set(state.get("allow_commands") or DEFAULT_ALLOW.split(","))
        commands = cfg.get("verify_commands") or []
        for spec in commands:
            if spec.get("command") not in allow:
                raise ValueError("verifier command not allowed: " + str(spec.get("command")))
        state["verify_commands"] = commands
        write_state(state)
    if state["status"] in {"completed", "cancelled"}:
        emit(snapshot(state))
        return
    if state["status"] == "blocked" and state.get("attempts", 0) >= state.get("max_attempts", 1):
        emit(snapshot(state))
        return
    if state["status"] == "verification_required" and not state.get("verify_commands"):
        emit(snapshot(state))
        return
    if lock_held(loop_id):
        emit(snapshot(load_state(loop_id)))
        return
    state["status"] = "repairing" if state.get("thread_id") else "running"
    state["owner_pid"] = None
    state["worker_spawned_at"] = now()
    history(state, "manual_resume", has_thread=bool(state.get("thread_id")))
    write_state(state)
    spawn_worker(loop_id)
    emit(snapshot(state))


def terminate_process(pgid, pid, grace, state=None, reason="cancel"):
    """Terminate a Codex child. Prefer its process group, fall back to child PID.

    Some macOS execution environments reject killpg across detached sessions with
    EPERM even for the same user. Cancellation must still remain effective via
    the monotonic cancel marker, so signaling errors are best-effort.
    """
    if not pgid and not pid:
        return False

    def send(sig):
        if pgid:
            try:
                os.killpg(int(pgid), sig)
                return True
            except (ProcessLookupError, PermissionError):
                pass
        if pid:
            try:
                os.kill(int(pid), sig)
                return True
            except (ProcessLookupError, PermissionError):
                pass
        return False

    if not send(signal.SIGTERM):
        return False

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        alive = False
        if pid:
            alive = process_alive(pid)
        elif pgid:
            try:
                os.killpg(int(pgid), 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
        if not alive:
            return False
        time.sleep(0.05)

    killed = send(signal.SIGKILL)
    if killed and state is not None:
        history(
            state,
            "watchdog_kill" if reason == "watchdog" else "forced_kill",
            pgid=int(pgid) if pgid else None,
            pid=int(pid) if pid else None,
        )
    return killed


def cmd_cancel(loop_id):
    # Write the marker first so no concurrent worker write can resurrect the loop.
    cancel_path(loop_id).touch(mode=0o600, exist_ok=True)
    state = load_state(loop_id)
    state["status"] = "cancelled"
    history(state, "cancelled")
    pgid = state.get("child_pgid")
    write_state(state)
    if pgid:
        terminate_process(pgid, state.get("child_pid"), float(state.get("terminate_grace_sec", 5)), state, "cancel")
    state = load_state(loop_id)
    state["child_pid"] = None
    state["child_pgid"] = None
    state["active_job_id"] = None
    write_state(state)
    emit(snapshot(state))


def cmd_recover():
    recovered = []
    for p in loops_dir().glob("*.json"):
        try:
            state = stale_to_interrupted(json.loads(p.read_text()))
            if state.get("status") == "interrupted":
                state["status"] = "repairing" if state.get("thread_id") else "running"
                state["worker_spawned_at"] = now()
                history(state, "startup_recovery", has_thread=bool(state.get("thread_id")))
                write_state(state)
                spawn_worker(state["loop_id"])
                recovered.append(state["loop_id"])
        except Exception:
            continue
    emit({"recovered": recovered})


def parse_codex_line(line, state):
    try:
        ev = json.loads(line)
    except Exception:
        return
    if ev.get("type") == "thread.started" and ev.get("thread_id"):
        state["thread_id"] = ev["thread_id"]
        state["last_progress_at"] = now()
        history(state, "thread_started", thread_id=ev["thread_id"])
    elif ev.get("type") == "item.completed" and ev.get("item"):
        item = ev["item"]
        typ = item.get("type")
        if typ == "agent_message":
            state.setdefault("activity", []).append("agent: " + short(item.get("text", ""), 700))
        elif typ == "command_execution":
            state.setdefault("activity", []).append("command exit=" + str(item.get("exit_code")))
        elif typ == "file_change":
            state.setdefault("activity", []).append("file_change")
        elif typ == "error":
            state.setdefault("activity", []).append("error: " + short(item.get("message", ""), 700))
        state["last_progress_at"] = now()
    elif ev.get("type") in {"turn.failed", "error"}:
        message = ((ev.get("error") or {}).get("message") or ev.get("message") or "Codex error")
        state["last_error"] = error_obj("process", message)


def stream_reader(pipe, queue, name):
    try:
        for line in iter(pipe.readline, ""):
            queue.append((name, line.rstrip("\n")))
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def codex_prompt(state, continuing):
    if continuing:
        err = state.get("last_error") or {}
        return "\n".join([
            "Continue working toward this goal:",
            state["goal"],
            "",
            "The previous supervised attempt did not pass or was interrupted.",
            "Attempt %s of %s completed." % (state.get("attempts", 0), state["max_attempts"]),
            "Failure category: %s" % err.get("category", "unknown"),
            "Failure fingerprint: %s" % err.get("fingerprint", "none"),
            "Same-fingerprint repeat count: %s" % state.get("strategy_repeat_count", 0),
            short(err.get("message", "Previous worker was interrupted."), 2500),
            "",
            "Supervisor strategy directive:",
            state.get("strategy_directive") or strategy_for_repeat(0)[1],
            "",
            "Fix the root cause. Do not merely explain it or repeat the same failed approach.",
            "Finish the turn when the implementation is ready; the external supervisor will verify it.",
        ])
    return "\n".join([
        "Goal:",
        state["goal"],
        "",
        "Work autonomously in the current workspace until the implementation for this goal is ready.",
        "Do not claim success based only on your own message; the external supervisor will run explicit verification commands.",
    ])


def launch_codex(state):
    continuing = bool(state.get("thread_id"))
    prompt = codex_prompt(state, continuing)
    if continuing:
        args = [state["codex_bin"], "exec", "--json", "--skip-git-repo-check", "-C", state["cwd"], "resume", state["thread_id"], prompt]
    else:
        args = [state["codex_bin"], "exec", "--json", "--skip-git-repo-check", "-C", state["cwd"], "-s", "workspace-write", prompt]
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    proc = subprocess.Popen(
        args,
        cwd=state["cwd"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    return proc


def run_codex_attempt(state):
    proc = launch_codex(state)
    state["active_job_id"] = "pid-" + str(proc.pid)
    state["child_pid"] = proc.pid
    state["child_pgid"] = os.getpgid(proc.pid)
    state["last_activity_at"] = now()
    state["last_heartbeat_at"] = now()
    write_state(state)

    queue = []
    qlock = threading.Lock()

    class SafeQueue:
        def append(self, item):
            with qlock:
                queue.append(item)

        def drain(self):
            with qlock:
                out = list(queue)
                queue.clear()
                return out

    safeq = SafeQueue()
    threads = [
        threading.Thread(target=stream_reader, args=(proc.stdout, safeq, "stdout"), daemon=True),
        threading.Thread(target=stream_reader, args=(proc.stderr, safeq, "stderr"), daemon=True),
    ]
    for t in threads:
        t.start()

    interval = float(state.get("watchdog_interval_sec", 5))
    timeout = float(state.get("hang_timeout_sec", 300))
    grace = float(state.get("terminate_grace_sec", 5))
    last_activity_mono = time.monotonic()
    next_heartbeat = 0.0
    hung = False
    stderr_tail = []

    while proc.poll() is None:
        current = load_state(state["loop_id"])
        if current.get("status") == "cancelled":
            terminate_process(state.get("child_pgid"), state.get("child_pid"), grace, current, "cancel")
            proc.wait()
            state = load_state(state["loop_id"])
            return state, "cancelled"

        had_output = False
        for source, line in safeq.drain():
            had_output = True
            last_activity_mono = time.monotonic()
            state["last_activity_at"] = now()
            if source == "stdout":
                parse_codex_line(line, state)
            else:
                stderr_tail.append(line)
                stderr_tail = stderr_tail[-30:]
            state["activity"] = state.get("activity", [])[-MAX_ACTIVITY:]
        if had_output:
            write_state(state)

        if time.monotonic() >= next_heartbeat:
            state["last_heartbeat_at"] = now()
            write_state(state)
            next_heartbeat = time.monotonic() + interval

        if time.monotonic() - last_activity_mono >= timeout:
            hung = True
            state["last_error"] = error_obj("hang", "Codex produced no stdout/stderr activity for %.1f seconds." % timeout)
            record_failure(state, state["last_error"])
            history(state, "watchdog_hung", timeout_sec=timeout, fingerprint=state["last_error"]["fingerprint"])
            write_state(state)
            forced = terminate_process(state.get("child_pgid"), state.get("child_pid"), grace, state, "watchdog")
            history(state, "watchdog_terminated", forced=forced)
            write_state(state)
            break
        time.sleep(min(0.1, max(0.02, interval / 5)))

    try:
        code = proc.wait(timeout=max(1.0, grace + 1))
    except subprocess.TimeoutExpired:
        terminate_process(state.get("child_pgid"), state.get("child_pid"), grace, state, "watchdog")
        code = proc.wait()

    for source, line in safeq.drain():
        state["last_activity_at"] = now()
        if source == "stdout":
            parse_codex_line(line, state)
        else:
            stderr_tail.append(line)
            stderr_tail = stderr_tail[-30:]

    state["child_pid"] = None
    state["child_pgid"] = None
    state["active_job_id"] = None
    state["last_progress_at"] = now()
    if hung:
        write_state(state)
        return state, "hung"
    if code != 0:
        msg = "\n".join(stderr_tail[-10:]) or "Codex exited with code %s" % code
        state["last_error"] = error_obj("process", msg)
        record_failure(state, state["last_error"])
        history(state, "process_interrupted", exit_code=code, fingerprint=state["last_error"]["fingerprint"])
        write_state(state)
        return state, "failed"
    history(state, "codex_attempt_completed", exit_code=code)
    write_state(state)
    return state, "done"


def run_verifiers(state):
    commands = state.get("verify_commands") or []
    if not commands:
        state["status"] = "verification_required"
        state["last_progress_at"] = now()
        history(state, "verification_required")
        return write_state(state)

    state["status"] = "verifying"
    state["last_progress_at"] = now()
    write_state(state)
    results = []
    allow = set(state.get("allow_commands") or DEFAULT_ALLOW.split(","))
    failure = None
    for spec in commands:
        cmd = spec["command"]
        if cmd not in allow:
            failure = {"command": cmd, "args": spec.get("args", []), "exit_code": None, "stdout": "", "stderr": "verifier command not allowed"}
            results.append(failure)
            break
        try:
            r = subprocess.run(
                [cmd] + list(spec.get("args", [])),
                cwd=state["cwd"],
                text=True,
                capture_output=True,
                timeout=120,
                env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""), "CI": "1"},
            )
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
            failure = item
            break

    state = load_state(state["loop_id"])
    state["last_verification"] = results
    state["last_progress_at"] = now()
    if failure is None:
        state["status"] = "completed"
        state["last_error"] = None
        history(state, "verification_passed", commands=len(results))
    else:
        msg = "Verifier failed: %s %s\nexit=%s\nstdout:\n%s\nstderr:\n%s" % (
            failure["command"], " ".join(failure.get("args", [])), failure["exit_code"], failure.get("stdout", ""), failure.get("stderr", "")
        )
        state["last_error"] = error_obj("verification", msg)
        record_failure(state, state["last_error"])
        if state["attempts"] >= state["max_attempts"]:
            state["status"] = "blocked"
            history(state, "blocked", reason="max_attempts", fingerprint=state["last_error"]["fingerprint"])
        else:
            state["status"] = "repairing"
            history(state, "verification_failed", fingerprint=state["last_error"]["fingerprint"])
    return write_state(state)


def worker(loop_id):
    lock = try_lock(loop_id)
    if lock is None:
        return
    try:
        state = load_state(loop_id)
        if state.get("status") in TERMINAL and state.get("status") != "interrupted":
            return
        state["owner_pid"] = os.getpid()
        history(state, "worker_started", owner_pid=os.getpid())
        write_state(state)

        while True:
            state = load_state(loop_id)
            if state.get("status") == "cancelled":
                return
            if state.get("status") == "verification_required":
                if state.get("verify_commands"):
                    state = run_verifiers(state)
                    if state.get("status") != "repairing":
                        return
                else:
                    return
            if state.get("status") in {"completed", "blocked", "failed", "cancelled"}:
                return
            if state.get("attempts", 0) >= state.get("max_attempts", 1):
                state["status"] = "blocked"
                history(state, "blocked", reason="max_attempts")
                write_state(state)
                return

            state["attempts"] = int(state.get("attempts", 0)) + 1
            state["status"] = "repairing" if state.get("thread_id") else "running"
            state["last_progress_at"] = now()
            history(state, "resume_attempt_started" if state.get("thread_id") else "attempt_started", attempt=state["attempts"])
            write_state(state)

            try:
                state, result = run_codex_attempt(state)
            except Exception as exc:
                state = load_state(loop_id)
                state["child_pid"] = None
                state["child_pgid"] = None
                state["active_job_id"] = None
                state["last_error"] = error_obj("process", exc)
                record_failure(state, state["last_error"])
                history(state, "spawn_or_process_error", fingerprint=state["last_error"]["fingerprint"])
                write_state(state)
                result = "failed"

            if result == "cancelled":
                return
            state = load_state(loop_id)
            if result in {"hung", "failed"}:
                if state["attempts"] >= state["max_attempts"]:
                    state["status"] = "blocked"
                    history(state, "blocked", reason="max_attempts", fingerprint=(state.get("last_error") or {}).get("fingerprint"))
                    write_state(state)
                    return
                state["status"] = "repairing"
                write_state(state)
                continue

            state = run_verifiers(state)
            if state.get("status") != "repairing":
                return
    finally:
        try:
            state = load_state(loop_id)
            state["owner_pid"] = None
            state["child_pid"] = None
            state["child_pgid"] = None
            state["active_job_id"] = None
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
    parser.add_argument("loop_id", nargs="?")
    args = parser.parse_args()
    if args.command == "create":
        cmd_create()
    elif args.command == "status":
        cmd_status(args.loop_id)
    elif args.command == "resume":
        cmd_resume(args.loop_id)
    elif args.command == "cancel":
        cmd_cancel(args.loop_id)
    elif args.command == "recover":
        cmd_recover()
    elif args.command == "worker":
        worker(args.loop_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"error": str(exc)})
        sys.exit(1)
