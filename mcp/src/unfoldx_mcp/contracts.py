"""MCP JSON Schemas from the packaged public API contract; no second set of response models."""
from unfoldx_cli.resources import DOCUMENT, OPERATIONS as RESOURCE_OPERATIONS

OPERATIONS = {operation['operationId']: operation for path in DOCUMENT['paths'].values()
              for operation in path.values() if isinstance(operation, dict) and 'operationId' in operation}
TOOL_OPERATIONS = dict(zip(
    ('capabilities', 'whoami', 'workspaces_list', 'workspace_get', 'run_status', 'run_events', 'run_questions',
     'run_result', 'run_usage', 'artifact_get', 'artifact_access', 'material_upload', 'material_get',
     'run_submit', 'command_get', 'run_answer', 'run_cancel'),
    ('capabilities', 'me', 'listWorkspaces', 'getWorkspace', 'getRun', 'getRunEvents', 'getRunQuestions',
     'getRunResult', 'getRunUsage', 'getArtifact', 'accessArtifact', 'completeMaterialUpload', 'getMaterial',
     'submitRun', 'getCommand', 'answerRun', 'cancelRun'), strict=True))

TOOL_OPERATIONS.update({name.removeprefix('unfoldx_'): op['operationId'] for name, (_, _, op) in RESOURCE_OPERATIONS.items()})


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


def output_schema(tool_name):
    if tool_name in DOCUMENT.get('x-local-tools', {}):
        data = schema_refs(DOCUMENT['x-local-tools'][tool_name]['output'])
        return with_definitions({'type':'object','oneOf':[{'type':'object','required':['schema','ok','data'],
            'properties':{'schema':{'const':'unfoldx.v2'},'ok':{'const':True},'data':data}}, {'$ref':'#/$defs/Failure'}]})
    operation = OPERATIONS[TOOL_OPERATIONS[tool_name.removeprefix('unfoldx_')]]
    response = next(value for code, value in operation['responses'].items() if str(code).startswith('2'))
    if '$ref' in response:
        response = DOCUMENT['components']['responses'][response['$ref'].rsplit('/', 1)[1]]
    return with_definitions({'type': 'object', 'oneOf': [schema_refs(response['content']['application/json']['schema']),
                                       {'$ref': '#/$defs/Failure'}]})
