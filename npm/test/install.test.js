import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const BIN = fileURLToPath(new URL('../bin/unfoldx-mcp.js', import.meta.url));
const API = 'https://unfoldx.example/api';
const KEY = 'ufx_test_key_0123456789';

function fakeTool(dir, name, body = '') {
  const path = join(dir, name);
  writeFileSync(path, `#!/bin/sh\necho "${name} $*" >> "$CALLS"\n${body}\nexit 0\n`);
  chmodSync(path, 0o755);
}

function setup({ tools = [], uv = true, loggedIn = false, dirs = [], failing = [], binDir } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'unfoldx-npx-'));
  const home = join(root, 'home');
  const bin = join(root, 'bin');
  const toolBin = join(root, 'uv-tools', 'bin');
  for (const dir of [home, bin, toolBin]) mkdirSync(dir, { recursive: true });
  for (const dir of dirs) mkdirSync(join(home, dir), { recursive: true });
  if (uv) fakeTool(bin, 'uv', `[ "$1 $2 $3" = "tool dir --bin" ] && echo "uv warning" >&2 && echo "${binDir ?? toolBin}"`);
  fakeTool(toolBin, 'unfoldx', `[ "$1" = "whoami" ] && exit ${loggedIn ? 0 : 3}`);
  fakeTool(toolBin, 'unfoldx-mcp');
  for (const name of tools) fakeTool(bin, name, failing.includes(name) && '[ "$2" = "add" ] && exit 1' || '');
  return { root, home, toolBin, calls: join(root, 'calls.log') };
}

function run(ctx, args) {
  const result = spawnSync(process.execPath, [BIN, ...args], {
    env: { PATH: `${join(ctx.root, 'bin')}:/usr/bin:/bin`, HOME: ctx.home, CALLS: ctx.calls }, encoding: 'utf8',
  });
  const calls = existsSync(ctx.calls) ? readFileSync(ctx.calls, 'utf8').trim().split('\n') : [];
  return { code: result.status, out: result.stdout + result.stderr, calls };
}

const readJson = path => JSON.parse(readFileSync(path, 'utf8'));

test('a key writes Cursor and Kimi configs next to the servers already there', () => {
  const ctx = setup();
  mkdirSync(join(ctx.home, '.cursor'));
  writeFileSync(join(ctx.home, '.cursor', 'mcp.json'), JSON.stringify({ mcpServers: { other: { command: 'other' } } }));
  const { code, out, calls } = run(ctx, ['install', '--client', 'cursor,kimi', '--api-base', API, '--key', KEY]);
  assert.equal(code, 0, out);
  const server = { command: join(ctx.toolBin, 'unfoldx-mcp'), env: { UNFOLDX_API_BASE: API, UNFOLDX_TOKEN: KEY } };
  assert.deepEqual(readJson(join(ctx.home, '.cursor', 'mcp.json')).mcpServers, { other: { command: 'other' }, unfoldx: server });
  assert.deepEqual(readJson(join(ctx.home, '.kimi-code', 'mcp.json')).mcpServers, { unfoldx: server });
  assert.ok(calls.includes('uv tool install --upgrade unfoldx') && calls.includes('uv tool install --upgrade unfoldx-mcp'), calls.join('\n'));
  assert.ok(!calls.some(line => line.startsWith('unfoldx login')), 'a key needs no browser login');
  assert.ok(!out.includes(KEY), 'the key is never printed');
});

test('Claude Code and Codex are added through their own commands, replacing an old entry', () => {
  const ctx = setup({ tools: ['claude', 'codex'] });
  const { code, out, calls } = run(ctx, ['install', '--client', 'claude,codex', '--api-base', API, '--key', KEY]);
  assert.equal(code, 0, out);
  const server = join(ctx.toolBin, 'unfoldx-mcp');
  assert.deepEqual(calls.filter(line => /^(claude|codex) /.test(line)), [
    'claude mcp remove -s user unfoldx',
    `claude mcp add -s user unfoldx -e UNFOLDX_API_BASE=${API} -e UNFOLDX_TOKEN=${KEY} -- ${server}`,
    'codex mcp remove unfoldx',
    `codex mcp add unfoldx --env UNFOLDX_API_BASE=${API} --env UNFOLDX_TOKEN=${KEY} -- ${server}`,
  ]);
});

