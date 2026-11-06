/**
 * The sidebar view.
 *
 * Three states, and which one you see is decided by what exists rather than by
 * where you are in a wizard:
 *
 *   no CLI     -> install it, and nothing else
 *   no layer   -> the walkthrough: connect, create, query
 *   a layer    -> the resting state: what is served, what drifted, what is open
 *
 * The walkthrough removes itself. A numbered "step 1 of 3" that survives to day
 * 30 is a wizard nobody can dismiss.
 */
import * as vscode from 'vscode';
import * as layers from './layer';
import * as mcp from './mcp';
import { version } from './cli';

interface State {
  cli: string | null;
  layer: layers.Layer | undefined;
  others: number;
  clients: { label: string; configured: boolean }[];
}

export class Panel implements vscode.WebviewViewProvider {
  public static readonly viewId = 'semlayer.panel';
  private view?: vscode.WebviewView;

  constructor(private readonly ctx: vscode.ExtensionContext) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [this.ctx.extensionUri] };
    view.webview.onDidReceiveMessage((m: { command: string }) => {
      if (typeof m?.command === 'string') { void vscode.commands.executeCommand(m.command); }
    });
    void this.refresh();
  }

  async refresh(): Promise<void> {
    if (!this.view) { return; }
    const [cli, layer, all] = await Promise.all([version(), layers.active(), layers.discover(5)]);
    const clients = await Promise.all(mcp.clients().map(async (c) => (
      { label: c.label, configured: await mcp.isConfigured(c) })));
    this.view.webview.html = render({ cli, layer, others: Math.max(0, all.length - 1), clients });
  }
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));

function render(s: State): string {
  const body = !s.cli ? noCli() : s.layer ? resting(s) : walkthrough(s);
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  body{font:13px/1.4 var(--vscode-font-family);color:var(--vscode-foreground);
       background:var(--vscode-sideBar-background);padding:0;margin:0}
  .pad{padding:10px 12px}
  h3{font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;
     color:var(--vscode-foreground);opacity:.75;margin:14px 0 6px;padding:0 12px}
  p{margin:6px 0;color:var(--vscode-descriptionForeground)}
  button{width:100%;padding:5px 10px;border:0;border-radius:2px;cursor:pointer;
         background:var(--vscode-button-background);color:var(--vscode-button-foreground);
         font:inherit;margin-top:8px}
  button:hover{background:var(--vscode-button-hoverBackground)}
  button.sec{background:var(--vscode-button-secondaryBackground);
             color:var(--vscode-button-secondaryForeground)}
  .row{display:flex;gap:7px;align-items:baseline;padding:4px 12px;cursor:pointer}
  .row:hover{background:var(--vscode-list-hoverBackground)}
  .row .t{flex:1;min-width:0}
  .dim{color:var(--vscode-descriptionForeground);font-size:12px}
  .mark{width:13px;flex:none;opacity:.8}
  .ok{color:var(--vscode-charts-green)}
  .warn{color:var(--vscode-editorWarning-foreground)}
  code{font-family:var(--vscode-editor-font-family);font-size:12px}
  a{color:var(--vscode-textLink-foreground);cursor:pointer;text-decoration:none}
</style></head><body>${body}
<script>
  const vs = acquireVsCodeApi();
  document.querySelectorAll('[data-cmd]').forEach((el) => {
    el.addEventListener('click', () => vs.postMessage({ command: el.dataset.cmd }));
  });
</script></body></html>`;
}

function noCli(): string {
  return `<div class="pad">
    <p>The <code>semlayer</code> engine was not found on your PATH.</p>
    <p class="dim">The extension runs the same CLI you would run in a terminal; it does not
    carry an engine of its own.</p>
    <p><code>pipx install semlayer</code></p>
    <p class="dim">Already installed somewhere else? Set <code>semlayer.cliPath</code>.</p>
    <button class="sec" data-cmd="semlayer.showOutput">Show output</button>
  </div>`;
}

function walkthrough(s: State): string {
  return `<div class="pad">
    <p>semlayer reads a warehouse it can already reach and writes one YAML file
    your agent loads over MCP.</p>
  </div>
  <h3>Get started</h3>
  <div class="row" data-cmd="semlayer.loadLayer">
    <span class="mark">&#9675;</span>
    <span class="t">Connect to a warehouse<br><span class="dim">reads connections already on this machine</span></span>
  </div>
  <div class="row" data-cmd="semlayer.createLayer">
    <span class="mark">&#9675;</span>
    <span class="t">Create the semantic layer<br><span class="dim">the cost is shown before anything runs</span></span>
  </div>
  <div class="row" data-cmd="semlayer.configureClient">
    <span class="mark">&#9675;</span>
    <span class="t">Query your warehouse<br><span class="dim">serve the layer to an agent over MCP</span></span>
  </div>
  <div class="pad">
    <button data-cmd="semlayer.createLayer">Create semantic layer</button>
    <button class="sec" data-cmd="semlayer.trySample">Try the sample warehouse</button>
    <p class="dim">36 tables, no credentials, no API key.</p>
  </div>`;
}

function resting(s: State): string {
  const l = s.layer!;
  const connected = s.clients.filter((c) => c.configured).map((c) => c.label);
  return `<div class="row" data-cmd="semlayer.selectLayer">
    <span class="mark ok">&#10003;</span>
    <span class="t"><b>${esc(l.name)}</b><br><span class="dim">${l.tables} tables${
      s.others ? ` &middot; ${s.others} other layer${s.others > 1 ? 's' : ''} here` : ''}</span></span>
  </div>
  <h3>Serving</h3>
  <div class="pad" style="padding-top:0">
    <p class="dim">${connected.length
      ? `${esc(connected.join(', '))} &mdash; connected`
      : 'No MCP client is configured yet.'}</p>
    <button class="sec" data-cmd="semlayer.configureClient">Connect an MCP client</button>
  </div>
  <h3>Findings</h3>
  <div class="row" data-cmd="semlayer.review">
    <span class="mark warn">&#9679;</span>
    <span class="t">Review findings<br><span class="dim">nothing routes you here; the layer works with every question open</span></span>
  </div>
  <h3>Keep it fresh</h3>
  <div class="pad" style="padding-top:0">
    <button class="sec" data-cmd="semlayer.createLayer">Re-infer</button>
  </div>`;
}
