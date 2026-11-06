/**
 * Tests for source-position reconstruction.
 *
 * The fixtures are real `semlayer lint --json` findings against the messy_mart
 * layer, including the two that caught a bug: a qualified column must underline
 * the column, not the table.
 *
 * Run with `npm test`. No test framework -- node:test is built in, and this
 * package has no runtime dependencies to keep.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findIdentifier, locate, rangeFor } from '../out/positions.js';

const SQL = `-- quarterly revenue for the board deck
SELECT date_trunc('quarter', ord_hdr.ord_dt) AS period,
       SUM(ord_hdr.tot_amt) AS revenue,
       COUNT(DISTINCT ord_ln.ln_id) AS order_lines
FROM ord_hdr
JOIN ord_ln ON ord_ln.ord_id = ord_hdr.ord_id
WHERE ord_hdr.ord_dt >= '2025-10-01'
GROUP BY 1;

SELECT COUNT(*) FROM cust_mstr WHERE custid = 1;
`;
const STMT1 = SQL.slice(SQL.indexOf('--'), SQL.indexOf('GROUP BY 1') + 'GROUP BY 1'.length);
const STMT2 = 'SELECT COUNT(*) FROM cust_mstr WHERE custid = 1';

const underlines = (stmt, f) => {
  const s = rangeFor(stmt, f);
  return stmt.slice(s.offset, s.offset + s.length);
};

test('qualified column underlines the column, not the table', () => {
  // The regression: a lookbehind excluding `.` made this return `ord_hdr`.
  const f = { table: 'ord_hdr', column: 'tot_amt' };
  assert.equal(underlines(STMT1, f), 'tot_amt');
  assert.equal(rangeFor(STMT1, f).how, 'qualified');
});

test('qualified column picks its own table, not the first match', () => {
  assert.equal(underlines(STMT1, { table: 'ord_ln', column: 'ln_id' }), 'ln_id');
});

test('a table-only finding underlines the table', () => {
  const f = { table: 'ord_hdr', column: null };     // missing_required_filter
  assert.equal(underlines(STMT1, f), 'ord_hdr');
  assert.equal(rangeFor(STMT1, f).how, 'table');
});

test('a column-only finding underlines the column', () => {
  const f = { table: null, column: 'custid' };      // unknown_column, unqualified
  assert.equal(underlines(STMT2, f), 'custid');
  assert.equal(rangeFor(STMT2, f).how, 'column');
});

test('an identifier that is not present falls back to the first line', () => {
  const s = rangeFor(STMT1, { table: 'nowhere', column: 'missing' });
  assert.equal(s.how, 'statement');
  assert.equal(s.offset, 0);
  assert.equal(STMT1.slice(0, s.length), '-- quarterly revenue for the board deck');
});

test('identifiers do not match inside longer words', () => {
  assert.equal(findIdentifier('select ord_hdr_legacy from x', 'ord_hdr'), -1);
  assert.equal(findIdentifier('select ord_hdr from x', 'ord_hdr'), 7);
});

test('matching is case-insensitive', () => {
  assert.equal(underlines('SELECT SUM(ORD_HDR.TOT_AMT) FROM ORD_HDR',
                          { table: 'ord_hdr', column: 'tot_amt' }), 'TOT_AMT');
});

test('locate maps statements to their own offsets', () => {
  const offsets = locate(SQL, [{ sql: STMT1 }, { sql: STMT2 }]);
  assert.equal(offsets.length, 2);
  assert.ok(SQL.startsWith(STMT1.slice(0, 20), offsets[0]));
  assert.ok(SQL.startsWith(STMT2, offsets[1]));
  assert.ok(offsets[1] > offsets[0]);
});

test('identical statements map to separate occurrences', () => {
  const text = 'select 1 from t;\nselect 1 from t;\n';
  const [a, b] = locate(text, [{ sql: 'select 1 from t' }, { sql: 'select 1 from t' }]);
  assert.equal(a, 0);
  assert.equal(b, 17);
});

test('an unlocatable statement still yields a usable offset', () => {
  const [a] = locate('completely different text', [{ sql: 'select 1' }]);
  assert.equal(a, 0);
});
