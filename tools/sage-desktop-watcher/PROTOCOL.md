# SAGE Desktop Watcher IPC Protocol Specification

## 1. Overview

`sage-desktop-watcher` is a native Windows C# .NET background daemon providing low-latency, event-driven desktop awareness and UI Automation (UIA) control to SAGE.

Communication between the SAGE Python runtime (`DesktopObserverClient`) and `sage-desktop-watcher` occurs strictly over standard I/O pipes (`stdin`, `stdout`, `stderr`) using newline-delimited JSON (NDJSON).

- **`stdout`**: Emits asynchronous desktop event notifications and synchronous command responses.
- **`stdin`**: Receives JSON command requests from the SAGE Python runtime.
- **`stderr`**: Receives diagnostic, debug, and non-fatal platform warning logs.

---

## 2. Event Stream Specification

Events are pushed unprompted to `stdout` whenever desktop state transitions occur.

### Event Envelope Schema

```json
{
  "Version": "1.0",
  "EventId": "evt-1700000000.123",
  "EventType": "FOREGROUND_CHANGED",
  "Timestamp": 1700000000.123,
  "ProcessId": 1234,
  "WindowHandle": 65538,
  "SessionGeneration": 1,
  "ProcessName": "notepad.exe",
  "WindowTitle": "Untitled - Notepad",
  "ClassName": "Notepad",
  "Bounds": {
    "X": 100,
    "Y": 100,
    "Width": 800,
    "Height": 600
  }
}
```

### Event Types

| EventType | Trigger Condition | Source |
|---|---|---|
| `OBSERVER_READY` | Daemon initialized and ready to accept commands | Startup handshake |
| `FOREGROUND_CHANGED` | User or system switches the active foreground window | `EVENT_SYSTEM_FOREGROUND` |
| `WINDOW_OPENED` | Top-level window created or made visible | `EVENT_OBJECT_CREATE` |
| `WINDOW_CLOSED` | Top-level window destroyed | `EVENT_OBJECT_DESTROY` |
| `FOCUS_CHANGED` | Keyboard focus moves to a different window or control | `EVENT_OBJECT_FOCUS` (0x8005) & `AutomationFocusChangedEventHandler` |

---

## 3. Synchronous Command Request / Response Protocol

Commands are dispatched as line-delimited JSON objects written to the daemon's `stdin`. Each command includes an integer correlation `id`.

### Command Request Envelope

```json
{
  "id": 1,
  "command": "inspect",
  "hwnd": 65538,
  "depth": 3
}
```

### Supported Commands

#### 1. `ping`
- **Purpose**: Health check and startup verification.
- **Arguments**: None.
- **Result**: `{"pong": true}`

#### 2. `inspect`
- **Purpose**: Traverse the UIA element tree rooted at a window handle or desktop root.
- **Arguments**:
  - `hwnd` (int, default `0` = desktop root)
  - `depth` (int, default `3`, max `10`)
- **Result**: Hierarchical `UIElementNode` JSON with `AutomationId`, `Name`, `ControlType`, `Bounds`, and `Children`.

#### 3. `invoke`
- **Purpose**: Invoke an interactive control using `InvokePattern`.
- **Arguments**:
  - `automation_id` (string)
  - `name` (string, optional)
- **Result**: `{"invoked": true}`

#### 4. `set_value`
- **Purpose**: Set text into an editable control using `ValuePattern`.
- **Arguments**:
  - `value` (string)
  - `automation_id` (string)
  - `name` (string, optional)
- **Result**: `{"value_set": true}`

#### 5. `click`
- **Purpose**: Simulate a mouse click at physical screen coordinates or centered on an element.
- **Arguments**:
  - `x` (int)
  - `y` (int)
  - `automation_id` (string, optional)
- **Result**: `{"clicked": true, "x": int, "y": int}`

#### 6. `type`
- **Purpose**: Send keystrokes to the focused control via `SendKeys`.
- **Arguments**:
  - `text` (string)
- **Result**: `{"typed": true}`

#### 7. `shortcut`
- **Purpose**: Send keyboard combinations (e.g. `^s` for Ctrl+S).
- **Arguments**:
  - `keys` (string)
- **Result**: `{"shortcut_sent": true}`

#### 8. `screenshot`
- **Purpose**: Capture the primary display or target window visual frame.
- **Arguments**:
  - `hwnd` (int, default `0` = virtual screen)
- **Result**: `{"screenshot_base64": "<base64_png>", "bounds": {"X": 0, "Y": 0, "Width": 1920, "Height": 1080}}`

#### 9. `shutdown`
- **Purpose**: Graceful daemon termination.
- **Arguments**: None.
- **Result**: `{"shutdown": true}`

### Command Response Envelope

```json
{
  "Id": 1,
  "Status": "SUCCESS",
  "Result": {
    "pong": true
  }
}
```

On failure:
```json
{
  "Id": 1,
  "Status": "ERROR",
  "Error": "Element with AutomationId 'NonExistent' not found."
}
```

---

## 4. Session Generation Semantics

- Every daemon instance increments or is assigned a `SessionGeneration` counter on startup.
- SAGE's `DesktopElementRegistry` computes reference locality tuples:
  $$\text{Locality} = (\text{SessionGeneration}, \text{HWND}, \text{AutomationId})$$
- When `sage-desktop-watcher` crashes and restarts, `SessionGeneration` increments, immediately invalidating cached element references to prevent stale interaction bugs across process boundaries.

---

## 5. Known Limitations & Fallbacks

1. **UIA Focus Events in Non-Interactive Sessions**: Registering `AddAutomationFocusChangedEventHandler` can fail with `0x8007029C` in headless background processes. The daemon catches this error, logs a warning to `stderr`, and relies on foreground WinEvent hooks and snapshot inspection.
2. **Untrusted UI Text**: All text returned from `inspect` or `screenshot` is treated as untrusted and marked with `untrusted_data: true` before ingestion into reasoning context to defend against indirect prompt injection.
