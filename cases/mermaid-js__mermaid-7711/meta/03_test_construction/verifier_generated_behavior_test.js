import { JSDOM } from 'jsdom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import mermaid from '../../../mermaid.js';
import { mermaidAPI } from '../../../mermaidAPI.js';

let oldWindow;
let oldDocument;
let oldMutationObserver;
let oldLocalStorageDescriptor;

const render = async (id, source) => {
  const { svg } = await mermaidAPI.render(id, source);
  return new JSDOM(svg).window.document;
};

describe('logical self-loop rendering contract', () => {
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
      deterministicIDSeed: 'benchmark-self-loop',
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

  it('renders one logical SVG path for a flowchart self-loop', async () => {
    const document = await render('flowchart-self-loop', 'flowchart TD\nC -->|retry| C');
    const paths = [...document.querySelectorAll('.edgePaths path.flowchart-link')];
    expect(paths.length).toBe(1);
    expect(paths[0].getAttribute('d')).toBeTruthy();
    expect(document.querySelectorAll('.edgePaths path[data-id*="cyclic-special"]').length).toBe(0);
    expect(document.querySelector('.edgeLabels')?.textContent).toContain('retry');
  });

  it('renders one logical SVG path for a state-diagram self-loop', async () => {
    const document = await render(
      'state-self-loop',
      'stateDiagram-v2\n[*] --> Node\nNode --> Node: Self Edge'
    );
    const paths = [...document.querySelectorAll('.edgePaths path')];
    expect(paths.length).toBe(2);
    expect(paths.every((path) => path.getAttribute('d'))).toBe(true);
    expect(
      document.querySelectorAll(
        '.edgePaths path[data-id*="cyclic-special"], .edgePaths path[id*="cyclic-special"]'
      ).length
    ).toBe(0);
    expect(document.querySelector('.edgeLabels')?.textContent).toContain('Self Edge');
  });

  it('preserves a labeled non-cyclic flowchart edge', async () => {
    const document = await render('ordinary-edge', 'flowchart TD\nA -->|next| B');
    const paths = [...document.querySelectorAll('.edgePaths path.flowchart-link')];
    expect(paths.length).toBe(1);
    expect(paths[0].getAttribute('d')).toBeTruthy();
    expect(document.querySelector('.edgeLabels')?.textContent).toContain('next');
  });
});
