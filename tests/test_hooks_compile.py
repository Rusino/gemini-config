#!/usr/bin/env python3
import glob
import os
import py_compile
import unittest

class TestHooksCompile(unittest.TestCase):
  def test_hooks_compile(self):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in glob.glob(os.path.join(root, "hooks", "*.py")):
      try:
        py_compile.compile(f, doraise=True)
      except py_compile.PyCompileError as e:
        self.fail(f"Hook {os.path.basename(f)} failed to compile: {e}")

if __name__ == "__main__":
  unittest.main()
