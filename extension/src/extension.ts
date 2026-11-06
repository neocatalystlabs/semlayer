/**
 * semlayer for VS Code and Cursor.
 *
 * The extension is a thin front end over the `semlayer` CLI. It holds no
 * engine, speaks no MCP, and has no runtime npm dependencies: every answer
 * comes from `semlayer <command> --json` run as a subprocess. MCP is how the
 * *agent* reads the layer; this is how the *human* does.
 */
import * as vscode from 'vscode';
import { Linter } from './diagnostics';
import { Panel } from './panel';
import { run, version, CliError } from './cli';
import * as layers from './layer';
import * as mcp from './mcp';

let status: vscode.StatusBarItem;

export async function activate(ctx: vscode.ExtensionContext): Promise<void> {
  const out = vscode.window.createOutputChannel('semlayer');
  const linter = new Linter(out);
  const panel = new Panel(ctx);

  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 95);
  status.command = 'semlayer.selectLayer';
  ctx.subscriptions.push(out, linter, status,
    vscode.window.registerWebviewViewProvider(Panel.viewId, panel));

  const refresh = async () => {
    await panel.refresh();
    await updateStatus();
    await setContexts();
  };

  // --- linting -----------------------------------------------------------
  const lintIfSql = (doc: vscode.TextDocument) => {
    if (doc.languageId === 'sql') { linter.settled(doc); }
  };
  vscode.workspace.textDocuments.forEach(lintIfSql);
  ctx.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument(lintIfSql),
    vscode.workspace.onDidSaveTextDocument(lintIfSql),
    vscode.workspace.onDidCloseTextDocument((d) => linter.clear(d)),
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (e.document.languageId === 'sql' && e.contentChanges.length) {
        linter.typing(e.document);
      }
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('semlayer')) {
        layers.invalidate();
        vscode.workspace.textDocuments.forEach(lintIfSql);
        void refresh();
      }
    }));

  // A layer that changes on disk changes every diagnostic in the workspace.
  const watcher = vscode.workspace.createFileSystemWatcher('**/*.{yaml,yml}');
  const onLayerChange = () => {
    layers.invalidate();
    vscode.workspace.textDocuments.forEach(lintIfSql);
    void refresh();
  };
  ctx.subscriptions.push(watcher,
    watcher.onDidCreate(onLayerChange), watcher.onDidChange(onLayerChange),
    watcher.onDidDelete(onLayerChange));

  // --- commands ----------------------------------------------------------
  const cmd = (id: string, fn: () => Promise<void> | void) =>
    ctx.subscriptions.push(vscode.commands.registerCommand(id, async () => {
      try { await fn(); } catch (e) { report(e, out); }
    }));

  cmd('semlayer.showOutput', () => out.show());

  cmd('semlayer.openWalkthrough', async () => {
    await vscode.commands.executeCommand('workbench.action.openWalkthrough',
      `${ctx.extension.id}#semlayer.setup`, false);
  });

  cmd('semlayer.selectLayer', async () => {
    const found = await layers.discover();
    if (!found.length) {
      vscode.window.showInformationMessage('No semantic layer found in this workspace.');
      return;
    }
    const pick = await vscode.window.showQuickPick(
      found.map((l) => ({ label: l.name, description: `${l.tables} tables`,
                          detail: vscode.workspace.asRelativePath(l.uri), layer: l })),
      { title: 'Lint against which semantic layer?' });
    if (!pick) { return; }
    await vscode.workspace.getConfiguration('semlayer')
      .update('layerPath', pick.layer.uri.fsPath, vscode.ConfigurationTarget.Workspace);
  });

  cmd('semlayer.loadLayer', async () => {
    const found = await layers.discover();
    if (found.length) { await vscode.commands.executeCommand('semlayer.selectLayer'); return; }
    vscode.window.showInformationMessage(
      'No layer here yet. Create one from a warehouse, or try the sample.',
      'Create', 'Try the sample').then((a) => {
        if (a === 'Create') { void vscode.commands.executeCommand('semlayer.createLayer'); }
        if (a === 'Try the sample') { void vscode.commands.executeCommand('semlayer.trySample'); }
      });
  });

  cmd('semlayer.createLayer', async () => {
    const source = await vscode.window.showInputBox({
      title: 'Create a semantic layer',
      prompt: 'Warehouse to read. Nothing is written to it.',
      placeHolder: 'duckdb:warehouse.duckdb | snowflake:my_conn | bigquery:my-project | postgresql://…',
      ignoreFocusOut: true,
    });
    if (!source) { return; }
    await infer(source, out, refresh);
  });

  cmd('semlayer.trySample', async () => {
    const root = vscode.workspace.workspaceFolders?.[0];
    if (!root) { vscode.window.showWarningMessage('Open a folder first.'); return; }
    vscode.window.showInformationMessage(
      'The sample warehouse ships with the semlayer source tree, not with this extension. ' +
      'Build it with `python fixtures/build.py`, then point Create at the .duckdb file it writes.',
      'Create from a file').then((a) => {
        if (a === 'Create from a file') { void vscode.commands.executeCommand('semlayer.createLayer'); }
      });
  });

  cmd('semlayer.review', async () => {
    const layer = await layers.active();
    if (!layer) { vscode.window.showInformationMessage('No layer selected.'); return; }
    const term = vscode.window.createTerminal('semlayer review');
    term.show();
    // Review is a conversation with prompts, so it belongs in a terminal
    // rather than behind a webview that would have to reimplement it.
    term.sendText(`semlayer review ${quote(layer.uri.fsPath)}`);
  });

  cmd('semlayer.configureClient', async () => {
    const layer = await layers.active();
    if (!layer) { vscode.window.showInformationMessage('Create a layer first.'); return; }
    const all = mcp.clients();
    const state = await Promise.all(all.map((c) => mcp.isConfigured(c)));
    const pick = await vscode.window.showQuickPick(
      all.map((c, i) => ({
        label: c.label,
        description: state[i] ? 'connected' : '',
        detail: c.file, client: c,
      })),
      { title: 'Connect which MCP client?' });
    if (!pick) { return; }
    await mcp.configure(pick.client, layer.uri);
    await refresh();
    vscode.window.showInformationMessage(
      `${pick.label} now serves ${layer.name}. Restart it to pick the server up.`);
  });

  await refresh();
}

