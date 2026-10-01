"""Phase 4 fault and lifecycle regression tests; uses only the supplied fake Codex."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ['CODEX_BRIDGE_STATE'])
FAKE = os.environ['CODEX_BIN']
assert 'fake-codex' in FAKE, 'these tests must never invoke real Codex'


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, check=True, **kw).stdout.strip()


def call(command, graph_id=None, cfg=None, state_root=None):
    args = [sys.executable, str(HERE / 'graph_supervisor.py'), command]
    if graph_id:
        args.append(graph_id)
    env = {**os.environ, **({"CODEX_BRIDGE_STATE": str(state_root)} if state_root else {})}
    return json.loads(run(args, input=json.dumps(cfg or {}), env=env))


def git(repo, *args):
    return run(['git', '-C', str(repo), *args])


def wait(gid, predicate, timeout=15, state_root=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = call('status', gid, state_root=state_root)
        if predicate(state):
            return state
        time.sleep(.08)
    raise AssertionError('graph timeout: ' + json.dumps(state))


def done(gid, state_root=None):
    return wait(gid, lambda s: s['status'] in {'completed', 'blocked', 'failed', 'cancelled', 'integration_ready', 'verification_required'} and not s.get('owner_pid'), state_root=state_root)


ok = [{'command': 'node', 'args': ['-e', 'process.exit(0)']}]

def task(tid='a', goal='__WRITE_FILE__:a.txt:A'):
    return {'task_id': tid, 'goal': goal, 'write_paths': ['a.txt', 'b.txt', 'base.txt'], 'verify_commands': ok}


def config(repo, tasks=None, **extra):
    return dict(cwd=str(repo), tasks=tasks or [task()], codex_bin=FAKE,
                final_verify_commands=ok, watchdog_interval_sec=.2,
                hang_timeout_sec=3, terminate_grace_sec=.2, **extra)


def new_repo(root, name):
    repo = root / name
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    (repo / 'base.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=test', '-c', 'user.email=test@localhost', 'commit', '-m', 'base')
    return repo


def expire(gid):
    p = STATE / 'graphs' / (gid + '.json')
    state = json.loads(p.read_text())
    state['worker_spawned_at'] = '2000-01-01T00:00:00Z'
    p.write_text(json.dumps(state))


def owner_exit(state):
    pid = state['owner_pid']
    os.kill(pid, signal.SIGKILL)
    # flock release is the authoritative evidence, not process existence (zombies).
    import graph_supervisor as g
    deadline = time.monotonic() + 3
    while g.lock_held(state['graph_id']) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not g.lock_held(state['graph_id'])
    expire(state['graph_id'])


with tempfile.TemporaryDirectory(prefix='codex-graph-tests-') as temp:
    root = Path(temp)
    repo = new_repo(root, 'disabled')
    state = done(call('create', cfg=config(repo, apply_to_base=False))['graph_id'])
    assert state['status'] == 'integration_ready', state
    assert not (repo / 'a.txt').exists()
    assert Path(state['patch_file']).read_text()
    assert done(call('resume', state['graph_id'])['graph_id'])['status'] == 'integration_ready'
    call('cancel', state['graph_id'])
    assert not Path(state['integration_worktree']).exists()

    for change in ['dirty', 'head', 'branch']:
        repo = new_repo(root, change)
        initial = call('create', cfg=config(repo, [task(goal='__ACTIVITY_LONG__ __WRITE_FILE__:a.txt:A')]))
        wait(initial['graph_id'], lambda s: any(t['status'] == 'running' for t in s['tasks']))
        if change == 'branch':
            git(repo, 'checkout', '-b', 'user-branch')
        else:
            (repo / 'user.txt').write_text('user change\n')
        if change == 'head':
            git(repo, 'add', '.')
            git(repo, '-c', 'user.name=test', '-c', 'user.email=test@localhost', 'commit', '-m', 'user')
        state = done(initial['graph_id'])
        assert (state['status'], state['blocked_reason']) == ('integration_ready', 'base_changed'), state
        assert not (repo / 'a.txt').exists()
        if change != 'branch':
            assert (repo / 'user.txt').read_text() == 'user change\n'
        else:
            assert git(repo, 'status', '--porcelain') == ''
        call('cancel', state['graph_id'])

    repo = new_repo(root, 'cancel')
    tasks = [task('a', '__SLOW__ __WRITE_FILE__:a.txt:A'), task('b', '__SLOW__ __WRITE_FILE__:b.txt:B')]
    initial = call('create', cfg=config(repo, tasks))
    running = wait(initial['graph_id'], lambda s: all(t['status'] == 'running' for t in s['tasks']) and s['peak_parallel'] == 2)
    assert call('cancel', initial['graph_id'])['status'] == 'cancelled'
    time.sleep(.7)
    assert call('resume', initial['graph_id'])['status'] == 'cancelled'
    for t in running['tasks']:
        loop = json.loads((STATE / 'loops' / (t['loop_id'] + '.json')).read_text())
        assert loop['status'] == 'cancelled', loop
        attempts = loop['attempts']
        time.sleep(.2)
        later = json.loads((STATE / 'loops' / (t['loop_id'] + '.json')).read_text())
        assert later['attempts'] == attempts
    assert git(repo, 'status', '--porcelain') == ''

    repo = new_repo(root, 'cancel-verification')
    verifier = [{'command': 'node', 'args': ['-e', 'setTimeout(()=>process.exit(0),1000)']}]
    cfg = config(repo)
    cfg['final_verify_commands'] = verifier
    initial = call('create', cfg=cfg)
    wait(initial['graph_id'], lambda s: s['status'] == 'verifying')
    call('cancel', initial['graph_id'])
    time.sleep(1.4)
    assert not (repo / 'a.txt').exists(), 'cancel during verifier applied patch'
    assert call('status', initial['graph_id'])['status'] == 'cancelled'

    repo = new_repo(root, 'no-change')
    state = done(call('create', cfg=config(repo, [task(goal='no changes')]))['graph_id'])
    assert state['status'] == 'completed', state
    assert state['tasks'][0]['commit'] is None
    assert git(repo, 'status', '--porcelain') == ''
    assert not Path(state['integration_worktree']).exists()
    assert not git(repo, 'branch', '--list', 'codex-graph-*')

    repo = new_repo(root, 'ignore-reveal')
    state = done(call('create', cfg=config(repo, [task(goal='__WRITE_FILE__:a.txt:A __IGNORED_ARTIFACT__')]))['graph_id'])
    assert state['status'] == 'completed', state
    assert (repo / 'a.txt').read_text() == 'A\n'
    assert not (repo / '.omx').exists() and not (repo / '.gitignore').exists()
    assert git(repo, 'show', '--format=', '--name-only', state['tasks'][0]['commit']) == 'a.txt'

    repo = new_repo(root, 'agent-commit')
    state = done(call('create', cfg=config(repo, [task(goal='__WRITE_FILE__:a.txt:A __COMMIT__')]))['graph_id'])
    assert state['status'] == 'completed', state
    assert state['tasks'][0]['commit'], state
    assert (repo / 'a.txt').read_text() == 'A\n'
    assert git(repo, 'rev-parse', 'HEAD') == state['base_head']

    repo = new_repo(root, 'binary-delete')
    binary_task = task(goal='binary and deletion')
    binary_task['verify_commands'] = [{'command': 'node', 'args': ['-e', "const fs=require('fs');fs.writeFileSync('a.txt',Buffer.from([0,255,128,10]));if(fs.existsSync('base.txt'))fs.unlinkSync('base.txt')"]}]
    cfg = config(repo, [binary_task])
    cfg['final_verify_commands'] = [{'command': 'node', 'args': ['-e', "const fs=require('fs');if(!fs.readFileSync('a.txt').equals(Buffer.from([0,255,128,10]))||fs.existsSync('base.txt'))process.exit(1)"]}]
    state = done(call('create', cfg=cfg)['graph_id'])
    assert state['status'] == 'completed', state
    assert (repo / 'a.txt').read_bytes() == bytes([0,255,128,10])
    assert not (repo / 'base.txt').exists()

    repo = new_repo(root, 'verifier-mutation')
    cfg = config(repo)
    cfg['final_verify_commands'] = [{'command': 'node', 'args': ['-e', "require('fs').writeFileSync('a.txt','bad')"]}]
    state = done(call('create', cfg=cfg)['graph_id'])
    assert (state['status'], state['blocked_reason']) == ('blocked', 'final_verifier_modified_tree'), state
    assert not (repo / 'a.txt').exists()
    call('cancel', state['graph_id'])

    repo = new_repo(root, 'duplicate-and-recovery')
    initial = call('create', cfg=config(repo, [task(goal='__ACTIVITY_LONG__ __WRITE_FILE__:a.txt:A')]))
    running = wait(initial['graph_id'], lambda s: s['owner_pid'] and s['tasks'][0]['status'] == 'running')
    duplicates = [subprocess.Popen([sys.executable, str(HERE / 'graph_supervisor.py'), 'worker', initial['graph_id']]) for _ in range(3)]
    for proc in duplicates:
        assert proc.wait(timeout=3) == 0
    assert len([h for h in call('status', initial['graph_id'])['history'] if h['type'] == 'graph_worker_started']) == 1
    owner_exit(running)
    assert call('status', initial['graph_id'])['status'] == 'interrupted'
    assert initial['graph_id'] in call('recover')['recovered']
    state = done(initial['graph_id'])
    assert state['status'] == 'completed', state
    assert state['tasks'][0]['loop_id'] == running['tasks'][0]['loop_id']
    assert len([h for h in state['history'] if h['type'] == 'task_started']) == 1

    repo = new_repo(root, 'interrupted-child')
    initial = call('create', cfg=config(repo, [task(goal='__ACTIVITY_LONG__ __WRITE_FILE__:a.txt:A')]))
    running = wait(initial['graph_id'], lambda s: s['owner_pid'] and s['tasks'][0]['status'] == 'running')
    loop_path = STATE / 'loops' / (running['tasks'][0]['loop_id'] + '.json')
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        child = json.loads(loop_path.read_text())
        if child.get('child_pid') and child.get('thread_id'):
            break
        time.sleep(.02)
    assert child.get('child_pid') and child.get('thread_id'), child
    owner_exit(running)
    os.kill(child['owner_pid'], signal.SIGKILL)
    try:
        os.killpg(child['child_pgid'], signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(.1)
    child = json.loads(loop_path.read_text())
    child['worker_spawned_at'] = '2000-01-01T00:00:00Z'
    loop_path.write_text(json.dumps(child))
    call('recover')
    state = done(initial['graph_id'])
    assert state['status'] == 'completed', state
    restored = json.loads(loop_path.read_text())
    assert restored['thread_id'] == child['thread_id']
    assert restored['attempts'] == 2, restored

    # Crash precisely between the external mutation and its graph checkpoint.
    for boundary in ['launch', 'cherry-pick', 'apply']:
        repo = new_repo(root, boundary)
        code = '''import graph_supervisor as g, os, json
from pathlib import Path
g.spawn_worker=lambda _:None
g.cmd_create()
s=json.loads(max(g.graphs_dir().glob('*.json'),key=lambda p:p.stat().st_mtime).read_text())
'''
        if boundary == 'launch':
            code += '''original=g.loop_call
def crash(command,*a,**kw):
    result=original(command,*a,**kw)
    if command=='create': os._exit(77)
    return result
g.loop_call=crash
'''
        elif boundary == 'cherry-pick':
            code += '''original=g.git
def crash(cwd,*args,**kw):
    result=original(cwd,*args,**kw)
    if 'cherry-pick' in args and '--abort' not in args and result.returncode==0: os._exit(77)
    return result
g.git=crash
'''
        else:
            code += """original=g.git
