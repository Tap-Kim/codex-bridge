#!/usr/bin/env node
// codex-bridge — MCP server so web ChatGPT can code on this machine.
//
//   ChatGPT (connector, auth none) -> OpenAI Secure MCP Tunnel
//     -> tunnel-client (this machine, adds Authorization: Bearer) -> http://127.0.0.1:7421/mcp -> here
//
// Two kinds of tools: direct file/command access (ChatGPT does the coding on
// its own quota), and codex_* which delegates a whole task to `codex exec` and
// lets ChatGPT resume existing Codex threads. Stateless HTTP like GPT-Bridge:
// a fresh McpServer per request, nothing to leak. Codex jobs live in module
// memory so a slow run can be polled across requests.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';

const HOME = os.homedir();
const STATE = process.env.CODEX_BRIDGE_STATE || path.join(HOME, '.codex-bridge');
const HOST = process.env.DAIMON_SERVER_MCP_HOST || '127.0.0.1';
const PORT = Number(process.env.CODEX_BRIDGE_PORT || process.env.DAIMON_SERVER_MCP_PORT || 7421);
// Folders ChatGPT may touch (colon-separated). Default: the directory the bridge was started in.
const ROOTS = (process.env.CODEX_BRIDGE_ROOTS || process.cwd())
  .split(':').filter(Boolean).map((p) => p.replace(/\/+$/, ''));
const CODEX = process.env.CODEX_BIN || ['/opt/homebrew/bin/codex', '/usr/local/bin/codex'].find((p) => fs.existsSync(p)) || 'codex';
const CODEX_HOME = process.env.CODEX_HOME || path.join(HOME, '.codex');
const PYTHON = process.env.PYTHON_BIN || ['/usr/bin/python3', '/opt/homebrew/bin/python3', '/usr/local/bin/python3'].find((p) => fs.existsSync(p)) || 'python3';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOOP_SUPERVISOR = path.join(HERE, 'loop_supervisor.py');
const GRAPH_SUPERVISOR = path.join(HERE, 'graph_supervisor.py');
// Executables ChatGPT may run directly. Anything else must go through codex_run (sandboxed).
const ALLOW_CMDS = new Set((process.env.CODEX_BRIDGE_ALLOW_CMDS ||
  'git,npm,npx,pnpm,yarn,node,bun,python3,pytest,go,cargo,make,ls,cat,grep,tsc,eslint,vitest,jest,prettier,gh').split(','));
const DENY_FILE = /(^|\/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|auth\.json|\.npmrc|\.netrc)$/;
const MAX_TEXT = 200_000;
const ACCESS_LOG = process.env.DAIMON_SERVER_ACCESS_LOG || path.join(STATE, 'access.log');

fs.mkdirSync(STATE, { recursive: true, mode: 0o700 });
const TOKEN_FILE = path.join(STATE, 'token');
if (!fs.existsSync(TOKEN_FILE)) fs.writeFileSync(TOKEN_FILE, crypto.randomBytes(32).toString('hex'), { mode: 0o600 });
const TOKEN = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
// ready-made header value for tunnel-client's `mcp.extra_headers.Authorization: file:...`
fs.writeFileSync(path.join(STATE, 'auth_header'), `Bearer ${TOKEN}`, { mode: 0o600 });

const log = (line) => fs.appendFile(ACCESS_LOG, `${new Date().toISOString()} ${line}\n`, () => {});

// Keep the HTTP event loop free while a Python controller reads state or cancels
// a worker. Long MCP waits return an id for polling instead of holding a tunnel.
function supervisorCall(file, command, id, payload = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PYTHON, [file, command, ...(id ? [id] : [])], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, CODEX_BRIDGE_STATE: STATE, CODEX_BIN: CODEX },
    });
    let stdout = '', stderr = '';
    proc.stdout.on('data', chunk => { stdout += chunk; if (stdout.length > 8e6) { proc.kill(); reject(new Error('supervisor output limit exceeded')); } });
    proc.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    proc.on('error', reject);
    proc.stdin.on('error', error => { if (error.code !== 'EPIPE') reject(error); });
    proc.on('close', code => {
      try {
        const parsed = JSON.parse(stdout.trim().split('\n').at(-1));
        if (code !== 0 || parsed?.error) throw new Error(parsed?.error || stderr.trim() || `supervisor exited ${code}`);
        resolve(parsed);
      } catch (error) { reject(error); }
    });
    proc.stdin.end(JSON.stringify(payload));
  });
}
const loopSupervisorCall = (command, id, payload) => supervisorCall(LOOP_SUPERVISOR, command, id, payload);
const graphSupervisorCall = (command, id, payload) => supervisorCall(GRAPH_SUPERVISOR, command, id, payload);
function waitBudget(sec) {
  const configured = Number(process.env.CODEX_BRIDGE_MAX_WAIT_SEC || 20);
  const limit = Number.isFinite(configured) ? Math.min(20, Math.max(.1, configured)) : 20;
  return Math.min(Math.max(sec || 0, 0), limit);
}
async function waitSupervisorLoop(loopId, sec) {
  const until = Date.now() + waitBudget(sec) * 1000;
  let state = await loopSupervisorCall('status', loopId);
  while (!['completed', 'blocked', 'failed', 'cancelled', 'verification_required', 'interrupted'].includes(state.status) && Date.now() < until) {
    await sleep(200);
    state = await loopSupervisorCall('status', loopId);
  }
  if (!['completed', 'blocked', 'failed', 'cancelled', 'verification_required', 'interrupted'].includes(state.status)) {
    state.hint = `still running — call codex_loop_status with loop_id ${loopId}`;
  }
  return state;
}

async function waitSupervisorGraph(graphId, sec) {
  const terminal = ['completed', 'blocked', 'failed', 'cancelled', 'verification_required', 'integration_ready', 'interrupted'];
  const until = Date.now() + waitBudget(sec) * 1000;
  let state = await graphSupervisorCall('status', graphId);
  while (!terminal.includes(state.status) && Date.now() < until) {
    await sleep(250);
    state = await graphSupervisorCall('status', graphId);
  }
  if (!terminal.includes(state.status)) state.hint = `still running — call codex_graph_status with graph_id ${graphId}`;
  return state;
}

