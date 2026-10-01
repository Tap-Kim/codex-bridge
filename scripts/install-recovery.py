#!/usr/bin/env python3
"""Install the bounded PC recovery check alongside the existing daimon services."""
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


def atomic(path, content):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.tmp-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This installer supports macOS launchd.')
    home = Path.home()
    root = Path(os.environ.get('CODEX_BRIDGE_STATE', str(home / '.codex-bridge')))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    agents = home / 'Library/LaunchAgents'
    daimon = home / '.daimon-server'
    use_daimon = (agents / 'com.daimon-server.mcp.codex-bridge.plist').exists()
    bridge_label = 'com.daimon-server.mcp.codex-bridge' if use_daimon else 'local.codex-bridge'
    tunnel_label = 'com.daimon-server.mcp.codex-tunnel' if use_daimon else 'local.codex-bridge.tunnel'
    for label in [bridge_label, tunnel_label]:
        if not (agents / (label + '.plist')).exists():
            raise SystemExit('Existing service required: ' + label)
    label = 'com.daimon-server.bridge-recovery' if use_daimon else 'local.codex-bridge.recovery'
    logs = daimon / 'logs' if use_daimon else root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    backup = root / 'backups' / ('recovery-install-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True, mode=0o700)
    config_file = root / 'recovery-config.json'
    plist_file = agents / (label + '.plist')
    registry_file = daimon / 'jobs.json'
    for path in [config_file, plist_file, registry_file] if use_daimon else [config_file, plist_file]:
        if path.exists():
            target = backup / path.name
            shutil.copyfile(path, target)
            target.chmod(0o600)
    config = {'bridge_url': 'http://127.0.0.1:7421', 'tunnel_url': 'http://127.0.0.1:7422',
              'bridge_label': bridge_label, 'tunnel_label': tunnel_label}
    atomic(config_file, json.dumps(config, indent=2).encode())
    repo = Path(__file__).resolve().parent.parent
    runtime = root / 'recovery-runtime'
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    for name in ['connection_watchdog.py', 'loop_supervisor.py', 'graph_supervisor.py']:
        source_file = repo / name
        target_file = runtime / name
        atomic(target_file, source_file.read_bytes())
        target_file.chmod(0o600)
    source = runtime / 'connection_watchdog.py'
    log = str(logs / 'bridge-recovery.log')
    plist = {'Label': label, 'ProgramArguments': ['/usr/bin/python3', str(source)],
             'EnvironmentVariables': {'CODEX_BRIDGE_STATE': str(root), 'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
             'WorkingDirectory': str(runtime), 'RunAtLoad': True, 'StartInterval': 30,
             'ProcessType': 'Background', 'StandardOutPath': log, 'StandardErrorPath': str(logs / 'bridge-recovery.err.log')}
    atomic(plist_file, plistlib.dumps(plist))
    if use_daimon:
        registry = json.loads(registry_file.read_text())
        entry = {'name': 'bridge-recovery', 'label': label, 'schedule': 'every 30s', 'log': log}
        existing = next((job for job in registry['jobs'] if job['name'] == entry['name']), None)
        if existing:
            if existing['label'] != label:
                raise SystemExit('Conflicting recovery job registration')
            existing.update(entry)
        else:
            registry['jobs'].append(entry)
        atomic(registry_file, json.dumps(registry, ensure_ascii=False, indent=2).encode())
    domain = 'gui/%d' % os.getuid()
    subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
    subprocess.run(['launchctl', 'enable', domain + '/' + label], check=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(plist_file)], check=True)
    print(json.dumps({'installed': label, 'interval_seconds': 30, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
