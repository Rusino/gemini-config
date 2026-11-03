#!/usr/bin/env python3
"""Contract tests for scripts/gerrit_draft.py, gerrit_draft_guard.py, and review prompts."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _d in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


class FakeGerritServer:
    """In-memory Gerrit REST double enforcing Gerrit's same-patchset reply invariant."""

    def __init__(self, current_ps: int = 2, owner_email: str = "dbebawy@gmail.com") -> None:
        self.current_ps = current_ps
        self.owner_email = owner_email
        self.project = "skia"
        self.revisions = {
            f"sha_ps{n}": {"_number": n} for n in range(1, current_ps + 1)
        }
        self.comments: dict[str, list[dict]] = {}
        self.drafts_by_ps: dict[int, dict[str, list[dict]]] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self._next_id = 1

    def handle(self, host: str, method: str, api_path: str, payload: dict | None = None):
        self.calls.append((method, api_path, payload))
        if api_path.startswith("changes/1414416?"):
            return {
                "_number": 1414416,
                "project": self.project,
                "subject": "Fix paragraph trailing spaces",
                "status": "NEW",
                "owner": {"email": self.owner_email},
                "current_revision": f"sha_ps{self.current_ps}",
                "revisions": self.revisions,
            }
        if api_path == "changes/1414416/comments":
            return self.comments
        if api_path == "changes/1414416/drafts":
            merged: dict[str, list[dict]] = {}
            for ps_num, by_path in sorted(self.drafts_by_ps.items()):
                for path, items in by_path.items():
                    for it in items:
                        merged.setdefault(path, []).append({**it, "patch_set": ps_num})
            return merged
        for ps_num in range(1, self.current_ps + 5):
            prefix = f"changes/1414416/revisions/{ps_num}/drafts"
            if api_path == prefix and method == "GET":
                return self.drafts_by_ps.get(ps_num, {})
            if (api_path == prefix or api_path.startswith(prefix + "/")) and method == "PUT":
                assert payload is not None
                # Enforce Gerrit's real HTTP 400 error on cross-patchset thread replies
                reply_id = payload.get("in_reply_to")
                if reply_id:
                    parent_ps = None
                    for items in self.comments.values():
                        for c in items:
                            if c.get("id") == reply_id:
                                parent_ps = c.get("patch_set")
                    if parent_ps is not None and parent_ps != ps_num:
                        raise RuntimeError(
                            "HTTP 400: Comment replies must be submitted on the same "
                            "patchset as the parent comment"
                        )
                path = payload["path"]
                ps_map = self.drafts_by_ps.setdefault(ps_num, {})
                items = ps_map.setdefault(path, [])
                if api_path.startswith(prefix + "/"):
                    draft_id = api_path.split("/")[-1]
                    for idx, existing in enumerate(items):
                        if existing["id"] == draft_id:
                            updated = {**existing, **payload, "id": draft_id, "patch_set": ps_num}
                            items[idx] = updated
                            return updated
                    raise AssertionError(f"Unknown draft id {draft_id}")
                draft_id = f"draft000_{self._next_id:08x}"
                self._next_id += 1
                created = {**payload, "id": draft_id, "patch_set": ps_num}
                items.append(created)
                return created
        raise AssertionError(f"Unexpected Gerrit request: {method} {api_path}")


