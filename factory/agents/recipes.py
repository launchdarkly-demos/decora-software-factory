from __future__ import annotations

from factory.errors import RecipeError
from factory.governance.flags import verifier_requires_default_off
from factory.models import BuildProposal, FileEdit, JobState
from factory.tools.layer import ToolLayer

FLAGS_NEEDLE = 'BOARD_INTAKE_FLAG = "decor-board-intake"\n'

CAPABILITIES_RETURN = """\
    return {
        "board_intake": get_flag(BOARD_INTAKE_FLAG, context, default=True),
        "sample_board": "living-linen",
    }
"""

CAPABILITIES_PATCHED = """\
    payload = {
        "board_intake": get_flag(BOARD_INTAKE_FLAG, context, default=True),
        "sample_board": "living-linen",
    }
    if get_flag(ROOM_MOOD_FLAG, context, default=FLAG_DEFAULT):
        payload.update(room_mood_payload(True))
    return payload
"""


def ship_flag_on(job: JobState) -> bool:
    """The sample request can ask the first build to leave the flag on.

    The repair is not a hidden counter. It happens when the verifier's text
    says the default must be off. Later builds then write FLAG_DEFAULT = False.
    """
    latest = job.latest_verify()
    if latest and verifier_requires_default_off(latest.actionable):
        return False
    if job.feature.demo_fail_first_build:
        return True
    return False


def add_import_name(line: str, name: str) -> str:
    if name in line or " import " not in line:
        return line
    newline = "\n" if line.endswith("\n") else ""
    body = line[:-1] if newline else line
    prefix, imports = body.split(" import ", 1)
    names = [part.strip() for part in imports.split(",") if part.strip()]
    names.insert(1, name)
    return prefix + " import " + ", ".join(names) + newline


def room_mood_proposal(job: JobState, tools: ToolLayer) -> BuildProposal:
    if job.plan is None or job.plan.recipe != "flagged_capability_field":
        recipe = job.plan.recipe if job.plan else "missing"
        raise RecipeError(
            f"Mock mode has no recipe for {recipe}. "
            "The offline demo implements decor-room-mood on /api/capabilities."
        )
    flag_on = ship_flag_on(job)
    server = tools.read("server.py")
    flags = tools.read("app/flags.py")
    if FLAGS_NEEDLE not in flags:
        raise RecipeError("app/flags.py is missing BOARD_INTAKE_FLAG; a human needs to point at the flag module.")
    if "room_mood_payload" not in server and CAPABILITIES_RETURN not in server:
        raise RecipeError(
            "server.py capabilities return block was not found. "
            "A human needs to point at the route this flag should wrap."
        )
    if not (tools.worktree / "app" / "__init__.py").exists():
        raise RecipeError("app package is missing; a human needs to confirm the layout.")

    edits: list[FileEdit] = [
        FileEdit(path="app/room_mood.py", kind="write", content=_room_mood_module(flag_on)),
        FileEdit(
            path="test_room_mood_flag.py",
            kind="write",
            content=_PURE_TEST,
        ),
    ]
    if "ROOM_MOOD_FLAG" not in flags:
        edits.append(
            FileEdit(
                path="app/flags.py",
                kind="replace",
                old=FLAGS_NEEDLE,
                new=FLAGS_NEEDLE + 'ROOM_MOOD_FLAG = "decor-room-mood"\n',
            )
        )
    if "room_mood_payload" not in server:
        patched = _patch_server(server)
        edits.append(FileEdit(path="server.py", kind="write", content=patched))
    if "FastAPI(" in server:
        edits.append(FileEdit(path="test_room_mood_api.py", kind="write", content=_API_TEST))

    default_word = "on" if flag_on else "off"
    return BuildProposal(
        summary=f"Room mood capability, code default {default_word}.",
        commit_message=(
            "Default the room mood flag off."
            if not flag_on
            else "Add a room mood line behind decor-room-mood."
        ),
        edits=edits,
        flag_default=flag_on,
    )


def _patch_server(server: str) -> str:
    lines = server.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for line in lines:
        if line.startswith("from app.flags import ") and "ROOM_MOOD_FLAG" not in line:
            line = add_import_name(line, "ROOM_MOOD_FLAG")
        out.append(line)
        if not inserted and line.startswith("from app.flags import "):
            out.append("from app.room_mood import FLAG_DEFAULT, room_mood_payload\n")
            inserted = True
    text = "".join(out)
    if not inserted:
        raise RecipeError("server.py has no app.flags import to extend.")
    if CAPABILITIES_RETURN not in text:
        raise RecipeError("server.py capabilities return block was not found.")
    return text.replace(CAPABILITIES_RETURN, CAPABILITIES_PATCHED, 1)


def _room_mood_module(flag_on: bool) -> str:
    default = "True" if flag_on else "False"
    return f'''"""Room mood copy for Studio.

The LaunchDarkly flag decor-room-mood defaults off. Studio only receives
the mood line after a human turns that flag on.
"""

FLAG_KEY = "decor-room-mood"
FLAG_DEFAULT = {default}

MOOD_LINE = "calm linen"


def room_mood_payload(enabled: bool | None = None) -> dict[str, str]:
    on = FLAG_DEFAULT if enabled is None else enabled
    if not on:
        return {{}}
    return {{"room_mood": MOOD_LINE}}
'''


_PURE_TEST = '''from app.room_mood import FLAG_DEFAULT, FLAG_KEY, room_mood_payload


def test_room_mood_flag_identity() -> None:
    assert FLAG_KEY == "decor-room-mood"
    assert FLAG_DEFAULT is False


def test_room_mood_hidden_when_disabled() -> None:
    assert room_mood_payload(False) == {}
    assert room_mood_payload() == {}


def test_room_mood_shown_when_enabled() -> None:
    assert room_mood_payload(True) == {"room_mood": "calm linen"}
'''


_API_TEST = '''from fastapi.testclient import TestClient

import server
from server import app


def test_capabilities_omit_room_mood_when_flag_off(monkeypatch) -> None:
    monkeypatch.setenv("DECORA_PERSIST", "0")

    def fake(key, _context, default=False):
        if key == "decor-room-mood":
            return False
        return default

    monkeypatch.setattr(server, "get_flag", fake)
    body = TestClient(app).get("/api/capabilities").json()
    assert "room_mood" not in body


def test_capabilities_follow_code_default(monkeypatch) -> None:
    monkeypatch.setenv("DECORA_PERSIST", "0")
    seen: dict[str, bool] = {}

    def fake(key, _context, default=False):
        if key == "decor-room-mood":
            seen["default"] = default
            return default
        return default

    monkeypatch.setattr(server, "get_flag", fake)
    body = TestClient(app).get("/api/capabilities").json()
    assert seen["default"] is False
    assert "room_mood" not in body


def test_capabilities_include_room_mood_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("DECORA_PERSIST", "0")

    def fake(key, _context, default=False):
        if key == "decor-room-mood":
            return True
        return default

    monkeypatch.setattr(server, "get_flag", fake)
    body = TestClient(app).get("/api/capabilities").json()
    assert body["room_mood"] == "calm linen"
'''
