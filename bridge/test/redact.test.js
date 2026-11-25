import test from 'node:test';
import assert from 'node:assert/strict';
import { redactPhone, redactSecrets, sanitizeLogObject } from '../src/redact.js';

test('redact: phone numbers keep at most last 2 digits', () => {
  assert.equal(redactPhone('15551234567@s.whatsapp.net'), '***67@s.whatsapp.net');
  assert.equal(redactPhone('15551234567'), '***67');
  assert.equal(redactPhone('12'), '**');
  assert.equal(redactPhone(''), '[empty]');
});

test('redact: secret patterns are redacted from strings', () => {
  const secretStr = 'My key is sk-1234567890abcdef1234567890abcdef and ghp_123456789012345678901234567890123456';
  const redacted = redactSecrets(secretStr);
  assert.ok(!redacted.includes('sk-1234567890abcdef'));
  assert.ok(!redacted.includes('ghp_1234567890'));
  assert.ok(redacted.includes('[REDACTED_SECRET]'));
});

test('redact: sanitizeLogObject never leaks message bodies', () => {
  const rawMetadata = {
    stanzaId: 'MSG_123',
    sender_pn: '15551234567@s.whatsapp.net',
    text: 'Confidential message content that must never be logged',
    nested: {
      conversation: 'Nested secret conversation',
      phone: '15559876543@s.whatsapp.net',
      code: 'ERR_TIMEOUT'
    }
  };

  const sanitized = sanitizeLogObject(rawMetadata);
  assert.equal(sanitized.stanzaId, 'MSG_123');
  assert.equal(sanitized.sender_pn, '***67@s.whatsapp.net');
  assert.equal(sanitized.text, '[REDACTED_BODY]');
  assert.equal(sanitized.nested.conversation, '[REDACTED_BODY]');
  assert.equal(sanitized.nested.phone, '***43@s.whatsapp.net');
  assert.equal(sanitized.nested.code, 'ERR_TIMEOUT');

  const jsonString = JSON.stringify(sanitized);
  assert.ok(!jsonString.includes('Confidential message content'));
  assert.ok(!jsonString.includes('Nested secret conversation'));
});
