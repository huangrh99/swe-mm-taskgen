import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (value.startsWith('--')) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
for (const key of ['playwright', 'chrome', 'css', 'output', 'screenshot']) {
  if (!args[key]) throw new Error(`missing --${key}`);
}
const { chromium } = await import(pathToFileURL(path.resolve(args.playwright)).href);
const css = await fs.readFile(args.css, 'utf8');
const browser = await chromium.launch({ executablePath: path.resolve(args.chrome), headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 720, height: 360 }, deviceScaleFactor: 1 });
  await page.setContent('<main><cds-combo-box id="combo" ai-label invalid isClosable disabled><cds-ai-label>AI</cds-ai-label></cds-combo-box><cds-combo-box-item id="item" disabled selected></cds-combo-box-item></main>');
  const observed = await page.evaluate((stylesheet) => {
    const install = (name, markup) => customElements.define(name, class extends HTMLElement {
      constructor() {
        super();
        const root = this.attachShadow({ mode: 'open' });
        const style = document.createElement('style');
        style.textContent = stylesheet;
        root.append(style, document.createRange().createContextualFragment(markup));
      }
    });
    install('cds-combo-box', '<div class="cds--list-box__wrapper--decorator"><div class="cds--list-box__field"><input class="cds--text-input"><span class="cds--list-box__invalid-icon">!</span><span class="cds--list-box__menu-icon">⌄</span><button class="cds--list-box__selection">×</button></div><div class="cds--form__helper-text">error</div><slot></slot></div>');
    install('cds-combo-box-item', '<div class="cds--list-box__menu-item__option">Item<span class="cds--list-box__menu-item__selected-icon">✓</span></div>');
    const combo = document.querySelector('#combo');
    const item = document.querySelector('#item');
    const get = (selector, pseudo = null) => getComputedStyle(selector.startsWith('item:') ? item.shadowRoot.querySelector(selector.slice(5)) : combo.shadowRoot.querySelector(selector), pseudo);
    const label = combo.querySelector('cds-ai-label');
    return {
      gradient: get('.cds--list-box__wrapper--decorator').backgroundImage,
      divider_after_width: getComputedStyle(label, '::after').width,
      divider_after_right: getComputedStyle(label, '::after').insetInlineEnd,
      divider_before_left: getComputedStyle(label, '::before').insetInlineStart,
      invalid_icon_end: get('.cds--list-box__invalid-icon').insetInlineEnd,
      input_padding_end: get('.cds--text-input').paddingInlineEnd,
      disabled_selection_pointer_events: get('.cds--list-box__selection').pointerEvents,
      invalid_helper_color: get('.cds--form__helper-text').color,
      disabled_item_cursor: get('item:.cds--list-box__menu-item__option').cursor,
      selected_icon_display: get('item:.cds--list-box__menu-item__selected-icon').display,
    };
  }, css);
  const checks = [
    ['f2p_ai_gradient_decorator', 'F2P', observed.gradient.includes('linear-gradient')],
    ['f2p_ai_right_divider', 'F2P', observed.divider_after_width === '1px' && observed.divider_after_right === '-9px'],
    ['f2p_invalid_ai_left_divider', 'F2P', observed.divider_before_left === '-9px'],
    ['f2p_invalid_ai_closable_spacing', 'F2P', observed.invalid_icon_end === '116px' && observed.input_padding_end === '141px'],
    ['p2p_disabled_selection_noninteractive', 'P2P', observed.disabled_selection_pointer_events === 'none'],
    ['p2p_invalid_helper_error_color', 'P2P', observed.invalid_helper_color === 'rgb(218, 30, 40)'],
    ['p2p_disabled_item_semantics', 'P2P', observed.disabled_item_cursor === 'not-allowed'],
    ['p2p_selected_item_state', 'P2P', observed.selected_icon_display === 'block'],
  ].map(([test_id, test_class, passed]) => ({ test_id, class: test_class, status: passed ? 'pass' : 'fail' }));
  await page.screenshot({ path: path.resolve(args.screenshot), fullPage: true });
  const result = { schema_version: 'browser-css-oracle-v1', css: path.resolve(args.css), observed, results: checks,
    scope: 'real Chromium computed-style oracle over a minimal shadow-DOM fixture; not full component pixel equivalence' };
  await fs.writeFile(args.output, JSON.stringify(result, null, 2) + '\n');
} finally {
  await browser.close();
}
