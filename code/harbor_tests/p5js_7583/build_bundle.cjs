'use strict';

const fs = require('fs');
const path = require('path');

const sourceRoot = path.resolve(process.argv[2]);
const dependencyRoot = path.resolve(process.argv[3]);
const output = path.resolve(process.argv[4]);
const browserify = require(path.join(dependencyRoot, 'browserify'));
const babelify = require.resolve('babelify', {paths: [dependencyRoot]});
const brfsBabel = require.resolve('brfs-babel', {paths: [dependencyRoot]});

process.chdir(sourceRoot);
const entry = path.join(sourceRoot, 'src', 'app.js');
const bundle = browserify(entry, {
  standalone: 'p5',
  insertGlobalVars: {P5_DEV_BUILD: () => true}
})
  .transform(brfsBabel)
  .transform(babelify, {
    babelrc: false,
    configFile: false,
    presets: [[path.join(dependencyRoot, '@babel', 'preset-env'), {
      useBuiltIns: 'usage',
      corejs: 3
    }]]
  })
  .bundle();

fs.mkdirSync(path.dirname(output), {recursive: true});
const destination = fs.createWriteStream(output, {flags: 'wx', mode: 0o444});
bundle.on('error', error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
bundle.pipe(destination);
