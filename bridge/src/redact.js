/**
 * SAGE / AEGIS WhatsApp Bridge -- Log Sanitization & Redaction Helpers
 *
 * Invariant: Never log raw message bodies, full phone numbers, or credentials.
 * Diagnostic logs only contain stanza IDs, event types, error codes, and masked phone tails.
 */

// Forbidden secret patterns to strip or replace
const SECRET_REGEX = /(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|bot\d+:[A-Za-z0-9_-]+)/g;

/**
 * Mask a phone number or JID, preserving at most the last 2 digits.
 * Example: "15551234567@s.whatsapp.net" -> "***67@s.whatsapp.net"
 *          "15551234567" -> "***67"
 */
export function redactPhone(phone) {
  if (!phone || typeof phone !== 'string') return '[empty]';
  const clean = phone.trim();
  const atIdx = clean.indexOf('@');
  const digitsPart = atIdx !== -1 ? clean.slice(0, atIdx) : clean;
  const suffix = atIdx !== -1 ? clean.slice(atIdx) : '';

  if (digitsPart.length <= 2) {
    return `**${suffix}`;
  }
  return `***${digitsPart.slice(-2)}${suffix}`;
}

/**
 * Strips known token/key patterns and replaces them with [REDACTED_SECRET].
 */
export function redactSecrets(text) {
  if (typeof text !== 'string') return text;
  return text.replace(SECRET_REGEX, '[REDACTED_SECRET]');
}

/**
 * Sanitizes an object before writing to diagnostic stderr logs.
 * Deeply strips keys like 'text', 'message', 'conversation', 'body', 'key', 'creds'.
 */
export function sanitizeLogObject(obj) {
  if (obj === null || obj === undefined) return obj;
  if (typeof obj !== 'object') {
    return typeof obj === 'string' ? redactSecrets(obj) : obj;
  }
  if (Array.isArray(obj)) {
    return obj.map(sanitizeLogObject);
  }

  const sanitized = {};
  for (const [k, v] of Object.entries(obj)) {
    // Drop message contents entirely from log metadata
    if (/^(text|message|conversation|body|extendedTextMessage|content|creds|keys)$/i.test(k)) {
      sanitized[k] = '[REDACTED_BODY]';
      continue;
    }
    if (/^(phone|jid|sender|recipient|remoteJid|chat_jid|sender_pn)$/i.test(k) && typeof v === 'string') {
      sanitized[k] = redactPhone(v);
      continue;
    }
    sanitized[k] = sanitizeLogObject(v);
  }
  return sanitized;
}

/**
 * Writes a sanitized diagnostic line to process.stderr.
 * STDOUT is strictly reserved for the Python wire protocol.
 */
export function logDiagnostic(tag, msg, extra = null) {
  const safeMsg = redactSecrets(msg);
  const prefix = `[${new Date().toISOString()}] [${tag}] ${safeMsg}`;
  if (extra) {
    const safeExtra = JSON.stringify(sanitizeLogObject(extra));
    process.stderr.write(`${prefix} ${safeExtra}\n`);
  } else {
    process.stderr.write(`${prefix}\n`);
  }
}
