#!/usr/bin/env python3
"""VM code size on Cortex-M4 for every combination of the feature switches.

    python3 c/codesize.py [--host-text]

Compiles flowscript.c with clang for thumbv7em (-Os, soft float, one section per
function/object) and counts what a device link with -Wl,--gc-sections keeps: every
section reachable through relocations from the public fs_* API.  fs_format,
fs_parse_float and fs_parse_time are host conveniences (traces, debugging); they are
left out of the roots unless --host-text is given.  The C library's soft-float and
libm routines are not included (they are listed as undefined symbols).

Needs clang, llvm-objdump and the macOS SDK headers (xcrun), as on a Mac with Xcode.
"""
import argparse
import itertools
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SWITCHES = ["FS_NO_FLOAT", "FS_NO_TIME", "FS_NO_STR", "FS_NO_TEXT", "FS_NO_RECORD"]
# host conveniences a device need not link: text helpers, and snapshots unless it uses them
HOST_TEXT = {"fs_format", "fs_parse_float", "fs_parse_time", "fs_format_log"}
SNAPSHOTS = {"fs_snapshot", "fs_restore", "fs_record_keyframe"}


def tool(name):
    return subprocess.check_output(["xcrun", "--find", name], text=True).strip()


def build(flags, obj):
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    subprocess.check_call([tool("clang"), "--target=thumbv7em-none-eabi", "-mcpu=cortex-m4",
                           "-mfloat-abi=soft", "-ffreestanding", "-isystem", sdk + "/usr/include",
                           "-std=c99", "-Os", "-ffp-contract=off", "-ffunction-sections",
                           "-fdata-sections"] + ["-D" + f for f in flags] +
                          ["-c", os.path.join(HERE, "flowscript.c"), "-o", obj])


def kept(obj, host_text, snapshots=False):
    od = tool("llvm-objdump")
    sizes = {}                                      # section name -> size
    for line in subprocess.check_output([od, "-h", obj], text=True).splitlines():
        m = re.match(r"\s*\d+\s+(\.\S+)\s+([0-9a-f]{8})\s", line)
        if m:
            sizes[m.group(1)] = int(m.group(2), 16)
    sym_sec = {}                                    # symbol -> section name
    roots = set()
    for line in subprocess.check_output([od, "-t", obj], text=True).splitlines():
        m = re.match(r"[0-9a-f]{8} (.)(.{6}) (\S+)\t[0-9a-f]{8} (\S+)$", line)
        if not m:
            continue
        bind, kind, sec, name = m.group(1), m.group(2), m.group(3), m.group(4)
        sym_sec.setdefault(name, sec)
        if bind == "g" and "F" in kind and (host_text or name not in HOST_TEXT) and \
                (snapshots or name not in SNAPSHOTS):
            roots.add(sec)
    refs, cur = {}, None
    for line in subprocess.check_output([od, "-r", obj], text=True).splitlines():
        m = re.match(r"RELOCATION RECORDS FOR \[(\S+)\]:", line)
        if m:
            cur = m.group(1)
            refs.setdefault(cur, set())
            continue
        p = line.split()
        if cur and len(p) == 3 and re.match(r"[0-9a-f]{8}$", p[0]):
            target = p[2]
            if target in sym_sec:
                refs[cur].add(sym_sec[target])
            elif target in sizes:
                refs[cur].add(target)
    seen, todo = set(), list(roots)
    while todo:
        sec = todo.pop()
        if sec in seen:
            continue
        seen.add(sec)
        todo.extend(refs.get(sec, ()))
    # merged string literals (.rodata.str*) are referenced through local labels: count them whole
    seen.update(n for n in sizes if n.startswith(".rodata.str"))
    return sum(sizes.get(sec, 0) for sec in seen if sec.startswith((".text", ".rodata")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-text", action="store_true",
                    help="count fs_format/fs_parse_*/fs_format_log as used by the host")
    ap.add_argument("--snapshots", action="store_true",
                    help="count fs_snapshot/fs_restore/fs_record_keyframe as used by the host")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        obj = os.path.join(tmp, "vm.o")
        rows = []
        for n in range(len(SWITCHES) + 1):
            for combo in itertools.combinations(SWITCHES, n):
                build(combo, obj)
                rows.append((combo, kept(obj, args.host_text, args.snapshots)))
        for combo, size in rows:
            print("%6d  %s" % (size, " ".join(combo) or "(all features)"))


if __name__ == "__main__":
    sys.exit(main())
