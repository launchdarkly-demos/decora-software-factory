from __future__ import annotations

import copy
import io
import json
import urllib.error
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from factory.cli import _announce_launchdarkly, main
from factory.dotenv import ensure_env_key
from factory.errors import FactoryError
from factory.launchdarkly.client import LaunchDarklyClient
from factory.launchdarkly.flags import LaunchDarklyService
from factory.launchdarkly.token import resolve_token
from factory.models import JobState, Stage
from factory.publish.pr import render_pr
from tests.support import (
    ScriptedHarness,
    ScriptedVerifier,
    bare_job,
    feature,
    init_repo,
    low_plan,
    make_factory,
    passed_verify,
    policy,
)

SECRET = "ld-test-token-value"
FLAG_URL = (
    "https://app.launchdarkly.com/projects/decor-agent-demo-v2/flags/"
    "decor-room-mood/targeting?env=production"
)


def test_dotenv_token_is_returned_to_the_caller_only(tmp_path: Path, capsys, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# not a token",
                "export OTHER=1",
                'LAUNCHDARKLY_API_TOKEN="from-dotenv"',
                "",
            ]
        )
    )
    monkeypatch.delenv("LAUNCHDARKLY_API_TOKEN", raising=False)
    token, source = resolve_token(tmp_path)
    assert token == "from-dotenv"
    assert source == "target .env"

    monkeypatch.setenv("LAUNCHDARKLY_API_TOKEN", SECRET)
    token, source = resolve_token(tmp_path)
    assert token == SECRET
    assert source == "env"
    _announce_launchdarkly("auto", tmp_path)
    announced = capsys.readouterr().out
    assert "token from env" in announced
    assert SECRET not in announced
    assert "from-dotenv" not in announced


