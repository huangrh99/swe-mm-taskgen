/**
 * @license
 * Copyright 2026 Google LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import puppeteer from 'puppeteer';
import {getChromePath} from 'chrome-launcher';

import {Server} from '../../cli/test/fixtures/static-server.js';

const KIB = 1024;
const BUNDLE_SIZE = 900 * KIB;
const BUNDLE_LABELS = [
  'bundle-alpha.js',
  'bundle-beta.js',
  'bundle-gamma.js',
  'bundle-delta.js',
];
const CHILD_LABELS = [
  'alpha-entry.js',
  'beta-entry.js',
  'gamma-entry.js',
  'delta-entry.js',
];

function makeBundle(name, entryName, helperName) {
  return {
    name,
    resourceBytes: BUNDLE_SIZE,
    unusedBytes: 100 * KIB,
    children: [
      {name: entryName, resourceBytes: 600 * KIB, unusedBytes: 100 * KIB},
      {name: helperName, resourceBytes: 300 * KIB, unusedBytes: 0},
    ],
  };
}

const treemapOptions = {
  lhr: {
    finalDisplayedUrl: 'https://example.test/',
    configSettings: {locale: 'en-US'},
    audits: {
      'script-treemap-data': {
        details: {
          type: 'treemap-data',
          nodes: [
            makeBundle('bundle-alpha.js', 'alpha-entry.js', 'alpha-helper.js'),
            makeBundle('bundle-beta.js', 'beta-entry.js', 'beta-helper.js'),
            makeBundle('bundle-gamma.js', 'gamma-entry.js', 'gamma-helper.js'),
            makeBundle('bundle-delta.js', 'delta-entry.js', 'delta-helper.js'),
          ],
        },
      },
    },
  },
};

function normalizeText(text) {
  return text.replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
}

function angularDistance(left, right) {
  const difference = Math.abs(left - right) % 360;
  return Math.min(difference, 360 - difference);
}

/** @type {import('puppeteer').Browser} */
let browser;
/** @type {import('puppeteer').Page} */
let page;
/** @type {Server} */
let server;
let baseUrl;

