"""Command line front end:  python3 -m flowscript <command> ...

  compile  FILE.fs [-o OUT.fsb] [-g] [--size]
                                        compile to bytecode (-g: include debug section)
  size     FILE.fs|FILE.fsb [-g] [--json]
                                        where the image bytes go, RAM needed by the C VM,
                                        smallest FS_* build settings, optimization hints
  disasm   FILE.fs|FILE.fsb             print a disassembly
  fmt      FILE.fs [-w]                 print (or rewrite) in canonical format
  ast      FILE.fs                      print the visual model (AST JSON)
  run      FILE.fs|FILE.fsb [SCENARIO] [--record OUT.rec]
                                        run with stubbed externs, print trace (and record it)
  replay   FILE.fs|FILE.fsb REC|- [--svg OUT.svg]
                                        replay a recording (- : stream it from stdin, e.g. a
                                        serial port) and print the trace; --svg redraws the
                                        state diagram, current state highlighted, on changes
  mermaid  FILE.fs                      print a Mermaid flowchart
  migrate  FILE.fs [-w]                 rewrite a 0.1 program (any_state) for 0.2
  debug    FILE.fs [--port N]           start the 3-view web debugger
"""

import argparse
import json
import os
import sys

from .lexer import FlowError
from .parser import parse
from .printer import to_source
from .compiler import compile_source, compile_ast
from .bytecode import Program, disassemble_text


def load_program(path):
    if path.endswith(".fsb"):
        with open(path, "rb") as f:
            return Program.from_bytes(f.read())
    with open(path) as f:
        _ast, prog, _dbg = compile_source(f.read())
    return prog


def main(argv=None):
    ap = argparse.ArgumentParser(prog="flowscript", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("compile")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.add_argument("-g", "--debug", action="store_true")
    p.add_argument("--size", action="store_true", help="print the size report")
    p = sub.add_parser("size")
    p.add_argument("file")
    p.add_argument("-g", "--debug", action="store_true", help="include the debug section")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("disasm")
    p.add_argument("file")
    p = sub.add_parser("fmt")
    p.add_argument("file")
    p.add_argument("-w", "--write", action="store_true")
    p = sub.add_parser("ast")
    p.add_argument("file")
    p = sub.add_parser("run")
    p.add_argument("file")
    p.add_argument("scenario", nargs="?")
    p.add_argument("--record", help="write the recording (inputs) to this file")
    p = sub.add_parser("replay")
    p.add_argument("file")
    p.add_argument("recording")
    p.add_argument("--svg", help="keep this SVG file showing the current state")
    p = sub.add_parser("migrate")
    p.add_argument("file")
    p.add_argument("-w", "--write", action="store_true")
    p = sub.add_parser("mermaid")
    p.add_argument("file")
    p = sub.add_parser("debug")
    p.add_argument("file", nargs="?")
    p.add_argument("--port", type=int, default=8642)
    p.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "compile":
            with open(args.file) as f:
                _ast, prog, dbg = compile_source(f.read())
            for w in dbg.warnings:
                print("%s: line %d: warning: %s" % (args.file, w["line"], w["msg"]), file=sys.stderr)
            out = args.output or os.path.splitext(args.file)[0] + ".fsb"
            data = prog.to_bytes(debug=args.debug)
            with open(out, "wb") as f:
                f.write(data)
            print("%s: %d bytes (%d bytes code)" % (out, len(data), len(prog.code)))
            if args.size:
                from .sizes import size_report, format_report
                print()
                sys.stdout.write(format_report(size_report(prog, args.debug), os.path.basename(args.file)))
        elif args.cmd == "size":
            from .sizes import size_report, format_report
            prog = load_program(args.file)
            image = open(args.file, "rb").read() if args.file.endswith(".fsb") else None
            rep = size_report(prog, args.debug, image)
            if args.json:
                print(json.dumps(rep, indent=1))
            else:
                sys.stdout.write(format_report(rep, os.path.basename(args.file)))
        elif args.cmd == "disasm":
            sys.stdout.write(disassemble_text(load_program(args.file)))
        elif args.cmd == "fmt":
            with open(args.file) as f:
                src = to_source(parse(f.read()))
            if args.write:
                with open(args.file, "w") as f:
                    f.write(src)
            else:
                sys.stdout.write(src)
        elif args.cmd == "ast":
            with open(args.file) as f:
                print(json.dumps(parse(f.read()), indent=1))
        elif args.cmd == "run":
            from .runner import run_scenario
            prog = load_program(args.file)
            text = ""
            if args.scenario:
                with open(args.scenario) as f:
                    text = f.read()
            sink = None
            if args.record:
                rec = open(args.record, "wb")
                sink = rec.write
            run_scenario(prog, text, print, record=sink)
            if args.record:
                rec.close()
        elif args.cmd == "replay":
            from .runner import replay_recording
            from .svg import to_svg
            prog = load_program(args.file)
            source = None
            if not args.file.endswith(".fsb"):
                with open(args.file) as f:
                    source = f.read()
            if args.svg and source is None:
                raise FlowError("--svg needs the source (FILE.fs): bytecode has no flowchart")

            def redraw(state):
                tmp = args.svg + ".tmp"
                with open(tmp, "w") as f:
                    f.write(to_svg(source, "states", state=state))
                os.replace(tmp, args.svg)            # a viewer never reads a half-written file

            def chunks(fd):
                while True:
                    b = os.read(fd, 4096)
                    if not b:
                        return
                    yield b

            out = lambda line: print(line, flush=True)
            on_state = redraw if args.svg else None
            if args.recording == "-":
                replay_recording(prog, chunks(sys.stdin.fileno()), out, on_state)
            else:
                with open(args.recording, "rb") as f:
                    replay_recording(prog, f.read(), out, on_state)
        elif args.cmd == "migrate":
            from .migrate import migrate
            with open(args.file) as f:
                out, notes = migrate(f.read())
            for n in notes:
                print("%s: %s" % (args.file, n), file=sys.stderr)
            if args.write:
                with open(args.file, "w") as f:
                    f.write(out)
            else:
                sys.stdout.write(out)
        elif args.cmd == "mermaid":
            from .mermaid import to_mermaid
            with open(args.file) as f:
                sys.stdout.write(to_mermaid(parse(f.read())))
        elif args.cmd == "debug":
            from .debugger.server import serve
            serve(args.file, args.port, open_browser=not args.no_browser)
        else:
            ap.print_help()
            return 1
    except FlowError as e:
        print("%s: %s" % (getattr(args, "file", ""), e), file=sys.stderr)
        return 1
    return 0
