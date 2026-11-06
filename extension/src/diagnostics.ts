/**
 * SQL diagnostics from `semlayer lint --json`.
 *
 * This is the one thing the extension does that a terminal cannot: a finding
 * is only useful at the moment you are writing the line.
 *
 * Positions are reconstructed here, not reported by the engine. A finding
 * carries rule/severity/message/table/column/fix and no source offset, so we
 * locate the statement by the SQL the engine echoes back, then the column or
 * table identifier inside it. Where that fails the whole statement is
 * underlined rather than guessing a narrower range.
 */
import * as vscode from 'vscode';
import { run } from './cli';
import * as layers from './layer';
import { locate, rangeFor, Finding, StatementResult } from './positions';

/** Held back while the user is mid-keystroke: half-written SQL never parses. */
const WHILE_TYPING_SUPPRESSED = new Set(['parse_error']);

const SEVERITY: Record<string, vscode.DiagnosticSeverity> = {
  error: vscode.DiagnosticSeverity.Error,
  warning: vscode.DiagnosticSeverity.Warning,
  info: vscode.DiagnosticSeverity.Information,
};

function toRange(doc: vscode.TextDocument, stmtStart: number, stmtText: string,
                 f: Finding): vscode.Range {
  const span = rangeFor(stmtText, f);
  return new vscode.Range(doc.positionAt(stmtStart + span.offset),
                          doc.positionAt(stmtStart + span.offset + span.length));
}

export class Linter {
  private readonly collection = vscode.languages.createDiagnosticCollection('semlayer');
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private readonly inflight = new Map<string, vscode.CancellationTokenSource>();

  constructor(private readonly out: vscode.OutputChannel) {}

  dispose(): void {
    this.collection.dispose();
    this.timers.forEach(clearTimeout);
    this.inflight.forEach((c) => c.cancel());
  }

  clear(doc: vscode.TextDocument): void { this.collection.delete(doc.uri); }

  /** Save and open: every rule. */
  settled(doc: vscode.TextDocument): void { void this.lint(doc, false); }

  /** Keystroke: debounced, and incomplete syntax stays quiet. */
  typing(doc: vscode.TextDocument): void {
    const cfg = vscode.workspace.getConfiguration('semlayer');
    if (!cfg.get<boolean>('lint.onType', true)) { return; }
    const key = doc.uri.toString();
    clearTimeout(this.timers.get(key));
    this.timers.set(key, setTimeout(
      () => void this.lint(doc, true),
      cfg.get<number>('lint.debounceMs', 500)));
  }

  private async lint(doc: vscode.TextDocument, whileTyping: boolean): Promise<void> {
    const cfg = vscode.workspace.getConfiguration('semlayer');
    if (!cfg.get<boolean>('lint.enable', true) || doc.languageId !== 'sql') { return; }

    const layer = await layers.active();
    if (!layer) { this.collection.delete(doc.uri); return; }

    const key = doc.uri.toString();
    this.inflight.get(key)?.cancel();
    const cancel = new vscode.CancellationTokenSource();
    this.inflight.set(key, cancel);

    const text = doc.getText();
    if (!text.trim()) { this.collection.delete(doc.uri); return; }

    let parsed: StatementResult[];
    try {
      const { stdout, stderr } = await run(
        ['lint', layer.uri.fsPath, '-', '--json',
         '--dialect', cfg.get<string>('dialect', 'duckdb')],
        { stdin: text, timeoutMs: 15_000, token: cancel.token });
      if (!stdout.trim()) {
        if (stderr.trim()) { this.out.appendLine(`lint: ${stderr.trim()}`); }
        return;
      }
      parsed = JSON.parse(stdout) as StatementResult[];
    } catch (e) {
      if (e instanceof vscode.CancellationError) { return; }
      this.out.appendLine(`lint failed: ${e instanceof Error ? e.message : String(e)}`);
      return;
    } finally {
      if (this.inflight.get(key) === cancel) { this.inflight.delete(key); }
    }

    const starts = locate(text, parsed);
    const diagnostics: vscode.Diagnostic[] = [];

    parsed.forEach((stmt, i) => {
      for (const f of stmt.findings) {
        if (whileTyping && WHILE_TYPING_SUPPRESSED.has(f.rule)) { continue; }
        const d = new vscode.Diagnostic(
          toRange(doc, starts[i], stmt.sql, f),
          f.fix ? `${f.message}\n\nfix: ${f.fix}` : f.message,
          SEVERITY[f.severity] ?? vscode.DiagnosticSeverity.Warning);
        d.source = 'semlayer';
        d.code = f.rule;
        diagnostics.push(d);
      }
    });

    this.collection.set(doc.uri, diagnostics);
  }
}