function quote(p: string): string { return /[\s"']/.test(p) ? `"${p}"` : p; }

/**
 * Context keys the walkthrough watches, so its steps tick themselves off as
 * the state they describe becomes true rather than when a button was pressed.
 */
async function setContexts(): Promise<void> {
  const [cli, layer] = await Promise.all([version(), layers.active()]);
  await vscode.commands.executeCommand('setContext', 'semlayer.hasCli', !!cli);
  await vscode.commands.executeCommand('setContext', 'semlayer.hasLayer', !!layer);
}

async function updateStatus(): Promise<void> {
  const layer = await layers.active();
  if (!layer) { status.hide(); return; }
  status.text = `$(database) ${layer.name}`;
  status.tooltip = `semlayer: linting against ${layer.uri.fsPath}`;
  status.show();
}

async function infer(source: string, out: vscode.OutputChannel,
                     refresh: () => Promise<void>): Promise<void> {
  // A run is a job, not a modal: it is minutes on a real warehouse, and it
  // resumes from cache if it is interrupted.
  await vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: `Inferring a semantic layer from ${source}`,
    cancellable: true,
  }, async (_progress, token) => {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const { code, stderr, stdout } = await run(['infer', source], { cwd: root, token });
    out.appendLine(stdout || stderr);
    if (code === 0) {
      layers.invalidate();
      await refresh();
      vscode.window.showInformationMessage('Semantic layer written.', 'Show output')
        .then((a) => { if (a) { out.show(); } });
    } else {
      vscode.window.showErrorMessage('Inference failed. See the semlayer output.', 'Show output')
        .then((a) => { if (a) { out.show(); } });
    }
  });
}

function report(e: unknown, out: vscode.OutputChannel): void {
  if (e instanceof vscode.CancellationError) { return; }
  const msg = e instanceof CliError ? e.message : e instanceof Error ? e.message : String(e);
  out.appendLine(msg);
  if (e instanceof CliError && e.stderr) { out.appendLine(e.stderr); }
  vscode.window.showErrorMessage(`semlayer: ${msg}`, 'Show output')
    .then((a) => { if (a) { out.show(); } });
}

export function deactivate(): void { /* disposables handle it */ }
