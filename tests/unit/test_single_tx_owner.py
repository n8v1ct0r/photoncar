"""Single TX owner: only sources/obd.py may send, and only it and socketcan.py open a bus.

Walks the syntax tree of every module under src/photoncar, and of every script except the
bench ECU emulator (it plays the car's ECU on the bench, and is not Photoncar). No src
module may import scripts/bench.

Kinds of finding:

- ``send``: any reference to a send method (called or not), ``getattr`` with a name that
  spells send, the bare string of a send method name (``methodcaller("send")``), any
  reference to a python-can helper that sends on its own (cyclic send tasks, BCM sockets),
  and a write to a socket descriptor (``os.write(bus.fileno(), ...)``, ``os.fdopen`` of one,
  also through a name the descriptor was assigned to first).
- ``bus``: any reference to a python-can ``*Bus`` name, called or not (``B = can.Bus``,
  ``functools.partial(can.Bus, ...)``, ``map(can.Bus, ...)``), ``getattr`` on the can
  module, a dynamic import of can.
- ``raw_can``: AF_CAN, PF_CAN and the CAN protocol constants, and ``socket.socket(29, ...)``.
- ``request_bus``: any reference to the request bus opener, the session key, or the bus
  attributes of a poller or session (``_bus``, ``_buses``) outside ``self``: the only routes
  to can1 besides ``open_live_can_session``.
- ``can_tool``: a command that sends on a bus or changes a link, in any string, f-string,
  argv list, or process call (``subprocess``, ``os.system``, ``os.exec*``): a can-utils
  sender (``cansend``, ``cangen``, ...), or ``ip link`` with any command but a read (``show``,
  ``list``, ``lst``), including ``ip`` abbreviations (``ip l s``) and an argv list split in
  two (``["ip"] + ["link", "set", ...]``). Docstrings are prose and are not scanned.
- ``bench_import``: importing scripts/bench.

A static scan cannot follow every runtime value (a command read from a file, say). The
runtime backs it: the capture audit sees every frame on the wire, and the link watch
re-judges can0 and can1 every second.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "photoncar"
SCRIPTS = REPO / "scripts"
BENCH_EMULATOR = Path("scripts/bench/ecu_emulator.py")
TX_OWNER = "sources/obd.py"
BUS_OWNERS = frozenset({"sources/obd.py", "sources/socketcan.py"})
SEND_ATTRS = frozenset(
    {
        "send",
        "send_periodic",
        "sendto",
        "sendall",
        "sendmsg",
        "_send_periodic_internal",
        "_send_once",
    }
)
RAW_CAN_ATTRS = frozenset({"AF_CAN", "PF_CAN", "CAN_RAW", "CAN_BCM", "CAN_ISOTP", "CAN_J1939"})
AF_CAN = 29
SELF_SENDING = frozenset(
    {
        "broadcastmanager",
        "ThreadBasedCyclicSendTask",
        "CyclicSendTask",
        "send_bcm",
        "create_bcm_socket",
    }
)
"""python-can names that put frames on a bus with no ``.send`` in the calling code."""
CAN_TOOLS = frozenset(
    {
        "cansend",
        "cangen",
        "canplayer",
        "cansequence",
        "cangw",
        "canfdtest",
        "isotpsend",
        "isotptun",
        "isotpserver",
        "bcmserver",
        "j1939cat",
        "testj1939",
    }
)
"""can-utils programs that put frames on a bus, or route frames onto one (cangw)."""
IP_READ_COMMANDS = frozenset({"show", "list", "lst"})
"""The ``ip link`` commands that only read. ``ip`` takes any prefix, and ``s`` means set."""
IP_WRITE_COMMANDS = ("set", "change", "add", "delete", "replace")
PLACEHOLDER = "\x00"
"""A token whose value is only known at run time (a name, a call, an f-string field)."""
PROCESS_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "pty.spawn",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)
PROCESS_PREFIXES = ("subprocess.", "os.exec", "os.spawn", "os.posix_spawn")
FD_WRITERS = frozenset({"write", "writev", "pwrite", "pwritev", "sendfile"})
"""Calls whose first argument is the descriptor they write to (``os.write``, libc via ctypes)."""
FD_OPENERS = frozenset({"os.fdopen", "open", "io.open", "io.FileIO", "_io.FileIO"})
"""Calls that wrap a descriptor in a writable file."""
SOCKET_ATTRS = frozenset({"fileno", "socket", "fromfd", "socketpair"})
"""Attributes that yield a socket or its descriptor: ``bus.fileno()``, ``bus.socket``."""
SOCKET_NAMES = frozenset({"socket.socket", "socket.fromfd", "socket.socketpair", "_socket.socket"})
REQUEST_BUS_NAMES = frozenset({"open_request_bus", "_open_request_bus", "_SESSION_KEY"})
BUS_ATTRS = frozenset({"_bus", "_buses"})
"""Where ``ObdPoller`` and ``LiveCanSession`` keep their buses."""
IMPORTERS = frozenset({"importlib.import_module", "__import__"})


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    kind: str  # "send", "bus", "raw_can", "request_bus", "can_tool", "bench_import"
    detail: str
    function: str | None


def _resolver(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _dotted(expr: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        base = _dotted(expr.value, aliases)
        return f"{base}.{expr.attr}" if base else None
    return None


def _is_can_module(name: str | None) -> bool:
    return name is not None and (name == "can" or name.startswith("can."))


def _spelled(expr: ast.expr) -> str:
    """Every string constant inside ``expr`` joined: "se" + "nd" and f"se{'nd'}" give "send"."""
    return "".join(
        n.value for n in ast.walk(expr) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _self_sending(dotted: str) -> bool:
    return any(part in SELF_SENDING for part in dotted.split("."))


def _is_can_bus_name(name: str | None) -> bool:
    return _is_can_module(name) and name is not None and name.rsplit(".", 1)[-1].endswith("Bus")


def _callee(func: ast.expr, aliases: dict[str, str]) -> str | None:
    """The dotted name a call goes to, through ``getattr(module, "name")`` too."""
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
        and len(func.args) >= 2
    ):
        base = _dotted(func.args[0], aliases)
        return f"{base}.{_spelled(func.args[1])}" if base else None
    return _dotted(func, aliases)


def _base(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _is_link_object(token: str) -> bool:
    """``ip`` accepts any prefix of an object name: ``l``, ``li``, ``lin``, ``link``."""
    return bool(token) and "link".startswith(token)


def _ip_command(tokens: list[str], obj: int) -> str | None:
    """The command after the ``link`` object at ``obj``, skipping options. None if absent."""
    k = obj + 1
    while k < len(tokens) and tokens[k].startswith("-"):
        k += 1
    return tokens[k] if k < len(tokens) else None


def _is_write_verb(token: str) -> bool:
    return bool(token) and any(verb.startswith(token) for verb in IP_WRITE_COMMANDS)


def _changes_a_link(tokens: list[str], *, argv_list: bool) -> bool:
    """True when ``tokens`` run ``ip ... link <cmd>`` and ``cmd`` is not a plain read.

    After an ``ip`` token, a missing or run-time command counts as a change (fail closed).
    An argv list that starts with ``link`` (the tail of a split argv) counts when its
    command is spelled out and is a prefix of a write verb.
    """
    ips = [i for i, tok in enumerate(tokens) if _base(tok) == "ip"]
    if ips:
        for j in range(ips[0] + 1, len(tokens)):
            if _is_link_object(tokens[j]):
                cmd = _ip_command(tokens, j)
                if cmd is None or cmd not in IP_READ_COMMANDS:
                    return True
    if argv_list and tokens and tokens[0] == "link":
        cmd = _ip_command(tokens, 0)
        if cmd is not None and cmd != PLACEHOLDER and _is_write_verb(cmd):
            return True
    return False


def _command_findings(tokens: list[str], *, argv_list: bool) -> list[str]:
    found = [f"runs {_base(t)}" for t in tokens if _base(t) in CAN_TOOLS]
    if _changes_a_link(tokens, argv_list=argv_list):
        found.append("runs ip link with a command that changes the link")
    return found


def _argv_tokens(node: ast.List | ast.Tuple | ast.Set) -> list[str]:
    """The tokens of an argv literal. A starred run of option flags stays an option."""
    tokens: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            tokens += elt.value.split() or [elt.value]
            continue
        strings = [
            n.value
            for n in ast.walk(elt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        if isinstance(elt, ast.Starred) and strings and all(v.startswith("-") for v in strings):
            tokens.append("-")
        else:
            tokens.append(PLACEHOLDER)
    return tokens


def _fstring_tokens(node: ast.JoinedStr) -> list[str]:
    parts = [
        v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else f" {PLACEHOLDER} "
        for v in node.values
    ]
    return "".join(parts).split()


def _is_process_call(name: str | None) -> bool:
    return name is not None and (name in PROCESS_CALLS or name.startswith(PROCESS_PREFIXES))


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, ast.Tuple | ast.List):
        return [n for elt in target.elts for n in _target_names(elt)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _socket_derived(expr: ast.expr, aliases: dict[str, str], tainted: set[str]) -> bool:
    """True when ``expr`` may be a socket or its descriptor (the bus's CAN socket)."""
    for n in ast.walk(expr):
        if isinstance(n, ast.Attribute) and (n.attr in SOCKET_ATTRS or n.attr in tainted):
            return True
        if isinstance(n, ast.Name) and (
            n.id == "fileno" or n.id in tainted or aliases.get(n.id) in SOCKET_NAMES
        ):
            return True
    return False


def _socket_names(tree: ast.Module, aliases: dict[str, str]) -> set[str]:
    """Names and attributes that hold a socket or descriptor anywhere in the module."""
    assigns: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assigns.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr) and node.value:
            assigns.append(([node.target], node.value))
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for targets, value in assigns:
            if _socket_derived(value, aliases, tainted):
                for name in (n for t in targets for n in _target_names(t)):
                    if name not in tainted:
                        tainted.add(name)
                        changed = True
    return tainted


