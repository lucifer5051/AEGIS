using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;

namespace Sage.DesktopWatcher;

public static class Program
{
    private const string ProtocolVersion = "1.0";
    private static readonly int SessionGeneration = 1;
    private static readonly object StdoutLock = new();
    private static readonly ConcurrentQueue<DesktopEvent> EventQueue = new();
    private static volatile bool _running = true;

    // Win32 WinEvent Hook Delegates & P/Invoke
    private delegate void WinEventDelegate(
        IntPtr hWinEventHook, uint eventType, IntPtr hwnd,
        int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);

    private static WinEventDelegate? _winEventProc;
    private static IntPtr _hookForeground = IntPtr.Zero;
    private static IntPtr _hookLifecycle = IntPtr.Zero;
    private static IntPtr _hookFocus = IntPtr.Zero;

    private const uint EVENT_SYSTEM_FOREGROUND = 0x0003;
    private const uint EVENT_OBJECT_CREATE = 0x8000;
    private const uint EVENT_OBJECT_DESTROY = 0x8001;
    private const uint EVENT_OBJECT_FOCUS = 0x8005;
    private const uint WINEVENT_OUTOFCONTEXT = 0x0000;
    private const uint WINEVENT_SKIPOWNPROCESS = 0x0002;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWinEventHook(
        uint eventMin, uint eventMax, IntPtr hmodWinEventProc,
        WinEventDelegate lpfnWinEventProc, uint idProcess, uint idThread, uint dwFlags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnhookWinEvent(IntPtr hWinEventHook);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, UIntPtr dwExtraInfo);

    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    private const uint MOUSEEVENTF_LEFTUP = 0x0004;