def crash(cwd,*args,**kw):
    result=original(cwd,*args,**kw)
    if 'apply' in args and '--check' not in args and result.returncode==0: os._exit(77)
    return result
g.git=crash
"""
        code += "g.worker(s['graph_id'])\n"
        result = subprocess.run([sys.executable, '-c', code], cwd=HERE, input=json.dumps(config(repo)), capture_output=True, text=True)
        assert result.returncode == 77, result
        initial = json.loads(result.stdout.splitlines()[0])
        expire(initial['graph_id'])
        call('recover')
        state = done(initial['graph_id'])
        assert state['status'] == ('integration_ready' if boundary == 'apply' else 'completed'), state
        assert (repo / 'a.txt').read_text() == 'A\n'
        if boundary == 'apply':
            assert state['apply_started']
            assert not any(h['type'] == 'graph_completed' for h in state['history'])
            call('cancel', state['graph_id'])
        assert len([h for h in state['history'] if h['type'] == 'task_integrated']) == 1
        assert git(repo, 'rev-parse', 'HEAD') == initial['base_head']

    # Native Git locks refuse foreign owners and serialize graphs across state roots.
    for lock_name in ['index.lock', 'HEAD.lock', 'refs/heads/main.lock']:
        repo = new_repo(root, 'foreign-' + lock_name.replace('/', '-'))
        native = repo / git(repo, 'rev-parse', '--git-path', lock_name)
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text('foreign owner\n')
        initial = call('create', cfg=config(repo))
        state = done(initial['graph_id'])
        assert (state['status'], state['blocked_reason']) == ('integration_ready', 'base_locked'), state
        assert native.read_text() == 'foreign owner\n'
        assert not (repo / 'a.txt').exists()
        native.unlink()
        state = done(call('resume', initial['graph_id'])['graph_id'])
        assert state['status'] == 'completed', state
        assert not any((repo / '.git').rglob('.codex-graph-lock-*'))

    repo = new_repo(root, 'native-lock-crash')
    code = """import graph_supervisor as g, os, json
