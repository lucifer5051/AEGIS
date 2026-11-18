# SAGE Desktop Watcher (`sage-desktop-watcher`)

High-performance native Windows desktop awareness and UI Automation (UIA) observer daemon for SAGE (System for Autonomous Guidance & Execution).

---

## Architecture Overview

`sage-desktop-watcher` provides SAGE with continuous, event-driven desktop perception and precise semantic UI interaction.

- **Non-blocking Event Hooking**: Uses Win32 `SetWinEventHook` on an MTA thread with a message pump to listen for:
  - `EVENT_SYSTEM_FOREGROUND`: Detects active window switching.
  - `EVENT_OBJECT_CREATE`: Detects new top-level windows.
  - `EVENT_OBJECT_DESTROY`: Detects window terminations.
  - `EVENT_OBJECT_FOCUS` (0x8005): Detects focused control and window changes deterministically across all sessions.
- **UI Automation (UIA) Integration**: Uses native WPF `System.Windows.Automation` to inspect element trees, invoke controls (`InvokePattern`), and manipulate values (`ValuePattern`).
- **Synchronous Stdio IPC**: Dispatches JSON commands (`ping`, `inspect`, `invoke`, `set_value`, `click`, `type`, `shortcut`, `screenshot`) with sub-millisecond dispatch overhead.
- **Zero Third-Party Dependencies**: Compiles cleanly against the core .NET SDK using `net9.0-windows` with WPF/Windows Forms frameworks enabled; no third-party NuGet packages required.

---

## Prerequisites

- Windows 10/11 or Windows Server 2019+
- .NET 9.0 SDK (or .NET 9.0 Desktop Runtime)

---

## Build Instructions

```powershell
cd tools/sage-desktop-watcher
dotnet build -c Release
```

The compiled binary will be placed at:
```
tools/sage-desktop-watcher/bin/Release/net9.0-windows/sage-desktop-watcher.exe
```

---

## Manual Testing & Demonstration

You can launch the executable directly in PowerShell to inspect its event stream or issue commands:

```powershell
.\bin\Release\net9.0-windows\sage-desktop-watcher.exe
```

### Example Commands (type into stdin):

```json
{"id": 1, "command": "ping"}
{"id": 2, "command": "inspect", "depth": 2}
{"id": 3, "command": "screenshot"}
{"id": 4, "command": "shutdown"}
```

---

## SAGE Python Integration

The daemon is managed by `src.desktop.client.DesktopObserverClient`. SAGE automatically launches and monitors the watcher, ingests events through `DesktopStateManager`, and registers verified semantic tools with `ToolBroker` and `TrustCore`.

See [PROTOCOL.md](PROTOCOL.md) for full IPC schema and lifecycle details.
