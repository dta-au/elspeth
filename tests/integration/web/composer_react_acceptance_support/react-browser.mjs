import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ headless: true });
const deadline = setTimeout(() => void browser.close(), 45000);
const state = page => page.evaluate(() => window.reactComposerAcceptance.state());
async function wait(read, predicate, name) {
  const until = Date.now() + 12000;
  while (Date.now() < until) { const value = await read(); if (predicate(value)) return value; await new Promise(resolve => setTimeout(resolve, 25)); }
  throw new Error('Timed out: ' + name);
}
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  await context.route('**/*', route => new URL(route.request().url()).origin === config.baseUrl ? route.continue() : route.abort('blockedbyclient'));
  const page = await context.newPage();
  await page.goto(config.baseUrl + '/docs');
  await page.addScriptTag({ url: config.baseUrl + '/__acceptance/react.js' });
  await page.evaluate(({ tokenA, sessionA }) => window.reactComposerAcceptance.mount(tokenA, sessionA), config);
  await page.getByRole('textbox', { name: 'Message input', exact: true }).waitFor();
  await page.evaluate(({ scenario, sessionA }) => window.reactComposerAcceptance.hold(scenario === 'principal-late-admission' ? 'admission' : 'terminal', sessionA), config);
  await page.getByRole('textbox', { name: 'Message input', exact: true }).fill('First actor request');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  if (config.scenario === 'session-late-terminal') {
    await page.evaluate(async () => { const response = await fetch('/__acceptance/react-control/release', { method: 'POST', headers: { Authorization: `Bearer ${localStorage.getItem('auth_token')}` } }); if (!response.ok) throw new Error('Release denied'); });
  }
  const first = await wait(() => state(page), x => x.held !== null, 'actual response held before old store publication');
  await page.evaluate(({ tokenB, sessionB }) => window.reactComposerAcceptance.replace(tokenB, sessionB), config);
  const current = await state(page);
  assert.equal(current.activeSessionId, config.sessionB);
  assert.equal(current.principalId, config.principalB);
  assert.equal(current.loaded, true);
  await page.getByRole('textbox', { name: 'Message input', exact: true }).fill('Winner actor request');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await page.evaluate(async () => { const response = await fetch('/__acceptance/react-control/release', { method: 'POST', headers: { Authorization: `Bearer ${localStorage.getItem('auth_token')}` } }); if (!response.ok) throw new Error('Release denied'); });
  await wait(() => state(page), x => !x.isComposing && x.messages.some(m => m.content === 'Completed delayed response.'), 'current actual composer result rendered');
  await page.getByRole('log', { name: 'Conversation', exact: true }).getByText('Winner actor request', { exact: true }).waitFor();
  assert.equal(await page.getByRole('log', { name: 'Conversation', exact: true }).getByText('First actor request', { exact: true }).count(), 0);
  await page.evaluate(() => window.reactComposerAcceptance.release());
  await page.evaluate(() => window.reactComposerAcceptance.joinOriginalAction());
  // The actual original hook action has settled before DOM/store checks.
  await wait(() => state(page), x => x.activeSessionId === config.sessionB && x.loaded && !x.isComposing, 'current generation remains selected');
  const after = await state(page);
  assert.equal(after.principalId, config.principalB);
  assert.equal(after.scope.principalId, config.principalB);
  assert.ok(after.messages.every(m => m.sessionId === config.sessionB));
  assert.ok(after.messages.every(m => m.id !== first.held.messageId));
  assert.equal(after.requests.filter(r => r.method === 'POST' && r.path.endsWith('/messages')).length, 2);
  assert.equal(after.requests.filter(r => r.method === 'POST' && r.operationId === first.held.operationId).length, 1);
  assert.equal(await page.getByRole('log', { name: 'Conversation', exact: true }).getByText('First actor request', { exact: true }).count(), 0);
  process.stdout.write(JSON.stringify({ scenario: config.scenario, first, after }) + '\n');
  await context.close();
} finally { clearTimeout(deadline); await browser.close(); }