class TestGerritDraftScript(unittest.TestCase):

    def test_parse_cl_target_variants(self) -> None:
        import gerrit_draft

        t1 = gerrit_draft.parse_cl_target(
            "https://skia-review.googlesource.com/c/skia/+/1414416/2"
        )
        self.assertEqual(t1, ("skia-review.googlesource.com", "1414416", 2))

        t2 = gerrit_draft.parse_cl_target("gerrit:1414416", host="skia")
        self.assertEqual(t2, ("skia-review.googlesource.com", "1414416", None))

        t3 = gerrit_draft.parse_cl_target("https://crrev.com/c/8663938/4", ps=5)
        self.assertEqual(t3, ("chromium-review.googlesource.com", "8663938", 5))

    def test_in_reply_to_uses_parent_comment_patchset_not_latest(self) -> None:
        import gerrit_draft

        srv = FakeGerritServer(current_ps=2)
        srv.comments["modules/skparagraph/src/TextLine.cpp"] = [
            {
                "id": "77640dee_0908b885",
                "patch_set": 1,
                "line": 1435,
                "unresolved": True,
                "message": "Unused capture in lambda",
            }
        ]
        stderr_buf = io.StringIO()
        with mock.patch.object(gerrit_draft, "gerrit_request", side_effect=srv.handle):
            res = gerrit_draft.save_draft(
                cl="1414416",
                ps=2,
                in_reply_to="77640dee_0908b885",
                message="Good catch, can drop the unused capture in PS3.",
                err=stderr_buf,
            )
        self.assertEqual(res["ps"], 1)
        self.assertEqual(res["path"], "modules/skparagraph/src/TextLine.cpp")
        self.assertEqual(res["line"], 1435)
        self.assertTrue(res["unresolved"])
        self.assertEqual(res["action"], "created")
        self.assertIn("PS1", stderr_buf.getvalue())
        put_paths = [p for m, p, _ in srv.calls if m == "PUT"]
        self.assertEqual(put_paths, ["changes/1414416/revisions/1/drafts"])

    def test_in_reply_to_rejects_mismatched_path_or_draft_id(self) -> None:
        import gerrit_draft

        srv = FakeGerritServer(current_ps=2)
        srv.comments["modules/skparagraph/src/TextLine.cpp"] = [
            {"id": "77640dee_0908b885", "patch_set": 1, "line": 1435, "unresolved": True}
        ]
        srv.drafts_by_ps[2] = {
            "/PATCHSET_LEVEL": [
                {"id": "deadbeef_00000001", "message": "Draft", "unresolved": False}
            ]
        }
        with mock.patch.object(gerrit_draft, "gerrit_request", side_effect=srv.handle):
            with self.assertRaises(gerrit_draft.GerritDraftError):
                gerrit_draft.save_draft(
                    cl="1414416",
                    path="modules/skparagraph/src/ParagraphImpl.cpp",
                    in_reply_to="77640dee_0908b885",
                    message="Wrong file",
                )
            with self.assertRaises(gerrit_draft.GerritDraftError) as ctx:
                gerrit_draft.save_draft(
                    cl="1414416",
                    in_reply_to="deadbeef_00000001",
                    message="Replying to own draft",
                )
            self.assertIn("unpublished draft", str(ctx.exception))

        # B3: file-level parent comment (line=None) must reject explicit --line
        srv.comments["modules/skparagraph/src/ParagraphImpl.cpp"] = [
            {"id": "filelvl_00000002", "patch_set": 1, "line": None, "unresolved": True}
        ]
        with mock.patch.object(gerrit_draft, "gerrit_request", side_effect=srv.handle):
            with self.assertRaises(gerrit_draft.GerritDraftError):
                gerrit_draft.save_draft(
                    cl="1414416",
                    in_reply_to="filelvl_00000002",
                    line=10,
                    message="Mismatched line on file-level comment",
                )

    def test_standalone_draft_resolves_latest_ps_and_validates_ps(self) -> None:
        import gerrit_draft

        srv = FakeGerritServer(current_ps=3)
        stderr_buf = io.StringIO()
        with mock.patch.object(gerrit_draft, "gerrit_request", side_effect=srv.handle):
            res = gerrit_draft.save_draft(
                cl="1414416",
                ps=None,
                message="LGTM, thanks!",
                unresolved=False,
                err=stderr_buf,
            )
            self.assertEqual(res["ps"], 3)
            self.assertEqual(res["url"], "https://skia-review.googlesource.com/c/skia/+/1414416/3")

            with self.assertRaises(gerrit_draft.GerritDraftError):
                gerrit_draft.save_draft(cl="1414416", ps=99, message="Bad PS")

            old_res = gerrit_draft.save_draft(
                cl="1414416", ps=1, message="Note on PS1", err=stderr_buf
            )
            self.assertEqual(old_res["ps"], 1)
            self.assertIn("older PS1", stderr_buf.getvalue())

    def test_in_place_dedup_and_append_modes(self) -> None:
        import gerrit_draft

        srv = FakeGerritServer(current_ps=2)
        with mock.patch.object(gerrit_draft, "gerrit_request", side_effect=srv.handle):
            r1 = gerrit_draft.save_draft(
                cl="1414416",
                ps=2,
                path="modules/skparagraph/src/TextLine.cpp",
                line=1435,
                message="First inline draft",
                unresolved=True,
            )
            self.assertEqual(r1["action"], "created")
            first_id = r1["id"]

            r2 = gerrit_draft.save_draft(
                cl="1414416",
                ps=2,
                path="modules/skparagraph/src/TextLine.cpp",
                line=1435,
                message="Updated inline draft",
                unresolved=True,
            )
            self.assertEqual(r2["action"], "updated")
            self.assertEqual(r2["id"], first_id)
            self.assertEqual(
                len(srv.drafts_by_ps[2]["modules/skparagraph/src/TextLine.cpp"]), 1
            )

            r3 = gerrit_draft.save_draft(
                cl="1414416",
                ps=2,
                path="modules/skparagraph/src/TextLine.cpp",
                line=1435,
                message="Updated inline draft",
                unresolved=True,
            )
            self.assertEqual(r3["action"], "unchanged")

            # Append mode keeps existing patchset summary and appends canary note once
            a1 = gerrit_draft.save_draft(
                cl="1414416", ps=2, message="Initial review", unresolved=True
            )
            a2 = gerrit_draft.save_draft(
                cl="1414416", ps=2, message="Canary-G3 note", append=True
            )
            self.assertEqual(a2["action"], "updated")
            self.assertEqual(a2["id"], a1["id"])
            stored = srv.drafts_by_ps[2]["/PATCHSET_LEVEL"][0]
            self.assertEqual(stored["message"], "Initial review\n\nCanary-G3 note")
            self.assertTrue(stored["unresolved"])

            a3 = gerrit_draft.save_draft(
                cl="1414416", ps=2, message="Canary-G3 note", append=True
            )
            self.assertEqual(a3["action"], "unchanged")

            # Q1: append must match whole paragraph blocks, not substrings inside existing text
            srv.drafts_by_ps[2]["/PATCHSET_LEVEL"] = [
                {"id": "d_sub", "message": "Needs LGTM from owner", "unresolved": True}
            ]
            a4 = gerrit_draft.save_draft(
                cl="1414416", ps=2, message="LGTM", append=True
            )
            self.assertEqual(a4["action"], "updated")
            self.assertEqual(
                srv.drafts_by_ps[2]["/PATCHSET_LEVEL"][0]["message"],
                "Needs LGTM from owner\n\nLGTM",
            )

    def test_list_drafts_flags_stale_patchset_drafts(self) -> None:
        import gerrit_draft

        srv = FakeGerritServer(current_ps=2)
        srv.drafts_by_ps[1] = {
            "/PATCHSET_LEVEL": [{"id": "d_old", "message": "Stale PS1 draft", "unresolved": True}]
        }
        srv.drafts_by_ps[2] = {
            "TextLine.cpp": [
                {"id": "d_cur", "line": 10, "message": "Current PS2 draft", "unresolved": False}
            ]
        }
        with mock.patch.object(gerrit_draft, "gerrit_request", side_effect=srv.handle):
            listing = gerrit_draft.list_drafts(cl="1414416")
        self.assertEqual(len(listing["drafts"]), 2)
        self.assertTrue(listing["drafts"][0]["stale_ps"])
        self.assertFalse(listing["drafts"][1]["stale_ps"])

    def test_never_allows_publish_or_non_draft_writes(self) -> None:
        import gerrit_draft

        with self.assertRaises(gerrit_draft.GerritDraftError):
            gerrit_draft.gerrit_request(
                "skia-review.googlesource.com",
                "POST",
                "changes/1414416/revisions/2/review",
                {"message": "Accidental publish"},
            )
        with self.assertRaises(gerrit_draft.GerritDraftError):
            gerrit_draft.gerrit_request(
                "skia-review.googlesource.com",
                "DELETE",
                "changes/1414416/revisions/2/drafts/abc",
            )
        with self.assertRaises(gerrit_draft.GerritDraftError):
            gerrit_draft.gerrit_request(
                "skia-review.googlesource.com",
                "PUT",
                "changes/1414416/revisions/2/review",
                {"labels": {"Code-Review": 1}},
            )

    def test_gerrit_request_strips_xssi_prefix_and_retries_expired_luci_token(self) -> None:
        import urllib.error
        import gerrit_draft

        gerrit_draft._TOKEN_CACHE.clear()
        seen_auth: list[str] = []

        class _FakeResp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return self._body

        def fake_urlopen(req, timeout=20):
            auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
            seen_auth.append(auth)
            if auth == "Bearer stale-tok":
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"token expired")
                )
            return _FakeResp(b")]}'\n{\"_number\": 1396956}")

        creds = iter([
            mock.Mock(stdout="username=git\npassword=stale-tok\n"),
            mock.Mock(stdout="username=git\npassword=fresh-tok\n"),
        ])
        with (
            mock.patch.object(
                gerrit_draft.subprocess, "run", side_effect=lambda *a, **k: next(creds)
            ),
            mock.patch.object(gerrit_draft.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            out = gerrit_draft.gerrit_request(
                "skia", "GET", "changes/1396956"
            )
        self.assertEqual(out, {"_number": 1396956})
        self.assertEqual(seen_auth, ["Bearer stale-tok", "Bearer fresh-tok"])
        gerrit_draft._TOKEN_CACHE.clear()


class TestPromptsAndStopGuardEnforcement(unittest.TestCase):

    def _eval_guard(self, steps: list[dict], watcher_items: dict | None = None) -> str:
        import gerrit_draft_guard

        with tempfile.TemporaryDirectory() as tmp:
            tpath = os.path.join(tmp, "transcript.jsonl")
            with open(tpath, "w", encoding="utf-8") as f:
                for st in steps:
                    f.write(json.dumps(st, ensure_ascii=False) + "\n")
            wstate_path = os.path.join(tmp, "cl_watcher_state.json")
            with open(wstate_path, "w", encoding="utf-8") as f:
                json.dump({"items": watcher_items or {}}, f)
            with mock.patch.dict(os.environ, {"JETSKI_CL_WATCHER_STATE_FILE": wstate_path}):
                return gerrit_draft_guard.gerrit_draft_guard_reason(tpath)

    def _run_stop_guard(
        self, steps: list[dict], watcher_items: dict | None = None
    ) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = os.path.join(tmp, "transcript.jsonl")
            with open(tpath, "w", encoding="utf-8") as f:
                for st in steps:
                    f.write(json.dumps(st, ensure_ascii=False) + "\n")
            wstate_path = os.path.join(tmp, "cl_watcher_state.json")
            with open(wstate_path, "w", encoding="utf-8") as f:
                json.dump({"items": watcher_items or {}}, f)
            env = {
                **os.environ,
                "JETSKI_CL_WATCHER_STATE_FILE": wstate_path,
            }
            payload = {
                "conversationId": "test-conv-1414416",
                "transcriptPath": tpath,
                "artifactDirectoryPath": tmp,
                "fullyIdle": True,
            }
            proc1 = subprocess.run(
                [sys.executable, str(HOOKS_DIR / "stop_guard.py")],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            proc2 = subprocess.run(
                [sys.executable, str(HOOKS_DIR / "stop_guard.py")],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            return (
                json.loads(proc1.stdout.strip() or "{}"),
                json.loads(proc2.stdout.strip() or "{}"),
            )

    def test_incident_1414416_ps2_blocks_when_draft_not_saved(self) -> None:
        steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": (
                    "<USER_REQUEST>[CL-WATCHER] 💬 Новое обновление в gerrit:1414416 (PS2) от "
                    "dbebawy@gmail.com: Patch Set 2: Thank you, Julia! Проведи инкрементальный "
                    "триаж/анализ, подготовь черновик ответа и запиши вердикт через "
                    "triage_store.py save-review.</USER_REQUEST>"
                ),
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": (
                                "python3 ~/.gemini/config/scripts/triage_store.py save-review "
                                "--target 1414416 --patchset 2 --outcome COMPANION_FIX "
                                '--summary "Unused capture" --draft "Thanks for PS2!"'
                            )
                        },
                    }
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Command exited with code 0.\nSaved review verdict to /tmp/x.json",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Я сохранила вердикт для [Gerrit 1414416 PS2]"
                    "(https://skia-review.googlesource.com/c/skia/+/1414416/2) и подготовила "
                    "черновик ответа:\n\n```text\nThanks for PS2! One unused capture left.\n```"
                ),
                "tool_calls": [],
            },
        ]
        watcher_items = {
            "gerrit:1414416": {
                "is_self": False,
                "owner": "dbebawy@gmail.com",
                "url": "https://skia-review.googlesource.com/c/skia/+/1414416",
            }
        }
        out1, out2 = self._run_stop_guard(steps, watcher_items)
        self.assertEqual(out1.get("decision"), "continue")
        self.assertIn("[GERRIT DRAFT GUARD]", out1.get("reason", ""))
        self.assertIn("gerrit_draft.py save", out1.get("reason", ""))
        self.assertIn("1414416", out1.get("reason", ""))
        self.assertIn("Do NOT repeat", out1.get("reason", ""))
        # Once-per-turn guard state prevents an infinite stop_guard loop on the same user turn
        self.assertEqual(out2, {})

    def test_allows_once_gerrit_draft_saved(self) -> None:
        steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Проверь PS2 для Gerrit 1414416</USER_REQUEST>",
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": (
                                "python3 ~/.gemini/config/scripts/gerrit_draft.py save "
                                "--cl 1414416 --ps 2 --message-file /tmp/d.txt && "
                                "python3 ~/.gemini/config/scripts/triage_store.py save-review "
                                "--target 1414416 --patchset 2 --outcome NITS --summary 'Nit'"
                            )
                        },
                    }
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": (
                    "Command exited with code 0.\n"
                    "GERRIT_DRAFT_SAVED host=skia-review.googlesource.com cl=1414416 ps=2 "
                    "action=created id=abc_123 path=/PATCHSET_LEVEL line=- unresolved=false "
                    "url=https://skia-review.googlesource.com/c/skia/+/1414416/2"
                ),
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Сохранила [черновик на Gerrit 1414416 PS2]"
                    "(https://skia-review.googlesource.com/c/skia/+/1414416/2)."
                ),
                "tool_calls": [],
            },
        ]
        watcher_items = {"gerrit:1414416": {"is_self": False, "owner": "dbebawy@gmail.com"}}
        self.assertEqual(self._eval_guard(steps, watcher_items), "")

    def test_allows_self_owned_cl_and_pending_corpus_and_meta_discussion(self) -> None:
        # Self-owned CL must not trigger external-author review draft guard
        self_steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Check my CL 1396956</USER_REQUEST>",
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Для [Gerrit 1396956 PS8]"
                    "(https://skia-review.googlesource.com/c/skia/+/1396956/8) черновик ответа:\n"
                    "```text\nDone.\n```"
                ),
                "tool_calls": [],
            },
        ]
        out_self = self._eval_guard(
            self_steps,
            {"gerrit:1396956": {"is_self": True, "owner": "jlavrova@google.com"}},
        )
        self.assertEqual(out_self, "")

        # Pending corpus gate with no drafted reply must not force premature LGTM draft
        pending_steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Triage 1414416</USER_REQUEST>",
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": (
                                "python3 ~/.gemini/config/scripts/triage_store.py save-review "
                                "--target 1414416 --patchset 2 --outcome CLEAN "
                                "--corpus pending --summary 'Awaiting corpus'"
                            )
                        },
                    }
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Command exited with code 0.\nSaved review verdict",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Корпус для [Gerrit 1414416 PS2]"
                    "(https://skia-review.googlesource.com/c/skia/+/1414416/2) ещё бежит, "
                    "аппрув пока не сохраняю."
                ),
                "tool_calls": [],
            },
        ]
        out_pending = self._eval_guard(
            pending_steps,
            {"gerrit:1414416": {"is_self": False, "owner": "dbebawy@gmail.com"}},
        )
        self.assertEqual(out_pending, "")

    def test_chat_only_draft_reply_without_save_review_blocks(self) -> None:
        steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": (
                    "<USER_REQUEST>Ответь на комментарий в "
                    "https://skia-review.googlesource.com/c/skia/+/1414416/2</USER_REQUEST>"
                ),
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Подготовила черновик ответа для [Gerrit 1414416 PS2]"
                    "(https://skia-review.googlesource.com/c/skia/+/1414416/2):\n\n"
                    "```text\nThank you! Please drop the unused capture.\n```"
                ),
                "tool_calls": [],
            },
        ]
        reason = self._eval_guard(
            steps,
            {"gerrit:1414416": {"is_self": False, "owner": "dbebawy@gmail.com"}},
        )
        self.assertIn("[GERRIT DRAFT GUARD]", reason)

        # Signal A alone: save-review on external Gerrit CL without prose draft block still blocks
        sig_a_steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Triage gerrit:1414416 PS2</USER_REQUEST>",
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": (
                                "python3 ~/.gemini/config/scripts/triage_store.py save-review "
                                "--target 1414416 --patchset 2 --outcome NITS --summary 'Nit'"
                            )
                        },
                    }
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Command exited with code 0.\nSaved review verdict",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Готово для [Gerrit 1414416 PS2]"
                    "(https://skia-review.googlesource.com/c/skia/+/1414416/2)."
                ),
                "tool_calls": [],
            },
        ]
        self.assertIn(
            "[GERRIT DRAFT GUARD]",
            self._eval_guard(
                sig_a_steps,
                {"gerrit:1414416": {"is_self": False, "owner": "dbebawy@gmail.com"}},
            ),
        )

    def test_background_task_does_not_misalign_pending_draft_queue_and_gh_pr_ignored(self) -> None:
        # B1: background RUNNING command before synchronous REST PUT /drafts must not shift queue
        bg_steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": (
                    "<USER_REQUEST>Review "
                    "https://skia-review.googlesource.com/c/skia/+/1414416/2</USER_REQUEST>"
                ),
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"CommandLine": "python3 canary_advisor.py watch --cl 1414416"},
                    }
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Tool is running as a background task with task id: c1/task-9",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": (
                                "curl -X PUT https://skia-review.googlesource.com/a/"
                                "changes/1414416/revisions/2/drafts && "
                                "python3 ~/.gemini/config/scripts/triage_store.py save-review "
                                "--target 1414416 --patchset 2 --outcome NITS --summary 'Nit'"
                            )
                        },
                    }
                ],
            },
            {
                "step_index": 5,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Command exited with code 0.\nSaved review verdict",
            },
            {
                "step_index": 6,
                "type": "PLANNER_RESPONSE",
                "content": (
                    "Сохранила [черновик на Gerrit 1414416 PS2]"
                    "(https://skia-review.googlesource.com/c/skia/+/1414416/2)."
                ),
                "tool_calls": [],
            },
        ]
        self.assertEqual(
            self._eval_guard(
                bg_steps,
                {"gerrit:1414416": {"is_self": False, "owner": "dbebawy@gmail.com"}},
            ),
            "",
        )

        # GitHub PR save-review with omitted --kind must not falsely trigger Gerrit draft guard
        gh_steps = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "content": (
                    "<USER_REQUEST>Review "
                    "https://github.com/flutter/flutter/pull/194980</USER_REQUEST>"
                ),
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": (
                                "python3 ~/.gemini/config/scripts/triage_store.py save-review "
                                "--target 194980 --patchset 1 --outcome CLEAN --summary 'LGTM'"
                            )
                        },
                    }
                ],
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Command exited with code 0.\nSaved review verdict",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": "LGTM on [PR #194980](https://github.com/flutter/flutter/pull/194980).",
                "tool_calls": [],
            },
        ]
        self.assertEqual(self._eval_guard(gh_steps, {}), "")

    def test_cl_watcher_gerrit_prompts_cite_gerrit_draft_script(self) -> None:
        import audit_gate
        import cl_watcher

        p_gerrit = cl_watcher._build_review_triage_prompt(
            "gerrit:1414416",
            "https://skia-review.googlesource.com/c/skia/+/1414416",
            "Fix trailing spaces",
            2,
        )
        self.assertIn("gerrit_draft.py save", p_gerrit)
        p_gh = cl_watcher._build_review_triage_prompt(
            "gh_pr:flutter/flutter#194980",
            "https://github.com/flutter/flutter/pull/194980",
            "Fix BiDi",
            1,
        )
        self.assertNotIn("gerrit_draft.py", p_gh)

        g_item = {
            "kind": "gerrit",
            "target_id": "1414416",
            "url": "https://skia-review.googlesource.com/c/skia/+/1414416",
            "subject": "Fix trailing spaces",
            "patchset": 2,
        }
        p_t1 = audit_gate.build_audit_prompt(
            tier=1, item=g_item, prev_triage={"triaged_patchset": 1}, delta_info={}
        )
        p_t2 = audit_gate.build_audit_prompt(
            tier=2,
            item=g_item,
            prev_triage={"triaged_patchset": 1},
            delta_info={"delta_kind": "code_change"},
        )
        self.assertIn("gerrit_draft.py save", p_t1)
        self.assertIn("gerrit_draft.py save", p_t2)
        p_gh_t2 = audit_gate.build_audit_prompt(
            tier=2,
            item={**g_item, "kind": "github", "target_id": "194980"},
            prev_triage=None,
            delta_info={"delta_kind": "initial"},
        )
        self.assertNotIn("gerrit_draft.py", p_gh_t2)

    def test_triage_store_save_review_prints_gerrit_draft_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "triage_store.py"),
                    "save-review",
                    "--target",
                    "99999999",
                    "--kind",
                    "gerrit",
                    "--patchset",
                    "2",
                    "--outcome",
                    "COMPANION_FIX",
                    "--summary",
                    "One nit",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "JETSKI_TRIAGE_RESULTS_DIR": tmp},
                check=True,
            )
            self.assertIn("gerrit_draft.py save", proc.stdout)

            proc_pending = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "triage_store.py"),
                    "save-review",
                    "--target",
                    "99999999",
                    "--kind",
                    "gerrit",
                    "--patchset",
                    "2",
                    "--outcome",
                    "CLEAN",
                    "--corpus",
                    "pending",
                    "--summary",
                    "Awaiting corpus",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "JETSKI_TRIAGE_RESULTS_DIR": tmp},
                check=True,
            )
            self.assertNotIn("gerrit_draft.py save", proc_pending.stdout)


if __name__ == "__main__":
    unittest.main()
