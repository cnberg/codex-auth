# Local installation and scheduled upstream maintenance

The macOS installation uses a native binary built from this fork. The global
entry point `/opt/homebrew/bin/codex-auth` links to
`~/Library/Application Support/codex-auth/current/bin/codex-auth`. The original
npm package remains installed for executable rollback. Do not use npm to update
this entry point: the maintained source is `cnberg/codex-auth`, with
`Loongphy/codex-auth` as the upstream remote.

## Installation state

The following files are outside both the Git checkout and `~/.codex`:

- `releases/<full-git-sha>/bin/codex-auth` and `manifest.json`: immutable build,
  upstream version, source commit, SHA-256, and install time.
- `current`: an atomically replaced symlink to the selected release.
- `installation.json`: current/previous source revisions and original npm link.
- `installation-origin.json`: original executable link saved before publication.
- `toolchains/zig-0.16.0/`: persistent build toolchain.
- `maintenance.json`: local paths for checkout, state, entry point, Zig, and gh.
- `maintenance-status.json` and `validation.log`: last maintenance result and
  isolated validation output. launchd stdout/stderr use `maintenance.log`.

Replacing the executable does not require logging in, importing credentials,
switching accounts, or restarting native Codex. Existing consumers, including
Hammerspoon's fixed executable path, use the new binary on their next invocation.
Existing application polling continues according to its configured policy;
installation validation never invokes the CLI against real account state.

## Daily maintenance

The user LaunchAgent `cnberg.codex-auth-maintenance` runs daily at **09:00 local
machine time** (currently Asia/Singapore, UTC+8). macOS runs a missed calendar
event after waking; the user must be logged in. It executes:

```sh
/usr/bin/python3 ~/Developer/codex-auth/scripts/maintenance.py run
```

The job uses the existing GitHub CLI authentication; it never starts a login or
refreshes an LLM credential. It does not call Codex or an LLM to maintain source.

1. Take a local nonblocking process lock. Require a clean `main` checkout and
   the expected fork/upstream remotes. Fetch both repositories and fast-forward
   local `main`; uncommitted work and diverged history are preserved.
2. If upstream has new commits, merge them in a disposable detached worktree.
   Keep fork commits and author attribution. Record the adopted upstream commit
   in [upstream-state.json](upstream-state.json) and verify npm/preview guards.
3. Run the maintenance tests, Zig suite, synthetic OAuth CLI tests, empty-home
   CLI smoke test, and npm metadata checks. Every CLI validation uses a temporary
   `CODEX_HOME`. No real authentication state is read or backed up.
4. Push a dedicated maintenance branch and open a PR. Require successful Linux,
   macOS, and Windows build/test checks on the exact PR head before merging.
   Fork `main` enforces these up-to-date checks even for administrators. The
   script refuses automated merges if this protection is absent. Pending CI is
   allowed 30 minutes; later runs reuse open PRs and orphaned pushed branches.
5. Fast-forward the local checkout and atomically select the tested native
   executable. If `main` changed during CI, defer installation until its exact
   revision passes three-platform CI and isolated local validation.

An unchanged upstream requires no rebuild. A new fork `main` revision is installed
only after exact-revision main CI succeeds. Conflicts, missing publishing guards,
failed tests, unexpected entry points, or dirty worktrees leave the installed
binary unchanged and record `blocked`; they require maintainer intervention.
An interrupted installation can verify and reuse its staged release. Installation
metadata is committed before the final executable-pointer change. A failed
metadata write keeps the old executable active; the active release manifest is
checked independently of that record when determining installation status.
The scheduler records status locally and does not send messages or notifications.

## Local commands

```sh
python3 ~/Developer/codex-auth/scripts/maintenance.py status
python3 ~/Developer/codex-auth/scripts/maintenance.py check  # fetch and report only
python3 ~/Developer/codex-auth/scripts/maintenance.py run    # synchronize and install
launchctl print gui/$(id -u)/cnberg.codex-auth-maintenance
```

Configuration is a private JSON file in the installation state directory:

```json
{
  "checkout": "/Users/berg/Developer/codex-auth",
  "state_dir": "/Users/berg/Library/Application Support/codex-auth",
  "entry_point": "/opt/homebrew/bin/codex-auth",
  "zig": "/Users/berg/Library/Application Support/codex-auth/toolchains/zig-0.16.0/zig",
  "gh": "/opt/homebrew/bin/gh"
}
```

The LaunchAgent is manually installed at
`~/Library/LaunchAgents/cnberg.codex-auth-maintenance.plist`; dotfiles `rcup` does
not manage it. It sets PATH to include Homebrew, runs without `RunAtLoad` or
`KeepAlive`, and sends stdout/stderr to the state directory. To pause it:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/cnberg.codex-auth-maintenance.plist
```

## Executable rollback

Pause the LaunchAgent first. Use `installation.json` to select a previously
validated release and atomically replace `current`, or restore the npm symlink
stored in `original_entry_target`. Keep the release manifests for provenance.
Rollback changes program files only. Never restore account snapshots or remove
refresh-state files as part of executable rollback: a saved refresh token may
already have been consumed.
