#!/usr/bin/env python3
"""Regression tests for Python 3.9 compatibility across hooks, scripts, and skills."""

from __future__ import annotations

import ast
import asyncio
import glob
import importlib.util
import inspect
import os
import py_compile
import sys
import unittest


# Jetski run_workflow executes *_workflow.py entry scripts with PyCF_ALLOW_TOP_LEVEL_AWAIT
def _is_workflow_script(path: str) -> bool:
  return os.path.basename(path).endswith("_workflow.py")


def check_python39_source_compatibility(src: str, filename: str) -> list[str]:
  """Returns Python 3.9 incompatibility diagnostics for a source string."""
  tree = ast.parse(src, filename=filename)
  has_future_annotations = any(
      isinstance(node, ast.ImportFrom)
      and node.module == "__future__"
      and any(alias.name == "annotations" for alias in node.names)
      for node in tree.body
  )

  errors: list[str] = []
  annotation_roots: list[tuple[int, ast.AST]] = []
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      if node.returns:
        annotation_roots.append((node.lineno, node.returns))
      all_args = (
          node.args.posonlyargs
          + node.args.args
          + node.args.kwonlyargs
          + ([node.args.vararg] if node.args.vararg else [])
          + ([node.args.kwarg] if node.args.kwarg else [])
      )
      for arg in all_args:
        if arg.annotation:
          annotation_roots.append((arg.lineno, arg.annotation))
    elif isinstance(node, ast.AnnAssign) and node.annotation:
      annotation_roots.append((node.lineno, node.annotation))

  if not has_future_annotations:
    for lineno, ann in annotation_roots:
      for sub in ast.walk(ann):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
          errors.append(
              f"{filename}:{lineno}: PEP 604 `X | Y` annotation requires "
              "`from __future__ import annotations` on Python 3.9"
          )
          break

  for node in ast.walk(tree):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("isinstance", "issubclass")
        and len(node.args) >= 2
    ):
      for sub in ast.walk(node.args[1]):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
          errors.append(
              f"{filename}:{node.lineno}: runtime `{node.func.id}(..., X | Y)` "
              "fails on Python 3.9 even with `__future__.annotations`"
          )
          break
    if hasattr(ast, "Match") and isinstance(node, ast.Match):
      errors.append(f"{filename}:{node.lineno}: `match/case` requires Python 3.10+")
    if hasattr(ast, "TryStar") and isinstance(node, ast.TryStar):
      errors.append(f"{filename}:{node.lineno}: `except*` requires Python 3.11+")
    if hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
      errors.append(f"{filename}:{node.lineno}: `type X = ...` requires Python 3.12+")
  return errors


class TestPython39Compat(unittest.TestCase):

  def _all_python_files(self) -> list[str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    patterns = (
        ("hooks", "*.py"),
        ("scripts", "*.py"),
        ("skills", "*", "scripts", "*.py"),
        ("tests", "*.py"),
    )
    files = [
        f
        for pat in patterns
        for f in sorted(glob.glob(os.path.join(root, *pat)))
    ]
    pre_commit = os.path.join(root, "githooks", "pre-commit")
    if os.path.isfile(pre_commit):
      files.append(pre_commit)
    return files

  # Xcode /usr/bin/python3 on macOS is Python 3.9.6, which rejects PEP 604 X | Y
  # annotations at def time unless `from __future__ import annotations` is active
  def test_python39_compatibility_and_imports(self) -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    all_errors: list[str] = []
    for f in self._all_python_files():
      rel = os.path.relpath(f, root)
      with open(f, encoding="utf-8") as fp:
        src = fp.read()
      if _is_workflow_script(f):
        compile(src, f, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
      else:
        py_compile.compile(f, doraise=True)
      all_errors.extend(check_python39_source_compatibility(src, rel))
    self.assertEqual(all_errors, [], "\n".join(all_errors))

    # Also verify every non-test module imports cleanly at top level
    import_patterns = (
        ("hooks", "*.py"),
        ("scripts", "*.py"),
        ("skills", "*", "scripts", "*.py"),
    )
    for pat in import_patterns:
      for f in sorted(glob.glob(os.path.join(root, *pat))):
        rel = os.path.relpath(f, root)
        mod_name = "_check_import_" + rel.replace(os.sep, "_").replace(".", "_")
        spec = importlib.util.spec_from_file_location(mod_name, f)
        self.assertIsNotNone(spec, f"Could not create spec for {rel}")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        script_dir = os.path.dirname(f)
        added_path = script_dir not in sys.path
        if added_path:
          sys.path.insert(0, script_dir)
        try:
          if _is_workflow_script(f):
            with open(f, encoding="utf-8") as fp:
              wf_src = fp.read()
            wf_code = compile(wf_src, f, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            mod.__file__ = f
            res = eval(wf_code, mod.__dict__)
            if inspect.iscoroutine(res):
              asyncio.run(res)
          else:
            spec.loader.exec_module(mod)
        finally:
          sys.modules.pop(mod_name, None)
          if added_path and script_dir in sys.path:
            sys.path.remove(script_dir)

  def test_python39_ast_checker_catches_unguarded_pep604(self) -> None:
    bad_src = "def f(x: list[str] | None = None) -> str | None:\n  return None\n"
    self.assertEqual(len(check_python39_source_compatibility(bad_src, "bad.py")), 2)

    good_src = "from __future__ import annotations\n" + bad_src
    self.assertEqual(check_python39_source_compatibility(good_src, "good.py"), [])

    bad_isinstance = "isinstance(1, int | str)\n"
    self.assertEqual(
        len(check_python39_source_compatibility(bad_isinstance, "bad_rt.py")), 1
    )

  def test_workflow_top_level_await_gated_to_workflow_scripts(self) -> None:
    wf_src = "async def main() -> int | None:\n  return None\nawait main()\n"
    self.assertTrue(_is_workflow_script("skills/debate/scripts/debate_workflow.py"))
    self.assertFalse(_is_workflow_script("skills/debate/scripts/debate_ls_runner.py"))
    self.assertEqual(len(check_python39_source_compatibility(wf_src, "x_workflow.py")), 1)
    with self.assertRaises(SyntaxError):
      compile(wf_src, "regular.py", "exec")
    compile(wf_src, "x_workflow.py", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


if __name__ == "__main__":
  unittest.main()
