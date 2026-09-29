using System;
using System.Collections.Generic;
using System.Reflection;
using NUnit.Framework;
using MCPForUnity.Editor.Services;

namespace MCPForUnityTests.Editor.Services
{
    /// <summary>
    /// Tests for TestJobManager's per-job InitTimeoutMs feature.
    /// Uses reflection to manipulate internal state since StartJob triggers a real test run.
    /// </summary>
    public class TestJobManagerInitTimeoutTests
    {
        private FieldInfo _jobsField;
        private FieldInfo _currentJobIdField;
        private MethodInfo _getJobMethod;
        private MethodInfo _persistMethod;
        private MethodInfo _restoreMethod;
        private Type _testJobType;

        private string _originalJobId;
        private object _originalJobs;

        [SetUp]
        public void SetUp()
        {
            var asm = typeof(MCPServiceLocator).Assembly;
            var managerType = asm.GetType("MCPForUnity.Editor.Services.TestJobManager");
            Assert.NotNull(managerType, "Could not find TestJobManager");

            _testJobType = asm.GetType("MCPForUnity.Editor.Services.TestJob");
            Assert.NotNull(_testJobType, "Could not find TestJob");

            _jobsField = managerType.GetField("Jobs", BindingFlags.NonPublic | BindingFlags.Static);
            Assert.NotNull(_jobsField, "Could not find Jobs field");

            _currentJobIdField = managerType.GetField("_currentJobId", BindingFlags.NonPublic | BindingFlags.Static);
            Assert.NotNull(_currentJobIdField, "Could not find _currentJobId field");

            _getJobMethod = managerType.GetMethod("GetJob", BindingFlags.NonPublic | BindingFlags.Static);
            Assert.NotNull(_getJobMethod, "Could not find GetJob method");

            _persistMethod = managerType.GetMethod("PersistToSessionState", BindingFlags.NonPublic | BindingFlags.Static);
            Assert.NotNull(_persistMethod, "Could not find PersistToSessionState method");

            _restoreMethod = managerType.GetMethod("TryRestoreFromSessionState", BindingFlags.NonPublic | BindingFlags.Static);
            Assert.NotNull(_restoreMethod, "Could not find TryRestoreFromSessionState method");

            // Snapshot original state
            _originalJobId = _currentJobIdField.GetValue(null) as string;
            // We'll restore _currentJobId in TearDown; Jobs dictionary is shared static state
        }

        [TearDown]
        public void TearDown()
        {
            // Restore original state
            _currentJobIdField.SetValue(null, _originalJobId);
            // Clean up any test jobs we inserted
            var jobs = _jobsField.GetValue(null) as System.Collections.IDictionary;
            jobs?.Remove("test-init-timeout-job");
            jobs?.Remove("test-init-timeout-default");
            jobs?.Remove("test-init-timeout-short");
            jobs?.Remove("test-init-timeout-slow");
            jobs?.Remove("test-init-timeout-persist");
        }

        [Test]
        public void GetJob_WithCustomInitTimeout_UsesPerJobTimeout()
        {
            // Arrange: insert a job with a custom init timeout and a start time far enough in the
            // past to exceed the default 120s but within the custom 300s.
            InsertUninitializedJob("test-init-timeout-job", "PlayMode", startedMsAgo: 200_000, initTimeoutMs: 300_000);

            // Act: GetJob should NOT auto-fail because 200s < 300s custom timeout
            var result = _getJobMethod.Invoke(null, new object[] { "test-init-timeout-job" });

            // Assert: job should still be running
            Assert.AreEqual(TestJobStatus.Running, StatusOf(result),
                "Job with 300s custom timeout should not auto-fail after 200s");
        }

        [Test]
        public void GetJob_WithShorterCustomTimeout_AutoFailsBeforeTheDefault()
        {
            InsertUninitializedJob("test-init-timeout-short", "EditMode", startedMsAgo: 40_000, initTimeoutMs: 30_000);

            var result = _getJobMethod.Invoke(null, new object[] { "test-init-timeout-short" });

            Assert.AreEqual(TestJobStatus.Failed, StatusOf(result),
                "Job with 30s custom timeout should auto-fail after 40s");
        }

