# MarkenJaden Workflows

Centralized repository for reusable GitHub Actions workflows across MarkenJaden projects.

## Available Workflows

### 1. `upstream-sync.yml` (Automated Upstream Sync with AI Conflict Resolution)

Keeps your fork synchronized with upstream repositories. If merge conflicts occur, Google Gemini automatically resolves them hunk-by-hunk while preserving your fork's custom features and invariants declared in `FORK_DIVERGENCES.md`.

#### Usage in Caller Repositories

Create `.github/workflows/upstream-sync.yml` in your project repository:

```yaml
name: Upstream Sync & Validate

on:
  schedule:
    # Run daily at 04:00 UTC
    - cron: '0 4 * * *'
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write
  workflows: write

jobs:
  sync:
    uses: MarkenJaden/workflows/.github/workflows/upstream-sync.yml@main
    with:
      upstream_repo: 'OpenByteInc/QuantDinger'
      upstream_branch: 'main'
      target_branch: 'main'
      preflight_command: |
        cd backend_api_python
        python -m py_compile run.py
        python -m compileall -q app scripts
        cd ..
        python scripts/check_version.py
        python scripts/check_mojibake.py
      post_merge_workflow: 'docker-publish.yml'
    secrets: inherit
```

#### Inputs

| Input | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `upstream_repo` | **Yes** | - | Upstream repository in format `owner/repo` |
| `upstream_branch` | No | `main` | Branch to fetch from upstream |
| `target_branch` | No | `main` | Target branch in this repository to merge into |
| `python_version` | No | `3.12` | Python version for runner environment |
| `preflight_command`| No | `''` | Shell commands to run for syntax / lint / preflight checks |
| `test_command` | No | `''` | Shell command to execute your test suite |
| `post_merge_workflow` | No | `''` | Workflow filename in caller repo to trigger after push (e.g. `docker-publish.yml`) |

#### Secrets

| Secret | Description |
| :--- | :--- |
| `GEMINI_API_KEY` | (Optional) API key for automated AI merge conflict resolution. If omitted, conflicts will fall back to automated branch creation and a Pull Request. |
| `GITHUB_TOKEN` | Handled automatically via `secrets: inherit` or repository default token. |
