#!/usr/bin/env python3
"""Compile the candidate SCSS and measure the frozen fixture in real Chromium."""

from __future__ import annotations

import html
import json
import os
import subprocess
import tempfile
from pathlib import Path


TESTS = {
    "f2p_ai_gradient_decorator": lambda value: value["gradient"] != "none",
    "f2p_ai_right_divider": lambda value: (
        value["divider_after_width"] == "1px"
        and value["divider_after_right"] == "-9px"
    ),
    "f2p_invalid_ai_left_divider": lambda value: value["divider_before_left"] == "-9px",
    "f2p_invalid_ai_closable_spacing": lambda value: (
        value["input_padding_end"] == "141px" and value["invalid_icon_end"] == "116px"
    ),
    "p2p_disabled_selection_noninteractive": lambda value: value["disabled_selection_pointer_events"] == "none",
    "p2p_invalid_helper_error_color": lambda value: value["invalid_helper_color"] == "rgb(218, 30, 40)",
    "p2p_disabled_item_semantics": lambda value: value["disabled_item_cursor"] == "not-allowed",
    "p2p_selected_item_state": lambda value: value["selected_icon_display"] == "block",
}


def _run(command: list[str], timeout: int = 120,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout,
                          check=False, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="harbor-functional-") as temporary:
        root = Path(temporary)
        css_path = root / "combo.css"
        compile_result = _run([
            "/testbed/node_modules/.bin/sass",
            "/testbed/packages/web-components/src/components/combo-box/combo-box.scss",
            str(css_path), "--load-path", "/testbed/packages", "--load-path", "/testbed/node_modules",
        ])
        if compile_result.returncode:
            raise RuntimeError(f"sass compile failed: {compile_result.stderr[-4000:]}")
        css = css_path.read_text()
        document = f'''<!doctype html><meta charset="utf-8"><body>
<cds-combo-box id="main" ai-label invalid isClosable><cds-ai-label></cds-ai-label></cds-combo-box>
<cds-combo-box id="disabled" disabled></cds-combo-box>
<cds-combo-box id="invalid" invalid></cds-combo-box>
<cds-combo-box-item id="disabled-item" disabled></cds-combo-box-item>
<cds-combo-box-item id="selected-item" selected></cds-combo-box-item>
<pre id="result"></pre>
<script>
const css = {json.dumps(css)};
class Combo extends HTMLElement {{
  connectedCallback() {{
    const shadow = this.attachShadow({{mode: 'open'}});
    shadow.innerHTML = `<style>${{css}}</style><div class="cds--list-box__wrapper--decorator"><div class="cds--list-box cds--list-box--invalid" data-invalid><div class="cds--list-box__field"><input class="cds--text-input cds--text-input--empty"><button class="cds--list-box__selection"></button><span class="cds--list-box__invalid-icon"></span></div><div class="cds--form__helper-text">error</div><slot></slot></div></div>`;
  }}
}}
class Item extends HTMLElement {{
  connectedCallback() {{
    const shadow = this.attachShadow({{mode: 'open'}});
    shadow.innerHTML = `<style>${{css}}</style><div class="cds--list-box__menu-item__option"><span class="cds--list-box__menu-item__selected-icon"></span></div>`;
  }}
}}
customElements.define('cds-combo-box', Combo);
customElements.define('cds-combo-box-item', Item);
customElements.define('cds-ai-label', class extends HTMLElement {{}});
setTimeout(() => {{
  const main = document.querySelector('#main');
  const ai = main.querySelector('cds-ai-label');
  const mainRoot = main.shadowRoot;
  const disabledRoot = document.querySelector('#disabled').shadowRoot;
  const invalidRoot = document.querySelector('#invalid').shadowRoot;
  const disabledItem = document.querySelector('#disabled-item');
  const selectedItem = document.querySelector('#selected-item');
  const observed = {{
    gradient: getComputedStyle(mainRoot.querySelector('.cds--list-box__wrapper--decorator')).backgroundImage,
    divider_after_width: getComputedStyle(ai, '::after').width,
    divider_after_right: getComputedStyle(ai, '::after').right,
    divider_before_left: getComputedStyle(ai, '::before').left,
    invalid_icon_end: getComputedStyle(mainRoot.querySelector('.cds--list-box__invalid-icon')).right,
    input_padding_end: getComputedStyle(mainRoot.querySelector('input')).paddingRight,
    disabled_selection_pointer_events: getComputedStyle(disabledRoot.querySelector('.cds--list-box__selection')).pointerEvents,
    invalid_helper_color: getComputedStyle(invalidRoot.querySelector('.cds--form__helper-text')).color,
    disabled_item_cursor: getComputedStyle(disabledItem.shadowRoot.querySelector('.cds--list-box__menu-item__option')).cursor,
    selected_icon_display: getComputedStyle(selectedItem.shadowRoot.querySelector('.cds--list-box__menu-item__selected-icon')).display
  }};
  document.querySelector('#result').textContent = JSON.stringify(observed);
}}, 0);
</script>'''
        page = root / "fixture.html"; page.write_text(document)
        browser_env = dict(os.environ)
        browser_env.update({"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                            "XDG_CACHE_HOME": str(root / "cache")})
        browser = _run(["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-gpu",
                        "--disable-dev-shm-usage", "--disable-breakpad",
                        "--disable-crash-reporter", "--virtual-time-budget=3000",
                        f"--user-data-dir={root / 'chromium-profile'}",
                        "--dump-dom", page.as_uri()], env=browser_env)
        if browser.returncode:
            raise RuntimeError(f"chromium failed: {browser.stderr[-4000:]}")
        marker = '<pre id="result">'
        if marker not in browser.stdout:
            raise RuntimeError("Chromium output did not contain the result marker")
        payload = browser.stdout.split(marker, 1)[1].split("</pre>", 1)[0]
        if not payload.strip():
            raise RuntimeError(f"Chromium fixture produced no result: {browser.stderr[-4000:]}")
        observed = json.loads(html.unescape(payload))
        results = [{"test_id": test_id, "status": "pass" if predicate(observed) else "fail"}
                   for test_id, predicate in TESTS.items()]
        print(json.dumps({"schema_version": "carbon-combo-functional-v1", "observed": observed,
                          "results": results}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
