import fs from 'node:fs';
import { spawnSync } from 'node:child_process';

const logs = '/logs/verifier';
fs.mkdirSync(logs, { recursive: true });

const run = (command, args) => spawnSync(command, args, {
  cwd: '/testbed',
  encoding: 'utf8',
  env: process.env,
  maxBuffer: 64 * 1024 * 1024,
});

const productionStylesheets = [
  {
    source: 'client/my-sites/domains/domain-management/settings/cards/style.scss',
    input: '/tmp/domain-settings-cards.input.scss',
    output: '/tmp/domain-settings-cards.css',
  },
  {
    source: 'client/my-sites/domains/domain-management/domain-overview-pane/style.scss',
    input: '/tmp/domain-overview.input.scss',
    output: '/tmp/domain-overview.css',
  },
];

const calypsoSassPrelude = "@use 'client/assets/stylesheets/shared/_utils.scss' as *;\n";
const sassRuns = productionStylesheets.map(({ source, input, output }) => {
  // Calypso's webpack config prepends _utils.scss to every Sass module.
  // Reproduce that production compiler contract instead of compiling a module
  // in an artificial standalone context.
  fs.writeFileSync(input, `${calypsoSassPrelude}${fs.readFileSync(source, 'utf8')}`);
  return {
    source,
    input,
    output,
    result: run('./node_modules/.bin/sass', [
    '--load-path=.',
    '--load-path=node_modules',
    '--style=compressed',
    input,
    output,
    ]),
  };
});
const stylesCompiled = sassRuns.every(({ result }) => result.status === 0);
const combinedCss = '/tmp/domain-forwarding-production.css';
if (stylesCompiled) {
  // These are both production styles imported by the Domain Forwarding page.
  // Testing their cascade permits any functionally equivalent implementation,
  // rather than requiring the fix to live in the reference patch's file.
  fs.writeFileSync(
    combinedCss,
    sassRuns.map(({ output }) => fs.readFileSync(output, 'utf8')).join('\n'),
  );
}

let functional = { status: null, stdout: '', stderr: 'stylesheet compilation failed' };
if (stylesCompiled) {
  const runtimeTest = '/testbed/test/client/domain-forwarding-link-color.test.mjs';
  fs.mkdirSync('/testbed/test/client', { recursive: true });
  fs.copyFileSync('/tests/functional.mjs', runtimeTest);
  functional = run('node', [runtimeTest, combinedCss]);
}

const combined = `${functional.stdout || ''}\n${functional.stderr || ''}`;
const statusFor = (id) => {
  if (combined.includes(`PASS [${id}]`)) return 'pass';
  if (combined.includes(`FAIL [${id}]`)) return 'fail';
  return stylesCompiled ? 'missing' : 'error';
};

const domain = run('yarn', [
  'test-client',
  '--runTestsByPath',
  'client/my-sites/domains/domain-management/domain-overview-pane/test/index.test.tsx',
  '--runInBand',
]);

const results = [
  {
    test_id: 'WPC-99049-LINK-COLOR',
    status: statusFor('WPC-99049-LINK-COLOR'),
    purpose: 'The rendered + Add forward action resolves to the page standard --color-link value.',
    source: 'verifier_generated',
  },
  {
    test_id: 'WPC-99049-P2P-COLOR-SCOPE',
    status: statusFor('WPC-99049-P2P-COLOR-SCOPE'),
    purpose: 'The color rule remains scoped to Domain Forwarding and does not recolor an unrelated link-button.',
    source: 'verifier_generated',
  },
  {
    test_id: 'WPC-99049-P2P-DOMAIN-OVERVIEW',
    status: domain.status === 0 ? 'pass' : 'fail',
    purpose: 'The repository existing DomainOverviewPane behavior suite remains green after the style repair.',
    source: 'repository_existing',
  },
];

const reward = results.every((item) => item.status === 'pass') ? 1 : 0;
const record = {
  schema_version: 'wp-calypso-99049-functional-results-v2',
  reward,
  results,
  commands: {
    sass: sassRuns.map(({ source, input, output, result }) => ({
      source,
      compiler_input: input,
      output,
      exit_code: result.status,
      stdout: result.stdout,
      stderr: result.stderr,
    })),
    computed_style: { exit_code: functional.status, stdout: functional.stdout, stderr: functional.stderr },
    domain_overview: { exit_code: domain.status, stdout: domain.stdout, stderr: domain.stderr },
  },
};
fs.writeFileSync(`${logs}/test_results.json`, `${JSON.stringify(record, null, 2)}\n`);
fs.writeFileSync(`${logs}/framework_results.json`, `${JSON.stringify({ reward, tests: results }, null, 2)}\n`);
fs.writeFileSync(`${logs}/test-stdout.txt`, [
  ...sassRuns.flatMap(({ source, result }) => [
    `SCSS source: ${source}`,
    result.stdout,
    result.stderr,
  ]),
  functional.stdout,
  functional.stderr,
  domain.stdout,
  domain.stderr,
].filter(Boolean).join('\n'));
fs.writeFileSync(`${logs}/reward.txt`, `${reward}\n`);
console.log(JSON.stringify({ reward, results }));
