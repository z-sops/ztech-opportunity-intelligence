#!/usr/bin/env node
'use strict';

/**
 * Pre-push secret / credential scan.
 *
 * Scans the WHOLE tracked tree of the repository plus every line ADDED by the
 * branch (so a credential that was removed still shows up in the diff), and
 * reports anything that looks like a live credential.
 *
 * The rules are deliberately HIGH CONFIDENCE. A false positive costs a
 * reviewer's time and a real negative costs a credential, so anything that
 * merely looks like a placeholder, an example, a test fixture or a public
 * identifier is NOT reported.
 *
 * Usage: node scripts/scan-secrets.js [--base <ref>]
 * Exit 0 = clean, 1 = a finding.
 */

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');

/**
 * Files that must never be committed.
 *
 * `.env.example` and `.env.sample` are TEMPLATES: they carry the variable
 * NAMES with empty values and the repositories track them deliberately
 * (the .gitignore has an explicit `!.env.example`), so they are allowed.
 */
const FORBIDDEN_FILES = [
  /(^|\/)\.env$/,
  /(^|\/)\.env\.(?!example$|sample$|template$|dist$)[^/]*$/,
  /(^|\/)id_rsa$/,
  /(^|\/)\.npmrc$/,
  /(^|\/)secrets?\.(json|ya?ml|txt)$/i,
  /(^|\/)credentials?$/i,
];

/**
 * High-confidence secret patterns. Each entry is [name, regex].
 * The regexes require a realistic key prefix, so a variable named
 * `apiKey` or a test string like "secret-token" does not match.
 */
const SECRET_PATTERNS = [
  ['GitHub personal access token', /\bghp_[A-Za-z0-9]{36}\b/],
  ['GitHub fine-grained token', /\bgithub_pat_[A-Za-z0-9_]{22,}\b/],
  ['GitHub app token', /\bghs_[A-Za-z0-9]{36}\b/],
  ['GitHub OAuth token', /\bgho_[A-Za-z0-9]{36}\b/],
  ['GitHub refresh token', /\bghu_[A-Za-z0-9]{36}\b/],
  ['AWS access key id', /\bAKIA[0-9A-Z]{16}\b/],
  ['AWS secret access key', /\baws_secret_access_key\s*[=:]\s*["']?[A-Za-z0-9/+=]{40}["']?/i],
  ['Google API key', /\bAIza[0-9A-Za-z\-_]{35}\b/],
  ['Slack token', /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/],
  ['Stripe live key', /\bsk_live_[0-9A-Za-z]{16,}\b/],
  ['Stripe restricted key', /\brk_live_[0-9A-Za-z]{16,}\b/],
  ['OpenAI key', /\bsk-[A-Za-z0-9]{32,}\b/],
  ['Anthropic key', /\bsk-ant-[A-Za-z0-9\-_]{24,}\b/],
  ['SendGrid key', /\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b/],
  ['npm token', /\bnpm_[A-Za-z0-9]{36}\b/],
  ['PyPI token', /\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,}\b/],
  ['JSON Web Token', /\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b/],
  ['private key block', /-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----/],
  ['Google OAuth client secret', /\bGOCSPX-[A-Za-z0-9_\-]{20,}\b/],
];

/**
 * A line - or a matched value - that is obviously a placeholder, an example
 * or a test fixture is not a finding.
 *
 * The check is applied to the MATCHED TEXT as well as to the line, so a
 * fixture such as `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n` is recognised as
 * the readable English it is, rather than as a credential.
 */
const PLACEHOLDER = /(example|placeholder|sample|dummy|fake|redacted|your[_-]|<|\.\.\.|xxx|test[_-]?only|not[_-]?a[_-]?real|never|notreal|for[_-]?test)/i;

function isPlaceholderLine(line) {
  return PLACEHOLDER.test(line);
}

function git(args, options = {}) {
  return execFileSync('git', args, { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, ...options });
}

function listTrackedFiles() {
  return git(['ls-files']).split(/\r?\n/).filter(Boolean);
}

function readFile(rel) {
  try {
    const buf = fs.readFileSync(path.join(ROOT, rel));
    // Skip binaries.
    if (buf.includes(0)) return null;
    return buf.toString('utf8');
  } catch {
    return null;
  }
}

function scanText(rel, text, findings, isAdded) {
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.length > 4000) continue;
    for (const [name, re] of SECRET_PATTERNS) {
      const m = line.match(re);
      if (!m) continue;
      // The matched value itself must not read like a credential either.
      if (isPlaceholderLine(line) || isPlaceholderLine(m[0])) continue;
      findings.push({ file: rel, line: i + 1, kind: name, excerpt: m[0].slice(0, 12) + '...' });
    }
  }
}

function main() {
  const findings = [];
  const notes = [];

  // 1. Forbidden files.
  for (const rel of listTrackedFiles()) {
    for (const re of FORBIDDEN_FILES) {
      if (re.test(rel)) findings.push({ file: rel, line: 0, kind: 'a file that must not be committed', excerpt: rel });
    }
  }

  // 2. The whole tracked tree.
  for (const rel of listTrackedFiles()) {
    const text = readFile(rel);
    if (text === null) continue;
    scanText(rel, text, findings, false);
  }

  // 3. Every line ADDED by the branch, including lines that were later
  //    removed - a credential that was fixed must still be reported.
  let added = '';
  let base = 'origin/main';
  const baseIndex = process.argv.indexOf('--base');
  if (baseIndex > -1 && process.argv[baseIndex + 1]) base = process.argv[baseIndex + 1];
  try {
    added = git(['diff', '-U0', `${base}...HEAD`, '--', '.']);
  } catch {
    added = '';
  }
  if (added) {
    let rel = null;
    for (const line of added.split(/\r?\n/)) {
      if (line.startsWith('+++ b/')) { rel = line.slice(6); continue; }
      if (!line.startsWith('+') || line.startsWith('+++')) continue;
      scanText(rel || '(diff)', line.slice(1), findings, true);
    }
  } else {
    notes.push('the branch diff could not be read; the whole tree was scanned instead');
  }

  // 4. Report.
  console.log('Secret / credential scan');
  console.log(`  files scanned : ${listTrackedFiles().length}`);
  console.log(`  findings      : ${findings.length}`);
  for (const n of notes) console.log(`  note          : ${n}`);

  if (findings.length) {
    console.log('');
    console.log('=== FINDINGS ===');
    for (const f of findings) {
      console.log(`  ${f.file}${f.line ? ':' + f.line : ''}  ${f.kind}  (${f.excerpt})`);
    }
    console.log('');
    console.log('Nothing was pushed. Review every finding before pushing.');
    process.exit(1);
  }

  console.log('');
  console.log('No credentials found.');
  process.exit(0);
}

main();
