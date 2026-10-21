# Phase 9B: SAGE Remote-Control Gateway Contract Audit

**Document Type:** Security Audit & Architecture Contract Evaluation  
**Status:** COMPLETE  
**Auditor:** Senior Software Architect & Security Systems Engineer  
**Date:** 2026-10-18  
**Scope:** Phase 9A Gateway Implementation (`src/gateway/`, `src/notifications/`, `src/config/`, `migrations/0009_channel_gateway_schema.sql`, related tests, specifications, and project records)  

---

## 1. Executive Summary

A comprehensive architectural and security audit of the **Phase 9A Secure Remote-Control Gateway (Telegram-First)** was conducted against the canonical AEGIS/SAGE architectural specifications and verified against source code and test implementations.

### Key Audit Conclusions:
1. **Core Architectural Foundation is Sound**:
   - Clean, modular separation between `ChannelGateway`, `GatewayStore`, `CommandRouter`, and `TelegramTransport`.
   - Inbound message deduplication is durably enforced in SQLite (`migrations/0009_channel_gateway_schema.sql`) using atomic `BEGIN IMMEDIATE` transactions.
   - Outbound delivery failures correctly transition messages to `UNCERTAIN` without corrupting internal task state or rolling back completed SAGE executions.
   - Zero-secret invariants are programmatically enforced across inbound updates, router responses, outbound chunks, and store queries.
   - Non-private chat types (groups, supergroups, channels) and bot accounts are rejected fail-closed before command parsing or model inference.
2. **Critical Documentation-to-Code Discrepancies**:
   - **HMAC Claim Discrepancy**: Previous documentation and changelog entries claimed "constant-time HMAC token validation". **Audit finding**: There is **no HMAC verification in `src/gateway/`**. The code simply checks that the bot token environment variable is non-empty and passes it in the HTTPS URL to Telegram Bot API.
   - **Webhook Claim Discrepancy**: Documentation referenced webhook support. **Audit finding**: The gateway implements **long-polling only** via `HttpTelegramTransport.get_updates`; there is no webhook endpoint, listener, or secret-token validation header (`X-Telegram-Bot-Api-Secret-Token`).
   - **Command Set Discrepancy**: Documentation claimed `/start`, `/briefing`, `/approve`, `/deny`, `/task`, `/cancel`, `/pause`, `/resume`. **Audit finding**: The actual code in `src/gateway/router.py` implements `/help`, `/status`, `/tasks`, `/inspect`, `/verify`, `/diagnose`, `/task`, `/confirm`, `/cancel` (alias `/stop`), `/approve`, `/reject`. Notably, **sending `/start` (standard Telegram default) currently results in an error**. Commands `/pause`, `/resume`, and `/briefing` do not exist as top-level router commands.
3. **Security & Operability Gaps for WhatsApp Expansion**:
   - `TelegramConfig` only defines `allowed_user_ids`. There is no `allowed_chat_ids`.
   - Outbound message controller does not validate against negative (group) destination IDs.
   - Rate limiting is purely in-memory (resets on process restart) and sends back a notification for every rejected message, creating potential outbound spam amplification.
   - No recovery routine exists to clear or resume in-flight `PROCESSING` updates or `UNCERTAIN` outbound messages after a process crash.
   - `TelegramDeliveryAdapter` in `src/notifications/adapters.py` has **zero test coverage**.

---

## 2. Component-by-Component Verification Matrix

The audit evaluated 14 critical dimensions of the gateway implementation. Each item is classified by verification status:
- **Verified in Source**: Inspected in `src/` and matches code behavior.
- **Covered by Tests**: Validated with explicit offline automated test assertions.
- **Missing or Unsafe**: Identified security weakness, missing control, or incorrect claim.

