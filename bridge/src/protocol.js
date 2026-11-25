/**
 * SAGE / AEGIS WhatsApp Bridge -- IPC Wire Protocol Encoder & Decoder
 *
 * Implements the newline-delimited JSON (JSON-Lines) protocol matching
 * Python's src/gateway/sidecar_protocol.py.
 *
 * Wire Invariants:
 * - Version: exactly "1.0"
 * - Max line length: 65536 bytes (64 KB)
 * - Message format: { version, type, correlation_id, timestamp, payload }
 * - Zero secrets allowed in envelope payloads
 */

import crypto from 'node:crypto';

export const PROTOCOL_VERSION = '1.0';
export const MAX_LINE_BYTES = 65536;

export const EventTypes = Object.freeze({
  INBOUND_MESSAGE: 'inbound_message',
  OUTBOUND_SEND: 'outbound_send',
  OUTBOUND_ACK: 'outbound_ack',
  CONNECTION_UPDATE: 'connection_update',
  HEARTBEAT: 'heartbeat',
  SHUTDOWN: 'shutdown',
  ERROR: 'error'
});

const VALID_TYPES = new Set(Object.values(EventTypes));

// Patterns matching API keys, tokens, and private keys
const SECRET_PATTERNS = [
  /sk-[A-Za-z0-9_-]{20,}/,
  /ghp_[A-Za-z0-9]{30,}/,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /bot\d+:[A-Za-z0-9_-]+/,
  /\b\d{8,12}:[A-Za-z0-9_-]{25,}\b/
];

/**
 * Validates that string contains no forbidden secret patterns.
 * Throws an Error if a secret is detected.
 */
export function assertNoSecrets(text) {
  if (typeof text !== 'string') return;
  for (const pat of SECRET_PATTERNS) {
    if (pat.test(text)) {
      throw new Error('Payload violates zero-secret invariant');
    }
  }
}

/**
 * Creates and formats a validated envelope object.
 */
export function createEnvelope(type, payload = {}, correlationId = null) {
  if (!VALID_TYPES.has(type)) {
    throw new Error(`Unsupported event type: ${type}`);
  }

  const envelope = {
    version: PROTOCOL_VERSION,
    type,
    correlation_id: correlationId || crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    payload: payload && typeof payload === 'object' ? payload : {}
  };

  const serialized = JSON.stringify(envelope);
  assertNoSecrets(serialized);
  return envelope;
}

/**
 * Serializes an envelope to a newline-terminated JSON string.
 */
export function serializeEnvelope(envelope) {
  const line = JSON.stringify(envelope);
  assertNoSecrets(line);
  if (Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES) {
    throw new Error(`Envelope exceeds maximum length of ${MAX_LINE_BYTES} bytes`);
  }
  return `${line}\n`;
}

/**
 * Parses and validates a raw line received over IPC.
 */
export function parseLine(line) {
  if (!line || typeof line !== 'string') {
    throw new Error('Invalid input: line must be a non-empty string');
  }

  const trimmed = line.trim();
  if (!trimmed) {
    throw new Error('Received empty or whitespace-only sidecar line');
  }

  const byteLength = Buffer.byteLength(trimmed, 'utf8');
  if (byteLength > MAX_LINE_BYTES) {
    throw new Error(`Sidecar message exceeded maximum length (${byteLength} bytes > ${MAX_LINE_BYTES} bytes limit)`);
  }

  assertNoSecrets(trimmed);

  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (err) {
    throw new Error(`Malformed JSON in sidecar IPC message: ${err.message}`);
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Sidecar IPC message must be a JSON object');
  }

  if (parsed.version !== PROTOCOL_VERSION) {
    throw new Error(`Unsupported sidecar protocol version '${parsed.version}'. Expected '${PROTOCOL_VERSION}'`);
  }

  if (!parsed.type || !VALID_TYPES.has(parsed.type)) {
    throw new Error(`Unknown or invalid message type '${parsed.type}'`);
  }

  return {
    version: parsed.version,
    type: parsed.type,
    correlation_id: parsed.correlation_id || crypto.randomUUID(),
    timestamp: parsed.timestamp || new Date().toISOString(),
    payload: parsed.payload && typeof parsed.payload === 'object' ? parsed.payload : {}
  };
}
