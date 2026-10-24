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

  # Direct hook/CLI invocations fail when write_file drops the +x mode bit
  def test_python_scripts_executable(self):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    patterns = (
        ("hooks", "*.py"),
        ("scripts", "*.py"),
        ("skills", "*", "scripts", "*.py"),
        ("tests", "*.py"),
    )
    non_exec = [
        os.path.relpath(f, root)
        for pat in patterns
        for f in sorted(glob.glob(os.path.join(root, *pat)))
        if not os.access(f, os.X_OK)
    ]
    self.assertEqual(non_exec, [], f"Python scripts missing +x bit: {non_exec}")

if __name__ == "__main__":
  unittest.main()
