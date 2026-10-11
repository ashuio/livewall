#!/usr/bin/env node
/**
 * Install a finished client build into `web/dist`.
 *
 * `web/dist` is served by a running instance. Building into it directly is
 * destructive: Vite empties the output directory before it compiles, so the
 * app answers 404 for the whole build, and stays 404 for good if the build
 * fails. `npm run build` therefore compiles into `.dist-staging` and calls
 * this script, which swaps the two with renames. The served tree is always
 * either the previous build or the new one — never missing.
 */
import { existsSync, renameSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const web = join(dirname(fileURLToPath(import.meta.url)), '..');
const staging = join(web, '.dist-staging');
const target = join(web, 'dist');
const previous = join(web, 'dist.old');

if (!existsSync(staging)) {
  console.error('install-dist: .dist-staging is missing — run the vite build first');
  process.exit(1);
}

rmSync(previous, { recursive: true, force: true });
if (existsSync(target)) renameSync(target, previous);
renameSync(staging, target);
rmSync(previous, { recursive: true, force: true });

console.log('install-dist: web/dist updated');
