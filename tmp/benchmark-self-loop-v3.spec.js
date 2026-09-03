import { chromium } from 'playwright';
import { JSDOM } from 'jsdom';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import mermaid from '../../../mermaid.js';
import { mermaidAPI } from '../../../mermaidAPI.js';

let browser;
let page;
let oldWindow;
let oldDocument;
let oldMutationObserver;
let oldLocalStorageDescriptor;

const render = async (id, source) => {
  const { svg } = await mermaidAPI.render(id, source);
  return new JSDOM(svg).window.document;
};

const nodeBox = (node) => {
  const transform = node.getAttribute('transform') ?? '';
  const translate = transform.match(/translate\(\s*(-?[\d.]+)[, ]+\s*(-?[\d.]+)\s*\)/);
  expect(translate).toBeTruthy();
  const shape = node.querySelector('rect');
  expect(shape).toBeTruthy();
  const tx = Number(translate[1]);
  const ty = Number(translate[2]);
  const x = tx + Number(shape.getAttribute('x'));
  const y = ty + Number(shape.getAttribute('y'));
  const width = Number(shape.getAttribute('width'));
  const height = Number(shape.getAttribute('height'));
  return { left: x, right: x + width, top: y, bottom: y + height };
};

const samplePath = async (path) => {
  const d = path.getAttribute('d');
  expect(d).toBeTruthy();
  await page.setContent(`<svg xmlns="http://www.w3.org/2000/svg"><path id="sample" d="${d}"/></svg>`);
  return page.$eval('#sample', (sample) => {
    const length = sample.getTotalLength();
    const count = Math.max(16, Math.ceil(length));
    const points = Array.from({ length: count + 1 }, (_, index) => {
      const point = sample.getPointAtLength((length * index) / count);
      return { x: point.x, y: point.y };
    });
    const turns = [];
    for (let index = 1; index < points.length - 1; index += 1) {
      const before = {
        x: points[index].x - points[index - 1].x,
        y: points[index].y - points[index - 1].y,
      };
      const after = {
        x: points[index + 1].x - points[index].x,
        y: points[index + 1].y - points[index].y,
      };
      const dot = before.x * after.x + before.y * after.y;
      const magnitude = Math.hypot(before.x, before.y) * Math.hypot(after.x, after.y);
      turns.push((Math.acos(Math.max(-1, Math.min(1, dot / magnitude))) * 180) / Math.PI);
    }
    return { points, maximumLocalTurn: Math.max(...turns) };
  });
};

const expectNaturalSideLoop = async (path, box) => {
  const { points, maximumLocalTurn } = await samplePath(path);
  const interior = points.slice(2, -2);
  const rightRatio = interior.filter((point) => point.x >= box.right - 0.5).length / interior.length;
  const leftRatio = interior.filter((point) => point.x <= box.left + 0.5).length / interior.length;
  const entersNode = interior.some(
    (point) =>
      point.x > box.left + 0.5 &&
      point.x < box.right - 0.5 &&
      point.y > box.top + 0.5 &&
      point.y < box.bottom - 0.5
  );

  expect(Math.max(leftRatio, rightRatio)).toBeGreaterThanOrEqual(0.9);
  expect(entersNode).toBe(false);
  expect(maximumLocalTurn).toBeLessThanOrEqual(45);
};

describe('state self-loop visual geometry contract', () => {
  beforeAll(async () => {
    browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true });
    page = await browser.newPage();
  });

  beforeEach(async () => {
    oldWindow = globalThis.window;
    oldDocument = globalThis.document;
    oldMutationObserver = globalThis.MutationObserver;
    oldLocalStorageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
    const dom = new JSDOM('<html lang="en"><body></body></html>', {
      url: 'https://benchmark.invalid/',
      beforeParse(window) {
        window.Element.prototype.getBBox = () => ({ x: 0, y: 0, width: 100, height: 50 });
        window.Element.prototype.getComputedTextLength = () => 50;
      },
    });
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.MutationObserver = undefined;
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: dom.window.localStorage,
    });
    await mermaid.registerExternalDiagrams([]);
    mermaid.initialize({
      deterministicIds: true,
      deterministicIDSeed: 'benchmark-self-loop-v3',
      flowchart: { htmlLabels: false },
      logLevel: 5,
    });
  });

  afterEach(() => {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.MutationObserver = oldMutationObserver;
    if (oldLocalStorageDescriptor) {
      Object.defineProperty(globalThis, 'localStorage', oldLocalStorageDescriptor);
    } else {
      delete globalThis.localStorage;
    }
  });

  afterAll(async () => {
    await page?.close();
    await browser?.close();
  });

  it('renders the Issue reproducer as a smooth left-or-right state self-loop', async () => {
    const document = await render(
      'state-self-loop',
      'stateDiagram-v2\n[*] --> Node\nNode --> Node: Self Edge'
    );
    const paths = [...document.querySelectorAll('.edgePaths path')];
    expect(paths.length).toBe(2);
    expect(document.querySelector('.edgeLabels')?.textContent).toContain('Self Edge');
    const box = nodeBox(document.querySelector('.node[id*="-Node-"]'));
    await expectNaturalSideLoop(paths.at(-1), box);
  });

  it('preserves labeled flowchart self-loop rendering', async () => {
    const document = await render('flowchart-self-loop', 'flowchart TD\nC -->|retry| C');
    expect(document.querySelectorAll('.edgePaths path.flowchart-link').length).toBeGreaterThan(0);
    expect(document.querySelector('.edgeLabels')?.textContent).toContain('retry');
  });

  it('preserves a labeled non-cyclic flowchart edge', async () => {
    const document = await render('ordinary-edge', 'flowchart TD\nA -->|next| B');
    expect(document.querySelectorAll('.edgePaths path.flowchart-link').length).toBe(1);
    expect(document.querySelector('.edgeLabels')?.textContent).toContain('next');
  });
});
