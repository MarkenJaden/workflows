# MarkenJaden Workflows

Centralized repository for reusable GitHub Actions workflows across MarkenJaden projects.

## Available Workflows

### 1. `upstream-sync.yml` (Automated Upstream Sync with AI Conflict Resolution)

Keeps your fork synchronized with upstream repositories while preserving fork customizations documented in `FORK_DIVERGENCES.md`.

Key capabilities:
- **Deterministic fast-path**: Binary/lock files and upstream files with zero custom fork commits are resolved automatically without consuming API quota.
- **Resilient AI merge (`gemini_conflict_resolver.py`)**: Hunk-by-hunk conflict resolution supporting CRLF/LF, empty hunk sides, diff3 base markers, rate-limit exponential backoff, and syntax checks (Python, JSON, YAML, JS/MJS) before committing.
- **Workflow-permission protection**: If `SYNC_TOKEN` is not configured and upstream modifies `.github/workflows/*` (which GitHub rejects pushing via `GITHUB_TOKEN`), sync automatically switches to 3-way non-workflow patch mode (`Upstream-Sync-Commit` trailer), keeping application code synced without failing pushes.
- **Rolling conflict PRs**: When manual intervention is needed, custom fork changes and partial resolutions are preserved on `upstream-sync/conflict-<target>` (reusing the same open PR rather than creating stale branches on every run).

#### Usage in Caller Repositories

Create `.github/workflows/upstream-sync.yml` in your project repository:

```yaml
name: Upstream Sync & Validate

on:
  schedule:
    # Run daily at 04:00 UTC
    - cron: '0 4 * * *'
  workflow_dispatch:
    inputs:
      upstream_repo:
        description: 'Upstream repository (owner/repo)'
        required: false
        default: 'StarPilotHQ/StarPilot'
      upstream_branch:
        description: 'Upstream branch to sync from'
        required: false
        default: 'Dom'
      target_branch:
        description: 'Target branch to update in this repo'
        required: false
        default: 'Dom'

permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write

jobs:
  sync:
    uses: MarkenJaden/workflows/.github/workflows/upstream-sync.yml@main
    with:
      upstream_repo: ${{ inputs.upstream_repo || 'StarPilotHQ/StarPilot' }}
      upstream_branch: ${{ inputs.upstream_branch || 'Dom' }}
      target_branch: ${{ inputs.target_branch || 'Dom' }}
    secrets: inherit
```

> **Note:** Do **not** add `workflows: write` under `permissions:` — GitHub Actions rejects it as an unknown scope. Configure optional `SYNC_TOKEN` (PAT with `repo` + `workflow` scope) if you want upstream `.github/workflows/*` changes synced directly as well.

#### Inputs

| Input | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `upstream_repo` | **Yes** | - | Upstream repository in format `owner/repo` |
| `upstream_branch` | No | `main` | Branch to fetch from upstream |
| `target_branch` | No | `main` | Target branch in this repository to merge into |
| `python_version` | No | `3.12` | Python version for runner environment |
| `preflight_command` | No | `''` | Shell commands to run for syntax / lint / preflight checks |
| `test_command` | No | `''` | Shell command to execute your test suite |
| `post_merge_workflow` | No | `''` | Workflow filename in caller repo to trigger after push (e.g. `docker-publish.yml`) |
| `submodules` | No | `'false'` | Checkout submodules option (`false`, `true`, `recursive`) |
| `workflow_sync_policy` | No | `'preserve_if_no_pat'` | `preserve_if_no_pat` (sync non-workflow code cleanly when `SYNC_TOKEN` is absent) or `fail_on_workflow_diff` |
| `dry_run` | No | `false` | Run merge, conflict resolution, and checks without pushing |

#### Secrets

| Secret | Description |
| :--- | :--- |
| `GEMINI_API_KEY` | (Optional) API key for automated AI merge conflict resolution of custom-edited files. Deterministic conflicts (unmodified upstream files, binary/locks, deletions) still auto-resolve without it. |
| `SYNC_TOKEN` | (Optional) PAT with `repo` + `workflow` scopes so merges touching `.github/workflows/*` push directly. When omitted, application code still syncs safely while `.github/workflows/*` updates are skipped and logged. |
| `GITHUB_TOKEN` | Automatic runner token used when `SYNC_TOKEN` is not set. |
