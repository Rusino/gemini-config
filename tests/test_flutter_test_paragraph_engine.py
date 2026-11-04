#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

FLUTTER_SKILL_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skills/flutter-test-paragraph-engine/scripts"
)
if str(FLUTTER_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(FLUTTER_SKILL_DIR))

import parse_flutter_paragraph_tests as pfpt
import run_flutter_paragraph_tests as rfpt


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestFlutterExternalSkiaPreflight(unittest.TestCase):
    def test_external_skia_staleness_and_preflight_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            ext_repo = tmp / "ext_skia"
            ext_repo.mkdir()
            _git(ext_repo, "init", "-b", "main")
            _git(ext_repo, "config", "user.email", "t@example.com")
            _git(ext_repo, "config", "user.name", "Test")

            src_dir = ext_repo / "modules/skparagraph/src"
            tst_dir = ext_repo / "modules/skparagraph/tests"
            gpu_dir = ext_repo / "src/gpu"
            src_dir.mkdir(parents=True)
            tst_dir.mkdir(parents=True)
            gpu_dir.mkdir(parents=True)

            (src_dir / "Run.h").write_text("struct Run { int fPositions; };\n", encoding="utf-8")
            (tst_dir / "SkParagraphTest.cpp").write_text("int t = 1;\n", encoding="utf-8")
            (gpu_dir / "Gpu.cpp").write_text("int g = 1;\n", encoding="utf-8")
            _git(ext_repo, "add", ".")
            _git(ext_repo, "commit", "-m", "deps base")
            deps_rev = _git(ext_repo, "rev-parse", "HEAD")

            _git(ext_repo, "checkout", "-b", "stale_stack", deps_rev)
            (src_dir / "Cand.cpp").write_text("int cand = 1;\n", encoding="utf-8")
            _git(ext_repo, "add", "modules/skparagraph/src/Cand.cpp")
            _git(ext_repo, "commit", "-m", "cand on stale base")

            _git(ext_repo, "checkout", "main")
            (src_dir / "Run.h").write_text("struct Run { int fGlyphData; };\n", encoding="utf-8")
            _git(ext_repo, "add", "modules/skparagraph/src/Run.h")
            _git(ext_repo, "commit", "-m", "upstream fGlyphData")
            _git(ext_repo, "update-ref", "refs/remotes/upstream/main", "HEAD")
            up_main = _git(ext_repo, "rev-parse", "HEAD")

            with self.assertRaisesRegex(RuntimeError, "behind upstream/main"):
                rfpt.check_external_skia_staleness(
                    ext_repo,
                    "stale_stack",
                    label="apple_cand",
                    allow_mismatch=False,
                    deps_skia_revision=deps_rev,
                    fetch_remote=False,
                )

            bypassed = rfpt.check_external_skia_staleness(
                ext_repo,
                "stale_stack",
                label="apple_cand",
                allow_mismatch=True,
                deps_skia_revision=deps_rev,
                fetch_remote=False,
            )
            self.assertIsNotNone(bypassed)
            assert bypassed is not None
            self.assertIn("modules/skparagraph/src/Run.h", bypassed["overlapping_files"])

            # Rebased branch stays clean even if upstream later touches tests/ or src/gpu/
            _git(ext_repo, "checkout", "-b", "rebased_stack", up_main)
            (src_dir / "Cand.cpp").write_text("int cand = 2;\n", encoding="utf-8")
            _git(ext_repo, "add", "modules/skparagraph/src/Cand.cpp")
            _git(ext_repo, "commit", "-m", "cand rebased")

            _git(ext_repo, "checkout", "main")
            (tst_dir / "SkParagraphTest.cpp").write_text("int t = 2;\n", encoding="utf-8")
            (gpu_dir / "Gpu.cpp").write_text("int g = 2;\n", encoding="utf-8")
            _git(ext_repo, "add", ".")
            _git(ext_repo, "commit", "-m", "test and gpu only")
            _git(ext_repo, "update-ref", "refs/remotes/upstream/main", "HEAD")

            clean = rfpt.check_external_skia_staleness(
                ext_repo,
                "rebased_stack",
                label="apple_cand",
                allow_mismatch=False,
                deps_skia_revision=deps_rev,
                fetch_remote=False,
            )
            self.assertIsNone(clean)

            with self.assertRaisesRegex(RuntimeError, "Cannot resolve external Skia ref"):
                rfpt.check_external_skia_staleness(
                    ext_repo,
                    "nonexistent_branch",
                    label="apple_cand",
                    allow_mismatch=True,
                    fetch_remote=False,
                )

            # Attestation parser must flag bypassed external Skia base skew
            runs = [
                (
                    "baseline",
                    {
                        "attestation": {
                            "ref": "HEAD@icu",
                            "git_sha": deps_rev[:12],
                            "unicode_backend": "ICU",
                            "requested_unicode": "icu",
                            "source_tree_sha256": "111111111111",
                            "binary_sha256": "aaaaaaaaaaaa",
                            "ninja_rc": 0,
                            "ninja_actions": "1/1",
                            "batches_run": "all",
                            "test_rc": 0,
                        },
                        "summary": {
                            "total_passed": 10,
                            "total_failed": 0,
                            "finished_cleanly": True,
                        },
                    },
                ),
                (
                    "apple_cand",
                    {
                        "attestation": {
                            "ref": "ext:stale_stack@apple",
                            "git_sha": "deadbeef1234",
                            "unicode_backend": "Apple (CFString/CoreText)",
                            "requested_unicode": "apple",
                            "source_tree_sha256": "222222222222",
                            "binary_sha256": "bbbbbbbbbbbb",
                            "ninja_rc": 0,
                            "ninja_actions": "1/1",
                            "batches_run": "all",
                            "test_rc": 0,
                            "ext_skia_staleness": bypassed,
                        },
                        "summary": {
                            "total_passed": 10,
                            "total_failed": 0,
                            "finished_cleanly": True,
                        },
                    },
                ),
            ]
            issues, _ = pfpt._evaluate_attestation(runs)
            self.assertTrue(any("external Skia base skew" in iss for iss in issues))

            # Submodule HEAD mismatch bypassed via --allow-skia-base-mismatch must also flag WARNING
            runs_sub_mismatch = [
                (
                    "baseline",
                    {
                        **runs[0][1],
                        "attestation": {
                            **runs[0][1]["attestation"],
                            "skia_base_matches_deps": False,
                            "skia_base_head": "111111111111",
                            "deps_skia_revision": deps_rev,
                        },
                    },
                )
            ]
            sub_issues, _ = pfpt._evaluate_attestation(runs_sub_mismatch)
            self.assertTrue(any("DEPS" in iss or "skia_revision" in iss for iss in sub_issues))

            # Offline TimeoutExpired on upstream fetch must fall back to origin without crashing
            def flaky_subproc(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
                if cmd[3:] == ["remote"]:
                    return subprocess.CompletedProcess(cmd, 0, "upstream\norigin\n", "")
                if len(cmd) >= 5 and cmd[3] == "fetch" and cmd[5] == "upstream":
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
                if len(cmd) >= 5 and cmd[3] == "fetch" and cmd[5] == "origin":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return subprocess.CompletedProcess(cmd, 1, "", "")

            with mock.patch("subprocess.run", side_effect=flaky_subproc) as m_sub:
                rfpt._resolve_freshest_upstream_ref(ext_repo, fetch_remote=True)
            fetched = [c.args[0][5] for c in m_sub.call_args_list if c.args[0][3] == "fetch"]
            self.assertEqual(fetched, ["upstream", "origin"])

            # Missing deps_skia_revision in ext_repo is fetched from submodule to detect skew
            isolated_ext = tmp / "isolated_ext"
            isolated_ext.mkdir()
            _git(isolated_ext, "init", "-b", "main")
            _git(isolated_ext, "config", "user.email", "t@example.com")
            _git(isolated_ext, "config", "user.name", "Test")
            _git(isolated_ext, "fetch", str(ext_repo), "stale_stack")
            _git(isolated_ext, "checkout", "-b", "stale_stack", "FETCH_HEAD")

            sub_ahead = tmp / "sub_ahead"
            sub_ahead.mkdir()
            _git(sub_ahead, "init", "-b", "main")
            _git(sub_ahead, "fetch", str(ext_repo), up_main)
            _git(sub_ahead, "checkout", "-f", up_main)

            with self.assertRaisesRegex(RuntimeError, "behind Flutter DEPS skia_revision"):
                rfpt.check_external_skia_staleness(
                    isolated_ext,
                    "stale_stack",
                    label="apple_cand",
                    allow_mismatch=False,
                    deps_skia_revision=up_main,
                    skia_submodule_repo=sub_ahead,
                    fetch_remote=False,
                )

            # Pre-flight in main() must abort on stale ext: ref BEFORE checkout_skia_ref runs leg 0
            flutter_repo = tmp / "flutter"
            sub_skia = flutter_repo / "engine/src/flutter/third_party/skia"
            sub_skia.mkdir(parents=True)
            _git(flutter_repo, "init", "-b", "master")
            _git(flutter_repo, "config", "user.email", "t@example.com")
            _git(flutter_repo, "config", "user.name", "Test")
            (flutter_repo / ".gitignore").write_text("engine/\n", encoding="utf-8")
            (flutter_repo / "DEPS").write_text(
                f"vars = {{'skia_revision': '{deps_rev}'}}\n", encoding="utf-8"
            )
            _git(flutter_repo, "add", ".gitignore", "DEPS")
            _git(flutter_repo, "commit", "-m", "deps")

            _git(sub_skia, "init", "-b", "main")
            _git(sub_skia, "config", "user.email", "t@example.com")
            _git(sub_skia, "config", "user.name", "Test")
            _git(sub_skia, "fetch", str(ext_repo), deps_rev)
            _git(sub_skia, "checkout", "-f", deps_rev)

            checkout_calls: list[str] = []

            def spy_checkout(skia_r: pathlib.Path, ref_arg: str, *a: object, **kw: object) -> dict:
                checkout_calls.append(ref_arg)
                return {
                    "ref": ref_arg,
                    "git_sha": deps_rev[:12],
                    "git_subject": "s",
                    "dirty_files": [],
                }

            argv = [
                "run_flutter_paragraph_tests.py",
                "--repo",
                str(flutter_repo),
                "--external-skia",
                str(ext_repo),
                "--out",
                str(tmp / "fl_out"),
                "baseline=HEAD@icu",
                "cand=ext:stale_stack@icu",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                rfpt, "checkout_skia_ref", side_effect=spy_checkout
            ):
                with self.assertRaisesRegex(RuntimeError, "behind upstream/main"):
                    rfpt.main()
            self.assertEqual(checkout_calls, [])


if __name__ == "__main__":
    unittest.main()
