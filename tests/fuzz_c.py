#!/usr/bin/env python3
"""Robustness fuzzer for the C VM.

Compiles every examples/*.fs and tests/programs/*.fs, then repeatedly corrupts
the .fsb image (random byte flips anywhere, flips restricted to the code
section, header count tweaks, truncation, garbage appended) and runs the
ASan/UBSan build of fsrun (c/fsrun-asan) with the program's scenario.

Rejections (fsrun exit 1) and faults are fine, and so is disagreeing with the
Python VM: the only failure is a crash or a sanitizer report.  Runs that hit
the timeout are counted separately; the VM bounds every run to idle (fault 9,
SPEC 7), so a timeout points at a bug worth investigating.

    python3 tests/fuzz_c.py [-n ITERATIONS] [--seed N]
"""

import argparse
import glob
import os
import random
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDIR = os.path.join(ROOT, "c")
FSRUN = os.path.join(CDIR, "fsrun-asan")

ENV = dict(os.environ,
           ASAN_OPTIONS="exitcode=99:abort_on_error=0:detect_leaks=0",
           UBSAN_OPTIONS="halt_on_error=1:exitcode=98:print_stacktrace=1")


def mutate(rng, data):
    d = bytearray(data)
    code_size = struct.unpack_from("<I", d, 28)[0]
    code_off = len(d) - code_size          # compiled without -g: code is last
    kind = rng.randrange(6)
    if kind == 0:                           # flips anywhere
        for _ in range(rng.randint(1, 8)):
            d[rng.randrange(len(d))] = rng.randrange(256)
    elif kind in (1, 2) and code_size:      # flips in code (passes validation)
        for _ in range(rng.randint(1, 6)):
            i = code_off + rng.randrange(code_size)
            d[i] = rng.randrange(256) if rng.random() < 0.7 else d[i] ^ (1 << rng.randrange(8))
    elif kind == 3:                         # header/table counts and indices
        for _ in range(rng.randint(1, 3)):
            i = rng.randrange(min(len(d), 32 + 200))
            d[i] = rng.choice([0, 1, 0x7F, 0x80, 0xFF, rng.randrange(256)])
    elif kind == 4:                         # truncate
        d = d[:rng.randrange(len(d))]
    else:                                   # append garbage / bump code_size
        d += bytes(rng.randrange(256) for _ in range(rng.randint(1, 16)))
        if rng.random() < 0.5:
            struct.pack_into("<I", d, 28, code_size + rng.randint(1, 16))
    return bytes(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--iterations", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    if subprocess.run(["make", "-s", "-C", CDIR, "fsrun-asan"]).returncode != 0:
        sys.exit("fuzz: build failed")
    rng = random.Random(args.seed)
    stats = {"ok": 0, "rejected": 0, "timeout": 0, "crash": 0}
    with tempfile.TemporaryDirectory() as tmp:
        corpus = []
        srcs = sorted(glob.glob(os.path.join(ROOT, "examples", "*.fs")) +
                      glob.glob(os.path.join(ROOT, "tests", "programs", "*.fs")))
        for src in srcs:
            base = os.path.splitext(src)[0]
            fsb = os.path.join(tmp, os.path.basename(base) + ".fsb")
            subprocess.run([sys.executable, "-m", "flowscript", "compile", src, "-o", fsb],
                           cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            with open(fsb, "rb") as f:
                scen = base + ".scenario"
                corpus.append((f.read(), [scen] if os.path.exists(scen) else []))
        bad = os.path.join(tmp, "fuzz.fsb")
        for it in range(args.iterations):
            data, scen = rng.choice(corpus)
            m = mutate(rng, data)
            with open(bad, "wb") as f:
                f.write(m)
            try:
                r = subprocess.run([FSRUN, bad] + scen, env=ENV, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                stats["timeout"] += 1
                continue
            err = r.stderr.decode("utf-8", "replace")
            if r.returncode == 0:
                stats["ok"] += 1
            elif r.returncode == 1 and "Sanitizer" not in err and "runtime error" not in err:
                stats["rejected"] += 1
            else:
                stats["crash"] += 1
                keep = os.path.join(ROOT, "tests", "fuzz_crash_%d.fsb" % it)
                with open(keep, "wb") as f:
                    f.write(m)
                print("CRASH (rc=%d) saved to %s\n%s" % (r.returncode, keep, err[-2000:]))
    print("fuzz: %d runs: %d ran, %d rejected by fs_load, %d timeouts, %d crashes"
          % (args.iterations, stats["ok"], stats["rejected"], stats["timeout"], stats["crash"]))
    return 1 if stats["crash"] else 0


if __name__ == "__main__":
    sys.exit(main())
