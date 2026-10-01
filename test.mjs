// Smallest check that fails if the bridge logic breaks. Run: npm test
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-bridge-'));
const root = path.join(tmp, 'root'); fs.mkdirSync(path.join(root, 'proj'), { recursive: true });
fs.writeFileSync(path.join(root, 'proj', 'a.txt'), 'hello\nworld\n');
fs.writeFileSync(path.join(root, 'proj', '.env'), 'SECRET=1');
process.env.CODEX_BRIDGE_STATE = path.join(tmp, 'state');
process.env.CODEX_BRIDGE_ROOTS = root;
process.env.CODEX_BRIDGE_PORT = '0';
// fake CODEX_HOME with one rollout so session tools are exercised without touching the real ~/.codex
const codexHome = path.join(tmp, 'codex'); fs.mkdirSync(path.join(codexHome, 'sessions', '2026', '09', '22'), { recursive: true });
process.env.CODEX_HOME = codexHome;
// fake Codex executable: loop tests must never invoke the real CLI.
const fakeCodex = path.join(tmp, 'fake-codex.mjs');
fs.writeFileSync(fakeCodex, `#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
// Match Codex CLI startup: it consumes piped stdin until EOF before starting.
await new Promise(resolve => { process.stdin.resume(); process.stdin.on('end', resolve); });
const args = process.argv.slice(2);
const resumeIndex = args.indexOf('resume');
const threadId = resumeIndex >= 0 ? args[resumeIndex + 1] : '01a0cafe-0000-7000-8000-000000000001';
const prompt = args.at(-1) || '';
fs.appendFileSync(process.env.FAKE_CODEX_LOG || path.join(process.cwd(), 'fake-codex.log'), JSON.stringify(args) + '\\n');
const writeMatch = /__WRITE_FILE__:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)/.exec(prompt);
if (writeMatch) fs.writeFileSync(path.join(process.cwd(), writeMatch[1]), writeMatch[2] + '\\n');
const extraMatch = /__EXTRA_FILE__:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)/.exec(prompt);
if (extraMatch) fs.writeFileSync(path.join(process.cwd(), extraMatch[1]), extraMatch[2] + '\\n');
if (prompt.includes('__IGNORED_ARTIFACT__')) {
  fs.mkdirSync('.omx', {recursive:true});
  fs.writeFileSync('.omx/plugin.txt', 'artifact');
  fs.writeFileSync('.gitignore', '.omx/\\n');
}
if (prompt.includes('__COMMIT__')) {
  execFileSync('git', ['add', '-A']);
  execFileSync('git', ['-c', 'user.name=fake', '-c', 'user.email=fake@localhost', 'commit', '-m', 'agent commit']);
}
const record = (event) => fs.appendFileSync(process.env.FAKE_EXECUTION_LOG, JSON.stringify({event,prompt,cwd:process.cwd(),at:Date.now()}) + '\\n');
record('start');
console.log(JSON.stringify({ type: 'thread.started', thread_id: threadId }));
const done = () => {
  record('end');
  console.log(JSON.stringify({ type: 'item.completed', item: { type: 'agent_message', text: 'fake complete' } }));
  process.exit(0);
};
if (prompt.includes('__HANG_ONCE__') && resumeIndex < 0) {
  process.on('SIGTERM', () => {});
  setInterval(() => {}, 1000);
} else if (prompt.includes('__ACTIVITY_LONG__')) {
  let n = 0;
  const timer = setInterval(() => {
    console.error('activity-' + (++n));
    if (n >= 6) { clearInterval(timer); done(); }
  }, 150);
} else if (prompt.includes('__SLOW__')) {
  setTimeout(done, 10000);
} else {
  done();
}
`);
fs.chmodSync(fakeCodex, 0o755);
process.env.CODEX_BIN = fakeCodex;
process.env.FAKE_EXECUTION_LOG = path.join(tmp, 'execution.jsonl');
process.env.FAKE_CODEX_LOG = path.join(root, 'proj', 'fake-codex.log');
const TID = '01a0c910-ebb7-7f63-bda4-20198846387b';
const ev = (item) => JSON.stringify({ type: 'event_msg', payload: { type: 'item_completed', item } });
fs.writeFileSync(path.join(codexHome, 'sessions', '2026', '09', '22', `rollout-2026-09-22T21-22-04-${TID}.jsonl`), [
  JSON.stringify({ type: 'session_meta', payload: { id: TID, cwd: path.join(root, 'proj'), timestamp: '2026-09-22T12:22:04Z', originator: 'Codex Desktop' } }),
  JSON.stringify({ type: 'turn_context', payload: { cwd: path.join(root, 'proj') } }),
  ev({ type: 'UserMessage', content: [{ type: 'text', text: 'fix the build' }] }),
  ev({ type: 'CommandExecution', command: ['/bin/zsh', '-lc', 'npm test'], exit_code: 1, aggregated_output: 'FAIL token sk-abcdefghijklmnop' }),
  ev({ type: 'FileChange', changes: { [path.join(root, 'proj', 'a.txt')]: { type: 'update', unified_diff: '' } } }),
  ev({ type: 'AgentMessage', content: [{ type: 'Text', text: 'patched a.txt' }] }),
  JSON.stringify({ type: 'event_msg', payload: { type: 'task_complete', error: { message: 'usage limit' } } }),
].join('\n') + '\n');
// legacy rollout (older date, huge first line): must sort after the new one and parse via response_items
const LEGACY = '019bf952-43df-7863-bcb3-2c6d88d8a321';
fs.mkdirSync(path.join(codexHome, 'sessions', '2026', '01', '26'), { recursive: true });
fs.writeFileSync(path.join(codexHome, 'sessions', '2026', '01', '26', `rollout-2026-01-26T17-01-16-${LEGACY}.jsonl`), [
  JSON.stringify({ type: 'session_meta', payload: { id: LEGACY, cwd: path.join(root, 'proj'), timestamp: '2026-01-26T08:01:16Z', base_instructions: { text: 'x'.repeat(200_000) } } }),
  JSON.stringify({ type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '# AGENTS.md instructions for x' }] } }),
  JSON.stringify({ type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: 'check changes' }] } }),
  JSON.stringify({ type: 'response_item', payload: { type: 'function_call', name: 'shell_command', arguments: JSON.stringify({ command: 'git status -sb' }) } }),
  JSON.stringify({ type: 'response_item', payload: { type: 'function_call_output', output: 'Exit code: 0\nOutput:\n## develop' } }),
  JSON.stringify({ type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'clean tree' }] } }),
].join('\n') + '\n');
// also give the NEW session an oversized first line, like real Codex Desktop rollouts
{ const f = path.join(codexHome, 'sessions', '2026', '09', '22', `rollout-2026-09-22T21-22-04-${TID}.jsonl`); const [first, ...rest] = fs.readFileSync(f, 'utf8').split('\n');
  const m = JSON.parse(first); m.payload.base_instructions = { text: 'y'.repeat(150_000) }; fs.writeFileSync(f, [JSON.stringify(m), ...rest].join('\n')); }
