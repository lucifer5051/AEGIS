import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { isAllowedTarget, loadAllowlist } from '../src/allowlist.js';

const OWNER_INFO = {
  id: '15551234567@s.whatsapp.net',
  lid: '987654321@lid'
};

const ALLOWED_PN = '15559876543@s.whatsapp.net';
const ALLOWED_LID = '11223344@lid';

test('allowlist: phone form allowed', () => {
  const allowlist = new Set([ALLOWED_PN]);
  const allowed = isAllowedTarget(ALLOWED_PN, allowlist, OWNER_INFO);
  assert.equal(allowed, true);
});

test('allowlist: LID form of an allowed contact allowed via resolver', () => {
  const allowlist = new Set([ALLOWED_PN]);
  const lidResolver = {
    getPNForLIDSync: (lid) => (lid === ALLOWED_LID ? ALLOWED_PN : null),
    getLIDForPNSync: (pn) => (pn === ALLOWED_PN ? ALLOWED_LID : null)
  };

  const allowed = isAllowedTarget(ALLOWED_LID, allowlist, OWNER_INFO, lidResolver);
  assert.equal(allowed, true);
});

test('allowlist: unknown JID rejected', () => {
  const allowlist = new Set([ALLOWED_PN]);
  const allowed = isAllowedTarget('15550000000@s.whatsapp.net', allowlist, OWNER_INFO);
  assert.equal(allowed, false);
});

test('allowlist: owner always allowed (even when not in allowlist)', () => {
  const emptyAllowlist = new Set();
  assert.equal(isAllowedTarget(OWNER_INFO.id, emptyAllowlist, OWNER_INFO), true);
  assert.equal(isAllowedTarget(OWNER_INFO.lid, emptyAllowlist, OWNER_INFO), true);
});

test('allowlist: malformed JID rejected', () => {
  const allowlist = new Set([ALLOWED_PN]);
  assert.equal(isAllowedTarget('', allowlist, OWNER_INFO), false);
  assert.equal(isAllowedTarget(null, allowlist, OWNER_INFO), false);
  assert.equal(isAllowedTarget(12345, allowlist, OWNER_INFO), false);
});

test('allowlist: group JID rejected', () => {
  const allowlist = new Set(['12345-67890@g.us']);
  assert.equal(isAllowedTarget('12345-67890@g.us', allowlist, OWNER_INFO), false);
});

test('allowlist: loadAllowlist missing or corrupt file returns empty set', () => {
  const tmpDir = os.tmpdir();
  const corruptFile = path.join(tmpDir, `corrupt_allowlist_${Date.now()}.json`);
  fs.writeFileSync(corruptFile, 'NOT_VALID_JSON{{{');

  try {
    const list = loadAllowlist(corruptFile);
    assert.ok(list instanceof Set);
    assert.equal(list.size, 0);

    // With empty list, non-owner is rejected
    assert.equal(isAllowedTarget(ALLOWED_PN, list, OWNER_INFO), false);
    // Owner is still allowed
    assert.equal(isAllowedTarget(OWNER_INFO.id, list, OWNER_INFO), true);
  } finally {
    fs.unlinkSync(corruptFile);
  }

  const missingList = loadAllowlist('/path/to/nonexistent/file.json');
  assert.equal(missingList.size, 0);
});

test('allowlist: valid JSON array loaded correctly', () => {
  const tmpDir = os.tmpdir();
  const validFile = path.join(tmpDir, `valid_allowlist_${Date.now()}.json`);
  fs.writeFileSync(validFile, JSON.stringify([ALLOWED_PN, '15550001111:1@s.whatsapp.net']));

  try {
    const list = loadAllowlist(validFile);
    assert.equal(list.size, 2);
    assert.ok(list.has(ALLOWED_PN));
    assert.ok(list.has('15550001111@s.whatsapp.net')); // Normalized (stripped device)
  } finally {
    fs.unlinkSync(validFile);
  }
});