| # | Dimension | Source Reference | Test Reference | Verification Status | Audit Finding |
|---|---|---|---|---|---|
| 1 | **Transport Abstraction** | [`src/gateway/transport.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/transport.py#L33) (`TelegramTransport`) | [`tests/test_remote_gateway.py:177`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L177) (`test_01`) | **Covered by Tests** | Decoupled ABC with `MockTelegramTransport` and `HttpTelegramTransport`. However, method signatures (`get_updates(offset)`) are Telegram-specific rather than channel-neutral. |
| 2 | **Update Ingestion & ID Extraction** | [`src/gateway/telegram.py:38`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/telegram.py#L38) (`TelegramAdapter.parse_update`) | [`tests/test_remote_gateway.py:290`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L290) (`test_07`) | **Covered by Tests** | Safely extracts integer `update_id`, `chat.id`, `from.id`, drops non-message updates, and enforces maximum text length (4096). |
| 3 | **Sender & Chat Allowlists** | [`src/gateway/gateway.py:137`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/gateway.py#L137) | [`tests/test_remote_gateway.py:216`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L216) (`test_04`, `test_05`) | **Verified in Source** / **Covered by Tests** | Numeric sender ID verified against `allowed_user_ids`. Rejects unauthorized users without replying. **Gap**: `allowed_chat_ids` is not in config or enforced. |
| 4 | **Group / Channel Rejection** | [`src/gateway/telegram.py:64`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/telegram.py#L64) | [`tests/test_remote_gateway.py:266`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L266) (`test_06`) | **Covered by Tests** | Strictly rejects `chat_type != 'private'` on inbound. **Gap**: Outbound dispatcher does not verify destination chat type or block group IDs. |
| 5 | **Authentication & Secret Validation** | [`src/gateway/gateway.py:81`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/gateway.py#L81), [`src/gateway/transport.py:27`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/transport.py#L27) | [`tests/test_remote_gateway.py:207`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L207) (`test_03`, `test_19`) | **Missing or Unsafe** | **NO HMAC EXISTS** in `src/gateway/`. Long polling only (no webhook). Secret scrubbing is present and verified (`_check_no_secrets`, token regex). |
| 6 | **Rate Limiting & Abuse Protection** | [`src/gateway/rate_limiter.py:15`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/rate_limiter.py#L15) | [`tests/test_remote_gateway.py:604`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L604) (`test_18`) | **Verified in Source** / **Covered by Tests** | In-memory sliding window rate limiter. **Gap**: State is lost on restart; auto-response sent on rate limit may cause outbound flood. |
| 7 | **Persistent Deduplication** | [`src/gateway/store.py:46`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/store.py#L46) (`GatewayStore`) | [`tests/test_remote_gateway.py:320`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L320) (`test_08`) | **Covered by Tests** | Backed by SQLite `UNIQUE(channel, channel_update_id)`. Duplicates safely ignored without side effects. |
| 8 | **Command Parsing & Dispatch** | [`src/gateway/router.py:61`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/router.py#L61) (`CommandRouter`) | [`tests/test_remote_gateway.py:406`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L406) (`test_11`, `test_12`, `test_13`) | **Missing or Unsafe** | Strict whitelist blocks arbitrary execution. **Gap**: Mismatch with documented commands; `/start` returns error; `/pause`, `/resume`, `/briefing` missing. |
| 9 | **Approval Lifecycle Integration** | [`src/gateway/router.py:481`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/router.py#L481) | [`tests/test_remote_gateway.py:520`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L520) (`test_15`) | **Covered by Tests** | Integrates with `ApprovalStore` for `/approve` and `/reject`. **Gap**: Outbound messages have NO approval gate (critical requirement for WhatsApp). |
| 10 | **Supervisor & Trust Core Wiring** | [`src/gateway/router.py:47`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/router.py#L47), [`src/skills/first_party.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/skills/first_party.py) | [`tests/test_remote_gateway.py:480`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L480) (`test_14`, `test_20`) | **Covered by Tests** | Submits goals, builds briefings, enforces confirmation gate, executes skills through `BoundSkillToolBroker` with `actor="telegram"`. |
| 11 | **Notification Handling** | [`src/notifications/adapters.py:139`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/notifications/adapters.py#L139) | None | **Missing or Unsafe** | `TelegramDeliveryAdapter` implemented but **not registered** in default factory and has **zero automated tests**. |
| 12 | **Error Handling & Redaction** | [`src/gateway/errors.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/errors.py), [`src/gateway/transport.py:27`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/transport.py#L27) | [`tests/test_remote_gateway.py:207`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L207) (`test_03`, `test_19`) | **Covered by Tests** | Clean error hierarchy, tokens scrubbed via `_redact_token`, zero secrets enforced via `_check_no_secrets`. |
| 13 | **Restart Behavior & Recovery** | [`src/gateway/gateway.py:288`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/gateway.py#L288), [`src/gateway/store.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/store.py) | [`tests/test_remote_gateway.py:578`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py#L578) (`test_17`) | **Missing or Unsafe** | Outbound errors mark `UNCERTAIN`. **Gap**: No startup reconciliation to recover orphaned `PROCESSING` updates or unverified `UNCERTAIN` sends. Polling offset lost on restart. |
| 14 | **Test Coverage & Failure Cases** | [`tests/test_remote_gateway.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py) (21 tests) | All 21 tests pass | **Verified in Source** | 21 offline tests pass in 1.26s. Gaps identified in Section 4. |

---

## 3. Detailed Audit Findings

### Finding 1 [HIGH SEVERITY]: Inaccurate HMAC Security Claims in Prior Documentation
- **Claimed in Prompt 032 State / Changelog**: "constant-time HMAC token validation (`hmac.compare_digest`)".
- **Actual Implementation**: In [`src/gateway/gateway.py:81-86`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/gateway.py#L81-L86), the gateway merely checks:
  ```python
  token = os.environ.get(self._config.bot_token_env)
  if not token or not token.strip():
      raise ChannelConfigError(...)
  ```
  The token is never compared using HMAC against any request payload or signature. In Telegram long-polling, Telegram does not sign payloads with HMAC; it serves updates over an established TLS connection authenticated by the bot token. Describing the gateway as possessing "constant-time HMAC token validation" is technically inaccurate.
- **Remediation**:
  1. Correct documentation to clarify that authentication relies on Telegram TLS transport authentication using the secret bot token, plus numeric sender ID allowlisting.
  2. If webhook support is introduced in the future, implement true HMAC/constant-time token verification using `X-Telegram-Bot-Api-Secret-Token` and `hmac.compare_digest`.

### Finding 2 [MEDIUM SEVERITY]: Command Set Mismatch & Missing `/start` Handler
- **Documented Commands**: `/start`, `/help`, `/status`, `/briefing`, `/approve`, `/deny`, `/task`, `/cancel`, `/pause`, `/resume`.
- **Actual Implemented Commands**: In [`src/gateway/router.py:82-103`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/router.py#L82-L103):
  `/help`, `/status`, `/tasks`, `/inspect`, `/verify`, `/diagnose`, `/task`, `/confirm`, `/cancel` (or `/stop`), `/approve`, `/reject`.
- **Impact**:
  - In Telegram, tapping "Start" sends `/start`. SAGE currently replies: `"Unknown or unsupported command '/start'..."`.
  - Commands `/pause`, `/resume`, `/briefing`, and `/deny` return errors or do not exist as top-level commands.
- **Remediation**: Add `/start` (alias to `/help` with personalized greeting) and aliases `/deny` -> `/reject`. Add `/briefing` to inspect current briefings.

### Finding 3 [MEDIUM SEVERITY]: Zero Test Coverage for Notification Delivery Adapter
- **Implementation**: [`TelegramDeliveryAdapter`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/notifications/adapters.py#L139) was added to `src/notifications/adapters.py`.
- **Audit Finding**: Grep search confirms `TelegramDeliveryAdapter` is not tested in [`tests/test_notifications.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_notifications.py) or [`tests/test_remote_gateway.py`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/tests/test_remote_gateway.py).
- **Remediation**: Add dedicated unit tests in `tests/test_notifications.py` verifying formatting, transport error absorption, and priority handling.

### Finding 4 [MEDIUM SEVERITY]: Outbound Group Destination Blocking Missing
- **Inbound**: Inbound updates from groups are strictly rejected in [`src/gateway/telegram.py:64`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/telegram.py#L64).
- **Outbound**: [`ChannelGateway._dispatch_outbound`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/gateway.py#L263) dispatches directly to `recipient_id`. If `recipient_id` is a group ID (negative integer in Telegram, or `@g.us` in WhatsApp), no validation occurs.
- **Remediation**: Add explicit destination validation in `_dispatch_outbound` that fails closed if a group or channel destination is targeted.

### Finding 5 [LOW SEVERITY]: Polling Offset and In-Flight State Ephemerality
- **Polling Offset**: `_next_offset` in [`ChannelGateway`](file:///c:/Users/digvi/OneDrive/Desktop/project/aeigs/src/gateway/gateway.py#L56) is an in-memory integer. If SAGE restarts, `_next_offset` resets to `None`. While SQLite deduplication stops duplicate tasks, Telegram will redeliver updates until acknowledged.
- **In-Flight State**: If a crash occurs during step 6 (`router.route`), the update remains in `state = 'PROCESSING'` in `gateway_updates` indefinitely.
- **Remediation**: Persist polling offset in SQLite metadata; add startup reconciliation that marks stalled `PROCESSING` updates as `FAILED` or re-queues them safely.

---

## 4. WhatsApp Integration Compatibility Assessment

The current gateway architecture provides a strong foundation for WhatsApp, but several components cannot be used as-is:

1. **Transport Incompatibility**:
   - `TelegramTransport` assumes HTTP polling (`offset`, `timeout`).
   - WhatsApp (Baileys) is a stateful WebSocket connection over a persistent event loop. A separate transport protocol is required.
2. **Identity Incompatibility**:
   - Telegram uses numeric integer user IDs (`123456789`).
   - WhatsApp uses Jabber IDs (JIDs) such as `[country_code][number]@s.whatsapp.net` for individual contacts and `[creator]-[timestamp]@g.us` for groups.
   - `sender_id` in SQLite is `TEXT`, which accommodates JIDs, but `TelegramConfig.allowed_user_ids` is `List[int]`. WhatsApp configuration requires `allowed_jids: List[str]` or E.164 phone numbers.
3. **Outbound Approval Incompatibility**:
   - Telegram Phase 9A auto-dispatches responses directly back to the requester.
   - WhatsApp requirements dictate that **all AI-generated outbound messages must require explicit human approval bound to exact recipient and exact content**. The existing gateway has no outbound approval stage.
4. **Channel Affinity**:
   - SAGE specification § 3 mandates channel affinity: WhatsApp-originated requests reply on WhatsApp; PC-originated requests reply on PC. Cross-channel notifications require explicit user policy. The gateway router must preserve originating channel context.
