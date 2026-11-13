#!/usr/bin/env python3
"""
Automated Git merge conflict resolver using Google Gemini API with smart hunk-by-hunk
resolution, porcelain tree-conflict handling, syntax verification, rate-limit backoff,
and fork divergence preservation.

Designed for MarkenJaden's centralized reusable GitHub Actions workflows.
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-2.5-pro",
]

AUTOMATED_COMMIT_PREFIXES = (
    "style: format",
    "merge: auto-resolve",
    "chore(sync):",
    "Merge remote-tracking",
    "Merge branch",
)

BINARY_OR_LOCK_EXTENSIONS = (
    ".mo", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".lock", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".bin", ".signed",
    ".tar", ".gz", ".xz", ".bz2", ".zip", ".pdf",
)

# Matches Git merge conflict blocks including diff3 base sections, CRLF, empty sides, and EOF without newline.
CONFLICT_BLOCK_RE = re.compile(
    r"^<{7}[^\r\n]*\r?\n"
    r"(?P<ours>.*?)"
    r"(?:^\|{7}[^\r\n]*\r?\n(?P<base>.*?))?"
    r"^={7}[^\r\n]*\r?\n"
    r"(?P<theirs>.*?)"
    r"^>{7}[^\r\n]*(?:\r?\n|\Z)",
    re.MULTILINE | re.DOTALL,
)

LEFTOVER_MARKER_RE = re.compile(r"^(?:<{7}(?:\s|$)|={7}$|>{7}(?:\s|$))", re.MULTILINE)

MAX_FULL_FILE_BYTES = 250_000
MAX_DIVERGENCE_CHARS = 24_000
_LAST_API_CALL_MONOTONIC = 0.0


def get_models() -> list[str]:
    env_models = os.environ.get("GEMINI_MODELS", "").strip()
    if env_models:
        parsed = [m.strip() for m in env_models.split(",") if m.strip()]
        if parsed:
            return parsed
    return list(DEFAULT_MODELS)


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def get_merge_base() -> str:
    """Find the common ancestor commit between HEAD and the incoming upstream ref."""
    explicit_base = os.environ.get("UPSTREAM_BASE", "").strip()
    if explicit_base:
        res = run_git(["rev-parse", "--verify", f"{explicit_base}^{{commit}}"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

    upstream_branch = os.environ.get("UPSTREAM_BRANCH", "").strip()
    candidates = ["MERGE_HEAD"]
    if upstream_branch:
        candidates.append(f"upstream/{upstream_branch}")
    candidates.extend(["FETCH_HEAD", "upstream/Dom", "upstream/main", "upstream/latest", "upstream/master"])

    seen = set()
    for ref in candidates:
        if ref in seen:
            continue
        seen.add(ref)
        res = run_git(["merge-base", "HEAD", ref], check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return ""


def file_has_custom_fork_commits(file_path: str, merge_base: str) -> bool:
    """Return True if the file was touched by custom fork commits since merge_base."""
    if not merge_base:
        return True
    res = run_git(["log", "--oneline", f"{merge_base}..HEAD", "--", file_path], check=False)
    if res.returncode != 0:
        return True
    commits = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if not commits:
        return False
    for line in commits:
        msg = " ".join(line.split()[1:])
        if not any(msg.startswith(prefix) for prefix in AUTOMATED_COMMIT_PREFIXES):
            return True
    return False


def get_unmerged_entries() -> dict[str, str]:
    """Return mapping of path -> 2-char XY porcelain status for all unmerged paths."""
    res = subprocess.run(
        ["git", "status", "--porcelain=v1", "-uno", "-z"],
        capture_output=True,
        check=True,
    )
    entries = res.stdout.split(b"\x00")
    unmerged_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    statuses: dict[str, str] = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy = entry[:2].decode("utf-8", errors="replace")
        path = entry[3:].decode("utf-8", errors="replace")
        if xy[0] in ("R", "C"):
            # Skip NUL-separated original path on renames/copies
            i += 1
        if xy in unmerged_codes:
            statuses[path] = xy
    return statuses


def get_conflicted_files() -> list[str]:
    return sorted(get_unmerged_entries().keys())


def is_binary_file(file_path: str) -> bool:
    lower = file_path.lower()
    if lower.endswith(BINARY_OR_LOCK_EXTENSIONS):
        return True
    p = Path(file_path)
    if not p.is_file():
        return False
    try:
        with p.open("rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False


def extract_conflict_hunks(text: str) -> list[re.Match[str]]:
    normalized = text.replace("\r\n", "\n")
    return list(CONFLICT_BLOCK_RE.finditer(normalized))


def has_conflict_markers(text: str) -> bool:
    return bool(LEFTOVER_MARKER_RE.search(text.replace("\r\n", "\n")))


def clean_resolved_content(content: str, *, trailing_newline: bool = True) -> str:
    """Strip outer markdown code fences without corrupting leading line indentation."""
    cleaned = content.replace("\r\n", "\n").strip("\r\n")
    lines = cleaned.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    body = "\n".join(lines).rstrip("\r\n")
    if not body:
        return ""
    return f"{body}\n" if trailing_newline else body


def validate_syntax(file_path: str, text: str) -> str | None:
    """Return None if file passes syntax check for its language, or an error description."""
    lower = file_path.lower()
    if lower.endswith(".py"):
        try:
            ast.parse(text, filename=file_path)
        except SyntaxError as exc:
            return f"Python SyntaxError at line {exc.lineno}: {exc.msg}"
    elif lower.endswith(".json"):
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"JSONDecodeError at line {exc.lineno}, col {exc.colno}: {exc.msg}"
    elif lower.endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
            list(yaml.safe_load_all(text))
        except ImportError:
            pass
        except Exception as exc:
            return f"YAML parse error: {exc}"
    elif lower.endswith((".js", ".mjs", ".cjs")):
        node_bin = shutil.which("node")
        if node_bin:
            suffix = Path(lower).suffix or ".js"
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False) as tf:
                tf.write(text)
                tmp_path = tf.name
            try:
                proc = subprocess.run([node_bin, "--check", tmp_path], capture_output=True, text=True)
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout).strip().splitlines()[:5]
                    return "Node syntax check failed: " + " | ".join(err)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    return None


def call_gemini(prompt: str, api_key: str) -> str:
    global _LAST_API_CALL_MONOTONIC
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 65536,
        },
    }
    data = json.dumps(payload).encode("utf-8")

    min_interval = float(os.environ.get("GEMINI_MIN_INTERVAL_SECONDS", "1.2"))
    last_error: Exception | None = None

    for model in get_models():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        for attempt in range(3):
            elapsed = time.monotonic() - _LAST_API_CALL_MONOTONIC
            if _LAST_API_CALL_MONOTONIC > 0 and elapsed < min_interval:
                time.sleep(min_interval - elapsed)

            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                print(f"Calling {model} (attempt {attempt + 1})...")
                _LAST_API_CALL_MONOTONIC = time.monotonic()
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                candidates = resp_json.get("candidates") or []
                if not candidates:
                    raise RuntimeError(f"Empty Gemini candidates: {resp_json}")
                parts = candidates[0].get("content", {}).get("parts") or []
                text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
                if not text:
                    finish_reason = candidates[0].get("finishReason", "unknown")
                    raise RuntimeError(f"Empty Gemini response text (finishReason={finish_reason})")
                return text
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                print(f"HTTPError {exc.code} with {model}: {err_body[:400]}")
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504):
                    backoff = (2 ** attempt) * 4.0 + random.uniform(0.3, 1.8)
                    print(f"Transient HTTP {exc.code}; sleeping {backoff:.1f}s...")
                    time.sleep(backoff)
                    continue
                # 400/404 model/request error -> move to next model immediately
                break
            except Exception as exc:
                print(f"Request failed with {model}: {exc}")
                last_error = exc
                time.sleep(2.0 + random.uniform(0.1, 0.8))

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def load_fork_divergences() -> str:
    """Load FORK_DIVERGENCES.md from current working directory if present."""
    for p in (
        Path.cwd() / "FORK_DIVERGENCES.md",
        Path(__file__).resolve().parent.parent / "FORK_DIVERGENCES.md",
    ):
        try:
            if p.is_file():
                content = p.read_text(encoding="utf-8", errors="replace")
                if len(content) > MAX_DIVERGENCE_CHARS:
                    content = content[:MAX_DIVERGENCE_CHARS] + "\n\n[... truncated for prompt budget ...]"
                return f"\nFork Specifications & Invariants:\n{content}\n"
        except OSError:
            pass
    return ""


def _stage_ours(file_path: str) -> None:
    run_git(["checkout", "--ours", "--", file_path])
    run_git(["add", "--", file_path])


def _stage_theirs(file_path: str) -> None:
    run_git(["checkout", "--theirs", "--", file_path])
    run_git(["add", "--", file_path])


def _remove_path(file_path: str) -> None:
    run_git(["rm", "-f", "--", file_path])


def resolve_tree_or_binary_conflict(file_path: str, status_xy: str, has_custom: bool) -> bool | None:
    """
    Handle deleted/added tree conflicts and binary/lock conflicts deterministically.
    Returns True if resolved and staged, False if failed, or None if standard text resolution should run.
    """
    if status_xy == "DD":
        print(f"Both sides deleted {file_path}; removing from index.")
        _remove_path(file_path)
        return True

    if status_xy == "UD":
        # Upstream deleted, fork kept/modified
        if has_custom:
            print(f"Fork customized {file_path} while upstream deleted it; preserving fork version.")
            run_git(["add", "--", file_path])
        else:
            print(f"Upstream deleted unmodified {file_path}; accepting deletion.")
            _remove_path(file_path)
        return True

    if status_xy == "DU":
        # Fork deleted, upstream modified
        if has_custom:
            print(f"Fork deleted {file_path} while upstream modified it; preserving fork deletion.")
            _remove_path(file_path)
        else:
            print(f"Accepting upstream version for {file_path} (no custom fork deletion found).")
            _stage_theirs(file_path)
        return True

    if is_binary_file(file_path):
        if has_custom:
            print(f"Binary/lock conflict in custom file {file_path}; preserving fork version.")
            _stage_ours(file_path)
        else:
            print(f"Binary/lock conflict in unmodified file {file_path}; accepting upstream version.")
            _stage_theirs(file_path)
        return True

    if not has_custom and status_xy in ("UU", "AA", "AU", "UA"):
        print(f"Auto-resolving {file_path}: no custom fork commits; accepting upstream.")
        _stage_theirs(file_path)
        return True

    return None


def _resolve_hunks_with_gemini(
    file_path: str,
    normalized_content: str,
    api_key: str,
    repo_name: str,
    divergences_text: str,
) -> str | None:
    matches = extract_conflict_hunks(normalized_content)
    if not matches:
        return None

    print(f"Found {len(matches)} conflict hunk(s) in {file_path}. Resolving hunk-by-hunk...")
    lines_all = normalized_content
    resolved_text = normalized_content

    for idx, match in reversed(list(enumerate(matches, start=1))):
        head_code = match.group("ours").rstrip("\n")
        base_code = (match.group("base") or "").rstrip("\n")
        incoming_code = match.group("theirs").rstrip("\n")
        start_idx = match.start()
        end_idx = match.end()

        before_lines = lines_all[:start_idx].splitlines()[-15:]
        after_lines = lines_all[end_idx:].splitlines()[:15:]
        base_section = f"\nCommon ancestor (base):\n{base_code}\n" if base_code else ""
        hunk_had_trailing_nl = match.group(0).endswith("\n")

        prompt = f"""You are an expert software engineer resolving Git merge conflict hunk #{idx} in {repo_name}.
