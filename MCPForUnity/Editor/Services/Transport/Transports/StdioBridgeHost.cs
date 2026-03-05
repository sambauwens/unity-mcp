using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using MCPForUnity.Editor.Constants;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Models;
using MCPForUnity.Editor.Services;
using MCPForUnity.Editor.Services.Transport;
using MCPForUnity.Editor.Tools;
using MCPForUnity.Editor.Tools.Prefabs;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Services.Transport.Transports
{
    [InitializeOnLoad]
    public static class StdioBridgeHost
    {
        private static TcpListener listener;
        private static bool isRunning = false;
        private static readonly object startStopLock = new();
        private static readonly object clientsLock = new();
        private static readonly HashSet<TcpClient> activeClients = new();
        private static CancellationTokenSource cts;
        private static Task listenerTask;
        private static bool initScheduled = false;
        private static bool ensureUpdateHooked = false;
        private static bool isStarting = false;
        private static double nextStartAt = 0.0f;
        private static double nextHeartbeatAt = 0.0f;
        private static int heartbeatSeq = 0;
        private static int mainThreadId;
        private static int currentUnityPort = 6400;
        private static bool isAutoConnectMode = false;
        private const ulong MaxFrameBytes = 64UL * 1024 * 1024;
        private const int FrameIOTimeoutMs = 30000;
        private static readonly Stopwatch _uptime = Stopwatch.StartNew();
        private static volatile int _consecutiveTimeouts = 0;

        private static void IoInfo(string s) { McpLog.Info(s, always: false); }

        private static bool IsDebugEnabled()
        {
            try { return EditorPrefs.GetBool(EditorPrefKeys.DebugLogs, false); } catch { return false; }
        }

        private static void LogBreadcrumb(string stage)
        {
            if (IsDebugEnabled())
            {
                McpLog.Info($"[{stage}]", always: false);
            }
        }

        public static bool IsRunning => isRunning;
        public static int GetCurrentPort() => currentUnityPort;
        public static bool IsAutoConnectMode() => isAutoConnectMode;

        public static void StartAutoConnect()
        {
            Stop();

            try
            {
                currentUnityPort = PortManager.GetPortWithFallback();
                Start();
                isAutoConnectMode = true;

                TelemetryHelper.RecordBridgeStartup();
            }
            catch (Exception ex)
            {
                McpLog.Error($"Auto-connect failed: {ex.Message}");
                TelemetryHelper.RecordBridgeConnection(false, ex.Message);
                throw;
            }
        }

        public static bool FolderExists(string path)
        {
            if (string.IsNullOrEmpty(path))
            {
                return false;
            }

            if (path.Equals("Assets", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            string fullPath = Path.Combine(
                Application.dataPath,
                path.StartsWith("Assets/") ? path[7..] : path
            );
            return Directory.Exists(fullPath);
        }

        static StdioBridgeHost()
        {
            try { mainThreadId = Thread.CurrentThread.ManagedThreadId; } catch { mainThreadId = 0; }

            if (Application.isBatchMode && string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("UNITY_MCP_ALLOW_BATCH")))
            {
                return;
            }
            if (ShouldAutoStartBridge())
            {
                ScheduleInitRetry();
                if (!ensureUpdateHooked)
                {
                    ensureUpdateHooked = true;
                    EditorApplication.update += EnsureStartedOnEditorIdle;
                }
            }
            EditorApplication.quitting += Stop;
            EditorApplication.playModeStateChanged += _ =>
            {
                if (ShouldAutoStartBridge())
                {
                    ScheduleInitRetry();
                }
            };
        }

        private static void InitializeAfterCompilation()
        {
            initScheduled = false;

            if (IsCompiling())
            {
                ScheduleInitRetry();
                return;
            }

            if (!isRunning)
            {
                Start();
                if (!isRunning)
                {
                    ScheduleInitRetry();
                }
            }
        }

        private static void ScheduleInitRetry()
        {
            if (initScheduled)
            {
                return;
            }
            initScheduled = true;
            nextStartAt = EditorApplication.timeSinceStartup + 0.20f;
            if (!ensureUpdateHooked)
            {
                ensureUpdateHooked = true;
                EditorApplication.update += EnsureStartedOnEditorIdle;
            }
            EditorApplication.delayCall += InitializeAfterCompilation;
        }

        private static bool ShouldAutoStartBridge()
        {
            try
            {
                bool useHttpTransport = EditorConfigurationCache.Instance.UseHttpTransport;
                return !useHttpTransport;
            }
            catch
            {
                return true;
            }
        }

        private static void EnsureStartedOnEditorIdle()
        {
            if (IsCompiling())
            {
                return;
            }

            if (isRunning)
            {
                EditorApplication.update -= EnsureStartedOnEditorIdle;
                ensureUpdateHooked = false;
                return;
            }

            if (nextStartAt > 0 && EditorApplication.timeSinceStartup < nextStartAt)
            {
                return;
            }

            if (isStarting)
            {
                return;
            }

            isStarting = true;
            try
            {
                Start();
            }
            finally
            {
                isStarting = false;
            }
            if (isRunning)
            {
                EditorApplication.update -= EnsureStartedOnEditorIdle;
                ensureUpdateHooked = false;
            }
        }

        private static bool IsCompiling()
        {
            if (EditorApplication.isCompiling)
            {
                return true;
            }
            try
            {
                Type pipeline = Type.GetType("UnityEditor.Compilation.CompilationPipeline, UnityEditor");
                var prop = pipeline?.GetProperty("isCompiling", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static);
                if (prop != null)
                {
                    return (bool)prop.GetValue(null);
                }
            }
            catch { }
            return false;
        }

        public static void Start()
        {
            lock (startStopLock)
            {
                if (isRunning && listener != null)
                {
                    if (IsDebugEnabled())
                    {
                        McpLog.Info($"StdioBridgeHost already running on port {currentUnityPort}");
                    }
                    return;
                }

                Stop();

                try
                {
                    currentUnityPort = PortManager.GetPortWithFallback();

                    // Clear any stale "reloading" heartbeat from a previous domain reload.
                    // After reload, static fields reset (isRunning=false), so Stop() above
                    // is a no-op and won't delete the status file. Writing now ensures clients
                    // see reloading=false even if listener creation fails below.
                    WriteHeartbeat(false, "starting");

                    LogBreadcrumb("Start");

                    try
                    {
                        listener = CreateConfiguredListener(currentUnityPort);
                        listener.Start();
                    }
                    catch (SocketException se) when (se.SocketErrorCode == SocketError.AddressAlreadyInUse)
                    {
                        // Port is busy. Try switching to a new port once; if that also fails,
                        // let the reload handler retry with async backoff instead of blocking here.
                        int oldPort = currentUnityPort;
                        currentUnityPort = PortManager.DiscoverNewPort();

                        try
                        {
                            EditorPrefs.SetInt(EditorPrefKeys.UnitySocketPort, currentUnityPort);
                        }
                        catch { }

                        if (IsDebugEnabled())
                        {
                            McpLog.Info($"Port {oldPort} occupied, switching to port {currentUnityPort}");
                        }

                        listener = CreateConfiguredListener(currentUnityPort);
                        listener.Start();
                    }

                    isRunning = true;
                    isAutoConnectMode = false;
                    string platform = Application.platform.ToString();
                    string serverVer = AssetPathUtility.GetPackageVersion();
                    McpLog.Info($"StdioBridgeHost started on port {currentUnityPort}. (OS={platform}, server={serverVer})");
                    cts = new CancellationTokenSource();
                    listenerTask = Task.Run(() => ListenerLoopAsync(cts.Token));
                    CommandRegistry.Initialize();
                    try { EditorApplication.quitting -= Stop; } catch { }
                    try { EditorApplication.quitting += Stop; } catch { }
                    try { EditorApplication.update -= TickHeartbeat; EditorApplication.update += TickHeartbeat; } catch { }
                    try { ApplyNoThrottling(); } catch (Exception ex) { McpLog.Warn($"ApplyNoThrottling failed: {ex}"); }
#if UNITY_EDITOR_OSX
                    try { BeginAppNapPrevention(); } catch (Exception ex) { McpLog.Warn($"BeginAppNapPrevention failed: {ex}"); }
#endif
                    heartbeatSeq++;
                    WriteHeartbeat(false, "ready");
                    nextHeartbeatAt = EditorApplication.timeSinceStartup + 0.5f;
                }
                catch (SocketException ex)
                {
                    McpLog.Error($"Failed to start TCP listener: {ex.Message}");
                    WriteHeartbeat(false, "start_failed");
                }
            }
        }

        private static TcpListener CreateConfiguredListener(int port)
        {
            var newListener = new TcpListener(IPAddress.Loopback, port);
#if UNITY_EDITOR_OSX
            // SO_REUSEADDR is intentionally NOT set. On macOS it allows multiple
            // processes (including AssetImportWorkers) to bind the same port,
            // causing connections to land on a worker that can't process commands.
            // The ExclusiveAddressUse flag prevents this; port-busy conflicts are
            // handled by the retry/fallback logic in Start() and the reload handler.
            try { newListener.Server.ExclusiveAddressUse = true; } catch { }

            // Prevent AssetImportWorker child processes from inheriting this socket.
            // Without FD_CLOEXEC, forked workers inherit the listening FD and may
            // accept MCP connections that should go to the main editor, causing
            // timeouts because workers have no command processing loop.
            SetCloseOnExec(newListener.Server);
#endif
            try
            {
                newListener.Server.LingerState = new LingerOption(true, 0);
            }
            catch (Exception)
            {
            }
            return newListener;
        }

#if UNITY_EDITOR_OSX
        [DllImport("libc", SetLastError = true)]
        private static extern int fcntl(int fd, int cmd, int arg);

        private const int F_GETFD = 1;
        private const int F_SETFD = 2;
        private const int FD_CLOEXEC = 1;

        private static void SetCloseOnExec(Socket socket)
        {
            try
            {
                int fd = (int)socket.Handle;
                int flags = fcntl(fd, F_GETFD, 0);
                if (flags >= 0)
                {
                    fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to set FD_CLOEXEC on listener socket: {ex.Message}");
            }
        }

        // --- App Nap prevention via NSProcessInfo.beginActivityWithOptions:reason: ---
        // macOS App Nap throttles unfocused apps to ~10 Hz, causing 100-200ms MCP latency.
        // beginActivity tells the OS this process is doing latency-critical work.

        [DllImport("libobjc.dylib", EntryPoint = "objc_getClass")]
        private static extern IntPtr objc_getClass(string className);

        [DllImport("libobjc.dylib", EntryPoint = "sel_registerName")]
        private static extern IntPtr sel_registerName(string name);

        [DllImport("libobjc.dylib", EntryPoint = "objc_msgSend")]
        private static extern IntPtr objc_msgSend_ptr(IntPtr receiver, IntPtr selector);

        [DllImport("libobjc.dylib", EntryPoint = "objc_msgSend")]
        private static extern IntPtr objc_msgSend_ptr_ulong_ptr(IntPtr receiver, IntPtr selector, ulong options, IntPtr reason);

        [DllImport("libobjc.dylib", EntryPoint = "objc_msgSend")]
        private static extern void objc_msgSend_void_ptr(IntPtr receiver, IntPtr selector, IntPtr arg);

        [DllImport("libobjc.dylib", EntryPoint = "objc_msgSend")]
        private static extern IntPtr objc_msgSend_ptr_byteptr(IntPtr receiver, IntPtr selector,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string arg);

        private static IntPtr _appNapActivity = IntPtr.Zero;

        private static void BeginAppNapPrevention()
        {
            try
            {
                if (_appNapActivity != IntPtr.Zero) return;

                IntPtr processInfoClass = objc_getClass("NSProcessInfo");
                IntPtr processInfoSel = sel_registerName("processInfo");
                IntPtr processInfo = objc_msgSend_ptr(processInfoClass, processInfoSel);

                // Create NSString for reason
                IntPtr nsStringClass = objc_getClass("NSString");
                IntPtr stringWithUTF8Sel = sel_registerName("stringWithUTF8String:");
                IntPtr reason = objc_msgSend_ptr_byteptr(nsStringClass, stringWithUTF8Sel,
                    "MCP bridge requires low-latency editor loop");

                // NSActivityUserInitiatedAllowingIdleSystemSleep = 0x00FFFFFFULL
                // This includes NSActivityLatencyCritical and prevents App Nap.
                const ulong NSActivityUserInitiatedAllowingIdleSystemSleep = 0x00FFFFFFUL;

                IntPtr beginActivitySel = sel_registerName("beginActivityWithOptions:reason:");
                _appNapActivity = objc_msgSend_ptr_ulong_ptr(
                    processInfo, beginActivitySel,
                    NSActivityUserInitiatedAllowingIdleSystemSleep, reason);

                if (_appNapActivity != IntPtr.Zero)
                {
                    UnityEngine.Debug.LogWarning("[MCP] App Nap prevention active (beginActivity)");
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to begin App Nap prevention: {ex.Message}");
            }
        }

        private static void EndAppNapPrevention()
        {
            try
            {
                if (_appNapActivity == IntPtr.Zero) return;

                IntPtr processInfoClass = objc_getClass("NSProcessInfo");
                IntPtr processInfoSel = sel_registerName("processInfo");
                IntPtr processInfo = objc_msgSend_ptr(processInfoClass, processInfoSel);

                IntPtr endActivitySel = sel_registerName("endActivity:");
                objc_msgSend_void_ptr(processInfo, endActivitySel, _appNapActivity);

                _appNapActivity = IntPtr.Zero;
                McpLog.Info("App Nap prevention ended");
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to end App Nap prevention: {ex.Message}");
            }
        }
#endif

        public static void Stop()
        {
            Task toWait = null;
            lock (startStopLock)
            {
                if (!isRunning)
                {
                    return;
                }

                try
                {
                    isRunning = false;

                    var cancel = cts;
                    cts = null;
                    try { cancel?.Cancel(); } catch { }

                    try { listener?.Stop(); } catch { }
                    try { listener?.Server?.Dispose(); } catch { }
                    listener = null;

                    toWait = listenerTask;
                    listenerTask = null;
                }
                catch (Exception ex)
                {
                    McpLog.Error($"Error stopping StdioBridgeHost: {ex.Message}");
                }
            }

            TcpClient[] toClose;
            lock (clientsLock)
            {
                toClose = activeClients.ToArray();
                activeClients.Clear();
            }
            foreach (var c in toClose)
            {
                try { c.Close(); } catch { }
            }

            if (toWait != null)
            {
                // CTS is already cancelled; give the listener task a brief moment to exit.
                try { toWait.Wait(500); } catch { }
            }

            try { EditorApplication.quitting -= Stop; } catch { }
            RestoreThrottling();
#if UNITY_EDITOR_OSX
            try { EndAppNapPrevention(); } catch { }
#endif

            try
            {
                string dir = Environment.GetEnvironmentVariable("UNITY_MCP_STATUS_DIR");
                if (string.IsNullOrWhiteSpace(dir))
                {
                    dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".unity-mcp");
                }
                string statusFile = Path.Combine(dir, $"unity-mcp-status-{ComputeProjectHash(Application.dataPath)}.json");
                if (File.Exists(statusFile))
                {
                    File.Delete(statusFile);
                    if (IsDebugEnabled()) McpLog.Info($"Deleted status file: {statusFile}");
                }
            }
            catch (Exception ex)
            {
                if (IsDebugEnabled()) McpLog.Warn($"Failed to delete status file: {ex.Message}");
            }

            if (IsDebugEnabled()) McpLog.Info("StdioBridgeHost stopped.");
        }

        private static async Task ListenerLoopAsync(CancellationToken token)
        {
            while (isRunning && !token.IsCancellationRequested)
            {
                try
                {
                    TcpClient client = await listener.AcceptTcpClientAsync();
                    client.Client.SetSocketOption(
                        SocketOptionLevel.Socket,
                        SocketOptionName.KeepAlive,
                        true
                    );

                    client.ReceiveTimeout = 60000;

                    _ = Task.Run(() => HandleClientAsync(client, token), token);
                }
                catch (ObjectDisposedException)
                {
                    if (!isRunning || token.IsCancellationRequested)
                    {
                        break;
                    }
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    if (isRunning && !token.IsCancellationRequested)
                    {
                        if (IsDebugEnabled()) McpLog.Error($"Listener error: {ex.Message}");
                    }
                }
            }
        }

        private static async Task HandleClientAsync(TcpClient client, CancellationToken token)
        {
            using (client)
            using (NetworkStream stream = client.GetStream())
            {
                int clientCount;
                lock (clientsLock)
                {
                    activeClients.Add(client);
                    clientCount = activeClients.Count;
                }
                try
                {
                    try
                    {
                        var ep = client.Client?.RemoteEndPoint?.ToString() ?? "unknown";
                        McpLog.Info($"Client connected {ep} (active clients: {clientCount})");
                    }
                    catch { }
                    try
                    {
                        client.NoDelay = true;
                    }
                    catch { }
                    try
                    {
                        string handshake = "WELCOME UNITY-MCP 1 FRAMING=1\n";
                        byte[] handshakeBytes = System.Text.Encoding.ASCII.GetBytes(handshake);
                        using var cts = new CancellationTokenSource(FrameIOTimeoutMs);
#if NETSTANDARD2_1 || NET6_0_OR_GREATER
                        await stream.WriteAsync(handshakeBytes.AsMemory(0, handshakeBytes.Length), cts.Token).ConfigureAwait(false);
#else
                        await stream.WriteAsync(handshakeBytes, 0, handshakeBytes.Length, cts.Token).ConfigureAwait(false);
#endif
                        if (IsDebugEnabled()) McpLog.Info("Sent handshake FRAMING=1 (strict)", always: false);
                    }
                    catch (Exception ex)
                    {
                        if (IsDebugEnabled()) McpLog.Warn($"Handshake failed: {ex.Message}");
                        return;
                    }

                    // In stdio transport there is only ever one active Python server.
                    // A new connection means the old one is dead — close stale clients so
                    // their hung ReadFrameAsUtf8Async calls throw and exit cleanly.
                    TcpClient[] staleClients;
                    lock (clientsLock)
                    {
                        staleClients = activeClients.Where(c => c != client).ToArray();
                    }
                    if (staleClients.Length > 0)
                    {
                        McpLog.Info($"Closing {staleClients.Length} stale client(s) after new connection");
                        foreach (var stale in staleClients)
                        {
                            try { stale.Close(); } catch { }
                        }
                    }

                    while (isRunning && !token.IsCancellationRequested)
                    {
                        try
                        {
                            string commandText = await ReadFrameAsUtf8Async(stream, FrameIOTimeoutMs, token).ConfigureAwait(false);

                            try
                            {
                                if (IsDebugEnabled())
                                {
                                    var preview = commandText.Length > 120 ? commandText.Substring(0, 120) + "…" : commandText;
                                    McpLog.Info($"recv framed: {preview}", always: false);
                                }
                            }
                            catch { }
                            if (commandText.Trim() == "ping")
                            {
                                byte[] pingResponseBytes = System.Text.Encoding.UTF8.GetBytes(
                                    "{\"status\":\"success\",\"result\":{\"message\":\"pong\"}}"
                                );
                                await WriteFrameAsync(stream, pingResponseBytes);
                                continue;
                            }

                            // Dispatch directly through TransportCommandDispatcher (same path as WebSocket transport).
                            // This avoids the intermediate commandQueue + ProcessCommands (EditorApplication.update)
                            // layer which added 100-200ms of latency waiting for the next editor frame.
                            string response;
                            try
                            {
                                using var respCts = new CancellationTokenSource(FrameIOTimeoutMs);
                                response = await TransportCommandDispatcher.ExecuteCommandJsonAsync(commandText, respCts.Token).ConfigureAwait(false);
                                Interlocked.Exchange(ref _consecutiveTimeouts, 0);
                            }
                            catch (OperationCanceledException)
                            {
                                int timeouts = Interlocked.Increment(ref _consecutiveTimeouts);
                                McpLog.Warn($"Command timed out ({timeouts} consecutive)");
                                var timeoutResponse = new
                                {
                                    status = "error",
                                    error = $"Command processing timed out after {FrameIOTimeoutMs} ms",
                                };
                                response = JsonConvert.SerializeObject(timeoutResponse);
                            }
                            catch (Exception ex)
                            {
                                var errorResponse = new
                                {
                                    status = "error",
                                    error = ex.Message,
                                };
                                response = JsonConvert.SerializeObject(errorResponse);
                            }

                            if (IsDebugEnabled())
                            {
                                try { McpLog.Info("[MCP] sending framed response", always: false); } catch { }
                            }
                            byte[] responseBytes;
                            try
                            {
                                responseBytes = System.Text.Encoding.UTF8.GetBytes(response);
                            }
                            catch (Exception ex)
                            {
                                IoInfo($"[IO] ✗ serialize FAIL tag=response reqId=? {ex.GetType().Name}: {ex.Message}");
                                throw;
                            }

                            try
                            {
                                await WriteFrameAsync(stream, responseBytes);
                            }
                            catch (Exception ex)
                            {
                                IoInfo($"[IO] ✗ write FAIL  tag=response reqId=? {ex.GetType().Name}: {ex.Message}");
                                throw;
                            }
                        }
                        catch (Exception ex)
                        {
                            string msg = ex.Message ?? string.Empty;
                            bool isBenign =
                                msg.IndexOf("Connection closed before reading expected bytes", StringComparison.OrdinalIgnoreCase) >= 0
                                || msg.IndexOf("Read timed out", StringComparison.OrdinalIgnoreCase) >= 0
                                || ex is IOException;
                            if (isBenign)
                            {
                                if (IsDebugEnabled()) McpLog.Info($"Client handler: {msg}", always: false);
                            }
                            else
                            {
                                McpLog.Error($"Client handler error: {msg}");
                            }
                            break;
                        }
                    }
                }
                finally
                {
                    lock (clientsLock) { activeClients.Remove(client); }
                    int remaining;
                    lock (clientsLock) { remaining = activeClients.Count; }
                    McpLog.Info($"Client handler exited (remaining clients: {remaining})");
                }
            }
        }

        private static async Task<byte[]> ReadExactAsync(NetworkStream stream, int count, int timeoutMs, CancellationToken cancel = default)
        {
            byte[] buffer = new byte[count];
            int offset = 0;
            var stopwatch = System.Diagnostics.Stopwatch.StartNew();

            while (offset < count)
            {
                int remaining = count - offset;
                int remainingTimeout = timeoutMs <= 0
                    ? Timeout.Infinite
                    : timeoutMs - (int)stopwatch.ElapsedMilliseconds;

                if (remainingTimeout != Timeout.Infinite && remainingTimeout <= 0)
                {
                    throw new IOException("Read timed out");
                }

                using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancel);
                if (remainingTimeout != Timeout.Infinite)
                {
                    cts.CancelAfter(remainingTimeout);
                }

                try
                {
#if NETSTANDARD2_1 || NET6_0_OR_GREATER
                    int read = await stream.ReadAsync(buffer.AsMemory(offset, remaining), cts.Token).ConfigureAwait(false);
#else
                    int read = await stream.ReadAsync(buffer, offset, remaining, cts.Token).ConfigureAwait(false);
#endif
                    if (read == 0)
                    {
                        throw new IOException("Connection closed before reading expected bytes");
                    }
                    offset += read;
                }
                catch (OperationCanceledException) when (!cancel.IsCancellationRequested)
                {
                    throw new IOException("Read timed out");
                }
            }

            return buffer;
        }

        private static Task WriteFrameAsync(NetworkStream stream, byte[] payload)
        {
            using var cts = new CancellationTokenSource(FrameIOTimeoutMs);
            return WriteFrameAsync(stream, payload, cts.Token);
        }

        private static async Task WriteFrameAsync(NetworkStream stream, byte[] payload, CancellationToken cancel)
        {
            if (payload == null)
            {
                throw new ArgumentNullException(nameof(payload));
            }
            if ((ulong)payload.LongLength > MaxFrameBytes)
            {
                throw new IOException($"Frame too large: {payload.LongLength}");
            }
            byte[] header = new byte[8];
            WriteUInt64BigEndian(header, (ulong)payload.LongLength);
#if NETSTANDARD2_1 || NET6_0_OR_GREATER
            await stream.WriteAsync(header.AsMemory(0, header.Length), cancel).ConfigureAwait(false);
            await stream.WriteAsync(payload.AsMemory(0, payload.Length), cancel).ConfigureAwait(false);
#else
            await stream.WriteAsync(header, 0, header.Length, cancel).ConfigureAwait(false);
            await stream.WriteAsync(payload, 0, payload.Length, cancel).ConfigureAwait(false);
#endif
        }

        private static async Task<string> ReadFrameAsUtf8Async(NetworkStream stream, int timeoutMs, CancellationToken cancel)
        {
            byte[] header = await ReadExactAsync(stream, 8, timeoutMs, cancel).ConfigureAwait(false);
            ulong payloadLen = ReadUInt64BigEndian(header);
            if (payloadLen > MaxFrameBytes)
            {
                throw new IOException($"Invalid framed length: {payloadLen}");
            }
            if (payloadLen == 0UL)
                throw new IOException("Zero-length frames are not allowed");
            if (payloadLen > int.MaxValue)
            {
                throw new IOException("Frame too large for buffer");
            }
            int count = (int)payloadLen;
            byte[] payload = await ReadExactAsync(stream, count, timeoutMs, cancel).ConfigureAwait(false);
            return System.Text.Encoding.UTF8.GetString(payload);
        }

        private static ulong ReadUInt64BigEndian(byte[] buffer)
        {
            if (buffer == null || buffer.Length < 8) return 0UL;
            return ((ulong)buffer[0] << 56)
                 | ((ulong)buffer[1] << 48)
                 | ((ulong)buffer[2] << 40)
                 | ((ulong)buffer[3] << 32)
                 | ((ulong)buffer[4] << 24)
                 | ((ulong)buffer[5] << 16)
                 | ((ulong)buffer[6] << 8)
                 | buffer[7];
        }

        private static void WriteUInt64BigEndian(byte[] dest, ulong value)
        {
            if (dest == null || dest.Length < 8)
            {
                throw new ArgumentException("Destination buffer too small for UInt64");
            }
            dest[0] = (byte)(value >> 56);
            dest[1] = (byte)(value >> 48);
            dest[2] = (byte)(value >> 40);
            dest[3] = (byte)(value >> 32);
            dest[4] = (byte)(value >> 24);
            dest[5] = (byte)(value >> 16);
            dest[6] = (byte)(value >> 8);
            dest[7] = (byte)(value);
        }

        private static object InvokeOnMainThreadWithTimeout(Func<object> func, int timeoutMs)
        {
            if (func == null) return null;
            try
            {
                if (mainThreadId == 0)
                {
                    try { return func(); }
                    catch (Exception ex) { throw new InvalidOperationException($"Main thread handler error: {ex.Message}", ex); }
                }
                try
                {
                    if (Thread.CurrentThread.ManagedThreadId == mainThreadId)
                    {
                        return func();
                    }
                }
                catch { }

                object result = null;
                Exception captured = null;
                var tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                EditorApplication.delayCall += () =>
                {
                    try
                    {
                        result = func();
                    }
                    catch (Exception ex)
                    {
                        captured = ex;
                    }
                    finally
                    {
                        try { tcs.TrySetResult(true); } catch { }
                    }
                };

                bool completed = tcs.Task.Wait(timeoutMs);
                if (!completed)
                {
                    return null;
                }
                if (captured != null)
                {
                    throw new InvalidOperationException($"Main thread handler error: {captured.Message}", captured);
                }
                return result;
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException($"Failed to invoke on main thread: {ex.Message}", ex);
            }
        }

        private static bool IsValidJson(string text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return false;
            }

            text = text.Trim();
            if (
                (text.StartsWith("{") && text.EndsWith("}"))
                ||
                (text.StartsWith("[") && text.EndsWith("]"))
            )
            {
                try
                {
                    JToken.Parse(text);
                    return true;
                }
                catch
                {
                    return false;
                }
            }

            return false;
        }


        private const string InteractionModeKey = "InteractionMode";
        private const string ApplicationIdleTimeKey = "ApplicationIdleTime";
        private const string SessionKey_PrevInteractionMode = "StdioBridge_PrevInteractionMode";
        private const string SessionKey_PrevIdleTime = "StdioBridge_PrevIdleTime";
        private const string SessionKey_ThrottleOverrideActive = "StdioBridge_ThrottleOverrideActive";

        private static void ApplyNoThrottling()
        {
            try
            {
                int currentMode = EditorPrefs.GetInt(InteractionModeKey, 0);
                int currentIdle = EditorPrefs.GetInt(ApplicationIdleTimeKey, 4);

                // Only save original values on first activation (not after domain reload)
                if (!SessionState.GetBool(SessionKey_ThrottleOverrideActive, false))
                {
                    SessionState.SetInt(SessionKey_PrevInteractionMode, currentMode);
                    SessionState.SetInt(SessionKey_PrevIdleTime, currentIdle);
                    SessionState.SetBool(SessionKey_ThrottleOverrideActive, true);
                }

                // Always set prefs AND force-apply, even if already set.
                // After domain reload, Unity resets internal throttling state even though
                // EditorPrefs persist — UpdateInteractionModeSettings must be called again.
                EditorPrefs.SetInt(InteractionModeKey, 1);
                EditorPrefs.SetInt(ApplicationIdleTimeKey, 0);

                var method = typeof(EditorApplication).GetMethod(
                    "UpdateInteractionModeSettings",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
                if (method != null)
                {
                    method.Invoke(null, null);
                    UnityEngine.Debug.LogWarning("[MCP] Applied No Throttling for MCP responsiveness (InteractionMode=1, IdleTime=0)");
                }
                else
                {
                    McpLog.Warn("UpdateInteractionModeSettings not found — No Throttling may not take effect until Unity restart");
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to apply No Throttling: {ex.Message}");
            }
        }

        private static void RestoreThrottling()
        {
            try
            {
                if (!SessionState.GetBool(SessionKey_ThrottleOverrideActive, false))
                    return;

                int prevMode = SessionState.GetInt(SessionKey_PrevInteractionMode, 0);
                int prevIdle = SessionState.GetInt(SessionKey_PrevIdleTime, 4);

                EditorPrefs.SetInt(InteractionModeKey, prevMode);
                EditorPrefs.SetInt(ApplicationIdleTimeKey, prevIdle);

                var method = typeof(EditorApplication).GetMethod(
                    "UpdateInteractionModeSettings",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic);
                method?.Invoke(null, null);

                SessionState.SetBool(SessionKey_ThrottleOverrideActive, false);
                McpLog.Info("Restored Interaction Mode after MCP bridge stop");
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to restore Interaction Mode: {ex.Message}");
            }
        }

        private static void TickHeartbeat()
        {
            if (!isRunning) return;
            double now = EditorApplication.timeSinceStartup;
            if (now >= nextHeartbeatAt)
            {
                WriteHeartbeat(false);
                nextHeartbeatAt = now + 0.5f;
            }
        }

        public static void WriteHeartbeat(bool reloading, string reason = null)
        {
            try
            {
                string dir = Environment.GetEnvironmentVariable("UNITY_MCP_STATUS_DIR");
                if (string.IsNullOrWhiteSpace(dir))
                {
                    dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".unity-mcp");
                }
                Directory.CreateDirectory(dir);
                string filePath = Path.Combine(dir, $"unity-mcp-status-{ComputeProjectHash(Application.dataPath)}.json");

                string projectName = "Unknown";
                try
                {
                    string projectPath = Application.dataPath;
                    if (!string.IsNullOrEmpty(projectPath))
                    {
                        projectPath = projectPath.TrimEnd('/', '\\');
                        if (projectPath.EndsWith("Assets", StringComparison.OrdinalIgnoreCase))
                        {
                            projectPath = projectPath.Substring(0, projectPath.Length - 6).TrimEnd('/', '\\');
                        }
                        projectName = Path.GetFileName(projectPath);
                        if (string.IsNullOrEmpty(projectName))
                        {
                            projectName = "Unknown";
                        }
                    }
                }
                catch { }

                var payload = new
                {
                    unity_port = currentUnityPort,
                    reloading,
                    reason = reason ?? (reloading ? "reloading" : "ready"),
                    seq = heartbeatSeq,
                    project_path = Application.dataPath,
                    project_name = projectName,
                    unity_version = Application.unityVersion,
                    last_heartbeat = DateTime.UtcNow.ToString("O")
                };
                File.WriteAllText(filePath, JsonConvert.SerializeObject(payload), new System.Text.UTF8Encoding(false));
            }
            catch (Exception)
            {
            }
        }

        private static string ComputeProjectHash(string input)
        {
            try
            {
                using var sha1 = System.Security.Cryptography.SHA1.Create();
                byte[] bytes = System.Text.Encoding.UTF8.GetBytes(input ?? string.Empty);
                byte[] hashBytes = sha1.ComputeHash(bytes);
                var sb = new System.Text.StringBuilder();
                foreach (byte b in hashBytes)
                {
                    sb.Append(b.ToString("x2"));
                }
                return sb.ToString()[..8];
            }
            catch
            {
                return "default";
            }
        }
    }
}