        [Test]
        public void GetJob_WithDefaultTimeout_WaitsForASlowDomainReload()
        {
            // A large project's domain reload can take well over 15s before RunStarted; the default
            // must not fail such a run (the run went on anyway and its results were lost).
            InsertUninitializedJob("test-init-timeout-slow", "EditMode", startedMsAgo: 60_000, initTimeoutMs: 0);

            var result = _getJobMethod.Invoke(null, new object[] { "test-init-timeout-slow" });

            Assert.AreEqual(TestJobStatus.Running, StatusOf(result),
                "Job with default timeout should still be running after 60s");
        }

        [Test]
        public void GetJob_WithDefaultTimeout_AutoFailsAfter120Seconds()
        {
            InsertUninitializedJob("test-init-timeout-default", "EditMode", startedMsAgo: 130_000, initTimeoutMs: 0);

            // Act: GetJob should auto-fail because 130s > 120s default
            var result = _getJobMethod.Invoke(null, new object[] { "test-init-timeout-default" });

            // Assert: job should be failed
            Assert.AreEqual(TestJobStatus.Failed, StatusOf(result),
                "Job with default timeout should auto-fail after 130s");
        }

        private object InsertUninitializedJob(string jobId, string mode, long startedMsAgo, long initTimeoutMs)
        {
            var jobs = _jobsField.GetValue(null) as System.Collections.IDictionary;
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

            var job = Activator.CreateInstance(_testJobType);
            _testJobType.GetProperty("JobId").SetValue(job, jobId);
            _testJobType.GetProperty("Status").SetValue(job, TestJobStatus.Running);
            _testJobType.GetProperty("Mode").SetValue(job, mode);
            _testJobType.GetProperty("StartedUnixMs").SetValue(job, now - startedMsAgo);
            _testJobType.GetProperty("LastUpdateUnixMs").SetValue(job, now - startedMsAgo);
            _testJobType.GetProperty("TotalTests").SetValue(job, null); // Not initialized yet
            _testJobType.GetProperty("InitTimeoutMs").SetValue(job, initTimeoutMs); // 0 = use default
            _testJobType.GetProperty("FailuresSoFar").SetValue(job, new List<TestJobFailure>());

            jobs[jobId] = job;
            _currentJobIdField.SetValue(null, jobId);
            return job;
        }

        private TestJobStatus StatusOf(object job)
        {
            return (TestJobStatus)_testJobType.GetProperty("Status").GetValue(job);
        }

        [Test]
        public void InitTimeoutMs_SurvivesPersistAndRestore()
        {
            // Arrange: insert a job with custom InitTimeoutMs
            var jobs = _jobsField.GetValue(null) as System.Collections.IDictionary;
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

            var job = Activator.CreateInstance(_testJobType);
            _testJobType.GetProperty("JobId").SetValue(job, "test-init-timeout-persist");
            _testJobType.GetProperty("Status").SetValue(job, TestJobStatus.Running);
            _testJobType.GetProperty("Mode").SetValue(job, "PlayMode");
            _testJobType.GetProperty("StartedUnixMs").SetValue(job, now);
            _testJobType.GetProperty("LastUpdateUnixMs").SetValue(job, now);
            _testJobType.GetProperty("TotalTests").SetValue(job, null);
            _testJobType.GetProperty("InitTimeoutMs").SetValue(job, 90_000L);
            _testJobType.GetProperty("FailuresSoFar").SetValue(job, new List<TestJobFailure>());

            jobs["test-init-timeout-persist"] = job;
            _currentJobIdField.SetValue(null, "test-init-timeout-persist");

            // Act: persist then restore (simulates domain reload)
            _persistMethod.Invoke(null, new object[] { true });
            // Clear in-memory state
            jobs.Remove("test-init-timeout-persist");
            _currentJobIdField.SetValue(null, null);
            // Restore from SessionState
            _restoreMethod.Invoke(null, null);

            // Assert: restored job should have the same InitTimeoutMs
            var restoredJobs = _jobsField.GetValue(null) as System.Collections.IDictionary;
            Assert.IsTrue(restoredJobs.Contains("test-init-timeout-persist"),
                "Job should be restored from SessionState");

            var restoredJob = restoredJobs["test-init-timeout-persist"];
            var restoredTimeout = (long)_testJobType.GetProperty("InitTimeoutMs").GetValue(restoredJob);
            Assert.AreEqual(90_000L, restoredTimeout,
                "InitTimeoutMs should survive persist/restore cycle");
        }
    }
}
