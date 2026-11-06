/**
 * Finding the semantic layer document a workspace holds.
 *
 * A semantic layer is a YAML file with a `semantic_layer:` key and a
 * `spec_version`. Workspaces are full of YAML that is not ours, so the test is
 * on content and deliberately cheap: read the head of the file, not all of it.
 */
import * as vscode from 'vscode';

const HEAD_BYTES = 4096;

export interface Layer {
  uri: vscode.Uri;
  name: string;
  tables: number;
}

function looksLikeLayer(head: string): boolean {
  return /^\s*semantic_layer\s*:/m.test(head) || /^\s*spec_version\s*:/m.test(head);
}

async function describe(uri: vscode.Uri): Promise<Layer | null> {
  let bytes: Uint8Array;
  try {
    bytes = await vscode.workspace.fs.readFile(uri);
  } catch {
    return null;
  }
  const head = Buffer.from(bytes.slice(0, HEAD_BYTES)).toString('utf8');
  if (!looksLikeLayer(head)) { return null; }

  const text = Buffer.from(bytes).toString('utf8');
  const name = /^\s*name\s*:\s*(\S+)/m.exec(head)?.[1]?.replace(/['"]/g, '')
    ?? uri.path.split('/').pop()!.replace(/\.ya?ml$/, '');
  // Counting `- name:` under tables is an estimate, and it is only ever shown
  // as one. Parsing the whole YAML to get it exactly would mean shipping a
  // YAML parser to render a subtitle.
  const tables = (text.match(/^\s{2,}-\s+name\s*:/gm) || []).length;
  return { uri, name, tables };
}

/** Every layer in the workspace, nearest the root first. */
export async function discover(max = 20): Promise<Layer[]> {
  const found = await vscode.workspace.findFiles(
    '**/*.{yaml,yml}', '**/{node_modules,.git,dist,build,target,.venv}/**', 200);
  const sorted = found.sort((a, b) => a.path.split('/').length - b.path.split('/').length);
  const out: Layer[] = [];
  for (const uri of sorted) {
    const layer = await describe(uri);
    if (layer) { out.push(layer); }
    if (out.length >= max) { break; }
  }
  return out;
}

/**
 * The layer to lint against: the configured one if set, else the first found.
 * Cached, because this runs on every keystroke and `findFiles` does not belong
 * in that path.
 */
let cached: { value: Layer | undefined; at: number } | undefined;
const TTL_MS = 30_000;

export function invalidate(): void { cached = undefined; }

export async function active(): Promise<Layer | undefined> {
  const configured = vscode.workspace.getConfiguration('semlayer').get<string>('layerPath');
  if (configured) {
    const uri = vscode.Uri.file(configured);
    return (await describe(uri)) ?? undefined;
  }
  if (cached && Date.now() - cached.at < TTL_MS) { return cached.value; }
  const value = (await discover(1))[0];
  cached = { value, at: Date.now() };
  return value;
}
