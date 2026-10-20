# Language

- All user-facing CLI output, prompts, help text, warnings, and error messages must be written in English only.

# Fork Maintenance

- This fork is maintained at `cnberg/codex-auth`; `Loongphy/codex-auth` is upstream.
- Before syncing upstream, changing credential persistence, installing this fork, or releasing, read [docs/fork-maintenance.md](docs/fork-maintenance.md).
- Validate authentication with synthetic credentials and an isolated `CODEX_HOME`. Real-account login, reauthentication, switching, credential import, and token refresh are performed by the user.

# Validation

After modifying any `.zig` file, always run `zig build run -- list` to verify the changes work correctly.

# Release Process

- When updating and pushing a release version, always follow [docs/release.md](./docs/release.md).

# Docs

- Do not add low-level technical implementation details to `README.md`. Put implementation-specific behavior in `docs/*.md` or `AGENTS.md` instead.

# Zig API Discovery

- Do not guess Zig APIs from memory or from examples targeting other Zig versions.
- Before using or changing a Zig API, run `zig env` and `zig version` to confirm the local toolchain and source layout.
- Use the paths reported by `zig env` as the source of truth, especially `std_dir` for the standard library and `lib_dir` for other bundled Zig libraries.
- Prefer evidence from local sources: symbol definitions, nearby tests, and existing call sites in this repository.
- If the needed behavior is not clear from `std_dir`, inspect other Zig sources and tests under the local `lib_dir` tree as needed.
