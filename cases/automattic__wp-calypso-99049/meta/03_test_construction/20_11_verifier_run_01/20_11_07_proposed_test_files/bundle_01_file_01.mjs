import fs from 'node:fs';
import assert from 'node:assert';

const cssPath = process.argv[2] || '/tmp/domain-overview.css';

assert.ok(fs.existsSync(cssPath), `Compiled CSS file must exist at ${cssPath}`);
const cssContent = fs.readFileSync(cssPath, 'utf8');
assert.ok(cssContent.length > 0, 'Compiled CSS must not be empty');

// Match rule block targeting .domain-forwarding-card__accordion (or parent) and .link-button
const linkButtonRuleRegex = /([^{}]*?\.domain-forwarding-card__accordion[^{}]*?\.link-button[^{}]*?)\{([^}]+)\}/g;
let foundMatchingRule = false;
let match;

while ((match = linkButtonRuleRegex.exec(cssContent)) !== null) {
	const declarations = match[2];
	if (/color\s*:\s*var\(\s*--color-link\s*\)/.test(declarations)) {
		foundMatchingRule = true;
		break;
	}
}

assert.ok(
	foundMatchingRule,
	'Expected compiled CSS to include a rule targeting .domain-forwarding-card__accordion .link-button setting color: var(--color-link)'
);

console.log('PASS: domain forwarding link button color rule is present and sets var(--color-link)');
