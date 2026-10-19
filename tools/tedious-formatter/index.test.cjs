"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const test = require("node:test");
const vm = require("node:vm");
const acorn = require("acorn");
const ledger = require("./compatibility.json");
const owned = require("./index.cjs");
const azuriteRequire = createRequire(require.resolve("azurite/package.json"));
const tediousRequire = createRequire(azuriteRequire.resolve("tedious"));
const installed = tediousRequire("sprintf-js");
const driverRoot = path.dirname(tediousRequire.resolve("tedious/package.json"));

function inventory(source) {
  const nodes = [];
  const parents = new Map();
  function visit(node, parent) {
    if (!node || typeof node !== "object") return;
    if (node.type) { nodes.push(node); parents.set(node, parent); }
    for (const [key, child] of Object.entries(node)) {
      if (key === "loc") continue;
      if (Array.isArray(child)) child.forEach((entry) => visit(entry, node));
      else if (child && typeof child === "object") visit(child, node);
    }
  }
  visit(acorn.parse(source, { ecmaVersion: "latest" }), null);
  const declarations = nodes.filter((node) => node.type === "VariableDeclarator"
    && node.init?.type === "CallExpression" && node.init.callee.name === "require"
    && node.init.arguments[0]?.value === "sprintf-js");
  const formats = [];
  for (const declaration of declarations) {
    assert.equal(declaration.id.type, "Identifier", "Unreviewed formatter import binding");
    for (const node of nodes.filter((entry) => entry.type === "Identifier" && entry.name === declaration.id.name)) {
      if (node === declaration.id) continue;
      const member = parents.get(node);
      assert.equal(member.type, "MemberExpression", "Unreviewed formatter alias/export");
      assert.equal(member.object, node);
      assert.equal(member.computed, false);
      assert.equal(member.property.name, "sprintf");
      let callee = member;
      if (parents.get(callee).type === "SequenceExpression") {
        const sequence = parents.get(callee);
        assert.equal(sequence.expressions.at(-1), callee);
        callee = sequence;
      }
      const call = parents.get(callee);
      assert.equal(call.type, "CallExpression");
      assert.equal(call.callee, callee);
      assert.equal(call.arguments[0]?.type, "Literal", "Driver formats must remain literal");
      assert.equal(typeof call.arguments[0].value, "string");
      formats.push(call.arguments[0].value);
    }
  }
  // A new import style or inline require must not disappear from the inventory.
  const requires = nodes.filter((node) => node.type === "CallExpression"
    && node.callee.name === "require" && node.arguments[0]?.value === "sprintf-js");
  assert.equal(requires.length, declarations.length, "Unreviewed formatter require");
  return formats;
}

function driverFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? driverFiles(file) : file.endsWith(".js") ? [file] : [];
  });
}

test("installed consumer resolves the owned formatter, not upstream vulnerable code", () => {
  assert.equal(installed, owned);
  assert.equal(tediousRequire("sprintf-js/package.json").name, "@elspeth/tedious-formatter");
  assert.equal(tediousRequire("tedious/package.json").version, "20.0.0");
});

test("inventory catches dynamic formats, aliasing and inline requires", () => {
  assert.deepEqual(inventory('const fmt = require("sprintf-js"); fmt.sprintf("%02X", 1)'), ["%02X"]);
  assert.deepEqual(inventory('const ordinary = "sprintf-js"; ordinary.toString()'), []);
  for (const source of [
    'const fmt = require("sprintf-js"); fmt.sprintf(external, 1)',
    'const fmt = require("sprintf-js"); const alias = fmt.sprintf;',
    'require("sprintf-js").sprintf("%s", "x")',
    'const { sprintf } = require("sprintf-js"); sprintf("%s", "x")',
  ]) assert.throws(() => inventory(source), assert.AssertionError);
});

test("every installed driver formatter call matches the reviewed literal contract", () => {
  const root = path.join(driverRoot, "lib");
  const actual = driverFiles(root).flatMap((file) => inventory(fs.readFileSync(file, "utf8"))
    .map((format) => ({ file: path.relative(root, file), format })));
  assert.deepEqual(actual, ledger.map(({ file, format }) => ({ file, format })));
});

