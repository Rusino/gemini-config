#!/usr/bin/env python3
"""Write an interleaved-rounds plan file for remote_runner.sh.

Plan lines are `<out> <point> <txt|ext> <reps> <filter|ALL>` or `BARRIER`. Round r runs every
point once per kind, odd rounds in the given order and even rounds reversed, so slow drift
of the machine (thermal, background load) hits both ends of the chain equally. A BARRIER
closes every round, so round r+1 never starts before round r is done. With --anchor, the
anchor point runs at the start of every round and after every --anchor-every points, to
show drift between rounds.

Raw outputs are named `<prefix>_<point>_r<round>_<kind>` (what eval_rounds.py reads) and
`<prefix>_<anchor>_r<round>a<slot>_<kind>` for anchor runs.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_TXT_FILTER = "SkParagraphFixture/(JustifyLayout|ShortLayout|TextBigO/64|SimpleBuilder)$"
DEFAULT_EXT_FILTER = ("ExtFixture/(JustifyMixed_Relayout|Many100x621_CacheHit|"
                      "EllipsisMaxLines3_Relayout|RelayoutManyWidths_Latin)$")


def plan_lines(prefix: str, points: list[str], rounds: int, reps: int, kinds: list[str],
               filters: dict[str, str], anchor: str | None = None,
               anchor_every: int = 0) -> list[str]:
  """Return the plan lines for points over rounds; see the module docstring for the layout.

  Raises ValueError for tokens with whitespace (the runner splits lines on it), unknown kinds
  and non-positive rounds or reps.
  """
  if rounds < 1 or reps < 1 or anchor_every < 0:
    raise ValueError("rounds and reps must be >= 1, anchor_every >= 0")
  for k in kinds:
    if k not in ("txt", "ext"):
      raise ValueError(f"unknown kind {k!r}")
  for tok in [prefix, *points, *(filters[k] for k in kinds), anchor or "x"]:
    if not tok or any(c.isspace() for c in tok):
      raise ValueError(f"empty token or whitespace in {tok!r}")
  if len(set(points)) != len(points):
    raise ValueError("duplicate points")

  def runs(out_stem: str, point: str) -> list[str]:
    """Return one plan line per kind for point, outputs named <out_stem>_<kind>."""
    return [f"{out_stem}_{k} {point} {k} {reps} {filters[k]}" for k in kinds]

  lines: list[str] = []
  for r in range(1, rounds + 1):
    order = points if r % 2 else points[::-1]
    slot = 0
    for i, pt in enumerate(order):
      if anchor and (i == 0 or (anchor_every and i % anchor_every == 0)):
        lines += runs(f"{prefix}_{anchor}_r{r}a{slot}", anchor)
        slot += 1
      lines += runs(f"{prefix}_{pt}_r{r}", pt)
    if r < rounds:
      lines.append("BARRIER")
  return lines


def main(argv: list[str] | None = None) -> int:
  """Parse arguments and write the plan to --out or stdout."""
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("points", nargs="+", help="staged point names, chain order")
  ap.add_argument("--prefix", required=True, help="run id, prefix of every raw output")
  ap.add_argument("--rounds", type=int, default=3)
  ap.add_argument("--reps", type=int, default=10, help="--benchmark_repetitions per run")
  ap.add_argument("--kinds", default="txt,ext", help="comma list of txt, ext")
  ap.add_argument("--txt-filter", default=DEFAULT_TXT_FILTER, help="regex or ALL")
  ap.add_argument("--ext-filter", default=DEFAULT_EXT_FILTER, help="regex or ALL")
  ap.add_argument("--anchor", help="point re-measured at round start and every N points")
  ap.add_argument("--anchor-every", type=int, default=0)
  ap.add_argument("--out", help="plan file path; stdout if omitted")
  a = ap.parse_args(argv)
  try:
    lines = plan_lines(a.prefix, a.points, a.rounds, a.reps, a.kinds.split(","),
                       {"txt": a.txt_filter, "ext": a.ext_filter}, a.anchor, a.anchor_every)
  except ValueError as e:
    ap.error(str(e))
  text = "\n".join(lines) + "\n"
  if a.out:
    with open(a.out, "w") as f:
      f.write(text)
  else:
    sys.stdout.write(text)
  return 0


if __name__ == "__main__":
  sys.exit(main())
