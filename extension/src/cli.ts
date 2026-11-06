/**
 * Running the semlayer CLI.
 *
 * The extension has no engine of its own: every answer comes from the same
 * `semlayer` the user runs in a terminal, invoked with `--json`. That is the
 * whole integration surface -- no MCP, no HTTP, no reimplementation of lint
 * in TypeScript that could drift from the Python one.
 */
import { spawn } from 'child_process';
import * as vscode from 'vscode';

export class CliError extends Error {
  constructor(message: string, readonly code: number | null, readonly stderr: string) {
    super(message);
  }
}

/** Resolved once per session and re-resolved when the setting changes. */
export function cliPath(): string {
  return vscode.workspace.getConfiguration('semlayer').get<string>('cliPath') || 'semlayer';
}

export interface RunOptions {
  stdin?: string;
  cwd?: string;
  /** Kills the process if it outlives this. Lint is sub-second; infer is minutes. */
  timeoutMs?: number;
  token?: vscode.CancellationToken;
}

export interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

export function run(args: string[], opts: RunOptions = {}): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(cliPath(), args, {
      cwd: opts.cwd,
      env: process.env,
      shell: false,          // never a shell: arguments are paths the user typed
    });

    let stdout = '';
    let stderr = '';
    let settled = false;

    const done = (fn: () => void) => {
      if (settled) { return; }
      settled = true;
      clearTimeout(timer);
      sub?.dispose();
      fn();
    };

    const timer = opts.timeoutMs
      ? setTimeout(() => { child.kill(); done(() => reject(new CliError(
          `semlayer did not answer within ${opts.timeoutMs} ms`, null, stderr))); }, opts.timeoutMs)
      : undefined as unknown as NodeJS.Timeout;

    const sub = opts.token?.onCancellationRequested(() => {
      child.kill();
      done(() => reject(new vscode.CancellationError()));
    });

    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });

    child.on('error', (e: NodeJS.ErrnoException) => done(() => {
      // ENOENT is the only failure worth a tailored message: it is what a
      // missing install looks like, and it is the most likely first-run state.
      reject(e.code === 'ENOENT'
        ? new CliError(`'${cliPath()}' was not found on your PATH.`, null, String(e))
        : new CliError(e.message, null, String(e)));
    }));

    child.on('close', (code) => done(() => resolve({ code, stdout, stderr })));

    if (opts.stdin !== undefined) {
      child.stdin.on('error', () => { /* the child exited first; `close` reports it */ });
      child.stdin.end(opts.stdin);
    } else {
      child.stdin.end();
    }
  });
}

/** Run and parse `--json` output. Non-zero exit is NOT an error: lint exits 1 on findings. */
export async function runJson<T>(args: string[], opts: RunOptions = {}): Promise<T> {
  const { stdout, stderr, code } = await run(args, opts);
  const text = stdout.trim();
  if (!text) {
    throw new CliError(`semlayer ${args[0]} produced no output`, code, stderr);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new CliError(`semlayer ${args[0]} did not return JSON`, code, stderr || text.slice(0, 400));
  }
}

/** Version string, or null when the CLI is missing or unrunnable. */
export async function version(): Promise<string | null> {
  try {
    const { stdout, code } = await run(['--help'], { timeoutMs: 10_000 });
    return code === 0 ? (stdout.split('\n')[0] || 'semlayer').trim() : null;
  } catch {
    return null;
  }
}
