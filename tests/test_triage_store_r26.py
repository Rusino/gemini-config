#!/usr/bin/env python3
import pathlib
import sys
import tempfile
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import triage_store


class TriageStoreR26Tests(unittest.TestCase):
    def test_save_review_verdict_persists_structured_deep_audit_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            triage_store.save_review_verdict(
                target_id="1409658",
                kind="gerrit",
                patchset=5,
                outcome="CLEAN",
                summary="PS5 fixes cluster index offset; tests pass",
                draft="LGTM pending corpus gate.",
                revision="598b140257",
                delta_kind="code_change",
                core_hunks=[
                    "modules/skparagraph/src/ParagraphImpl.cpp:1548-1555:globalClusterIndex slice",
                ],
                open_thread_verdicts=[
                    "William|PS2 patchset|already addressed in PS3|intro thread only",
                ],
                audit_state="done",
                files=["modules/skparagraph/src/ParagraphImpl.cpp"],
                results_dir=tmp,
            )
            saved = triage_store.load_triage_result("gerrit", "1409658", results_dir=tmp)
            self.assertIsNotNone(saved)
            self.assertEqual(saved["delta_kind"], "code_change")
            self.assertEqual(saved["audit_state"], "done")
            self.assertEqual(len(saved["core_hunks"]), 1)
            self.assertEqual(
                saved["core_hunks"][0],
                {
                    "file": "modules/skparagraph/src/ParagraphImpl.cpp",
                    "lines": "1548-1555",
                    "note": "globalClusterIndex slice",
                },
            )
            self.assertEqual(len(saved["open_thread_verdicts"]), 1)
            self.assertEqual(
                saved["open_thread_verdicts"][0]["verdict"],
                "already addressed in PS3",
            )
            # Mandatory corpus gate keeps LGTM amber until compare.md passes
            self.assertEqual(saved["corpus_status"], "pending")
            self.assertEqual(saved["verdict_tone"], "corpus_blocked")

    def test_tier0_rebase_and_commit_msg_carry_forward_and_staleness_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            triage_store.save_review_verdict(
                target_id="1412383",
                kind="gerrit",
                patchset=2,
                outcome="CLEAN",
                summary="Cluster owner removal verified",
                revision="268361ea75",
                corpus="passed",
                files=["modules/skparagraph/src/Run.cpp"],
                results_dir=tmp,
            )
            # Rebase onto a new base carries outcome forward and resets corpus_status to pending
            triage_store.record_tier0_delta_verdict(
                target_id="1412383",
                kind="gerrit",
                patchset=4,
                revision="dced28a241",
                delta_kind="rebase_only",
                summary="PS4 = rebase of PS2 (no code change); no new audit needed",
                draft="",
                files=["modules/skparagraph/src/Run.cpp"],
                results_dir=tmp,
            )
            rebased = triage_store.load_triage_result("gerrit", "1412383", results_dir=tmp)
            self.assertEqual(rebased["triaged_patchset"], 4)
            self.assertEqual(rebased["delta_kind"], "rebase_only")
            self.assertEqual(rebased["review_outcome"], "CLEAN")
            self.assertEqual(rebased["corpus_status"], "pending")

            # Commit-message-only update on an already corpus-passed PS preserves passed corpus
            triage_store.save_review_verdict(
                target_id="1412383",
                kind="gerrit",
                patchset=4,
                outcome="CLEAN",
                summary="Cluster owner removal verified",
                revision="dced28a241",
                corpus="passed",
                files=["modules/skparagraph/src/Run.cpp"],
                results_dir=tmp,
            )
            triage_store.record_tier0_delta_verdict(
                target_id="1412383",
                kind="gerrit",
                patchset=5,
                revision="ffff111122",
                delta_kind="commit_msg_only",
                summary="PS5 updates commit message only (code identical to PS4)",
                draft="",
                files=["modules/skparagraph/src/Run.cpp"],
                results_dir=tmp,
            )
            msg_only = triage_store.load_triage_result("gerrit", "1412383", results_dir=tmp)
            self.assertEqual(msg_only["triaged_patchset"], 5)
            self.assertEqual(msg_only["corpus_status"], "passed")
            self.assertEqual(msg_only["corpus_tested_patchset"], 5)

            triage_store.save_review_verdict(
                target_id="1412383",
                kind="gerrit",
                patchset=5,
                outcome="PUSHBACK",
                summary="Corpus failed on PS5",
                revision="ffff111122",
                corpus="failed",
                files=["modules/skparagraph/src/Run.cpp"],
                results_dir=tmp,
            )
            triage_store.record_tier0_delta_verdict(
                target_id="1412383",
                kind="gerrit",
                patchset=6,
                revision="eeee222233",
                delta_kind="commit_msg_only",
                summary="PS6 updates commit message only (code identical to PS5)",
                draft="",
                files=["modules/skparagraph/src/Run.cpp"],
                results_dir=tmp,
            )
            msg_failed = triage_store.load_triage_result("gerrit", "1412383", results_dir=tmp)
            self.assertEqual(msg_failed["corpus_status"], "failed")

    def test_record_audit_status_preserves_prior_summary_while_running_or_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            triage_store.save_review_verdict(
                target_id="1409697",
                kind="gerrit",
                patchset=5,
                outcome="CLEAN",
                summary="PS5 verified",
                revision="11d7fba59d",
                results_dir=tmp,
            )
            triage_store.record_audit_status(
                target_id="1409697",
                kind="gerrit",
                patchset=6,
                revision="6e8dd008c2",
                audit_state="blocked_auth",
                audit_error="LOAS expired (run gcert)",
                results_dir=tmp,
            )
            items = [{
                "item_key": "gerrit:1409697",
                "target_id": "1409697",
                "patchset": 6,
                "revision": "6e8dd008c2",
                "files": [],
            }]
            triage_store.overlay_triage_on_items(items, results_dir=tmp, ingest_tmp=False)
            tr = items[0]["triage"]
            self.assertTrue(tr["is_stale"])
            self.assertEqual(tr["audit_state"], "blocked_auth")
            self.assertEqual(tr["audit_target_patchset"], 6)
            self.assertIn("gcert", tr["audit_error"])


if __name__ == "__main__":
    unittest.main()
