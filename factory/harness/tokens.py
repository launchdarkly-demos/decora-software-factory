from __future__ import annotations

from factory.models import JobState


def charge(job: JobState, incoming: str, outgoing: str, *, source: str = "estimate") -> None:
    job.usage.input_tokens += max(1, len(incoming) // 4)
    job.usage.output_tokens += max(1, len(outgoing) // 4)
    if source == "provider":
        job.usage.source = "provider"


def charge_provider(job: JobState, result: object, fallback_in: str, fallback_out: str) -> None:
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage is None:
        usage = getattr(result, "usage", None)
    if usage is None:
        charge(job, fallback_in, fallback_out)
        return
    incoming = getattr(usage, "input_tokens", None)
    outgoing = getattr(usage, "output_tokens", None)
    if incoming is None:
        incoming = getattr(usage, "request_tokens", 0)
    if outgoing is None:
        outgoing = getattr(usage, "response_tokens", 0)
    job.usage.input_tokens += int(incoming or 0)
    job.usage.output_tokens += int(outgoing or 0)
    job.usage.source = "provider"
