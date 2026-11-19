"""MCP tools and their JSON Schemas from the packaged public API contract; no second set of models.

`x-mcp-tools` in openapi.json names every tool, its toolset and the operations it runs. A tool with several
operations takes `method` to pick one; reads and writes never share a tool, so read-only filtering and
annotations stay per tool.
"""
from unfoldx_cli.resources import DOCUMENT, OPERATIONS, input_schema

TOOLS = DOCUMENT['x-mcp-tools']
TOOLSETS = sorted({spec['toolset'] for spec in TOOLS.values()})
DEFAULT_TOOLSETS = frozenset({'core'})


def schema_refs(value):
    if isinstance(value, dict):
        return {k: v.replace('#/components/schemas/', '#/$defs/') if k == '$ref' else schema_refs(v)
                for k, v in value.items()}
    return [schema_refs(v) for v in value] if isinstance(value, list) else value


DEFINITIONS = schema_refs(DOCUMENT['components']['schemas'])


def with_definitions(schema):
    used = {}
    def visit(value):
        if isinstance(value, dict):
            ref = value.get('$ref', '')
            if ref.startswith('#/$defs/'):
                name = ref.rsplit('/', 1)[1]
                if name not in used:
                    used[name] = DEFINITIONS[name]
                    visit(used[name])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)
    return {**schema, '$defs': used}


def operations(tool_name):
    """{method: operationId}; a single-operation tool has the one method ''."""
    return TOOLS[tool_name].get('operations', {})


def _success(operation_id):
    operation = OPERATIONS[operation_id][2]
    response = next(value for code, value in operation['responses'].items() if str(code).startswith('2'))
    if '$ref' in response:
        response = DOCUMENT['components']['responses'][response['$ref'].rsplit('/', 1)[1]]
    return schema_refs(response['content']['application/json']['schema'])


def operation_output_schema(operation_id):
    return with_definitions({'type': 'object', 'oneOf': [_success(operation_id), {'$ref': '#/$defs/Failure'}]})


def output_schema(tool_name):
    local = TOOLS[tool_name].get('local')
    if isinstance(local, str):
        successes = [_success(local)]
    elif local is not None:
        successes = [{'type': 'object', 'required': ['schema', 'ok', 'data'],
                      'properties': {'schema': {'const': 'unfoldx.v2'}, 'ok': {'const': True}, 'data': schema_refs(local)}}]
    else:
        successes = [_success(operation_id) for operation_id in operations(tool_name).values()]
    success = successes[0] if len(successes) == 1 else {'anyOf': successes}
    return with_definitions({'type': 'object', 'oneOf': [success, {'$ref': '#/$defs/Failure'}]})


def operation_input_schema(operation_id):
    return with_definitions(schema_refs(input_schema(OPERATIONS[operation_id][2])))


def tool_input_schema(tool_name):
    ops = operations(tool_name)
    if list(ops) == ['']:
        return operation_input_schema(ops[''])
    properties = {'method': {'enum': list(ops)}}
    for operation_id in ops.values():
        for name, spec in schema_refs(input_schema(OPERATIONS[operation_id][2]))['properties'].items():
            properties.setdefault(name, spec)
    return with_definitions({'type': 'object', 'properties': properties, 'required': ['method'],
                             'additionalProperties': False})


def operation_for(tool_name, arguments):
    """The operation a call runs and the arguments it takes, without `method`."""
    ops = operations(tool_name)
    if list(ops) == ['']:
        return ops[''], dict(arguments)
    values = dict(arguments)
    return ops[values.pop('method')], values


def description(tool_name):
    spec = TOOLS[tool_name]
    ops = operations(tool_name)
    if list(ops) == ['']:
        op = OPERATIONS[ops['']][2]
        text = spec.get('description') or op.get('description') or op['summary']
        return text + (' Required scope: ' + op['x-scope'] if op.get('x-scope') else '')
    lines = [spec['description'], 'method:']
    for method, operation_id in ops.items():
        op = OPERATIONS[operation_id][2]
        required = [name for name in input_schema(op)['required']]
        lines.append(f"- {method}: {op['summary']}" + (f" Needs {', '.join(required)}." if required else '')
                     + (f" Scope {op['x-scope']}." if op.get('x-scope') else ''))
    return '\n'.join(lines)


def hints(tool_name):
    ops = [OPERATIONS[operation_id] for operation_id in operations(tool_name).values()]
    return {'read_only_hint': all(method == 'GET' for _, method, _ in ops),
            'destructive_hint': any(op.get('x-destructive', False) for _, _, op in ops),
            'idempotent_hint': all(method == 'GET' or op.get('x-idempotent', False) for _, method, op in ops)}