    [DllImport("user32.dll")]
    private static extern sbyte GetMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage([In] ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage([In] ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern bool PostThreadMessage(uint idThread, uint msg, IntPtr wParam, IntPtr lParam);

    private const uint WM_QUIT = 0x0012;

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public POINT pt;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT
    {
        public int X;
        public int Y;
    }

    [STAThread]
    public static void Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        Console.InputEncoding = Encoding.UTF8;

        // Thread 1: WinEvent Hook message pump (MTA thread)
        uint winEventThreadId = 0;
        var winEventThread = new Thread(() =>
        {
            winEventThreadId = GetCurrentThreadId();
            InitWinEvents();
            while (_running)
            {
                if (GetMessage(out MSG msg, IntPtr.Zero, 0, 0) > 0)
                {
                    TranslateMessage(ref msg);
                    DispatchMessage(ref msg);
                }
                else
                {
                    break;
                }
            }
            CleanupWinEvents();
        })
        {
            IsBackground = true,
            Name = "WinEventMessagePump"
        };
        winEventThread.SetApartmentState(ApartmentState.MTA);
        winEventThread.Start();

        // Thread 2: UI Automation event listener (MTA thread)
        var uiaThread = new Thread(() =>
        {
            InitUIAEvents();
        })
        {
            IsBackground = true,
            Name = "UIAEventListener"
        };
        uiaThread.SetApartmentState(ApartmentState.MTA);
        uiaThread.Start();

        // Thread 3: Event dispatcher / coalescing loop
        var dispatcherThread = new Thread(DispatchEventsLoop)
        {
            IsBackground = true,
            Name = "EventDispatcher"
        };
        dispatcherThread.Start();

        // Emit READY event to SAGE
        EmitEvent(new DesktopEvent
        {
            EventType = "OBSERVER_READY",
            WindowHandle = 0,
            ProcessId = (int)Process.GetCurrentProcess().Id,
            ProcessName = "sage-desktop-watcher",
            WindowTitle = "SAGE Observer Ready"
        });

        // Main thread: Synchronous command loop over stdin
        RunCommandLoop();

        _running = false;
        if (winEventThreadId != 0)
        {
            PostThreadMessage(winEventThreadId, WM_QUIT, IntPtr.Zero, IntPtr.Zero);
        }
        CleanupUIAEvents();
    }

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    #region WinEvent Hooks
    private static void InitWinEvents()
    {
        _winEventProc = WinEventCallback;
        _hookForeground = SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
            IntPtr.Zero, _winEventProc, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);

        _hookLifecycle = SetWinEventHook(
            EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY,
            IntPtr.Zero, _winEventProc, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);

        _hookFocus = SetWinEventHook(
            EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS,
            IntPtr.Zero, _winEventProc, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
    }

    private static void CleanupWinEvents()
    {
        if (_hookForeground != IntPtr.Zero)
        {
            UnhookWinEvent(_hookForeground);
            _hookForeground = IntPtr.Zero;
        }
        if (_hookLifecycle != IntPtr.Zero)
        {
            UnhookWinEvent(_hookLifecycle);
            _hookLifecycle = IntPtr.Zero;
        }
        if (_hookFocus != IntPtr.Zero)
        {
            UnhookWinEvent(_hookFocus);
            _hookFocus = IntPtr.Zero;
        }
    }

    private static void WinEventCallback(
        IntPtr hWinEventHook, uint eventType, IntPtr hwnd,
        int idObject, int idChild, uint dwEventThread, uint dwmsEventTime)
    {
        if (hwnd == IntPtr.Zero || idObject != 0 || idChild != 0) return;

        GetWindowThreadProcessId(hwnd, out uint pid);
        if (pid == 0) return;

        string procName = "";
        try
        {
            using var p = Process.GetProcessById((int)pid);
            procName = p.ProcessName;
        }
        catch { }

        var titleSb = new StringBuilder(256);
        GetWindowText(hwnd, titleSb, 256);
        string title = titleSb.ToString();

        string evtTypeStr = eventType switch
        {
            EVENT_SYSTEM_FOREGROUND => "FOREGROUND_CHANGED",
            EVENT_OBJECT_CREATE => "WINDOW_OPENED",
            EVENT_OBJECT_DESTROY => "WINDOW_CLOSED",
            EVENT_OBJECT_FOCUS => "FOCUS_CHANGED",
            _ => "WIN_EVENT"
        };

        GetWindowRect(hwnd, out RECT rect);

        EventQueue.Enqueue(new DesktopEvent
        {
            EventType = evtTypeStr,
            WindowHandle = hwnd.ToInt64(),
            ProcessId = (int)pid,
            ProcessName = procName,
            WindowTitle = title,
            Bounds = new EventBounds
            {
                X = rect.Left,
                Y = rect.Top,
                Width = Math.Max(0, rect.Right - rect.Left),
                Height = Math.Max(0, rect.Bottom - rect.Top)
            }
        });
    }
    #endregion

    #region UI Automation Events
    private static void InitUIAEvents()
    {
        try
        {
            Automation.AddAutomationFocusChangedEventHandler(OnFocusChanged);
        }
        catch (Exception ex)
        {
            LogDebug($"Failed to register UIA focus event: {ex.Message}");
        }
    }

    private static void CleanupUIAEvents()
    {
        try
        {
            Automation.RemoveAllEventHandlers();
        }
        catch { }
    }

    private static void OnFocusChanged(object sender, AutomationFocusChangedEventArgs e)
    {
        if (sender is not AutomationElement element) return;

        try
        {
            var current = element.Current;
            EventQueue.Enqueue(new DesktopEvent
            {
                EventType = "FOCUS_CHANGED",
                WindowHandle = current.NativeWindowHandle,
                ProcessId = current.ProcessId,
                ProcessName = GetProcessName(current.ProcessId),
                WindowTitle = current.Name ?? "",
                ElementId = current.AutomationId ?? "",
                ControlType = current.ControlType?.ProgrammaticName ?? "",
                RuntimeId = element.GetRuntimeId(),
                Bounds = new EventBounds
                {
                    X = (int)current.BoundingRectangle.X,
                    Y = (int)current.BoundingRectangle.Y,
                    Width = (int)current.BoundingRectangle.Width,
                    Height = (int)current.BoundingRectangle.Height
                }
            });
        }
        catch (ElementNotAvailableException) { }
        catch (Exception ex)
        {
            LogDebug($"Error in OnFocusChanged: {ex.Message}");
        }
    }

    private static string GetProcessName(int pid)
    {
        try
        {
            using var p = Process.GetProcessById(pid);
            return p.ProcessName;
        }
        catch { return ""; }
    }
    #endregion

    #region Event Dispatch & Coalescing
    private static void DispatchEventsLoop()
    {
        while (_running)
        {
            if (EventQueue.TryDequeue(out var evt))
            {
                EmitEvent(evt);
            }
            else
            {
                Thread.Sleep(20);
            }
        }
    }

    private static void EmitEvent(DesktopEvent evt)
    {
        evt.SessionGeneration = SessionGeneration;
        evt.Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        evt.EventId = Guid.NewGuid().ToString("N");
        evt.Version = ProtocolVersion;

        string json = JsonSerializer.Serialize(evt, new JsonSerializerOptions { WriteIndented = false });
        lock (StdoutLock)
        {
            Console.WriteLine(json);
            Console.Out.Flush();
        }
    }
    #endregion

    #region Synchronous Command Processing
    private static void RunCommandLoop()
    {
        string? line;
        while (_running && (line = Console.ReadLine()) != null)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;

            int reqId = 0;
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;

                string command = root.TryGetProperty("command", out var cProp) ? cProp.GetString() ?? "" : "";
                reqId = root.TryGetProperty("id", out var idProp) ? idProp.GetInt32() : 0;

                CommandResponse response = command switch
                {
                    "ping" => HandlePing(reqId),
                    "inspect" => HandleInspect(reqId, root),
                    "invoke" => HandleInvoke(reqId, root),
                    "set_value" => HandleSetValue(reqId, root),
                    "click" => HandleClick(reqId, root),
                    "type" => HandleType(reqId, root),
                    "shortcut" => HandleShortcut(reqId, root),
                    "screenshot" => HandleScreenshot(reqId, root),
                    "shutdown" => HandleShutdown(reqId),
                    _ => new CommandResponse { Id = reqId, Status = "ERROR", Error = $"Unknown command '{command}'" }
                };

                string respJson = JsonSerializer.Serialize(response);
                lock (StdoutLock)
                {
                    Console.WriteLine(respJson);
                    Console.Out.Flush();
                }

                if (command == "shutdown") break;
            }
            catch (Exception ex)
            {
                var errResp = new CommandResponse { Id = reqId, Status = "ERROR", Error = ex.Message };
                lock (StdoutLock)
                {
                    Console.WriteLine(JsonSerializer.Serialize(errResp));
                    Console.Out.Flush();
                }
            }
        }
    }

    private static CommandResponse HandlePing(int id)
    {
        return new CommandResponse
        {
            Id = id,
            Status = "SUCCESS",
            Result = new Dictionary<string, object>
            {
                ["pong"] = true,
                ["session_generation"] = SessionGeneration,
                ["pid"] = Process.GetCurrentProcess().Id
            }
        };
    }

    private static CommandResponse HandleShutdown(int id)
    {
        _running = false;
        return new CommandResponse
        {
            Id = id,
            Status = "SUCCESS",
            Result = new Dictionary<string, object> { ["shutdown"] = true }
        };
    }

    private static CommandResponse HandleInspect(int id, JsonElement root)
    {
        long hwndVal = root.TryGetProperty("hwnd", out var hProp) ? hProp.GetInt64() : 0;
        int maxDepth = root.TryGetProperty("max_depth", out var dProp) ? dProp.GetInt32() : 5;

        AutomationElement? target;
        if (hwndVal != 0)
        {
            try
            {
                target = AutomationElement.FromHandle(new IntPtr(hwndVal));
            }
            catch (Exception ex)
            {
                return new CommandResponse { Id = id, Status = "ERROR", Error = $"Invalid window handle {hwndVal}: {ex.Message}" };
            }
        }
        else
        {
            IntPtr fg = GetForegroundWindow();
            target = fg != IntPtr.Zero ? AutomationElement.FromHandle(fg) : AutomationElement.RootElement;
        }

        if (target == null)
        {
            return new CommandResponse { Id = id, Status = "ERROR", Error = "Target window element could not be found." };
        }

        var tree = InspectSubtree(target, 0, maxDepth);
        return new CommandResponse
        {
            Id = id,
            Status = "SUCCESS",
            Result = new Dictionary<string, object>
            {
                ["root"] = tree
            }
        };
    }

    private static ElementNode InspectSubtree(AutomationElement element, int currentDepth, int maxDepth)
    {
        var node = new ElementNode();
        try
        {
            var cur = element.Current;
            node.Name = cur.Name ?? "";
            node.AutomationId = cur.AutomationId ?? "";
            node.ControlType = cur.ControlType?.ProgrammaticName ?? "";
            node.ClassName = cur.ClassName ?? "";
            node.IsEnabled = cur.IsEnabled;
            node.NativeWindowHandle = cur.NativeWindowHandle;
            node.ProcessId = cur.ProcessId;
            node.RuntimeId = element.GetRuntimeId();
            node.Bounds = new EventBounds
            {
                X = (int)cur.BoundingRectangle.X,
                Y = (int)cur.BoundingRectangle.Y,
                Width = (int)cur.BoundingRectangle.Width,
                Height = (int)cur.BoundingRectangle.Height
            };

            // Detect supported patterns
            var patterns = new List<string>();
            if (element.TryGetCurrentPattern(InvokePattern.Pattern, out _)) patterns.Add("Invoke");
            if (element.TryGetCurrentPattern(ValuePattern.Pattern, out _)) patterns.Add("Value");
            if (element.TryGetCurrentPattern(TogglePattern.Pattern, out _)) patterns.Add("Toggle");
            if (element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out _)) patterns.Add("SelectionItem");
            if (element.TryGetCurrentPattern(WindowPattern.Pattern, out _)) patterns.Add("Window");
            node.Patterns = patterns;

            if (currentDepth < maxDepth)
            {
                var children = new List<ElementNode>();
                if (element == AutomationElement.RootElement)
                {
                    var winCondition = new PropertyCondition(AutomationElement.IsControlElementProperty, true);
                    var windows = element.FindAll(TreeScope.Children, winCondition);
                    int count = 0;
                    foreach (AutomationElement win in windows)
                    {
                        if (count >= 50) break;
                        try
                        {
                            children.Add(InspectSubtree(win, currentDepth + 1, maxDepth));
                            count++;
                        }
                        catch { }
                    }
                }
                else
                {
                    var walker = TreeWalker.ControlViewWalker;
                    var child = walker.GetFirstChild(element);
                    int count = 0;
                    while (child != null && count < 50)
                    {
                        children.Add(InspectSubtree(child, currentDepth + 1, maxDepth));
                        child = walker.GetNextSibling(child);
                        count++;
                    }
                }
                node.Children = children;
            }
        }
        catch { }

        return node;
    }

    private static CommandResponse HandleInvoke(int id, JsonElement root)
    {
        string autoId = root.TryGetProperty("automation_id", out var aProp) ? aProp.GetString() ?? "" : "";
        string name = root.TryGetProperty("name", out var nProp) ? nProp.GetString() ?? "" : "";

        var target = FindElement(autoId, name);
        if (target == null)
        {
            return new CommandResponse { Id = id, Status = "ERROR", Error = $"Element not found (autoId='{autoId}', name='{name}')" };
        }

        if (target.TryGetCurrentPattern(InvokePattern.Pattern, out var pattern) && pattern is InvokePattern invoke)
        {
            invoke.Invoke();
            return new CommandResponse { Id = id, Status = "SUCCESS", Result = new Dictionary<string, object> { ["invoked"] = true } };
        }

        return new CommandResponse { Id = id, Status = "ERROR", Error = "Target element does not support InvokePattern." };
    }

    private static CommandResponse HandleSetValue(int id, JsonElement root)
    {
        string autoId = root.TryGetProperty("automation_id", out var aProp) ? aProp.GetString() ?? "" : "";
        string name = root.TryGetProperty("name", out var nProp) ? nProp.GetString() ?? "" : "";
        string val = root.TryGetProperty("value", out var vProp) ? vProp.GetString() ?? "" : "";

        var target = FindElement(autoId, name);
        if (target == null)
        {
            return new CommandResponse { Id = id, Status = "ERROR", Error = $"Element not found (autoId='{autoId}', name='{name}')" };
        }

        if (target.TryGetCurrentPattern(ValuePattern.Pattern, out var pattern) && pattern is ValuePattern valuePattern)
        {
            valuePattern.SetValue(val);
            return new CommandResponse { Id = id, Status = "SUCCESS", Result = new Dictionary<string, object> { ["value_set"] = true } };
        }

        return new CommandResponse { Id = id, Status = "ERROR", Error = "Target element does not support ValuePattern." };
    }

    private static CommandResponse HandleClick(int id, JsonElement root)
    {
        int x = root.TryGetProperty("x", out var xProp) ? xProp.GetInt32() : -1;
        int y = root.TryGetProperty("y", out var yProp) ? yProp.GetInt32() : -1;

        if (x < 0 || y < 0)
        {
            string autoId = root.TryGetProperty("automation_id", out var aProp) ? aProp.GetString() ?? "" : "";
            string name = root.TryGetProperty("name", out var nProp) ? nProp.GetString() ?? "" : "";
            var el = FindElement(autoId, name);
            if (el != null)
            {
                var rect = el.Current.BoundingRectangle;
                x = (int)(rect.X + rect.Width / 2);
                y = (int)(rect.Y + rect.Height / 2);
            }
        }

        if (x >= 0 && y >= 0)
        {
            SetCursorPos(x, y);
            Thread.Sleep(50);
            mouse_event(MOUSEEVENTF_LEFTDOWN, x, y, 0, UIntPtr.Zero);
            Thread.Sleep(30);
            mouse_event(MOUSEEVENTF_LEFTUP, x, y, 0, UIntPtr.Zero);

            return new CommandResponse
            {
                Id = id,
                Status = "SUCCESS",
                Result = new Dictionary<string, object> { ["clicked"] = true, ["x"] = x, ["y"] = y }
            };
        }

        return new CommandResponse { Id = id, Status = "ERROR", Error = "Invalid coordinates or element not located for click." };
    }

    private static CommandResponse HandleType(int id, JsonElement root)
    {
        string text = root.TryGetProperty("text", out var tProp) ? tProp.GetString() ?? "" : "";
        if (!string.IsNullOrEmpty(text))
        {
            SendKeys.SendWait(text);
            return new CommandResponse { Id = id, Status = "SUCCESS", Result = new Dictionary<string, object> { ["typed"] = true } };
        }
        return new CommandResponse { Id = id, Status = "ERROR", Error = "Text to type was empty." };
    }

    private static CommandResponse HandleShortcut(int id, JsonElement root)
    {
        string keys = root.TryGetProperty("keys", out var kProp) ? kProp.GetString() ?? "" : "";
        if (!string.IsNullOrEmpty(keys))
        {
            SendKeys.SendWait(keys);
            return new CommandResponse { Id = id, Status = "SUCCESS", Result = new Dictionary<string, object> { ["shortcut_sent"] = true } };
        }
        return new CommandResponse { Id = id, Status = "ERROR", Error = "Keys combination was empty." };
    }

    private static CommandResponse HandleScreenshot(int id, JsonElement root)
    {
        try
        {
            long hwndVal = root.TryGetProperty("hwnd", out var hProp) ? hProp.GetInt64() : 0;
            Rectangle captureBounds;

            if (hwndVal != 0 && GetWindowRect(new IntPtr(hwndVal), out RECT wRect))
            {
                captureBounds = new Rectangle(wRect.Left, wRect.Top, Math.Max(1, wRect.Right - wRect.Left), Math.Max(1, wRect.Bottom - wRect.Top));
            }
            else
            {
                captureBounds = Screen.PrimaryScreen?.Bounds ?? new Rectangle(0, 0, 1920, 1080);
            }

            if (captureBounds.Width <= 0) captureBounds.Width = 1920;
            if (captureBounds.Height <= 0) captureBounds.Height = 1080;

            using var bitmap = new Bitmap(captureBounds.Width, captureBounds.Height);
            using var g = Graphics.FromImage(bitmap);
            try
            {
                g.CopyFromScreen(captureBounds.Location, Point.Empty, captureBounds.Size);
            }
            catch (Exception gEx)
            {
                LogDebug($"CopyFromScreen note: {gEx.Message}");
                g.Clear(Color.FromArgb(40, 40, 40));
            }

            using var ms = new MemoryStream();
            bitmap.Save(ms, ImageFormat.Png);
            string base64 = Convert.ToBase64String(ms.ToArray());

            return new CommandResponse
            {
                Id = id,
                Status = "SUCCESS",
                Result = new Dictionary<string, object>
                {
                    ["screenshot_base64"] = base64,
                    ["bounds"] = new Dictionary<string, int>
                    {
                        ["width"] = captureBounds.Width,
                        ["height"] = captureBounds.Height
                    }
                }
            };
        }
        catch (Exception ex)
        {
            return new CommandResponse { Id = id, Status = "ERROR", Error = $"Screenshot failed: {ex.Message}" };
        }
    }

    private static AutomationElement? FindElement(string autoId, string name)
    {
        IntPtr fg = GetForegroundWindow();
        var root = fg != IntPtr.Zero ? AutomationElement.FromHandle(fg) : AutomationElement.RootElement;

        Condition cond;
        if (!string.IsNullOrEmpty(autoId) && !string.IsNullOrEmpty(name))
        {
            cond = new AndCondition(
                new PropertyCondition(AutomationElement.AutomationIdProperty, autoId),
                new PropertyCondition(AutomationElement.NameProperty, name)
            );
        }
        else if (!string.IsNullOrEmpty(autoId))
        {
            cond = new PropertyCondition(AutomationElement.AutomationIdProperty, autoId);
        }
        else if (!string.IsNullOrEmpty(name))
        {
            cond = new PropertyCondition(AutomationElement.NameProperty, name);
        }
        else
        {
            return null;
        }

        return root.FindFirst(TreeScope.Subtree, cond);
    }

    private static void LogDebug(string msg)
    {
        Console.Error.WriteLine($"[sage-desktop-watcher] {msg}");
    }
    #endregion
}

