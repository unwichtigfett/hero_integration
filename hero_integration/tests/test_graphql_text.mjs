import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const window = {};
vm.runInNewContext(readFileSync(new URL('../public/js/hero_tools.js', import.meta.url), 'utf8'), {window});
const {parseVariablesText, formatVariablesText, readableResult, resultSummary} = window.hero_workspace_tools.graphqlText;
// The vm realm has its own Object prototype; compare plain JSON shapes.
const plain = (value) => JSON.parse(JSON.stringify(value));

test('variables: numbers, booleans, null and strings keep their JSON types', () => {
  const text = 'id := 5\noffset := 2\n\n  first   :=   25  \nratio := -0.5\nzero := 0\nsearch := "Photovoltaik"\nactive := true\noff := false\ndeleted := null';
  assert.deepEqual(plain(parseVariablesText(text)), {id: 5, offset: 2, first: 25, ratio: -0.5, zero: 0, search: 'Photovoltaik', active: true, off: false, deleted: null});
});

test('variables: a closing semicolon is the standard form and stays optional', () => {
  const withSemicolons = plain(parseVariablesText('id := 5;\noffset := 5;\norderBy := "id";'));
  assert.deepEqual(withSemicolons, {id: 5, offset: 5, orderBy: 'id'});
  assert.deepEqual(plain(parseVariablesText('id := 5\noffset := 5\norderBy := "id"')), withSemicolons);
  assert.deepEqual(plain(parseVariablesText('id := 5;\noffset := 5\nids := [1, 2] ;\nfilter := {"a":"b;"};')),
    {id: 5, offset: 5, ids: [1, 2], filter: {a: 'b;'}});
  // A semicolon inside a string belongs to the value.
  assert.deepEqual(plain(parseVariablesText('text := "a;"\nother := "b;";')), {text: 'a;', other: 'b;'});
  assert.throws(() => parseVariablesText('id := 5;;'), /Line 1: invalid value for "id"/);
  assert.throws(() => parseVariablesText('id := ;'), /Line 1: "id" has no value/);
  assert.throws(() => parseVariablesText('id := 5; offset := 6;'), /Line 1: invalid value for "id"/);
});

test('variables: unicode, escaped quotes and line breaks in strings', () => {
  const values = plain(parseVariablesText('name := "Photovoltaik und Speicher – Müller"\nquote := "say \\"hi\\""\nlines := "a\\nb"'));
  assert.equal(values.name, 'Photovoltaik und Speicher – Müller');
  assert.equal(values.quote, 'say "hi"');
  assert.equal(values.lines, 'a\nb');
});

test('variables: JSON arrays and nested objects', () => {
  assert.deepEqual(plain(parseVariablesText('ids := [5, 7, 9]\nfilter := {"status":"active","year":2026,"tags":["pv",{"x":null}]}')),
    {ids: [5, 7, 9], filter: {status: 'active', year: 2026, tags: ['pv', {x: null}]}});
});

test('variables: clear errors for duplicates, missing operator, invalid values and names', () => {
  assert.throws(() => parseVariablesText('id := 1\nid := 2'), /Line 2: variable "id" is defined more than once/);
  assert.throws(() => parseVariablesText('id = 1'), /Line 1: expected "name := value;"/);
  assert.throws(() => parseVariablesText('search := Photovoltaik'), /Line 1: invalid value for "search". Strings need double quotes/);
  assert.throws(() => parseVariablesText("name := 'single'"), /invalid value/);
  assert.throws(() => parseVariablesText('filter := {"a":}'), /invalid value for "filter"/);
  assert.throws(() => parseVariablesText('$id := 1'), /without "\$"/);
  assert.throws(() => parseVariablesText('1id := 1'), /not a valid GraphQL variable name/);
  assert.throws(() => parseVariablesText('id :='), /has no value/);
  assert.throws(() => parseVariablesText('big := 12345678901234567890'), /too large to keep exactly/);
  assert.throws(() => parseVariablesText('huge := 1e400'), /out of range/);
  // All problems are reported at once, with their line numbers.
  assert.throws(() => parseVariablesText('a := x\n\nb = 1'), (error) => /Line 1/.test(error.message) && /Line 3/.test(error.message));
});