// ---- path guard -----------------------------------------------------------
function realish(p) {
  // realpath of the deepest existing ancestor + the rest, so new files resolve too
  let cur = p; const rest = [];
  while (!fs.existsSync(cur)) { rest.unshift(path.basename(cur)); cur = path.dirname(cur); }
  return path.join(fs.realpathSync(cur), ...rest);
}
export function guard(p, { write = false } = {}) {
  if (typeof p !== 'string' || !p.startsWith('/')) throw new Error('path must be absolute');
  const real = realish(path.resolve(p));
  const roots = ROOTS.filter((r) => fs.existsSync(r)).map((r) => fs.realpathSync(r));
  if (!roots.some((r) => real === r || real.startsWith(r + '/'))) throw new Error(`outside allowed roots: ${p}`);
  if (DENY_FILE.test(real)) throw new Error(`protected file: ${p}`);
  if (write && /(^|\/)\.git\//.test(real)) throw new Error('refusing to write inside .git');
  return real;
}
const clip = (s, n = MAX_TEXT) => (s.length > n ? s.slice(0, n) + `\n…[truncated ${s.length - n} chars]` : s);
const text = (s) => ({ content: [{ type: 'text', text: typeof s === 'string' ? s : JSON.stringify(s, null, 2) }] });

const ORCA_DAEMON_SIGNATURES = [
  '/Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper',
  '/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js',
];
export function parseOrcaDaemonProcesses(output) {
  return String(output || '').split('\n').flatMap((line) => {
    const m = /^\s*(\d+)\s+(\S+)\s+(.*)$/.exec(line);
    if (!m || !ORCA_DAEMON_SIGNATURES.every((s) => m[3].includes(s))) return [];
    return [{ pid: Number(m[1]), elapsed: m[2], command: m[3] }];
  });
}
function orcaDaemonProcesses() {
  const r = spawnSync('/bin/ps', ['-axo', 'pid=,etime=,command='], { encoding: 'utf8', maxBuffer: 4e6 });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(r.stderr?.trim() || 'ps failed');
  return parseOrcaDaemonProcesses(r.stdout);
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function pidAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (e) { if (e.code === 'ESRCH') return false; throw e; }
}

// ---- codex jobs -------------------------------------------------------------
const jobs = new Map(); // job_id -> { proc, thread_id, status, final, items[], cwd, started }
const JOBS_DIR = path.join(STATE, 'jobs');
fs.mkdirSync(JOBS_DIR, { recursive: true, mode: 0o700 });
const atomicJson = (file, value) => {
  const tmp = file + '.' + crypto.randomUUID() + '.tmp';
  try { fs.writeFileSync(tmp, JSON.stringify(value), { mode: 0o600 }); fs.renameSync(tmp, file); }
  finally { if (fs.existsSync(tmp)) fs.unlinkSync(tmp); }
};
atomicJson(path.join(STATE, 'jobs-runtime.json'), { version: 1, owner_pid: process.pid });
function jobSnapshot(job) {
  return { job_id: job.id, kind: job.kind || 'codex', status: job.status, thread_id: job.thread_id,
    cwd: job.cwd, started: job.started, elapsed_sec: Math.round((Date.now() - job.started) / 1000),
    final_message: job.final == null ? null : short(job.final, MAX_TEXT),
    activity: job.items.slice(-40).map(item => short(item, 2000)),
    stderr: short(job.stderr, 4000), error: job.error ? short(job.error, 2000) : null,
    finished: !!job.finished, owner_pid: process.pid, child_pid: job.proc?.pid || null, exit_code: job.exit_code };
}
let journalHealthy = true;
function persistJob(job) {
  try { atomicJson(path.join(JOBS_DIR, job.id + '.json'), jobSnapshot(job)); }
  catch (error) { if (journalHealthy) log('job journal unavailable: ' + short(error.message, 500)); journalHealthy = false; }
}
function savedJob(id) {
  if (!/^[a-f0-9]{8}$/.test(id)) throw new Error('invalid job_id');
  const file = path.join(JOBS_DIR, id + '.json');
  if (!fs.existsSync(file)) throw new Error('unknown job');
  const out = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (out.status === 'running') {
    out.status = 'interrupted';
    out.recoverable = !!out.thread_id;
    out.hint = out.thread_id ? 'Inspect codex_session_read before explicitly resuming this thread; it is never replayed automatically.' : 'No thread was recorded; inspect the original request before starting again.';
  }
  return out;
}
function startCodex(args, cwd) {
  const id = crypto.randomUUID().slice(0, 8);
  const job = { id, cwd, status: 'running', thread_id: null, final: null, items: [], stderr: '', started: Date.now() };
  const proc = spawn(CODEX, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH || ''}` } });
  job.proc = proc;
  persistJob(job);
  let buf = '';
  proc.stdout.on('data', (d) => {
    buf += d;
    const lines = buf.split('\n'); buf = lines.pop();
    for (const line of lines) {
      let ev; try { ev = JSON.parse(line); } catch { continue; }
      if (ev.type === 'thread.started') job.thread_id = ev.thread_id;
      else if (ev.type === 'item.completed' && ev.item) {
        const it = ev.item;
        if (it.type === 'agent_message') job.final = it.text;
        else if (it.type === 'command_execution') job.items.push(`$ ${it.command} -> exit ${it.exit_code}`);
        else if (it.type === 'file_change') job.items.push(`edited ${(it.changes || []).map((c) => c.path).join(', ')}`);
        else if (it.type === 'error') job.items.push(`error: ${it.message}`);
      } else if (ev.type === 'turn.failed' || ev.type === 'error') { job.status = 'failed'; job.items.push(`error: ${ev.error?.message || ev.message}`); }
    }
    persistJob(job);
  });
  proc.stderr.on('data', (d) => { job.stderr = (job.stderr + d).slice(-4000); });
  proc.on('close', (code) => { job.finished = true; if (job.status === 'running') job.status = code === 0 ? 'done' : 'failed'; job.exit_code = code; persistJob(job); });
  proc.on('error', (e) => { job.status = 'failed'; job.items.push(`spawn error: ${e.message}`); persistJob(job); });
  jobs.set(id, job);
  // IDs/results are journaled. Interrupted work is reported, never auto-replayed.
  return job;
}
function signalCommand(job, signal) {
  try { process.kill(-job.proc.pid, signal); }
  catch (error) { if (error.code !== 'ESRCH' && job.proc.exitCode === null && job.proc.signalCode === null) job.proc.kill(signal); }
}
function startCommand(command, args, cwd, timeoutSec) {
  const job = { id: crypto.randomUUID().slice(0, 8), kind: 'command', cwd, status: 'running',
    thread_id: null, final: null, items: [], stderr: '', started: Date.now() };
  const proc = spawn(command, args, { cwd, detached: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH || ''}`, CI: '1' } });
  job.proc = proc;
  jobs.set(job.id, job);
  persistJob(job);
  let stdout = '', stderr = '', killTimer;
  const stop = reason => {
    job.error = reason;
    signalCommand(job, 'SIGTERM');
    killTimer ||= setTimeout(() => signalCommand(job, 'SIGKILL'), 1000);
  };
  const timer = setTimeout(() => stop(`command timed out after ${timeoutSec} seconds`), timeoutSec * 1000);
  proc.stdout.on('data', chunk => { stdout += chunk; if (stdout.length + stderr.length > 8e6) { stdout = stdout.slice(0, 8e6); stop('command output limit exceeded'); } });
  proc.stderr.on('data', chunk => { stderr += chunk; if (stdout.length + stderr.length > 8e6) { stderr = stderr.slice(0, 4000); stop('command output limit exceeded'); } });
  proc.on('error', error => { job.error = error.message; });
  proc.on('close', (code, signal) => {
    job.finished = true;
    if (job.error || job.status === 'cancelled') signalCommand(job, 'SIGKILL');
    clearTimeout(timer); clearTimeout(killTimer);
    job.exit_code = code;
    job.stderr = stderr.slice(-4000);
    job.final = clip(`exit ${code}${signal ? ` (${signal})` : ''}\n--- stdout\n${stdout}\n--- stderr\n${stderr}`);
    if (job.status !== 'cancelled') job.status = job.error || code !== 0 ? 'failed' : 'done';
    persistJob(job);
  });
  return job;
}
async function waitJob(job, sec) {
  const until = Date.now() + waitBudget(sec) * 1000; // short response budget; the job continues for later polling
  while (job.status === 'running' && Date.now() < until) await sleep(Math.min(100, until - Date.now()));
  return {
    job_id: job.id, status: job.status, ...(job.kind ? { kind: job.kind } : {}), ...(job.error ? { error: job.error } : {}), thread_id: job.thread_id, cwd: job.cwd,
    elapsed_sec: Math.round((Date.now() - job.started) / 1000),
    final_message: job.final, activity: job.items.slice(-40),
    ...(job.status === 'failed' && !job.final ? { stderr: job.stderr.slice(-1500) } : {}),
    ...(job.status === 'running' ? { hint: `still running — call codex_status with job_id ${job.id}` } : {}),
  };
}