File path: {file_path}
{divergences_text}
Context before conflict:
{chr(10).join(before_lines)}

Conflict hunk:
<<<<<<< HEAD (local fork features)
{head_code}
{base_section}======= (incoming upstream changes)
{incoming_code}
>>>>>>> upstream

Context after conflict:
{chr(10).join(after_lines)}

Instructions:
1. Merge the HEAD block and incoming upstream block cleanly.
2. Preserve local fork features and invariants from HEAD/FORK_DIVERGENCES.md.
3. Integrate incoming upstream fixes and improvements seamlessly.
4. Preserve exact leading indentation.
5. Output ONLY the replacement code for this conflict hunk. No markdown fences, no conflict markers, no explanations.
"""
        resolved_hunk = call_gemini(prompt, api_key)
        cleaned = clean_resolved_content(resolved_hunk, trailing_newline=hunk_had_trailing_nl)
        if has_conflict_markers(cleaned):
            raise ValueError(f"Gemini output still contained conflict markers for {file_path} hunk #{idx}")
        resolved_text = resolved_text[:start_idx] + cleaned + resolved_text[end_idx:]

    return resolved_text


def _resolve_full_file_with_gemini(
    file_path: str,
    normalized_content: str,
    api_key: str,
    repo_name: str,
    divergences_text: str,
    *,
    validation_hint: str = "",
) -> str:
    if len(normalized_content.encode("utf-8")) > MAX_FULL_FILE_BYTES:
        raise ValueError(f"{file_path} exceeds {MAX_FULL_FILE_BYTES} bytes limit for full-file fallback")

    had_trailing_nl = normalized_content.endswith("\n")
    hint_block = f"\nPrevious resolution failed syntax validation: {validation_hint}\nFix that syntax error strictly.\n" if validation_hint else ""
    prompt = f"""You are an expert software engineer resolving Git merge conflicts for {repo_name}.
