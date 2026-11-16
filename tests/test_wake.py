#!/usr/bin/env python3
"""Waking from sleep: which ssh masters get closed, and the clocks behind it.

    python3 tests/test_wake.py

No ssh runs: revive_masters takes its runner as an argument.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hosts  # noqa: E402

failures = total = 0


def check(label, passed, detail=""):
    global failures, total
    total += 1
    failures += not passed
    print(("ok   " if passed else "FAIL ") + label
          + ("" if passed or not detail else f"\n     {detail}"))


def main():
    calls = []
    slow = []

    def fake(argv, **kw):
        calls.append(argv)
        sock = next(a.split("=", 1)[1] for a in argv if a.startswith("ControlPath="))
        if argv[-1] == "true":
            if "dead" in sock:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if "gone" in sock:
                return subprocess.CompletedProcess(argv, 255)
            if "forced" in sock:            # the far side answered, with 1
                return subprocess.CompletedProcess(argv, 1)
            if "slow" in sock:
                slow.append(1)
                if len(slow) == 1:
                    raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0)

    socks = [Path("/x/live@a:22"), Path("/x/dead@mini:2222"), Path("/x/gone@b:22"),
             Path("/x/forced@git:22"), Path("/x/slow@c:22")]
    start = time.monotonic()
    dropped = hosts.revive_masters(socks, timeout=1, run=fake)
    check("a master that hangs or fails is closed; one that answers is kept",
          dropped == ["dead@mini:2222", "gone@b:22"], repr(dropped))
    exits = [c for c in calls if "-O" in c]
    check("closing is ssh -O exit on that socket",
          len(exits) == 2 and all(c[c.index("-O") + 1] == "exit" for c in exits))
    check("the check never reaches a real host",
          all(c[-1] == "true" and c[-2] == "conn-mux-check.invalid"
              or c[-1] == "conn-mux-check.invalid" for c in calls), repr(calls))
    check("a far side that answers non-zero is alive; a slow one gets a retry",
          "forced@git:22" not in dropped and "slow@c:22" not in dropped
          and slow == [1, 1])
    check("checks run at once, not one timeout each",
          time.monotonic() - start < 3)
    check("no sockets, nothing to do", hosts.revive_masters([], run=fake) == [])

    a = hosts.slept_seconds()
    time.sleep(0.2)
    b = hosts.slept_seconds()
    check("time asleep does not grow while awake", abs(b - a) < 0.1, f"{a} {b}")
    check("addresses read", isinstance(hosts.local_addresses(), frozenset))
    check("network ready now (route and jump host up)",
          hosts.network_ready(wait=10))
    real = hosts.subprocess.run
    def no_route(argv, **kw):
        if argv[:3] == ["ip", "route", "show"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real(argv, **kw)
    hosts.subprocess.run = no_route
    try:
        check("no route: not ready, and gives up",
              hosts.network_ready(wait=0.5, sleep=lambda s: time.sleep(0.1)) is False)
    finally:
        hosts.subprocess.run = real
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
