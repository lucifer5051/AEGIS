/**
 * SAGE / AEGIS WhatsApp Bridge -- Outbound Allowlist Verification
 *
 * Enforces outbound recipient whitelist:
 * 1. Owner's own JID is always allowed (for self-chat approvals).
 * 2. Compares target in both phone-number and LID forms.
 * 3. Re-reads allowlist file on demand so live edits take immediate effect.
 * 4. Fails closed (empty set) if file is missing or corrupted.
 */

import fs from 'node:fs';
import { jidNormalizedUser, isJidGroup, isJidBroadcast } from '@whiskeysockets/baileys';

/**
 * Loads and validates the allowlist JSON file.
 * Returns a Set of normalized phone-number JIDs.
 */
export function loadAllowlist(filePath) {
  if (!filePath) return new Set();

  try {
    if (!fs.existsSync(filePath)) {
      return new Set();
    }
    const raw = fs.readFileSync(filePath, 'utf8');
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return new Set();
    }
    const set = new Set();
    for (const item of parsed) {
      if (typeof item === 'string' && item.trim()) {
        const clean = jidNormalizedUser(item.trim().toLowerCase());
        if (!isJidGroup(clean) && !isJidBroadcast(clean)) {
          set.add(clean);
        }
      }
    }
    return set;
  } catch (_err) {
    // Fail closed on error or invalid syntax
    return new Set();
  }
}

/**
 * PURE check determining whether a target JID is permitted for outbound messages.
 *
 * @param {string} targetJid - Destination JID to evaluate
 * @param {Set<string>|Array<string>} allowlist - Permitted contact JIDs
 * @param {object} [ownerInfo] - { id: string, lid?: string }
 * @param {object} [lidMapping] - Optional LID mapping resolver ({ getPNForLIDSync, getLIDForPNSync })
 * @returns {boolean} True if target is allowed; false otherwise
 */
export function isAllowedTarget(targetJid, allowlist, ownerInfo = null, lidMapping = null) {
  if (!targetJid || typeof targetJid !== 'string') return false;

  const cleanTarget = targetJid.trim().toLowerCase();

  // Strictly forbid groups and broadcasts
  if (
    isJidGroup(cleanTarget) ||
    isJidBroadcast(cleanTarget) ||
    cleanTarget.endsWith('@g.us') ||
    cleanTarget.includes('@broadcast')
  ) {
    return false;
  }

  const normTarget = jidNormalizedUser(cleanTarget);

  // 1. Owner is always allowed
  if (ownerInfo) {
    if (ownerInfo.id && normTarget === jidNormalizedUser(ownerInfo.id.toLowerCase())) {
      return true;
    }
    if (ownerInfo.lid && normTarget === jidNormalizedUser(ownerInfo.lid.toLowerCase())) {
      return true;
    }
  }

  const allowedSet = allowlist instanceof Set ? allowlist : new Set(allowlist || []);

  // 2. Direct match against allowlist
  if (allowedSet.has(normTarget)) {
    return true;
  }

  // 3. LID addressing resolution (LID -> PN or PN -> LID)
  if (lidMapping) {
    // If target is an LID, resolve its PN
    if (typeof lidMapping.getPNForLIDSync === 'function') {
      const pn = lidMapping.getPNForLIDSync(normTarget);
      if (pn && allowedSet.has(jidNormalizedUser(pn.toLowerCase()))) {
        return true;
      }
    }
    // If allowlist contains PNs, check if any maps to this LID
    if (typeof lidMapping.getLIDForPNSync === 'function') {
      for (const allowedPn of allowedSet) {
        const lid = lidMapping.getLIDForPNSync(allowedPn);
        if (lid && jidNormalizedUser(lid.toLowerCase()) === normTarget) {
          return true;
        }
      }
    }
  }

  return false;
}