fs.writeFileSync(path.join(codexHome, 'session_index.jsonl'), JSON.stringify({ id: TID, thread_name: 'Build fix' }) + '\n');
const { createHttpServer, guard, parseOrcaDaemonProcesses } = await import('./server.mjs');

const srv = createHttpServer().listen(0, '127.0.0.1');
await new Promise((r) => srv.once('listening', r));
const url = `http://127.0.0.1:${srv.address().port}/mcp`;
const token = fs.readFileSync(path.join(tmp, 'state', 'token'), 'utf8');

// auth
assert.equal((await fetch(url, { method: 'POST', body: '{}' })).status, 401);
assert.equal((await fetch(url.replace('/mcp', '/health'))).status, 200);
assert.equal((await fetch(url.replace('/mcp', '/runtime'))).status, 401);
const runtime = await (await fetch(url.replace('/mcp', '/runtime'), {headers:{Authorization:`Bearer ${token}`}})).json();
assert.equal(runtime.active_jobs, 0);

// guard
assert.throws(() => guard('/etc/passwd'), /outside/);
assert.throws(() => guard(path.join(root, 'proj', '.env')), /protected/);
assert.throws(() => guard(path.join(root, 'proj', '..', '..', 'x')), /outside/);
assert.ok(guard(path.join(root, 'proj', 'new', 'file.ts')));

const orcaPs = [
  '  932 01:23:45 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper /Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js --socket /tmp/orca.sock',
  '  999 00:00:10 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper --type=renderer',
  ' 1000 00:00:01 node daemon-entry.js',
].join('\n');
assert.deepEqual(parseOrcaDaemonProcesses(orcaPs).map((p) => p.pid), [932]);

const client = new Client({ name: 't', version: '0' });
await client.connect(new StreamableHTTPClientTransport(new URL(url), { requestInit: { headers: { Authorization: `Bearer ${token}` } } }));
const names = (await client.listTools()).tools.map((t) => t.name).sort();
assert.deepEqual(names, ['codex_cancel', 'codex_graph_cancel', 'codex_graph_resume', 'codex_graph_start', 'codex_graph_status', 'codex_handoff', 'codex_jobs', 'codex_loop_cancel', 'codex_loop_resume', 'codex_loop_start', 'codex_loop_status', 'codex_recovery_status', 'codex_resume', 'codex_run', 'codex_session_read', 'codex_sessions', 'codex_status', 'edit_file', 'git_diff', 'list_dir', 'list_projects', 'orca_daemon_restart', 'orca_daemon_status', 'read_file', 'run_command', 'search', 'write_file']);
assert.match(client.getInstructions(), /\/goal <objective>[\s\S]*codex_resume/);
const call = async (name, args) => { const r = await client.callTool({ name, arguments: args }); return [r.isError === true, r.content[0].text]; };
const recoveryHeaders={Authorization:`Bearer ${token}`};
assert.equal((await fetch(url.replace('/mcp','/recovery/prepare'),{method:'POST',headers:recoveryHeaders})).status,200);
assert.equal((await call('write_file',{path:`${root}/proj/gated.txt`,content:'must not write'}))[0],true);
assert.equal(fs.existsSync(`${root}/proj/gated.txt`),false);
assert.equal((await fetch(url.replace('/mcp','/recovery/release'),{method:'POST',headers:recoveryHeaders})).status,200);


