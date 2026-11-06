/**
 * Reconstructing source positions for lint findings.
 *
 * A finding carries rule, severity, message, table, column and fix -- and no
 * source offset. These two functions put it back: `locate` finds where each
 * linted statement sits in the document (the engine echoes the SQL it linted,
 * so the common case is an exact search), and `rangeFor` finds the identifier
 * inside it.
 *
 * Kept apart from `diagnostics.ts` and free of any `vscode` import so it can
 * be tested with `node --test`, which is also how the qualified-column bug
 * below stays fixed.
 */

export interface Finding {
  rule: string;
  severity: 'error' | 'warning' | 'info';
  message: string;
  table: string | null;
  column: string | null;
  fix: string | null;
}

export interface StatementResult {
  sql: string;
  ok: boolean;
  findings: Finding[];
  errors: number;
  warnings: number;
}

export interface Span { offset: number; length: number; how: 'qualified' | 'column' | 'table' | 'statement' }

/**
 * Identifier match that will not fire inside a longer word.
 *
 * The lookbehind deliberately allows a dot: a finding on `tot_amt` has to be
 * able to match inside `ord_hdr.tot_amt`, which is how columns are usually
 * written. Excluding `.` was the first version, and it meant every qualified
 * column fell silently through to underlining the table instead.
 */
export function findIdentifier(haystack: string, needle: string, from = 0): number {
  const re = new RegExp(`(?<!\\w)${needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?!\\w)`, 'i');
  const m = re.exec(haystack.slice(from));
  return m ? from + m.index : -1;
}

/**
 * Where each linted statement starts in `text`.
 *
 * `cursor` only moves forward, so two identical statements map to their own
 * occurrences rather than both to the first.
 */
export function locate(text: string, statements: Pick<StatementResult, 'sql'>[]): number[] {
  const out: number[] = [];
  let cursor = 0;
  for (const s of statements) {
    const sql = s.sql.trim();
    let at = sql ? text.indexOf(sql, cursor) : -1;
    if (at < 0 && sql) {
      // Whitespace was normalised somewhere: fall back to the first token.
      const head = sql.split(/\s+/)[0];
      at = head ? findIdentifier(text, head, cursor) : -1;
    }
    if (at < 0) { at = cursor; }
    out.push(at);
    cursor = at + Math.max(1, sql.length);
  }
  return out;
}

/** The span to underline, relative to the start of `stmtText`. */
export function rangeFor(stmtText: string, f: Pick<Finding, 'table' | 'column'>): Span {
  // Most specific first. `table.column` is tried before a bare column so the
  // right `tot_amt` is underlined when several tables have one, and the span
  // still covers only the column half of the qualified name.
  if (f.table && f.column) {
    const i = findIdentifier(stmtText, `${f.table}.${f.column}`);
    if (i >= 0) {
      return { offset: i + f.table.length + 1, length: f.column.length, how: 'qualified' };
    }
  }
  if (f.column) {
    const i = findIdentifier(stmtText, f.column);
    if (i >= 0) { return { offset: i, length: f.column.length, how: 'column' }; }
  }
  if (f.table) {
    const i = findIdentifier(stmtText, f.table);
    if (i >= 0) { return { offset: i, length: f.table.length, how: 'table' }; }
  }
  // Nothing to anchor to: underline the statement's first line rather than
  // inventing a narrower range.
  const nl = stmtText.search(/\r?\n/);
  return { offset: 0, length: nl < 0 ? stmtText.length : nl, how: 'statement' };
}
