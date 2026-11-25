/**
 * SAGE / AEGIS WhatsApp Bridge -- Stdio IPC Entrypoint
 *
 * Bridges between WhatsApp Web (Baileys) and Python SAGE Gateway over stdio.
 *
 * Invariants:
 * - STDOUT is reserved EXCLUSIVELY for JSON-Lines envelopes (Protocol 1.0).
 * - STDERR is strictly for diagnostic logs and pairing UI.
 * - Inbound messages are normalized and emitted as `inbound_message`.
 * - Outbound sends execute strictly upon receiving `outbound_send` from Python.
 */

import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { jidNormalizedUser } from '@whiskeysockets/baileys';
import {
  createEnvelope,
  serializeEnvelope,
  parseLine,
  EventTypes
} from './protocol.js';
import { normalizeMessage } from './normalize.js';
import { isAllowedTarget, loadAllowlist } from './allowlist.js';
import { createWhatsAppConnection, DEFAULT_AUTH_DIR } from './connection.js';
import { logDiagnostic } from './redact.js';

// Configuration parsed from CLI flags
function parseArgs() {
  const args = process.argv.slice(2);
  const config = {
    authDir: DEFAULT_AUTH_DIR,
    allowlistPath: path.join(os.homedir(), '.aegis', 'whatsapp_allowlist.json')
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--auth-dir' && args[i + 1]) {
      config.authDir = args[i + 1];
      i++;
    } else if (args[i] === '--allowlist' && args[i + 1]) {
      config.allowlistPath = args[i + 1];
      i++;
    }
  }
  return config;
}

const config = parseArgs();
const bridgeStartTime = Math.floor(Date.now() / 1000);

// Rate limiter tracking for outbound sends (sliding window)
const recentSendsMinute = [];
const recentSendsHour = [];
const MAX_SENDS_PER_MINUTE = 6;
const MAX_SENDS_PER_HOUR = 60;

function checkRateLimit() {
  const now = Date.now();
  // Drain older timestamps
  while (recentSendsMinute.length && recentSendsMinute[0] < now - 60000) {
    recentSendsMinute.shift();
  }
  while (recentSendsHour.length && recentSendsHour[0] < now - 3600000) {
    recentSendsHour.shift();
  }

  if (recentSendsMinute.length >= MAX_SENDS_PER_MINUTE) {
    return false;
  }
  if (recentSendsHour.length >= MAX_SENDS_PER_HOUR) {
    return false;
  }
  return true;
}

function recordSend() {
  const now = Date.now();
  recentSendsMinute.push(now);
  recentSendsHour.push(now);
}

// Write a validated envelope to STDOUT
function sendEnvelope(type, payload = {}, correlationId = null) {
  try {
    const envelope = createEnvelope(type, payload, correlationId);
    const line = serializeEnvelope(envelope);
    process.stdout.write(line);
  } catch (err) {
    logDiagnostic('IPC_ERROR', `Failed serializing envelope to stdout: ${err.message}`);
  }
}

// Random human-paced delay between 2 and 6 seconds
function randomHumanDelay() {
  const ms = 2000 + Math.floor(Math.random() * 4000);
  return new Promise((resolve) => setTimeout(resolve, ms));
}

let connectionHandle = null;

async function handleOutboundSend(payload, correlationId) {
  const recipientJid = payload.recipient_jid || payload.target_jid || payload.recipient;
  const text = payload.text;

  if (!recipientJid || typeof recipientJid !== 'string') {
    sendEnvelope(EventTypes.ERROR, {
      code: 'INVALID_RECIPIENT',
      error: 'Missing or invalid recipient_jid in outbound_send'
    }, correlationId);
    return;
  }

  if (!text || typeof text !== 'string') {
    sendEnvelope(EventTypes.ERROR, {
      code: 'INVALID_TEXT',
      error: 'Missing or invalid text in outbound_send'
    }, correlationId);
    return;
  }

  if (text.length > 4000) {
    sendEnvelope(EventTypes.ERROR, {
      code: 'TEXT_TOO_LONG',
      error: 'Text length exceeds maximum of 4000 characters'
    }, correlationId);
    return;
  }

  // Check rate limit
  if (!checkRateLimit()) {
    logDiagnostic('RATE_LIMIT', 'Outbound rate limit exceeded (rejected)');
    sendEnvelope(EventTypes.ERROR, {
      code: 'RATE_LIMITED',
      error: 'Exceeded maximum permitted sending rate (6/min or 60/hr)'
    }, correlationId);
    return;
  }

  const sock = connectionHandle ? connectionHandle.getSocket() : null;
  if (!sock) {
    sendEnvelope(EventTypes.ERROR, {
      code: 'NOT_CONNECTED',
      error: 'WhatsApp socket is not connected'
    }, correlationId);
    return;
  }

  // Build owner info from socket
  const ownerInfo = sock.user ? {
    id: sock.user.id ? jidNormalizedUser(sock.user.id) : null,
    lid: sock.user.lid ? jidNormalizedUser(sock.user.lid) : null
  } : null;

  // Load live allowlist on every send
  const allowlist = loadAllowlist(config.allowlistPath);

  // Check recipient allowlist
  const isAllowed = isAllowedTarget(recipientJid, allowlist, ownerInfo, {
    getPNForLIDSync: (lid) => {
      // Synchronous lid lookup fallback if cached
      return null;
    }
  });

  if (!isAllowed) {
    logDiagnostic('SECURITY', 'Blocked outbound message to unauthorized destination');
    sendEnvelope(EventTypes.ERROR, {
      code: 'TARGET_NOT_ALLOWED',
      error: 'Recipient is not present in allowlist and is not the owner'
    }, correlationId);
    return;
  }

  try {
    // 1. Human-paced delay (2 - 6 seconds)
    await randomHumanDelay();

    // 2. Composing presence update
    try {
      await sock.sendPresenceUpdate('composing', recipientJid);
    } catch (_err) {
      // Non-fatal if presence update fails
    }

    // 3. Dispatch text message
    const sendResult = await sock.sendMessage(recipientJid, { text });
    recordSend();

    logDiagnostic('OUTBOUND', 'Message successfully dispatched');
    sendEnvelope(EventTypes.OUTBOUND_ACK, {
      status: 'sent',
      message_id: sendResult?.key?.id || 'unknown',
      timestamp: Math.floor(Date.now() / 1000)
    }, correlationId);
  } catch (err) {
    logDiagnostic('SEND_ERROR', `Failed sending WhatsApp message: ${err.message}`);
    sendEnvelope(EventTypes.ERROR, {
      code: 'SEND_FAILED',
      error: err.message
    }, correlationId);
  }
}

