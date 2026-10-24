"""Run every test suite in order and print a pass/fail table.

    python tests/run_all.py            # all 17 suites (live model calls: 2-10 minutes depending on the gateway cache, under $0.10)
    python tests/run_all.py --offline  # only the suites that make no model calls (about 1 minute, $0)
"""

import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SUITES = [  # (file, makes live model calls?, what it covers)
    ("test_step1.py", True, "config, .env loading, live model ping"),
    ("test_step2.py", False, "schemas, SQLite, seed data"),
    ("test_step3.py", False, "constraint checker, swap solver, catering math, weather"),
    ("test_step4.py", True, "token metering, Cost-Saver, scale projections"),
    ("test_step5.py", False, "3 safety layers, red-team self-test"),
    ("test_step6.py", True, "new-event setup agent, CSV/template import"),
    ("test_step7.py", True, "Triage -> Solver hand-off, malfunction repair"),
    ("test_step8.py", True, "full 3-agent pipeline, commit, .ics, injection blocked"),
    ("test_step9.py", False, "API endpoints and pages"),
    ("test_step10.py", True, "resolve run/commit API, live attendee page"),
    ("test_step11.py", True, "usage/safety API, live A/B benchmark, evidence"),
    ("test_phase1.py", False, "planning data: deadlines, workstreams, budget, flags"),
    ("test_phase2.py", True, "tab assistants, confirm-before-save"),
    ("test_phase3.py", True, "Inbox Watcher, pasted email, Daily Brief"),
    ("test_phase4.py", False, "workspace UI shell, direct edits"),
    ("test_phase5.py", False, "Gmail MCP connector, resolve approval, usage/safety data"),
    ("test_final.py", False, "submission readiness: evidence, docs, dependencies, secrets"),
]


def main() -> int:
    offline = "--offline" in sys.argv
    chosen = [s for s in SUITES if not (offline and s[1])]
    print(f"Running {len(chosen)} suite(s){' (offline only)' if offline else ''}\n")
    failed = []
    for name, live, what in chosen:
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(TESTS / name)], capture_output=True, text=True)
        secs = time.perf_counter() - t0
        status = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"  {status}  {name:<17} {secs:6.1f}s  {'live' if live else '    '}  {what}")
        if proc.returncode:
            failed.append(name)
            print("\n".join("        " + line for line in (proc.stdout + proc.stderr).strip().splitlines()[-15:]))
    print(f"\n{len(chosen) - len(failed)}/{len(chosen)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
