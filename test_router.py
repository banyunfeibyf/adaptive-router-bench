"""Offline CLI and SQLite policy tests using simulated HTTP responses."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import router


class FakeEndpoint:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        payload = json.loads(request.data)
        messages = payload["messages"]
        is_classifier = messages[0]["content"].startswith("Classify")
        prompt = next(message["content"] for message in reversed(messages)
                      if message["role"] == "user" and not message["content"].startswith("Routing context"))
        self.requests.append((request, payload))
        if prompt == "simulate failure" and not is_classifier:
            error_body = io.BytesIO(b"sensitive upstream response")
            raise HTTPError(request.full_url, 403, "Denied", {}, error_body)
        if is_classifier:
            stages = (("verify", "mid"), ("compose", "high")) if prompt == "verify and write" else (("compose", "low"),)
            plan = [{"stage": stage, "tier": tier, "goal": "test"} for stage, tier in stages]
            answer = json.dumps({"plan": plan, "answer": "Lightweight answer", "family": "writing"})
        else:
            answer = f"Answer from {payload['model']}"
        return io.BytesIO(json.dumps({
            "model": f"{payload['model']}-version",
            "choices": [{"message": {"content": answer}}],
            "usage": {"total_tokens": 5},
        }).encode())


class RouterTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state.sqlite"
        self.endpoint = FakeEndpoint()
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        request_patch = patch.object(router, "build_opener", return_value=self.endpoint)
        request_patch.start()
        self.addCleanup(request_patch.stop)
        environment = {
            "ROUTER_BASE_URL": "https://fixture.invalid/v1",
            "ROUTER_API_KEY": "fixture-secret",
            "ROUTER_LOW_MODEL": "fixture-low",
            "ROUTER_MID_MODEL": "fixture-mid",
            "ROUTER_HIGH_MODEL": "fixture-high",
        }
        environment_patch = patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def run_cli(self, *args, status=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        command = ["router.py", "--state", str(self.state), *args]
        with patch.object(sys, "argv", command), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(router.main(), status)
        self.assertNotIn("fixture-secret", stdout.getvalue() + stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def ask(self, prompt, *flags, status=0):
        stdout, _ = self.run_cli("route", "--json", *flags, prompt, status=status)
        return json.loads(stdout)

    def test_direct_response_default_and_staged_handoff(self):
        light = self.ask("simple request", "--no-memory")
        self.assertFalse(self.state.exists())
        self.assertEqual(light["answer"], "Lightweight answer")
        self.assertEqual(light["family"], "writing")
        self.assertEqual(light["usage"]["known_token_totals"]["total_tokens"], 5)
        response, = light["decisions"]
        self.assertEqual(response["initial_tier"], "low")
        self.assertEqual(response["reported_model"], "fixture-low-version")
        self.assertTrue(response["selection_reason"])
        regular = self.ask("verify and write")
        self.assertEqual(len(regular["usage"]["calls"]), 2)
        self.assertEqual(regular["decisions"][0]["tier"], "high")
        staged = self.ask("verify and write", "--staged", "--family", "editorial")
        self.assertEqual(staged["family"], "editorial")
        self.assertEqual([entry["stage"] for entry in staged["decisions"]], ["verify", "compose"])
        self.assertEqual(staged["usage"]["known_token_totals"]["total_tokens"], 15)
        self.assertIn("Answer from fixture-mid", self.endpoint.requests[-1][1]["messages"][-1]["content"])

    def test_failure_and_manual_override_create_no_feedback_target(self):
        failure = self.ask("simulate failure", "--risk", "critical", status=2)
        self.assertIn("HTTP 403", failure["error"])
        self.assertNotIn("sensitive upstream response", json.dumps(failure))
        self.assertIsNone(failure["decisions"][0]["decision_id"])
        self.assertEqual(failure["usage"]["calls"][-1]["status"], "failed")
        _, stderr = self.run_cli("feedback", "--id", f"{failure['run_id']}.compose",
                                 "--signal", "corrected", "--reason", "simulated", status=2)
        self.assertIn("该阶段未完成", stderr)
        manual = self.ask("simple request", "--tier", "mid", "--risk", "critical")
        self.assertEqual(manual["decisions"][0]["selection"], "manual")
        self.assertEqual(manual["decisions"][0]["initial_tier"], "mid")
        self.assertIsNone(manual["decisions"][0]["decision_id"])


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state.sqlite"
        self.models = self.state.with_name("models.json")
        self.models.write_text(json.dumps({"low": "low-fixture", "mid": "mid-fixture", "high": "high-fixture"}))
        self.counter = itertools.count()
        self.call("explore", "on")

    def call(self, *args):
        return router.memory(self.state, *args)

    def identifier(self, sample):
        for index in self.counter:
            identity = f"run-{index}"
            selected = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], "big") % 10 == 0
            if selected == sample:
                return identity

    def choice(self, sample=False, identity=None, family="fixture", stage="compose", initial="mid",
               risk="ordinary", challenger=None):
        args = ["choose", "--id", identity or self.identifier(sample), "--family", family,
                "--stage", stage, "--initial", initial, "--models", str(self.models), "--risk", risk]
        if challenger:
            args.extend(("--challenger", challenger))
        return self.call(*args)

    def feedback(self, decision, signal="satisfied", attribution="model"):
        self.call("feedback", "--id", decision["id"], "--signal", signal,
                  "--attribution", attribution, "--reason", "simulated test")

    def test_lower_adoption_scope_rollback_cooldown_and_user_control(self):
        for _ in range(3):
            self.feedback(self.choice())
        for _ in range(3):
            trial = self.choice(sample=True)
            self.assertEqual((trial["tier"], trial["mode"]), ("low", "trial"))
            self.feedback(trial)
        adopted = self.choice()
        self.assertEqual((adopted["tier"], adopted["mode"]), ("low", "adopted"))
        self.assertEqual(self.choice(identity=adopted["id"]), adopted)
        self.assertEqual(self.choice(family="different")["mode"], "baseline")
        self.assertEqual(self.choice(stage="verify")["mode"], "baseline")
        self.assertEqual(self.choice(risk="critical")["mode"], "fixed")
        self.feedback(adopted, "corrected", "external")
        self.assertEqual(self.choice()["mode"], "adopted")
        self.call("explore", "off")
        self.assertEqual(self.choice()["mode"], "baseline")
        self.call("explore", "on")
        self.assertEqual(self.choice()["mode"], "adopted")
        self.feedback(adopted, "corrected")
        for _ in range(3):
            self.assertEqual(self.choice(sample=True)["mode"], "baseline")
        self.feedback(adopted, "corrected")  # A retry cannot extend the rollback watermark.
        for _ in range(3):
            trial = self.choice(sample=True)
            self.assertEqual(trial["mode"], "trial")
            self.feedback(trial)
        self.assertEqual(self.choice()["mode"], "adopted")

    def test_escalated_baseline_can_restart_exploration_after_new_evidence(self):
        self.call("explore", "off")
        for _ in range(2):
            self.feedback(self.choice(initial="low", challenger="novel-fixture"), "corrected")
        self.call("explore", "on")
        options = {"initial": "low", "challenger": "novel-fixture"}
        for _ in range(3):
            escalated = self.choice(sample=True, **options)
            self.assertEqual((escalated["tier"], escalated["mode"]), ("mid", "escalated"))
            self.feedback(escalated)
        for _ in range(3):
            trial = self.choice(sample=True, **options)
            self.assertEqual(trial["mode"], "trial")
            self.feedback(trial)
        self.assertEqual(self.choice(**options)["mode"], "adopted")
        self.call("explore", "off")
        self.assertEqual(self.choice(**options)["mode"], "escalated")

    def test_challenger_adoption_excludes_duplicates_unclear_and_other_bindings(self):
        options = {"challenger": "novel-fixture", "initial": "low"}
        trial = self.choice(sample=True, **options)
        self.feedback(trial)
        self.feedback(trial)
        self.feedback(self.choice(sample=True, **options), attribution="unclear")
        self.feedback(self.choice(sample=True, **options))
        self.assertEqual(self.choice(**options)["mode"], "baseline")
        self.feedback(self.choice(sample=True, **options))
        adopted = self.choice(**options)
        self.assertEqual((adopted["model"], adopted["tier"], adopted["mode"]),
                         ("novel-fixture", None, "adopted"))
        self.assertEqual(self.choice(initial="low", challenger="different-fixture")["mode"], "baseline")
        self.models.write_text(json.dumps({"low": "replacement", "mid": "mid-fixture", "high": "high-fixture"}))
        self.assertEqual(self.choice(**options)["mode"], "baseline")


if __name__ == "__main__":
    unittest.main()