// Setup line reader for STDIN IPC
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

rl.on('line', async (line) => {
  if (!line || !line.trim()) return;

  let envelope;
  try {
    envelope = parseLine(line);
  } catch (err) {
    logDiagnostic('PROTOCOL_ERROR', `Malformed line on STDIN: ${err.message}`);
    sendEnvelope(EventTypes.ERROR, {
      code: 'PROTOCOL_ERROR',
      error: err.message
    });
    return;
  }

  const { type, payload, correlation_id } = envelope;

  switch (type) {
    case EventTypes.SHUTDOWN:
      logDiagnostic('LIFECYCLE', 'Shutdown request received from Python supervisor');
      if (connectionHandle) {
        connectionHandle.close();
      }
      process.exit(0);
      break;

    case EventTypes.HEARTBEAT:
      sendEnvelope(EventTypes.HEARTBEAT, { ack: true }, correlation_id);
      break;

    case EventTypes.OUTBOUND_SEND:
      await handleOutboundSend(payload, correlation_id);
      break;

    default:
      logDiagnostic('PROTOCOL_WARN', `Unsupported command type: ${type}`);
      sendEnvelope(EventTypes.ERROR, {
        code: 'UNKNOWN_COMMAND',
        error: `Unsupported command type '${type}'`
      }, correlation_id);
      break;
  }
});

// Setup Baileys Connection
async function main() {
  logDiagnostic('STARTUP', 'Starting WhatsApp bridge', {
    authDir: config.authDir,
    allowlist: config.allowlistPath
  });

  try {
    connectionHandle = await createWhatsAppConnection({
      authDir: config.authDir,
      onEvent: (eventType, eventPayload) => {
        sendEnvelope(eventType, eventPayload);
      },
      onMessageUpsert: (upsert, sock) => {
        // Only accept messages with type notify
        if (upsert.type !== 'notify') return;

        // CVE-2026-48063 mitigation: drop placeholder requestId
        if (upsert.requestId) return;

        const ownerPn = sock.user?.id ? jidNormalizedUser(sock.user.id) : null;
        const ownerLid = sock.user?.lid ? jidNormalizedUser(sock.user.lid) : null;

        for (const rawMsg of upsert.messages || []) {
          const normalized = normalizeMessage(rawMsg, {
            startTime: bridgeStartTime,
            now: Math.floor(Date.now() / 1000),
            ownerPn,
            ownerLid
          });

          if (normalized) {
            sendEnvelope(EventTypes.INBOUND_MESSAGE, normalized);
          }
        }
      }
    });
  } catch (err) {
    logDiagnostic('FATAL', `Failed initializing WhatsApp connection: ${err.message}`);
    process.exit(1);
  }
}

// Graceful process exit
process.on('SIGINT', () => {
  logDiagnostic('LIFECYCLE', 'Received SIGINT, closing bridge');
  if (connectionHandle) connectionHandle.close();
  process.exit(0);
});

process.on('SIGTERM', () => {
  logDiagnostic('LIFECYCLE', 'Received SIGTERM, closing bridge');
  if (connectionHandle) connectionHandle.close();
  process.exit(0);
});

// Run main loop only when executed directly
if (process.argv[1] && process.argv[1].endsWith('index.js')) {
  main().catch((err) => {
    logDiagnostic('FATAL', `Unhandled error in main: ${err.message}`);
    process.exit(1);
  });
}