// session_meta can run past 100KB (it embeds base_instructions), so read until the first newline.
function firstLine(file, cap = 4 << 20) {
  const fd = fs.openSync(file, 'r'); const chunk = Buffer.alloc(65536); let out = '';
  try {
    for (let pos = 0; out.length < cap; pos += chunk.length) {
      const n = fs.readSync(fd, chunk, 0, chunk.length, pos); if (n === 0) break;
      const str = chunk.toString('utf8', 0, n); const nl = str.indexOf('\n');
      if (nl >= 0) return out + str.slice(0, nl);
      out += str;
    }
  } finally { fs.closeSync(fd); }
  return out;
}

function codexSessions(limit, cwdFilter) {
  const names = new Map();
  try {
    for (const line of fs.readFileSync(path.join(CODEX_HOME, 'session_index.jsonl'), 'utf8').split('\n')) {
      try { const r = JSON.parse(line); names.set(r.id, r.thread_name); } catch { /* skip */ }
    }
  } catch { /* no index yet */ }
  const files = [];
  const walk = (d, depth) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory() && depth < 3) walk(p, depth + 1);
      else if (e.isFile() && e.name.endsWith('.jsonl')) files.push(p);
    }
  };
  try { walk(path.join(CODEX_HOME, 'sessions'), 0); } catch { return []; }
  files.sort((a, b) => path.basename(b).localeCompare(path.basename(a))); // rollout-<timestamp>-<id>.jsonl
  const out = [];
  for (const f of files) {
    if (out.length >= limit) break;
    let meta; try { meta = JSON.parse(firstLine(f)).payload; } catch { continue; }
    if (!meta?.id) continue;
    if (cwdFilter && meta.cwd !== cwdFilter) continue;
    out.push({ thread_id: meta.id, title: names.get(meta.id) || null, cwd: meta.cwd, started: meta.timestamp, source: meta.originator });
  }
  return out;
}

const REDACT = /(sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}|xox[abp]-[A-Za-z0-9-]{10,})/g;
const redact = (t) => String(t ?? '').replace(REDACT, '[REDACTED]');
const short = (t, n) => { t = redact(t); return t.length > n ? t.slice(0, n) + `…(+${t.length - n})` : t; };

// Recover stale persisted workers when the MCP bridge itself starts. Workers that
// survived the restart still hold their flock and are left untouched.
try {
  const recovery = await loopSupervisorCall('recover', null);
  if (recovery?.recovered?.length) log(`loop recovery ${recovery.recovered.join(',')}`);
} catch (e) {
  log(`loop recovery failed: ${e.message}`);
}
try {
  const recovery = await graphSupervisorCall('recover', null);
  if (recovery?.recovered?.length) log(`graph recovery ${recovery.recovered.join(',')}`);
} catch (e) {
  log(`graph recovery failed: ${e.message}`);
}

function sessionFile(threadId) {
  const walk = (d, depth) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory() && depth < 3) { const r = walk(p, depth + 1); if (r) return r; }
      else if (e.isFile() && e.name.endsWith(`-${threadId}.jsonl`)) return p;
    }
    return null;
  };
  const f = walk(path.join(CODEX_HOME, 'sessions'), 0);
  if (!f) throw new Error(`no session file for thread ${threadId}`);
  return f;
}