assert.deepEqual(JSON.parse((await call('list_projects', {}))[1]), [path.join(root, 'proj')]);
assert.match((await call('read_file', { path: `${root}/proj/a.txt` }))[1], /hello\nworld/);
assert.equal((await call('edit_file', { path: `${root}/proj/a.txt`, old_text: 'world', new_text: 'there' }))[0], false);
assert.equal(fs.readFileSync(`${root}/proj/a.txt`, 'utf8'), 'hello\nthere\n');
assert.match((await call('edit_file', { path: `${root}/proj/a.txt`, old_text: 'zzz', new_text: '' }))[1], /not found/);
assert.match((await call('search', { path: `${root}/proj`, pattern: 'the+re' }))[1], /a.txt:2:there/);
assert.match((await call('run_command', { cwd: `${root}/proj`, command: 'rm', args: ['-rf', '/'] }))[1], /not allowed/);
assert.match((await call('run_command', { cwd: `${root}/proj`, command: 'git', args: ['--version'] }))[1], /exit 0\n--- stdout\ngit version/);
assert.match((await call('write_file', { path: `${root}/proj/.git/config`, content: 'x' }))[1], /\.git/);
assert.match((await call('read_file', { path: '/etc/hosts' }))[1], /outside/);
assert.equal((await call('codex_status', { job_id: 'nope' }))[0], true);

// A client response budget must not become an execution timeout. Long tools
// return their job id, continue running, and leave the health endpoint responsive.
const stdinStartup = JSON.parse((await call('codex_run', { cwd: `${root}/proj`, prompt: 'stdin EOF startup regression', wait_sec: 5 }))[1]);
assert.equal(stdinStartup.status, 'done');
assert.ok(stdinStartup.thread_id);
assert.equal(stdinStartup.final_message, 'fake complete');
const jobJournal=JSON.parse(fs.readFileSync(path.join(process.env.CODEX_BRIDGE_STATE,'jobs',stdinStartup.job_id+'.json')));
assert.equal(jobJournal.final_message,'fake complete');
assert.equal(jobJournal.finished,true);
assert.equal(JSON.parse((await call('codex_jobs',{}))[1]).find(j=>j.job_id===stdinStartup.job_id).status,'done');
assert.equal(JSON.parse((await call('codex_recovery_status',{}))[1]).status,'not_configured');

process.env.CODEX_BRIDGE_MAX_WAIT_SEC = '0.3';
const commandStarted = Date.now();
const longCommandPromise = call('run_command', { cwd: `${root}/proj`, command: 'node', args: ['-e', "setTimeout(()=>console.log('long-result'),1500)"] });
await new Promise(resolve => setTimeout(resolve, 100));
assert.equal((await fetch(url.replace('/mcp', '/health'), { signal: AbortSignal.timeout(1000) })).status, 200);
const longCommand = JSON.parse((await longCommandPromise)[1]);
assert.equal(longCommand.kind, 'command');
assert.equal(longCommand.status, 'running');
assert.ok(Date.now() - commandStarted < 1400);
let commandFinished;
for (let n = 0; n < 10; n++) {
  commandFinished = JSON.parse((await call('codex_status', { job_id: longCommand.job_id, wait_sec: 540 }))[1]);
  if (commandFinished.status !== 'running') break;
}
assert.equal(commandFinished.status, 'done');
assert.match(commandFinished.final_message, /long-result/);

const legacyStarted = Date.now();
const legacyLong = JSON.parse((await call('codex_run', { cwd: `${root}/proj`, prompt: '__SLOW__ response budget', wait_sec: 540 }))[1]);
assert.equal(legacyLong.status, 'running');
assert.equal((await fetch(url.replace('/mcp','/recovery/prepare'),{method:'POST',headers:recoveryHeaders})).status,409);
assert.ok(Date.now() - legacyStarted < 1400);
await call('codex_cancel', { job_id: legacyLong.job_id });

const boundedLoop = JSON.parse((await call('codex_loop_start', { cwd: `${root}/proj`, goal: '__SLOW__ bounded loop response', verify_commands: [{command:'node',args:['-e','process.exit(0)']}], terminate_grace_sec:0.2, wait_sec:540 }))[1]);
assert.ok(boundedLoop.loop_id);
assert.equal(boundedLoop.status, 'running');
await call('codex_loop_cancel', { loop_id: boundedLoop.loop_id });

// A genuine command execution deadline kills its process group, including a
// child that ignores SIGTERM, and remains distinguishable from a short MCP wait.
const childCode = "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)";
const parentCode = `require('child_process').spawn(process.execPath,['-e',${JSON.stringify(childCode)}],{stdio:'inherit'});setInterval(()=>{},1000)`;
const timeoutCommand = JSON.parse((await call('run_command', { cwd: `${root}/proj`, command:'node',args:['-e',parentCode],timeout_sec:1 }))[1]);
assert.equal(timeoutCommand.status, 'running');
let timeoutResult;
for (let n=0;n<12;n++) {
  timeoutResult=JSON.parse((await call('codex_status',{job_id:timeoutCommand.job_id,wait_sec:540}))[1]);
  if(timeoutResult.status!=='running')break;
}
assert.equal(timeoutResult.status,'failed');
assert.match(timeoutResult.error,/command timed out after 1 seconds/);
delete process.env.CODEX_BRIDGE_MAX_WAIT_SEC;

