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
const { createHttpServer, guard } = await import('./server.mjs');

const srv = createHttpServer().listen(0, '127.0.0.1');
await new Promise((r) => srv.once('listening', r));
const url = `http://127.0.0.1:${srv.address().port}/mcp`;
const token = fs.readFileSync(path.join(tmp, 'state', 'token'), 'utf8');

// auth
assert.equal((await fetch(url, { method: 'POST', body: '{}' })).status, 401);
assert.equal((await fetch(url.replace('/mcp', '/health'))).status, 200);

// guard
assert.throws(() => guard('/etc/passwd'), /outside/);
assert.throws(() => guard(path.join(root, 'proj', '.env')), /protected/);
assert.throws(() => guard(path.join(root, 'proj', '..', '..', 'x')), /outside/);
assert.ok(guard(path.join(root, 'proj', 'new', 'file.ts')));

const client = new Client({ name: 't', version: '0' });
await client.connect(new StreamableHTTPClientTransport(new URL(url), { requestInit: { headers: { Authorization: `Bearer ${token}` } } }));
const names = (await client.listTools()).tools.map((t) => t.name).sort();
assert.deepEqual(names, ['codex_cancel', 'codex_handoff', 'codex_resume', 'codex_run', 'codex_session_read', 'codex_sessions', 'codex_status', 'edit_file', 'git_diff', 'list_dir', 'list_projects', 'read_file', 'run_command', 'search', 'write_file']);
assert.match(client.getInstructions(), /\/goal <objective>[\s\S]*codex_resume/);
const call = async (name, args) => { const r = await client.callTool({ name, arguments: args }); return [r.isError === true, r.content[0].text]; };

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
const child = spawn(process.execPath, [link], { stdio: ['ignore', 'ignore', 'pipe'] });
const started = await new Promise((r) => { let err = ''; child.stderr.on('data', (d) => { err += d; if (err.includes('listening')) r(true); }); child.on('exit', () => r(false)); setTimeout(() => r(false), 5000); });
child.kill(); assert.ok(started, 'server did not start through a bin symlink');

await client.close(); srv.close(); fs.rmSync(tmp, { recursive: true, force: true });
console.log('ok');
