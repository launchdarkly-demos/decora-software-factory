"""Reject credentials before generated content can reach disk or Git."""
from __future__ import annotations

import os
import re

from factory.errors import SafetyError

_SECRET = re.compile(
    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
    r'|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}'
    r'|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b'
    r'|(?:api[_-]?key|secret|password|access[_-]?token)\s*[=:]\s*[\'"][^\'"\s]{16,}[\'"]',
    re.IGNORECASE,
)


def check_content(content: str) -> None:
    credentials = [value for key, value in os.environ.items()
                   if any(marker in key.upper() for marker in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD', 'SDK_KEY'))
                   and len(value) >= 8]
    if _SECRET.search(content) or any(value in content for value in credentials):
        raise SafetyError('Secret detected in Builder content; edit refused')
