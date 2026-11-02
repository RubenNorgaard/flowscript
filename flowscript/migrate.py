"""Rewrite a FlowScript 0.1 program (with ``any_state:``) for 0.2, where only states handle events.

    new_source, notes = flowscript.migrate(old_source)

The rewrite keeps what the program does wherever 0.2 can say the same thing:

* ``on boot`` moves into the initial state;
* every other ``any_state`` handler is copied into each state that has no handler of its own
  for that event (in 0.1 a state's own handler replaced the shared one);
* a program without states gets a single ``state main:`` holding the handlers;
* comments directly in ``any_state`` go above the initial state.

``notes`` lists what may behave differently and is worth a look, chiefly timers: in 0.2 every
timer belongs to the state that started it and stops when that state is left, while timers
started by ``any_state`` handlers used to keep running across states.  The result is in the
canonical format; it is not compiled (run ``flowscript.compile`` on it).  A program without
``any_state`` comes back reformatted, with no notes.
"""

import copy

from .parser import parse
from .printer import to_source

__all__ = ["migrate"]


def _starts(stmts, acc):
    for s in stmts or []:
        if s["type"] == "start":
            acc.append(s)
        for key in ("body", "else_body"):
            _starts(s.get(key), acc)
        for br in s.get("branches") or []:
            _starts(br["body"], acc)
    return acc


def migrate(source):
    """FlowScript 0.1 source -> (0.2 source, [notes])."""
    ast = parse(source, legacy=True)
    items = ast["items"]
    anys = [i for i in items if i["type"] == "any_state"]
    if not anys:
        return to_source(ast), []
    if len(anys) > 1:
        raise ValueError("more than one any_state block (0.1 allowed only one)")
    a = anys[0]
    pos = items.index(a)
    states = [i for i in items if i["type"] == "state"]
    handlers = [h for h in a["handlers"] if h["type"] == "on"]
    loose = [h for h in a["handlers"] if h["type"] == "comment"]
    notes = []
    for h in handlers:
        for st in _starts(h["body"], []):
            notes.append("line %d: timer '%s' was started in any_state's 'on %s' and kept running across "
                         "states; it now stops when its state is left. To keep a periodic timer running, "
                         "start it with the same period in every state's 'on enter'."
                         % (st.get("line", 0), st["timer"], h["event"]))
    if not states:
        main = {"id": a["id"], "type": "state", "line": a["line"], "name": "main", "vars": [],
                "handlers": a["handlers"]}
        if a.get("note"):
            main["note"] = a["note"]
        items[pos] = main
        return to_source(ast), notes
    initial = next((s for s in states if s.get("initial")), states[0])
    del items[pos]
    for h in handlers:
        if h["event"] == "boot":
            initial["handlers"].insert(0, copy.deepcopy(h))
            continue
        got = [s["name"] for s in states
               if not any(x["type"] == "on" and x["event"] == h["event"] for x in s["handlers"])]
        for s in states:
            if s["name"] in got:
                s["handlers"].append(copy.deepcopy(h))
        if len(got) > 1:
            notes.append("line %d: any_state's 'on %s' is now in %s; consider moving its body into a "
                         "func the states call" % (h.get("line", 0), h["event"], ", ".join(got)))
    if loose or a.get("note"):
        text = "\n".join(([a["note"]] if a.get("note") else []) + [c["text"] for c in loose])
        initial["note"] = text + ("\n" + initial["note"] if initial.get("note") else "")
    return to_source(ast), notes
