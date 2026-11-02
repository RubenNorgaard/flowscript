"""FlowScript: an event-driven scripting language whose programs are flowcharts.

Compile a script, run it (from source or bytecode), and draw it:

    import flowscript

    program = flowscript.compile(source)            # flowscript.CompileError on errors
    machine = flowscript.Machine(program, externs={"log": print})
    machine.start()
    machine.send("tick")
    svg = flowscript.to_svg(program, state=machine.state)

See https://github.com/RubenNorgaard/flowscript for the language (SPEC.md) and the C VM.
"""

from .api import compile, load, Machine, CompileError, Diagnostic, Fault
from .svg import to_svg
from .migrate import migrate
from .bytecode import Program, disassemble, disassemble_text
from .lexer import FlowError
from .parser import parse, parse_expression
from .printer import to_source, expr_to_str
from .compiler import compile_source, compile_ast
from .mermaid import to_mermaid
from .sizes import size_report, format_report
from .vm import VM

__all__ = [
    # the host API
    "compile", "load", "Machine", "to_svg", "Program", "CompileError", "Diagnostic", "Fault",
    # tools
    "migrate", "to_source", "to_mermaid", "size_report", "format_report", "disassemble", "disassemble_text",
    # lower level
    "FlowError", "parse", "parse_expression", "expr_to_str", "compile_source", "compile_ast", "VM",
]
__version__ = "0.3.0"
