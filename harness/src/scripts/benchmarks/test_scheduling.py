"""Check admission safety without containers, subscriptions or inference calls."""

import asyncio
import gzip
import hashlib
import importlib
import io
import json
import os
import tempfile
import tarfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.benchmarks.scheduling import Admission, GIB, capacity
from scripts.benchmarks.shared_admission import SharedPool

POLICY = {
    "initial_trials": 3,
    "second_stage_trials": 6,
    "maximum_trials": 9,
    "cpu_reserve": 2,
    "memory_reserve_gib": 8,
    "disk_reserve_gib": 5,
    "disk_estimate_per_trial_gib": 2,
    "admission_poll_seconds": 0.001,
    "quota_refresh_seconds": 0.001,
    "backoff_seconds": 0.03,
    "backoff_statuses": [429, 500, 502, 503, 504],
}
SNAPSHOT = {"cpus": 20, "memory_available": 40 * GIB, "disk_free": 24 * GIB}


class RecoveryReportingTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.benchmarks.balanced")
        self.original = {
            "trial": "/saved/original",
            "agent": "hermes",
            "suite": "wild",
            "task": "fixture",
            "score": 0.9,
        }
        self.issue = {**self.original, "reason": "Incomplete native delegation"}

    def test_pending_recovery_preserves_evidence_but_excludes_its_score(self):
        rows, superseded = self.module.apply_lifecycle_recovery(
            [self.original], {"affected_trials": [self.issue]}
        )
        self.assertIsNone(rows[0]["score"])
        self.assertEqual(rows[0]["raw_score"], 0.9)
        self.assertFalse(superseded)

    def test_replacement_fills_one_slot_and_preserves_other_candidates(self):
        replacement = {
            **self.original,
            "trial": "/job/hermes-recovery/replacement",
            "score": 0.0,
        }
        other = {**self.original, "trial": "/saved/leon", "agent": "leon"}
        self.issue["replacement_trial"] = replacement["trial"]
        rows, superseded = self.module.apply_lifecycle_recovery(
            [self.original, replacement, other], {"affected_trials": [self.issue]}
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["score"], 0.0)
        self.assertEqual(rows[1], other)
        self.assertEqual(superseded, [self.original])

    def test_unassigned_results_cannot_add_extra_attempts(self):
        replacement = {**self.original, "trial": "/job/hermes-recovery/unassigned"}
        rows, _ = self.module.apply_lifecycle_recovery(
            [self.original, replacement], {"affected_trials": [self.issue]}
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["score"])

    def test_duplicate_slot_assignment_is_rejected(self):
        replacement = "/job/hermes-recovery/replacement"
        issues = [
            {**self.issue, "replacement_trial": replacement},
            {**self.issue, "trial": "/saved/other", "replacement_trial": replacement},
        ]
        with self.assertRaises(RuntimeError):
            self.module.apply_lifecycle_recovery([], {"affected_trials": issues})


class FixturePreparationTests(unittest.TestCase):
    def test_partial_or_modified_fixtures_cannot_reuse_a_preparation_receipt(self):
        module = importlib.import_module("scripts.benchmarks.balanced")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            environment = target / "environment"
            environment.mkdir()
            fixture = environment / "fixture.pdf"
            fixture.write_bytes(b"immutable fixture")
            receipt = target / "receipt.json"
            receipt.write_text(
                json.dumps(
                    {
                        "prepared_files": {
                            "environment/fixture.pdf": module.digest(fixture)
                        }
                    }
                )
            )
            self.assertTrue(module.prepared_task(target, receipt))
            fixture.unlink()
            self.assertFalse(module.prepared_task(target, receipt))
            fixture.write_bytes(b"different contents")
            self.assertFalse(module.prepared_task(target, receipt))


class RecoveryExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_launches_only_the_authorized_candidate_and_attempt_count(
        self,
    ):
        module = importlib.import_module("scripts.benchmarks.balanced")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "mounts.json").write_text("[]")
            (output / "network.yaml").write_text("services: {}")
            (output / "scheduling-policy.json").write_text(json.dumps(POLICY))
            job = MagicMock()
            job.run = AsyncMock()
            create = AsyncMock(return_value=job)
            with (
                patch.object(module.Job, "create", create),
                patch.object(module.scheduling, "capacity", return_value=9),
                patch.object(module.scheduling, "resources", return_value=SNAPSHOT),
                patch.object(module.scheduling, "Admission"),
                patch.object(module, "capture_resources"),
                patch.object(module, "cleanup_job"),
            ):
                await module.execute(
                    output,
                    {},
                    "wild",
                    {"name": "fixture"},
                    "hermes-recovery",
                    set(),
                    agents={"hermes": module.benchmark.AGENTS["hermes"]},
                    attempts=2,
                )
            config = create.call_args.args[0]
            self.assertEqual(config.n_attempts, 2)
            self.assertEqual(
                [agent.import_path for agent in config.agents],
                [module.benchmark.AGENTS["hermes"]],
            )
            self.assertTrue(config.environment.delete)
            self.assertEqual(config.retry.max_retries, 0)


