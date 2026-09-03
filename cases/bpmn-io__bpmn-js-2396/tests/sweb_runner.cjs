// Test-side observer. Uses repository Karma configuration and unchanged assertions.
const fs = require('fs');
const path = require('path');
const root = '/testbed';
const harness = '/harness';
const testRoot = '/harness/test';
const output = '/results/framework_results.json';
const manifest = JSON.parse(fs.readFileSync(path.join(harness, 'test_manifest.json')));
const suites = manifest.selected_suites;

// The frozen image provides distro Chromium, while the repository's Puppeteer
// helper points at a user-cache path that is intentionally absent in Harbor.
if (!process.env.CHROME_BIN || !fs.existsSync(process.env.CHROME_BIN)) {
  process.env.CHROME_BIN = process.env.CHROME_PATH || '/usr/bin/chromium';
}

if (!Array.isArray(suites) || !suites.length || suites.some(p =>
  !p.startsWith('test/') || p.includes('..') || !p.endsWith('Spec.js'))) {
  throw new Error('Invalid frozen component-suite list');
}

let config;
require(testRoot + '/config/karma.unit.js')({ set(value) { config = value; } });
// karma.unit.js overwrites CHROME_BIN with Puppeteer's build-time cache path.
// Rebind it after loading the repository config to the browser frozen in the image.
process.env.CHROME_BIN = process.env.CHROME_PATH || '/usr/bin/chromium';
const bundle = '/tmp/sweb_bundle.js';
const globalSetup = fs.existsSync(testRoot + '/globals.js') ? "require('/harness/test/globals');\n" : '';
fs.writeFileSync(bundle, globalSetup + suites.map(p =>
  `require(${JSON.stringify(harness + '/' + p)});`).join('\n'));
config.basePath = root;
config.files = [bundle];
config.preprocessors = { [bundle]: ['webpack', 'env'] };
config.singleRun = true;
config.autoWatch = false;
config.browserNoActivityTimeout = 60000;
config.browsers = ['PilotChrome'];
config.customLaunchers = {
  PilotChrome: { base: 'ChromeHeadless', flags: [
    '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1280,900',
    '--force-device-scale-factor=1', '--lang=en-US'
  ] }
};
config.webpack.resolve.modules = [root + '/node_modules', harness, root];
config.webpack.resolve.alias = Object.assign({}, config.webpack.resolve.alias, {
  test: testRoot,
  lib: root + '/lib'
});
config.reporters = ['sweb-json'];
config.plugins = ['karma-mocha', 'karma-webpack', 'karma-env-preprocessor',
  'karma-chrome-launcher', 'karma-chrome-launcher-2', 'karma-sinon-chai']
  .filter(name => fs.existsSync(root + '/node_modules/' + name))
  .map(name => require(root + '/node_modules/' + name)).concat([{
  'reporter:sweb-json': ['type', Reporter]
}]);
config.client = Object.assign({}, config.client, { mocha: { timeout: 10000 } });
const observations = { schema: 'karma-observation-v1', suites, tests: [],
  browser_errors: [], complete: false, runner_exit_code: null };
fs.mkdirSync('/results', { recursive: true });
function save() { fs.writeFileSync(output, JSON.stringify(observations, null, 2)); }
function Reporter() {
  this.onSpecComplete = (browser, result) => {
    observations.tests.push({
      test_id: JSON.stringify([result.suite, result.description]),
      suite: result.suite, title: result.description,
      status: result.skipped ? 'skip' : result.success ? 'pass' : 'fail',
      logs: result.log, duration_ms: result.time, browser: browser.name
    });
    save();
  };
  this.onBrowserError = (browser, error) => {
    observations.browser_errors.push({ browser: browser.name, error }); save();
  };
  this.onRunComplete = (browsers, result) => {
    observations.complete = true;
    observations.framework_summary = result;
    save();
  };
}
const { Server } = require(root + '/node_modules/karma');
new Server(config, exitCode => {
  observations.runner_exit_code = exitCode;
  save();
  process.exitCode = exitCode;
}).start().catch(error => {
  observations.browser_errors.push({ error: String(error) });
  observations.runner_exit_code = 1;
  save();
  process.exit(1);
});
