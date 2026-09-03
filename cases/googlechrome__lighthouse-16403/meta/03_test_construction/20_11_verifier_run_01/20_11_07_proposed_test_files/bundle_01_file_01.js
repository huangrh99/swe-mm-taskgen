/**
 * @license
 * Copyright 2024 Google LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import puppeteer from 'puppeteer';
import {getChromePath} from 'chrome-launcher';

import {Server} from '../cli/test/fixtures/static-server.js';

const portNumber = 20204;
const treemapUrl = `http://localhost:${portNumber}/dist/gh-pages/treemap/index.html`;

describe('Lighthouse Treemap Visual Contract', () => {
  /** @type {import('puppeteer').Browser} */
  let browser;
  /** @type {import('puppeteer').Page} */
  let page;
  let server;

  before(async function() {
    this.timeout(40000);
    server = new Server(portNumber);
    await server.listen(portNumber, 'localhost');
    browser = await puppeteer.launch({
      headless: process.env.DEBUG ? false : 'new',
      executablePath: getChromePath(),
    });
  });

  after(async function() {
    await Promise.all([
      server && server.close(),
      browser && browser.close(),
    ]);
  });

  beforeEach(async () => {
    page = await browser.newPage();
  });

  afterEach(async () => {
    if (page) await page.close();
  });

  it('renders split captions and omits root caption', async () => {
    await page.goto(`${treemapUrl}?debug`, {
      waitUntil: 'networkidle0',
      timeout: 30000,
    });

    const rootCaptions = await page.evaluate(() => {
      const root = document.querySelector('.webtreemap-node--root');
      if (!root) return [];
      return Array.from(root.children)
        .filter(el => el.classList.contains('webtreemap-caption'))
        .map(el => el.textContent.trim());
    });
    expect(rootCaptions).toHaveLength(0);

    const leafCaptionStructure = await page.evaluate(() => {
      const captions = Array.from(document.querySelectorAll('.webtreemap-caption'));
      if (captions.length === 0) return null;
      const sample = captions[0];
      const childSpans = Array.from(sample.querySelectorAll('span'));
      return {
        totalCaptions: captions.length,
        sampleSpanCount: childSpans.length,
        hasTextInSpans: childSpans.every(s => s.textContent.trim().length > 0),
      };
    });

    expect(leafCaptionStructure).not.toBeNull();
    expect(leafCaptionStructure.totalCaptions).toBeGreaterThan(0);
    expect(leafCaptionStructure.sampleSpanCount).toBeGreaterThanOrEqual(2);
    expect(leafCaptionStructure.hasTextInSpans).toBe(true);
  });

  it('renders depth-dependent shades within bundles', async () => {
    await page.goto(`${treemapUrl}?debug`, {
      waitUntil: 'networkidle0',
      timeout: 30000,
    });

    const depthColors = await page.evaluate(() => {
      const nodes = Array.from(document.querySelectorAll('.webtreemap-node:not(.webtreemap-node--root)'));
      const nestedNodesWithParent = nodes.filter(n => {
        const parent = n.parentElement && n.parentElement.closest('.webtreemap-node:not(.webtreemap-node--root)');
        return Boolean(parent);
      });

      if (nestedNodesWithParent.length === 0) return null;
      const sampleChild = nestedNodesWithParent[0];
      const sampleParent = sampleChild.parentElement.closest('.webtreemap-node:not(.webtreemap-node--root)');

      return {
        parentBg: window.getComputedStyle(sampleParent).backgroundColor,
        childBg: window.getComputedStyle(sampleChild).backgroundColor,
      };
    });

    expect(depthColors).not.toBeNull();
    expect(depthColors.parentBg).not.toBe('');
    expect(depthColors.childBg).not.toBe('');
    expect(depthColors.childBg).not.toBe(depthColors.parentBg);
  });
});