for (const [index, { file, format, args, expected }] of ledger.entries()) {
  test(`driver format ${index + 1} (${file}) retains baseline output`, () => {
    assert.equal(installed.sprintf(format, ...args), expected);
  });
}

test("integer, string and percent behavior preserves driver-compatible edge values", () => {
  assert.equal(installed.sprintf("%d|%d|%d|%d|%d", -1, -0, NaN, Infinity, -Infinity), "-1|0|-NaN|NaN|-NaN");
  assert.equal(installed.sprintf("%d|%d|%d", "12", "1e3", "0xFF"), "12|1|0");
  assert.equal(installed.sprintf("%02X|%04X|%08X", -1, 65536, 4294967296), "FFFFFFFF|10000|00000000");
  assert.equal(installed.sprintf("%s|%s|%%", undefined, "%.101f"), "undefined|%.101f|%");
  assert.equal(installed.sprintf("%d|%s", () => -2, () => "ordinary"), "-2|ordinary");
  assert.throws(() => installed.sprintf("%d", "not numeric"), TypeError);
});

test("unsupported precision and width fail validation before argument side effects", () => {
  let evaluated = false;
  const argument = () => { evaluated = true; return 1; };
  for (const format of ["%f", "%e", "%g", "%.0f", "%.100e", "%.101f", "%.101e", "%.101g", "%.0g", "%.999999999999999999999f",
    "%1$.101f", "%(value).101f", "%*f", "%099999999999999999X", "%09X", "%02d", "%s %.101f"]) {
    assert.throws(() => installed.sprintf(format, argument), (error) => error instanceof TypeError
      && !(error instanceof RangeError) && /Unsupported Tedious format/.test(error.message));
  }
  assert.equal(evaluated, false);
  assert.throws(() => installed.sprintf("x".repeat(4097)), /at most 4096/);
});

test("the installed replacement never invokes native floating precision methods", () => {
  // Isolate the instrument from Node's own test reporter and other consumers.
  const realm = vm.createContext({ module: { exports: {} }, precisionCalls: 0 });
  vm.runInContext(`
    for (const name of ["toFixed", "toExponential", "toPrecision"]) {
      Number.prototype[name] = function () { precisionCalls++; throw new Error("precision sink invoked"); };
      try { (1)[name](1); } catch (error) { if (error.message !== "precision sink invoked") throw error; }
    }
  `, realm);
  assert.equal(realm.precisionCalls, 3); // Positive controls prove all three traps are active.
  realm.precisionCalls = 0;
  vm.runInContext(fs.readFileSync(tediousRequire.resolve("sprintf-js"), "utf8"), realm);
  const instrumented = realm.module.exports.sprintf;
  for (const { format, args, expected } of ledger) assert.equal(instrumented(format, ...args), expected);
  for (const format of ["%.101f", "%.101e", "%.101g"]) {
    assert.throws(() => instrumented(format, 1), (error) => error.name === "TypeError");
  }
  assert.equal(realm.precisionCalls, 0);
});

test("real driver packet, prelogin and login diagnostics render through the replacement", () => {
  const { Packet, TYPE } = tediousRequire("tedious/lib/packet");
  const packet = new Packet(TYPE.SQL_BATCH);
  packet.addData(Buffer.from([0, 255]));
  assert.match(packet.headerToString(), /type:0x01\(SQL_BATCH\).*length:0x000A/);
  assert.equal(packet.dataToString(), "0000  00FF  ..");
  const Prelogin = tediousRequire("tedious/lib/prelogin-payload");
  assert.match(new Prelogin().toString(), /version:0\.0\.0\.0/);
  const Login = tediousRequire("tedious/lib/login7-payload");
  const login = new Login({ tdsVersion: 0x74000004, packetSize: 4096, clientProgVer: 1,
    clientPid: 123, connectionId: 0, clientTimeZone: -60, clientLcid: 1033 });
  assert.match(login.toString(), /TDS:0x74000004.*ClientTimezone:-60/s);
});