// persistent Loop Engineering: success is external-verifier driven.
const loopOkCall = await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: 'make the loop pass',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  max_attempts: 3,
  wait_sec: 5,
});
assert.equal(loopOkCall[0], false);
const loopOk = JSON.parse(loopOkCall[1]);
assert.equal(loopOk.status, 'completed');
assert.equal(loopOk.attempts, 1);
const loopsDir = path.join(process.env.CODEX_BRIDGE_STATE, 'loops');
assert.ok(fs.existsSync(path.join(loopsDir, `${loopOk.loop_id}.json`)), 'loop state was not persisted');

// verifier fails once; supervisor must resume the same Codex thread and pass on attempt 2.
const verifyOnce = [
  "const fs=require('fs')",
  "const p='verify-count.txt'",
  "let n=fs.existsSync(p)?Number(fs.readFileSync(p,'utf8')):0",
  "n++",
  "fs.writeFileSync(p,String(n))",
  "process.exit(n===1?1:0)",
].join(';');
const loopRetryCall = await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: 'repair after failed verification',
  verify_commands: [{ command: 'node', args: ['-e', verifyOnce] }],
  max_attempts: 3,
  wait_sec: 5,
});
assert.equal(loopRetryCall[0], false);
const loopRetry = JSON.parse(loopRetryCall[1]);
assert.equal(loopRetry.status, 'completed');
assert.equal(loopRetry.attempts, 2);
assert.ok(loopRetry.thread_id);
const fakeInvocations = fs.readFileSync(path.join(root, 'proj', 'fake-codex.log'), 'utf8').trim().split('\n').map(JSON.parse);
assert.ok(fakeInvocations.some((args) => args.includes('resume') && args.includes(loopRetry.thread_id)), 'second attempt did not resume the same thread');

// regular activity must refresh heartbeat/activity and prevent a false hang.
const activityStart = JSON.parse((await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: '__ACTIVITY_LONG__ stay alive while emitting progress',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  max_attempts: 2,
  watchdog_interval_sec: 0.2,
  hang_timeout_sec: 0.5,
  terminate_grace_sec: 0.2,
  wait_sec: 0,
}))[1]);
const activityDone = JSON.parse((await call('codex_loop_status', { loop_id: activityStart.loop_id, wait_sec: 5 }))[1]);
assert.equal(activityDone.status, 'completed');
assert.equal(activityDone.attempts, 1);
assert.ok(Date.parse(activityDone.last_heartbeat_at) >= Date.parse(activityDone.created_at));
assert.ok(Date.parse(activityDone.last_activity_at) > Date.parse(activityDone.created_at));
assert.equal(activityDone.history.some((h) => h.type === 'watchdog_hung'), false);

// a silent hung Codex that ignores SIGTERM must be force-killed, then resumed on the same thread.
const hangCall = await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: '__HANG_ONCE__ recover automatically after watchdog kill',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  max_attempts: 3,
  watchdog_interval_sec: 0.2,
  hang_timeout_sec: 0.5,
  terminate_grace_sec: 0.2,
  wait_sec: 5,
});
assert.equal(hangCall[0], false);
const hangRecovered = JSON.parse(hangCall[1]);
assert.equal(hangRecovered.status, 'completed');
assert.equal(hangRecovered.attempts, 2);
assert.ok(hangRecovered.thread_id);
assert.ok(hangRecovered.history.some((h) => h.type === 'watchdog_hung'));
assert.ok(hangRecovered.history.some((h) => h.type === 'watchdog_kill'), 'hung child did not exercise SIGKILL fallback');
const hangInvocations = fs.readFileSync(path.join(root, 'proj', 'fake-codex.log'), 'utf8').trim().split('\n').map(JSON.parse)
  .filter((args) => String(args.at(-1)).includes('__HANG_ONCE__'));
assert.equal(hangInvocations.length, 2);
assert.ok(hangInvocations[1].includes('resume'));
assert.ok(hangInvocations[1].includes(hangRecovered.thread_id));

// duplicate resume requests may spawn contenders, but flock ownership must allow only one Codex child.
const beforeDup = fs.readFileSync(path.join(root, 'proj', 'fake-codex.log'), 'utf8').trim().split('\n').filter(Boolean).length;
const dupStart = JSON.parse((await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: '__ACTIVITY_LONG__ duplicate ownership check',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  max_attempts: 2,
  watchdog_interval_sec: 0.2,
  hang_timeout_sec: 1,
  wait_sec: 0,
}))[1]);
await call('codex_loop_resume', { loop_id: dupStart.loop_id, wait_sec: 0 });
const dupDone = JSON.parse((await call('codex_loop_status', { loop_id: dupStart.loop_id, wait_sec: 5 }))[1]);
assert.equal(dupDone.status, 'completed');
const afterDupLines = fs.readFileSync(path.join(root, 'proj', 'fake-codex.log'), 'utf8').trim().split('\n').filter(Boolean);
const dupInvocations = afterDupLines.slice(beforeDup).map(JSON.parse)
  .filter((args) => String(args.at(-1)).includes('__ACTIVITY_LONG__ duplicate ownership check'));
