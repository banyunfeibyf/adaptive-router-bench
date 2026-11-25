"""Offline regression tests for consecutive turns. No credentials needed."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import dialogue


class ConversationTests(unittest.TestCase):
    def test_feedback_and_stop(self):
        feedback = None
        calls = []

        def fake_chat(model, messages, stage, timeout, usage):
            calls.append(model.name)
            usage.append({"stage": stage, "model": model.name, "usage": None, "status": "ok"})
            if stage != "classify":
                return "Checked."
            return json.dumps({"family": "writing", "plan": [
                {"stage": "compose", "goal": "summarize", "tier": "low"}],
                "answer": "Draft.", "previous_feedback": feedback})

        env = {"ROUTER_LOW_MODEL": "l", "ROUTER_MID_MODEL": "m", "ROUTER_HIGH_MODEL": "h"}
        with TemporaryDirectory() as folder, patch.dict("os.environ", env, clear=True), \
                patch.object(dialogue.router, "chat", fake_chat):
            def turn(message):
                args = ["dialogue.py", "--directory", folder, "--session", "synthetic", "--json", message]
                output = StringIO()
                calls.clear()
                with patch("sys.argv", args), redirect_stdout(output):
                    self.assertEqual(dialogue.main(), 0, output.getvalue())
                return json.loads(output.getvalue())
            first = turn("Write a caption")
            self.assertEqual(calls, ["l"])
            feedback = {"stage": "compose", "signal": "followup", "attribution": "model"}
            self.assertIsNone(turn("Now expand")["previous_feedback"])
            feedback = {"stage": "compose", "signal": "corrected", "attribution": "model"}
            first_correction = turn("You omitted the supplied unit")
            self.assertNotEqual(first_correction["previous_feedback"]["decision_id"],
                                first["decisions"][0]["decision_id"])
            self.assertEqual(calls, ["l"])
            turn("The unit is still missing")
            self.assertEqual(calls, ["l", "m"])
            state = Path(folder) / "state.sqlite"
            dialogue.router.memory(state, "explore", "on")
            feedback = None
            self.assertEqual(turn("停止探索")["exploration"], "off")
            with sqlite3.connect(state) as db:
                self.assertEqual(db.execute("SELECT value FROM config WHERE key='explore'").fetchone(), ("off",))


if __name__ == "__main__":
    unittest.main()
