"""Plain tool output at execution and report boundaries."""
import re

# CSI (colors/cursor controls), OSC (including hyperlinks), and short escapes.
_ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]')


def strip_ansi(text: str) -> str:
    return _ANSI.sub('', text)


def plain_command(argv: list[str]) -> list[str]:
    from pathlib import Path

    if not argv:
        return argv
    name = Path(argv[0]).name
    pytest = name in {'pytest', 'py.test'} or argv[1:3] == ['-m', 'pytest']
    ruff = name == 'ruff' or argv[1:3] == ['-m', 'ruff']
    option = '--color' if pytest else '--output-format' if ruff and 'check' in argv else None
    if option is None:
        return argv
    result = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        if arg == option:
            index += 2
            continue
        if not arg.startswith(option + '='):
            result.append(arg)
        index += 1
    result.append(option + ('=no' if pytest else '=concise'))
    return result + argv[index:]