assert.equal(dupInvocations.length, 1, 'flock allowed duplicate Codex workers');

// max attempts must block rather than silently succeed or loop forever.
const loopBlockedCall = await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: 'this verifier always fails',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(1)'] }],
  max_attempts: 2,
  wait_sec: 5,
});
assert.equal(loopBlockedCall[0], false);
const loopBlocked = JSON.parse(loopBlockedCall[1]);
assert.equal(loopBlocked.status, 'blocked');
assert.equal(loopBlocked.attempts, 2);
assert.equal(loopBlocked.last_error.category, 'verification');

// repeated identical failures must escalate repair strategy instead of blindly replaying the same approach.
const strategyCall = await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: 'strategy fingerprint escalation',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(1)'] }],
  max_attempts: 5,
  wait_sec: 5,
});
assert.equal(strategyCall[0], false);
const strategyLoop = JSON.parse(strategyCall[1]);
assert.equal(strategyLoop.status, 'blocked');
assert.equal(strategyLoop.attempts, 5);
assert.equal(strategyLoop.strategy_level, 3);
assert.equal(strategyLoop.strategy_repeat_count, 5);
assert.equal(strategyLoop.failure_fingerprints[strategyLoop.last_error.fingerprint].count, 5);
assert.deepEqual(
  strategyLoop.history.filter((h) => h.type === 'strategy_escalated').map((h) => h.level),
  [1, 2, 3],
);
const strategyInvocations = fs.readFileSync(path.join(root, 'proj', 'fake-codex.log'), 'utf8').trim().split('\n').map(JSON.parse)
  .filter((args) => String(args.at(-1)).includes('strategy fingerprint escalation'));
assert.equal(strategyInvocations.length, 5);
assert.match(String(strategyInvocations.at(-1).at(-1)), /Architecture escalation/);

// simulate a bridge restart: persisted active state with no in-memory runner becomes recoverable interruption.
const interruptedId = 'persisted-restart-case';
fs.writeFileSync(path.join(loopsDir, `${interruptedId}.json`), JSON.stringify({
  schema_version: 1,
  loop_id: interruptedId,
  cwd: path.join(root, 'proj'),
  goal: 'survive restart',
  status: 'running',
  attempts: 1,
  max_attempts: 5,
  thread_id: '01a0cafe-0000-7000-8000-000000000001',
  active_job_id: 'lost-job',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  created_at: new Date(Date.now() - 10_000).toISOString(),
  updated_at: new Date(Date.now() - 10_000).toISOString(),
  last_error: null,
  activity: [],
  history: [],
}, null, 2));
const interrupted = JSON.parse((await call('codex_loop_status', { loop_id: interruptedId }))[1]);
assert.equal(interrupted.status, 'interrupted');
assert.equal(interrupted.recoverable, true);
assert.equal(interrupted.active_job_id, null);
const resumedAfterRestart = JSON.parse((await call('codex_loop_resume', { loop_id: interruptedId, wait_sec: 5 }))[1]);
assert.equal(resumedAfterRestart.status, 'completed');
assert.equal(resumedAfterRestart.attempts, 2);
assert.equal(resumedAfterRestart.thread_id, '01a0cafe-0000-7000-8000-000000000001');

// cancellation is persisted even while the fake Codex process is still alive.
const slowStart = JSON.parse((await call('codex_loop_start', {
  cwd: `${root}/proj`,
  goal: '__SLOW__ keep running until cancelled',
  verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  max_attempts: 2,
  wait_sec: 0,
}))[1]);
const cancelled = JSON.parse((await call('codex_loop_cancel', { loop_id: slowStart.loop_id }))[1]);
assert.equal(cancelled.status, 'cancelled');
assert.equal(JSON.parse(fs.readFileSync(path.join(loopsDir, `${slowStart.loop_id}.json`), 'utf8')).status, 'cancelled');
const cancelledAttempts = cancelled.attempts;
await new Promise((r) => setTimeout(r, 700));
const cancelledLater = JSON.parse((await call('codex_loop_status', { loop_id: slowStart.loop_id }))[1]);
assert.equal(cancelledLater.status, 'cancelled');
assert.equal(cancelledLater.attempts, cancelledAttempts, 'watchdog restarted a cancelled loop');


