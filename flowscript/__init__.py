"""FlowScript: an event-driven flow-control scripting language with a bytecode VM."""

from .lexer import FlowError
from .parser import parse, parse_expression
from .printer import to_source, expr_to_str
from .compiler import compile_source, compile_ast
from .bytecode import Program, disassemble, disassemble_text
from .vm import VM

__all__ = ["FlowError", "parse", "parse_expression", "to_source", "expr_to_str",
           "compile_source", "compile_ast", "Program", "disassemble", "disassemble_text", "VM"]
__version__ = "0.1.0"
