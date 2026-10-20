# cnberg maintenance fork

This fork adds guarded, on-demand OAuth refresh while preserving the upstream
CLI and registry format. It does not add a background service or replace the
official Codex CLI. The npm package `@loongphy/codex-auth` remains the upstream
distribution; identify a build of this fork by its Git commit as well as its
upstream version.

## Baseline and patch ownership

- Upstream: <https://github.com/Loongphy/codex-auth> (`upstream` remote).
- Maintained fork: <https://github.com/cnberg/codex-auth> (`origin` remote).
- Starting upstream revision: `05d50bc4b613d36e2799e381a2640050766b679e`.
- OAuth refresh foundation: upstream [PR #156](https://github.com/Loongphy/codex-auth/pull/156),
  commits `6e339df` and `0808c30`, retained with their original author attribution.
- Fork fixes live in separate commits so upstream adoption can remove local
  changes individually. Public fork history is advanced with merges, not rewritten.

The fork maintains these behaviors:

- JSON mutation uses the allocator that owns the parsed JSON objects. Successful
  refresh responses require a nonempty access token; omitted optional tokens
  and unknown auth fields are preserved.
- Usage refresh sends at most one token request per account per invocation.
  Concurrent callers replay usage after either caller refreshes the snapshot.
- A token request first records a private per-account refresh state. A timeout,
  interrupted process, or other ambiguous result leaves that state in place.
  Later invocations report `RefreshUncertain` instead of automatically spending
  the same refresh token again. A user-supplied replacement credential clears
  an obsolete state when its refresh-token fingerprint differs.
- A successful response is journaled before publishing credentials. Partial
  storage failures report `RefreshPersistence` and block account activation.
  The next invocation attempts to recover a journaled response before syncing
  active auth back into account snapshots. The snapshot and active file each
  use atomic replacement; the journal handles interruption between the writes.
- Usage transport errors remain errors in JSON, rather than becoming `no_data`.
  Session timestamps and auth freshness share one fractional-second UTC parser.

Refresh-state files are named `accounts/<account-file-key>.refresh-state.json`.
They use the same private permissions as credentials and may contain a successful
token response. Treat them as credentials. Keep the state until recovery or
user reauthentication; removing an ambiguous state permits token reuse.

The per-account lock coordinates this tool's refresh callers. Unmodified native
Codex processes do not acquire that lock. A native credential observed to have
advanced during refresh is preserved, but this is not a guarantee against an
independent native refresh already in flight. Continue to restart clients after
switching, as upstream requires. Keep this limitation in scope when changing
credential ownership or adding background polling.

## Validation

Use Zig 0.16.0. Run filesystem and CLI checks in a temporary directory with an
isolated `CODEX_HOME`; use only synthetic credentials. For example, with
`checkout` set to the absolute repository path:

```sh
work=$(mktemp -d)
mkdir -p "$work/codex"
export CODEX_HOME="$work/codex"
cd "$work"
zig build --build-file "$checkout/build.zig" test -j2
zig build --build-file "$checkout/build.zig" -p "$work/install"
python3 "$checkout/tests/oauth_refresh_cli.py" "$work/install/bin/codex-auth"
zig build --build-file "$checkout/build.zig" run -- list
```

The Python CLI suite uses a local curl replacement and tests JSON capacity growth,
token rotation, ambiguous failures across invocations, storage recovery, replay
failures, and concurrent 401 recovery. Its POSIX launcher and permission tests
run on Linux/macOS. Zig unit and integration tests also run on Windows.

Run `zig fmt --check` on changed Zig files, `git diff --check`, and the existing
npm metadata checks. CI runs the Zig suite on Linux, macOS, and Windows, plus
the CLI regression suite on Linux/macOS.

Real-account login, reauthentication, account switching, credential import, and
token refresh are performed by the user. Installing an automatic-refresh build
into an existing polling integration can start real refresh requests, so source
maintenance and isolated validation do not by themselves authorize deployment
into that integration.

## Upstream upgrades

1. Start with a clean checkout and record the current fork commit and its CI run.
   Fetch `upstream/main` and tags:

   ```sh
   git fetch upstream main --tags
   git log --oneline HEAD..upstream/main
   git diff HEAD...upstream/main -- src/auth src/registry src/workflows tests
   ```

2. Create an upgrade branch and merge the new upstream revision. If a fix has
   landed upstream, remove the corresponding duplicate implementation and keep
   the behavioral regression test. Resolve conflicts before proceeding:

   ```sh
   git switch -c maintenance/upstream-upgrade
   git merge --no-ff upstream/main
   ```

3. Verify the JSON schema, registry compatibility, refresh-state recovery, and
   token-rotation safeguards. Run the validation commands above and require CI
   on all supported operating systems before merging into fork `main`.
4. Record the adopted upstream revision and any remaining fork-specific fixes
   here. Keep publishing guards when merging upstream workflow changes.
5. Build a new binary into a versioned installation directory. Switch the user
   entry point only after validation and user-controlled authentication checks.
   Rollback changes the executable only: restoring an older credential snapshot
   can restore a refresh token that has already been spent.

## Releases

Follow [release.md](release.md) for version selection and CI gates. No stable
version is inferred on the user's behalf. Upstream package names and metadata
remain unchanged, and npm/preview publication jobs are gated to the upstream
repository. This fork can build and test without publishing upstream npm packages.
