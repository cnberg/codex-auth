#!/usr/bin/env python3
"""Maintain this fork and its local installation without accessing Codex credentials.

The manually invoked script uses existing GitHub authentication. All executable validation
uses a temporary CODEX_HOME. Conflicts, dirty checkouts, failed CI, and unexpected
entry points stop the update and leave the installed executable in place.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

REPOSITORY = "cnberg/codex-auth"
UPSTREAM = "Loongphy/codex-auth"
CHECKS = {"Build & Test (" + os_name + ")" for os_name in
          ("ubuntu-latest", "macos-latest", "windows-latest")}
BAD_CHECKS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
DEFAULT_STATE = Path.home() / "Library/Application Support/codex-auth"


class MaintenanceError(RuntimeError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_link(path, target):
    temporary = path.with_name("." + path.name + "." + str(os.getpid()))
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def check_rollup(rollup):
    """Only all three successful native checks authorize a merge/install."""
    checks = {item["name"]: item for item in rollup if item.get("name") in CHECKS}
    if any(item.get("conclusion", "").upper() in BAD_CHECKS for item in checks.values()):
        raise MaintenanceError("Native CI failed; current installation is unchanged")
    return set(checks) == CHECKS and all(
        item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS"
        for item in checks.values())


def verify_publishing_guards(checkout):
    for filename, job in (("release.yml", "publish-npm"), ("preview-release.yml", "release-assets")):
        source = (checkout / ".github/workflows" / filename).read_text()
        marker = "  " + job + ":\n"
        if marker not in source:
            raise MaintenanceError("Publishing workflow changed; review required: " + filename)
        section = source.split(marker, 1)[1]
        # End at the next job, rather than accepting a guard elsewhere in the file.
        section = re.split(r"\n  [A-Za-z0-9_-]+:\n", section, maxsplit=1)[0]
        if not re.search(r"^    if: \$\{\{ github.repository == 'Loongphy/codex-auth' \}\}$", section, re.MULTILINE):
            raise MaintenanceError("Upstream publishing guard missing: " + filename)


class Maintainer:
    def __init__(self, config):
        self.repo = Path(config["checkout"]).expanduser().resolve()
        self.state = Path(config["state_dir"]).expanduser().resolve()
        self.zig = Path(config["zig"]).expanduser().resolve()
        self.entry = Path(config["entry_point"]).expanduser()
        self.gh = config.get("gh", "/opt/homebrew/bin/gh")
        self.state.mkdir(parents=True, exist_ok=True)

    def command(self, args, cwd=None, env=None, timeout=120):
        try:
            result = subprocess.run([str(arg) for arg in args], cwd=cwd or self.repo,
                                    env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MaintenanceError("Command timed out: " + str(args[0])) from None
        if result.returncode:
            raise MaintenanceError("Command failed: " + " ".join(str(arg) for arg in args[:5]) +
                                   "\n" + (result.stderr or result.stdout)[-2500:])
        return result.stdout.strip()

    def git(self, *args, cwd=None):
        # A per-command helper avoids a hanging macOS credential-osxkeychain
        # without changing the user's global Git configuration.
        return self.command(["git", "-c", "credential.helper=", "-c",
                             "credential.helper=!" + shlex.quote(self.gh) + " auth git-credential",
                             *args], cwd=cwd)

    def github(self, *args):
        return self.command([self.gh, *args, "--repo", REPOSITORY])

    def status(self, result, **details):
        value = dict(checked_at=timestamp(), result=result, **details)
        atomic_json(self.state / "maintenance-status.json", value)
        print(json.dumps(value), flush=True)

    def ensure_checkout(self):
        if self.git("status", "--porcelain"):
            raise MaintenanceError("Local checkout has uncommitted changes; no stash or reset was attempted")
        if self.git("branch", "--show-current") != "main":
            raise MaintenanceError("Local checkout is not on main")
        for remote, expected in (("origin", REPOSITORY), ("upstream", UPSTREAM)):
            url = self.git("remote", "get-url", remote).removesuffix(".git")
            if url not in ("https://github.com/" + expected, "git@github.com:" + expected):
                raise MaintenanceError("Unexpected " + remote + " repository")

    def fetch(self):
        self.git("fetch", "--no-tags", "origin", "main")
        self.git("fetch", "--no-tags", "upstream", "main")

    def contains(self, ancestor, descendant, cwd=None):
        result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
                                cwd=cwd or self.repo, capture_output=True)
        if result.returncode not in (0, 1):
            raise MaintenanceError("Could not compare Git revisions")
        return result.returncode == 0

    @contextmanager
    def worktree(self, revision):
        with tempfile.TemporaryDirectory(prefix="codex-auth-maintenance-") as temporary:
            checkout = Path(temporary) / "checkout"
            self.git("worktree", "add", "--detach", str(checkout), revision)
            try:
                yield checkout
            finally:
                self.git("worktree", "remove", "--force", str(checkout))

    def validate(self, checkout):
        verify_publishing_guards(checkout)
        if self.command([self.zig, "version"]) != "0.16.0":
            raise MaintenanceError("This maintenance pipeline requires Zig 0.16.0")
        build_dir = Path(tempfile.mkdtemp(prefix="validation-", dir=self.state))
        try:
            codex_home = build_dir / "codex"
            codex_home.mkdir()
            env = dict(os.environ, CODEX_HOME=str(codex_home),
                       PATH=str(self.zig.parent) + os.pathsep + os.environ.get("PATH", ""),
                       ZIG_GLOBAL_CACHE_DIR=str(self.state / "zig-cache"),
                       ZIG_LOCAL_CACHE_DIR=str(build_dir / "cache"),
                       CODEX_AUTH_CLI_INTEGRATION_INSTALL_PREFIX=str(build_dir / "helpers"))
            for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
                env.pop(key, None)
            build = [self.zig, "build", "--build-file", checkout / "build.zig"]
            commands = [
                [sys.executable, "-m", "unittest", "discover", "-s", str(checkout / "tests"), "-p", "maintenance_test.py"],
                [*build, "test", "-j2", "--summary", "all"],
                [*build, "-Doptimize=ReleaseSafe", "-p", build_dir / "install"],
                [sys.executable, checkout / "tests/oauth_refresh_cli.py", build_dir / "install/bin/codex-auth"],
                [*build, "run", "--", "list"],
                ["node", checkout / "scripts/npm/check-versions.mjs", ""],
                ["node", checkout / "scripts/npm/check-release-metadata.mjs"],
            ]
            with (self.state / "validation.log").open("w") as output:
                for args in commands:
                    result = subprocess.run([str(arg) for arg in args], cwd=build_dir, env=env,
                                            stdout=output, stderr=subprocess.STDOUT, timeout=1200)
                    if result.returncode:
                        raise MaintenanceError("Isolated validation failed; see validation.log")
            return build_dir
        except BaseException:
            shutil.rmtree(build_dir)
            raise

    def wait_pr(self, number, head, timeout=1800):
        deadline = time.monotonic() + timeout
        while True:
            pr = json.loads(self.github("pr", "view", str(number), "--json", "headRefOid,statusCheckRollup,state"))
            if pr["state"] != "OPEN" or pr["headRefOid"] != head:
                raise MaintenanceError("Maintenance PR changed while waiting for CI")
            if check_rollup(pr["statusCheckRollup"]):
                return
            if time.monotonic() >= deadline:
                raise MaintenanceError("CI is still pending; maintenance PR retained for the next run")
            time.sleep(30)

    def require_main_ci(self, revision):
        runs = json.loads(self.github("run", "list", "--workflow", "CI", "--branch", "main",
                                     "--commit", revision, "--event", "push", "--limit", "1", "--json", "databaseId,status,conclusion"))
        if not runs or runs[0]["status"] != "completed" or runs[0]["conclusion"] != "success":
            raise MaintenanceError("Main CI is not successful for this exact revision; installation deferred")
        details = json.loads(self.github("run", "view", str(runs[0]["databaseId"]), "--json", "jobs"))
        checks = [{"name": job["name"], "status": job["status"].upper(),
                   "conclusion": job["conclusion"].upper()} for job in details["jobs"]]
        if not check_rollup(checks):
            raise MaintenanceError("Main CI did not run all three native platforms")

    def require_strict_protection(self):
        policy = json.loads(self.command([self.gh, "api", "repos/" + REPOSITORY + "/branches/main/protection"]))
        checks = policy.get("required_status_checks") or {}
        contexts = set(checks.get("contexts", [])) | {item["context"] for item in checks.get("checks", [])}
        if not (checks.get("strict") and CHECKS <= contexts and policy.get("enforce_admins", {}).get("enabled")):
            raise MaintenanceError("Main must enforce up-to-date three-platform checks, including for administrators")

    def install(self, revision, build_dir):
        self.ensure_checkout()
        if self.git("rev-parse", "HEAD") != revision:
            raise MaintenanceError("Checkout changed before installation")
        expected = self.state / "current/bin/codex-auth"
        if not self.entry.is_symlink():
            raise MaintenanceError("Entry point is not a managed symlink; refusing to overwrite it")
        original = os.readlink(self.entry)
        old_target = (self.entry.parent / original).absolute()
        npm_target = Path("/opt/homebrew/lib/node_modules/@loongphy/codex-auth/bin/codex-auth.js")
        if os.path.normpath(old_target) not in (str(expected), str(npm_target)):
            raise MaintenanceError("Entry point belongs to another installation")
        origin = self.state / "installation-origin.json"
        if not origin.exists():
            atomic_json(origin, dict(original_entry_target=original))
        original_target = json.loads(origin.read_text())["original_entry_target"]
        source = build_dir / "install/bin/codex-auth"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        release = self.state / "releases" / revision
        if release.exists():
            manifest = json.loads((release / "manifest.json").read_text())
            digest = hashlib.sha256((release / "bin/codex-auth").read_bytes()).hexdigest()
            if manifest.get("revision") != revision or manifest.get("sha256") != digest:
                raise MaintenanceError("An existing release failed provenance verification")
        else:
            stage = Path(tempfile.mkdtemp(prefix=".release-", dir=self.state))
            try:
                (stage / "bin").mkdir()
                shutil.copy2(source, stage / "bin/codex-auth")
                version = self.command([stage / "bin/codex-auth", "--version"],
                                       env=dict(os.environ, CODEX_HOME=str(build_dir / "codex")))
                atomic_json(stage / "manifest.json", dict(revision=revision, sha256=digest, version=version,
                                                          repository=REPOSITORY, installed_at=timestamp()))
                release.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage, release)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        record = self.state / "installation.json"
        previous = json.loads(record.read_text()) if record.exists() else {}
        prior_revision = previous.get("previous_revision") if previous.get("revision") == revision else previous.get("revision")
        # Commit metadata before the last pointer operation that changes which
        # executable runs. A failed write cannot switch the active program.
        atomic_json(record, dict(revision=revision, previous_revision=prior_revision,
                                 original_entry_target=original_target,
                                 sha256=digest, installed_at=timestamp()))
        atomic_link(self.state / "current", Path("releases") / revision)
        if os.path.normpath(old_target) != str(expected):
            # On the first install, current is unused until this final replace.
            atomic_link(self.entry, expected)

    def installed(self, revision):
        record = self.state / "installation.json"
        binary = self.state / "current/bin/codex-auth"
        if not record.exists() or not binary.exists() or not self.entry.is_symlink():
            return False
        data = json.loads(record.read_text())
        manifest = json.loads((self.state / "current/manifest.json").read_text())
        return (data.get("revision") == revision and manifest.get("revision") == revision and
                self.entry.resolve() == binary.resolve() and
                hashlib.sha256(binary.read_bytes()).hexdigest() == data.get("sha256"))

    def merge_upstream(self, base, upstream):
        self.require_strict_protection()
        branch = "maintenance/upstream-" + upstream[:12]
        existing = json.loads(self.github("pr", "list", "--head", branch, "--base", "main", "--state", "open", "--json", "number,headRefOid"))
        remote_branch = self.git("ls-remote", "--heads", "origin", "refs/heads/" + branch)
        with self.worktree(base) as checkout:
            if remote_branch:
                self.git("fetch", "origin", branch, cwd=checkout)
                self.git("checkout", "--detach", "FETCH_HEAD", cwd=checkout)
                if not self.contains(upstream, "HEAD", cwd=checkout):
                    raise MaintenanceError("Existing maintenance branch has unexpected ancestry")
                if not self.contains(base, "HEAD", cwd=checkout):
                    self.git("merge", "--no-ff", "--no-edit", base, cwd=checkout)
            else:
                self.git("merge", "--no-ff", "--no-edit", upstream, cwd=checkout)
                verify_publishing_guards(checkout)
                atomic_json(checkout / "docs/upstream-state.json", dict(upstream=UPSTREAM, adopted_revision=upstream))
                self.git("add", "docs/upstream-state.json", cwd=checkout)
                self.git("commit", "-m", "docs(fork): record adopted upstream revision", cwd=checkout)
                # Validate locally before publishing an update for remote CI.
            build_dir = self.validate(checkout)
            try:
                head = self.git("rev-parse", "HEAD", cwd=checkout)
                self.git("push", "origin", "HEAD:refs/heads/" + branch, cwd=checkout)
                if not existing:
                    body = build_dir / "pr-body.md"
                    body.write_text("Merge upstream `" + upstream + "` while preserving fork refresh safeguards.\n\n"
                                    "Isolated local tests passed. Automated merge requires successful Linux, macOS, and Windows CI.\n"
                                    "No real Codex credentials were accessed.\n")
                    self.github("pr", "create", "--base", "main", "--head", branch,
                                "--title", "Sync upstream " + upstream[:12], "--body-file", str(body))
                    number = json.loads(self.github("pr", "view", branch, "--json", "number"))["number"]
                else:
                    number = existing[0]["number"]
                self.status("waiting_for_ci", pull_request=number, upstream_revision=upstream)
                self.wait_pr(number, head)
                self.git("fetch", "--no-tags", "origin", "main")
                if not self.contains("origin/main", head):
                    raise MaintenanceError("Main advanced during CI; retained PR will be updated on the next run")
                self.github("pr", "merge", str(number), "--merge", "--delete-branch", "--match-head-commit", head)
                self.git("fetch", "--no-tags", "origin", "main")
                revision = self.git("rev-parse", "origin/main")
                # Base-branch changes during CI require a fresh validation run.
                self.git("diff", "--exit-code", head, revision)
                self.ensure_checkout()
                self.git("merge", "--ff-only", "origin/main")
                self.install(revision, build_dir)
                return revision
            finally:
                shutil.rmtree(build_dir)

    def run(self, check_only=False):
        self.ensure_checkout()
        self.fetch()
        base = self.git("rev-parse", "origin/main")
        upstream = self.git("rev-parse", "upstream/main")
        if not self.contains("HEAD", base):
            raise MaintenanceError("Local main has diverged; no reset was attempted")
        if check_only:
            self.status("update_available" if not self.contains(upstream, base) else "up_to_date",
                        local_revision=self.git("rev-parse", "HEAD"), fork_revision=base, upstream_revision=upstream)
            return
        self.git("merge", "--ff-only", "origin/main")
        if not self.contains(upstream, base):
            revision = self.merge_upstream(base, upstream)
            self.status("updated", revision=revision, upstream_revision=upstream)
            return
        if not self.installed(base):
            self.require_main_ci(base)
            with self.worktree(base) as checkout:
                build_dir = self.validate(checkout)
                try:
                    self.install(base, build_dir)
                finally:
                    shutil.rmtree(build_dir)
        self.status("up_to_date", revision=base, upstream_revision=upstream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "check", "status"))
    parser.add_argument("--config", type=Path, default=DEFAULT_STATE / "maintenance.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    maintainer = Maintainer(config)
    if args.action == "status":
        path = maintainer.state / "maintenance-status.json"
        print(path.read_text() if path.exists() else '{"result":"not_run"}')
        return 0
    import fcntl
    with (maintainer.state / "maintenance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another maintenance run is active")
            return 0
        try:
            maintainer.run(check_only=args.action == "check")
        except (MaintenanceError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            maintainer.status("blocked", reason=str(error))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
