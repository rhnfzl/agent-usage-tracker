"""Run evidence recipes against an explicitly selected set of scopes."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

from .. import __version__
from ..config import AutError
from .access import Evidence, READ_OPERATIONS
from .semantic_search import LocalEmbedder
from .store import Store, canonical, digest

CONTRACT = 1
MAX_SOURCE = 16_384
MAX_PARAMS = 8_192
MAX_SCOPES = 8
MAX_CALLS = 48
MAX_DATA = 2 * 1024 * 1024
MAX_OUTPUT = 65_536
TIMEOUT = 12
MODEL_TIMEOUT = 30
WORKER = "agent_usage_tracker.improve.investigation"


class InvestigationTimeout(AutError):
    """The evidence worker exceeded the wall-clock limit."""


def require_runtime() -> None:
    if find_spec("pydantic_monty") is None:
        raise AutError("investigation: install agent-usage-tracker[code] to run recipes")


def _matches(value: Any, annotation: Any) -> bool:
    if get_origin(annotation) is Literal:
        return any(type(value) is type(choice) and value == choice
                   for choice in get_args(annotation))
    if get_origin(annotation) is list:
        element_type, = get_args(annotation)
        return type(value) is list and all(_matches(item, element_type) for item in value)
    if options := get_args(annotation):
        return any(_matches(value, option) for option in options)
    return type(value) is annotation


def catalog(*, with_model: bool = False) -> dict[str, Any]:
    operations = {}
    for name in READ_OPERATIONS:
        method = getattr(Evidence, name)
        hints = get_type_hints(method)
        fields = {}
        for param in list(inspect.signature(method).parameters.values())[1:]:
            field = {"type": inspect.formatannotation(hints[param.name])}
            if param.default is not inspect.Parameter.empty:
                field["default"] = "exact" if name == "search" and param.name == "mode" else param.default
            fields[param.name] = field
        operations[name] = {"description": method.__doc__, "arguments": fields}
    return {"contract": CONTRACT, "operations": operations,
            "interface": "read(operation, scope='1', lead=None, **arguments) returns receipt and data",
            "limits": {"source_bytes": MAX_SOURCE, "parameter_bytes": MAX_PARAMS,
                       "calls": MAX_CALLS, "seconds": MODEL_TIMEOUT if with_model else TIMEOUT,
                       "output_bytes": MAX_OUTPUT}}


def scope_specs(root: Path, content: str, audit: str | None, bundle: Path | None,
                audits: list[str], bundles: list[Path], model_path: Path | None = None) -> list[dict[str, Any]]:
    sources = [(audit, bundle), *((key, None) for key in audits),
               *((None, path) for path in bundles)]
    if len(sources) > MAX_SCOPES:
        raise AutError(f"investigation: select at most {MAX_SCOPES} evidence scopes")
    specs = [{"audit": key, "bundle": str(path.expanduser().resolve()) if path else None,
              "root": str(root.expanduser().resolve()) if key else None, "content": content,
              **({"embedding_model": str(model_path)} if model_path else {})}
             for key, path in sources]
    if len({(row["audit"], row["bundle"]) for row in specs}) != len(specs):
        raise AutError("investigation: select distinct evidence scopes")
    return specs


def _bounded(value: Any, limit: int, label: str) -> bytes:
    try:
        data = canonical(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AutError(f"investigation: {label} must be finite JSON") from exc
    if len(data) > limit:
        raise AutError(f"investigation: {label} exceeds {limit} bytes")
    return data


def _timeout(specs: list[dict[str, Any]]) -> int:
    return MODEL_TIMEOUT if any(spec.get("embedding_model") for spec in specs) else TIMEOUT


def run(source: str, params: dict[str, Any], specs: list[dict[str, Any]],
        expected_scopes: dict[str, Any] | None = None) -> dict[str, Any]:
    require_runtime()
    try:
        source_size = len(source.encode("utf-8"))
    except UnicodeError as exc:
        raise AutError("investigation: source must be UTF-8") from exc
    if not source.strip() or source_size > MAX_SOURCE:
        raise AutError(f"investigation: provide 1..{MAX_SOURCE} bytes of Python source")
    if not isinstance(params, dict):
        raise AutError("investigation: parameters must be a JSON object")
    _bounded(params, MAX_PARAMS, "parameters")
    payload = canonical({"source": source, "params": params, "specs": specs,
                         "expected_scopes": expected_scopes}).encode("utf-8")
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR")
           if name in os.environ}
    try:
        process = subprocess.run([sys.executable, "-I", "-m", WORKER], input=payload,
                                 capture_output=True, timeout=_timeout(specs), env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        raise InvestigationTimeout("investigation: execution exceeded the wall-clock limit") from exc
    except OSError as exc:
        raise AutError("investigation: could not start the local runtime") from exc
    if process.returncode:
        raise AutError("investigation: local runtime failed")
    try:
        response = json.loads(process.stdout)
    except (ValueError, RecursionError) as exc:
        raise AutError("investigation: local runtime returned an invalid response") from exc
    if "error" in response:
        raise AutError(response["error"])
    return dict(response)



def save(store: Store, source: str, params: dict[str, Any], value: dict[str, Any]) -> str:
    return store.put("investigation_runs", {
        "contract": CONTRACT, "source": source, "params": params, "scopes": value["scopes"],
        "result_sha256": digest(value), "reads": value["reads"],
        "aut_version": value["aut_version"], "runtime_version": value["runtime_version"],
    })


def _references(operation: str, value: dict[str, Any]) -> list[dict[str, str]]:
    references: dict[tuple[str, str], None] = {}

    def include(session: Any, *events: Any) -> None:
        if isinstance(session, str):
            for event in events:
                if isinstance(event, str):
                    references[session, event] = None

    if operation == "compare":
        difference = value["first_tool_difference"] or {}
        for side in ("baseline", "candidate"):
            row = value[side]
            tool = difference.get(side) or {}
            include(row["id"], *row["error_event_ids"], tool.get("call_event_id"),
                    tool.get("result_event_id"))
    elif operation == "event":
        include(value["session_id"], value["id"])
    elif operation == "context":
        events = [value["anchor"], value["counterpart"], *value["neighbors"]]
        include(value["session_id"], *(event["id"] for event in events if event))
    elif operation == "case":
        include(value["session_id"], *value["event_ids"])
    else:
        session = value.get("session", {}).get("id")
        for item in value.get("items", []):
            key = item.get("session_id") or item.get("session_snapshot") or session
            include(key, item.get("event_id") or item.get("id"), item.get("call_event_id"),
                    item.get("result_event_id"))
    return [{"session_id": session, "event_id": event} for session, event in references]


def _open_scope(spec: dict[str, Any]) -> Evidence:
    store = Store(Path(spec["root"]), read_only=True) if spec["root"] else None
    bundle = Path(spec["bundle"]) if spec["bundle"] else None
    model = spec.get("embedding_model")
    embedder = LocalEmbedder(Path(model)) if model else None
    return Evidence(spec["content"], store, spec["audit"], bundle, embedder=embedder)


def _describe_scope(evidence: Evidence) -> dict[str, Any]:
    result = {**evidence.describe(), "bundle": digest(evidence.bundled) if evidence.bundled else None}
    if evidence.embedding_model_id is not None:
        result["embedding_model"] = evidence.embedding_model_id
    return result


def pin_scopes(specs: list[dict[str, Any]], primary: Evidence) -> dict[str, Any]:
    scopes = {"1": _describe_scope(primary)}
    for i, spec in enumerate(specs[1:], 2):
        scopes[str(i)] = _describe_scope(_open_scope(spec))
    return scopes


def _execute(source: str, params: dict[str, Any], specs: list[dict[str, Any]],
             expected_scopes: dict[str, Any] | None) -> dict[str, Any]:
    from pydantic_monty import Monty, MontyError, MontyRuntimeError, MontySyntaxError

    sources = {str(i): _open_scope(spec) for i, spec in enumerate(specs, 1)}
    scopes = {alias: _describe_scope(evidence) for alias, evidence in sources.items()}
    if expected_scopes is not None and scopes != expected_scopes:
        raise AutError("investigation: evidence scope or content grant changed")

    reads: list[dict[str, Any]] = []
    printed: list[str] = []
    calls = data_bytes = print_bytes = 0

    def capture(stream: str, message: str) -> None:
        nonlocal print_bytes
        print_bytes += len(message.encode())
        if print_bytes > MAX_OUTPUT:
            raise AutError("investigation: printed output exceeds the output budget")
        printed.append(message)

    def read(operation: str, *, scope: str = "1", lead: int | None = None,
             **arguments: Any) -> dict[str, Any]:
        nonlocal calls, data_bytes
        calls += 1
        if calls > MAX_CALLS:
            raise AutError("investigation: evidence call budget exhausted")
        if not isinstance(operation, str) or operation not in READ_OPERATIONS:
            raise AutError("investigation: unknown evidence operation")
        if not isinstance(scope, str) or scope not in sources:
            raise AutError("investigation: unknown evidence scope")
        if lead is not None and (type(lead) is not int or not 1 <= lead <= len(reads)):
            raise AutError("investigation: lead must reference an earlier receipt")
        _bounded(arguments, MAX_PARAMS, "operation arguments")
        method = getattr(sources[scope], operation)
        hints = get_type_hints(method)
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as exc:
            raise AutError("investigation: invalid argument names for evidence operation") from exc
        if any(not _matches(value, hints[name]) for name, value in arguments.items()):
            raise AutError("investigation: invalid argument types for evidence operation")
        # Existing recipe receipts used literal search when mode was omitted.
        call_arguments = {"mode": "exact", **arguments} if operation == "search" else arguments
        data = method(**call_arguments)
        encoded = _bounded(data, MAX_DATA - data_bytes, "evidence data")
        data_bytes += len(encoded)
        receipt = len(reads) + 1
        has_more = any(data.get(key) is not None for key in ("next_cursor", "next_offset"))
        if operation == "context":
            events = [data["anchor"], data["counterpart"], *data["neighbors"]]
            has_more = any(event and event.get("next_offset") is not None for event in events)
        reads.append({"receipt": receipt, "lead": lead, "scope": scope, "operation": operation,
                      "arguments_sha256": digest(arguments), "data_sha256": digest(data),
                      "has_more": has_more,
                      "references": _references(operation, data)})
        return {"receipt": receipt, "data": data}

    try:
        with Monty(max_processes=1, request_timeout=_timeout(specs)) as pool, pool.checkout(
            limits={"max_feed_duration_secs": 3, "max_memory": 32 * 1024 * 1024,
                    "max_recursion_depth": 128, "max_suspensions": 256, "max_total_sleep_secs": 0},
            os_policy={"datetime": datetime(2000, 1, 1, tzinfo=timezone.utc),
                       "random_start": {"seed": 0}},
        ) as session:
            result = session.feed_run(source, inputs={"params": params, "scopes": scopes},
                                      external_lookup={"read": read}, print_callback=capture)
    except MontySyntaxError as exc:
        raise AutError(f"investigation: invalid Python syntax: {exc.display('type-msg')[:300]}") from exc
    except MontyRuntimeError as exc:
        error_type = type(exc.exception()).__name__
        raise AutError(f"investigation: program stopped with {error_type}") from exc
    except MontyError as exc:
        raise AutError("investigation: sandbox could not complete the program") from exc
    output = {"result": result, "stdout": "".join(printed)}
    _bounded(output, MAX_OUTPUT, "program output")
    _bounded(reads, MAX_DATA, "read receipts")
    return {**output, "contract": CONTRACT, "aut_version": __version__,
            "runtime_version": version("pydantic-monty"), "scopes": scopes, "reads": reads,
            "calls": calls, "read_errors": calls - len(reads), "evidence_bytes": data_bytes,
            "source_sha256": digest(source), "parameters_sha256": digest(params)}


def worker() -> None:
    try:
        request = json.load(sys.stdin.buffer)
        value = _execute(**request)
    except AutError as exc:
        value = {"error": str(exc)}
    sys.stdout.buffer.write(canonical(value).encode("utf-8") + b"\n")


if __name__ == "__main__":
    worker()
