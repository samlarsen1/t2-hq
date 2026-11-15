"""The agent calls its read-only tools and answers from what they return."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from t2hq.agent import INSTRUCTIONS, Deps, build_agent
from t2hq.config import Settings
from t2hq.droids import Droids
from t2hq.llm import build_model
from t2hq.pacing import PacedModel


def scripted(tool: str, args: dict[str, Any]) -> tuple[FunctionModel, list[Any]]:
    """Call ``tool`` once, then answer with what it returned."""
    returned: list[Any] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    returned.append(part.content)
                    return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    return FunctionModel(respond), returned


async def test_tools_are_read_only(droids: Droids) -> None:
    model, _ = scripted("status", {})
    agent = build_agent(model)
    names = set(agent._function_toolset.tools)  # noqa: SLF001 - checking the tool list
    assert names == {
        "status",
        "last_report",
        "recent_runs",
        "show_config",
        "dry_run",
        "suggestions",
    }
    assert "No tool can place, change or cancel an order" in INSTRUCTIONS


async def test_status_tool(droids: Droids) -> None:
    model, returned = scripted("status", {})
    result = await build_agent(model).run("how are things?", deps=Deps(droids))
    assert result.output == "done"
    assert returned[0]["droids"]["t2-d2"]["last_run"]["status"] == "ok"


async def test_dry_run_tool(droids: Droids, apps: Path) -> None:
    model, returned = scripted("dry_run", {"droid": "t2-d2", "session": "eu_morning"})
    await build_agent(model).run("dry-run d2", deps=Deps(droids))
    assert returned[0]["summary"] == "3 decisions, 0 trades simulated"
    assert "--dry-run" in (apps / "t2-d2/.venv/bin/calls.log").read_text()


async def test_tool_errors_come_back_as_text(droids: Droids) -> None:
    model, returned = scripted("dry_run", {"droid": "t2-d2"})
    await build_agent(model).run("dry-run d2", deps=Deps(droids))
    assert "needs a session" in returned[0]


async def test_large_results_are_clipped(droids: Droids, apps: Path) -> None:
    big = {"status": "ok", "result": {"decisions": [{"reason": "x" * 100}] * 200}}
    (apps / "t2-d2/var/runs/20261120T000000Z-big.json").write_text(json.dumps(big))
    model, returned = scripted("last_report", {"droid": "t2-d2"})
    await build_agent(model).run("last d2 run?", deps=Deps(droids))
    assert returned[0].endswith("(truncated)") and len(returned[0]) < 7000


def test_model_toggle() -> None:
    gemini = build_model(Settings(google_api_key=SecretStr("k"), _env_file=None))  # type: ignore[call-arg]
    assert isinstance(gemini, PacedModel)
    fallback = build_model(
        Settings(google_api_key=SecretStr("k"), ollama_fallback=True, _env_file=None)  # type: ignore[call-arg]
    )
    assert isinstance(fallback, FallbackModel)
    local = build_model(Settings(model_provider="ollama", _env_file=None))  # type: ignore[call-arg]
    assert not isinstance(local, PacedModel | FallbackModel)
