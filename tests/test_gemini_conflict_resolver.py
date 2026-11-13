#!/usr/bin/env python3
"""Unit and temporary-repo integration tests for gemini_conflict_resolver."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gemini_conflict_resolver as gcr  # noqa: E402


class TestPureHelpers(unittest.TestCase):
    def test_extract_hunks_handles_empty_sides_crlf_eof_and_diff3(self) -> None:
        text = (
            "header\r\n"
            "<<<<<<< HEAD\r\n"
            "=======\r\n"
            "added_from_upstream()\r\n"
            ">>>>>>> upstream/Dom\r\n"
            "mid\r\n"
            "<<<<<<< HEAD\r\n"
            "    indented_custom()\r\n"
            "||||||| base\r\n"
            "    old_base()\r\n"
            "=======\r\n"
            "    upstream_change()\r\n"
            ">>>>>>> upstream/Dom"
        )
        matches = gcr.extract_conflict_hunks(text)
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].group("ours"), "")
        self.assertEqual(matches[0].group("theirs"), "added_from_upstream()\n")
        self.assertEqual(matches[1].group("ours"), "    indented_custom()\n")
        self.assertEqual(matches[1].group("base"), "    old_base()\n")
        self.assertEqual(matches[1].group("theirs"), "    upstream_change()\n")

    def test_clean_resolved_content_preserves_leading_indentation(self) -> None:
        ai_out = "```python\n    x = 1\n    y = 2\n```"
        cleaned = gcr.clean_resolved_content(ai_out, trailing_newline=True)
        self.assertEqual(cleaned, "    x = 1\n    y = 2\n")

    def test_validate_syntax_catches_bad_python_and_json(self) -> None:
        self.assertIsNone(gcr.validate_syntax("ok.py", "def f():\n    return 1\n"))
        self.assertIsNotNone(gcr.validate_syntax("bad.py", "def f(:\n    pass\n"))
        self.assertIsNone(gcr.validate_syntax("ok.json", '{"a": 1}\n'))
        self.assertIsNotNone(gcr.validate_syntax("bad.json", "{bad}\n"))

    def test_conflict_marker_detection_ignores_normal_equals(self) -> None:
        self.assertFalse(gcr.has_conflict_markers("Title\n=========\nHello\n"))
        self.assertTrue(gcr.has_conflict_markers("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> up\n"))


class TestGitRepoIntegration(unittest.TestCase):
    def _git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)

    def _init_repo(self, cwd: Path) -> None:
        self._git(cwd, "init", "-b", "Dom")
        self._git(cwd, "config", "user.name", "Test")
        self._git(cwd, "config", "user.email", "test@example.com")

    def test_deterministic_and_custom_conflicts_with_ud_du_and_py(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            (repo / "unmodified.py").write_text("VAL = 1\n", encoding="utf-8")
            (repo / "custom.py").write_text("A = 1\nB = 2\n", encoding="utf-8")
            (repo / "fork_kept_up_deleted.txt").write_text("base\n", encoding="utf-8")
            (repo / "fork_deleted_up_edited.txt").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-m", "base")

            # Upstream branch
            self._git(repo, "checkout", "-b", "upstream_branch")
            (repo / "unmodified.py").write_text("VAL = 99\n", encoding="utf-8")
            (repo / "custom.py").write_text("A = 1\nB = 200\n", encoding="utf-8")
            (repo / "fork_kept_up_deleted.txt").unlink()
            (repo / "fork_deleted_up_edited.txt").write_text("upstream edit\n", encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-m", "upstream updates")

            # Fork branch
            self._git(repo, "checkout", "Dom")
            # Automated-only change on unmodified.py, real custom changes on others
            (repo / "unmodified.py").write_text("VAL = 2\n", encoding="utf-8")
            self._git(repo, "add", "unmodified.py")
            self._git(repo, "commit", "-m", "style: format unmodified")

            (repo / "custom.py").write_text("A = 10\nB = 20\n", encoding="utf-8")
            (repo / "fork_kept_up_deleted.txt").write_text("custom fork content\n", encoding="utf-8")
            (repo / "fork_deleted_up_edited.txt").unlink()
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-m", "feat: fork custom edits")

            # Merge upstream_branch into Dom -> conflicts!
            res = self._git(repo, "merge", "--no-edit", "upstream_branch", check=False)
            self.assertNotEqual(res.returncode, 0)

            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key", "UPSTREAM_BASE": self._git(repo, "merge-base", "HEAD", "MERGE_HEAD").stdout.strip()}):
                    with mock.patch.object(gcr, "call_gemini", return_value="A = 10\nB = 200\n"):
                        gcr.main()
            finally:
                os.chdir(old_cwd)

            self.assertEqual((repo / "unmodified.py").read_text(encoding="utf-8"), "VAL = 99\n")
            self.assertEqual((repo / "custom.py").read_text(encoding="utf-8"), "A = 10\nB = 200\n")
            self.assertEqual((repo / "fork_kept_up_deleted.txt").read_text(encoding="utf-8"), "custom fork content\n")
            self.assertFalse((repo / "fork_deleted_up_edited.txt").exists())
            self.assertEqual(gcr.get_conflicted_files(), [])

    def test_3way_patch_sync_without_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            (repo / ".github" / "workflows").mkdir(parents=True)
            (repo / ".github" / "workflows" / "ci.yml").write_text("name: ForkCI\n", encoding="utf-8")
            (repo / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-m", "base")

            self._git(repo, "checkout", "-b", "upstream_branch")
            (repo / ".github" / "workflows" / "ci.yml").write_text("name: UpstreamCI\n", encoding="utf-8")
            (repo / "app.py").write_text("def greet():\n    return 'hello world'\n", encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-m", "upstream ci + app change")
            up_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            self._git(repo, "checkout", "Dom")
            base_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            patch_res = subprocess.run(
                ["git", "diff", "--binary", "--full-index", "-M", f"{base_sha}..{up_sha}", "--", ".", ":!.github/workflows"],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            patch_path = repo / "sync.patch"
            patch_path.write_bytes(patch_res.stdout)
            self._git(repo, "apply", "--3way", "--index", str(patch_path))
            patch_path.unlink()
            self._git(repo, "commit", "-m", "chore(sync): patch", "-m", f"Upstream-Sync-Commit: {up_sha}")

            self.assertIn("ForkCI", (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
            self.assertIn("hello world", (repo / "app.py").read_text(encoding="utf-8"))

    def test_3way_patch_sync_conflict_resolves_with_gemini_and_keeps_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            (repo / ".github" / "workflows").mkdir(parents=True)
            (repo / ".github" / "workflows" / "ci.yml").write_text("name: ForkCI\n", encoding="utf-8")
            (repo / "custom.py").write_text("X = 1\nY = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-m", "base")

            self._git(repo, "checkout", "-b", "upstream_branch")
            (repo / ".github" / "workflows" / "ci.yml").write_text("name: UpstreamCI\n", encoding="utf-8")
            (repo / "custom.py").write_text("X = 1\nY = 20\n", encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-m", "upstream edit")
            up_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            self._git(repo, "checkout", "Dom")
            base_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "custom.py").write_text("X = 10\nY = 2\n", encoding="utf-8")
            self._git(repo, "add", "custom.py")
            self._git(repo, "commit", "-m", "feat: fork X")

            patch_res = subprocess.run(
                ["git", "diff", "--binary", "--full-index", "-M", f"{base_sha}..{up_sha}", "--", ".", ":!.github/workflows"],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            patch_path = repo / "sync.patch"
            patch_path.write_bytes(patch_res.stdout)
            apply_res = self._git(repo, "apply", "--3way", "--index", str(patch_path), check=False)
            patch_path.unlink()
            self.assertNotEqual(apply_res.returncode, 0)

            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(
                    os.environ,
                    {
                        "GEMINI_API_KEY": "fake-key",
                        "UPSTREAM_BASE": base_sha,
                        "SYNC_COMMIT_TRAILER": f"Upstream-Sync-Commit: {up_sha}",
                    },
                ):
                    with mock.patch.object(gcr, "call_gemini", return_value="X = 10\nY = 20\n"):
                        gcr.main()
            finally:
                os.chdir(old_cwd)

            self.assertEqual((repo / "custom.py").read_text(encoding="utf-8"), "X = 10\nY = 20\n")
            self.assertIn(f"Upstream-Sync-Commit: {up_sha}", self._git(repo, "log", "-1", "--format=%B").stdout)


if __name__ == "__main__":
    unittest.main()
