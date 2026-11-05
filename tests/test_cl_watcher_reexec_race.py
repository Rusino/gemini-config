#!/usr/bin/env python3
"""Contract tests for cl_watcher.py hot-reload debounce and execv SHA-256 race protection."""

import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class ClWatcherReexecRaceContractTest(unittest.TestCase):

  def test_initial_script_sha256_captured_at_startup(self):
    self.assertTrue(hasattr(w, "_INITIAL_SCRIPT_SHA256"))
    self.assertEqual(len(w._INITIAL_SCRIPT_SHA256), 64)

  def test_transient_sub3s_edit_debounced_when_min_age_set(self):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as tf:
      tf.write("x = 1\n")
      path = tf.name
    try:
      now = time.time()
      base_mtime = now - 60.0
      os.utime(path, (base_mtime, base_mtime))
      with open(path, "rb") as f:
        base_sha = hashlib.sha256(f.read()).hexdigest()

      # Write mutated valid Python with mtime only 0.5s ago (< 3.0s debounce window)
      with open(path, "w", encoding="utf-8") as f:
        f.write("x = 2\n")
      recent_mtime = now - 0.5
      os.utime(path, (recent_mtime, recent_mtime))
      self.assertFalse(
          w.script_changed_on_disk(
              path, base_mtime, initial_sha256=base_sha, min_age_sec=3.0
          )
      )

      # Once the edit has been stable on disk for >= 3.0s, reload triggers
      settled_mtime = now - 5.0
      os.utime(path, (settled_mtime, settled_mtime))
      self.assertTrue(
          w.script_changed_on_disk(
              path, base_mtime, initial_sha256=base_sha, min_age_sec=3.0
          )
      )
    finally:
      os.unlink(path)

  def test_restored_identical_content_does_not_trigger_reload(self):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as tf:
      tf.write("x = 1\n")
      path = tf.name
    try:
      now = time.time()
      base_mtime = now - 60.0
      os.utime(path, (base_mtime, base_mtime))
      with open(path, "rb") as f:
        base_sha = hashlib.sha256(f.read()).hexdigest()

      # Touch mtime to 10s ago without changing content (e.g. cp restore after test)
      restored_mtime = now - 10.0
      os.utime(path, (restored_mtime, restored_mtime))
      self.assertFalse(
          w.script_changed_on_disk(
              path, base_mtime, initial_sha256=base_sha, min_age_sec=3.0
          )
      )
    finally:
      os.unlink(path)

  def test_execv_race_mtime_matches_restored_file_but_sha256_differs(self):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as tf:
      tf.write("x = 'restored_good_code'\n")
      path = tf.name
    try:
      now = time.time()
      settled_mtime = now - 10.0
      os.utime(path, (settled_mtime, settled_mtime))
      mutated_sha = hashlib.sha256(b"x = 'transient_mutated_code'\n").hexdigest()

      # Even when initial_mtime equals current disk mtime (sampled after cp restore),
      # the mismatch against the in-memory bytecode's pinned SHA-256 triggers reload
      self.assertTrue(
          w.script_changed_on_disk(
              path,
              settled_mtime,
              initial_sha256=mutated_sha,
              min_age_sec=3.0,
          )
      )
    finally:
      os.unlink(path)

  def test_reexec_self_exports_exec_sha256_env(self):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as tf:
      tf.write("x = 42\n")
      path = tf.name
    try:
      expected_sha = hashlib.sha256(b"x = 42\n").hexdigest()
      fake_server = mock.MagicMock()
      with mock.patch.dict(w.os.environ, {}, clear=False):
        with mock.patch.object(w.os, "execv") as mock_execv:
          w.reexec_self(fake_server, script_path=path)
          self.assertEqual(
              w.os.environ.get("_CL_WATCHER_EXEC_SHA256"), expected_sha
          )
          mock_execv.assert_called_once()
    finally:
      os.unlink(path)


if __name__ == "__main__":
  unittest.main()
