"""Compile and exercise the complete production cache without launching Unity.

Only external Unity, clock, logging and service peers are supplied by the fixture.
The production source is passed unchanged to the installed C# compiler. The clock
peer resolves DateTimeOffset in the cache's namespace; no cache methods or policy
are copied, rewritten or replaced. Each example runs in a fresh process.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


EXTERNAL_PEERS = r"""
using System;
using System.Collections.Generic;
using UnityEditor;

namespace Fixture
{
    public static class Clock
    {
        public const long Epoch = 1700000000000;
        public static long UnixMs = Epoch;
        public static void Advance(double seconds)
        {
            UnixMs = Epoch + (long)(seconds * 1000);
            EditorApplication.timeSinceStartup = seconds;
        }
    }
}

namespace MCPForUnity.Editor.Services
{
    // An external wall-clock peer, not a replacement cache implementation.
    internal readonly struct DateTimeOffset
    {
        private readonly long _milliseconds;
        private DateTimeOffset(long milliseconds) { _milliseconds = milliseconds; }
        public static DateTimeOffset UtcNow => new(Fixture.Clock.UnixMs);
        public long ToUnixTimeMilliseconds() => _milliseconds;
    }
    internal static class TestRunStatus
    {
        public static bool IsRunning = false;
        public static object Mode = null;
        public static long? StartedUnixMs = null;
        public static long? FinishedUnixMs = null;
    }
    internal static class TestJobManager { public static string CurrentJobId = null; }
}
namespace MCPForUnity.Editor.Helpers
{
    internal static class McpLog
    {
        public static readonly List<string> Errors = new();
        public static void Error(string message) { Errors.Add(message); }
    }
}
namespace MCPForUnity.Editor.Tools
{
    internal static class BatchExecute
    {
        public static int Calls;
        public static int GetMaxCommandsPerBatch() { Calls++; return 25; }
    }
}
namespace UnityEditor
{
    [AttributeUsage(AttributeTargets.Class)]
    public sealed class InitializeOnLoadAttribute : Attribute { }
    public enum PlayModeStateChange { EnteredPlayMode }
    public static class EditorApplication
    {
        public static double timeSinceStartup;
        public static bool isCompiling, isPlaying, isPaused, isUpdating;
        public static bool isPlayingOrWillChangePlaymode;
        public static event Action update;
        public static event Action<PlayModeStateChange> playModeStateChanged;
        public static void Tick() => update?.Invoke();
        public static void PlayModeChanged() =>
            playModeStateChanged?.Invoke(PlayModeStateChange.EnteredPlayMode);
    }
    public static class AssemblyReloadEvents
    {
        public static event Action beforeAssemblyReload, afterAssemblyReload;
        public static void Before() => beforeAssemblyReload?.Invoke();
        public static void After() => afterAssemblyReload?.Invoke();
    }
    public static class AssetDatabase
    {
        public static int GuidCalls;
        public static string AssetPathToGUID(string path) { GuidCalls++; return "guid:" + path; }
    }
}
namespace UnityEditorInternal
{
    public static class InternalEditorUtility { public static bool isApplicationActive = true; }
}
namespace UnityEditor.SceneManagement
{
    public struct Scene { public string path, name; }
    public static class EditorSceneManager
    {
        public static Scene Active = new() { path = "Assets/Idle.unity", name = "Idle" };
        public static int Calls;
        public static Scene GetActiveScene() { Calls++; return Active; }
    }
}
namespace UnityEngine
{
    public enum RuntimePlatform { OSXEditor }
    public static class Application
    {
        public static string unityVersion = "fixture";
        public static RuntimePlatform platform = RuntimePlatform.OSXEditor;
        public static bool isBatchMode;
    }
}
"""


HARNESS = r"""
using System;
using System.Reflection;
using System.Threading.Tasks;
using Fixture;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEditorInternal;

internal static class Program
{
    private static JObject Snapshot() => EditorStateCache.GetSnapshot();
    private static object Cached() => typeof(EditorStateCache)
        .GetField("_cached", BindingFlags.NonPublic | BindingFlags.Static).GetValue(null);
    private static void Tick(double seconds) { Clock.Advance(seconds); EditorApplication.Tick(); }