def test_openai_key_loads_from_project_dotenv_without_printing(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('OPENAI_API_KEY="sk-test-value"\n')
    env = {"OPENAI_API_KEY": "already-set"}
    assert ensure_env_key(tmp_path, "OPENAI_API_KEY", environ=env) == "env"
    assert env["OPENAI_API_KEY"] == "already-set"

    empty: dict[str, str] = {}
    assert ensure_env_key(tmp_path, "OPENAI_API_KEY", environ=empty) == "project .env"
    assert empty["OPENAI_API_KEY"] == "sk-test-value"
    assert "sk-test-value" not in "OpenAI: live (key from project .env)"


def test_agents_default_to_openai(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["demo", "--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--agents" in help_text
    assert "default: openai" in help_text
    assert "default: auto" in help_text


def test_client_scrubs_the_token_from_api_errors() -> None:
    def urlopen(req: Any, timeout: float = 0) -> Any:
        assert req.get_header("Authorization") == SECRET
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "unauthorized",
            EmailMessage(),
            io.BytesIO(json.dumps({"message": f"rejected {SECRET}"}).encode()),
        )

    client = LaunchDarklyClient(SECRET, urlopen=urlopen)
    with pytest.raises(FactoryError) as caught:
        client.request("GET", "/flags/decor-agent-demo-v2/decor-room-mood")
    assert SECRET not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_client_keeps_not_found_and_sends_semantic_patch() -> None:
    seen: dict[str, str] = {}

    class Response:
        status = 404

        def read(self) -> bytes:
            return b'{"message":"not found"}'

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    def urlopen(req: Any, timeout: float = 0) -> Response:
        seen["content_type"] = req.get_header("Content-type") or ""
        return Response()

    client = LaunchDarklyClient(SECRET, urlopen=urlopen)
    status, body = client.request(
        "PATCH",
        "/flags/decor-agent-demo-v2/decor-room-mood",
        {"instructions": [{"kind": "turnFlagOff"}]},
        semantic=True,
    )
    assert status == 404
    assert body["message"] == "not found"
    assert seen["content_type"] == "application/json; domain-model=launchdarkly.semanticpatch"


def test_reuses_a_flag_and_turns_targeting_off(tmp_path: Path) -> None:
    memory = MemoryLaunchDarkly(
        boolean_flag(on=True, off_index=0, tags=["software-factory"], description="kept by hand")
    )
    record = _service(tmp_path, memory).ensure(_job())

    assert record.reused
    assert record.created is False
    assert record.production_off()
    assert record.environments_off is True
    assert record.url == FLAG_URL
    assert "POST" not in [call[0] for call in memory.calls]
    env_patches = [call for call in memory.calls if call[0] == "PATCH" and "environmentKey" in call[2]]
    assert {call[2]["environmentKey"] for call in env_patches} == {"test", "production"}
    assert [item["kind"] for item in env_patches[0][2]["instructions"]] == [
        "turnFlagOff",
        "updateOffVariation",
    ]
    assert env_patches[0][2]["instructions"][1]["variationId"] == "var-false"
    assert memory.flag is not None
    assert memory.flag["tags"] == ["software-factory", "job-abc12345"]
    assert "job-job-" not in "".join(memory.flag["tags"])
    assert "software-factory run job-abc12345" in memory.flag["description"]
    assert memory.flag["environments"]["production"]["on"] is False
    assert memory.flag["environments"]["test"]["on"] is False

    before = len(memory.calls)
    again = _service(tmp_path, memory).ensure(_job())
    assert again.reused
    assert again.production_off()
    assert [call[0] for call in memory.calls[before:]] == ["GET"]


def test_creates_a_boolean_flag_without_variations(tmp_path: Path) -> None:
    memory = MemoryLaunchDarkly(None)
    record = _service(tmp_path, memory).ensure(_job())
    created = next(call for call in memory.calls if call[0] == "POST")

    assert record.created
    assert record.production_off()
    assert created[2]["kind"] == "boolean"
    assert created[2]["temporary"] is True
    assert "variations" not in created[2]
    assert created[2]["tags"] == ["software-factory", "job-abc12345"]


def test_conflict_on_create_reuses_the_flag(tmp_path: Path) -> None:
    memory = MemoryLaunchDarkly(
        boolean_flag(
            on=False,
            tags=["software-factory", "job-abc12345"],
            description="software-factory run job-abc12345 already recorded",
        ),
        hide_until_create=True,
    )
    record = _service(tmp_path, memory).ensure(_job())

    assert record.reused
    assert record.created is False
    assert record.production_off()
    assert [call[0] for call in memory.calls] == ["GET", "POST", "GET"]


def test_flag_that_stays_on_fails_before_review(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    memory = MemoryLaunchDarkly(boolean_flag(on=True), stick_on=True)
    harness = ScriptedHarness([low_plan()])
    job = make_factory(
        tmp_path,
        repo,
        harness,
        flag_service=_service(repo, memory),
        verifier=ScriptedVerifier([passed_verify()]),
    ).run(feature(user_facing=True, flag_key="decor-room-mood", title="Room mood"))

    assert job.stage == Stage.FAILED
    assert job.review is None
    assert job.reviews == []
    assert job.pr is None
    assert job.error is not None
    assert "production" in job.error
    assert "is not off serving false" in job.error


def test_live_api_error_does_not_leak_the_token(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")

    class Leaky(LaunchDarklyClient):
        def request(self, method: str, path: str, body: dict[str, Any] | None = None, **_kwargs: Any):
            raise FactoryError(f"LaunchDarkly API error (403): denied {self._token}")

    service = LaunchDarklyService(
        mode="live",
        policy=policy(),
        target_repo=repo,
        client=Leaky(SECRET),
        environ={"LAUNCHDARKLY_API_TOKEN": SECRET},
    )
    job = make_factory(
        tmp_path,
        repo,
        ScriptedHarness([low_plan()]),
        flag_service=service,
        verifier=ScriptedVerifier([passed_verify()]),
    ).run(feature(user_facing=True, flag_key="decor-room-mood"))

    assert job.stage == Stage.FAILED
    assert job.pr is None
    assert job.review is None
    assert SECRET not in (job.error or "")
    assert "[redacted]" in (job.error or "")
    assert SECRET not in job.model_dump_json()
    assert SECRET not in render_pr(job, "")


def test_auto_without_a_token_skips_and_the_pr_says_so(tmp_path: Path) -> None:
    service = LaunchDarklyService(mode="auto", policy=policy(), target_repo=tmp_path, environ={})
    record = service.ensure(_job())
    assert record.mode == "offline"
    assert record.required is True
    assert "LAUNCHDARKLY_API_TOKEN is unset" in record.detail

    job = bare_job(tmp_path, feature=feature(user_facing=True, flag_key="decor-room-mood"))
    job.launchdarkly = record
    body = render_pr(job, "")
    assert "Live LaunchDarkly check skipped" in body
    assert FLAG_URL not in body

    job.launchdarkly = record.model_copy(update={"mode": "live", "url": FLAG_URL, "created": True})
    live_body = render_pr(job, "")
    assert FLAG_URL in live_body
    assert "## Rollout" in live_body
    assert "Created in LaunchDarkly" not in live_body
    assert "separate human release decision" in live_body


def test_offline_mode_does_not_call_launchdarkly(tmp_path: Path) -> None:
    memory = MemoryLaunchDarkly(None)
    record = LaunchDarklyService(
        mode="offline",
        policy=policy(),
        target_repo=tmp_path,
        client=memory,
        environ={"LAUNCHDARKLY_API_TOKEN": SECRET},
    ).ensure(_job())
    assert record.mode == "offline"
    assert memory.calls == []
    assert SECRET not in record.detail


def test_live_mode_without_a_token_fails(tmp_path: Path) -> None:
    service = LaunchDarklyService(mode="live", policy=policy(), target_repo=tmp_path, environ={})
    with pytest.raises(FactoryError, match="LAUNCHDARKLY_API_TOKEN"):
        service.ensure(_job())


def _job() -> JobState:
    return bare_job(
        Path("."),
        id="job-abc12345",
        stage=Stage.ENSURE_FLAG,
        feature=feature(
            user_facing=True,
            flag_key="decor-room-mood",
            title="Show a room mood line on capabilities",
        ),
        plan=low_plan(),
    )


def _service(repo: Path, memory: MemoryLaunchDarkly) -> LaunchDarklyService:
    return LaunchDarklyService(
        mode="live",
        policy=policy(),
        target_repo=repo,
        client=memory,
        environ={"LAUNCHDARKLY_API_TOKEN": SECRET},
    )


def boolean_flag(
    *,
    on: bool,
    off_index: int = 1,
    tags: list[str] | None = None,
    description: str = "",
) -> dict[str, Any]:
    return {
        "key": "decor-room-mood",
        "name": "Show a room mood line on capabilities",
        "kind": "boolean",
        "temporary": True,
        "description": description,
        "tags": list(tags or []),
        "variations": [
            {"_id": "var-true", "value": True},
            {"_id": "var-false", "value": False},
        ],
        "environments": {
            "test": {"on": on, "offVariation": off_index},
            "production": {"on": on, "offVariation": off_index},
        },
    }


class MemoryLaunchDarkly:
    def __init__(
        self,
        flag: dict[str, Any] | None,
        *,
        stick_on: bool = False,
        hide_until_create: bool = False,
    ) -> None:
        self.flag = flag
        self.stick_on = stick_on
        self.hidden = hide_until_create
        self.calls: list[tuple[str, str, dict[str, Any], bool]] = []

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        semantic: bool = False,
        query: list[tuple[str, str]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        payload = body or {}
        self.calls.append((method, path, payload, semantic))
        if method == "GET":
            if self.flag is None or self.hidden:
                return 404, {"message": "not found"}
            return 200, copy.deepcopy(self.flag)
        if method == "POST":
            if self.flag is not None:
                self.hidden = False
                return 409, {"message": "flag already exists"}
            self.flag = boolean_flag(
                on=False,
                tags=list(payload.get("tags") or []),
                description=str(payload.get("description") or ""),
            )
            return 201, copy.deepcopy(self.flag)
        if method == "PATCH" and semantic and self.flag is not None:
            if "environmentKey" in payload:
                if not self.stick_on:
                    env = self.flag["environments"][payload["environmentKey"]]
                    env["on"] = False
                    env["offVariation"] = 1
            else:
                for instruction in payload["instructions"]:
                    if instruction["kind"] == "addTags":
                        for tag in instruction["values"]:
                            if tag not in self.flag["tags"]:
                                self.flag["tags"].append(tag)
                    if instruction["kind"] == "updateDescription":
                        self.flag["description"] = instruction["value"]
            return 200, copy.deepcopy(self.flag)
        raise AssertionError(method)
