#!/usr/bin/env python3
"""ssh/rsync side of the Cloudtop build + remote measure workflow.

Every ssh and rsync goes through one ControlMaster that the user opens once by hand: a new
non-mux ssh costs a security-key touch, so this script never opens one (ControlMaster=no,
BatchMode=yes) and exits with EXIT_NO_MASTER and the exact open command instead.

Subcommands:
  check                      verify the master is alive
  has <point>                exit 0 if <remote>/bin/<point>/.complete exists, else 1
  push <stage_dir> <point>   atomic push: bin/<point>.tmp, sha256 check, fonts, mv, .complete
  enqueue <plan>...          upload remote_runner.sh and plans, add them to lanes.txt, start
                             the runner detached on the remote side unless it already runs
  status                     runner log tail and the list of complete points
  fetch <local_dir>          rsync <remote>/raw/ to <local_dir>

Host, remote dir and ControlPath come from --host/--remote-dir/--control-path or PERF_HOST,
PERF_REMOTE_DIR, PERF_CM. Exit codes: 0 ok, 1 negative answer or remote failure, 2 bad
arguments, 3 no live ControlMaster.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(SCRIPT_DIR, "remote_runner.sh")
EXIT_FAIL = 1
EXIT_NO_MASTER = 3


def default_control_path() -> str:
  """Return the shared ControlPath, /tmp/<user>_perf_cm, used by every perf script."""
  return f"/tmp/{getpass.getuser()}_perf_cm"


def ssh_opts(control_path: str) -> list[str]:
  """Return ssh options that only reuse the existing master and never prompt."""
  return ["-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", f"ControlPath={control_path}"]


def ssh_argv(host: str, control_path: str, remote_cmd: str) -> list[str]:
  """Return the argv that runs remote_cmd on host through the master."""
  return ["ssh", *ssh_opts(control_path), host, remote_cmd]


def rsync_argv(control_path: str, srcs: list[str], dst: str,
               extra: list[str] | None = None) -> list[str]:
  """Return the rsync argv for srcs -> dst whose transport is the same mux-only ssh."""
  rsh = " ".join(["ssh", *ssh_opts(control_path)])
  return ["rsync", "-a", "-e", rsh, *(extra or []), *srcs, dst]


def open_master_command(host: str, control_path: str) -> str:
  """Return the one-time command the user runs to open the master (one key touch)."""
  return f"ssh -fNM -o ControlPersist=10h -o ControlPath={control_path} {host}"


def check_master(host: str, control_path: str, run=subprocess.run) -> bool:
  """Return True if the master answers `ssh -O check`; otherwise print how to open it.

  `-O check` talks only to the local control socket, so it never touches the network or the
  security key, even when the master is dead.
  """
  r = run(["ssh", "-O", "check", "-o", f"ControlPath={control_path}", host],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
  if r.returncode == 0:
    return True
  sys.stderr.write(
      f"perf_remote: no live ControlMaster at {control_path} for {host}.\n"
      f"Ask the user to run once (one security-key touch):\n"
      f"  {open_master_command(host, control_path)}\n")
  return False


def push_remote_script(remote_dir: str, point: str) -> str:
  """Return the remote shell that publishes bin/<point>.tmp as bin/<point>.

  Order is the contract the runner relies on: checksums verified, fixture fonts linked, then
  the directory is renamed into place and .complete is touched last, so the runner never
  starts a point with a partial binary set.
  """
  r, p = shlex.quote(remote_dir), shlex.quote(point)
  return (
      f"set -e; cd {r}; R=$(pwd); T=bin/{p}.tmp; cd $T; sha256sum -c --quiet SHA256SUMS; "
      "fx=$(sed -n 's/^fixtures_path=//p' META); "
      # Binaries look for fonts at the absolute out-dir path baked in at build time
      "if [ ! -d $R/txt_fonts ] && [ -d assets ]; then cp -r assets $R/txt_fonts; fi; "
      "for f in assets/*; do [ -e \"$f\" ] || continue; "
      "cmp -s \"$f\" $R/txt_fonts/\"${f#assets/}\" || echo WARN_FONT_DIFFERS $f >&2; done; "
      'if [ -n "$fx" ]; then mkdir -p "$(dirname "$fx")"; ln -sfn $R/txt_fonts "$fx"; fi; '
      f"cd $R; rm -rf bin/{p}; mv $T bin/{p}; touch bin/{p}/.complete"
  )


def enqueue_remote_script(remote_dir: str, plan_names: list[str]) -> str:
  """Return the remote shell that adds plans to lanes.txt and starts the runner if idle.

  The runner rereads lanes.txt every pass, so a running runner picks up new lanes; setsid
  and nohup detach it on the remote side only, the local ssh returns at once.
  """
  r = shlex.quote(remote_dir)
  adds = "; ".join(
      f"tr -s ' ' '\\n' < lanes.txt | grep -qxF {shlex.quote('plans/' + n)} || "
      f"echo {shlex.quote('plans/' + n)} >> lanes.txt" for n in plan_names)
  # Probe the runner's own .runner.lock: pgrep -f would match this very shell, whose
  # command line contains the runner's name in the start branch, and never start it
  return (
      f"set -e; cd {r}; mkdir -p logs raw bin plans; touch lanes.txt; {adds}; "
      "if ! flock -n .runner.lock true; then echo RUNNING; else "
      "(setsid nohup ./remote_runner.sh >> logs/runner.out 2>&1 < /dev/null &); "
      "echo STARTED; fi"
  )


def _run(argv: list[str]) -> int:
  """Run argv with inherited stdio and return its exit code."""
  return subprocess.run(argv, check=False).returncode


def main(argv: list[str] | None = None) -> int:
  """Parse arguments, require a live master and dispatch the subcommand."""
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--host", default=os.environ.get("PERF_HOST"))
  ap.add_argument("--remote-dir", default=os.environ.get("PERF_REMOTE_DIR", "perf_weekly"),
                  help="relative to the remote $HOME unless absolute")
  ap.add_argument("--control-path", default=os.environ.get("PERF_CM", default_control_path()))
  sub = ap.add_subparsers(dest="cmd", required=True)
  sub.add_parser("check")
  sub.add_parser("status")
  p = sub.add_parser("has")
  p.add_argument("point")
  p = sub.add_parser("push")
  p.add_argument("stage_dir")
  p.add_argument("point")
  p = sub.add_parser("enqueue")
  p.add_argument("plans", nargs="+")
  p = sub.add_parser("fetch")
  p.add_argument("local_dir")
  a = ap.parse_args(argv)
  if not a.host:
    ap.error("--host or PERF_HOST is required")
  host, cp, rd = a.host, a.control_path, a.remote_dir
  if not check_master(host, cp):
    return EXIT_NO_MASTER
  if a.cmd == "check":
    print(f"OK master {cp} -> {host}")
    return 0
  q = shlex.quote
  if a.cmd == "has":
    return _run(ssh_argv(host, cp, f"test -f {q(rd)}/bin/{q(a.point)}/.complete"))
  if a.cmd == "status":
    return _run(ssh_argv(host, cp, f"cd {q(rd)} && tail -n 20 logs/runner.log; "
                         "cat lanes.txt; ls bin/*/.complete 2>/dev/null | wc -l"))
  if a.cmd == "fetch":
    os.makedirs(a.local_dir, exist_ok=True)
    return _run(rsync_argv(cp, [f"{host}:{rd}/raw/"], a.local_dir.rstrip("/") + "/"))
  if a.cmd == "push":
    if not os.path.isfile(os.path.join(a.stage_dir, "SHA256SUMS")):
      sys.stderr.write(f"perf_remote: {a.stage_dir} has no SHA256SUMS\n")
      return EXIT_FAIL
    rc = _run(ssh_argv(host, cp, f"mkdir -p {q(rd)}/bin && rm -rf {q(rd)}/bin/{q(a.point)}.tmp"))
    rc = rc or _run(rsync_argv(cp, [a.stage_dir.rstrip("/") + "/"],
                               f"{host}:{rd}/bin/{a.point}.tmp/",
                               ["--exclude", ".complete", "--exclude", ".pushed"]))
    return rc or _run(ssh_argv(host, cp, push_remote_script(rd, a.point)))
  # enqueue
  names = [os.path.basename(x) for x in a.plans]
  rc = _run(ssh_argv(host, cp, f"mkdir -p {q(rd)}/plans"))
  rc = rc or _run(rsync_argv(cp, [RUNNER], f"{host}:{rd}/remote_runner.sh"))
  rc = rc or _run(rsync_argv(cp, a.plans, f"{host}:{rd}/plans/"))
  return rc or _run(ssh_argv(host, cp, enqueue_remote_script(rd, names)))


if __name__ == "__main__":
  sys.exit(main())
