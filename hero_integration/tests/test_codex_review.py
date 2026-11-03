"""Codex review before the push: read-only run on the exact commit, gate and stored report."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("codex_review", ROOT / "ci/codex_review.py")
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def finding(severity="P2", **values):
    return {"severity": severity, "title": "Titel", "file": "a.py", "line": 3, "scenario": "x", "fix": "y", **values}


class _Codex:
    """Stands in for `codex exec`: records the call and writes the given review."""

    def __init__(self, review, returncode=0):
        self.review, self.returncode, self.calls = review, returncode, []

    def __call__(self, command, input, text, capture_output, timeout):
        cwd = Path(command[command.index("--cd") + 1])
        self.calls.append({"command": command, "prompt": input, "files": sorted(p.name for p in cwd.iterdir())})
        output = Path(command[command.index("--output-last-message") + 1])
        if self.review is not None:
            output.write_text(json.dumps(self.review))
        return subprocess.CompletedProcess(command, self.returncode, "", "boom" if self.returncode else "")


class TestCodexReview(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.repo = self.tmp / "repo"
        self.git("init", "-q", str(self.repo), cwd=self.tmp)
        (self.repo / "a.py").write_text("x = 1\n")
        self.git("add", "-A"); self.git("commit", "-qm", "base"); self.git("tag", "release")
        (self.repo / "a.py").write_text("x = 2\n")
        self.git("add", "-A"); self.git("commit", "-qm", "change")
        self.head = self.git("rev-parse", "HEAD")
        (self.repo / "a.py").write_text("uncommitted\n")  # the live checkout may be dirty
        self.reports = self.tmp / "reports"
        patcher = patch.object(cr, "REPORTS", self.reports)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env={**os.environ, **GIT_ENV},
                              check=True, text=True, capture_output=True).stdout.strip()

    def main(self, codex, *extra):
        with patch.dict(os.environ, GIT_ENV):
            return cr.main(["--repo", str(self.repo), "--base", "release", "--head", self.head, *extra], runner=codex)

    def test_p1_blocks_and_the_report_is_stored(self):
        codex = _Codex({"summary": "s", "findings": [finding("P1", title="Datenverlust")]})
        self.assertEqual(self.main(codex), 1)
        report = (self.reports / f"{self.head}.md").read_text()
        self.assertIn("Freigabe gesperrt", report)
        self.assertIn("Datenverlust", report)

    def test_codex_runs_read_only_on_the_exact_commit_not_the_working_copy(self):
        codex = _Codex({"summary": "s", "findings": []})
        self.assertEqual(self.main(codex), 0)
        command = codex.calls[0]["command"]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
        self.assertEqual(command[command.index("--config") + 1], 'model_reasoning_effort="high"')
        self.assertIn("--ephemeral", command)
        worktree = Path(command[command.index("--cd") + 1])
        self.assertNotEqual(worktree, self.repo)
        self.assertFalse(worktree.exists())  # removed after the review
        self.assertIn(f"..{self.head}", codex.calls[0]["prompt"])
        self.assertEqual((self.repo / "a.py").read_text(), "uncommitted\n")
        self.assertNotIn(str(worktree), self.git("worktree", "list"))

    def test_p2_passes_report_mode_never_blocks_and_reports_are_reused(self):
        codex = _Codex({"summary": "s", "findings": [finding("P2")]})
        self.assertEqual(self.main(codex), 0)
        self.assertEqual(self.main(codex), 0)
        self.assertEqual(len(codex.calls), 1)  # second run reuses the stored report
        self.assertEqual(self.main(codex, "--effort", "xhigh"), 0)
        self.assertEqual(len(codex.calls), 2)  # another effort is a new review
        self.assertEqual(self.main(codex, "--effort", "xhigh", "--block", "P2"), 1)  # reused review, stricter threshold
        self.assertEqual(len(codex.calls), 2)
        self.assertIn("Sperre ab: P2", (self.reports / f"{self.head}.md").read_text())  # report is rewritten
        strict = _Codex({"summary": "s", "findings": [finding("P2")]})
        self.assertEqual(self.main(strict, "--block", "P2", "--force"), 1)
        self.assertEqual(self.main(_Codex({"summary": "s", "findings": [finding("P1")]}), "--block", "none", "--force"), 0)

    def test_failed_or_malformed_codex_runs_fail_closed(self):
        for codex in (_Codex(None, returncode=1), _Codex(None), _Codex({"summary": "s", "findings": [finding("P0")]})):
            with self.subTest(codex=codex), self.assertRaises(cr.ReviewError):
                self.main(codex, "--force")
        self.assertFalse((self.reports / f"{self.head}.json").exists())

    def test_nothing_to_review_since_the_release(self):
        codex = _Codex({"summary": "s", "findings": [finding("P1")]})
        with patch.dict(os.environ, GIT_ENV):
            self.assertEqual(cr.main(["--repo", str(self.repo), "--base", "release", "--head", "release"], runner=codex), 0)
        self.assertEqual(codex.calls, [])


class TestPipelineHasNoApiKeyJob(unittest.TestCase):
    def test_the_subscription_review_replaces_the_api_job(self):
        self.assertNotIn("ai-review", (ROOT / ".github/workflows/ci.yml").read_text())
        self.assertNotIn("ai-review", (ROOT / ".woodpecker.yml").read_text())
        self.assertFalse((ROOT / "ci/ai_review.py").exists())


if __name__ == "__main__":
    unittest.main()