File path: {file_path}
{hint_block}
Git Context:
- HEAD blocks contain local fork custom features.
- Incoming blocks contain upstream updates and bug fixes.
{divergences_text}

Instructions:
1. Merge both sides intelligently without losing local fork features.
2. Avoid duplicate imports, duplicate symbol definitions, or syntax errors.
3. Remove all conflict markers (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`).
4. Output ONLY raw resolved file text with NO markdown code fences.

File content:
{normalized_content}
"""
    resolved = call_gemini(prompt, api_key)
    cleaned = clean_resolved_content(resolved, trailing_newline=had_trailing_nl)
    if has_conflict_markers(cleaned):
        raise ValueError(f"Full-file resolution for {file_path} still contained conflict markers")
    return cleaned


def resolve_file(file_path: str, status_xy: str, api_key: str, merge_base: str, repo_name: str) -> bool:
    print(f"\n--- Resolving conflict ({status_xy}) in: {file_path} ---")
    has_custom = file_has_custom_fork_commits(file_path, merge_base)

    handled = resolve_tree_or_binary_conflict(file_path, status_xy, has_custom)
    if handled is not None:
        return handled

    p = Path(file_path)
    if not p.is_file():
        print(f"Expected working-tree file missing for {file_path} ({status_xy}); failing cleanly.")
        return False

    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        print(f"Could not read {file_path}: {exc}")
        return False

    uses_crlf = b"\r\n" in raw_bytes
    content = raw_bytes.decode("utf-8", errors="replace")
    normalized = content.replace("\r\n", "\n")

    if not has_conflict_markers(normalized):
        print(f"No conflict markers found in {file_path} (likely mode/index conflict); staging current file.")
        run_git(["add", "--", file_path])
        return True

    divergences_text = load_fork_divergences()

    try:
        resolved_norm = _resolve_hunks_with_gemini(file_path, normalized, api_key, repo_name, divergences_text)
        if resolved_norm is None:
            print(f"Hunk regex did not match cleanly in {file_path}; trying full-file resolution...")
            resolved_norm = _resolve_full_file_with_gemini(file_path, normalized, api_key, repo_name, divergences_text)

        syntax_err = validate_syntax(file_path, resolved_norm)
        if syntax_err:
            print(f"Syntax validation failed on first pass for {file_path}: {syntax_err}. Retrying with repair prompt...")
            repair_source = resolved_norm if not has_conflict_markers(resolved_norm) else normalized
            resolved_norm = _resolve_full_file_with_gemini(
                file_path,
                repair_source,
                api_key,
                repo_name,
                divergences_text,
                validation_hint=syntax_err,
            )
            syntax_err = validate_syntax(file_path, resolved_norm)
            if syntax_err:
                print(f"Syntax validation still failed for {file_path}: {syntax_err}")
                return False

        final_text = resolved_norm.replace("\n", "\r\n") if uses_crlf else resolved_norm
        p.write_text(final_text, encoding="utf-8", newline="")
        run_git(["add", "--", file_path])
        print(f"Successfully resolved, validated, and staged {file_path}")
        return True
    except Exception as exc:
        print(f"Failed to resolve {file_path}: {exc}")
        return False


def commit_resolved_state() -> None:
    trailer = os.environ.get("SYNC_COMMIT_TRAILER", "").strip()
    merge_head = run_git(["rev-parse", "--verify", "MERGE_HEAD"], check=False).returncode == 0
    if merge_head:
        cmd = ["git", "commit", "--no-edit", "-m", "merge: auto-resolve upstream conflicts using Gemini"]
        if trailer:
            cmd.extend(["-m", trailer])
        subprocess.run(cmd, check=True)
    else:
        cmd = ["git", "commit", "-m", "chore(sync): auto-resolve upstream changes using Gemini"]
        if trailer:
            cmd.extend(["-m", trailer])
        subprocess.run(cmd, check=True)


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    repo_name = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "this project"

    unmerged = get_unmerged_entries()
    if not unmerged:
        print("No conflicted files found.")
        sys.exit(0)

    merge_base = get_merge_base()
    print(f"Merge base: {merge_base or 'unknown'}")
    print(f"Found {len(unmerged)} conflicted file(s):")
    for f, xy in sorted(unmerged.items()):
        print(f"  - [{xy}] {f}")

    # Deterministic conflicts (unmodified upstream files, binary/locks, deletions) can succeed even without GEMINI_API_KEY.
    resolved_files: list[str] = []
    failed_files: list[str] = []

    for f, xy in sorted(unmerged.items()):
        has_custom = file_has_custom_fork_commits(f, merge_base)
        handled = resolve_tree_or_binary_conflict(f, xy, has_custom)
        if handled is True:
            resolved_files.append(f)
            continue
        if handled is False:
            failed_files.append(f)
            continue
        if not api_key:
            print(f"No GEMINI_API_KEY configured; cannot AI-resolve custom conflict in {f}.")
            failed_files.append(f)
            continue
        if resolve_file(f, xy, api_key, merge_base, repo_name):
            resolved_files.append(f)
        else:
            failed_files.append(f)

    report_path = os.environ.get("CONFLICT_REPORT_PATH", "").strip()
    if report_path:
        report = {
            "resolved": resolved_files,
            "failed": failed_files,
        }
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    if failed_files:
        print("\nError: Could not automatically resolve all conflicted files:")
        for f in failed_files:
            print(f"  - {f}")
        sys.exit(1)

    remaining = get_conflicted_files()
    if remaining:
        print(f"Error: {len(remaining)} conflicted files still remain unmerged: {remaining}")
        sys.exit(1)

    print("\nAll conflicts resolved cleanly. Committing sync...")
    commit_resolved_state()
    print("Sync commit created successfully.")


if __name__ == "__main__":
    main()
