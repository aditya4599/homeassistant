"""Computer-control tools. Every target must be in an allowlist from configuration."""

from __future__ import annotations

import os
from typing import Any

from app.services.computer.controller import ComputerController
from app.services.devices_alias import best_key
from app.services.llm.tools import ToolDefinition
from app.services.tools.base import Tool, ToolResult, arg_str, schema


def parse_mapping(raw: str) -> dict[str, str]:
    """``"spotify=Spotify; chrome=Google Chrome"`` -> ``{"spotify": "Spotify", ...}``."""
    mapping: dict[str, str] = {}
    for item in raw.replace("\n", ";").split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def computer_tools(
    controller: ComputerController,
    *,
    apps: dict[str, str],
    websites: dict[str, str],
    projects: dict[str, str],
    project_app: str = "",
) -> list[Tool]:
    def pick(kind: str, table: dict[str, str], spoken: str | None) -> tuple[str, str] | ToolResult:
        if not spoken:
            raise ValueError(f"{kind} is required")
        key = best_key(table, spoken)
        if key is None:
            return ToolResult(
                ok=False, say=f"{spoken.strip().capitalize()} isn't on my list of allowed {kind}s.",
                error_code="not_allowed", data={"allowed": sorted(table)},
            )  # fmt: skip
        return key, table[key]

    async def open_app(args: dict[str, Any]) -> ToolResult:
        spoken = arg_str(args, "name")
        # "Open YouTube" can land here: fall through to allowlisted websites.
        if spoken and best_key(apps, spoken) is None and best_key(websites, spoken):
            return await open_website({"name": spoken})
        picked = pick("app", apps, spoken)
        if isinstance(picked, ToolResult):
            return picked
        key, app_name = picked
        await controller.open_app(app_name)
        return ToolResult(ok=True, say=f"Opening {key.title() if key.islower() else key}.",
                          data={"app": app_name})

    async def close_app(args: dict[str, Any]) -> ToolResult:
        picked = pick("app", apps, arg_str(args, "name"))
        if isinstance(picked, ToolResult):
            return picked
        key, app_name = picked
        await controller.close_app(app_name)
        return ToolResult(ok=True, say=f"Closing {key.title()}.", data={"app": app_name})

    async def open_website(args: dict[str, Any]) -> ToolResult:
        picked = pick("website", websites, arg_str(args, "name"))
        if isinstance(picked, ToolResult):
            return picked
        key, url = picked
        await controller.open_url(url)
        return ToolResult(ok=True, say=f"Opening {key.title()}.", data={"url": url})

    async def open_project(args: dict[str, Any]) -> ToolResult:
        spoken = arg_str(args, "name") or (next(iter(projects)) if len(projects) == 1 else None)
        picked = pick("project", projects, spoken)
        if isinstance(picked, ToolResult):
            return picked
        key, path = picked
        await controller.open_path(os.path.expanduser(path), project_app or None)
        return ToolResult(ok=True, say=f"Opening your {key} project.", data={"project": key})

    tools: list[Tool] = []
    name_arg = {"type": "string", "description": "Name as the user said it."}
    if apps:
        listed = ", ".join(sorted(apps))
        tools += [
            Tool(ToolDefinition("open_app", f"Open an allowed app on the computer. Allowed: {listed}.",
                                schema({"name": name_arg}, ["name"])), open_app),
            Tool(ToolDefinition("close_app", f"Quit an allowed app on the computer. Allowed: {listed}.",
                                schema({"name": name_arg}, ["name"])), close_app),
        ]  # fmt: skip
    if websites:
        tools.append(Tool(ToolDefinition(
            "open_website", f"Open an allowed website in the browser. Allowed: {', '.join(sorted(websites))}.",
            schema({"name": name_arg}, ["name"])), open_website))  # fmt: skip
    if projects:
        tools.append(Tool(ToolDefinition(
            "open_project", f"Open one of the user's code projects. Allowed: {', '.join(sorted(projects))}.",
            schema({"name": name_arg})), open_project))  # fmt: skip
    return tools