// Turn a rollout JSONL into a flat, human-sized transcript. Only `item_completed`
// events matter; reasoning is encrypted and developer/system prompts are noise.
export function parseSession(file, { outputChars = 0 } = {}) {
  const meta = { thread_id: null, cwd: null, started: null, source: null };
  const items = []; const legacy = []; const files = new Map(); let lastError = null; let turns = 0;
  const joinText = (c) => (c || []).map((x) => x.text || '').join('');
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    let e; try { e = JSON.parse(line); } catch { continue; }
    const p = e.payload || {};
    // pre-2026-03 rollouts have no item_completed events: messages + function_call pairs only
    if (e.type === 'response_item') {
      if (p.type === 'message' && p.role === 'user') { const t = joinText(p.content); if (!/^(# AGENTS\.md|<environment_context>|<app-context>|<recommended_plugins>)/.test(t)) legacy.push({ kind: 'user', text: short(t, 4000) }); }
      else if (p.type === 'message' && p.role === 'assistant') legacy.push({ kind: 'agent', text: short(joinText(p.content), 4000) });
      else if (p.type === 'function_call' && /shell/.test(p.name || '')) { let a = {}; try { a = JSON.parse(p.arguments); } catch { /* raw */ } legacy.push({ kind: 'command', text: short(Array.isArray(a.command) ? a.command.join(' ') : a.command || p.arguments, 300), exit: null }); }
      else if (p.type === 'function_call_output') { const m = /Exit code: (\d+)/.exec(p.output || ''); const last = legacy.at(-1); if (last?.kind === 'command') { if (m) last.exit = Number(m[1]); if (outputChars) last.output = short(p.output, outputChars); } }
    }
    if (e.type === 'session_meta') Object.assign(meta, { thread_id: p.id, cwd: p.cwd, started: p.timestamp, source: p.originator });
    else if (e.type === 'turn_context') { turns++; if (p.cwd) meta.cwd = p.cwd; }
    else if (e.type === 'event_msg' && p.type === 'task_complete') { if (p.error?.message) { lastError = redact(p.error.message); items.push({ kind: 'error', text: lastError }); } else lastError = null; }
    else if (e.type === 'event_msg' && p.type === 'item_completed' && p.item) {
      const it = p.item;
      if (it.type === 'UserMessage') items.push({ kind: 'user', text: short(joinText(it.content), 4000) });
      else if (it.type === 'AgentMessage') items.push({ kind: 'agent', text: short(joinText(it.content), 4000) });
      else if (it.type === 'CommandExecution') {
        const cmd = Array.isArray(it.command) ? it.command.slice(-1)[0] : String(it.command);
        items.push({ kind: 'command', text: short(cmd, 300), exit: it.exit_code, ...(outputChars ? { output: short(it.aggregated_output || it.stdout || '', outputChars) } : {}) });
      } else if (it.type === 'FileChange') {
        for (const [f, ch] of Object.entries(it.changes || {})) files.set(f, ch.type);
        items.push({ kind: 'file_change', files: Object.entries(it.changes || {}).map(([f, ch]) => `${ch.type} ${f}`) });
      } else if (it.type === 'McpToolCall') items.push({ kind: 'mcp', text: `${it.server}.${it.tool}` });
    }
  }
  const talk = (arr) => arr.filter((i) => i.kind === 'user' || i.kind === 'agent').length;
  const chosen = talk(legacy) > talk(items) ? legacy.concat(items.filter((i) => i.kind === 'error')) : items;
  return { meta, turns, items: chosen, files: [...files].map(([f, t]) => `${t} ${f}`), last_error: lastError };
}

function gitSnapshot(cwd) {
  const g = (...a) => { const r = spawnSync('git', a, { cwd, encoding: 'utf8', maxBuffer: 8e6 }); return r.status === 0 ? r.stdout.trim() : null; };
  if (g('rev-parse', '--is-inside-work-tree') !== 'true') return { git: false };
  return { git: true, branch: g('rev-parse', '--abbrev-ref', 'HEAD'), status: g('status', '--short') || '(clean)', diff_stat: g('diff', '--stat') || '', last_commits: g('log', '--oneline', '-5') };
}

// ---- tools --------------------------------------------------------------------
const INSTRUCTIONS = `You are working on the user's own machine through codex-bridge. Roots: ${ROOTS.join(', ')}.
Start with list_projects. Paths are absolute. Read before you edit; edit_file needs an exact unique match.
Two ways to work: (1) do it yourself with read_file/edit_file/write_file/run_command, or (2) hand a whole task to the local Codex CLI with codex_run (sandboxed, may take minutes; if it returns status running, poll codex_status). To pick up work in progress from the Codex app: codex_handoff (newest thread by default) gives goal, last message, files Codex touched, and the live git state; codex_session_read shows the transcript; codex_resume continues that thread; git_diff shows uncommitted changes. Use wait_sec=0 for start/resume calls and short status polls; MCP calls have a maximum 20-second wait budget. Prefer codex_run for multi-file changes and test loops, direct tools for small edits and inspection. Report real tool results; never claim a change you did not verify.
Slash commands (Codex CLI style). The user may start a message with one of these; follow it. If a project dir is needed and not given, use the one we are discussing or ask.
/goal <objective> - for one long autonomous workstream, prefer codex_loop_start with explicit verifier commands. If the objective can be safely decomposed into independent/dependent sub-tasks and the git working tree is clean, prefer codex_graph_start with an explicit DAG, per-task verifiers, final verification, and bounded max_parallel. Poll codex_loop_status/codex_graph_status and use the matching resume tool after recoverable interruption. Never claim verified completion without external verifier success. codex_run/codex_resume remain available for one-off or manually supervised work. End with status, what changed, and the verification you ran.
/review [focus] - codex_run with sandbox read-only asking Codex to review \`git diff HEAD\` for correctness bugs, findings by severity with file:line, no edits. Check its findings against git_diff and report only the ones that hold up.
/diff - git_diff, summarize per file; show the diff if short.
/status - codex_handoff: thread goal, where Codex stopped and why, files it changed, git branch/status.
/plan <task> - investigate with read_file/search/list_dir (or codex_run read-only), no edits. Return files to touch, ordered steps, risks, how to verify; wait for a go.
/resume [thread_id] - with an id, codex_resume it; without, list codex_sessions (title, cwd, id) and ask which one.`;

// The slash commands above are for web ChatGPT, which has no slash menu for MCP servers
// (neither client ever calls prompts/list). The Codex app has these natively.

let drainingUntil = 0;
let inFlightTools = 0;
const admissionTools = new Set(['codex_run','codex_resume','run_command','codex_loop_start','codex_loop_resume','codex_graph_start','codex_graph_resume','edit_file','write_file','orca_daemon_restart']);
function buildServer() {
  const s = new McpServer({ name: 'codex-bridge', version: '0.1.0' }, { instructions: INSTRUCTIONS });
  const tool = (name, desc, schema, fn) => s.registerTool(name, { description: desc, inputSchema: schema }, async (a) => {
    const t0 = Date.now();
    const admission = admissionTools.has(name);
    if (admission && Date.now() < drainingUntil) return {isError:true,content:[{type:'text',text:'Bridge recovery is preparing a restart; no new work was started. Retry shortly.'}]};
    if (admission) inFlightTools++;
    try { const r = await fn(a); log(`${name} ok ${Date.now() - t0}ms ${a.path || a.cwd || ''}`); return text(r); }
    catch (e) { log(`${name} err ${e.message}`); return { isError: true, content: [{ type: 'text', text: `error: ${e.message}` }] }; }
    finally { if (admission) inFlightTools--; }
  });

  tool('list_projects', 'List project directories under the allowed roots.', {}, () =>
    ROOTS.filter((r) => fs.existsSync(r)).flatMap((r) =>
      fs.readdirSync(r, { withFileTypes: true }).filter((e) => e.isDirectory() && !e.name.startsWith('.')).map((e) => path.join(r, e.name))));

  tool('list_dir', 'List a directory (skips node_modules/.git). depth 1-3.',
    { path: z.string(), depth: z.number().int().min(1).max(3).default(1) }, ({ path: p, depth }) => {
      const root = guard(p); const out = [];
      const walk = (d, lvl) => {
        for (const e of fs.readdirSync(d, { withFileTypes: true })) {
          if (e.name === 'node_modules' || e.name === '.git') continue;
          const rel = path.relative(root, path.join(d, e.name));
          out.push(e.isDirectory() ? rel + '/' : rel);
          if (e.isDirectory() && lvl < depth && out.length < 800) walk(path.join(d, e.name), lvl + 1);
        }
      };
      walk(root, 1);
      return out.length > 800 ? out.slice(0, 800).concat(['…truncated']) : out;
    });

  tool('read_file', 'Read a text file, optionally a line range (1-based, inclusive).',
    { path: z.string(), start_line: z.number().int().min(1).optional(), end_line: z.number().int().min(1).optional() },
    ({ path: p, start_line, end_line }) => {
      const lines = fs.readFileSync(guard(p), 'utf8').split('\n');
      const s = start_line || 1, e = end_line || lines.length;
      return clip(`[lines ${s}-${Math.min(e, lines.length)} of ${lines.length}]\n` + lines.slice(s - 1, e).join('\n'));
    });

  tool('search', 'Regex search (grep -rnE) under a directory. Returns up to 200 matching lines.',
    { path: z.string(), pattern: z.string(), glob: z.string().optional().describe('e.g. *.ts') }, ({ path: p, pattern, glob }) => {
      const args = ['-rnIE', '--exclude-dir=node_modules', '--exclude-dir=.git', '--exclude-dir=dist', ...(glob ? [`--include=${glob}`] : []), '-e', pattern, guard(p)];
      const r = spawnSync('grep', args, { encoding: 'utf8', maxBuffer: 8e6 });
      const lines = (r.stdout || '').split('\n').filter(Boolean);
      return lines.length ? clip(lines.slice(0, 200).join('\n') + (lines.length > 200 ? `\n…${lines.length - 200} more` : '')) : '(no matches)';
    });

  tool('write_file', 'Create or overwrite a file (parents created).',
    { path: z.string(), content: z.string() }, ({ path: p, content }) => {
      const real = guard(p, { write: true }); fs.mkdirSync(path.dirname(real), { recursive: true });
      fs.writeFileSync(real, content); return `wrote ${content.length} chars to ${real}`;
    });

  tool('edit_file', 'Replace one exact occurrence of old_text with new_text. Fails if 0 or >1 matches.',
    { path: z.string(), old_text: z.string().min(1), new_text: z.string() }, ({ path: p, old_text, new_text }) => {
      const real = guard(p, { write: true }); const src = fs.readFileSync(real, 'utf8');
      const n = src.split(old_text).length - 1;
      if (n !== 1) throw new Error(n === 0 ? 'old_text not found' : `old_text matches ${n} times; include more context`);
      fs.writeFileSync(real, src.replace(old_text, () => new_text)); return `edited ${real}`;
    });

  tool('run_command', `Run an allowlisted program in a project dir. Allowed: ${[...ALLOW_CMDS].join(', ')}. Long commands return a job_id after at most 20 seconds; poll codex_status to retrieve output, or codex_cancel to stop them.`,
    { cwd: z.string(), command: z.string(), args: z.array(z.string()).default([]), timeout_sec: z.number().int().min(1).max(600).default(120) },
    async ({ cwd, command, args, timeout_sec }) => {
      if (!ALLOW_CMDS.has(command)) throw new Error(`command not allowed: ${command}`);
      const job = startCommand(command, args, guard(cwd), timeout_sec);
      const result = await waitJob(job, 20);
      if (job.status === 'running') return result;
      if (job.error) throw new Error(job.error);
      return job.final;
    });

  tool('orca_daemon_status', 'Show the currently running Orca terminal daemon process(es). Read-only.', {}, () => {
    const processes = orcaDaemonProcesses();
    return { running: processes.length > 0, count: processes.length, processes };
  });

  tool('orca_daemon_restart', 'Restart only the exact Orca terminal daemon process. Requires confirm=true and may interrupt active Orca terminal/agent sessions.',
    { confirm: z.boolean() }, async ({ confirm }) => {
      if (confirm !== true) throw new Error('confirm=true is required because restarting the Orca daemon can interrupt active sessions');
      const before = orcaDaemonProcesses();
      if (!before.length) return { restarted: false, reason: 'Orca terminal daemon not running', before: [], after: [] };
      const oldPids = new Set(before.map((p) => p.pid));
      for (const { pid } of before) process.kill(pid, 'SIGTERM');
      const gracefulDeadline = Date.now() + 2500;
      while (Date.now() < gracefulDeadline && [...oldPids].some(pidAlive)) await sleep(100);
      const forced = [...oldPids].filter(pidAlive);
      for (const pid of forced) process.kill(pid, 'SIGKILL');
      const respawnDeadline = Date.now() + 10000;
      let after = [];
      do {
        await sleep(150);
        after = orcaDaemonProcesses().filter((p) => !oldPids.has(p.pid));
      } while (!after.length && Date.now() < respawnDeadline);
      return { restarted: after.length > 0, before, forced_pids: forced, after };
    });

  const codexOut = 'Returns {job_id, status: running|done|failed, thread_id, final_message, activity}. Each call waits at most 20 seconds; poll codex_status for ongoing work.';
  tool('codex_run', `Delegate a coding task to the local Codex CLI (codex exec) in a project dir. Waits up to wait_sec, then returns even if still running. ${codexOut}`,
    { cwd: z.string(), prompt: z.string().min(1), sandbox: z.enum(['read-only', 'workspace-write']).default('workspace-write'),
      model: z.string().optional(), wait_sec: z.number().int().min(0).max(540).default(20) },
    ({ cwd, prompt, sandbox, model, wait_sec }) => {
      const dir = guard(cwd);
      const args = ['exec', '--json', '--skip-git-repo-check', '-C', dir, '-s', sandbox, ...(model ? ['-m', model] : []), prompt];
      return waitJob(startCodex(args, dir), wait_sec);
    });

  tool('codex_resume', `Continue an existing Codex thread with a new prompt. ${codexOut}`,
    { thread_id: z.string(), prompt: z.string().min(1), cwd: z.string().optional().describe('defaults to the thread\'s original cwd'), wait_sec: z.number().int().min(0).max(540).default(20) },
    ({ thread_id, prompt, cwd, wait_sec }) => {
      const dir = guard(cwd || codexSessions(200).find((s) => s.thread_id === thread_id)?.cwd || '/nonexistent');
      return waitJob(startCodex(['exec', '--json', '--skip-git-repo-check', '-C', dir, 'resume', thread_id, prompt], dir), wait_sec);
    });

  tool('codex_status', 'Poll a running codex job.', { job_id: z.string(), wait_sec: z.number().int().min(0).max(540).default(20) },
    ({ job_id, wait_sec }) => { const j = jobs.get(job_id); if (!j) return savedJob(job_id); return waitJob(j, wait_sec); });

  tool('codex_jobs', 'List saved job IDs/results after a lost response. Never starts or replays work.',
    { limit: z.number().int().min(1).max(100).default(20) }, ({ limit }) =>
      fs.readdirSync(JOBS_DIR).filter(name => /^[a-f0-9]{8}\.json$/.test(name))
        .map(name => JSON.parse(fs.readFileSync(path.join(JOBS_DIR, name), 'utf8')))
        .sort((a, b) => b.started - a.started).slice(0, limit).map(job => jobs.has(job.job_id) ? jobSnapshot(jobs.get(job.job_id)) : savedJob(job.job_id)));
  tool('codex_recovery_status', 'Read the PC-side service watchdog state. This does not restart services.', {}, () => {
    const file = path.join(STATE, 'recovery-state.json');
    return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : { status: 'not_configured' };
  });

  tool('codex_cancel', 'Kill a running codex job.', { job_id: z.string() }, ({ job_id }) => {
    const j = jobs.get(job_id); if (!j) throw new Error('unknown job');
    if (j.status === 'running') { if (j.kind === 'command') { signalCommand(j, 'SIGTERM'); const timer = setTimeout(() => signalCommand(j, 'SIGKILL'), 1000); timer.unref(); } else j.proc.kill('SIGTERM'); j.status = 'cancelled'; persistJob(j); } return { job_id, status: j.status };
  });

  const verifyCommandSchema = z.object({
    command: z.string().min(1),
    args: z.array(z.string()).default([]),
  });
  const loopOut = 'Each MCP call waits at most 20 seconds; use the returned loop_id to poll. Persistent Loop Engineering supervisor backed by a detached Python worker with flock ownership, heartbeat/watchdog and restart recovery.';
  tool('codex_loop_start', `Start an autonomous Codex loop with external verification. ${loopOut}`,
    {
      cwd: z.string(),
      goal: z.string().min(1),
      verify_commands: z.array(verifyCommandSchema).max(20).default([]),
      max_attempts: z.number().int().min(1).max(20).default(5),
      watchdog_interval_sec: z.number().min(0.1).max(300).default(5),
      hang_timeout_sec: z.number().min(0.5).max(86400).default(300),
      terminate_grace_sec: z.number().min(0.1).max(60).default(5),
      wait_sec: z.number().int().min(0).max(540).default(20),
    },
    async ({ cwd, goal, verify_commands, max_attempts, watchdog_interval_sec, hang_timeout_sec, terminate_grace_sec, wait_sec }) => {
      const dir = guard(cwd);
      for (const spec of verify_commands) if (!ALLOW_CMDS.has(spec.command)) throw new Error(`verifier command not allowed: ${spec.command}`);
      const state = await loopSupervisorCall('create', null, {
        cwd: dir,
        goal,
        verify_commands,
        max_attempts,
        watchdog_interval_sec,
        hang_timeout_sec,
        terminate_grace_sec,
        codex_bin: CODEX,
        allow_commands: [...ALLOW_CMDS],
      });
      return wait_sec > 0 ? waitSupervisorLoop(state.loop_id, wait_sec) : state;
    });

  tool('codex_loop_status', `Read or poll a persistent loop. Heartbeat, last activity/progress, worker/child PID and watchdog history are included. ${loopOut}`,
    { loop_id: z.string(), wait_sec: z.number().int().min(0).max(540).default(0) },
    async ({ loop_id, wait_sec }) => wait_sec > 0 ? waitSupervisorLoop(loop_id, wait_sec) : await loopSupervisorCall('status', loop_id));

  tool('codex_loop_resume', `Resume an interrupted/retryable loop. The detached worker reuses the stored Codex thread when available; flock prevents duplicate workers. ${loopOut}`,
    {
      loop_id: z.string(),
      verify_commands: z.array(verifyCommandSchema).max(20).optional(),
      wait_sec: z.number().int().min(0).max(540).default(20),
    },
    async ({ loop_id, verify_commands, wait_sec }) => {
      if (verify_commands) for (const spec of verify_commands) if (!ALLOW_CMDS.has(spec.command)) throw new Error(`verifier command not allowed: ${spec.command}`);
      const state = await loopSupervisorCall('resume', loop_id, verify_commands ? { verify_commands } : {});
      return wait_sec > 0 ? waitSupervisorLoop(loop_id, wait_sec) : state;
    });

  tool('codex_loop_cancel', `Cancel an active loop. The supervisor persists cancelled before terminating the child, so the watchdog cannot race into a restart. ${loopOut}`,
    { loop_id: z.string() },
    async ({ loop_id }) => await loopSupervisorCall('cancel', loop_id));

  const graphTaskSchema = z.object({
    task_id: z.string().regex(/^[A-Za-z0-9_-]{1,64}$/),
    goal: z.string().min(1),
    depends_on: z.array(z.string()).default([]),
    write_paths: z.array(z.string()).describe('Explicit relative files/directories this task may modify. Out-of-scope changes are discarded before integration.'),
    verify_commands: z.array(verifyCommandSchema).max(20).default([]),
    max_attempts: z.number().int().min(1).max(20).optional(),
  });
  const graphOut = 'Polling waits at most 20 seconds; use the returned graph_id to poll. Dependency-aware multi-worker orchestration. Tasks run in isolated git worktrees, then merge into a private integration worktree before an externally verified final patch is applied to the clean base working tree.';
  tool('codex_graph_start', `Start a DAG of Codex loop tasks with bounded parallelism. Requires a clean git working tree. ${graphOut}`,
    {
      cwd: z.string(),
      goal: z.string().default(''),
      tasks: z.array(graphTaskSchema).min(1).max(32),
      max_parallel: z.number().int().min(1).max(8).default(2),
      task_max_attempts: z.number().int().min(1).max(20).default(5),
      final_verify_commands: z.array(verifyCommandSchema).max(20).default([]),
      apply_to_base: z.boolean().default(true),
      watchdog_interval_sec: z.number().min(0.1).max(300).default(5),
      hang_timeout_sec: z.number().min(0.5).max(86400).default(300),
      terminate_grace_sec: z.number().min(0.1).max(60).default(5),
      wait_sec: z.number().int().min(0).max(540).default(0),
    },
    async ({ cwd, goal, tasks, max_parallel, task_max_attempts, final_verify_commands, apply_to_base, watchdog_interval_sec, hang_timeout_sec, terminate_grace_sec, wait_sec }) => {
      const dir = guard(cwd);
      for (const task of tasks) for (const spec of task.verify_commands) if (!ALLOW_CMDS.has(spec.command)) throw new Error(`verifier command not allowed: ${spec.command}`);
      for (const spec of final_verify_commands) if (!ALLOW_CMDS.has(spec.command)) throw new Error(`final verifier command not allowed: ${spec.command}`);
      const state = await graphSupervisorCall('create', null, {
        cwd: dir,
        goal,
        tasks,
        max_parallel,
        task_max_attempts,
        final_verify_commands,
        apply_to_base,
        watchdog_interval_sec,
        hang_timeout_sec,
        terminate_grace_sec,
        codex_bin: CODEX,
        allow_commands: [...ALLOW_CMDS],
      });
      return wait_sec > 0 ? waitSupervisorGraph(state.graph_id, wait_sec) : state;
    });

  tool('codex_graph_status', `Read or poll a persistent task graph, including per-task loop/worktree/integration state and peak concurrency. ${graphOut}`,
    { graph_id: z.string(), wait_sec: z.number().int().min(0).max(540).default(0) },
    async ({ graph_id, wait_sec }) => wait_sec > 0 ? waitSupervisorGraph(graph_id, wait_sec) : await graphSupervisorCall('status', graph_id));

  tool('codex_graph_resume', `Resume an interrupted/integration-ready graph or attach missing final verification. Active owners keep their current verifiers. For merge conflicts, resolve and stage files in the integration worktree, then pass resolve_conflict=true to reverify and adopt that resolution. Task-failed graphs stay blocked. flock prevents duplicate graph workers. ${graphOut}`,
    {
      graph_id: z.string(),
      resolve_conflict: z.boolean().default(false),
      final_verify_commands: z.array(verifyCommandSchema).max(20).optional(),
      wait_sec: z.number().int().min(0).max(540).default(0),
    },
    async ({ graph_id, resolve_conflict, final_verify_commands, wait_sec }) => {
      if (final_verify_commands) for (const spec of final_verify_commands) if (!ALLOW_CMDS.has(spec.command)) throw new Error(`final verifier command not allowed: ${spec.command}`);
      const state = await graphSupervisorCall('resume', graph_id, { resolve_conflict, ...(final_verify_commands ? { final_verify_commands } : {}) });
      return wait_sec > 0 ? waitSupervisorGraph(graph_id, wait_sec) : state;
    });

  tool('codex_graph_cancel', `Cancel a graph and all currently running child loops. ${graphOut}`,
    { graph_id: z.string() },
    async ({ graph_id }) => await graphSupervisorCall('cancel', graph_id));

  tool('codex_sessions', 'List recent Codex threads on this machine (newest first), optionally filtered by cwd.',
    { limit: z.number().int().min(1).max(50).default(20), cwd: z.string().optional() }, ({ limit, cwd }) => codexSessions(limit, cwd));

  tool('codex_session_read', 'Transcript of one Codex thread: user/agent messages, commands (exit codes), file changes, MCP calls. Newest last. Secrets redacted.',
    { thread_id: z.string(), last_n: z.number().int().min(1).max(300).default(60), output_chars: z.number().int().min(0).max(4000).default(0).describe('include this many chars of each command output') },
    ({ thread_id, last_n, output_chars }) => {
      const p = parseSession(sessionFile(thread_id), { outputChars: output_chars });
      return { ...p.meta, turns: p.turns, total_items: p.items.length, items: p.items.slice(-last_n), last_error: p.last_error };
    });

  tool('codex_handoff', 'Pull a Codex thread into this chat: goal, latest agent message, error if it stopped, files Codex changed, recent commands, and the current git state of its project. Defaults to the newest thread.',
    { thread_id: z.string().optional(), cwd: z.string().optional().describe('pick the newest thread for this project dir') },
    ({ thread_id, cwd }) => {
      // newest thread that actually has content — empty just-opened chats are skipped
      let id = thread_id, p;
      if (!id) for (const s of codexSessions(10, cwd)) { const q = parseSession(sessionFile(s.thread_id)); if (q.items.length) { id = s.thread_id; p = q; break; } }
      if (!id) throw new Error('no Codex sessions with content found');
      p = p || parseSession(sessionFile(id));
      const users = p.items.filter((i) => i.kind === 'user'), agents = p.items.filter((i) => i.kind === 'agent');
      let git = null; try { git = gitSnapshot(guard(p.meta.cwd)); } catch (e) { git = { git: false, note: e.message }; }
      return {
        ...p.meta, title: codexSessions(200).find((s) => s.thread_id === id)?.title || null, turns: p.turns,
        goal: users[0]?.text || null, latest_user_message: users.at(-1)?.text || null, latest_agent_message: agents.at(-1)?.text || null,
        stopped_with_error: p.last_error, files_changed_by_codex: p.files,
        recent_commands: p.items.filter((i) => i.kind === 'command').slice(-10).map((c) => `${c.text} -> exit ${c.exit}`),
        git,
        next: `codex_resume thread_id=${id} to let Codex continue, or read_file/edit_file to take over here; git_diff cwd=${p.meta.cwd} for the full uncommitted diff.`,
      };
    });

  tool('git_diff', 'Uncommitted diff of a project (working tree vs HEAD; staged=true for the index). Optional single path.',
    { cwd: z.string(), path: z.string().optional(), staged: z.boolean().default(false) }, ({ cwd, path: p, staged }) => {
      const dir = guard(cwd); const args = ['diff', ...(staged ? ['--cached'] : []), ...(p ? ['--', guard(p)] : [])];
      const r = spawnSync('git', args, { cwd: dir, encoding: 'utf8', maxBuffer: 16e6 });
      if (r.status !== 0) throw new Error(r.stderr.trim() || 'git diff failed');
      return r.stdout ? clip(r.stdout) : '(no changes)';
    });

  return s;
}

// ---- http ---------------------------------------------------------------------
function authorized(req) {
  const m = /^Bearer\s+(.+)$/i.exec(req.headers.authorization || '');
  if (!m) return false;
  const a = Buffer.from(m[1].trim()), b = Buffer.from(TOKEN);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
export function createHttpServer() {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    if (url.pathname === '/health') return res.writeHead(200, { 'content-type': 'application/json' }).end('{"status":"ok"}');
    if (!['/mcp', '/runtime', '/recovery/prepare', '/recovery/release'].includes(url.pathname)) return res.writeHead(404).end();
    if (!authorized(req)) { log(`auth-fail ${req.socket.remoteAddress}`); return res.writeHead(401, { 'WWW-Authenticate': 'Bearer' }).end('{"error":"unauthorized"}'); }
    const activeJobs = () => [...jobs.values()].filter(job => job.proc.exitCode === null && job.proc.signalCode === null).length;
    if (url.pathname === '/recovery/prepare' || url.pathname === '/recovery/release') {
      if (req.method !== 'POST') return res.writeHead(405).end();
      if (url.pathname === '/recovery/release') { drainingUntil = 0; return res.writeHead(200).end('{"ok":true}'); }
      if (activeJobs() || inFlightTools || !journalHealthy) return res.writeHead(409).end('{"ok":false,"reason":"active_work"}');
      drainingUntil = Date.now() + 30000;
      return res.writeHead(200, {'content-type':'application/json'}).end(JSON.stringify({ok:true,owner_pid:process.pid}));
    }
    if (url.pathname === '/runtime') return res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ version: 1, owner_pid: process.pid, active_jobs: activeJobs(), in_flight_tools: inFlightTools, journal_healthy: journalHealthy }));
    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined }); // stateless
    res.on('close', () => { transport.close().catch(() => {}); server.close().catch(() => {}); });
    // log JSON-RPC methods so we can see what the client actually asks for (e.g. prompts/list)
    let body;
    if (req.method === 'POST') {
      const chunks = []; for await (const c of req) chunks.push(c);
      try { body = JSON.parse(Buffer.concat(chunks)); }
      catch { return res.writeHead(400, { 'content-type': 'application/json' }).end('{"jsonrpc":"2.0","error":{"code":-32700,"message":"Parse error"},"id":null}'); }
    }
    const methods = [body].flat().map((m) => m?.method).filter((m) => m && m !== 'tools/call');
    if (methods.length) log(`rpc ${methods.join(',')} ${req.headers['user-agent'] || ''}`);
    try { await server.connect(transport); await transport.handleRequest(req, res, body); }
    catch (e) { log(`mcp err ${e.message}`); if (!res.headersSent) res.writeHead(500).end(); }
  });
}

// realpath: `npx`/npm bin links point here through a symlink
if (process.argv[1] && pathToFileURL(fs.realpathSync(process.argv[1])).href === import.meta.url) {
  const server = createHttpServer();
  server.listen(PORT, HOST, () => console.error(`codex-bridge listening on http://${HOST}:${server.address().port}/mcp  (token: ${TOKEN_FILE}, roots: ${ROOTS.join(', ')})`));
}