from pathlib import Path
g.spawn_worker=lambda _:None
g.cmd_create()
s=json.loads(max(g.graphs_dir().glob('*.json'),key=lambda p:p.stat().st_mtime).read_text())
original=g.git
def crash(cwd,*args,**kw):
    result=original(cwd,*args,**kw)
    if 'apply' in args and '--check' in args: os._exit(77)
    return result
g.git=crash
g.worker(s['graph_id'])
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=HERE, input=json.dumps(config(repo)), capture_output=True, text=True)
    assert result.returncode == 77, result
    initial = json.loads(result.stdout.splitlines()[0])
    crashed = json.loads((STATE / 'graphs' / (initial['graph_id'] + '.json')).read_text())
    assert len(crashed['base_apply_locks']) == 3
    assert all(Path(e['path']).read_text() == e['content'] for e in crashed['base_apply_locks'])
    expire(initial['graph_id'])
    call('recover')
    state = done(initial['graph_id'])
    assert state['status'] == 'completed', state
    assert all(not Path(e['path']).exists() for e in crashed['base_apply_locks'])

    repo = new_repo(root, 'native-link-crash')
    code = """import graph_supervisor as g, os, json
g.spawn_worker=lambda _:None
g.cmd_create()
s=json.loads(max(g.graphs_dir().glob('*.json'),key=lambda p:p.stat().st_mtime).read_text())
original=g.os.link
def crash(src,dst):
    original(src,dst)
    os._exit(77)
g.os.link=crash
g.worker(s['graph_id'])
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=HERE, input=json.dumps(config(repo)), capture_output=True, text=True)
    assert result.returncode == 77, result
    initial = json.loads(result.stdout.splitlines()[0])
    assert (repo / '.git' / 'index.lock').exists()
    expire(initial['graph_id'])
    call('recover')
    state = done(initial['graph_id'])
    assert state['status'] == 'completed', state
    assert not any((repo / '.git').rglob('.codex-graph-lock-*'))
    assert not (repo / '.git' / 'index.lock').exists()

    repo = new_repo(root, 'native-git-exclusion')
    code = """import graph_supervisor as g, json, subprocess
