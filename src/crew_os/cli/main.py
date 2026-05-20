"""``crew`` command-line interface.

Commands:

    crew run "task description"        delegate a task to the crew
    crew status                        environment + agent overview
    crew dashboard                     serve the web dashboard + open browser
    crew audit-llm MODEL --probes ...  run garak against a model
    crew logs --trace ID               show audit events for a trace
    crew replay TRACE_ID               replay a trace's event sequence

Heavy work runs through the composition root in ``crew_os.cli.composition``;
commands reference it as a module attribute so tests can substitute a fake.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from crew_os import __version__
from crew_os.cli import composition
from crew_os.config import get_settings
from crew_os.core.exceptions import CrewOSError, OllamaError
from crew_os.core.models import Task, TraceContext

console = Console()


def _audit_path() -> Path:
    return get_settings().data_dir / "audit" / "audit.jsonl"


def _read_audit() -> list[dict[str, Any]]:
    path = _audit_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line:
                records.append(json.loads(line))
    return records


@click.group()
@click.version_option(__version__, prog_name="crew")
def cli() -> None:
    """Textual Crew OS - local AI agents controller."""


# ─────────────────────────────── run ───────────────────────────────────


@cli.command()
@click.argument("description")
def run(description: str) -> None:
    """Delegate a task described by DESCRIPTION to the crew."""

    asyncio.run(_run(description))


async def _run(description: str) -> None:
    crew = await composition.build_crew()
    try:
        task = Task(description=description, trace=TraceContext.new_root())
        result = await crew.delegator.delegate(task)
    finally:
        await crew.aclose()

    r = result.routing
    console.print(
        f"[bold]Routed[/bold] -> [cyan]{r.target_role.value}[/cyan] "
        f"(category={r.category.value}, complexity={r.complexity}, "
        f"escalated={'yes' if r.escalate_to_t2 else 'no'})"
    )
    if result.plan is not None:
        console.print(f"[magenta]Plan[/magenta] ({result.plan.source.value}):")
        console.print(result.plan.text)
    color = "green" if result.task.status.value == "completed" else "red"
    console.print(f"[bold {color}]Status: {result.task.status.value}[/bold {color}]")
    if result.task.error:
        console.print(f"[red]Error:[/red] {result.task.error}")
    if result.task.result:
        console.print_json(data=result.task.result)


# ─────────────────────────────── status ────────────────────────────────


@cli.command()
def status() -> None:
    """Show Ollama health, GPU, and registered agents."""

    asyncio.run(_status())


async def _status() -> None:
    from crew_os.metrics.gpu import sample_gpu  # noqa: PLC0415 - avoid GPU import at startup

    crew = await composition.build_crew()
    try:
        try:
            version = await crew.ollama.health()
            ollama_line = f"[green]reachable[/green] (v{version})"
        except OllamaError:
            ollama_line = "[red]unreachable[/red]"

        gpu = sample_gpu()
        gpu_line = (
            f"{gpu.name} - {gpu.vram_used_mb}/{gpu.vram_total_mb} MB, "
            f"{gpu.utilization_percent}% util"
            if gpu.available
            else "[yellow]no GPU[/yellow]"
        )

        console.print(f"Ollama:   {ollama_line}")
        console.print(f"GPU:      {gpu_line}")
        console.print(f"LAB_MODE: {'[red]ON[/red]' if crew.settings.lab_mode else 'off'}")

        table = Table(title="Agents")
        table.add_column("Role")
        table.add_column("Model")
        table.add_column("Capabilities")
        for role in sorted(crew.registry.roles(), key=lambda x: x.value):
            agent = crew.registry.require(role)
            table.add_row(role.value, agent.model, ", ".join(sorted(agent.capabilities)))
        console.print(table)
    finally:
        await crew.aclose()


# ─────────────────────────────── dashboard ─────────────────────────────


@cli.command()
@click.option("--no-browser", is_flag=True, help="Do not open a browser.")
def dashboard(no_browser: bool) -> None:
    """Serve the web dashboard and open it in a browser."""

    import webbrowser  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415 - heavy import, only when serving

    from crew_os.web.app import create_app  # noqa: PLC0415

    settings = get_settings()
    crew = asyncio.run(composition.build_crew(settings))
    app = create_app(
        registry=crew.registry,
        store=crew.store,
        bus=crew.bus,
        sampler=crew.sampler,
        usage=crew.usage,
        settings=crew.settings,
        chat_service=crew.chat_service,
    )
    url = f"http://{settings.web_host}:{settings.web_port}"
    console.print(f"[green]Serving dashboard at[/green] {url}")
    if not no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_level="warning")


# ─────────────────────────────── audit-llm ─────────────────────────────


@cli.command(name="audit-llm")
@click.argument("model")
@click.option("--probes", required=True, help="Comma-separated garak probe names.")
def audit_llm(model: str, probes: str) -> None:
    """Run garak probes against MODEL via the Auditor agent."""

    probe_list = [p.strip() for p in probes.split(",") if p.strip()]
    asyncio.run(_audit_llm(model, probe_list))


async def _audit_llm(model: str, probes: list[str]) -> None:
    from crew_os.core.models import AgentRole  # noqa: PLC0415

    crew = await composition.build_crew()
    try:
        auditor = crew.registry.require(AgentRole.AUDITOR)
        try:
            result = await auditor.invoke_tool(
                "run_garak",
                {"model_name": model, "probes": probes},
                TraceContext.new_root(),
            )
        except (CrewOSError, ValueError) as exc:
            console.print(f"[red]audit failed:[/red] {exc}")
            return
        if result.get("ok"):
            console.print(f"[green]garak completed[/green] -> {result.get('report_prefix')}")
        else:
            console.print(f"[yellow]garak reported issues[/yellow]: {result}")
    finally:
        await crew.aclose()


# ─────────────────────────────── logs ──────────────────────────────────


@cli.command()
@click.option("--trace", "trace_id", default=None, help="Filter by trace id.")
@click.option("--limit", default=50, help="Max events to show.")
def logs(trace_id: str | None, limit: int) -> None:
    """Show audit-log events, optionally filtered by --trace."""

    records = _read_audit()
    rows = []
    for rec in records:
        event = rec.get("event", {})
        trace = event.get("trace") or {}
        if trace_id and trace.get("trace_id") != trace_id:
            continue
        rows.append(rec)
    rows = rows[-limit:]

    if not rows:
        console.print("[yellow]no matching audit events[/yellow]")
        return

    table = Table(title="Audit Log")
    table.add_column("seq", justify="right")
    table.add_column("type")
    table.add_column("severity")
    table.add_column("agent")
    for rec in rows:
        event = rec.get("event", {})
        table.add_row(
            str(rec.get("seq")),
            str(event.get("type")),
            str(event.get("severity")),
            str(event.get("agent_role") or "-"),
        )
    console.print(table)


# ─────────────────────────────── replay ────────────────────────────────


@cli.command()
@click.argument("trace_id")
def replay(trace_id: str) -> None:
    """Replay the ordered event sequence for TRACE_ID."""

    records = _read_audit()
    seq = [
        rec
        for rec in records
        if (rec.get("event", {}).get("trace") or {}).get("trace_id") == trace_id
    ]
    if not seq:
        console.print(f"[yellow]no events for trace {trace_id}[/yellow]")
        return
    console.print(f"[bold]Replay of trace[/bold] [cyan]{trace_id}[/cyan] ({len(seq)} events)")
    for rec in seq:
        event = rec.get("event", {})
        console.print(
            f"  [{rec.get('ts')}] [bold]{event.get('type')}[/bold] "
            f"({event.get('severity')}) "
            f"{json.dumps(event.get('payload', {}), ensure_ascii=False)}"
        )


if __name__ == "__main__":
    cli()