class ArchiveReaderTests(unittest.TestCase):
    def test_repacked_layers_preserve_contents_and_archive_member_boundaries(self):
        module = importlib.import_module("scripts.benchmarks.load-wild-image")
        payload = b"layer contents" * 100
        precompressed = gzip.compress(payload, mtime=0)
        manifest = b"{}"
        source = io.BytesIO()
        with tarfile.open(fileobj=source, mode="w") as archive:
            layer = tarfile.TarInfo("layer.tar")
            layer.size = len(payload)
            layer.pax_headers = {"size": str(len(payload))}
            archive.addfile(layer, io.BytesIO(payload))

            compressed_layer = tarfile.TarInfo("compressed-layer.tar.gz")
            compressed_layer.size = len(precompressed)
            archive.addfile(compressed_layer, io.BytesIO(precompressed))

            metadata = tarfile.TarInfo("manifest.json")
            metadata.size = len(manifest)
            archive.addfile(metadata, io.BytesIO(manifest))

        source.seek(0)
        packed = io.BytesIO()
        with tempfile.TemporaryDirectory() as temporary:
            module.repack(
                source,
                packed,
                Path(temporary),
                {"layer.tar", "compressed-layer.tar.gz"},
            )

        packed.seek(0)
        with tarfile.open(fileobj=packed, mode="r:") as archive:
            layer = archive.next()
            compressed = archive.extractfile(layer).read()
            self.assertLess(layer.size, len(payload))
            self.assertEqual(gzip.decompress(compressed), payload)

            compressed_layer = archive.next()
            compressed = archive.extractfile(compressed_layer).read()
            self.assertEqual(compressed, precompressed)
            self.assertEqual(gzip.decompress(compressed), payload)

            metadata = archive.next()
            self.assertEqual(metadata.name, "manifest.json")
            self.assertEqual(archive.extractfile(metadata).read(), manifest)
            self.assertIsNone(archive.next())

    def test_archive_resume_delivers_and_hashes_each_upstream_byte_once(self):
        module = importlib.import_module("scripts.benchmarks.load-wild-image")
        first = MagicMock(status=206, headers={"Content-Range": "bytes 0-5/6"})
        first.read.side_effect = [b"abc", ConnectionResetError()]
        second = MagicMock(status=206, headers={"Content-Range": "bytes 3-5/6"})
        second.read.return_value = b"def"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(module, "ARCHIVE_SIZE", 6),
            patch.object(
                module.urllib.request, "urlopen", side_effect=[first, second]
            ) as open_url,
            patch.object(module.time, "sleep"),
        ):
            source = module.ResumableReader("https://example.test/image.tar")
            reader = module.VerifiedReader(source, Path(temporary))
            self.assertEqual(reader.read(3), b"abc")
            self.assertEqual(reader.read(3), b"def")
            self.assertEqual(reader.read(3), b"")
            self.assertEqual(
                reader.sha256.hexdigest(), hashlib.sha256(b"abcdef").hexdigest()
            )
            self.assertEqual(
                open_url.call_args_list[1].args[0].get_header("Range"), "bytes=3-"
            )
            source.close()

    def test_archive_resume_refuses_a_server_replaying_the_prefix(self):
        module = importlib.import_module("scripts.benchmarks.load-wild-image")
        wrong = MagicMock(status=200)
        with patch.object(module.urllib.request, "urlopen", return_value=wrong):
            source = module.ResumableReader("https://example.test/image.tar")
            source.offset = 3
            with self.assertRaisesRegex(RuntimeError, "resume offset"):
                source.read(3)


class CapacityTests(unittest.TestCase):
    def test_capacity_preserves_host_reserves_and_task_resource_limits(self):
        for cpus, memory, expected in (
            (2, 4_096, 6),
            (4, 4_096, 3),
            (2, 8_192, 3),
            (4, 8_192, 3),
            (8, 4_096, 2),
        ):
            with self.subTest(cpus=cpus, memory=memory):
                self.assertEqual(
                    capacity(SNAPSHOT, {"cpus": cpus, "memory_mb": memory}, POLICY),
                    expected,
                )
        self.assertEqual(
            capacity(
                {**SNAPSHOT, "disk_free": 6 * GIB},
                {"cpus": 2, "memory_mb": 4_096},
                POLICY,
            ),
            0,
        )


class SuitePriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_can_defer_candidates_after_all_references_finish(self):
        module = importlib.import_module("scripts.benchmarks.priority")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            status = {
                "status": "oracle validation",
                "pid": 123,
                "suite": "terminal",
                "task": "fixture",
            }
            (output / "status.json").write_text(json.dumps(status))
            for attempt in range(module.balanced.ORACLE_ATTEMPTS):
                trial = output / f"jobs/terminal--fixture/oracle/{attempt}"
                trial.mkdir(parents=True)
                (trial / "config.json").write_text("{}")
                (trial / "result.json").write_text("{}")
            with (
                patch.object(module, "active", return_value=True),
                patch.object(module.os, "kill") as kill,
                patch.object(module.balanced, "command") as command,
            ):
                await module.drain(output)

            self.assertIn(
                module.signal.SIGTERM,
                [call.args[1] for call in kill.call_args_list],
            )
            command.assert_called_once_with(
                ["systemctl", "--user", "stop", module.MAIN_SERVICE]
            )

    async def test_priority_handoff_preserves_unfinished_trials(self):
        module = importlib.import_module("scripts.benchmarks.priority")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            status = {
                "status": "preparing task",
                "pid": 123,
                "suite": "terminal",
                "task": "fixture",
            }
            (output / "status.json").write_text(json.dumps(status))
            trial = output / "jobs/terminal--fixture/native/attempt"
            trial.mkdir(parents=True)
            (trial / "config.json").write_text("{}")
            with (
                patch.object(module, "active", side_effect=[True, False]),
                patch.object(module.os, "kill") as kill,
                patch.object(module.balanced, "command") as command,
                patch.object(module.asyncio, "sleep"),
            ):
                await module.drain(output)

            self.assertEqual(
                [call.args[1] for call in kill.call_args_list],
                [module.signal.SIGSTOP, module.signal.SIGCONT],
            )
            command.assert_not_called()

    async def test_priority_handoff_stops_only_between_task_groups(self):
        module = importlib.import_module("scripts.benchmarks.priority")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            status = {
                "status": "preparing task",
                "pid": 123,
                "suite": "terminal",
                "task": "fixture",
            }
            (output / "status.json").write_text(json.dumps(status))
            with (
                patch.object(module, "active", return_value=True),
                patch.object(module.os, "kill") as kill,
                patch.object(module.balanced, "command") as command,
            ):
                await module.drain(output)

            self.assertEqual(
                [call.args[1] for call in kill.call_args_list],
                [
                    module.signal.SIGSTOP,
                    module.signal.SIGTERM,
                    module.signal.SIGCONT,
                ],
            )
            command.assert_called_once_with(
                ["systemctl", "--user", "stop", module.MAIN_SERVICE]
            )


class CatchupGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_groups_overlap_with_a_bounded_concurrency(self):
        module = importlib.import_module("scripts.benchmarks.catchup")
        active = 0
        peak = 0
        completed = []

        async def worker(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            completed.append(item)
            active -= 1

        await module.run_groups(range(9), 3, worker)
        self.assertEqual(peak, 3)
        self.assertCountEqual(completed, range(9))

    async def test_one_failed_group_does_not_cancel_unrelated_attempts(self):
        module = importlib.import_module("scripts.benchmarks.catchup")
        completed = []

        async def worker(item):
            if item == 1:
                raise RuntimeError("Task infrastructure failed")
            await asyncio.sleep(0.005)
            completed.append(item)

        with self.assertRaisesRegex(RuntimeError, "Task infrastructure failed"):
            await module.run_groups(range(6), 3, worker)
        self.assertCountEqual(completed, [0, 2, 3, 4, 5])

    async def test_paused_terminal_lane_does_not_block_catchup_start(self):
        module = importlib.import_module("scripts.benchmarks.catchup")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "status.json").write_text(
                json.dumps({"status": "stopped", "pid": 123})
            )
            with patch.object(module.asyncio, "sleep") as sleep:
                await module.handoff(output, "fixture.service", {})
            sleep.assert_not_called()