from pathlib import Path
g.spawn_worker=lambda _:None
g.cmd_create()
s=json.loads(max(g.graphs_dir().glob('*.json'),key=lambda p:p.stat().st_mtime).read_text())
original=g.git
probed=False
def probe(cwd,*args,**kw):
    global probed
    if 'apply' in args and '--check' in args and not probed:
        probed=True
        tree=original(cwd,'rev-parse','HEAD^{tree}').stdout.strip()
        alternate=original(cwd,'-c','user.name=test','-c','user.email=test@localhost','commit-tree',tree,'-p',s['base_head'],input_text='alternate\\n').stdout.strip()
        for command in [['add','base.txt'],['symbolic-ref','HEAD','refs/heads/other'],['update-ref','refs/heads/main',alternate]]:
            result=original(cwd,*command,check=False)
            assert result.returncode!=0, command
        (g.state_root()/'native-probe.json').write_text(json.dumps({'all_git_mutations_blocked':True}))
    return original(cwd,*args,**kw)
g.git=probe
g.worker(s['graph_id'])
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=HERE, input=json.dumps(config(repo)), capture_output=True, text=True)
    assert result.returncode == 0, result
    initial = json.loads(result.stdout.splitlines()[0])
    state = done(initial['graph_id'])
    assert state['status'] == 'completed', state
    assert json.loads((STATE / 'native-probe.json').read_text())['all_git_mutations_blocked']
    assert git(repo, 'symbolic-ref', 'HEAD') == 'refs/heads/main'

    repo = new_repo(root, 'concurrent-apply')
    gate = STATE / 'apply-gate'
    release = STATE / 'apply-release'
    code = """import graph_supervisor as g, json, time
from pathlib import Path
g.spawn_worker=lambda _:None
g.cmd_create()
s=json.loads(max(g.graphs_dir().glob('*.json'),key=lambda p:p.stat().st_mtime).read_text())
original=g.git
def pause(cwd,*args,**kw):
    if 'apply' in args and '--check' in args:
        (g.state_root()/'apply-gate').touch()
        deadline=time.monotonic()+15
        while not (g.state_root()/'apply-release').exists():
            if time.monotonic()>deadline: raise RuntimeError('apply gate timeout')
            time.sleep(.02)
    return original(cwd,*args,**kw)
g.git=pause
g.worker(s['graph_id'])
"""
    proc = subprocess.Popen([sys.executable, '-c', code], cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.stdin.write(json.dumps(config(repo)))
    proc.stdin.close()
    initial = json.loads(proc.stdout.readline())
    deadline = time.monotonic() + 8
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert gate.exists()
    second_root = root / 'second-state'
    second = call('create', cfg=config(repo, [task('b', '__WRITE_FILE__:b.txt:B')]), state_root=second_root)
    state = done(second['graph_id'], state_root=second_root)
    assert state['blocked_reason'] == 'base_locked', state
    assert not (repo / 'b.txt').exists()
    release.touch()
    assert proc.wait(timeout=8) == 0, proc.stderr.read()
    assert done(initial['graph_id'])['status'] == 'completed'
    state = done(call('resume', second['graph_id'], state_root=second_root)['graph_id'], state_root=second_root)
    assert state['blocked_reason'] == 'base_changed', state
    call('cancel', second['graph_id'], state_root=second_root)

    def conflict_config(repo, with_sibling=False):
        tasks = [task('left', '__ACTIVITY_LONG__ __WRITE_FILE__:base.txt:LEFT'), task('right', '__ACTIVITY_LONG__ __WRITE_FILE__:base.txt:RIGHT')]
        tasks[1]['verify_commands'] = [{'command': 'node', 'args': ['-e', "if(require('fs').readFileSync('base.txt','utf8')!=='RIGHT\\n')process.exit(1)"]}]
        if with_sibling:
            c = task('slow', '__SLOW__ __WRITE_FILE__:c.txt:C')
            c['write_paths'] = ['c.txt']
            d = task('dependent', '__WRITE_FILE__:d.txt:D')
            d.update(write_paths=['d.txt'], depends_on=['right'], verify_commands=[{'command':'node','args':['-e',"if(require('fs').readFileSync('base.txt','utf8')!=='RIGHT\\n')process.exit(1)"]}])
            tasks += [c, d]
        cfg = config(repo, tasks, max_parallel=3)
        cfg['final_verify_commands'] = tasks[1]['verify_commands']
        cfg['hang_timeout_sec'] = 20
        return cfg

    repo = new_repo(root, 'manual-resolution')
    initial = call('create', cfg=conflict_config(repo, True))
    state = done(initial['graph_id'])
    assert state['blocked_reason'] == 'merge_conflict', state
    wt = Path(state['integration_worktree'])
    assert git(wt, 'ls-files', '-u')
    assert git(repo, 'status', '--porcelain') == ''
    assert call('resume', initial['graph_id'])['status'] == 'blocked'
    state = done(call('resume', initial['graph_id'], {'resolve_conflict': True})['graph_id'])
    assert state['resolution_error']['reason'] == 'unresolved_files', state
    (wt / 'base.txt').write_text('WRONG\n')
    git(wt, 'add', 'base.txt')
    (wt / 'base.txt').write_text('RIGHT\n')
    state = done(call('resume', initial['graph_id'], {'resolve_conflict': True})['graph_id'])
    assert state['resolution_error']['reason'] == 'stage_all_resolution_changes', state
    (wt / 'base.txt').write_text('WRONG\n')
    state = done(call('resume', initial['graph_id'], {'resolve_conflict': True})['graph_id'])
    assert state['resolution_error']['reason'] == 'resolution_verification_failed', state
    (wt / 'base.txt').write_text('RIGHT\n')
    (wt / 'extra.txt').write_text('unrelated\n')
    git(wt, 'add', '.')
    state = done(call('resume', initial['graph_id'], {'resolve_conflict': True})['graph_id'])
    assert state['resolution_error']['reason'] == 'resolution_out_of_scope', state
    git(wt, 'rm', '-f', 'extra.txt')
    state = done(call('resume', initial['graph_id'], {'resolve_conflict': True})['graph_id'])
    assert state['status'] == 'completed', state
    assert (repo / 'base.txt').read_text() == 'RIGHT\n'
    assert (repo / 'c.txt').read_text() == 'C\n' and (repo / 'd.txt').read_text() == 'D\n'
    assert next(t for t in state['tasks'] if t['task_id'] == 'slow')['generation'] == 1
    assert any(h['type'] == 'task_integrated' and h.get('manual_resolution') for h in state['history'])
    assert git(repo, 'rev-parse', 'HEAD') == initial['base_head']

    repo = new_repo(root, 'resolution-cancel')
    cfg = conflict_config(repo)
    cfg['tasks'][1]['verify_commands'] = [{'command': 'node', 'args': ['-e', "if(process.cwd().endsWith('/integration'))setTimeout(()=>process.exit(0),1000)"]}]
    initial = call('create', cfg=cfg)
    state = done(initial['graph_id'])
    wt = Path(state['integration_worktree'])
    (wt / 'base.txt').write_text('RIGHT\n')
    git(wt, 'add', 'base.txt')
    call('resume', initial['graph_id'], {'resolve_conflict': True})
    wait(initial['graph_id'], lambda s: s['status'] == 'resolving')
    call('cancel', initial['graph_id'])
    state = done(initial['graph_id'])
    assert state['status'] == 'cancelled'
    assert (repo / 'base.txt').read_text() == 'base\n'
    assert call('resume', initial['graph_id'], {'resolve_conflict': True})['status'] == 'cancelled'

    repo = new_repo(root, 'resolution-crash')
    initial = call('create', cfg=conflict_config(repo))
    state = done(initial['graph_id'])
    wt = Path(state['integration_worktree'])
    git(wt, 'cherry-pick', '--abort')
    assert not git(wt, 'ls-files', '-u')
    (wt / 'base.txt').write_text('RIGHT\n')
    git(wt, 'add', 'base.txt')
    code = """import graph_supervisor as g, os, sys
g.spawn_worker=lambda _:None
g.cmd_resume(sys.argv[1])
original=g.git
def crash(cwd,*args,**kw):
    result=original(cwd,*args,**kw)
    if args and args[0]=='reset' and '--hard' in args: os._exit(77)
    return result
g.git=crash
g.worker(sys.argv[1])
"""
    result = subprocess.run([sys.executable, '-c', code, initial['graph_id']], cwd=HERE, input=json.dumps({'resolve_conflict': True}), capture_output=True, text=True)
    assert result.returncode == 77, result
    expire(initial['graph_id'])
    call('recover')
    state = done(initial['graph_id'])
    assert state['status'] == 'completed', state
    assert (repo / 'base.txt').read_text() == 'RIGHT\n'
    assert len([h for h in state['history'] if h['type'] == 'task_integrated' and h.get('manual_resolution')]) == 1

    repo = new_repo(root, 'invalid')
    for tasks in [[dict(task(), depends_on=['missing'])], [dict(task(), depends_on=['b']), dict(task('b'), depends_on=['a'])], [task(), task()]]:
        result = subprocess.run([sys.executable, str(HERE / 'graph_supervisor.py'), 'create'], input=json.dumps(config(repo,tasks)), capture_output=True,text=True)
        assert result.returncode == 1, result.stdout
    (repo / 'dirty.txt').write_text('dirty')
    result = subprocess.run([sys.executable, str(HERE / 'graph_supervisor.py'), 'create'], input=json.dumps(config(repo)), capture_output=True,text=True)
    assert result.returncode == 1 and 'clean' in result.stdout

print('graph lifecycle/fault tests: ok')
