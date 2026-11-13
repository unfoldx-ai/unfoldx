import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, dirname, join } from 'node:path';

export const DEFAULT_API_BASE = 'https://dev.unfoldx.ai/api';
const SERVER = 'unfoldx';
const EXE = process.platform === 'win32' ? '.exe' : '';

const USAGE = `Usage: npx unfoldx-mcp install [--client <list>] [--api-base <url>] [--key <api key>]

Installs the unfoldx-mcp server (with uv) and adds it to your AI tools.
  --client    comma list of: claude, codex, cursor, gemini, qwen, kimi, or none.
              Default: every one of them found on this machine.
  --api-base  UnfoldX API (default ${DEFAULT_API_BASE}).
  --key       API key from the UnfoldX Connections page (or UNFOLDX_TOKEN).
              Without one you log in once in the browser.`;

const which = name => (process.env.PATH || '').split(delimiter).some(dir => dir && existsSync(join(dir, name + EXE)));

function envPairs(env) {
  return Object.entries(env).map(([key, value]) => `${key}=${value}`);
}

function runQuiet(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8' });
  return { ok: result.status === 0, out: `${result.stdout || ''}${result.stderr || ''}`.trim() };
}

function cliClient(id, name, binary, removeArgs, addArgs) {
  return {
    id, name, detect: () => which(binary),
    add(server, env) {
      runQuiet(binary, removeArgs);
      return runQuiet(binary, addArgs(server, env));
    },
  };
}

function jsonClient(id, name, configDir) {
  return {
    id, name, detect: () => existsSync(join(homedir(), configDir)),
    add(server, env) {
      const path = join(homedir(), configDir, 'mcp.json');
      let config = {};
      if (existsSync(path)) {
        try { config = JSON.parse(readFileSync(path, 'utf8') || '{}'); } catch { return { ok: false, out: `${path} is not valid JSON` }; }
      }
      config.mcpServers = { ...(config.mcpServers || {}), [SERVER]: { command: server, env } };
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(`${path}.tmp`, `${JSON.stringify(config, null, 2)}\n`);
      renameSync(`${path}.tmp`, path);
      return { ok: true, out: path };
    },
  };
}

export const CLIENTS = [
  cliClient('claude', 'Claude Code', 'claude', ['mcp', 'remove', '-s', 'user', SERVER],
    (server, env) => ['mcp', 'add', '-s', 'user', SERVER, ...envPairs(env).flatMap(pair => ['-e', pair]), '--', server]),
  cliClient('codex', 'Codex', 'codex', ['mcp', 'remove', SERVER],
    (server, env) => ['mcp', 'add', SERVER, ...envPairs(env).flatMap(pair => ['--env', pair]), '--', server]),
  jsonClient('cursor', 'Cursor', '.cursor'),
  cliClient('gemini', 'Gemini CLI', 'gemini', ['mcp', 'remove', '-s', 'user', SERVER],
    (server, env) => ['mcp', 'add', '-s', 'user', SERVER, server, ...envPairs(env).flatMap(pair => ['-e', pair])]),
  cliClient('qwen', 'Qwen Code', 'qwen', ['mcp', 'remove', '-s', 'user', SERVER],
    (server, env) => ['mcp', 'add', '-s', 'user', SERVER, server, ...envPairs(env).flatMap(pair => ['-e', pair])]),
  jsonClient('kimi', 'Kimi Code', '.kimi-code'),
];

export function parseArgs(argv) {
  const [command, ...rest] = argv;
  if (command !== 'install') return { error: command ? `Unknown command: ${command}` : null };
  const options = { apiBase: process.env.UNFOLDX_API_BASE || DEFAULT_API_BASE, key: process.env.UNFOLDX_TOKEN || '', clients: null };
  for (let i = 0; i < rest.length; i += 1) {
    const flag = rest[i];
    const value = rest[i + 1];
    if (!['--client', '--api-base', '--key'].includes(flag) || value === undefined) return { error: `Unknown or incomplete option: ${flag}` };
    i += 1;
    if (flag === '--api-base') options.apiBase = value.replace(/\/+$/, '');
    if (flag === '--key') options.key = value.trim();
    if (flag === '--client') {
      const ids = value.split(',').map(id => id.trim()).filter(Boolean);
      const unknown = ids.filter(id => id !== 'none' && !CLIENTS.some(client => client.id === id));
      if (unknown.length) return { error: `Unknown client: ${unknown.join(', ')}` };
      options.clients = ids.filter(id => id !== 'none');
    }
  }
  return { options };
}

export function install(options, log = console.log) {
  if (!which('uv')) {
    log('unfoldx-mcp runs on uv, which is not installed. Install it, then run this command again:');
    log('  macOS / Linux: curl -LsSf https://astral.sh/uv/install.sh | sh');
    log('  Windows:       powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"');
    return 1;
  }
  log('Installing unfoldx-mcp and the unfoldx CLI ...');
  for (const tool of ['unfoldx', 'unfoldx-mcp']) {
    const installed = runQuiet('uv', ['tool', 'install', '--upgrade', tool]);
    if (!installed.ok) {
      log(`uv could not install ${tool}:\n${installed.out}`);
      return 1;
    }
  }
  const toolBin = runQuiet('uv', ['tool', 'dir', '--bin']).out.split('\n').pop().trim();
  const server = join(toolBin, `unfoldx-mcp${EXE}`);
  const cli = join(toolBin, `unfoldx${EXE}`);

  const env = { UNFOLDX_API_BASE: options.apiBase };
  if (options.key) {
    env.UNFOLDX_TOKEN = options.key;
  } else if (!runQuiet(cli, ['whoami', '--api-base', options.apiBase]).ok) {
    log(`Log in to ${options.apiBase} in your browser:`);
    if (spawnSync(cli, ['login', '--api-base', options.apiBase], { stdio: 'inherit' }).status !== 0) {
      log('Login did not finish. Run this command again, or pass --key from the UnfoldX Connections page.');
      return 1;
    }
  }

  const targets = options.clients === null
    ? CLIENTS.filter(client => client.detect())
    : CLIENTS.filter(client => options.clients.includes(client.id));
  if (!targets.length) {
    log(options.clients === null
      ? `No supported AI tool found. Add the MCP server "${SERVER}" with command ${server} to your tool by hand.`
      : `Installed. MCP command for any tool: ${server}`);
    return 0;
  }
  const failed = [];
  for (const client of targets) {
    const result = client.add(server, env);
    if (result.ok) {
      log(`  added to ${client.name}`);
    } else {
      failed.push(client.name);
      log(`  could not add to ${client.name}: ${options.key ? result.out.split(options.key).join('***') : result.out}`);
    }
  }
  log(failed.length
    ? `Not added to: ${failed.join(', ')}.`
    : 'Done. Restart the tool and ask it to "call unfoldx_whoami"; it should name your credential.');
  return failed.length ? 1 : 0;
}

export function main(argv) {
  const { options, error } = parseArgs(argv);
  if (!options) {
    if (error) console.error(error);
    console.error(USAGE);
    return 2;
  }
  return install(options);
}
