import assert from 'node:assert/strict';
import { mkdir, stat, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

// Browser E2E contract and failure inventory: docs-understand-failures.md.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const artifactDir = resolve(root, process.env.DOCS_ARTIFACT_DIR || 'tmp/documentation-e2e');
const url = pathToFileURL(resolve(root, 'docs/understand.html')).href;
await mkdir(artifactDir, { recursive: true });
const report = { guide: url, checks: [], errors: [], externalRequests: [], artifacts: [] };
const browser = await chromium.launch({
  executablePath: process.env.BROWSER_EXECUTABLE || '/usr/bin/google-chrome',
  headless: true,
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, offline: true });
await context.tracing.start({ screenshots: true, snapshots: true });
const page = await context.newPage();
page.on('pageerror', error => report.errors.push(error.message));
page.on('request', request => { if (/^https?:/.test(request.url())) report.externalRequests.push(request.url()); });
async function check(name, action) { await action(); report.checks.push({ name, passed: true }); }
async function screenshot(name) {
  await page.screenshot({ path: resolve(artifactDir, name), fullPage: true });
  report.artifacts.push(name);
}
try {
  await page.goto(url);
  await check('Offline default shows both environments and all guide links', async () => {
    assert(await page.getByRole('heading', { name: 'I run a production server' }).isVisible());
    assert(await page.getByRole('link', { name: 'Open the source quick start' }).isVisible());
    assert.equal(await page.locator('#guide-list li:visible').count(), 9);
  });
  await check('Every local link resolves to a file or a page fragment', async () => {
    const links = await page.locator('a').evaluateAll(elements => elements.map(element => element.href));
    for (const link of links) {
      const target = new URL(link);
      assert.equal(target.protocol, 'file:', `Unexpected external link: ${link}`);
      const fragment = target.hash.slice(1);
      target.hash = '';
      assert((await stat(fileURLToPath(target))).isFile(), `Missing file: ${link}`);
      if (target.href === url && fragment) assert.equal(await page.locator(`[id="${fragment}"]`).count(), 1);
    }
  });
  await check('Each stage reveals its explanation and source links', async () => {
    for (const stage of ['record', 'upload', 'verify', 'process', 'query', 'export']) {
      const details = page.locator(`#${stage}`);
      await details.locator('summary').click();
      assert.equal(await details.getAttribute('open'), '');
      assert(await details.locator('.stage-body').isVisible());
      assert(await details.getByRole('link').count() > 0);
      await details.locator('summary').click();
    }
  });
  await check('Keyboard navigation opens the linked upload stage', async () => {
    const link = page.locator('.flow').getByRole('link', { name: 'Upload', exact: true });
    await link.focus();
    await link.press('Enter');
    await page.locator('#upload[open]').waitFor({ state: 'attached' });
    assert.equal(await page.locator('#upload').getAttribute('open'), '');
  });
  await check('Reader filters keep shared guides and hide irrelevant guides', async () => {
    const operator = page.getByRole('button', { name: 'Operators', exact: true });
    await operator.focus();
    await operator.press('Enter');
    assert.equal(await operator.getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#guide-list li:visible').count(), 4);
    assert(await page.getByRole('link', { name: 'Operate a source release deployment', exact: true }).isVisible());
    assert.equal(await page.getByRole('link', { name: 'Start the development stack', exact: true }).isVisible(), false);
    await page.getByRole('button', { name: 'Developers', exact: true }).click();
    assert.equal(await page.locator('#guide-list li:visible').count(), 9);
  });
  await check('Search combines with the reader filter and recovers from empty results', async () => {
    await page.getByLabel('Search guides', { exact: true }).fill('backup');
    assert.equal(await page.locator('#guide-list li:visible').count(), 1);
    await page.getByLabel('Search guides', { exact: true }).fill('no-such-guide-123');
    assert(await page.locator('#no-results').isVisible());
    assert.equal(await page.locator('#guide-list li:visible').count(), 0);
    await page.getByLabel('Search guides', { exact: true }).fill('');
    await page.getByRole('button', { name: 'Everyone', exact: true }).click();
    assert.equal(await page.locator('#guide-list li:visible').count(), 9);
    assert.equal(await page.locator('#no-results').isVisible(), false);
  });
  await page.goto(url);
  await screenshot('desktop.png');
  await page.setViewportSize({ width: 390, height: 844 });
  await check('Mobile has no horizontal overflow and usable disclosures', async () => {
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert(fits, 'Guide overflows mobile viewport');
    await page.locator('#export summary').click();
    assert(await page.locator('#export .stage-body').isVisible());
    await page.getByRole('button', { name: 'Operators', exact: true }).click();
    assert.equal(await page.locator('#guide-list li:visible').count(), 4);
  });
  await page.goto(url);
  await screenshot('mobile.png');
  const plainContext = await browser.newContext({ javaScriptEnabled: false, offline: true });
  try {
    const plain = await plainContext.newPage();
    await plain.goto(url);
    await check('Without JavaScript all guides and native disclosures remain usable', async () => {
      assert.equal(await plain.locator('#guide-list li:visible').count(), 9);
      assert.equal(await plain.locator('#tools').isVisible(), false);
      await plain.locator('#process summary').click();
      assert(await plain.locator('#process .stage-body').isVisible());
    });
    await plain.screenshot({ path: resolve(artifactDir, 'no-javascript.png'), fullPage: true });
    report.artifacts.push('no-javascript.png');
  } finally { await plainContext.close(); }
  assert.deepEqual(report.errors, [], 'Browser errors');
  assert.deepEqual(report.externalRequests, [], 'Guide made network requests');
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = error.stack;
  process.exitCode = 1;
} finally {
  await context.tracing.stop({ path: resolve(artifactDir, 'trace.zip') });
  report.artifacts.push('trace.zip');
  await browser.close();
  await writeFile(resolve(artifactDir, 'report.json'), JSON.stringify(report, null, 2) + '\n');
}
console.log(`${report.passed ? 'PASS' : 'FAIL'}: ${report.checks.length} documentation browser checks. Evidence: ${artifactDir}`);
if (!report.passed) console.error(report.failure);