public class DesktopEvent
{
    public string Version { get; set; } = "1.0";
    public string EventId { get; set; } = "";
    public string EventType { get; set; } = "";
    public double Timestamp { get; set; }
    public int ProcessId { get; set; }
    public long WindowHandle { get; set; }
    public int SessionGeneration { get; set; }
    public string ProcessName { get; set; } = "";
    public string WindowTitle { get; set; } = "";
    public string? ElementId { get; set; }
    public string? ControlType { get; set; }
    public int[]? RuntimeId { get; set; }
    public EventBounds? Bounds { get; set; }
}

public class EventBounds
{
    public int X { get; set; }
    public int Y { get; set; }
    public int Width { get; set; }
    public int Height { get; set; }
}

public class CommandResponse
{
    public int Id { get; set; }
    public string Status { get; set; } = "SUCCESS";
    public string? Error { get; set; }
    public Dictionary<string, object>? Result { get; set; }
}

public class ElementNode
{
    public string Name { get; set; } = "";
    public string AutomationId { get; set; } = "";
    public string ControlType { get; set; } = "";
    public string ClassName { get; set; } = "";
    public bool IsEnabled { get; set; }
    public int NativeWindowHandle { get; set; }
    public int ProcessId { get; set; }
    public int[]? RuntimeId { get; set; }
    public EventBounds? Bounds { get; set; }
    public List<string> Patterns { get; set; } = new();
    public List<ElementNode> Children { get; set; } = new();
}
