# SAGE / AEGIS WhatsApp Bridge (Baileys)

An isolated Node.js sidecar worker bridging WhatsApp Web (`@whiskeysockets/baileys`) and the SAGE Python supervisor using JSON-Lines IPC over stdio.

---

> [!WARNING]
> **KNOWN SECURITY GAP: UNENCRYPTED CREDENTIALS ON DISK**  
> Baileys multi-file auth state stores cryptographic secrets (noise keys, identity keys, pre-keys, app-state sync keys) unencrypted as JSON files inside `~/.aegis/whatsapp_session/`.  
> While restricted by OS ACLs and excluded from Git, this plaintext storage is a known architectural gap. In the next milestone, session keys will migrate into the SQLCipher/DPAPI encrypted database.

---

## Prerequisites

- **Node.js**: `>= 22.0.0` (Tested on `v24.19.0`)
- **OS**: Windows 11 / Linux

---

## 1. Installation

From the `bridge/` directory:

```bash
cd bridge
npm ci
```

---

## 2. Running Unit Tests

Execute the offline unit test suite (32 tests, zero network access required):

```bash
cd bridge
npm test
```

---

## 3. Pairing a Number (Offline by default, pair by hand)

By default, the bridge makes no network connections until started. To pair with a spare number:

### Option A: 8-Digit Pairing Code (Recommended)
Set the phone number in `AEGIS_PAIRING_PHONE` (digits only, including country code):

```powershell
$env:AEGIS_PAIRING_PHONE="15551234567"
node src/index.js --auth-dir "$HOME/.aegis/whatsapp_session"
```
The terminal will display the 8-digit pairing code on STDERR. Enter this code on your phone in WhatsApp under **Linked Devices > Link with phone number instead**.

### Option B: Terminal QR Code
If `AEGIS_PAIRING_PHONE` is unset, a terminal QR code will be rendered on STDERR upon startup. Scan it from **WhatsApp > Linked Devices > Link a Device**.

---

## 4. Running the Bridge

### Standalone (Manual Stdio Interaction)
```bash
node src/index.js --auth-dir "$HOME/.aegis/whatsapp_session" --allowlist "$HOME/.aegis/whatsapp_allowlist.json"
```

### Supervised by Python (SAGE Gateway)
The Python class `BaileysSidecarSupervisor` (`src/gateway/baileys_sidecar.py`) launches this script as a child process:

```python
from src.gateway.baileys_sidecar import BaileysSidecarSupervisor

supervisor = BaileysSidecarSupervisor(
    node_script_path="bridge/src/index.js",
    auth_dir="~/.aegis/whatsapp_session",
    auto_start=True
)
```

---

## 5. Stdio Protocol Summary

- **STDOUT**: Reserved **EXCLUSIVELY** for single-line JSON envelopes (`version: "1.0"`).
- **STDERR**: Reserved strictly for diagnostic logs, pairing codes, and QR displays.
- **STDIN**: Accepts commands (`shutdown`, `heartbeat`, `outbound_send`).
