import fs from 'node:fs';
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';

const TEST_ID = 'WPC-99049-LINK-COLOR';
const SCOPE_ID = 'WPC-99049-P2P-COLOR-SCOPE';
const cssPath = process.argv[2] || '/tmp/domain-forwarding-production.css';
const css = fs.readFileSync(cssPath, 'utf8');
assert.ok(css.length > 0, `[${TEST_ID}] compiled stylesheet must not be empty`);

const expected = 'rgb(17, 34, 51)';
const browser = await puppeteer.launch({
  headless: true,
  executablePath: process.env.CHROME_BIN || '/usr/bin/chromium',
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
});

try {
  const page = await browser.newPage();
  await page.setContent(`
    <style>
      :root { --color-link: ${expected}; }
      ${css}
    </style>
    <div class="domains-overview">
      <main class="hosting-dashboard-item-view__content">
        <div>
          <section class="domain-forwarding-card__accordion">
            <button id="target" class="link-button">+ Add forward</button>
          </section>
          <button id="outside" class="link-button">Unrelated action</button>
        </div>
      </main>
    </div>
  `);
  const colors = await page.evaluate(() => ({
    target: getComputedStyle(document.querySelector('#target')).color,
    outside: getComputedStyle(document.querySelector('#outside')).color,
  }));
  let failed = false;
  if (colors.target === expected) {
    console.log(`PASS [${TEST_ID}] computed color ${colors.target}`);
  } else {
    console.error(`FAIL [${TEST_ID}] expected ${expected}, received ${colors.target}`);
    failed = true;
  }
  if (colors.outside !== expected) {
    console.log(`PASS [${SCOPE_ID}] unrelated link-button remains ${colors.outside}`);
  } else {
    console.error(`FAIL [${SCOPE_ID}] color rule leaked outside the Domain Forwarding accordion`);
    failed = true;
  }
  if (failed) process.exitCode = 1;
} finally {
  await browser.close();
}
