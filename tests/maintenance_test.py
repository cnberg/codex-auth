"""Local Git/filesystem tests; no GitHub or Codex authentication is used."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("maintenance", Path(__file__).resolve().parents[1] / "scripts/maintenance.py")
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-auth-maintenance-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic Maintainer")
        self.git("config", "user.email", "maintenance@example.test")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "program").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        for remote, repo in (("origin", maintenance.REPOSITORY), ("upstream", maintenance.UPSTREAM)):
            self.git("remote", "add", remote, "https://github.com/" + repo + ".git")
        self.state = self.root / "state"
        self.entry = self.root / "bin/codex-auth"
        self.entry.parent.mkdir()
        self.entry.symlink_to("/opt/homebrew/lib/node_modules/@loongphy/codex-auth/bin/codex-auth.js")
        self.job = maintenance.Maintainer(dict(checkout=str(self.repo), state_dir=str(self.state),
                                               zig=str(self.root / "zig"), entry_point=str(self.entry)))

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.repo, stderr=subprocess.DEVNULL,
                                       text=True).strip()

    def test_local_changes_and_divergence_are_preserved(self):
        self.job.ensure_checkout()
        (self.repo / "program").write_text("user edit\n")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "uncommitted"):
            self.job.ensure_checkout()
        self.assertEqual((self.repo / "program").read_text(), "user edit\n")
        self.git("checkout", "-b", "user-branch")
        with self.assertRaises(maintenance.MaintenanceError):
            self.job.ensure_checkout()

    def test_ci_requires_every_platform_and_rejects_failure(self):
        checks = [dict(name=name, status="COMPLETED", conclusion="SUCCESS") for name in maintenance.CHECKS]
        self.assertFalse(maintenance.check_rollup([]))
        self.assertFalse(maintenance.check_rollup(checks[:-1]))
        self.assertTrue(maintenance.check_rollup(checks))
        checks[0]["conclusion"] = "FAILURE"
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.check_rollup(checks)

    def test_worktree_merge_keeps_fork_patch_and_does_not_change_main(self):
        self.git("branch", "upstream-test")
        (self.repo / "fork-fix").write_text("keep refresh safeguards\n")
        self.git("add", ".")
        self.git("commit", "-m", "fork fix")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "upstream-test")
        (self.repo / "program").write_text("upstream update\n")
        self.git("commit", "-am", "upstream update")
        upstream = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        with self.job.worktree(base) as checkout:
            self.job.git("merge", "--no-ff", "--no-edit", upstream, cwd=checkout)
            self.assertEqual((checkout / "fork-fix").read_text(), "keep refresh safeguards\n")
            self.assertEqual((checkout / "program").read_text(), "upstream update\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual((self.repo / "program").read_text(), "base\n")

    def test_conflict_leaves_main_and_current_installation_untouched(self):
        self.git("branch", "upstream-test")
        (self.repo / "program").write_text("fork\n")
        self.git("commit", "-am", "fork fix")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "upstream-test")
        (self.repo / "program").write_text("upstream\n")
        self.git("commit", "-am", "upstream change")
        upstream = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        with self.assertRaises(maintenance.MaintenanceError):
            with self.job.worktree(base) as checkout:
                self.job.git("merge", "--no-ff", "--no-edit", upstream, cwd=checkout)
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(os.readlink(self.entry), "/opt/homebrew/lib/node_modules/@loongphy/codex-auth/bin/codex-auth.js")

    def test_guard_on_another_job_does_not_authorize_publishing(self):
        workflows = self.repo / ".github/workflows"
        workflows.mkdir(parents=True)
        (workflows / "release.yml").write_text("jobs:\n  publish-npm:\n    runs-on: ubuntu-latest\n"
                                               "  unrelated:\n    if: ${{ github.repository == 'Loongphy/codex-auth' }}\n")
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.verify_publishing_guards(self.repo)

    @unittest.skipIf(os.name == "nt", "POSIX executable and symlink installation")
    def test_install_switches_only_code_and_preserves_rollback(self):
        revision = self.git("rev-parse", "HEAD")
        build = self.root / "build"
        (build / "install/bin").mkdir(parents=True)
        (build / "codex").mkdir()
        binary = build / "install/bin/codex-auth"
        binary.write_text("#!/bin/sh\nprintf 'codex-auth synthetic\\n'\n")
        binary.chmod(0o755)
        self.job.install(revision, build)
        self.assertTrue(self.job.installed(revision))
        self.assertEqual(self.entry.resolve(), (self.state / "releases" / revision / "bin/codex-auth").resolve())
        manifest = json.loads((self.state / "installation.json").read_text())
        self.assertTrue(manifest["original_entry_target"].endswith("codex-auth.js"))
        binary_path = self.entry.resolve()
        binary_path.write_text("tampered")
        self.assertFalse(self.job.installed(revision))

    def test_failed_main_ci_blocks_installation(self):
        with patch.object(self.job, "github", return_value='[{"status":"completed","conclusion":"failure"}]'):
            with self.assertRaises(maintenance.MaintenanceError):
                self.job.require_main_ci("synthetic-commit")
        self.assertTrue(self.entry.is_symlink())

    def test_unknown_entry_point_is_never_overwritten(self):
        revision = self.git("rev-parse", "HEAD")
        self.entry.unlink()
        self.entry.symlink_to("/another/installation")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "another installation"):
            self.job.install(revision, self.root / "not-built")
        self.assertEqual(os.readlink(self.entry), "/another/installation")
        self.assertFalse((self.state / "current").exists())

    def test_changed_pr_head_blocks_automatic_merge(self):
        with patch.object(self.job, "github", return_value='{"headRefOid":"different","state":"OPEN","statusCheckRollup":[]}'):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "changed"):
                self.job.wait_pr(1, "tested-head", timeout=0)

    def build_stub(self):
        build = Path(tempfile.mkdtemp(dir=self.root))
        (build / "install/bin").mkdir(parents=True)
        (build / "codex").mkdir()
        binary = build / "install/bin/codex-auth"
        binary.write_text("#!/bin/sh\nprintf 'codex-auth synthetic\\n'\n")
        binary.chmod(0o755)
        return build

    @unittest.skipIf(os.name == "nt", "POSIX executable installation")
    def test_failed_install_record_keeps_old_program_and_retry_recovers_release(self):
        first = self.git("rev-parse", "HEAD")
        build = self.build_stub()
        self.job.install(first, build)
        (self.repo / "program").write_text("new version\n")
        self.git("commit", "-am", "next version")
        second = self.git("rev-parse", "HEAD")
        write = maintenance.atomic_json

        def fail_record(path, value):
            if path.name == "installation.json":
                raise PermissionError("synthetic record failure")
            return write(path, value)

        with patch.object(maintenance, "atomic_json", side_effect=fail_record):
            with self.assertRaises(PermissionError):
                self.job.install(second, build)
        self.assertTrue(self.job.installed(first))
        self.assertTrue((self.state / "releases" / second).exists())
        self.job.install(second, build)
        self.assertTrue(self.job.installed(second))
        self.assertEqual(json.loads((self.state / "installation.json").read_text())["previous_revision"], first)

    @unittest.skipIf(os.name == "nt", "POSIX executable installation")
    def test_first_entry_failure_keeps_npm_entry_and_can_be_retried(self):
        revision = self.git("rev-parse", "HEAD")
        build = self.build_stub()
        link = maintenance.atomic_link

        def fail_entry(path, target):
            if path == self.entry:
                raise PermissionError("synthetic entry failure")
            return link(path, target)

        with patch.object(maintenance, "atomic_link", side_effect=fail_entry):
            with self.assertRaises(PermissionError):
                self.job.install(revision, build)
        self.assertTrue(os.readlink(self.entry).endswith("codex-auth.js"))
        self.assertFalse(self.job.installed(revision))
        self.job.install(revision, build)
        self.assertTrue(self.job.installed(revision))

    def test_missing_strict_admin_protection_blocks_automatic_merge(self):
        policy = dict(required_status_checks=dict(strict=True, contexts=list(maintenance.CHECKS)),
                      enforce_admins=dict(enabled=False))
        with patch.object(self.job, "command", return_value=json.dumps(policy)):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "administrators"):
                self.job.require_strict_protection()
        policy["enforce_admins"]["enabled"] = True
        with patch.object(self.job, "command", return_value=json.dumps(policy)):
            self.job.require_strict_protection()

    def test_pushed_branch_without_pr_is_reused_after_creation_failure(self):
        workflows = self.repo / ".github/workflows"
        workflows.mkdir(parents=True)
        for filename, job in (("release.yml", "publish-npm"), ("preview-release.yml", "release-assets")):
            (workflows / filename).write_text("jobs:\n  " + job + ":\n    if: ${{ github.repository == 'Loongphy/codex-auth' }}\n")
        (self.repo / "docs").mkdir()
        self.git("add", ".")
        self.git("commit", "-m", "fork safeguards")
        base = self.git("rev-parse", "HEAD")
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "set-url", "origin", str(remote))
        self.git("push", "origin", "main")
        self.git("checkout", "-b", "upstream-test")
        (self.repo / "upstream-file").write_text("new upstream\n")
        self.git("add", ".")
        self.git("commit", "-m", "upstream update")
        upstream = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")

        def github(*args):
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                raise maintenance.MaintenanceError("synthetic PR creation outage")
            self.fail("unexpected GitHub operation")

        with patch.object(self.job, "github", side_effect=github), \
             patch.object(self.job, "require_strict_protection"), \
             patch.object(self.job, "validate", side_effect=lambda checkout: self.build_stub()):
            for _ in range(2):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "synthetic PR creation outage"):
                    self.job.merge_upstream(base, upstream)
        branch = "maintenance/upstream-" + upstream[:12]
        self.assertTrue(self.git("ls-remote", "--heads", "origin", branch))
        self.assertEqual(self.git("rev-parse", "HEAD"), base)


if __name__ == "__main__":
    unittest.main()
