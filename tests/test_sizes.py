"""Size report: section accounting, stack analysis, fs_vm layout vs the real C compiler."""
import glob, os, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flowscript import compile_source, Program
from flowscript.sizes import size_report, fs_vm_size, DEFAULTS, SWITCHES
from flowscript.runner import run_scenario

ROOT = os.path.join(os.path.dirname(__file__), "..")
PROGRAMS = sorted(glob.glob(os.path.join(ROOT, "examples", "*.fs")) +
                  glob.glob(os.path.join(ROOT, "tests", "programs", "*.fs")))
CC = shutil.which("cc")


def compiled(fn):
    with open(fn) as f:
        return compile_source(f.read())[1]


class Report(unittest.TestCase):
    def test_sections_add_up(self):
        for fn in PROGRAMS:
            prog = compiled(fn)
            for debug in (False, True):
                with self.subTest(fn=os.path.basename(fn), debug=debug):
                    image = prog.to_bytes(debug=debug)
                    rep = size_report(prog, debug)
                    self.assertEqual(rep["image_bytes"], len(image))
                    self.assertEqual(sum(s["bytes"] for s in rep["sections"]), len(image))
                    self.assertEqual(sum(r["bytes"] for r in rep["routines"]), len(prog.code))
                    # reporting on the .fsb file itself gives the same numbers
                    rep2 = size_report(Program.from_bytes(image), image=image)
                    self.assertEqual(rep2["sections"], rep["sections"])

    def test_gps_minimum(self):
        rep = size_report(compiled(os.path.join(ROOT, "examples", "gps_tracker.fs")))
        mins = {r["macro"]: r["minimum"] for r in rep["ram"]["rows"]}
        self.assertEqual(mins["FS_STACK_SIZE"], 4)
        self.assertEqual(mins["FS_MAX_FRAMES"], 2)
        self.assertEqual(mins["FS_MAX_EXTERNS"], 6)
        self.assertIsNone(mins["FS_QUEUE_SIZE"])
        self.assertIn("-DFS_STACK_SIZE=4", rep["ram"]["cflags"])
        self.assertEqual(rep["ram"]["default_bytes"], 6080)
        # the GPS tracker uses no floats, times or run-time strings: all three switches apply
        self.assertTrue(rep["ram"]["cflags"].startswith("-DFS_NO_FLOAT -DFS_NO_TIME -DFS_NO_STR "))
        self.assertEqual(rep["ram"]["minimum_bytes"], 384)
        self.assertEqual(rep["ram"]["minimum_value_bytes"], 5)

    def test_recursion_has_no_bound(self):
        rep = size_report(compile_source("func f(int n) -> int:\n  return f(n)\nany_state:\n  on boot:\n    f(1)\n")[1])
        mins = {r["macro"]: r["minimum"] for r in rep["ram"]["rows"]}
        self.assertIsNone(mins["FS_STACK_SIZE"])
        self.assertNotIn("FS_STACK_SIZE", rep["ram"]["cflags"])

    def test_unused_hints(self):
        rep = size_report(compile_source("extern a() -> int\nextern b() -> int\nevent e\nint g = 1\n"
                                         "any_state:\n  on boot:\n    a()\n")[1])
        text = "\n".join(rep["hints"])
        self.assertIn("extern 'b' is never called", text)
        self.assertIn("event 'e' is neither handled nor emitted", text)
        self.assertIn("global 'g' is never read", text)


@unittest.skipUnless(CC, "no C compiler")
class AgainstC(unittest.TestCase):
    def test_fs_vm_layout(self):
        """sizeof(fs_vm) computed in Python equals the compiler's (host pointer size)."""
        ptr = 8 if sys.maxsize > 2 ** 32 else 4
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "sz.c")
            with open(src, "w") as f:
                f.write('#include "flowscript.h"\n#include <stdio.h>\n'
                        'int main(void){printf("%u\\n",(unsigned)sizeof(fs_vm));return 0;}\n')
            small = dict(DEFAULTS, FS_STACK_SIZE=3, FS_MAX_EXTERNS=5, FS_MAX_TIMERS=1,
                         FS_MAX_EVENT_ARGS=1, FS_QUEUE_SIZE=3, FS_MAX_FRAMES=2)
            # every combination of the feature switches, on the default and a small config
            combos = [{k: 1 for k, bit in zip(SWITCHES, (1, 2, 4)) if m & bit} for m in range(8)]
            for cfg in [dict(base, **sw) for base in (DEFAULTS, small) for sw in combos]:
                flags = ["-D%s=%d" % kv for kv in cfg.items()]
                exe = os.path.join(tmp, "sz")
                subprocess.check_call([CC, "-I", os.path.join(ROOT, "c")] + flags + [src, "-o", exe])
                self.assertEqual(int(subprocess.check_output([exe])), fs_vm_size(cfg, ptr))

    def test_minimum_settings_run_every_program(self):
        """A C VM built with a program's minimum FS_* settings gives the reference trace."""
        with tempfile.TemporaryDirectory() as tmp:
            for fn in PROGRAMS:
                prog = compiled(fn)
                flags = size_report(prog)["ram"]["cflags"].split()
                exe = os.path.join(tmp, "fsrun_min")
                subprocess.check_call([CC, "-O1", "-I", os.path.join(ROOT, "c")] + flags +
                                      [os.path.join(ROOT, "c", "flowscript.c"),
                                       os.path.join(ROOT, "c", "fsrun.c"), "-o", exe])
                fsb = os.path.join(tmp, "p.fsb")
                with open(fsb, "wb") as f:
                    f.write(prog.to_bytes())
                sc = fn[:-3] + ".scenario"
                text = open(sc).read() if os.path.exists(sc) else ""
                out = subprocess.run([exe, fsb] + ([sc] if text else []), capture_output=True,
                                     text=True).stdout.splitlines()
                ref = []
                run_scenario(prog, text, ref.append)
                with self.subTest(fn=os.path.basename(fn), flags=flags):
                    self.assertEqual(out, ref)


if __name__ == "__main__":
    unittest.main()