// Phase 4 task graph: independent tasks run in parallel worktrees, dependencies wait for integration,
// and the verified integration diff is applied back to the clean base without committing there.
const graphProj = path.join(root, 'graphproj');
fs.mkdirSync(graphProj, { recursive: true });
fs.writeFileSync(path.join(graphProj, 'shared.txt'), 'base\n');
const gitOk = async (args) => {
  const result = await call('run_command', { cwd: graphProj, command: 'git', args });
  assert.equal(result[0], false, result[1]);
  assert.match(result[1], /^exit 0/m);
};
await gitOk(['init', '-b', 'main']);
await gitOk(['add', '-A']);
await gitOk(['-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'base']);

process.env.CODEX_BRIDGE_MAX_WAIT_SEC = '0.3';
const boundedGraph = JSON.parse((await call('codex_graph_start', {
  cwd:graphProj,tasks:[{task_id:'bounded',goal:'__SLOW__ graph response budget',write_paths:[],verify_commands:[{command:'node',args:['-e','process.exit(0)']}]}],
  final_verify_commands:[{command:'node',args:['-e','process.exit(0)']}],terminate_grace_sec:0.2,wait_sec:540,
}))[1]);
assert.ok(boundedGraph.graph_id);
assert.ok(['starting','running'].includes(boundedGraph.status));
await call('codex_graph_cancel',{graph_id:boundedGraph.graph_id});
delete process.env.CODEX_BRIDGE_MAX_WAIT_SEC;

const finalGraphCheck = [
  "const fs=require('fs')",
  "const read=(p)=>fs.readFileSync(p,'utf8').trim()",
  "if(read('a.txt')!=='A'||read('b.txt')!=='B'||read('c.txt')!=='C')process.exit(1)",
].join(';');
const graphCall = await call('codex_graph_start', {
  cwd: graphProj,
  goal: 'parallel graph integration test',
  max_parallel: 2,
  watchdog_interval_sec: 0.2,
  hang_timeout_sec: 3,
  terminate_grace_sec: 0.2,
  tasks: [
    {
      task_id: 'a',
      write_paths: ['a.txt'],
      goal: '__ACTIVITY_LONG__ __WRITE_FILE__:a.txt:A',
      verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('a.txt','utf8').trim()==='A'?0:1)"] }],
    },
    {
      task_id: 'b',
      write_paths: ['b.txt'],
      goal: '__ACTIVITY_LONG__ __WRITE_FILE__:b.txt:B',
      verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('b.txt','utf8').trim()==='B'?0:1)"] }],
    },
    {
      task_id: 'c',
      write_paths: ['c.txt'],
      depends_on: ['a', 'b'],
      goal: '__WRITE_FILE__:c.txt:C',
      verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('c.txt','utf8').trim()==='C'?0:1)"] }],
    },
  ],
  final_verify_commands: [{ command: 'node', args: ['-e', finalGraphCheck] }],
  wait_sec: 15,
});
assert.equal(graphCall[0], false, graphCall[1]);
const graphDone = JSON.parse(graphCall[1]);
assert.equal(graphDone.status, 'completed');
assert.equal(graphDone.peak_parallel, 2);
const execution = fs.readFileSync(process.env.FAKE_EXECUTION_LOG, 'utf8').trim().split('\n').map(JSON.parse);
const interval = (file) => execution.filter(e => e.prompt.includes(`__WRITE_FILE__:${file}:`));
const ae = interval('a.txt'), be = interval('b.txt');
assert.ok(ae[0].at < be[1].at && be[0].at < ae[1].at, 'fake child processes must actually overlap');
assert.notEqual(ae[0].cwd, be[0].cwd);
assert.deepEqual(graphDone.tasks.map((t) => t.status), ['integrated', 'integrated', 'integrated']);
assert.equal(fs.readFileSync(path.join(graphProj, 'a.txt'), 'utf8').trim(), 'A');
assert.equal(fs.readFileSync(path.join(graphProj, 'b.txt'), 'utf8').trim(), 'B');
assert.equal(fs.readFileSync(path.join(graphProj, 'c.txt'), 'utf8').trim(), 'C');
assert.ok(graphDone.history.find((h) => h.type === 'task_integrated' && h.task_id === 'a'));
assert.ok(graphDone.history.find((h) => h.type === 'task_integrated' && h.task_id === 'b'));
assert.deepEqual(graphDone.history.filter((h) => h.type === 'task_integrated').map((h) => h.task_id), ['a', 'b', 'c']);
assert.ok(graphDone.history.find((h) => h.type === 'task_started' && h.task_id === 'c'));
const cStart = graphDone.history.find((h) => h.type === 'task_started' && h.task_id === 'c');
const aIntegrated = graphDone.history.find((h) => h.type === 'task_integrated' && h.task_id === 'a');
const bIntegrated = graphDone.history.find((h) => h.type === 'task_integrated' && h.task_id === 'b');
assert.ok(Date.parse(cStart.at) >= Date.parse(aIntegrated.at));
assert.ok(Date.parse(cStart.at) >= Date.parse(bIntegrated.at));

for (const t of graphDone.tasks) {
  assert.ok(t.commit, 'task must create a temporary commit');
  assert.equal(JSON.parse(fs.readFileSync(path.join(loopsDir, `${t.loop_id}.json`))).last_verification.at(-1).exit_code, 0);
  assert.notEqual(t.worktree, graphProj);
}
assert.equal(new Set(graphDone.tasks.map(t => t.worktree)).size, 3);
const graphHeadText = (await call('run_command', { cwd: graphProj, command: 'git', args: ['rev-parse', 'HEAD'] }))[1];
const graphHeadMatch = /--- stdout\n([^\n]+)/.exec(graphHeadText);
assert.ok(graphHeadMatch, graphHeadText);
assert.equal(graphDone.base_head, graphHeadMatch[1].trim());
assert.equal(graphDone.final_verification.at(-1).exit_code, 0);

