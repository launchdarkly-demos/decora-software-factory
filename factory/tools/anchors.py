"""Unique anchor matching with conservative Unicode fallback and source spans."""
from __future__ import annotations

import unicodedata

from factory.errors import FactoryError

# Explicit folds, rather than fuzzy matching: the control/arrow pair occurred in #22.
_FOLDS = str.maketrans({
    '→': '->', '\x12': '->', '⇒': '=>',
    '‘': "'", '’': "'", '“': '"', '”': '"',
    '–': '-', '—': '-', '−': '-', '\u00a0': ' ',
})


def _normalized(text: str) -> tuple[str, list[tuple[int, int]]]:
    output: list[str] = []
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = start + 1
        # Keep a combining sequence together so NFC composes without losing offsets.
        while end < len(text):
            cluster = unicodedata.normalize('NFC', text[start:end])
            next_char = text[end]
            if (not unicodedata.combining(next_char) and
                    unicodedata.normalize('NFC', cluster + next_char) ==
                    cluster + unicodedata.normalize('NFC', next_char)):
                break
            end += 1
        cluster = unicodedata.normalize('NFC', text[start:end])
        folded = ''.join(char.translate(_FOLDS) if char in '\x12' or
                         unicodedata.category(char) not in {'Cc', 'Cf'} or char in '\n\r\t'
                         else '' for char in cluster)
        output.extend(folded)
        spans.extend([(start, end)] * len(folded))
        start = end
    return ''.join(output), spans


def anchor_span(path: str, text: str, old: str) -> tuple[int, int, bool]:
    from factory.tools.layer import _anchor_error

    count = text.count(old) if old else 0
    if not old or count > 1:
        raise FactoryError(_anchor_error(path, text, old, count))
    if count == 1:
        start = text.index(old)
        return start, start + len(old), False
    normalized, spans = _normalized(text)
    needle, _ = _normalized(old)
    matches: list[tuple[int, int]] = []
    offset = 0
    while needle and (index := normalized.find(needle, offset)) >= 0:
        end_index = index + len(needle)
        # Never match only part of an expanded character or combining cluster.
        if ((index == 0 or spans[index - 1] != spans[index]) and
                (end_index == len(spans) or spans[end_index - 1] != spans[end_index])):
            matches.append((spans[index][0], spans[end_index - 1][1]))
        offset = index + 1
    if len(matches) != 1:
        if matches:
            lines = ', '.join(str(text.count('\n', 0, start) + 1) for start, _ in matches)
            raise FactoryError(f'{path}: normalized anchor matched {len(matches)} times; line numbers: {lines}. '
                               'Extend the anchor until it\'s unique.')
        raise FactoryError(_anchor_error(path, text, old, 0))
    return *matches[0], True
