#!/usr/bin/env python3
"""Tests for amnesia-precommit/scripts/mutation_check.py on a toy repo."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "skills", "amnesia-precommit", "scripts", "mutation_check.py",
))

PROD = "def style(xml_style, scanned):\n    return xml_style or scanned\n"
LIVE_TEST = "from prod import style\nassert style(None, 'Normal') == 'Normal'\n"
# The incident shape: iterates an empty collection, so no mutation can make it fail
VACUOUS_TEST = "from prod import style\nfor fam in []:\n    assert style(None, fam) == fam\n"


class TestMutationCheck(unittest.TestCase):

  def make_repo(self, tmp, test_body):
    with open(os.path.join(tmp, "prod.py"), "w") as f:
      f.write(PROD)
    with open(os.path.join(tmp, "test_prod.py"), "w") as f:
      f.write(test_body)
    subprocess.run(["git", "init", "-q", tmp], check=True)
    subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
    subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)

  def run_gate(self, tmp, replace="return 'BoldItalic'", build="true"):
    return subprocess.run(
        [sys.executable, SCRIPT, "--repo", tmp, "--file", "prod.py",
         "--find", "return xml_style or scanned", "--replace", replace,
         "--build", build, "--test", f"{sys.executable} test_prod.py",
         "--test-file", "test_prod.py", "--test-name", "test_prod"],
        capture_output=True, text=True, check=False,
    )

  def attestation(self, tmp):
    rel = subprocess.run(["git", "-C", tmp, "rev-parse", "--git-path",
                          "jetski/mutation_attestations/test_prod.json"],
                         capture_output=True, text=True, check=True).stdout.strip()
    with open(os.path.join(tmp, rel)) as f:
      return json.load(f)

  def assert_clean(self, tmp):
    status = subprocess.run(["git", "-C", tmp, "status", "--porcelain", "--", "prod.py"],
                            capture_output=True, text=True, check=True).stdout
    self.assertEqual(status, "")

  def test_live_test_passes_gate_and_tree_is_restored(self):
    with tempfile.TemporaryDirectory() as tmp:
      self.make_repo(tmp, LIVE_TEST)
      res = self.run_gate(tmp)
      self.assertEqual(res.returncode, 0, res.stdout)
      self.assertIn("PASS", res.stdout)
      self.assert_clean(tmp)
      att = self.attestation(tmp)
      self.assertEqual(att["result"], "PASS")
      self.assertIn("+    return 'BoldItalic'", att["mutation_diff"])
      self.assertEqual(att["prod_sha256_before"], att["prod_sha256_after"])
      self.assertNotEqual(att["mutated_rc"], 0)
      self.assertEqual(att["restored_rc"], 0)

  def test_vacuous_test_fails_gate(self):
    with tempfile.TemporaryDirectory() as tmp:
      self.make_repo(tmp, VACUOUS_TEST)
      res = self.run_gate(tmp)
      self.assertEqual(res.returncode, 1, res.stdout)
      self.assertIn("passed with the mutation", res.stdout)
      self.assert_clean(tmp)
      self.assertEqual(self.attestation(tmp)["result"], "FAIL")

  def test_mutated_build_failure_still_restores_source(self):
    with tempfile.TemporaryDirectory() as tmp:
      self.make_repo(tmp, LIVE_TEST)
      res = self.run_gate(tmp, build=f"{sys.executable} -m py_compile prod.py",
                          replace="return (")
      self.assertEqual(res.returncode, 1, res.stdout)
      self.assertIn("does not compile", res.stdout)
      self.assert_clean(tmp)


if __name__ == "__main__":
  unittest.main()