test('Gemini CLI and Qwen Code are added at user scope', () => {
  const ctx = setup({ tools: ['gemini', 'qwen'] });
  const { code, out, calls } = run(ctx, ['install', '--client', 'gemini,qwen', '--api-base', API]);
  assert.equal(code, 0, out);
  const server = join(ctx.toolBin, 'unfoldx-mcp');
  assert.ok(calls.includes(`gemini mcp add -s user unfoldx ${server} -e UNFOLDX_API_BASE=${API}`), calls.join('\n'));
  assert.ok(calls.includes(`qwen mcp add -s user unfoldx ${server} -e UNFOLDX_API_BASE=${API}`), calls.join('\n'));
});

test('without a key it logs in through the browser and the config carries no token', () => {
  const ctx = setup();
  const { code, out, calls } = run(ctx, ['install', '--client', 'cursor', '--api-base', API]);
  assert.equal(code, 0, out);
  assert.ok(calls.includes(`unfoldx login --api-base ${API}`), calls.join('\n'));
  assert.deepEqual(readJson(join(ctx.home, '.cursor', 'mcp.json')).mcpServers.unfoldx.env, { UNFOLDX_API_BASE: API });
});

test('an existing login is reused', () => {
  const ctx = setup({ loggedIn: true });
  const { code, out, calls } = run(ctx, ['install', '--client', 'cursor', '--api-base', API]);
  assert.equal(code, 0, out);
  assert.ok(!calls.some(line => line.startsWith('unfoldx login')), calls.join('\n'));
});

test('with no --client it configures only the tools found on this machine', () => {
  const ctx = setup({ tools: ['codex'], dirs: ['.kimi-code'], loggedIn: true });
  const { code, out, calls } = run(ctx, ['install', '--api-base', API]);
  assert.equal(code, 0, out);
  assert.ok(calls.some(line => line.startsWith('codex mcp add')), calls.join('\n'));
  assert.ok(existsSync(join(ctx.home, '.kimi-code', 'mcp.json')));
  assert.ok(!existsSync(join(ctx.home, '.cursor', 'mcp.json')));
  assert.match(out, /Codex/);
  assert.match(out, /Kimi Code/);
});

test('--client none installs and logs in without touching any tool', () => {
  const ctx = setup({ tools: ['claude'], dirs: ['.cursor'] });
  const { code, out, calls } = run(ctx, ['install', '--client', 'none', '--api-base', API]);
  assert.equal(code, 0, out);
  assert.ok(calls.includes(`unfoldx login --api-base ${API}`));
  assert.ok(!calls.some(line => line.startsWith('claude')));
  assert.ok(!existsSync(join(ctx.home, '.cursor', 'mcp.json')));
});

test('without uv it says how to get uv and changes nothing', () => {
  const ctx = setup({ uv: false, dirs: ['.cursor'] });
  const { code, out } = run(ctx, ['install', '--client', 'cursor', '--key', KEY]);
  assert.notEqual(code, 0);
  assert.match(out, /astral\.sh\/uv/);
  assert.ok(!existsSync(join(ctx.home, '.cursor', 'mcp.json')));
});

test('an unknown client or command is a usage error', () => {
  const ctx = setup();
  assert.equal(run(ctx, ['install', '--client', 'vim']).code, 2);
  assert.equal(run(ctx, ['frobnicate']).code, 2);
});

test('a tool that refuses the server fails the run and says which one', () => {
  const ctx = setup({ tools: ['claude', 'codex'], failing: ['claude'] });
  const { code, out, calls } = run(ctx, ['install', '--client', 'claude,codex', '--api-base', API, '--key', KEY]);
  assert.equal(code, 1);
  assert.match(out, /Claude Code/);
  assert.ok(calls.some(line => line.startsWith('codex mcp add')), 'the other tools are still configured');
  assert.ok(!out.includes(KEY));
});

test('a uv bin directory without the tools fails before any tool is touched', () => {
  const ctx = setup({ binDir: '/nowhere', dirs: ['.cursor'] });
  const { code, out, calls } = run(ctx, ['install', '--client', 'cursor', '--api-base', API]);
  assert.equal(code, 1);
  assert.match(out, /uv tool dir --bin/);
  assert.ok(!calls.some(line => line.startsWith('unfoldx ')), calls.join('\n'));
  assert.ok(!existsSync(join(ctx.home, '.cursor', 'mcp.json')));
});
