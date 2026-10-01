#!/usr/bin/env python3
"""Bounded, independent service recovery. Never submits coding goals."""
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent


def atomic(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.tmp-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True  # Unknown ownership is never treated as idle.


def probe(url, token=None, post=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password:
        return {'ok': False, 'reason': 'invalid_local_url'}
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=b'' if post else None), timeout=3) as response:
            data = response.read(65537)
            if len(data) > 65536:
                return {'ok': False, 'reason': 'oversized_response'}
            try:
                decoded = json.loads(data)
            except (ValueError, UnicodeError):
                decoded = {}
            return {'ok': response.status == 200, 'code': response.status, 'data': decoded}
    except urllib.error.HTTPError as exc:
        return {'ok': False, 'code': exc.code}
    except Exception:
        return {'ok': False, 'reason': 'probe_failed'}


def disk_idle(root):
    """Conservative guard for an unresponsive bridge: saved jobs + direct children."""
    try:
        marker = json.loads((root / 'jobs-runtime.json').read_text())
        if marker.get('version') != 1:
            return False
        for path in (root / 'jobs').glob('*.json'):
            job = json.loads(path.read_text())
            if not job.get('finished') and job.get('child_pid') and alive(job['child_pid']):
                return False
        if alive(marker['owner_pid']):
            result = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, timeout=3, check=True)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2 and int(parts[1]) == marker['owner_pid']:
                    return False
        return True
    except Exception:
        return False


def action(service, config, force=True):
    label = config[service + '_label']
    if not re.fullmatch(r'(?:com\.daimon-server\.mcp|local\.codex-bridge)(?:\.[a-z0-9-]+)?', label):
        raise ValueError('invalid service label')
    subprocess.run(['launchctl', 'kickstart'] + (['-k'] if force else []) + ['gui/%d/%s' % (os.getuid(), label)],
                   capture_output=True, timeout=10, check=True)


def recover_workers():
    root = Path(os.environ.get('CODEX_BRIDGE_STATE', str(Path.home() / '.codex-bridge')))
    for path in (root / 'loops').glob('*.json'):
        state = json.loads(path.read_text())
        if not re.fullmatch(r'[A-Za-z0-9_-]+', state['loop_id']):
            raise RuntimeError('invalid loop checkpoint')
        with open(path.with_suffix('.lock'), 'a+') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if state.get('child_pid') and alive(state['child_pid']):
                raise RuntimeError('existing child without supervisor ownership; manual inspection required')
    # Existing IDs only, with flock/attempt budgets/cancel markers enforced by the
    # supervisors. Failed, cancelled, blocked and verification-required stay put.
    for script in ['loop_supervisor.py', 'graph_supervisor.py']:
        subprocess.run([os.environ.get('PYTHON_BIN', '/usr/bin/python3'), str(HERE / script), 'recover'],
                       capture_output=True, timeout=15, check=True)


def tick(state, health, idle, restart, recover, stamp):
    state.setdefault('streaks', {'bridge': 0, 'tunnel': 0})
    state.setdefault('last_restart', {})
    state.setdefault('restart_attempts', [])
    state.setdefault('history', [])
    state['restart_attempts'] = [x for x in state['restart_attempts'] if stamp - x < 3600]
    old = state.get('status')
    state['status'] = 'healthy' if health['bridge'] and health['tunnel'] else 'degraded'
    for service in ['bridge', 'tunnel']:
        state['streaks'][service] = 0 if health[service] else state['streaks'][service] + 1
        if health[service] or state['streaks'][service] < 3:
            continue
        if service == 'tunnel' and not health['bridge']:
            continue  # Repair the local server first, never bounce both at once.
        if service == 'bridge' and not idle:
            state['status'] = 'paused_for_active_or_unknown_jobs'
            continue
        previous = state['last_restart'].get(service)
        count = sum(x.get('service') == service and stamp - x['time'] < 3600 for x in state['history'])
        cooldown = min(1800, 300 * 2 ** max(0, count - 1))
        if len(state['restart_attempts']) >= 3:
            state['status'] = 'attention_required'
            continue
        if previous is not None and stamp - previous < cooldown:
            continue
        # Save attempt intent before invoking the OS; a kill cannot bypass the
        # rate limit and reissue an action on every scheduler tick.
        state['restart_attempts'].append(stamp)
        state['last_restart'][service] = stamp
        event = {'time': stamp, 'service': service, 'action': 'restart_requested'}
        state['history'].append(event)
        state['status'] = 'recovering'
        restart(service, state)
    if health['bridge']:
        try:
            recover()
        except Exception:
            state['status'] = 'worker_recovery_check_failed'
    state['history'] = state['history'][-100:]
    state['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    state['health'] = health
    return state, old != state['status']


def main():
    root = Path(os.environ.get('CODEX_BRIDGE_STATE', str(Path.home() / '.codex-bridge')))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = json.loads((root / 'recovery-config.json').read_text())
    state_file = root / 'recovery-state.json'
    with open(root / 'recovery.lock', 'a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        bridge = probe(config['bridge_url'] + '/health')
        tunnel = probe(config['tunnel_url'] + '/readyz')
        token = (root / 'token').read_text().strip()
        runtime = probe(config['bridge_url'] + '/runtime', token)
        idle = disk_idle(root)
        if runtime['ok'] and runtime.get('data', {}).get('version') == 1:
            idle = idle and runtime['data'].get('active_jobs') == 0 and runtime['data'].get('in_flight_tools', 0) == 0 and runtime['data'].get('journal_healthy') is True
        else:
            try:
                owner = json.loads((root / 'jobs-runtime.json').read_text())['owner_pid']
                idle = idle and not alive(owner)
            except Exception:
                idle = False
        def restart(service, pending):
            atomic(state_file, pending)
            prepared = False
            try:
                if service == 'bridge' and runtime['ok']:
                    guard = probe(config['bridge_url'] + '/recovery/prepare', token, post=True)
                    prepared = guard['ok'] and guard.get('data', {}).get('owner_pid') == runtime['data']['owner_pid']
                    if not prepared:
                        pending['status'] = 'paused_for_active_or_unknown_jobs'
                        return
                action(service, config, force=service == 'tunnel' or prepared)
            except Exception:
                pending['status'] = 'restart_failed'
                if prepared:
                    probe(config['bridge_url'] + '/recovery/release', token, post=True)
        state, changed = tick(state, {'bridge': bridge['ok'], 'tunnel': tunnel['ok']}, idle, restart, recover_workers, time.time())
        atomic(state_file, state)
        if changed or state.get('history', []) and state['history'][-1]['time'] > time.time() - 10:
            print('[%s] %s' % (dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S'), state['status']))


if __name__ == '__main__':
    main()
