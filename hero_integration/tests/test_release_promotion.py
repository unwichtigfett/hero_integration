"""Release chain: GitHub CI gate and promotion of hero_int_dev to hero_int_release."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PROMOTE = ROOT / "ci/promote-release.sh"
WAIT = ROOT / "ci/wait-github-checks.sh"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True,
                          text=True, capture_output=True).stdout.strip()


class _Remotes:
    """Two bare repositories standing in for Forgejo and GitHub."""

    def __init__(self, root: Path):
        self.root, self.work = root, root / "work"
        self.forgejo, self.github = root / "forgejo.git", root / "github.git"
        for bare in (self.forgejo, self.github):
            git(root, "init", "-q", "--bare", str(bare))
        git(root, "init", "-q", "-b", "hero_int_dev", str(self.work))
        (self.work / "hero_integration").mkdir()
        self.commit("base", version="1.0.0")
        self.push("hero_int_dev", "hero_int_release")

    def commit(self, message, version=None, name="change.txt") -> str:
        if version:
            (self.work / "hero_integration/__init__.py").write_text(f'__version__ = "{version}"\n')
            (self.work / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n'
                                                      '[tool.ruff]\ntarget-version = "py310"\n')
        (self.work / name).write_text(message)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        return git(self.work, "rev-parse", "HEAD")

    def push(self, *branches, remotes=None):
        for bare in remotes or (self.forgejo, self.github):
            git(self.work, "push", "-q", str(bare), *[f"HEAD:refs/heads/{b}" for b in branches])

    def head(self, bare, ref) -> str:
        return git(bare, "rev-parse", ref)

    def promote(self, sha, **changes):
        env = {**os.environ, **GIT_ENV, "GITHUB_TOKEN": "", "GITHUB_REMOTE": str(self.github),
               "CI_COMMIT_SHA": sha, "CI_COMMIT_BRANCH": "hero_int_dev", "CI_PIPELINE_EVENT": "manual",
               "CI_COMMIT_MESSAGE": "MANUAL: RELEASE_APPROVED @ hero_int_dev", "CI_PIPELINE_STATUS": "success",
               **changes}
        return subprocess.run(["bash", str(PROMOTE)], env=env, text=True, capture_output=True)


class TestPromotion(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.repo = _Remotes(Path(tmp))
        self.base = self.repo.head(self.repo.forgejo, "hero_int_release")

    def test_fixture_channel_is_independent_of_the_enclosing_pipeline(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        with patch.dict(os.environ, {"CI_COMMIT_BRANCH": "unrelated-ci-branch"}):
            result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release^"), tested)

    def test_tested_commit_is_released_on_github_without_touching_forgejo(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.forgejo, 'hero_int_dev'), tested)
        self.assertEqual(self.repo.head(self.repo.forgejo, 'hero_int_release'), self.base)
        for bare in (self.repo.github,):
            release = self.repo.head(bare, "hero_int_release")
            self.assertEqual(self.repo.head(bare, "hero_int_dev"), release)
            self.assertEqual(self.repo.head(bare, f"{release}^"), tested)
            self.assertEqual(self.repo.head(bare, "v1.0.1^{commit}"), release)
            self.assertIn("[skip ci]", git(bare, "log", "-1", "--format=%s", release))
            self.assertEqual(git(bare, "diff", "--name-only", tested, release).split(),
                             ["hero_integration/__init__.py", "pyproject.toml"])
            pyproject = git(bare, "show", f"{release}:pyproject.toml")
            self.assertIn('version = "1.0.1"', pyproject)
            self.assertIn('target-version = "py310"', pyproject)

    def test_newer_dev_commit_leaves_the_installation_branch_unchanged(self):
        tested = self.repo.commit("feature")
        self.repo.commit("newer")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD moved", result.stderr)
        for bare in (self.repo.forgejo, self.repo.github):
            self.assertEqual(self.repo.head(bare, "hero_int_release"), self.base)

    def test_diverged_installation_branch_is_never_overwritten(self):
        git(self.repo.work, "checkout", "-q", "-b", "hotfix")
        self.repo.commit("hotfix on 1.0", name="hotfix.txt")
        self.repo.push("hero_int_release")
        hotfix = self.repo.head(self.repo.forgejo, "hero_int_release")
        git(self.repo.work, "checkout", "-q", "hero_int_dev")
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertNotEqual(result.returncode, 0)
        for bare in (self.repo.forgejo, self.repo.github):
            self.assertEqual(self.repo.head(bare, "hero_int_release"), hotfix)
            self.assertEqual(self.repo.head(bare, "hero_int_dev"), tested)

    def test_interrupted_promotion_is_completed_with_the_same_release_and_tag(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        github = self.repo.github
        self.repo.github = self.repo.root / "missing.git"  # GitHub unreachable on the first run
        self.assertNotEqual(self.repo.promote(tested).returncode, 0)
        self.repo.github = github
        self.assertEqual(self.repo.head(github, "hero_int_release"), self.base)
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repeated_promotion_reuses_the_release_and_tag(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        self.assertEqual(self.repo.promote(tested).returncode, 0)
        release = self.repo.head(self.repo.github, "hero_int_release")
        tag = self.repo.head(self.repo.github, "v1.0.1")
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), release)
        self.assertEqual(self.repo.head(self.repo.github, "v1.0.1"), tag)

    def test_reused_release_commit_with_extra_changes_is_rejected(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        for extra in ({"hero_integration/__init__.py": '__version__ = "1.0.1"\nimport os\nos.system("x")\n'},
                      {"pyproject.toml": '[project]\nname = "x"\nversion = "1.0.1"\ndependencies = ["evil"]\n'
                                         '[tool.ruff]\ntarget-version = "py310"\n'}):
            with self.subTest(extra=list(extra)):
                git(self.repo.work, "checkout", "-q", "--detach", tested)
                (self.repo.work / "hero_integration/__init__.py").write_text('__version__ = "1.0.1"\n')
                (self.repo.work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.1"\n'
                                                               '[tool.ruff]\ntarget-version = "py310"\n')
                for path, content in extra.items():
                    (self.repo.work / path).write_text(content)
                git(self.repo.work, "commit", "-q", "-am", "Release 1.0.1 [skip ci]")
                self.repo.push("hero_int_dev", remotes=(self.repo.github,))
                result = self.repo.promote(tested)
                git(self.repo.github, "update-ref", "refs/heads/hero_int_dev", tested)  # reset for the next case
                self.assertNotEqual(result.returncode, 0)
                for bare in (self.repo.forgejo, self.repo.github):
                    self.assertEqual(self.repo.head(bare, "hero_int_release"), self.base)

    def test_version_raised_by_hand_is_released_without_a_bump_commit(self):
        tested = self.repo.commit("minor release", version="1.1.0")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), tested)
        self.assertEqual(self.repo.head(self.repo.github, "v1.1.0^{commit}"), tested)


    def test_only_exact_manual_approval_after_green_gates_can_publish(self):
        tested = self.repo.commit("RELEASE_APPROVED")
        self.repo.push("hero_int_dev")
        cases = [{"CI_PIPELINE_EVENT": event} for event in ("push", "pull_request", "deployment", "")]
        cases += [{"CI_PIPELINE_STATUS": status} for status in ("running", "failure", "skipped", "")]
        cases += [{"CI_COMMIT_MESSAGE": message} for message in ("", "RELEASE_APPROVED",
                  "MANUAL: RELEASE_APPROVED extra @ hero_int_dev", "MANUAL: other @ hero_int_dev")]
        cases += [{"CI_COMMIT_BRANCH": "hero_int_release"}, {"CI_COMMIT_SHA": "HEAD"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertNotEqual(self.repo.promote(tested, **changes).returncode, 0)
                self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), self.base)
                self.assertEqual(git(self.repo.github, "tag", "-l"), "")

    def test_existing_tag_cannot_be_replaced(self):
        git(self.repo.github, "tag", "v1.0.1", self.base)
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        self.assertNotEqual(self.repo.promote(tested).returncode, 0)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), self.base)
        self.assertEqual(self.repo.head(self.repo.github, "v1.0.1"), self.base)

    def test_missing_stable_branch_is_not_created(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        git(self.repo.github, "update-ref", "-d", "refs/heads/hero_int_release")
        self.assertNotEqual(self.repo.promote(tested).returncode, 0)
        self.assertEqual(git(self.repo.github, "branch", "--list", "hero_int_release"), "")

    def test_local_test_remote_never_receives_a_credential(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        token = "test-only-release-credential"
        result = self.repo.promote(tested, GITHUB_TOKEN=token)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(token, result.stderr + result.stdout)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), self.base)

    def test_atomic_remote_rejection_preserves_all_refs(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        hook = self.repo.github / "hooks/update"
        hook.write_text('#!/bin/sh\ntest "$1" != refs/heads/hero_int_release\n')
        hook.chmod(0o700)
        self.assertNotEqual(self.repo.promote(tested).returncode, 0)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_dev"), tested)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), self.base)
        self.assertEqual(git(self.repo.github, "tag", "-l"), "")

    def test_same_preversioned_source_and_tag_replay_does_not_bump(self):
        tested = self.repo.commit("feature", version="1.2.0")
        self.repo.push("hero_int_dev")
        for _ in range(2):
            result = self.repo.promote(tested)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.repo.github, "tag", "-l"), "v1.2.0")
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), tested)

@unittest.skipUnless(shutil.which("jq"), "jq is installed in the Woodpecker step image")
class TestWaitForGitHub(unittest.TestCase):
    SHA = "a" * 40

    def wait(self, *answers, wait_seconds=0):
        with tempfile.TemporaryDirectory() as tmp:
            for index, runs in enumerate(answers):
                Path(tmp, f"answer{index}").write_text(json.dumps({"check_runs": runs}))
            curl = Path(tmp, "curl")
            curl.write_text('#!/bin/sh\nn=$(cat "$STATE" 2>/dev/null || echo 0)\necho $((n+1)) > "$STATE"\n'
                            'last=$(ls "$DIR"/answer* | wc -l)\n[ "$n" -ge "$last" ] && n=$((last-1))\n'
                            'cat "$DIR/answer$n"\n')
            curl.chmod(0o755)
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "STATE": f"{tmp}/state", "DIR": tmp,
                   "CI_COMMIT_SHA": self.SHA, "POLL_SECONDS": "0", "WAIT_SECONDS": str(wait_seconds)}
            return subprocess.run(["sh", str(WAIT)], env=env, text=True, capture_output=True)

    @staticmethod
    def check_run(name, status="completed", conclusion="success", started="2026-11-03T10:00:00Z"):
        return {"head_sha": TestWaitForGitHub.SHA, "name": name, "status": status, "conclusion": conclusion, "started_at": started,
                "app": {"slug": "github-actions"}}

    def test_waits_until_both_checks_succeeded(self):
        result = self.wait([self.check_run("package"), self.check_run("install-test", "in_progress", None)],
                           [self.check_run("package"), self.check_run("install-test")], wait_seconds=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_check_stops_the_chain(self):
        result = self.wait([self.check_run("package"), self.check_run("install-test", conclusion="failure")], wait_seconds=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("failure", result.stderr)

    def test_missing_check_times_out(self):
        result = self.wait([self.check_run("package")])
        self.assertEqual(result.returncode, 1)

    def test_rerun_replaces_an_older_failure(self):
        result = self.wait([self.check_run("package"), self.check_run("install-test", conclusion="failure"),
                            self.check_run("install-test", started="2026-11-03T11:00:00Z")])
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_success_for_another_commit_is_rejected(self):
        wrong = {**self.check_run("package"), "head_sha": "b" * 40}
        self.assertNotEqual(self.wait([wrong, self.check_run("install-test")]).returncode, 0)