// Commit the applied graph patch so the base is clean for a deliberate parallel merge-conflict test.
await gitOk(['add', '-A']);
await gitOk(['-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'graph result']);

// Missing graph-level verification must stop at verification_required, then resume once a verifier is supplied.
const needsVerifyCall = await call('codex_graph_start', {
  cwd: graphProj,
  goal: 'attach final verifier after task completion',
  max_parallel: 1,
  tasks: [{
    task_id: 'd',
    write_paths: ['d.txt'],
    goal: '__WRITE_FILE__:d.txt:D',
    verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('d.txt','utf8').trim()==='D'?0:1)"] }],
  }],
  final_verify_commands: [],
  wait_sec: 10,
});
assert.equal(needsVerifyCall[0], false, needsVerifyCall[1]);
const needsVerify = JSON.parse(needsVerifyCall[1]);
assert.equal(needsVerify.status, 'verification_required');
assert.equal(fs.existsSync(path.join(graphProj, 'd.txt')), false, 'unverified graph must not modify base');
const resumedVerifiedCall = await call('codex_graph_resume', {
  graph_id: needsVerify.graph_id,
  final_verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('d.txt','utf8').trim()==='D'?0:1)"] }],
  wait_sec: 10,
});
assert.equal(resumedVerifiedCall[0], false, resumedVerifiedCall[1]);
const resumedVerified = JSON.parse(resumedVerifiedCall[1]);
assert.equal(resumedVerified.status, 'completed');
assert.equal(fs.readFileSync(path.join(graphProj, 'd.txt'), 'utf8').trim(), 'D');

await gitOk(['add', '-A']);
await gitOk(['-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'verified graph result']);

// Explicit write scope must discard unrelated agent/plugin artifacts before integration.
const scopedCall = await call('codex_graph_start', {
  cwd: graphProj,
  goal: 'discard out-of-scope agent changes',
  max_parallel: 1,
  tasks: [{
    task_id: 'scoped',
    write_paths: ['scoped.txt'],
    goal: '__WRITE_FILE__:scoped.txt:OK __EXTRA_FILE__:accidental.txt:JUNK',
    verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('scoped.txt','utf8').trim()==='OK'?0:1)"] }],
  }],
  final_verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('scoped.txt','utf8').trim()==='OK'&&!fs.existsSync('accidental.txt')?0:1)"] }],
  wait_sec: 10,
});
assert.equal(scopedCall[0], false, scopedCall[1]);
const scopedGraph = JSON.parse(scopedCall[1]);
assert.equal(scopedGraph.status, 'completed');
assert.equal(fs.readFileSync(path.join(graphProj, 'scoped.txt'), 'utf8').trim(), 'OK');
assert.equal(fs.existsSync(path.join(graphProj, 'accidental.txt')), false);
const scopeEvent = scopedGraph.history.find((h) => h.type === 'out_of_scope_discarded');
assert.ok(scopeEvent);
assert.deepEqual(scopeEvent.paths, ['accidental.txt']);