def _docstrings(tree: ast.Module) -> set[int]:
    """ids of string constants that are statements on their own (docstrings, prose)."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _is_af_can(expr: ast.expr, aliases: dict[str, str]) -> bool:
    if isinstance(expr, ast.Constant) and expr.value == AF_CAN and type(expr.value) is int:
        return True
    name = _dotted(expr, aliases)
    return name is not None and name.rsplit(".", 1)[-1] in ("AF_CAN", "PF_CAN")


def scan_source(text: str, relpath: str) -> list[Finding]:
    tree = ast.parse(text, filename=relpath)
    aliases = _resolver(tree)
    prose = _docstrings(tree)
    sockets = _socket_names(tree, aliases)
    # The enclosing function of each node. ast.walk is breadth first, so an inner function
    # is visited after its outer one and its name overwrites the outer name.
    owner: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(fn):
                owner[child] = fn.name

    out: list[Finding] = []

    def add(node: ast.AST, kind: str, detail: str) -> None:
        line = getattr(node, "lineno", 0)
        out.append(Finding(relpath, line, kind, detail, owner.get(node)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "scripts" or a.name.startswith("scripts."):
                    add(node, "bench_import", a.name)
                if _self_sending(a.name):
                    add(node, "send", f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "scripts" or mod.startswith("scripts."):
                add(node, "bench_import", mod)
            if _self_sending(mod):
                add(node, "send", f"from {mod}")
            for a in node.names:
                if a.name in SELF_SENDING:
                    add(node, "send", f"import {a.name}")
                if a.name in REQUEST_BUS_NAMES:
                    add(node, "request_bus", f"import {a.name}")
                if mod in ("socket", "_socket") and a.name in RAW_CAN_ATTRS:
                    add(node, "raw_can", f"import {a.name}")
        elif isinstance(node, ast.Name):
            if node.id in REQUEST_BUS_NAMES:
                add(node, "request_bus", node.id)
            if node.id in SELF_SENDING:
                add(node, "send", node.id)
            if node.id in aliases and _is_can_bus_name(aliases[node.id]):
                add(node, "bus", aliases[node.id])
        elif isinstance(node, ast.Attribute):
            if _is_can_bus_name(dotted := _dotted(node, aliases)):
                add(node, "bus", str(dotted))
            if node.attr in SEND_ATTRS or node.attr in SELF_SENDING:
                add(node, "send", node.attr)
            if node.attr in RAW_CAN_ATTRS:
                add(node, "raw_can", node.attr)
            if node.attr in REQUEST_BUS_NAMES:
                add(node, "request_bus", node.attr)
            if node.attr in BUS_ATTRS and not (
                isinstance(node.value, ast.Name) and node.value.id == "self"
            ):
                add(node, "request_bus", f"<object>.{node.attr}")
        elif isinstance(node, ast.Call):
            _scan_call(node, aliases, sockets, add)
        elif isinstance(node, ast.List | ast.Tuple | ast.Set):
            for detail in _command_findings(_argv_tokens(node), argv_list=True):
                add(node, "can_tool", detail)
        elif isinstance(node, ast.JoinedStr):
            for detail in _command_findings(_fstring_tokens(node), argv_list=False):
                add(node, "can_tool", detail)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if "scripts/bench" in value or "scripts.bench" in value:
                add(node, "bench_import", value)
            if value in SEND_ATTRS:
                add(node, "send", f"string {value!r}")
            if value in REQUEST_BUS_NAMES or value in BUS_ATTRS:
                add(node, "request_bus", f"string {value!r}")
            if id(node) not in prose:
                for detail in _command_findings(value.split(), argv_list=False):
                    add(node, "can_tool", detail)
    return out


Add = Callable[[ast.AST, str, str], None]


def _scan_call(node: ast.Call, aliases: dict[str, str], sockets: set[str], add: Add) -> None:
    func = node.func
    name = _dotted(func, aliases)
    callee = _callee(func, aliases)
    last = callee.rsplit(".", 1)[-1] if callee else None
    if _is_process_call(callee):
        spelled = _spelled(ast.Tuple(elts=[*node.args, *(kw.value for kw in node.keywords)]))
        for tool in sorted(CAN_TOOLS):
            if tool in spelled:
                add(node, "can_tool", f"process runs {tool}")
    fd_arg = node.args[0] if node.args else None
    if fd_arg is not None and _socket_derived(fd_arg, aliases, sockets):
        if last in FD_WRITERS:
            add(node, "send", f"{callee} on a socket descriptor")
        if callee in FD_OPENERS:
            add(node, "send", f"{callee} of a socket descriptor")
    if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
        target, attr = node.args[0], node.args[1]
        spelled = _spelled(attr)
        if "send" in spelled:
            add(node, "send", f"getattr {spelled}")
        if spelled in REQUEST_BUS_NAMES or spelled in BUS_ATTRS:
            add(node, "request_bus", f"getattr {spelled}")
        if _is_can_module(_dotted(target, aliases)):
            add(node, "bus", "getattr on the can module")
    if name in IMPORTERS and node.args and _is_can_module(_spelled(node.args[0])):
        add(node, "bus", f"dynamic import {_spelled(node.args[0])}")
    if name in ("socket.socket", "_socket.socket"):
        family = [kw.value for kw in node.keywords if kw.arg == "family"]
        first = node.args[:1] + family
        if any(_is_af_can(a, aliases) for a in first):
            add(node, "raw_can", "socket.socket(AF_CAN)")


def _scan_tree() -> tuple[list[str], list[Finding]]:
    files = sorted(SRC.rglob("*.py"))
    rels = [p.relative_to(SRC).as_posix() for p in files]
    findings: list[Finding] = []
    for path, rel in zip(files, rels, strict=True):
        findings += scan_source(path.read_text(encoding="utf-8"), rel)
    return rels, findings


def test_only_obd_sends_and_only_obd_and_socketcan_open_a_bus() -> None:
    rels, findings = _scan_tree()
    assert TX_OWNER in rels and "sources/socketcan.py" in rels
    sends = [f for f in findings if f.kind == "send"]
    buses = [f for f in findings if f.kind == "bus"]
    assert [f for f in sends if f.path != TX_OWNER] == []
    assert [f for f in buses if f.path not in BUS_OWNERS] == []
    assert [f for f in findings if f.kind == "request_bus" and f.path != TX_OWNER] == []
    assert [f for f in findings if f.kind in ("raw_can", "bench_import", "can_tool")] == []


def test_the_one_send_is_guarded() -> None:
    text = (SRC / TX_OWNER).read_text(encoding="utf-8")
    sends = [f for f in scan_source(text, TX_OWNER) if f.kind == "send"]
    assert len(sends) == 1
    send = sends[0]
    assert send.detail == "send"
    assert send.function == "_transmit"
    tree = ast.parse(text)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_transmit")
    guard_lines = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "check_and_consume"
    ]
    assert guard_lines and min(guard_lines) < send.line


def test_scripts_never_send_or_open_a_bus() -> None:
    """The plan's rule holds outside src too: no script sends, opens a bus, or reaches can1.

    The one exception is the bench ECU emulator, which plays the car's ECU on the bench.
    """
    files = [p for p in sorted(SCRIPTS.rglob("*.py")) if p.relative_to(REPO) != BENCH_EMULATOR]
    assert len(files) >= 2 and (REPO / BENCH_EMULATOR).is_file()
    findings = [
        f
        for p in files
        for f in scan_source(p.read_text(encoding="utf-8"), p.relative_to(REPO).as_posix())
        if f.kind in ("send", "bus", "raw_can", "request_bus", "can_tool")
    ]
    assert findings == []


def test_socketcan_opens_a_bus_and_never_sends() -> None:
    rel = "sources/socketcan.py"
    found = scan_source((SRC / rel).read_text(encoding="utf-8"), rel)
    assert [f.kind for f in found if f.kind == "bus"] == ["bus"]
    assert [f for f in found if f.kind in ("send", "request_bus", "raw_can", "can_tool")] == []


def test_the_request_bus_opener_is_private_and_used_only_by_the_session() -> None:
    text = (SRC / TX_OWNER).read_text(encoding="utf-8")
    tree = ast.parse(text)
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_open_request_bus" in defs
    assert "open_request_bus" not in defs
    callers = {f.function for f in scan_source(text, TX_OWNER) if f.detail == "_open_request_bus"}
    assert callers == {"open_live_can_session"}


# Each pattern the scanner must catch. The first group is the original set; the second is
# the set of routes the adversarial review found open.
CAUGHT = [
    ("bus.send(msg)", "send"),
    ("self._bus.send(msg, timeout=1)", "send"),
    ("bus.send_periodic(msg, 0.1)", "send"),
    ("getattr(bus, 'send')(msg)", "send"),
    ("sock.sendto(b'', addr)", "send"),
    ("import can\ncan.Bus(interface='socketcan', channel='can1')", "bus"),
    ("import can\ncan.interface.Bus('can1', interface='socketcan')", "bus"),
    ("import can\ncan.ThreadSafeBus('can1', interface='socketcan')", "bus"),
    ("import can as c\nc.Bus('can1')", "bus"),
    ("from can import Bus as B\nB('can1')", "bus"),
    ("from can import interface\ninterface.Bus('can1')", "bus"),
    ("from can.interfaces.socketcan import SocketcanBus\nSocketcanBus('can1')", "bus"),
    ("import socket\nsocket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)", "raw_can"),
    ("from scripts.bench import ecu_emulator", "bench_import"),
    ("import scripts.bench.check_log", "bench_import"),
    ("import importlib\nimportlib.import_module('scripts.bench.ecu_emulator')", "bench_import"),
    # Review probes.
    (
        "from photoncar.sources.obd import open_request_bus\nbus = open_request_bus(cfg)",
        "request_bus",
    ),
    (
        "from photoncar.sources.obd import _open_request_bus as opener\nopener(cfg, factory)",
        "request_bus",
    ),
    ("from photoncar.sources import obd\nbus = obd._open_request_bus(cfg, f)", "request_bus"),
    ("import photoncar.sources.obd as o\nopen_it = o.open_request_bus", "request_bus"),
    ("bus = poller._bus", "request_bus"),
    ("session._poller._bus.recv(0.1)", "request_bus"),
    ("bus = getattr(poller, '_bus')", "request_bus"),
    (
        "from photoncar.sources import obd\nObdPoller(bus, g, c, k, _key=obd._SESSION_KEY)",
        "request_bus",
    ),
    ("bus._send_periodic_internal([msg], 0.1)", "send"),
    ("task._send_once()", "send"),
    ("f = bus.send\nf(msg)", "send"),
    ("getattr(bus, 'send', None)", "send"),
    ("getattr(bus, 'se' + 'nd')(msg)", "send"),
    ("getattr(bus, f'se{\"nd\"}')(msg)", "send"),
    ("getattr(bus, 'send_periodic')(msg, 0.1)", "send"),
    ("import operator\noperator.methodcaller('send', msg)(bus)", "send"),
    ("import socket\ns = socket.socket(29, socket.SOCK_RAW, 1)", "raw_can"),
    ("import socket\ns = socket.socket(29, 3, 1)", "raw_can"),
    ("import socket\ns = socket.socket(family=29, type=3, proto=1)", "raw_can"),
    ("from socket import socket as sk\nsk(29, 3, 1)", "raw_can"),
    ("from socket import AF_CAN, socket\nsocket(AF_CAN, 3, 1)", "raw_can"),
    ("import can\ngetattr(can, 'Bus')('can1', interface='socketcan')", "bus"),
    ("import can as c\ngetattr(c.interface, 'Bus')('can1')", "bus"),
    ("import can\nmaker = getattr(can, name)", "bus"),
    ("import importlib\nimportlib.import_module('can').Bus('can1')", "bus"),
    ("__import__('can').Bus('can1')", "bus"),
    # Routes found in the resume audit: the session's bus tuple, and python-can helpers that
    # send with no .send in the calling code.
    ("session._buses[1].send_periodic", "request_bus"),
    ("bus = getattr(session, '_buses')[1]", "request_bus"),
    (
        "from can.broadcastmanager import ThreadBasedCyclicSendTask\n"
        "ThreadBasedCyclicSendTask(bus, lock, [msg], 0.1)",
        "send",
    ),
    ("import can\ncan.broadcastmanager.ThreadBasedCyclicSendTask(bus, lock, [msg], 0.1)", "send"),
    (
        "import can.broadcastmanager as bm\nbm.ThreadBasedCyclicSendTask(bus, lock, [msg], 0.1)",
        "send",
    ),
    (
        "from can.interfaces.socketcan.socketcan import create_bcm_socket, send_bcm\n"
        "send_bcm(create_bcm_socket('can1'), data)",
        "send",
    ),
    (
        "from can.interfaces.socketcan import CyclicSendTask\nCyclicSendTask(s, 1, [msg], 0.1)",
        "send",
    ),
    # Routes found in the minor round: can-utils and ip link through a process, a Bus
    # constructor used without a call at the reference, and writes to a socket descriptor.
    ("import subprocess\nsubprocess.run(['cansend', 'can1', '7DF#02010C'])", "can_tool"),
    ("import subprocess\nsubprocess.run('cansend can1 7DF#02010C', shell=True)", "can_tool"),
    ("import os\nos.system('cansend can1 7DF#02010CCCCCCCCCCC')", "can_tool"),
    ("import os\nos.popen('/usr/bin/cangen can1 -g 10')", "can_tool"),
    ("import subprocess\nsubprocess.Popen(['/usr/bin/cangen', 'can1', '-g', '10'])", "can_tool"),
    ("import subprocess\nsubprocess.check_call(['canplayer', '-I', 'drive.log'])", "can_tool"),
    ("import os\nos.execvp('cansend', ['cansend', 'can1', '7DF#02'])", "can_tool"),
    ("import asyncio\nasyncio.create_subprocess_exec('cangen', 'can1')", "can_tool"),
    ("from subprocess import run as r\nr(['can' + 'send', 'can1', '7DF#02'])", "can_tool"),
    ("import subprocess\nsubprocess.run(['ip', 'link', 'set', 'can1', 'down'])", "can_tool"),
    (
        "import subprocess\nsubprocess.run(\n"
        "    ['sudo', '/sbin/ip', '-details', 'link', 'set', 'can0', 'type', 'can',"
        " 'listen-only', 'off']\n)",
        "can_tool",
    ),
    ("import os\nos.system('ip link set can1 down && ip link set can1 up')", "can_tool"),
    ("import os\nos.system('ip -json link show can1; ip link set can1 down')", "can_tool"),
    ("import subprocess\nsubprocess.run(['ip', 'l', 's', 'can1', 'down'])", "can_tool"),
    ("import subprocess\nsubprocess.run(['ip', 'link', 'change', 'can1', 'up'])", "can_tool"),
    ("cmd = 'ip link ' + 'set can1 type can restart-ms 100'", "can_tool"),
    ("verb = 'set'\ncmd = f'ip link {verb} can1 down'", "can_tool"),
    ("argv = ['ip', '-json']\nargv += ['link', 'set', 'can1', 'down']", "can_tool"),
    ("argv = ['ip', 'link']\nargv += ['set', 'can1', 'down']", "can_tool"),
    ("argv = ('ip', 'link', verb, 'can1', 'down')", "can_tool"),
    ("import can\nB = can.Bus\nB('can1', interface='socketcan')", "bus"),
    (
        "import can\nimport functools\n"
        "open_can1 = functools.partial(can.Bus, interface='socketcan')",
        "bus",
    ),
    ("import can\nbuses = list(map(can.Bus, ['can1']))", "bus"),
    ("from can import Bus\nmake = Bus", "bus"),
    ("from can.interfaces.socketcan import SocketcanBus\nmakers = {'x': SocketcanBus}", "bus"),
    ("import can as c\nopeners = [c.interface.Bus]", "bus"),
    ("import os\nos.write(bus.fileno(), frame)", "send"),
    ("import os\nos.write(bus.socket.fileno(), frame)", "send"),
    ("import os\nfd = bus.fileno()\nos.write(fd, frame)", "send"),
    ("import os\nfd = bus.fileno()\nfd2 = fd\nos.writev(fd2, [frame])", "send"),
    ("import os\nself._fd = bus.socket.fileno()\nos.write(self._fd, frame)", "send"),
    ("from os import write\nwrite(sock.fileno(), frame)", "send"),
    ("import os\ngetattr(os, 'write')(bus.fileno(), frame)", "send"),
    ("import os\nos.fdopen(bus.fileno(), 'wb').write(frame)", "send"),
    ("import os\nos.sendfile(bus.fileno(), src_fd, 0, 16)", "send"),
    ("import ctypes\nlibc = ctypes.CDLL(None)\nlibc.write(bus.fileno(), frame, 16)", "send"),
]


@pytest.mark.parametrize(("snippet", "kind"), CAUGHT)
def test_scanner_catches_each_pattern(snippet: str, kind: str) -> None:
    kinds = {f.kind for f in scan_source(snippet, "api/app.py")}
    assert kind in kinds


@pytest.mark.parametrize(
    "snippet",
    [
        "import can\nmsg = can.Message(arbitration_id=0x7DF)",
        "async def app(scope, receive, send):\n    await send({'type': 'http.response.start'})",
        "queue.put(item)",
        "x = gen.asend(None)",
        "t = getattr(record, 't_ns', None)",
        "vals = (getattr(r, attr) for r in rows)",
        "class D:\n    def __init__(self, bus):\n        self._bus = bus",
        "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        "import socket\ns = socket.socket(2, 1)",
        "from photoncar.sources.obd import open_live_can_session, LiveCanUnavailable",
        "sent = poller.sent",
        # The minor-round rules leave the real non-TX shapes alone.
        "import subprocess\nsubprocess.run(['ip', '-details', '-json', 'link', 'show', 'can1'])",
        "argv = ['ip', '-details', *(['-statistics'] if s else []), '-json']\n"
        "argv += ['link', 'show', ifname]",
        "import os\nos.system('ip -json link show can0')",
        "import subprocess\nsubprocess.run(['git', 'ls-files', '-z'], capture_output=True)",
        "import os\nfd = os.open('/dev/ttyACM0', os.O_RDWR)\nos.write(fd, b'\\xb5b')",
        "import os\nself._fd = os.open(path, os.O_RDWR)\nos.write(self._fd, frame)",
        "import sys\nsys.stdout.write('socket closed')",
        "import can\nbus: can.BusABC | None = None",
        "class I2cBus:\n    pass\nbus = I2cBus()",
        "headers = [('link', '</app.js>; rel=preload')]",
        'def f():\n    """Never runs cansend or ip link set."""',
    ],
)
def test_scanner_allows_non_tx_code(snippet: str) -> None:
    assert scan_source(snippet, "api/app.py") == []
