"""Release chain: GitHub CI gate and promotion of hero_int_dev to hero_int_1.0."""
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
CANDIDATE = ROOT / "ci/promote-candidate.sh"
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
        self.push("hero_int_dev", "hero_int_1.0")

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

    def promote(self, sha, channel="hero_int_dev"):
        env = {**os.environ, **GIT_ENV, "COMMIT_SHA": sha, "GITHUB_REMOTE": str(self.github),
               "RELEASE_CHANNEL": channel, "CI_COMMIT_BRANCH": channel}
        return subprocess.run(["bash", str(PROMOTE)], env=env, text=True, capture_output=True)


class TestPromotion(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.repo = _Remotes(Path(tmp))
        self.base = self.repo.head(self.repo.forgejo, "hero_int_1.0")

    def test_fixture_channel_is_independent_of_the_enclosing_pipeline(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        with patch.dict(os.environ, {"CI_COMMIT_BRANCH": "unrelated-ci-branch"}):
            result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_1.0^"), tested)

    def test_opt_bootstrap_and_repeat_leave_standard_branches_and_tags_unchanged(self):
        git(self.repo.work,'checkout','-q','-b','hero_int_dev_opt')
        tested=self.repo.commit('optimized materializer')
        self.repo.push('hero_int_dev_opt',remotes=(self.repo.github,))
        standard_dev=self.repo.head(self.repo.github,'hero_int_dev')
        for attempt in range(2):
            result=self.repo.promote(tested,'hero_int_dev_opt')
            self.assertEqual(result.returncode,0,result.stderr)
            released=self.repo.head(self.repo.github,'hero_int_opt')
            self.assertEqual(self.repo.head(self.repo.github,'hero_int_dev_opt'),released)
            self.assertEqual(self.repo.head(self.repo.github,'opt-v1.0.1^{commit}'),released)
            self.assertEqual(self.repo.head(self.repo.github,'hero_int_dev'),standard_dev)
            self.assertEqual(self.repo.head(self.repo.github,'hero_int_1.0'),self.base)
        self.assertEqual(git(self.repo.github,'tag','--list','v*'),'')

    def test_opt_never_overwrites_a_diverged_opt_release(self):
        git(self.repo.work,'checkout','-q','-b','hero_int_dev_opt')
        tested=self.repo.commit('optimized materializer')
        self.repo.push('hero_int_dev_opt',remotes=(self.repo.github,))
        git(self.repo.work,'checkout','-q','-b','other-opt',self.base)
        unrelated=self.repo.commit('unrelated release',name='unrelated')
        self.repo.push('hero_int_opt',remotes=(self.repo.github,))
        result=self.repo.promote(tested,'hero_int_dev_opt')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.repo.head(self.repo.github,'hero_int_opt'),unrelated)
        self.assertEqual(self.repo.head(self.repo.github,'hero_int_dev_opt'),tested)

    def test_tested_commit_is_released_on_github_without_touching_forgejo(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.forgejo, 'hero_int_dev'), tested)
        self.assertEqual(self.repo.head(self.repo.forgejo, 'hero_int_1.0'), self.base)
        for bare in (self.repo.github,):
            release = self.repo.head(bare, "hero_int_1.0")
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
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("moved past", result.stdout)
        for bare in (self.repo.forgejo, self.repo.github):
            self.assertEqual(self.repo.head(bare, "hero_int_1.0"), self.base)

    def test_diverged_installation_branch_is_never_overwritten(self):
        git(self.repo.work, "checkout", "-q", "-b", "hotfix")
        self.repo.commit("hotfix on 1.0", name="hotfix.txt")
        self.repo.push("hero_int_1.0")
        hotfix = self.repo.head(self.repo.forgejo, "hero_int_1.0")
        git(self.repo.work, "checkout", "-q", "hero_int_dev")
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertNotEqual(result.returncode, 0)
        for bare in (self.repo.forgejo, self.repo.github):
            self.assertEqual(self.repo.head(bare, "hero_int_1.0"), hotfix)
            self.assertEqual(self.repo.head(bare, "hero_int_dev"), tested)

    def test_interrupted_promotion_is_completed_with_the_same_release_and_tag(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        github = self.repo.github
        self.repo.github = self.repo.root / "missing.git"  # GitHub unreachable on the first run
        self.assertNotEqual(self.repo.promote(tested).returncode, 0)
        self.repo.github = github
        self.assertEqual(self.repo.head(github, "hero_int_1.0"), self.base)
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repeated_promotion_reuses_the_release_and_tag(self):
        tested = self.repo.commit("feature")
        self.repo.push("hero_int_dev")
        self.assertEqual(self.repo.promote(tested).returncode, 0)
        release = self.repo.head(self.repo.github, "hero_int_1.0")
        tag = self.repo.head(self.repo.github, "v1.0.1")
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_1.0"), release)
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
                self.assertIn("changes more than the version", result.stderr)
                for bare in (self.repo.forgejo, self.repo.github):
                    self.assertEqual(self.repo.head(bare, "hero_int_1.0"), self.base)

    def test_version_raised_by_hand_is_released_without_a_bump_commit(self):
        tested = self.repo.commit("minor release", version="1.1.0")
        self.repo.push("hero_int_dev")
        result = self.repo.promote(tested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_1.0"), tested)
        self.assertEqual(self.repo.head(self.repo.github, "v1.1.0^{commit}"), tested)


class TestCandidatePromotion(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.repo = _Remotes(Path(tmp))
        self.repo.push("hero_int_release", remotes=(self.repo.github,))
        self.base = self.repo.head(self.repo.github, "hero_int_release")
        self.tested = self.repo.commit("tested candidate")
        self.repo.push("hero_int_dev")

    def refs(self):
        return git(self.repo.github, "for-each-ref", "--format=%(refname) %(objectname)")

    def candidate(self, **changes):
        env = {**os.environ, **GIT_ENV, "GITHUB_REMOTE": str(self.repo.github), "GITHUB_TOKEN": "",
               "CI_PIPELINE_EVENT": "manual", "CI_PIPELINE_STATUS": "success",
               "CI_COMMIT_BRANCH": "hero_int_dev", "CI_COMMIT_SHA": self.tested,
               "CI_COMMIT_MESSAGE": "MANUAL: -canidate Validate release @ hero_int_dev", **changes}
        return subprocess.run(["bash", str(CANDIDATE)], env=env, text=True, capture_output=True)

    def test_manual_candidate_publishes_only_the_exact_tested_commit(self):
        git(self.repo.work, "tag", "existing-tag")
        git(self.repo.work, "push", "-q", str(self.repo.github), "refs/tags/existing-tag")
        before = self.refs().replace(f"refs/heads/hero_int_release {self.base}",
                                     f"refs/heads/hero_int_release {self.tested}")
        result = self.candidate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), self.tested)
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.repo.head(self.repo.forgejo, "hero_int_1.0"), self.base)
        self.assertEqual(git(self.repo.github, "show", "hero_int_release:hero_integration/__init__.py"),
                         '__version__ = "1.0.0"')

    def test_ordinary_manual_runs_and_incomplete_markers_do_not_write(self):
        before = self.refs()
        for message in ("", "MANUAL PIPELINE @ hero_int_dev", "MANUAL: Validate -canidate @ hero_int_dev",
                        "MANUAL: -canidate-extra @ hero_int_dev", "MANUAL: -canidates @ hero_int_dev",
                        "MANUAL:  -canidate @ hero_int_dev"):
            with self.subTest(message=message):
                result = self.candidate(CI_COMMIT_MESSAGE=message, GITHUB_REMOTE="unreachable")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.refs(), before)

    def test_manual_message_with_marker_alone_and_newline_are_supported(self):
        for message in ("-canidate", "MANUAL: -canidate @ hero_int_dev", "-canidate\nDetails"):
            with self.subTest(message=message):
                result = self.candidate(CI_COMMIT_MESSAGE=message)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.repo.head(self.repo.github, "hero_int_release"), self.tested)

    def test_push_and_other_events_cannot_publish_even_with_the_marker(self):
        before = self.refs()
        for event in ("push", "pull_request", "cron", ""):
            with self.subTest(event=event):
                result = self.candidate(CI_PIPELINE_EVENT=event)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.refs(), before)

    def test_failed_or_unknown_preceding_ci_cannot_publish(self):
        before = self.refs()
        for status in ("failure", "pending", "running", ""):
            with self.subTest(status=status):
                self.assertNotEqual(self.candidate(CI_PIPELINE_STATUS=status).returncode, 0)
                self.assertEqual(self.refs(), before)

    def test_unsupported_source_and_invalid_sha_cannot_publish(self):
        before = self.refs()
        for changes in ({"CI_COMMIT_BRANCH": "hero_int_release"}, {"CI_COMMIT_BRANCH": "hero_int_1.0"},
                        {"CI_COMMIT_BRANCH": "refs/heads/hero_int_dev"}, {"CI_COMMIT_SHA": ""},
                        {"CI_COMMIT_SHA": "HEAD"}, {"CI_COMMIT_SHA": "a" * 39}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.candidate(**changes).returncode, 0)
                self.assertEqual(self.refs(), before)

    def test_opt_source_can_publish_without_creating_an_opt_release_or_tag(self):
        self.repo.push("hero_int_dev_opt", remotes=(self.repo.github,))
        before = self.refs().replace(f"refs/heads/hero_int_release {self.base}",
                                     f"refs/heads/hero_int_release {self.tested}")
        result = self.candidate(CI_COMMIT_BRANCH="hero_int_dev_opt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.refs(), before)

    def test_source_moved_since_ci_started_is_rejected(self):
        self.repo.commit("newer untested source")
        self.repo.push("hero_int_dev")
        before = self.refs()
        result = self.candidate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Source branch moved", result.stderr)
        self.assertEqual(self.refs(), before)

    def test_diverged_release_is_rejected_without_force(self):
        git(self.repo.work, "checkout", "-q", "--detach", self.base)
        self.repo.commit("independent release hotfix", name="hotfix.txt")
        self.repo.push("hero_int_release", remotes=(self.repo.github,))
        before = self.refs()
        result = self.candidate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("diverged", result.stderr)
        self.assertEqual(self.refs(), before)

    def test_missing_release_is_not_silently_created(self):
        git(self.repo.github, "update-ref", "-d", "refs/heads/hero_int_release")
        before = self.refs()
        self.assertNotEqual(self.candidate().returncode, 0)
        self.assertEqual(self.refs(), before)

    def test_repeat_is_idempotent(self):
        self.assertEqual(self.candidate().returncode, 0)
        before = self.refs()
        result = self.candidate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already contains", result.stdout)
        self.assertEqual(self.refs(), before)

    def test_remote_rejection_fails_without_modifying_any_ref(self):
        hook = self.repo.github / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
        before = self.refs()
        result = self.candidate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.refs(), before)

    def test_credential_is_not_embedded_in_remote_errors(self):
        token = "synthetic-candidate-credential"
        result = self.candidate(GITHUB_REMOTE=str(self.repo.root / "missing.git"), GITHUB_TOKEN=token)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_workflow_requires_manual_marker_and_installation_success(self):
        import yaml

        steps = {s["name"]: s for s in yaml.safe_load((ROOT / ".woodpecker/opt.yml").read_text())["steps"]}
        step = steps["promote-candidate"]
        self.assertEqual(step["depends_on"], ["install-test"])
        self.assertEqual(step["when"], [{"event": "manual", "branch": "hero_int_dev_opt",
                         "evaluate": 'CI_COMMIT_MESSAGE matches "^(MANUAL: )?-canidate([[:space:]]|$)"'}])
        self.assertEqual(step["environment"]["GITHUB_TOKEN"], {"from_secret": "github_candidate_token"})
        self.assertNotIn("hero_site_update_key", str(step))
        self.assertNotIn("status", step["when"][0])


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
                   "COMMIT_SHA": self.SHA, "POLL_SECONDS": "0", "WAIT_SECONDS": str(wait_seconds)}
            return subprocess.run(["sh", str(WAIT)], env=env, text=True, capture_output=True)

    @staticmethod
    def check_run(name, status="completed", conclusion="success", started="2026-11-03T10:00:00Z"):
        return {"name": name, "status": status, "conclusion": conclusion, "started_at": started,
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


class TestWoodpeckerChain(unittest.TestCase):
    def test_opt_promotion_runs_last_and_only_for_opt_pushes(self):
        import yaml

        steps = {s["name"]: s for s in yaml.safe_load((ROOT / ".woodpecker/opt.yml").read_text())["steps"]}
        self.assertEqual(steps["install-test"]["depends_on"], ["wait-github"])
        self.assertEqual(steps["promote-opt"]["depends_on"], ["install-test"])
        self.assertEqual(steps["promote-opt"]["when"], [{"event": "push", "branch": "hero_int_dev_opt"}])
        self.assertEqual(steps["promote-opt"]["environment"]["GITHUB_TOKEN"], {"from_secret": "github_release_token"})
        self.assertNotIn("promote", steps)
        self.assertNotIn("update-test-site", steps)


if __name__ == "__main__":
    unittest.main()
