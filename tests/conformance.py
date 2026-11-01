#!/usr/bin/env python3
"""Conformance test: C VM (c/fsrun) vs. reference Python VM (flowscript/runner.py).

For every examples/*.fs and tests/programs/*.fs the program is compiled to .fsb
(in a temp dir) and run with its scenario (same basename, .scenario; none =
empty scenario) by both runners.  The traces must be byte-identical.

    python3 tests/conformance.py [--no-asan] [-v] [PATTERN...]

By default the ASan/UBSan build (c/fsrun-asan) is checked as well.
Exit status is non-zero on any mismatch.
"""

import argparse
import difflib
import glob
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDIR = os.path.join(ROOT, "c")


def build(targets):
    r = subprocess.run(["make", "-s", "-C", CDIR] + targets)
    if r.returncode != 0:
        sys.exit("conformance: building %s failed" % " ".join(targets))


def programs(patterns):
    files = sorted(glob.glob(os.path.join(ROOT, "examples", "*.fs")))
    files += sorted(glob.glob(os.path.join(ROOT, "tests", "programs", "*.fs")))
    if patterns:
        files = [f for f in files if any(p in os.path.basename(f) for p in patterns)]
    return files


def run(cmd):
    r = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-asan", action="store_true", help="skip the sanitizer build")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("patterns", nargs="*")
    args = ap.parse_args()

    runners = [os.path.join(CDIR, "fsrun")]
    if not args.no_asan:
        runners.append(os.path.join(CDIR, "fsrun-asan"))
    build([os.path.basename(r) for r in runners])

    passed, failed = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for src in programs(args.patterns):
            base = os.path.splitext(src)[0]
            name = os.path.relpath(src, ROOT)
            fsb = os.path.join(tmp, os.path.basename(base) + ".fsb")
            rc, out, err = run([sys.executable, "-m", "flowscript", "compile", src, "-o", fsb])
            if rc != 0:
                failed.append(name)
                print("FAIL %s: compile error\n%s" % (name, err))
                continue
            scen = base + ".scenario"
            extra = [scen] if os.path.exists(scen) else []
            prc, pout, perr = run([sys.executable, "-m", "flowscript", "run", fsb] + extra)
            ok = True
            for runner in runners:
                crc, cout, cerr = run([runner, fsb] + extra)
                tag = os.path.basename(runner)
                if cout != pout or (prc == 0) != (crc == 0):
                    ok = False
                    print("FAIL %s [%s] (python rc=%d, c rc=%d)" % (name, tag, prc, crc))
                    sys.stdout.writelines(difflib.unified_diff(
                        pout.splitlines(True), cout.splitlines(True), "python", tag, n=2))
                    if cerr:
                        print(cerr)
            if prc != 0:
                print("note %s: python runner exited with %d: %s" % (name, prc, perr.strip().splitlines()[-1:]))
            if ok:
                passed.append(name)
                if args.verbose:
                    print("ok   %s (%d trace lines)" % (name, pout.count("\n")))
            else:
                failed.append(name)

    print("conformance: %d passed, %d failed (%d programs, runners: %s)"
          % (len(passed), len(failed), len(passed) + len(failed),
             ", ".join(os.path.basename(r) for r in runners)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
