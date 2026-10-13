from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from factory.clock import Clock
from factory.models import JobEvent


class JsonLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: JobEvent) -> None:
        with self.path.open("a") as handle:
            handle.write(event.model_dump_json() + "\n")


class Tracer:
    """Writes a JSONL span per stage. Also opens an OpenTelemetry span when the API is installed."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._otel = _otel_tracer()

    @contextmanager
    def span(self, name: str, component: str, **attributes: object) -> Iterator[None]:
        start = self.clock.now()
        attrs = {key: str(value) for key, value in attributes.items()}
        otel_span = None
        if self._otel is not None:
            otel_span = self._otel.start_span(name)
            for key, value in attrs.items():
                otel_span.set_attribute(key, value)
        try:
            yield
        except Exception as exc:
            if otel_span is not None:
                otel_span.record_exception(exc)
            raise
        finally:
            record = {
                "name": name,
                "component": component,
                "start": start,
                "end": self.clock.now(),
                "attributes": attrs,
            }
            with self.path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            if otel_span is not None:
                otel_span.end()


def _otel_tracer():
    try:
        from opentelemetry import trace
    except Exception:
        return None
    return trace.get_tracer("software_factory")
