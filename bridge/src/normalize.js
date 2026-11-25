/**
 * SAGE / AEGIS WhatsApp Bridge -- Pure Message Normalizer
 *
 * Normalizes raw Baileys WAMessage events into strict, safe event payloads.
 * Drops groups, broadcasts, media, reactions, protocol messages, and stale backlog.
 */

import {
  normalizeMessageContent,
  jidNormalizedUser,
  isJidGroup,
  isJidBroadcast,
  isLidUser
} from '@whiskeysockets/baileys';

export const MAX_TEXT_LENGTH = 4000;
export const MAX_BACKLOG_AGE_SECONDS = 120;

/**
 * PURE normalization function.
 *
 * @param {object} rawMsg - Baileys WAMessage
 * @param {object} options
 * @param {number} [options.startTime] - Bridge startup timestamp in epoch seconds
 * @param {number} [options.now] - Current epoch seconds for testing
 * @param {string} [options.ownerPn] - Owner's phone-number JID (e.g. 15551234567@s.whatsapp.net)
 * @param {string} [options.ownerLid] - Owner's LID JID (e.g. 1234567890@lid)
 * @param {object} [options.lidMapping] - Optional LID mapping resolver or cache
 * @returns {object|null} Normalized message record, or null if ignored
 */
export function normalizeMessage(rawMsg, options = {}) {
  if (!rawMsg || typeof rawMsg !== 'object') return null;

  const key = rawMsg.key;
  if (!key || typeof key !== 'object') return null;

  const id = key.id;
  const remoteJid = key.remoteJid;
  if (!id || typeof id !== 'string' || !remoteJid || typeof remoteJid !== 'string') {
    return null;
  }

  // 1. CVE-2026-48063 mitigation: drop placeholder requestId payloads
  if (
    rawMsg.requestId ||
    key.requestId ||
    (rawMsg.message && rawMsg.message.requestId)
  ) {
    return null;
  }

  const cleanRemote = remoteJid.trim().toLowerCase();
  const cleanAlt = key.remoteJidAlt ? String(key.remoteJidAlt).trim().toLowerCase() : null;
  const participant = key.participant ? String(key.participant).trim().toLowerCase() : null;

  // 2. Reject groups, broadcasts, status, and newsletters
  if (
    isJidGroup(cleanRemote) ||
    isJidBroadcast(cleanRemote) ||
    cleanRemote.endsWith('@g.us') ||
    cleanRemote.includes('@broadcast') ||
    cleanRemote.endsWith('@newsletter') ||
    (participant && (participant.endsWith('@g.us') || participant.includes('@broadcast'))) ||
    (cleanAlt && (cleanAlt.endsWith('@g.us') || cleanAlt.includes('@broadcast')))
  ) {
    return null;
  }

  // 3. Reject stale messages older than 120s relative to bridge start
  const now = options.now || Math.floor(Date.now() / 1000);
  const startTime = options.startTime || now;
  let msgTime = Number(rawMsg.messageTimestamp);
  if (isNaN(msgTime) || msgTime <= 0) {
    msgTime = now;
  }

  if (msgTime < startTime - MAX_BACKLOG_AGE_SECONDS) {
    return null;
  }

  // 4. Unwrap message content (ephemeral, view-once, etc.)
  const content = normalizeMessageContent(rawMsg.message);
  if (!content || typeof content !== 'object') {
    return null;
  }

  // 5. Reject non-text message types
  if (
    content.protocolMessage ||
    content.reactionMessage ||
    content.pollCreationMessage ||
    content.pollCreationMessageV2 ||
    content.pollCreationMessageV3 ||
    content.pollUpdateMessage ||
    content.imageMessage ||
    content.videoMessage ||
    content.audioMessage ||
    content.documentMessage ||
    content.documentWithCaptionMessage ||
    content.stickerMessage ||
    content.contactMessage ||
    content.contactsArrayMessage ||
    content.locationMessage ||
    content.liveLocationMessage ||
    content.callMessage
  ) {
    return null;
  }

  // 6. Extract text strictly from conversation or extendedTextMessage
  let rawText = null;
  if (typeof content.conversation === 'string') {
    rawText = content.conversation;
  } else if (
    content.extendedTextMessage &&
    typeof content.extendedTextMessage.text === 'string'
  ) {
    rawText = content.extendedTextMessage.text;
  }

  if (!rawText || !rawText.trim()) {
    return null;
  }

  // 7. Text length capping (4000 characters) - pass verbatim without executing/modifying
  let text = rawText;
  let truncated = false;
  if (text.length > MAX_TEXT_LENGTH) {
    text = text.slice(0, MAX_TEXT_LENGTH);
    truncated = true;
  }

  // 8. Sender phone number resolution
  let senderPn = null;
  if (cleanRemote.endsWith('@s.whatsapp.net')) {
    senderPn = jidNormalizedUser(cleanRemote);
  } else if (cleanAlt && cleanAlt.endsWith('@s.whatsapp.net')) {
    senderPn = jidNormalizedUser(cleanAlt);
  } else if (options.lidMapping && typeof options.lidMapping.getPNForLIDSync === 'function') {
    senderPn = options.lidMapping.getPNForLIDSync(cleanRemote);
  }

  // 9. Self-chat resolution
  const normRemote = jidNormalizedUser(cleanRemote);
  const ownerPn = options.ownerPn ? jidNormalizedUser(options.ownerPn.toLowerCase()) : null;
  const ownerLid = options.ownerLid ? jidNormalizedUser(options.ownerLid.toLowerCase()) : null;

  let isSelfChat = false;
  if (ownerPn && normRemote === ownerPn) {
    isSelfChat = true;
  } else if (ownerLid && normRemote === ownerLid) {
    isSelfChat = true;
  }

  const fromMe = Boolean(key.fromMe);

  return {
    id: String(id),
    timestamp: msgTime,
    chat_jid: cleanRemote,
    alt_jid: cleanAlt,
    sender_pn: senderPn,
    from_me: fromMe,
    is_self_chat: isSelfChat,
    text,
    truncated,
    // Compatibility fields for Python WhatsAppAdapter
    key: {
      remoteJid: cleanRemote,
      id: String(id),
      fromMe,
      participant: participant || undefined
    },
    message: {
      conversation: text
    },
    messageTimestamp: msgTime,
    remoteJid: cleanRemote
  };
}
