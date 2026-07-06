# Releasing catalpa-workspace-tooling

Version numbers come from **git tags** (`v0.9.1` → package version `0.9.1`). Do not edit a static `version` field in `pyproject.toml`; [hatch-vcs](https://github.com/ofek/hatch-vcs) derives it at build time.

Consumer repos pin tooling with a git tag in `[tool.uv.sources]`:

```toml
catalpa-workspace-tooling = { git = "https://github.com/catalpainternational/catalpa-workspace-tooling", tag = "v0.9.1" }
```

## Release checklist

1. **Finalize `CHANGELOG.md`** — move items from `## Unreleased` into a new `## X.Y.Z` section.
2. **Merge to `main`** and push.
3. **Tag from `main`** (on the machine that pushed):

   ```bash
   scripts/release.sh 0.9.1
   ```

   The script checks a clean tree, that `main` is pushed, and that the changelog section exists. It creates an annotated tag and pushes it.

4. **CI** (`.github/workflows/release.yml`) runs tests, builds the wheel, verifies the version matches the tag, and creates the [GitHub release](https://github.com/catalpainternational/catalpa-workspace-tooling/releases) with notes from `CHANGELOG.md`.

5. **Bump consumers** when ready (one command per repo):

   ```bash
   uv add --group tooling \
     "catalpa-workspace-tooling @ git+https://github.com/catalpainternational/catalpa-workspace-tooling@v0.9.1"
   uv sync
   ```

   Commit the updated `pyproject.toml` and `uv.lock`. Run `uv run test smoke` on local dev stacks after breaking changes.

## Preview branches and pre-releases

For parallel consumer tracks (e.g. local-proxy work), tag from the branch line consumers should follow:

```bash
git tag v0.9-lp
git push origin v0.9-lp
```

Pre-release semver tags (`v0.10.0-rc.1`) work with hatch-vcs and uv.

## Local development without a release tag

Editable install from a sibling checkout (see consumer `docs/DEVELOPMENT.md`):

```bash
uv add --group tooling --editable ../../catalpa-workspace-tooling
```

Off-tag commits get a dev version suffix from hatch-vcs (e.g. `0.9.0.dev5+gabc1234`).

## Manual fallback

If you cannot run `scripts/release.sh`:

```bash
git tag -a v0.9.1 -m "Release v0.9.1"
git push origin v0.9.1
```

CI still creates the GitHub release if `CHANGELOG.md` contains `## 0.9.1`.
