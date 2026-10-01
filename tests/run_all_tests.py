#!/usr/bin/env python3
"""Run all tests for gemini config hooks and scripts."""

import os
import sys
import unittest

if __name__ == "__main__":
  test_dir = os.path.dirname(os.path.abspath(__file__))
  suite = unittest.defaultTestLoader.discover(start_dir=test_dir, pattern="test_*.py")
  runner = unittest.TextTestRunner(verbosity=2)
  result = runner.run(suite)
  sys.exit(0 if result.wasSuccessful() else 1)