    public static int Main(string[] args)
    {
        try
        {
            var result = new JObject();
            result["initialized"] = Snapshot();
            if (args[0] == "never_ticked")
            {
                Clock.Advance(206);
                result["current"] = Snapshot();
            }
            else
            {
                Tick(1); // Establish tracked state through the actual update callback.
                var initial = Snapshot();
                result["initial"] = initial;
                var cached = Cached();
                int builds = BatchExecute.Calls;
                int guidCalls = AssetDatabase.GuidCalls;
                switch (args[0])
                {
                    case "idle":
                        Tick(2); Tick(206); Tick(207);
                        result["current"] = Snapshot();
                        break;
                    case "frozen":
                        Clock.Advance(207);
                        result["current"] = Snapshot();
                        Clock.Advance(300);
                        result["later"] = Snapshot();
                        break;
                    case "throttle":
                        Tick(2);
                        result["accepted"] = Snapshot();
                        int sceneCalls = EditorSceneManager.Calls;
                        Tick(2.5); Tick(2.99);
                        result["throttled"] = Snapshot();
                        result["throttled_scene_calls"] = EditorSceneManager.Calls - sceneCalls;
                        Tick(3);
                        result["current"] = Snapshot();
                        break;
                    case "scene":
                        EditorSceneManager.Active = new Scene { path = "Assets/Changed.unity", name = "Changed" };
                        Tick(2);
                        result["changed"] = Snapshot();
                        Tick(3);
                        result["current"] = Snapshot();
                        break;
                    case "activity":
                        EditorApplication.isUpdating = true;
                        Tick(2);
                        result["changed"] = Snapshot();
                        Tick(3);
                        result["current"] = Snapshot();
                        break;
                    case "compilation":
                        EditorApplication.isCompiling = true;
                        Tick(1.1);
                        result["started"] = Snapshot();
                        EditorApplication.isCompiling = false;
                        Tick(1.2);
                        result["current"] = Snapshot();
                        break;
                    case "clones":
                        Tick(2);
                        var mutated = Snapshot();
                        mutated["observed_at_unix_ms"] = -1;
                        mutated["editor"]["active_scene"]["name"] = "corrupted";
                        ((JArray)mutated["activity"]["reasons"]).Add("corrupted");
                        result["current"] = Snapshot();
                        result["distinct_clones"] = !ReferenceEquals(mutated, result["current"]);
                        break;
                    case "background":
                        InternalEditorUtility.isApplicationActive = false;
                        Tick(2);
                        result["background_tick"] = Snapshot();
                        var backgroundCache = Cached();
                        int backgroundBuilds = BatchExecute.Calls;
                        Clock.Advance(208);
                        result["background_read"] = Snapshot();
                        result["background_cache_same"] = ReferenceEquals(backgroundCache, Cached());
                        result["background_read_builds"] = BatchExecute.Calls - backgroundBuilds;
                        InternalEditorUtility.isApplicationActive = true;
                        result["focused_without_tick"] = Snapshot();
                        Tick(209);
                        result["current"] = Snapshot();
                        break;
                    case "callbacks":
                        Clock.Advance(2);
                        EditorApplication.isPlaying = true;
                        EditorApplication.isPlayingOrWillChangePlaymode = true;
                        EditorApplication.PlayModeChanged();
                        result["playmode"] = Snapshot();
                        Clock.Advance(3); AssemblyReloadEvents.Before();
                        result["before_reload"] = Snapshot();
                        Clock.Advance(4); AssemblyReloadEvents.After();
                        result["current"] = Snapshot();
                        break;
                    case "allocation":
                        // Warm the idle path before measuring tick-only allocation.
                        Tick(2);
                        long allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
                        for (int second = 3; second < 1003; second++) Tick(second);
                        result["idle_allocated_bytes"] = GC.GetAllocatedBytesForCurrentThread() - allocatedBefore;
                        result["current"] = Snapshot();
                        break;
                    case "readers":
                        Tick(2);
                        var readers = new Task[4];
                        for (int index = 0; index < readers.Length; index++)
                            readers[index] = Task.Run(() => {
                                for (int read = 0; read < 100; read++)
                                {
                                    var clone = Snapshot();
                                    clone["editor"]["active_scene"]["name"] = "reader mutation";
                                }
                            });
                        Task.WaitAll(readers); // No fixture threads survive handback.
                        result["joined_readers"] = readers.Length;
                        result["current"] = Snapshot();
                        break;
                    default: throw new ArgumentException("Unknown fixture example");
                }
                result["cache_same"] = ReferenceEquals(cached, Cached());
                result["builds"] = BatchExecute.Calls - builds;
                result["guid_calls"] = AssetDatabase.GuidCalls - guidCalls;
            }
            if (McpLog.Errors.Count != 0) throw new Exception(string.Join("\n", McpLog.Errors));
            Console.WriteLine(result.ToString(Formatting.None));
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
"""


class EditorStateCacheLivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="editor-cache-liveness-harness-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.dotnet = Path(os.environ.get("EDITOR_CACHE_DOTNET", shutil.which("dotnet") or "dotnet")).resolve()
        cls.root = cls.dotnet.parent
        compiler = cls.root / "sdk/8.0.204/Roslyn/bincore/csc.dll"
        references = sorted((cls.root / "packs/Microsoft.NETCore.App.Ref").glob("8.*/ref/net8.0"))
        runtimes = sorted((cls.root / "shared/Microsoft.NETCore.App").glob("8.*"))
        newtonsoft = Path(os.environ.get(
            "EDITOR_CACHE_NEWTONSOFT",
            "/Applications/Unity/Hub/Editor/6000.3.8f1/Unity.app/Contents/Resources/Scripting/Managed/Newtonsoft.Json.dll",
        ))
        if not compiler.is_file() or not references or not runtimes or not newtonsoft.is_file():
            raise RuntimeError("Installed SDK 8.0.204, net8 references/runtime and Unity Newtonsoft assembly are required; no install, restore or skip is allowed.")
        source = Path(__file__).resolve().parents[2] / "MCPForUnity/Editor/Services/EditorStateCache.cs"
        peers = cls.directory / "ExternalPeers.cs"
        peers.write_text(EXTERNAL_PEERS)
        harness = cls.directory / "Harness.cs"
        harness.write_text(HARNESS)
        cls.assembly = cls.directory / "CacheHarness.dll"
        response = cls.directory / "compile.rsp"
        arguments = ["-nologo", "-nostdlib+", "-target:exe", "-langversion:latest", "-warnaserror+", f'-out:"{cls.assembly}"']
        arguments += [f'-reference:"{path}"' for path in sorted(references[-1].glob("*.dll"))]
        arguments += [f'-reference:"{newtonsoft}"', f'"{peers}"', f'"{harness}"', f'"{source}"']
        response.write_text("\n".join(arguments) + "\n")
        cls.environment = dict(os.environ, DOTNET_CLI_HOME=str(cls.directory), DOTNET_NOLOGO="1")
        compiled = subprocess.run([str(cls.dotnet), str(compiler), "-noconfig", f"@{response}"], capture_output=True, text=True, timeout=60, env=cls.environment)
        print(f"Compiler: {compiler}\nProduction source: {source}\nReference pack: {references[-1]}\nNewtonsoft: {newtonsoft}\nCompiler exit: {compiled.returncode}", file=sys.stderr)
        if compiled.stdout or compiled.stderr:
            print(compiled.stdout + compiled.stderr, file=sys.stderr)
        if compiled.returncode:
            raise RuntimeError("Production cache compilation failed:\n" + compiled.stdout + compiled.stderr)
        shutil.copyfile(newtonsoft, cls.directory / "Newtonsoft.Json.dll")
        (cls.directory / "CacheHarness.runtimeconfig.json").write_text(json.dumps({
            "runtimeOptions": {"tfm": "net8.0", "framework": {"name": "Microsoft.NETCore.App", "version": runtimes[-1].name}}
        }))

    def example(self, name):
        run = subprocess.run([str(self.dotnet), str(self.assembly), name], capture_output=True, text=True, timeout=20, env=self.environment)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(run.stderr, "", "The fixture must not hide runtime errors.")
        return json.loads(run.stdout)

    def assert_only_observation_changed(self, first, second):
        first, second = dict(first), dict(second)
        first.pop("observed_at_unix_ms")
        second.pop("observed_at_unix_ms")
        self.assertEqual(first, second)

    def assert_no_rebuild(self, result):
        self.assertTrue(result["cache_same"])
        self.assertEqual(result["builds"], 0)
        self.assertEqual(result["guid_calls"], 0)

    def test_focused_idle_ticks_refresh_liveness_without_rebuilding_state(self):
        result = self.example("idle")
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700000207000)
        self.assert_only_observation_changed(result["initial"], result["current"])
        self.assert_no_rebuild(result)

    def test_focused_reads_do_not_make_a_frozen_editor_fresh(self):
        result = self.example("frozen")
        self.assertEqual(result["initial"], result["current"])
        self.assertEqual(result["initial"], result["later"])
        self.assertEqual(1700000207000 - result["current"]["observed_at_unix_ms"], 206000)
        self.assert_no_rebuild(result)

    def test_an_editor_that_never_ticks_remains_stale(self):
        result = self.example("never_ticked")
        self.assertEqual(result["initialized"], result["current"])
        self.assertEqual(1700000206000 - result["current"]["observed_at_unix_ms"], 206000)

    def test_idle_ticks_keep_the_one_second_throttle(self):
        result = self.example("throttle")
        self.assertEqual(result["accepted"]["observed_at_unix_ms"], 1700000002000)
        self.assertEqual(result["accepted"], result["throttled"])
        self.assertEqual(result["throttled_scene_calls"], 0)
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700000003000)
        self.assert_no_rebuild(result)

    def test_scene_changes_rebuild_once_then_idle_preserves_sequence_and_state_since(self):
        result = self.example("scene")
        self.assertEqual(result["builds"], 1)
        self.assertEqual(result["changed"]["sequence"], result["initial"]["sequence"] + 1)
        self.assertEqual(result["changed"]["editor"]["active_scene"], {"path": "Assets/Changed.unity", "name": "Changed", "guid": "guid:Assets/Changed.unity"})
        self.assertEqual(result["changed"]["activity"]["since_unix_ms"], 1700000002000)
        self.assert_only_observation_changed(result["changed"], result["current"])
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700000003000)

    def test_meaningful_activity_changes_rebuild_and_idle_preserves_activity_since(self):
        result = self.example("activity")
        self.assertEqual(result["builds"], 1)
        self.assertEqual(result["changed"]["activity"]["phase"], "asset_import")
        self.assertTrue(result["changed"]["assets"]["is_updating"])
        self.assertEqual(result["changed"]["activity"]["since_unix_ms"], 1700000002000)
        self.assert_only_observation_changed(result["changed"], result["current"])

    def test_compilation_edges_still_bypass_the_throttle(self):
        result = self.example("compilation")
        self.assertEqual(result["builds"], 2)
        self.assertEqual(result["started"]["activity"]["phase"], "compiling")
        self.assertEqual(result["started"]["compilation"]["last_compile_started_unix_ms"], 1700000001100)
        self.assertEqual(result["current"]["activity"]["phase"], "idle")
        self.assertEqual(result["current"]["compilation"]["last_compile_finished_unix_ms"], 1700000001200)

    def test_snapshots_are_isolated_deep_clones_after_idle_ticks(self):
        result = self.example("clones")
        self.assertTrue(result["distinct_clones"])
        self.assert_only_observation_changed(result["initial"], result["current"])
        self.assertEqual(result["initial"]["observed_at_unix_ms"], 1700000001000)
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700000002000)
        self.assert_no_rebuild(result)

    def test_background_restamping_does_not_refresh_focused_liveness_without_a_tick(self):
        result = self.example("background")
        self.assertEqual(result["background_read"]["observed_at_unix_ms"], 1700000208000)
        self.assert_only_observation_changed(result["background_tick"], result["background_read"])
        self.assertTrue(result["background_cache_same"])
        self.assertEqual(result["background_read_builds"], 0)
        self.assertEqual(result["focused_without_tick"], result["background_tick"])
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700000209000)
        self.assertTrue(result["current"]["editor"]["is_focused"])
        self.assertEqual(result["builds"], 2)

    def test_playmode_and_domain_reload_callbacks_keep_rebuilding(self):
        result = self.example("callbacks")
        self.assertEqual(result["builds"], 3)
        self.assertTrue(result["playmode"]["editor"]["play_mode"]["is_playing"])
        self.assertEqual(result["playmode"]["activity"]["reasons"], ["playmode"])
        self.assertTrue(result["before_reload"]["compilation"]["is_domain_reload_pending"])
        self.assertEqual(result["before_reload"]["compilation"]["last_domain_reload_before_unix_ms"], 1700000003000)
        self.assertFalse(result["current"]["compilation"]["is_domain_reload_pending"])
        self.assertEqual(result["current"]["compilation"]["last_domain_reload_after_unix_ms"], 1700000004000)

    def test_a_thousand_idle_ticks_allocate_no_json_and_keep_the_cached_content(self):
        result = self.example("allocation")
        self.assertEqual(result["idle_allocated_bytes"], 0)
        self.assert_no_rebuild(result)
        self.assert_only_observation_changed(result["initial"], result["current"])
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700001002000)

    def test_concurrent_snapshot_readers_keep_clones_isolated_and_are_joined(self):
        result = self.example("readers")
        self.assertEqual(result["joined_readers"], 4)
        self.assert_only_observation_changed(result["initial"], result["current"])
        self.assertEqual(result["current"]["observed_at_unix_ms"], 1700000002000)
        self.assert_no_rebuild(result)


if __name__ == "__main__":
    unittest.main()
