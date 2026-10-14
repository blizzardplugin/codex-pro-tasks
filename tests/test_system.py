import concurrent.futures
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from codex_tasks.common import Problem, coordinator_url, validate_plan
from codex_tasks.runner import Runner, RunError, subscription_auth
from codex_tasks.server import Coordinator, initialize
from codex_tasks.store import Store
from codex_tasks.worker import Client, LocalLock, run


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = [1000.0]
        self.store = Store(self.root / "tasks.sqlite", lambda: self.clock[0])
        self.store.add_project({"id": "project", "remote": str(self.root / "source"), "verify": []})
        self.w1 = self.store.add_worker({"id": "pro-1", "projects": ["project"]})
        self.w2 = self.store.add_worker({"id": "pro-2", "projects": ["project"]})

    def tearDown(self): self.temp.cleanup()

    def task(self, **kwargs):
        return self.store.create_task(dict(project="project", title="Implementar", prompt="TASK_FILE=feature.txt", **kwargs))

    def completion(self, task, **kwargs):
        return dict(attempt=task["attempt"], ok=True, result=dict(summary="feito", changed=False, published=False), **kwargs)


class QueueTests(Fixture):
    def test_counts_include_tasks_outside_the_latest_250(self):
        for _ in range(251): self.task()
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot['tasks']), 250)
        self.assertEqual(snapshot['counts']['queued'], 251)

    def test_two_workers_claim_different_tasks_atomically(self):
        tasks = {self.task(), self.task()}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(self.store.claim, ["pro-1", "pro-2"]))
        self.assertEqual({x["task"]["id"] for x in outcomes}, tasks)
        self.assertIsNone(self.store.claim("pro-1")["task"])

    def test_dependency_is_not_claimed_until_parent_done(self):
        parent = self.task()
        child = self.task(dependencies=[parent])
        claimed = self.store.claim("pro-1")["task"]
        self.assertEqual(claimed["id"], parent)
        self.assertIsNone(self.store.claim("pro-2")["task"])
        self.store.complete("pro-1", parent, self.completion(claimed))
        self.assertEqual(self.store.claim("pro-2")["task"]["id"], child)

    def test_quota_pause_does_not_rotate_task_to_other_account(self):
        task = self.task()
        claimed = self.store.claim("pro-1")["task"]
        self.store.complete("pro-1", task, dict(attempt=claimed["attempt"], ok=False, error_kind="quota", error="limite"))
        self.assertEqual(self.store.claim("pro-1")["worker_state"], "paused")
        self.assertIsNone(self.store.claim("pro-2")["task"])
        self.store.action(task, "retry")
        self.assertIsNone(self.store.claim("pro-2")["task"])
        self.store.resume_worker("pro-1")
        self.assertEqual(self.store.claim("pro-1")["task"]["id"], task)

    def test_expired_lease_and_cancel_reject_old_owner(self):
        task = self.task()
        claimed = self.store.claim("pro-1")["task"]
        self.clock[0] += 181
        self.assertEqual(self.store.snapshot()["tasks"][0]["state"], "stalled")
        with self.assertRaises(Problem): self.store.heartbeat("pro-1", task, claimed["attempt"])
        self.store.action(task, "retry"); self.store.resume_worker("pro-1")
        newer = self.store.claim("pro-1")["task"]
        self.assertNotEqual(newer["attempt"], claimed["attempt"])
        with self.assertRaises(Problem): self.store.complete("pro-1", task, self.completion(claimed))
        self.store.action(task, "cancel")
        with self.assertRaises(Problem): self.store.heartbeat("pro-1", task, newer["attempt"])

    def test_worker_cannot_complete_another_workers_task(self):
        task = self.task(); claimed = self.store.claim("pro-1")["task"]
        with self.assertRaises(Problem): self.store.complete("pro-2", task, self.completion(claimed))

    def test_plan_auto_fans_out_once_and_validates_dependencies(self):
        task = self.task(kind="plan", publish=True)
        claimed = self.store.claim("pro-1")["task"]
        plan = {"tasks": [{"title": "A", "prompt": "do A", "depends_on": []}, {"title": "B", "prompt": "do B", "depends_on": [0]}]}
        self.store.complete("pro-1", task, dict(attempt=claimed["attempt"], ok=True, result={"plan": plan}))
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["tasks"]), 3)
        self.assertEqual(len([t for t in snapshot["tasks"] if t["kind"] == "code" and t["dependencies"]]), 1)
        with self.assertRaises(Problem): self.store.action(task, "apply-plan")
        for invalid in [True, [0], -1, 1]:
            with self.assertRaises(Problem): validate_plan({"tasks": [{"title": "A", "prompt": "A", "depends_on": [invalid]}]})

    def test_no_plain_worker_secret_or_attempt_in_admin_state(self):
        self.task(); self.store.claim("pro-1")
        snapshot = json.dumps(self.store.snapshot())
        self.assertNotIn(self.w1["token"], snapshot)
        self.assertNotIn('"attempt"', snapshot)
        with self.assertRaises(Problem): self.store.authenticate_worker("pro-1", "wrong")


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = Coordinator(("127.0.0.1", 0), self.store, "a" * 43)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); super().tearDown()

    def test_dashboard_is_public_but_api_requires_role_credentials(self):
        with urllib.request.urlopen(self.url) as response:
            self.assertIn(b"Codex Pro Tasks", response.read())
            self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(self.url + "/api/state")
        self.assertEqual(e.exception.code, 401)
        request = urllib.request.Request(self.url + "/api/state", headers={"Authorization": "Bearer " + self.w1["token"]})
        with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(request)
        request = urllib.request.Request(self.url + "/api/state", headers={"Authorization": "Bearer " + "a" * 43})
        with urllib.request.urlopen(request) as response: self.assertEqual(len(json.load(response)["workers"]), 2)
        client = Client(self.url, "pro-1", self.w1["token"])
        task = self.task(); claimed = client.post("/api/worker/claim", {})["task"]
        self.assertEqual(claimed["id"], task)
        client.post("/api/worker/heartbeat", {"task": task, "attempt": claimed["attempt"]})

    def test_url_credentials_and_plain_remote_http_are_rejected(self):
        for url in ["http://example.org", "https://user:password@example.org", "http://localhost/?token=x"]:
            with self.assertRaises(Problem): coordinator_url(url)


class RunnerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / "source"
        subprocess.run(["git", "init", "-b", "main", str(self.source)], check=True, capture_output=True)
        (self.source / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "Initial"], cwd=self.source, check=True, capture_output=True)
        self.base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.source, text=True).strip()
        self.fake = self.root / "fake-codex"
        shutil.copyfile(Path(__file__).parent / "fake_codex.py", self.fake); self.fake.chmod(0o755)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "", "CODEX_API_KEY": "", "TASK_TEST_MODE": "code", "TASK_TEST_AUTH": "chatgpt"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.runner = Runner(self.root / "jobs", str(self.fake), timeout=60)

    def execute(self, task, worker="pro-1"):
        claimed = self.store.claim(worker)["task"]
        result = self.runner.execute(claimed, threading.Event(), lambda: self.store.heartbeat(worker, task, claimed["attempt"]))
        self.store.complete(worker, task, dict(attempt=claimed["attempt"], ok=True, result=result))
        return result

    def test_real_git_checkout_commit_and_publication_are_isolated(self):
        task = self.task(publish=True)
        result = self.execute(task)
        self.assertTrue(result["published"])
        self.assertTrue(result["branch"].startswith("codex-tasks/"))
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "main"], cwd=self.source, text=True).strip(), self.base)
        self.assertEqual(subprocess.check_output(["git", "show", result["branch"] + ":feature.txt"], cwd=self.source, text=True), "implemented by offline test double\n")

    def test_dependency_branches_are_merged_only_into_child_branch(self):
        parent = self.store.create_task(dict(project="project",title="Parent",prompt="TASK_FILE=parent.txt",publish=True))
        child = self.store.create_task(dict(project="project",title="Child",prompt="TASK_FILE=child.txt",publish=True,dependencies=[parent]))
        self.execute(parent)
        result = self.execute(child, "pro-2")
        for name in ["parent.txt", "child.txt"]:
            self.assertIn("implemented", subprocess.check_output(["git", "show", result["branch"] + ":" + name], cwd=self.source, text=True))
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "main"], cwd=self.source, text=True).strip(), self.base)

    def test_plan_uses_readonly_mode_and_creates_no_git_changes(self):
        task = self.task(kind="plan", publish=True)
        result = self.execute(task)
        self.assertEqual(len(result["plan"]["tasks"]), 2)
        self.assertEqual(len(self.store.snapshot()["tasks"]), 3)

    def test_api_auth_is_refused_before_execution(self):
        with patch.dict(os.environ, {"TASK_TEST_AUTH": "api"}):
            with self.assertRaises(RunError) as e: subscription_auth(str(self.fake))
            self.assertEqual(e.exception.kind, "auth")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "dummy-test-value"}):
            with self.assertRaises(RunError): subscription_auth(str(self.fake))

    def test_quota_is_reported_without_reassignment(self):
        task = self.task(); claimed = self.store.claim("pro-1")["task"]
        with patch.dict(os.environ, {"TASK_TEST_MODE": "quota"}):
            with self.assertRaises(RunError) as e: self.runner.execute(claimed, threading.Event(), lambda: None)
        self.assertEqual(e.exception.kind, "quota")
        self.assertFalse(subprocess.check_output(["git", "branch", "--list", "codex-tasks/*"], cwd=self.source, text=True).strip())

    def test_failed_verification_never_pushes(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT data FROM projects WHERE id='project'").fetchone()
            project = json.loads(row[0]); project["verify"] = [[shutil.which("python3"), "-c", "raise SystemExit(2)"]]
            db.execute("UPDATE projects SET data=? WHERE id='project'", (json.dumps(project),))
        task = self.task(publish=True); claimed = self.store.claim("pro-1")["task"]
        with self.assertRaises(RunError) as e: self.runner.execute(claimed, threading.Event(), lambda: None)
        self.assertEqual(e.exception.kind, "verification")
        self.assertFalse(subprocess.check_output(["git", "branch", "--list", "codex-tasks/*"], cwd=self.source, text=True).strip())

    def test_cancelled_subprocess_stops_without_publication(self):
        task = self.task(publish=True); claimed = self.store.claim("pro-1")["task"]
        cancelled = threading.Event()
        timer = threading.Timer(0.7, cancelled.set); timer.start()
        try:
            with patch.dict(os.environ, {"TASK_TEST_MODE": "slow"}):
                with self.assertRaises(RunError) as e: self.runner.execute(claimed, cancelled, lambda: None)
            self.assertEqual(e.exception.kind, "lease_lost")
        finally: timer.cancel()

    def test_private_credential_filename_blocks_publication(self):
        task = self.store.create_task(dict(project="project",title="Unsafe",prompt="TASK_FILE=auth.json",publish=True))
        claimed = self.store.claim("pro-1")["task"]
        with self.assertRaisesRegex(RunError, "credenciais"): self.runner.execute(claimed, threading.Event(), lambda: None)
        self.assertFalse(subprocess.check_output(["git", "branch", "--list", "codex-tasks/*"], cwd=self.source, text=True).strip())

    def test_full_worker_http_cycle(self):
        self.store.clock = time.time
        server = Coordinator(("127.0.0.1", 0), self.store, "a" * 43)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            task = self.task(publish=True)
            config = self.root / "worker.json"
            config.write_text(json.dumps(dict(coordinator=f"http://127.0.0.1:{server.server_port}",worker_id="pro-1",token=self.w1["token"],allowed_projects=["project"],work_dir=str(self.root/"worker-jobs"),codex_command=str(self.fake),task_timeout_seconds=60)))
            self.assertTrue(run(config, once=True))
            self.assertEqual(next(t for t in self.store.snapshot()["tasks"] if t["id"]==task)["state"], "done")
        finally:
            server.shutdown(); server.server_close(); thread.join()


class InitTests(unittest.TestCase):
    def test_private_configuration_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = initialize(directory)
            self.assertGreater(len(json.loads(path.read_text())["admin_token"]), 32)
            if os.name != "nt": self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(Problem): initialize(directory)


if __name__ == "__main__": unittest.main()
