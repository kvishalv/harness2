# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`--bench local` on a small folder of tasks, with the demo's deterministic editors."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import config
from harness2.benchmarks.local import LocalAdapter
from harness2.core import judge, schedule, tree
from harness2.core.spec import ChannelSpec
from harness2.demo import BASE_SCRIPT, DemoProposer
from harness2.substrate.opencode import OPENCODE_SURFACE

LEDGERS = {
    "ledger-1": ("100.25\n-20.10\n5.00\n", "85.15"),
    "ledger-2": ("35.50\n-10.25\n-5.00\n", "20.25"),
}
CHECK = """import sys
from pathlib import Path

answer = Path("answer.txt")
sys.exit(0 if answer.is_file() and answer.read_text() == Path(sys.argv[1]).read_text() else 1)
"""


class ScriptedAdapter(LocalAdapter):
    """Stands in for OpenCode: runs the rendered harness's helper script in the rollout's copy."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def _run_agent(self, prompt, workspace, run_dir, timeout):
        self.calls.append(run_dir.name)
        files = workspace / "files"
        if (files / "answer.txt").exists():
            raise RuntimeError("this rollout started from another rollout's edits")
        done = subprocess.run(
            [sys.executable, str(workspace / "scripts/summarize.py"), str(files / "ledger.csv")],
            capture_output=True,
            text=True,
            check=True,
        )
        (files / "answer.txt").write_text(done.stdout, encoding="utf-8")
        event = {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {
            "status": "completed", "input": {"command": "python3 scripts/summarize.py"},
            "output": done.stdout}}}  # fmt: skip
        (run_dir / "opencode_session.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


@unittest.skipUnless(importlib.util.find_spec("yaml"), "needs PyYAML: uv pip install -e '.[local]'")
class LocalBenchTests(unittest.TestCase):
    def test_tasks_folder_runs_through_the_method(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        tasks = root / "tasks"
        for name, (amounts, total) in LEDGERS.items():
            task = tasks / "accounting" / name
            (task / "files").mkdir(parents=True)
            (task / "files/ledger.csv").write_text("amount\n" + amounts, encoding="utf-8")
            (task / "task.md").write_text(
                "Sum every signed amount in ledger.csv.\n", encoding="utf-8"
            )
            (task / "expected.txt").write_text(f"Total: {total}\n", encoding="utf-8")
            (task / "check.py").write_text(CHECK, encoding="utf-8")
            (task / "task.yaml").write_text(
                f'outputs:\n  - answer.txt\nverify: \'"{sys.executable}" "$TASK_DIR/check.py" '
                f'"$TASK_DIR/expected.txt"\'\n',
                encoding="utf-8",
            )
        (tasks / "bench.yaml").write_text("work: reconcile a ledger\n", encoding="utf-8")
        base = {name: "" for name in OPENCODE_SURFACE.always_on}
        base["systemprompt.md"] = "Reconcile the ledger using python scripts/summarize.py.\n"
        base["scripts/summarize.py"] = BASE_SCRIPT
        tree.write_tree(OPENCODE_SURFACE, base, root / "base", stubs=False)

        with patch.dict(config.PROPOSER_MODEL_BY_CHANNEL, {"opencode": "local/deterministic"}):
            adapter = ScriptedAdapter(tasks, base_harness=root / "base", runs_root=root / "runs")
            other = LocalAdapter(root, runs_root=root / "runs")
        self.assertEqual(adapter.tasks(), ["accounting/ledger-1", "accounting/ledger-2"])
        self.assertEqual(adapter.domain_of("accounting/ledger-1"), "accounting")
        self.assertNotEqual(adapter.spec.name, other.spec.name)
        self.assertRegex(adapter.spec.name, r"^local-[0-9a-f]{8}$")
        self.assertEqual(adapter.spec.prompts.work, "reconcile a ledger")
        prompt = adapter.task_prompt("accounting/ledger-1")
        self.assertIn("answer.txt", prompt)
        self.assertNotIn("check.py", prompt)

        proposer = DemoProposer(ChannelSpec("opencode", "local/deterministic"))
        result = schedule.run_domain(
            "accounting",
            adapter.tasks(),
            1,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root / "adapt",
            library_root=root / "library",
        )
        self.assertEqual((result["done"], result["failed"]), (2, 0))

        namespace = f"{adapter.spec.name}-oc-agg1"
        summary = json.loads(
            (
                adapter.spec.runs_root / "adapt" / namespace / "accounting__ledger-1/summary.json"
            ).read_text()
        )
        chosen = Path(summary["selections"]["agg"]["chosen_run_dir"])
        self.assertEqual(adapter.deliverable_text(chosen).split("\n")[-1], "Total: 85.15")
        self.assertIn("scripts/summarize.py", adapter.trajectory(chosen, obs_trunc=1000))
        self.assertFalse((tasks / "accounting/ledger-1/files/answer.txt").exists())

        rows = judge.judge_selections(adapter, list(summary["selections"].values()))
        self.assertTrue(rows and all(r.graded and r.all_pass for r in rows))
        self.assertEqual({(r.n_passed, r.n_criteria) for r in rows}, {(1, 1)})
        base_run = Path(summary["base"][0]["run_dir"])
        failed = adapter.score(base_run, "accounting/ledger-1")
        self.assertEqual(failed, {"n_passed": 0, "n_criteria": 1, "all_pass": False})
        self.assertIsInstance(failed["all_pass"], bool)
        reports = list((adapter.spec.runs_root / "verify").glob("*.log"))
        self.assertTrue(reports)
        self.assertEqual([p for p in chosen.rglob("*") if p.suffix == ".log"], [])

        (tasks / "notes/plain").mkdir(parents=True)
        (tasks / "notes/plain/task.md").write_text("Write a note.\n", encoding="utf-8")
        self.assertIsNone(adapter.score(chosen, "notes/plain"))

        calls = len(adapter.calls)
        again = adapter.execute("accounting/ledger-1", None, summary["base"][0]["run_id"])
        self.assertEqual((again, len(adapter.calls)), (base_run, calls))
        (base_run / "workspace/files/answer.txt").unlink()
        self.assertFalse(adapter.delivered(base_run))
        self.assertFalse(adapter.artifacts_intact(base_run))


if __name__ == "__main__":
    unittest.main()
