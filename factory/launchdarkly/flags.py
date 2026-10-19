"""Create or reuse a boolean flag and prove it is off before review."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from factory.errors import FactoryError
from factory.governance.policy import Policy, flag_required, issue_supplied_flag_key, selected_flag_key
from factory.launchdarkly.client import LaunchDarklyClient
from factory.launchdarkly.token import resolve_token, scrub
from factory.models import FlagAssurance, JobState

Json = dict[str, Any]


class FlagClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        semantic: bool = False,
        query: list[tuple[str, str]] | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class LaunchDarklyService:
    def __init__(
        self,
        *,
        mode: str,
        policy: Policy,
        target_repo: Path,
        client: FlagClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if mode not in {"auto", "live", "offline"}:
            raise FactoryError(f"unknown LaunchDarkly mode {mode}")
        self.mode = mode
        self.policy = policy
        self.target_repo = target_repo
        self._client = client
        self._environ = environ

    def scrub(self, text: str) -> str:
        token, _source = self._token()
        return scrub(text, token)

    def ensure(self, job: JobState) -> FlagAssurance:
        key = _flag_key(job)
        rules = self.policy.launchdarkly
        required = flag_required(self.policy, job)
        if not required:
            return FlagAssurance(
                mode="offline",
                required=False,
                key=key,
                project_key=rules.project_key,
                detail=("Feature flags disabled by policy."
                        if not self.policy.flags.enabled else "no LaunchDarkly flag required for this change"),
            )
        if not key:
            raise FactoryError("Required rollout flag has no key; return to planning.")
        if self.mode == "offline":
            return self._skipped(key, "live LaunchDarkly check skipped (--ld offline)")
        token, _source = self._token()
        if not token:
            if self.mode == "live":
                raise FactoryError(
                    "LAUNCHDARKLY_API_TOKEN is unset. Export it, add it to the factory repo .env, "
                    "or pass --ld offline."
                )
            return self._skipped(key, "live LaunchDarkly check skipped; LAUNCHDARKLY_API_TOKEN is unset")
        try:
            return self._sync(job, key, token)
        except FactoryError as exc:
            raise FactoryError(scrub(str(exc), token)) from None

    def _skipped(self, key: str, detail: str) -> FlagAssurance:
        return FlagAssurance(
            mode="offline",
            required=True,
            key=key,
            project_key=self.policy.launchdarkly.project_key,
            detail=detail,
        )

    def _token(self) -> tuple[str | None, str]:
        if self._environ is not None:
            token = self._environ.get("LAUNCHDARKLY_API_TOKEN", "").strip()
            return (token or None, "env" if token else "missing")
        return resolve_token(Path(__file__).resolve().parents[2])

    def _sync(self, job: JobState, key: str, token: str) -> FlagAssurance:
        rules = self.policy.launchdarkly
        project = rules.project_key
        environments = list(rules.environments)
        production = rules.production_environment
        url = console_url(project, key, production)
        client = self._client or LaunchDarklyClient(token)
        tags = ["software-factory", job.id]
        query = [("env", name) for name in environments]

        status, payload = client.request("GET", f"/flags/{project}/{key}", query=query)
        created = False
        if status == 404 and issue_supplied_flag_key(job):
            raise FactoryError(f"Issue names existing flag {key}, but it was not found in LaunchDarkly.")
        if status == 404:
            description = scrub(_description(job, ""), token)
            create_status, _created = client.request(
                "POST",
                f"/flags/{project}",
                {
                    "name": job.feature.title or key,
                    "key": key,
                    "description": description,
                    "kind": "boolean",
                    "temporary": True,
                    "tags": tags,
                },
            )
            if create_status == 409:
                created = False
            elif create_status in {200, 201}:
                created = True
            else:
                raise FactoryError(f"LaunchDarkly could not create flag {key} ({create_status})")
            status, payload = client.request("GET", f"/flags/{project}/{key}", query=query)
            if status != 200:
                raise FactoryError(f"flag {key} could not be read after create ({status})")
        elif status != 200:
            raise FactoryError(f"LaunchDarkly could not read flag {key} ({status})")

        _require_boolean(payload, key)
        mutated = _sync_metadata(client, project, key, job, payload, tags, token)
        mutated = _sync_environments(client, project, key, payload, environments) or mutated
        if mutated or created:
            status, payload = client.request("GET", f"/flags/{project}/{key}", query=query)
            if status != 200:
                raise FactoryError(f"flag {key} could not be re-read ({status})")
            _require_boolean(payload, key)

        return _assess(
            payload,
            key=key,
            project=project,
            url=url,
            environments=environments,
            production=production,
            created=created,
        )


def console_url(project_key: str, flag_key: str, environment: str) -> str:
    return (
        "https://app.launchdarkly.com/projects/"
        f"{project_key}/flags/{flag_key}/targeting?env={environment}"
    )


def _flag_key(job: JobState) -> str | None:
    return selected_flag_key(job)


def _description(job: JobState, existing: str) -> str:
    marker = f"software-factory run {job.id}"
    if marker in existing:
        return existing
    link = f"{marker} for {job.feature.title}. Record: .factory/runs/{job.id}."
    if existing.strip():
        return existing.rstrip() + " " + link
    return link


def _sync_metadata(
    client: FlagClient,
    project: str,
    key: str,
    job: JobState,
    payload: Json,
    tags: list[str],
    token: str,
) -> bool:
    current_tags = _tags(payload)
    missing = [tag for tag in tags if tag not in current_tags]
    raw_description = payload.get("description")
    current_description = raw_description if isinstance(raw_description, str) else ""
    new_description = scrub(_description(job, current_description), token)
    instructions: list[dict[str, Any]] = []
    if missing:
        instructions.append({"kind": "addTags", "values": missing})
    if new_description != current_description:
        instructions.append({"kind": "updateDescription", "value": new_description})
    if not instructions:
        return False
    status, _body = client.request(
        "PATCH",
        f"/flags/{project}/{key}",
        {"comment": f"software-factory run {job.id}", "instructions": instructions},
        semantic=True,
    )
    if status >= 400:
        raise FactoryError(f"LaunchDarkly could not update flag {key} metadata ({status})")
    return True


def _sync_environments(
    client: FlagClient,
    project: str,
    key: str,
    payload: Json,
    environments: list[str],
) -> bool:
    false_id = _false_variation_id(payload, key)
    mutated = False
    for name in environments:
        env = _environment(payload, name)
        if env is not None:
            on, serves_false = _targeting(payload, env)
            if on is False and serves_false is True:
                continue
        status, _body = client.request(
            "PATCH",
            f"/flags/{project}/{key}",
            {
                "comment": "software-factory keeps targeting off",
                "environmentKey": name,
                "instructions": [
                    {"kind": "turnFlagOff"},
                    {"kind": "updateOffVariation", "variationId": false_id},
                ],
            },
            semantic=True,
        )
        if status >= 400:
            raise FactoryError(f"LaunchDarkly could not turn {key} off in {name} ({status})")
        mutated = True
    return mutated


def _assess(
    payload: Json,
    *,
    key: str,
    project: str,
    url: str,
    environments: list[str],
    production: str,
    created: bool,
) -> FlagAssurance:
    problems: list[str] = []
    states: dict[str, tuple[bool | None, bool | None]] = {}
    for name in environments:
        env = _environment(payload, name)
        if env is None:
            problems.append(f"{name} was not returned")
            states[name] = (None, None)
            continue
        on, serves_false = _targeting(payload, env)
        states[name] = (on, serves_false)
        if on is not False or serves_false is not True:
            problems.append(f"{name} is not off serving false")
    prod_on, prod_serves = states.get(production, (None, None))
    if created:
        action = f"created {key}"
    else:
        action = f"reused {key}"
    if problems:
        detail = f"{action}; " + "; ".join(problems)
    else:
        detail = f"{action}; targeting is off in {', '.join(environments)}"
    return FlagAssurance(
        mode="live",
        required=True,
        key=key,
        project_key=project,
        url=url,
        created=created,
        reused=not created,
        production_on=prod_on,
        production_serves_false=prod_serves,
        environments_off=not problems,
        detail=detail,
    )


def _require_boolean(payload: Json, key: str) -> None:
    kind = payload.get("kind")
    if kind == "boolean":
        return
    if kind is None and _false_variation_id(payload, key):
        return
    raise FactoryError(f"flag {key} exists with kind {kind}; expected boolean")


def _variations(payload: Json) -> list[Json]:
    raw = payload.get("variations")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _false_variation_id(payload: Json, key: str) -> str:
    for item in _variations(payload):
        if item.get("value") is False and isinstance(item.get("_id"), str):
            return str(item["_id"])
    raise FactoryError(f"flag {key} has no false variation id")


def _tags(payload: Json) -> list[str]:
    raw = payload.get("tags")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _environment(payload: Json, name: str) -> Json | None:
    environments = payload.get("environments")
    if not isinstance(environments, dict):
        return None
    env = environments.get(name)
    if not isinstance(env, dict):
        return None
    return env


def _targeting(payload: Json, env: Json) -> tuple[bool | None, bool | None]:
    on = env.get("on")
    if not isinstance(on, bool):
        return None, None
    return on, _serves_false(payload, env)


def _serves_false(payload: Json, env: Json) -> bool:
    variations = _variations(payload)
    off = env.get("offVariation")
    if isinstance(off, int) and 0 <= off < len(variations):
        return variations[off].get("value") is False
    if isinstance(off, str):
        for item in variations:
            if item.get("_id") == off:
                return item.get("value") is False
    return False
