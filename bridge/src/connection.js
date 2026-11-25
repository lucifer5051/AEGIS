/**
 * SAGE / AEGIS WhatsApp Bridge -- Connection & Socket Lifecycle
 *
 * Manages Baileys socket creation, credential loading, pairing (QR & 8-digit code),
 * and exponential-backoff reconnects.
 *
 * Invariant: Never write secrets or raw QR/pairing codes to STDOUT.
 * STDOUT is reserved exclusively for the Python wire protocol.
 */

import os from 'node:os';
import path from 'node:path';
import pino from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  Browsers
} from '@whiskeysockets/baileys';
import { logDiagnostic, redactPhone } from './redact.js';

export const DEFAULT_AUTH_DIR = path.join(os.homedir(), '.aegis', 'whatsapp_session');
export const MAX_RECONNECT_ATTEMPTS = 5;

/**
 * Calculates exponential backoff delay with random jitter.
 * Base 1s, factor 1.5, cap 60s.
 */
export function calculateBackoff(attempt) {
  const baseDelay = 1000 * Math.pow(1.5, Math.max(0, attempt));
  const jitter = Math.random() * 1000;
  return Math.min(60000, Math.floor(baseDelay + jitter));
}

/**
 * Establishes and manages the Baileys socket connection.
 */
export async function createWhatsAppConnection(options = {}) {
  const authDir = options.authDir || DEFAULT_AUTH_DIR;
  const onEvent = options.onEvent || (() => {});
  const onMessageUpsert = options.onMessageUpsert || (() => {});

  let consecutiveFailures = 0;
  let sock = null;
  let pairingCodeRequested = false;

  // Silence verbose Baileys logging; diagnostics are routed to STDERR
  const logger = pino({
    level: options.logLevel || 'warn',
    serializers: {
      err: (err) => ({ message: err.message, code: err.code })
    }
  }, pino.destination({ fd: 2, sync: true })); // STDERR

  async function connect() {
    logDiagnostic('SOCKET', 'Initializing Baileys authentication state', { authDir });
    const { state, saveCreds } = await useMultiFileAuthState(authDir);

    let version;
    try {
      const v = await fetchLatestBaileysVersion();
      version = v.version;
    } catch (_err) {
      logDiagnostic('SOCKET', 'Failed to fetch latest WA version, using default');
      version = [2, 3000, 1015901307];
    }

    sock = makeWASocket({
      version,
      auth: state,
      printQRInTerminal: false, // Deprecated; handled explicitly via qr event below
      markOnlineOnConnect: false,
      syncFullHistory: false,
      generateHighQualityLinkPreview: false,
      getMessage: async () => undefined,
      logger,
      browser: Browsers.ubuntu('Chrome')
    });

    // Save auth credentials whenever updated
    sock.ev.on('creds.update', saveCreds);

    // Connection lifecycle and state updates
    sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      // Handle QR code rendering to STDERR
      if (qr) {
        logDiagnostic('PAIRING', 'QR code generated for pairing');
        process.stderr.write('\nScan this QR code with WhatsApp:\n');
        qrcodeTerminal.generate(qr, { small: true }, (ascii) => {
          process.stderr.write(`${ascii}\n`);
        });
      }

      // Handle phone-number pairing code
      const pairingPhone = process.env.AEGIS_PAIRING_PHONE;
      if (
        !sock.authState.creds.registered &&
        pairingPhone &&
        !pairingCodeRequested &&
        !qr
      ) {
        pairingCodeRequested = true;
        const cleanPhone = pairingPhone.replace(/\D/g, '');
        logDiagnostic('PAIRING', `Requesting pairing code for phone: ${redactPhone(cleanPhone)}`);
        try {
          // Delay briefly to allow socket handshake
          setTimeout(async () => {
            try {
              const code = await sock.requestPairingCode(cleanPhone);
              process.stderr.write(`\n=============================\n`);
              process.stderr.write(`WHATSAPP PAIRING CODE: ${code}\n`);
              process.stderr.write(`=============================\n\n`);
            } catch (err) {
              logDiagnostic('PAIRING_ERROR', `Pairing code request failed: ${err.message}`);
            }
          }, 3000);
        } catch (err) {
          logDiagnostic('PAIRING_ERROR', `Failed scheduling pairing code: ${err.message}`);
        }
      }

      if (connection === 'open') {
        consecutiveFailures = 0;
        logDiagnostic('SOCKET', 'Connection successfully established (OPEN)', {
          user: sock.user ? sock.user.id : 'unknown'
        });
        onEvent('connection_update', {
          status: 'open',
          user: sock.user ? { id: sock.user.id, lid: sock.user.lid } : null
        });
      } else if (connection === 'close') {
        const error = lastDisconnect?.error;
        const statusCode = error?.output?.statusCode;
        logDiagnostic('SOCKET', `Connection closed with status ${statusCode || 'unknown'}`, {
          error: error?.message
        });

        onEvent('connection_update', {
          status: 'close',
          statusCode: statusCode || null,
          error: error?.message || 'Connection closed'
        });

        // 401: Logged out -> Do NOT reconnect; fail closed and exit with code 40
        if (statusCode === DisconnectReason.loggedOut) {
          logDiagnostic('SOCKET', 'Session was logged out by WhatsApp server. Exiting.');
          process.exit(40);
        }

        consecutiveFailures += 1;
        if (consecutiveFailures > MAX_RECONNECT_ATTEMPTS) {
          logDiagnostic(
            'SOCKET',
            `Exceeded maximum reconnect attempts (${MAX_RECONNECT_ATTEMPTS}). Halting.`,
            { consecutiveFailures }
          );
          process.exit(41);
        }

        const delay = calculateBackoff(consecutiveFailures - 1);
        logDiagnostic('SOCKET', `Reconnecting in ${delay}ms (attempt ${consecutiveFailures})`);
        setTimeout(() => {
          connect().catch((err) => {
            logDiagnostic('SOCKET', `Reconnect attempt failed: ${err.message}`);
          });
        }, delay);
      } else if (connection === 'connecting') {
        onEvent('connection_update', { status: 'connecting' });
      }
    });

    // Inbound messages
    sock.ev.on('messages.upsert', (upsert) => {
      onMessageUpsert(upsert, sock);
    });

    return sock;
  }

  await connect();
  return {
    getSocket: () => sock,
    close: () => {
      if (sock) {
        sock.end(undefined);
      }
    }
  };
}
