"""OpenAI code review gate: diff grouping, response parsing, blocking and the CI wiring."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ai_review", ROOT / "ci/ai_review.py")
ai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ai)


def finding(severity="P2", **values):
    return {"severity": severity, "title": "Titel", "file": "a.py", "line": 3, "scenario": "x", "fix": "y", **values}


def payload(review, status="completed"):
    return {"status": status, "output": [{"type": "reasoning"}, {"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(review)}]}]}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestGrouping(unittest.TestCase):
    def test_files_are_grouped_cut_and_reported_when_beyond_the_limit(self):
        diffs = [("a.py", "a" * 40), ("b.py", "b" * 40), ("c.py", "c" * 300), ("d.py", "d" * 40)]
        groups, unreviewed = ai.chunks(diffs, limit=100, max_chunks=2)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0], "a" * 40 + "b" * 40)
        self.assertIn("Diff von c.py gekürzt", groups[1])
        self.assertEqual(unreviewed, ["d.py"])

    def test_translations_bundles_and_binaries_are_not_sent(self):
        for path in ("hero_integration/translations/de.csv", "hero_integration/public/dist/js/x.js", "a.png"):
            self.assertFalse(ai.reviewable(path), path)
        for path in ("hero_integration/services/x.py", "hero_integration/hero_integration/doctype/x/x.json", "ci/x.sh"):
            self.assertTrue(ai.reviewable(path), path)


class TestOpenAIResponses(unittest.TestCase):
    def test_structured_review_is_read_from_the_message_output(self):
        review = {"summary": "ok", "findings": [finding("P1")]}
        self.assertEqual(ai.parse(payload(review)), review)

    def test_incomplete_refused_or_empty_responses_fail_closed(self):
        for bad in (payload({"summary": "", "findings": []}, status="incomplete"),
                    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
                    {"status": "completed", "output": []}):
            with self.subTest(bad=bad), self.assertRaises(ai.ReviewError):
                ai.parse(bad)

    def test_request_uses_strict_json_schema_and_retries_rate_limits(self):
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            if len(sent) == 1:
                raise urllib.error.HTTPError(ai.API_URL, 429, "slow down", {}, io.BytesIO(b"{}"))
            return _Response(json.dumps(payload({"summary": "s", "findings": []})).encode())

        with patch.object(ai.time, "sleep"):
            self.assertEqual(ai.request("key", "model-x", "diff", opener=opener)["summary"], "s")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["model"], "model-x")
        self.assertTrue(sent[0]["text"]["format"]["strict"])
        self.assertEqual(sent[0]["text"]["format"]["schema"]["properties"]["findings"]["items"]["properties"]["severity"]["enum"],
                         ["P1", "P2", "P3"])

    def test_authentication_errors_are_not_retried(self):
        calls = []

        def opener(request, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(ai.API_URL, 401, "no", {}, io.BytesIO(b'{"error":"bad key"}'))

        with self.assertRaisesRegex(ai.ReviewError, "HTTP 401"):
            ai.request("key", "m", "diff", opener=opener)
        self.assertEqual(len(calls), 1)


class TestGate(unittest.TestCase):
    def test_blocking_threshold(self):
        findings = [finding("P1"), finding("P2"), finding("P3")]
        self.assertEqual([f["severity"] for f in ai.blocking(findings, "P1")], ["P1"])
        self.assertEqual([f["severity"] for f in ai.blocking(findings, "P2")], ["P1", "P2"])
        self.assertEqual(ai.blocking(findings, "none"), [])
        self.assertEqual([f["severity"] for f in ai.blocking(findings, "unknown")], ["P1"])  # fails safe

    def run_main(self, reviews, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                                            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                                                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
            run("init", "-q")
            (repo / "a.py").write_text("x = 1\n")
            run("add", "-A"); run("commit", "-qm", "base"); run("tag", "base")
            (repo / "a.py").write_text("x = 2\n")
            (repo / "de.csv").write_text("a,b\n")
            run("add", "-A"); run("commit", "-qm", "change")
            summary = repo / "summary.md"
            answers = iter(reviews)
            cwd = os.getcwd()
            os.chdir(repo)
            try:
                with patch.dict(os.environ, {"OPENAI_API_KEY": "key", "AI_REVIEW_BASE": "base",
                                             "GITHUB_STEP_SUMMARY": str(summary), **(env or {})}), \
                        patch.object(ai, "request", side_effect=lambda *a, **k: next(answers)) as request:
                    code = ai.main()
                return code, summary.read_text(), request, json.loads((repo / "ai-review.json").read_text())
            finally:
                os.chdir(cwd)

    def test_p1_blocks_and_is_reported(self):
        code, summary, request, report = self.run_main([{"summary": "s", "findings": [finding("P1", title="Datenverlust")]}])
        self.assertEqual(code, 1)
        self.assertIn("Freigabe gesperrt", summary)
        self.assertIn("Datenverlust", summary)
        self.assertNotIn("de.csv", request.call_args.args[2])  # translations are not sent
        self.assertEqual(report["findings"][0]["severity"], "P1")

    def test_p2_only_passes_and_report_mode_never_blocks(self):
        self.assertEqual(self.run_main([{"summary": "s", "findings": [finding("P2")]}])[0], 0)
        self.assertEqual(self.run_main([{"summary": "s", "findings": [finding("P1")]}], {"AI_REVIEW_BLOCK": "none"})[0], 0)

    def test_missing_key_keeps_the_release_blocked(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(ai.main(), 2)


class TestCiWiring(unittest.TestCase):
    def test_review_runs_on_dev_pushes_and_woodpecker_waits_for_it_there(self):
        import yaml

        job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["ai-review"]
        self.assertIn("hero_int_dev", job["if"])
        step = next(s for s in job["steps"] if s.get("run") == "python ci/ai_review.py")
        self.assertEqual(step["env"]["OPENAI_API_KEY"], "${{ secrets.OPENAI_API_KEY }}")
        wait = next(s for s in yaml.safe_load((ROOT / ".woodpecker.yml").read_text())["steps"] if s["name"] == "wait-github")
        self.assertIn('REQUIRED_CHECKS="package install-test ai-review"', " ".join(wait["commands"]))


if __name__ == "__main__":
    unittest.main()
