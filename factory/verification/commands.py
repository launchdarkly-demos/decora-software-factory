"""Reduce Builder quick commands to targets; execution belongs to the gate."""
from pathlib import Path

from factory.errors import FactoryError


def pytest_targets(commands: list[list[str]]) -> list[str]:
    targets: list[str] = []
    value_options = {'-c', '--rootdir', '-o', '--override-ini', '-k', '-m', '-p', '--color',
                     '--tb', '--maxfail', '--confcutdir', '--import-mode', '--basetemp',
                     '--junitxml', '--junit-xml', '--capture', '--durations'}
    flags = {'-q', '-qq', '-v', '-vv', '-s', '-x', '--disable-warnings', '--no-header',
             '--no-summary', '--collect-only', '--lf', '--ff', '--exitfirst'}
    for argv in commands:
        if argv and Path(argv[0]).name in {'pytest', 'py.test'}:
            args = argv[1:]
        elif (len(argv) >= 3 and Path(argv[0]).name in {'python', 'python3'}
              and argv[1:3] == ['-m', 'pytest']):
            args = argv[3:]
        else:
            raise FactoryError('Quick commands must invoke pytest; other commands are refused.')
        selected: list[str] = []
        index = 0
        positional = False
        while index < len(args):
            arg = args[index]
            if arg == '--' and not positional:
                positional = True
            elif not positional and arg.split('=', 1)[0] in value_options:
                if '=' not in arg:
                    index += 1
                    if index == len(args):
                        raise FactoryError(f'Quick pytest option {arg} requires a value.')
            elif not positional and arg in flags:
                pass
            elif arg.startswith('-'):
                raise FactoryError(f'Unsupported quick pytest option: {arg}; supply pytest targets only.')
            else:
                selected.append(arg)
            index += 1
        targets.extend(selected or ['.'])
    return list(dict.fromkeys(targets))
