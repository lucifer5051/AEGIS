import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createEnvelope,
  serializeEnvelope,
  parseLine,
  EventTypes,
  MAX_LINE_BYTES
} from '../src/protocol.js';

test('protocol: valid envelope serialization and parsing', () => {
  const envelope = createEnvelope(EventTypes.INBOUND_MESSAGE, {
    chat_jid: '15551234567@s.whatsapp.net',
    text: 'Hello Python'
  });

  const line = serializeEnvelope(envelope);
  assert.ok(line.endsWith('\n'));

  const parsed = parseLine(line);
  assert.equal(parsed.version, '1.0');
  assert.equal(parsed.type, EventTypes.INBOUND_MESSAGE);
  assert.equal(parsed.payload.text, 'Hello Python');
  assert.ok(parsed.correlation_id);
});

test('protocol: oversized line rejected (>64KB)', () => {
  const hugePayload = 'A'.repeat(MAX_LINE_BYTES + 10);
  assert.throws(() => {
    parseLine(`{"version":"1.0","type":"inbound_message","payload":{"text":"${hugePayload}"}}`);
  }, /exceeded maximum length/);
});

test('protocol: malformed JSON rejected', () => {
  assert.throws(() => {
    parseLine('NOT_A_JSON_STRING\n');
  }, /Malformed JSON/);
});

test('protocol: unsupported version rejected', () => {
  assert.throws(() => {
    parseLine('{"version":"2.0","type":"inbound_message","payload":{}}\n');
  }, /Unsupported sidecar protocol version '2.0'/);
});

test('protocol: unknown or invalid command type rejected', () => {
  assert.throws(() => {
    parseLine('{"version":"1.0","type":"malicious_exec","payload":{}}\n');
  }, /Unknown or invalid message type/);
});

test('protocol: zero-secret enforcement rejects secret patterns', () => {
  assert.throws(() => {
    createEnvelope(EventTypes.OUTBOUND_SEND, {
      token: 'sk-1234567890abcdef1234567890abcdef'
    });
  }, /zero-secret invariant/);

  assert.throws(() => {
    parseLine('{"version":"1.0","type":"outbound_send","payload":{"key":"-----BEGIN RSA PRIVATE KEY-----"}}\n');
  }, /zero-secret invariant/);
});
