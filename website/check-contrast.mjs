#!/usr/bin/env node
// Check the opaque text/background roles used by the public site in both themes.
// Browser accessibility checks cover the composed page and interactive states.
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("tokens/colors.css", import.meta.url), "utf8");
function blockVars(source, selector) {
  const escaped = selector.replace(/[[\]"^$.*+?()|{}\\]/g, "\\$&");
  const match = source.match(new RegExp(escaped + "\\s*\\{([^}]*)\\}"));
  if (!match) throw new Error(`Missing token block: ${selector}`);
  return Object.fromEntries(
    [...match[1].matchAll(/(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;/g)]
      .map((pair) => [pair[1], pair[2]]),
  );
}
function luminance(hex) {
  if (!/^#[0-9a-f]{6}$/i.test(hex)) throw new Error(`Invalid colour: ${hex}`);
  const [r, g, b] = [1, 3, 5].map((offset) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function ratio(fg, bg) {
  const values = [luminance(fg), luminance(bg)];
  return (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05);
}
// Known positive and negative controls prevent a broken instrument passing.
if (ratio("#000000", "#ffffff") !== 21 || ratio("#ffffff", "#ffffff") !== 1) {
  throw new Error("Contrast controls failed");
}
const parsed = blockVars(":root { --sample: #123456; }", ":root");
if (parsed["--sample"] !== "#123456") throw new Error("Token parser control failed");
let missingRejected = false;
try { blockVars(":root {}", "absent"); } catch { missingRejected = true; }
if (!missingRejected) throw new Error("Missing-block control failed");

let failures = 0;
let count = 0;
for (const [theme, selector] of [["dark", ":root"], ["light", '[data-theme="light"]']]) {
  const tokens = blockVars(css, selector);
  const pairs = [];
  for (const bg of ["bg", "surface", "surface-elevated", "surface-nav", "surface-paper"]) {
    for (const fg of ["text", "text-secondary", "text-muted", "link"]) {
      pairs.push([fg, bg]);
    }
  }
  pairs.push(["text-inverse", "btn-primary-bg"], ["text-inverse", "btn-primary-bg-hover"]);
  for (const [fg, bg] of pairs) {
    const value = ratio(tokens[`--color-${fg}`], tokens[`--color-${bg}`]);
    const passed = value >= 4.5;
    failures += passed ? 0 : 1;
    count++;
    console.log(`${passed ? "PASS" : "FAIL"} ${theme} ${fg} on ${bg}: ${value.toFixed(2)}:1`);
  }
}
console.log(`${count} pairs checked; ${failures} below 4.5:1. Controls passed.`);
if (failures) process.exitCode = 1;
