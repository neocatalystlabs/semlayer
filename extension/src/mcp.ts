/**
 * Writing an MCP client's config.
 *
 * Hand-editing a JSON config is the step that loses people, and it is the step
 * an extension can simply do: we already know the layer path and where the
 * binary is. Every client below takes the same `mcpServers` shape; only the
 * file differs.
 */
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';
import { cliPath } from './cli';

export interface Client {
  id: string;
  label: string;
  file: string;
}

function home(...parts: string[]): string { return path.join(os.homedir(), ...parts); }

export function clients(): Client[] {
  const list: Client[] = [
    { id: 'claude-code', label: 'Claude Code', file: home('.claude.json') },
    { id: 'cline', label: 'Cline', file: home('.cline', 'data', 'settings', 'cline_mcp_settings.json') },
    { id: 'cursor', label: 'Cursor', file: home('.cursor', 'mcp.json') },
  ];
  // Claude Desktop keeps its config somewhere different on every platform.
  const desktop = process.platform === 'darwin'
    ? home('Library', 'Application Support', 'Claude', 'claude_desktop_config.json')
    : process.platform === 'win32'
      ? path.join(process.env.APPDATA ?? home('AppData', 'Roaming'), 'Claude', 'claude_desktop_config.json')
      : home('.config', 'Claude', 'claude_desktop_config.json');
  list.push({ id: 'claude-desktop', label: 'Claude Desktop', file: desktop });
  return list;
}

async function readJson(file: string): Promise<Record<string, unknown>> {
  try {
    const bytes = await vscode.workspace.fs.readFile(vscode.Uri.file(file));
    const text = Buffer.from(bytes).toString('utf8').trim();
    return text ? JSON.parse(text) as Record<string, unknown> : {};
  } catch {
    return {};   // absent or unreadable: we are about to write it
  }
}

/** True when this client already points at a semlayer server. */
export async function isConfigured(client: Client): Promise<boolean> {
  const cfg = await readJson(client.file);
  const servers = cfg.mcpServers as Record<string, unknown> | undefined;
  return !!servers && 'semlayer' in servers;
}

/**
 * Merge our entry in, leaving every other server and every unrelated key
 * alone. A client config is the user's file, not ours: we add one key to it.
 */
export async function configure(client: Client, layer: vscode.Uri): Promise<void> {
  const cfg = await readJson(client.file);
  const servers = (cfg.mcpServers ?? {}) as Record<string, unknown>;

  servers.semlayer = {
    command: cliPath(),
    args: ['mcp', layer.fsPath],
  };
  cfg.mcpServers = servers;

  const uri = vscode.Uri.file(client.file);
  await vscode.workspace.fs.createDirectory(vscode.Uri.file(path.dirname(client.file)));
  await vscode.workspace.fs.writeFile(
    uri, Buffer.from(JSON.stringify(cfg, null, 2) + '\n', 'utf8'));
}