test('variables: no code is evaluated', () => {
  const marker = 'globalThis.__heroEval = 1';
  assert.throws(() => parseVariablesText(`x := ${marker}`), /invalid value/);
  assert.equal(globalThis.__heroEval, undefined);
  assert.throws(() => parseVariablesText('x := (() => 1)()'), /invalid value/);
});

test('variables: existing JSON is accepted and converted to the line format and back', () => {
  const legacy = '{\n  "id": "7678",\n  "offset": 2,\n  "filter": {"status": "active"}\n}';
  const parsed = parseVariablesText(legacy);
  assert.deepEqual(plain(parsed), {id: '7678', offset: 2, filter: {status: 'active'}});
  const lines = formatVariablesText(parsed);
  assert.equal(lines, 'id := "7678";\noffset := 2;\nfilter := {"status":"active"};');
  assert.deepEqual(plain(parseVariablesText(lines)), plain(parsed));
  assert.equal(formatVariablesText({}), '');
  assert.deepEqual(plain(parseVariablesText('')), {});
  assert.throws(() => parseVariablesText('[1, 2]'), /expected "name := value;"/);
  assert.throws(() => parseVariablesText('{"bad name": 1}'), /not a valid GraphQL variable name/);
});

test('result: scalars use field = value with exact types', () => {
  assert.equal(readableResult({project_id: 7678, name: 'Photovoltaik und Speicher', power_kwp: 14.5, active: true, deleted_at: null}),
    'project_id = 7678\nname = "Photovoltaik und Speicher"\npower_kwp = 14.5\nactive = true\ndeleted_at = null');
  // A string that looks like a number stays a string.
  assert.equal(readableResult({project_id: '7678'}), 'project_id = "7678"');
  assert.equal(readableResult({flag: 'true', none: 'null'}), 'flag = "true"\nnone = "null"');
});

test('result: nested objects and lists keep order, aliases and indices', () => {
  const data = {projects: [
    {project_id: 7678, name: 'Photovoltaik und Speicher', customer: {name: 'Max Mustermann'}, power_kwp: 14.5},
    {project_id: 7679, name: 'Photovoltaik Erweiterung'},
  ]};
  assert.equal(readableResult(data), [
    'projects[0].project_id = 7678', 'projects[0].name = "Photovoltaik und Speicher"', 'projects[0].customer.name = "Max Mustermann"',
    'projects[0].power_kwp = 14.5', '', 'projects[1].project_id = 7679', 'projects[1].name = "Photovoltaik Erweiterung"',
  ].join('\n'));
  assert.equal(readableResult({myAlias: {id: 1}}), 'myAlias.id = 1');
  assert.equal(readableResult({ids: [3, 1, 2]}), 'ids[0] = 3\nids[1] = 1\nids[2] = 2');
});

test('result: empty collections, null and escaping are visible', () => {
  assert.equal(readableResult({items: [], meta: {}, owner: null}), 'items = []\nmeta = {}\nowner = null');
  assert.equal(readableResult({text: 'He said "hi"\nnext – Ü'}), 'text = "He said \\"hi\\"\\nnext – Ü"');
  assert.equal(readableResult({'odd key': 1}), '["odd key"] = 1');
  assert.equal(readableResult({}), '(root) = {}');
});

test('result: summary counts delivered top-level values without changing them', () => {
  const data = {projects: [{id: 1}, {id: 2}], customer: {id: 3}, missing: null, version: '1'};
  const before = JSON.stringify(data);
  assert.deepEqual(plain(resultSummary(data)), ['projects: 2 items', 'customer: object', 'missing: null', 'version: value']);
  readableResult(data);
  assert.equal(JSON.stringify(data), before, 'formatting has no side effect on the result');
});