class SharedPoolTests(unittest.TestCase):
    def test_provider_backoff_survives_the_last_trial_releasing_its_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            policy = {**POLICY, "host_maximum_trials": 9, "legacy_trials": 3}
            first = SharedPool(output, policy)
            second = SharedPool(output, policy)
            task = {"cpus": 2, "memory_mb": 1_024}
            self.assertIsNone(first.acquire("first", task, SNAPSHOT, directory=output))
            path = output / "first/agent/requests.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"status": 429}) + "\n")
            first.release("first")
            self.assertEqual(
                second.acquire("second", task, SNAPSHOT), "shared_provider_backoff"
            )

    def test_independent_lanes_share_a_single_cpu_reservation_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            policy = {**POLICY, "host_maximum_trials": 9, "legacy_trials": 3}
            first = SharedPool(output, policy)
            second = SharedPool(output, policy)
            task = {"cpus": 8, "memory_mb": 1_024}
            self.assertIsNone(first.acquire("first", task, SNAPSHOT))
            self.assertIsNone(second.acquire("second", task, SNAPSHOT))
            self.assertEqual(
                second.acquire("third", task, SNAPSHOT), "shared_cpu_headroom"
            )
            first.release("first")
            self.assertIsNone(second.acquire("third", task, SNAPSHOT))

    def test_live_unregistered_runner_keeps_resources_during_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            policy = {**POLICY, "host_maximum_trials": 9, "legacy_trials": 3}
            pool = SharedPool(output, policy)
            legacy_pid = os.getpid() + 10_000
            (output / "status.json").write_text(
                json.dumps({"pid": legacy_pid, "status": "agent execution"})
            )
            task = {"cpus": 13, "memory_mb": 1_024}
            with patch("scripts.benchmarks.shared_admission.alive", return_value=True):
                self.assertEqual(
                    pool.acquire("catchup", task, SNAPSHOT), "shared_cpu_headroom"
                )
            (output / "status.json").write_text(
                json.dumps({"pid": legacy_pid, "status": "finished"})
            )
            self.assertIsNone(pool.acquire("catchup", task, SNAPSHOT))

    def test_image_transfer_preserves_root_and_workspace_reserves_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = {**POLICY, "host_maximum_trials": 9, "legacy_trials": 3}
            pool = SharedPool(Path(temporary), policy)
            task = {"cpus": 1, "memory_mb": 512}
            snapshot = {
                **SNAPSHOT,
                "disk_root_free": 20 * GIB,
                "disk_workspace_free": 9 * GIB,
            }
            self.assertIsNone(
                pool.acquire(
                    "image",
                    task,
                    snapshot,
                    kind="image_transfer",
                    disk_bytes=13 * GIB,
                    workspace_bytes=3 * GIB,
                )
            )
            self.assertEqual(
                pool.acquire("trial", task, snapshot), "shared_disk_headroom"
            )


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.snapshot = SNAPSHOT.copy()
        self.quota = True
        self.resource_patch = patch(
            "scripts.benchmarks.scheduling.resources",
            side_effect=lambda _: self.snapshot.copy(),
        )
        self.resource_patch.start()
        self.admission = Admission(
            self.directory,
            self.directory,
            {"cpus": 2, "memory_mb": 4_096},
            POLICY,
            lambda: self.quota,
            bool,
            6,
        )

    async def asyncTearDown(self):
        self.resource_patch.stop()
        self.temporary.cleanup()

    async def test_parallel_trials_ramp_after_validation_and_release_once(self):
        ready = asyncio.Event()
        events = [SimpleNamespace(trial_name=f"trial-{index}") for index in range(9)]

        async def trial(event):
            await self.admission.admit(event)
            if self.admission.peak == 3:
                ready.set()
            await ready.wait()
            await asyncio.sleep(0.02)
            await self.admission.release(event)
            await self.admission.release(event)

        await asyncio.wait_for(asyncio.gather(*(trial(event) for event in events)), 1)
        self.assertEqual(self.admission.completed, 9)
        self.assertEqual(self.admission.peak, 6)
        self.assertEqual(self.admission.active, set())
        records = [
            json.loads(line)
            for line in (self.directory / "scheduling.jsonl").read_text().splitlines()
        ]
        self.assertTrue(
            all(len(row["active"]) <= 3 for row in records if row["completed"] < 3)
        )

    async def test_quota_wait_does_not_cancel_an_active_trial(self):
        first = SimpleNamespace(trial_name="first")
        second = SimpleNamespace(trial_name="second")
        await self.admission.admit(first)
        self.quota = False
        await asyncio.sleep(0.002)
        pending = asyncio.create_task(self.admission.admit(second))
        await asyncio.sleep(0.01)
        self.assertFalse(pending.done())
        self.assertEqual(self.admission.active, {"first"})
        self.quota = True
        await asyncio.wait_for(pending, 1)

    async def test_memory_pressure_waits_until_headroom_returns(self):
        self.snapshot["memory_available"] = 10 * GIB
        pending = asyncio.create_task(
            self.admission.admit(SimpleNamespace(trial_name="queued"))
        )
        await asyncio.sleep(0.01)
        self.assertFalse(pending.done())
        self.snapshot["memory_available"] = 40 * GIB
        await asyncio.wait_for(pending, 1)

    async def test_provider_throttling_blocks_ramp_without_changing_agent_budget(self):
        path = self.directory / "first/agent/requests.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"status": 429}) + "\n")
        pending = asyncio.create_task(
            self.admission.admit(SimpleNamespace(trial_name="queued"))
        )
        await asyncio.sleep(0.01)
        self.assertFalse(pending.done())
        await asyncio.wait_for(pending, 1)
        self.assertFalse(self.admission.healthy)
        self.assertEqual(self.admission.limit, 3)
