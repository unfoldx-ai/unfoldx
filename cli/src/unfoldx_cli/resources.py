"""Resource commands from the shipped OpenAPI: one parameter/description source for CLI and MCP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .api import quote
from .errors import UnfoldxError

DOCUMENT = json.loads(Path(__file__).with_name("openapi.json").read_text(encoding="utf-8"))
OPERATIONS = {
    op["x-mcp"]: (path, method.upper(), op)
    for path, methods in DOCUMENT["paths"].items()
    for method, op in methods.items()
    if isinstance(op, dict) and "x-cli" in op
}


def parameters(op):
    return op.get("parameters", [])


def request_schema(op):
    return (
        op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    )


def input_schema(op):
    body = request_schema(op)
    props = dict(body.get("properties", {}))
    required = list(body.get("required", []))
    for p in parameters(op):
        name = "command_id" if p["in"] == "header" else p["name"]
        props[name] = p["schema"]
        if p.get("required"):
            required.append(name)
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": body.get("additionalProperties", False),
    }


def _bool(value):
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("use true or false")
    return value == "true"


def _json(value):
    try:
        return json.loads(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected JSON") from None


def add_parsers(sub, common, existing):
    groups = dict(existing)
    for name, (path, method, op) in OPERATIONS.items():
        group, action = op["x-cli"]
        if group not in groups:
            groups[group] = sub.add_parser(
                group, parents=[common], help=group + " resources"
            ).add_subparsers(dest=group + "_command", required=True)
        parser = groups[group].add_parser(
            action, parents=[common], help=op["summary"], description=op["description"]
        )
        parser.set_defaults(resource_operation=name)
        schema = input_schema(op)
        path_names = {p["name"] for p in parameters(op) if p["in"] == "path"}
        for field, spec in schema["properties"].items():
            flag = (
                field
                if field in path_names
                else "--"
                + (
                    "yes"
                    if field == "confirm"
                    else "query"
                    if field == "q"
                    else field.replace("_", "-")
                )
            )
            options = {"help": spec.get("description", field.replace("_", " "))}
            if field == "confirm":
                options.update(action="store_true", default=False)
            else:
                typ = spec.get("type", "string")
                options["type"] = {
                    "integer": int,
                    "number": float,
                    "boolean": _bool,
                    "object": _json,
                    "array": _json,
                }.get(typ, str)
                if "enum" in spec:
                    options["choices"] = spec["enum"]
                if field not in path_names:
                    options["default"] = None
            if field not in path_names:
                options["dest"] = field
            parser.add_argument(flag, **options)
        if request_schema(op):
            parser.add_argument(
                "--input-json",
                help="request body from a UTF-8 JSON file or -; do not mix with body flags",
            )
    return groups


def invoke(client, name, values):
    path, method, op = OPERATIONS[name]
    header = {}
    query = {}
    body = {}
    values = dict(values)
    if (
        request_schema(op).get("properties", {}).get("confirm")
        and values.get("confirm") is not True
    ):
        raise UnfoldxError(
            "usage",
            "Explicit confirmation required: --yes / confirm=true",
            code="confirmation_required",
        )
    missing = [k for k in input_schema(op)["required"] if values.get(k) is None]
    if missing:
        raise UnfoldxError("usage", "Missing: " + ", ".join(missing), code="invalid_arguments")
    for p in parameters(op):
        key = "command_id" if p["in"] == "header" else p["name"]
        value = values.pop(key, None)
        if value is None:
            continue
        if p["in"] == "path":
            path = path.replace("{" + p["name"] + "}", quote(value))
        elif p["in"] == "query":
            query[p["name"]] = value
        else:
            header[p["name"]] = value
    body = {k: v for k, v in values.items() if v is not None}
    if method == "GET":
        return client.get(path, **query)
    try:
        return client.request(
            method, path, body=body, retries=2 if op.get("x-idempotent") else 0, headers=header
        )
    except UnfoldxError as exc:
        key = header.get("Idempotency-Key") or body.get("command_id")
        if key:
            exc.details = {
                **(exc.details or {}),
                "command_id": key,
                "recovery_action": "Repeat this exact operation with the same command_id and unchanged body.",
            }
            exc.details.pop("recovery_command", None)
        raise


def dispatch(args, client, emit):
    _, _, op = OPERATIONS[args.resource_operation]
    fields = input_schema(op)["properties"]
    values = {k: getattr(args, k, None) for k in fields}
    source = getattr(args, "input_json", None)
    if source:
        body_keys = set(request_schema(op).get("properties", {}))
        if any(values.get(k) is not None and k != "confirm" for k in body_keys):
            raise UnfoldxError(
                "usage", "Do not mix --input-json with body flags", code="invalid_arguments"
            )
        try:
            raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise UnfoldxError(
                "usage", "Invalid JSON input: " + str(exc), code="invalid_json"
            ) from None
        if not isinstance(data, dict):
            raise UnfoldxError("usage", "Request body must be an object", code="invalid_json")
        if not request_schema(op).get("additionalProperties", False) and set(data) - body_keys:
            raise UnfoldxError(
                "usage",
                "Unknown body fields: " + ", ".join(sorted(set(data) - body_keys)),
                code="invalid_arguments",
            )
        # --yes is deliberate CLI consent even when the rest of the body comes from a file.
        values.update(data)
        if getattr(args, "confirm", False):
            values["confirm"] = True
    result = invoke(client, args.resource_operation, values)
    emit.result(result)
    return 0