describe('Lighthouse Treemap redesign [LHM-16403]', () => {
  before(async () => {
    server = new Server(0);
    await server.listen(0, '127.0.0.1');
    baseUrl = 'http://127.0.0.1:' + server.getPort();
    browser = await puppeteer.launch({
      headless: 'new',
      executablePath: getChromePath(),
      args: ['--no-sandbox', '--disable-setuid-sandbox'],
    });
    page = await browser.newPage();
    await page.setViewport({width: 1440, height: 900});
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (request.url().startsWith(baseUrl)) request.continue();
      else request.abort();
    });
    await page.evaluateOnNewDocument(options => {
      window.__treemapOptions = options;
    }, treemapOptions);
  });

  beforeEach(async function() {
    // Harbor may execute several browser tasks on the same Docker host. Keep
    // the semantic oracle stable under that expected resource contention.
    this.timeout(60000);
    await page.goto(baseUrl + '/dist/gh-pages/treemap/index.html', {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForSelector('.lh-treemap .webtreemap-node', {timeout: 30000});
    await page.waitForFunction(() => {
      const main = document.querySelector('main');
      const bytes = document.querySelector('.lh-header--url-bytes');
      return Boolean(main && !main.classList.contains('hidden') && bytes?.textContent);
    }, {timeout: 30000});
  });

  after(async () => {
    await browser.close();
    await server.close();
  });

  it('[LHM-16403-TITLE-VISUAL] renders a visible, emphasized title and logo', async () => {
    const state = await page.evaluate(() => {
      const title = document.querySelector('.lh-header--title');
      const logo = document.querySelector('.lh-topbar__logo');
      const style = title && getComputedStyle(title);
      const titleRect = title?.getBoundingClientRect();
      const logoRect = logo?.getBoundingClientRect();
      return {
        title: style && {
          fontSize: Number.parseFloat(style.fontSize),
          fontWeight: Number.parseInt(style.fontWeight, 10),
          visible: style.visibility !== 'hidden' && style.display !== 'none' &&
            Boolean(titleRect?.width && titleRect?.height),
        },
        logo: logo && {
          width: logoRect.width,
          height: logoRect.height,
          visible: getComputedStyle(logo).visibility !== 'hidden' &&
            getComputedStyle(logo).display !== 'none' &&
            Boolean(logoRect.width && logoRect.height),
        },
        separated: Boolean(titleRect && logoRect && logoRect.right <= titleRect.left),
      };
    });

    expect(state.title).not.toBeNull();
    expect(state.logo).not.toBeNull();
    expect(state.title.visible).toBe(true);
    expect(state.logo.visible).toBe(true);
    expect(state.title.fontSize).toBeGreaterThanOrEqual(15);
    expect(state.title.fontSize).toBeLessThanOrEqual(18);
    expect(state.title.fontWeight).toBeGreaterThanOrEqual(500);
    expect(state.logo.width).toBeGreaterThanOrEqual(24);
    expect(state.logo.width).toBeLessThanOrEqual(32);
    expect(Math.abs(state.logo.width - state.logo.height)).toBeLessThanOrEqual(1);
    expect(state.separated).toBe(true);
  });

  it('[LHM-16403-CAPTION-CONTENT] removes the redundant root caption and preserves bundle facts', async () => {
    const state = await page.evaluate(bundles => {
      const root = document.querySelector('.lh-treemap > .webtreemap-node');
      const directRootCaption = root && Array.from(root.children)
        .find(element => element.matches('.webtreemap-caption'));
      const rootCaptionStyle = directRootCaption && getComputedStyle(directRootCaption);
      const rootCaptionRect = directRootCaption?.getBoundingClientRect();
      return {
        hasVisibleDirectRootCaption: Boolean(directRootCaption &&
          rootCaptionStyle.display !== 'none' &&
          rootCaptionStyle.visibility !== 'hidden' &&
          rootCaptionRect.width > 0 && rootCaptionRect.height > 0),
        captions: bundles.map(label => {
          const caption = Array.from(document.querySelectorAll(
            '.lh-treemap .webtreemap-caption'
          )).find(element => (element.textContent || '').trim().startsWith(label));
          return caption?.textContent || '';
        }),
      };
    }, BUNDLE_LABELS);

    expect(state.hasVisibleDirectRootCaption).toBe(false);
    expect(state.captions).toHaveLength(BUNDLE_LABELS.length);
    for (const [index, text] of state.captions.entries()) {
      const normalized = normalizeText(text);
      expect(normalized).toContain(BUNDLE_LABELS[index]);
      expect(normalized).toMatch(/900(?:\.0)? KiB/);
      expect(normalized).toContain('25%');
    }
  });

  it('[LHM-16403-CAPTION-HIERARCHY] gives names and metrics distinct readable emphasis', async () => {
    const state = await page.evaluate(bundles => {
      const visible = element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' &&
          rect.width > 0 && rect.height > 0;
      };
      return bundles.map(label => {
        const caption = Array.from(document.querySelectorAll(
          '.lh-treemap .webtreemap-caption'
        )).find(element => (element.textContent || '').trim().startsWith(label));
        const leaves = caption ? Array.from(caption.querySelectorAll('*'))
          .filter(element => element.children.length === 0 &&
            (element.textContent || '').trim() && visible(element)) : [];
        const name = leaves.find(element => (element.textContent || '').includes(label));
        const metrics = leaves.find(element => /KiB|%/.test(element.textContent || ''));
        if (!caption || !name || !metrics || name === metrics) return null;
        const nameStyle = getComputedStyle(name);
        const metricsStyle = getComputedStyle(metrics);
        const nameRect = name.getBoundingClientRect();
        const metricsRect = metrics.getBoundingClientRect();
        const captionRect = caption.getBoundingClientRect();
        return {
          distinctStyle: nameStyle.fontWeight !== metricsStyle.fontWeight ||
            nameStyle.color !== metricsStyle.color ||
            nameStyle.opacity !== metricsStyle.opacity,
          nonOverlapping: nameRect.right <= metricsRect.left ||
            metricsRect.right <= nameRect.left ||
            nameRect.bottom <= metricsRect.top || metricsRect.bottom <= nameRect.top,
          contained: [nameRect, metricsRect].every(rect =>
            rect.left >= captionRect.left - 1 && rect.right <= captionRect.right + 1 &&
            rect.top >= captionRect.top - 1 && rect.bottom <= captionRect.bottom + 1),
        };
      });
    }, BUNDLE_LABELS);

    expect(state).toHaveLength(BUNDLE_LABELS.length);
    for (const item of state) {
      expect(item).not.toBeNull();
      expect(item.distinctStyle).toBe(true);
      expect(item.nonOverlapping).toBe(true);
      expect(item.contained).toBe(true);
    }
  });

  it('[LHM-16403-COLOR-FAMILIES] keeps top-level bundles distinct and readable', async () => {
    const state = await page.evaluate(bundles => {
      const findNode = label => {
        const caption = Array.from(
          document.querySelectorAll('.lh-treemap .webtreemap-caption')
        ).find(element => (element.textContent || '').trim().startsWith(label));
        return caption?.parentElement || null;
      };
      const parseRgb = color => {
        const values = color.match(/\d+(?:\.\d+)?/g);
        if (!values || values.length < 3) return null;
        return values.slice(0, 3).map(Number);
      };
      const relativeLuminance = rgb => {
        const linear = rgb.map(value => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 :
            ((channel + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
      };
      return bundles.map(label => {
        const node = findNode(label);
        if (!node) return null;
        const style = getComputedStyle(node);
        const rgb = parseRgb(style.backgroundColor);
        const textRgb = parseRgb(getComputedStyle(
          node.querySelector('.webtreemap-caption') || node
        ).color);
        if (!rgb || !textRgb) return null;
        const background = relativeLuminance(rgb);
        const foreground = relativeLuminance(textRgb);
        return {
          color: style.backgroundColor,
          contrast: (Math.max(background, foreground) + 0.05) /
            (Math.min(background, foreground) + 0.05),
        };
      });
    }, BUNDLE_LABELS);

    expect(state).toHaveLength(BUNDLE_LABELS.length);
    expect(state.every(Boolean)).toBe(true);
    expect(new Set(state.map(item => item.color)).size).toBe(BUNDLE_LABELS.length);
    for (const item of state) expect(item.contrast).toBeGreaterThanOrEqual(4.5);
  });

  it('[LHM-16403-DEPTH-COLOR] encodes depth within each bundle color family', async () => {
    const state = await page.evaluate(({bundles, children}) => {
      const findNode = label => {
        const caption = Array.from(
          document.querySelectorAll('.lh-treemap .webtreemap-caption')
        ).find(element => (element.textContent || '').trim().startsWith(label));
        return caption?.parentElement || null;
      };
      const toHsl = color => {
        const values = color.match(/\d+(?:\.\d+)?/g);
        if (!values || values.length < 3) return null;
        const [red, green, blue] = values.slice(0, 3).map(value => Number(value) / 255);
        const max = Math.max(red, green, blue);
        const min = Math.min(red, green, blue);
        const lightness = (max + min) / 2;
        if (max === min) return {h: 0, s: 0, l: lightness * 100};
        const delta = max - min;
        const saturation = lightness > 0.5 ?
          delta / (2 - max - min) : delta / (max + min);
        let hue;
        if (max === red) hue = (green - blue) / delta + (green < blue ? 6 : 0);
        else if (max === green) hue = (blue - red) / delta + 2;
        else hue = (red - green) / delta + 4;
        return {h: hue * 60, s: saturation * 100, l: lightness * 100};
      };
      const describe = node => {
        if (!node) return null;
        const style = getComputedStyle(node);
        return {
          color: style.backgroundColor,
          hsl: toHsl(style.backgroundColor),
        };
      };
      return bundles.map((label, index) => ({
        parent: describe(findNode(label)),
        child: describe(findNode(children[index])),
      }));
    }, {bundles: BUNDLE_LABELS, children: CHILD_LABELS});

    expect(state).toHaveLength(4);
    for (const item of state) {
      expect(item.parent).not.toBeNull();
      expect(item.child).not.toBeNull();
      expect(item.parent.hsl).not.toBeNull();
      expect(item.child.hsl).not.toBeNull();
      expect(angularDistance(item.child.hsl.h, item.parent.hsl.h)).toBeLessThanOrEqual(30);
      expect(item.parent.hsl.l - item.child.hsl.l).toBeGreaterThanOrEqual(4);
      const rgbDistance = Math.hypot(...item.parent.color.match(/\d+(?:\.\d+)?/g)
        .slice(0, 3).map((value, index) =>
          Number(value) - Number(item.child.color.match(/\d+(?:\.\d+)?/g)[index])));
      expect(rgbDistance).toBeGreaterThanOrEqual(15);
    }
  });

  async function selectAlphaBundle() {
    const optionValue = await page.$eval('.bundle-selector', select => {
      const option = Array.from(select.options)
        .find(item => (item.textContent || '').includes('bundle-alpha.js'));
      return option?.value || '';
    });
    expect(optionValue).not.toBe('');
    await page.select('.bundle-selector', optionValue);
    await page.waitForFunction(() => {
      const root = document.querySelector('.lh-treemap > .webtreemap-node');
      const caption = root && Array.from(root.children)
        .find(element => element.matches('.webtreemap-caption'));
      return Boolean(caption?.textContent?.includes('bundle-alpha.js'));
    }, {timeout: 30000});
  }

  it('[LHM-16403-SELECTION-DETAILS] shows the selected bundle details', async () => {
    await selectAlphaBundle();

    const caption = normalizeText(await page.evaluate(() => {
      const root = document.querySelector('.lh-treemap > .webtreemap-node');
      const directCaption = root && Array.from(root.children)
        .find(element => element.matches('.webtreemap-caption'));
      return directCaption?.textContent || '';
    }));
    expect(caption).toContain('Resource Bytes');
    expect(caption).toContain('bundle-alpha.js');
    expect(caption).toMatch(/900(?:\.0)? KiB/);
    expect(caption).toContain('100%');
  });

  it('[LHM-16403-HEADER-TOTAL] preserves the report total after bundle selection', async () => {
    const beforeHeader = normalizeText(await page.$eval(
      '.lh-header--url-bytes', element => element.textContent || ''));
    await selectAlphaBundle();
    const afterHeader = normalizeText(await page.$eval(
      '.lh-header--url-bytes', element => element.textContent || ''));
    expect(afterHeader).toBe(beforeHeader);
  });

  it('[LHM-16403-TABLE-P2P] keeps the resource table populated', async () => {
    const state = await page.evaluate(() => {
      const clean = text => text.replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
      const table = document.querySelector('.lh-table');
      return {
        text: clean(table?.textContent || ''),
        rowCount: table?.querySelectorAll('.tabulator-row').length || 0,
        headings: table ? Array.from(table.querySelectorAll('[role="columnheader"], th'))
          .map(element => clean(element.textContent || '')) : [],
      };
    });
    expect(state.headings.some(heading => /resource|name/i.test(heading))).toBe(true);
    expect(state.headings.some(heading => /size|bytes/i.test(heading))).toBe(true);
    expect(state.rowCount).toBeGreaterThanOrEqual(8);
    for (const label of [
      'alpha-entry.js', 'alpha-helper.js', 'beta-entry.js', 'beta-helper.js',
      'gamma-entry.js', 'gamma-helper.js', 'delta-entry.js', 'delta-helper.js',
    ]) expect(state.text).toContain(label);
    expect(state.text).toMatch(/\d+(?:\.\d+)? KiB/);
  });
});