await gitOk(['add', '-A']);
await gitOk(['-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'scoped graph result']);

const conflictCall = await call('codex_graph_start', {
  cwd: graphProj,
  goal: 'parallel conflict detection',
  max_parallel: 2,
  watchdog_interval_sec: 0.2,
  hang_timeout_sec: 3,
  terminate_grace_sec: 0.2,
  tasks: [
    {
      task_id: 'left',
      write_paths: ['shared.txt'],
      goal: '__ACTIVITY_LONG__ __WRITE_FILE__:shared.txt:LEFT',
      verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('shared.txt','utf8').trim()==='LEFT'?0:1)"] }],
    },
    {
      task_id: 'right',
      write_paths: ['shared.txt'],
      goal: '__ACTIVITY_LONG__ __WRITE_FILE__:shared.txt:RIGHT',
      verify_commands: [{ command: 'node', args: ['-e', "const fs=require('fs');process.exit(fs.readFileSync('shared.txt','utf8').trim()==='RIGHT'?0:1)"] }],
    },
  ],
  final_verify_commands: [{ command: 'node', args: ['-e', 'process.exit(0)'] }],
  wait_sec: 15,
});
assert.equal(conflictCall[0], false, conflictCall[1]);
const conflictGraph = JSON.parse(conflictCall[1]);
assert.equal(conflictGraph.status, 'blocked');
assert.equal(conflictGraph.blocked_reason, 'merge_conflict');
assert.equal(conflictGraph.peak_parallel, 2);
assert.equal(JSON.parse((await call('codex_graph_resume', { graph_id: conflictGraph.graph_id }))[1]).status, 'blocked');

assert.equal(fs.readFileSync(path.join(graphProj, 'shared.txt'), 'utf8').trim(), 'base', 'conflicted graph must not alter base');

// MCP resume accepts an explicit staged manual resolution and reruns verification.
fs.writeFileSync(path.join(conflictGraph.integration_worktree, 'shared.txt'), 'RIGHT\n');
const stageResolution = spawn('git', ['-C', conflictGraph.integration_worktree, 'add', '--', 'shared.txt'], { stdio: 'ignore' });
assert.equal(await new Promise(resolve => stageResolution.on('exit', resolve)), 0);
const resolvedConflict = JSON.parse((await call('codex_graph_resume', { graph_id: conflictGraph.graph_id, resolve_conflict: true, wait_sec: 10 }))[1]);
assert.equal(resolvedConflict.status, 'completed');
assert.equal(fs.readFileSync(path.join(graphProj, 'shared.txt'), 'utf8'), 'RIGHT\n');
assert.equal(resolvedConflict.tasks.find(t => t.task_id === 'right').resolution_verification.at(-1).exit_code, 0);

const graphRegression = spawn('python3', [fileURLToPath(new URL('./graph_test.py', import.meta.url))], { stdio: ['ignore', 'pipe', 'pipe'] });
let graphOutput = '';
graphRegression.stdout.on('data', data => { graphOutput += data; });
graphRegression.stderr.on('data', data => { graphOutput += data; });
const graphExit = await new Promise((resolve, reject) => {
  const timer = setTimeout(() => { graphRegression.kill(); reject(new Error('graph tests timed out')); }, 90000);
  graphRegression.on('error', reject);
  graphRegression.on('exit', code => { clearTimeout(timer); resolve(code); });
});
assert.equal(graphExit, 0, graphOutput);
console.log(graphOutput.trim());

const sessions = JSON.parse((await call('codex_sessions', { limit: 2 }))[1]);
assert.deepEqual(sessions.map((s) => s.thread_id), [TID, LEGACY]); assert.equal(sessions[0].title, 'Build fix');
const lg = JSON.parse((await call('codex_session_read', { thread_id: LEGACY }))[1]);
assert.deepEqual(lg.items, [{ kind: 'user', text: 'check changes' }, { kind: 'command', text: 'git status -sb', exit: 0 }, { kind: 'agent', text: 'clean tree' }]);
const tr = JSON.parse((await call('codex_session_read', { thread_id: TID, output_chars: 200 }))[1]);
assert.deepEqual(tr.items.map((i) => i.kind), ['user', 'command', 'file_change', 'agent', 'error']);
assert.match(tr.items[1].output, /\[REDACTED\]/); assert.doesNotMatch(tr.items[1].output, /sk-abc/);
// an empty, newer rollout must not be picked as the handoff target
fs.writeFileSync(path.join(codexHome, 'sessions', '2026', '09', '22', 'rollout-2026-09-22T23-35-00-01a0c9ff-0000-7000-8000-000000000000.jsonl'),
  JSON.stringify({ type: 'session_meta', payload: { id: '01a0c9ff-0000-7000-8000-000000000000', cwd: '/tmp/elsewhere', timestamp: '2026-09-22T14:35:00Z' } }) + '\n');
const h = JSON.parse((await call('codex_handoff', {}))[1]);
assert.equal(h.thread_id, TID); assert.equal(h.goal, 'fix the build'); assert.equal(h.latest_agent_message, 'patched a.txt');
assert.equal(h.stopped_with_error, 'usage limit'); assert.equal(h.files_changed_by_codex.length, 1); assert.equal(h.git.git, false);
assert.match((await call('git_diff', { cwd: `${root}/proj` }))[1], /error/); // not a git repo

// npx starts the bin through a symlink; the server must still listen
const link = path.join(tmp, 'codex-bridge'); fs.symlinkSync(fileURLToPath(new URL('./server.mjs', import.meta.url)), link);
let childUrl;
const child = spawn(process.execPath, [link], { stdio: ['ignore', 'ignore', 'pipe'] });
const started = await new Promise((r) => { let err = ''; child.stderr.on('data', (d) => { err += d; if (err.includes('listening')) {childUrl=err.match(/http:\/\/127\.0\.0\.1:\d+\/mcp/)[0];r(true);} }); child.on('exit', () => r(false)); setTimeout(() => r(false), 5000); });
assert.ok(started, 'server did not start through a bin symlink');
const restartedClient=new Client({name:'journal-recovery',version:'1'});
await restartedClient.connect(new StreamableHTTPClientTransport(new URL(childUrl),{requestInit:{headers:{Authorization:`Bearer ${token}`}}}));
const recoveredJob=await restartedClient.callTool({name:'codex_status',arguments:{job_id:stdinStartup.job_id,wait_sec:0}});
assert.equal(JSON.parse(recoveredJob.content[0].text).final_message,'fake complete');
fs.writeFileSync(path.join(process.env.CODEX_BRIDGE_STATE,'jobs','aaaabbbb.json'),JSON.stringify({job_id:'aaaabbbb',status:'running',thread_id:TID,owner_pid:1,started:Date.now()}));
const interruptedJob=await restartedClient.callTool({name:'codex_status',arguments:{job_id:'aaaabbbb',wait_sec:0}});
assert.equal(JSON.parse(interruptedJob.content[0].text).status,'interrupted');
await restartedClient.close();
child.kill();

await client.close(); srv.close(); fs.rmSync(tmp, { recursive: true, force: true });
console.log('ok');
