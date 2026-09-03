'use strict';

const path = require('path');

const dependencyRoot = path.resolve(process.argv[2]);
const executablePath = process.argv[3];
const fixtureUrl = process.argv[4];
const puppeteer = require(path.join(dependencyRoot, 'puppeteer-core'));

(async () => {
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
  });
  try {
    const page = await browser.newPage();
    await page.goto(fixtureUrl, {waitUntil: 'load', timeout: 30000});
    await page.waitForFunction(
      () => document.querySelector('#result')?.textContent.trim().length > 0,
      {timeout: 30000}
    );
    process.stdout.write(await page.content());
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
