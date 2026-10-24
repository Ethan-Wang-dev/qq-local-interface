"""Regenerate the checked-in operation schema from the versioned operation set."""
import json
from pathlib import Path

from qq_local_interface.protocol import OPERATIONS

ROOT = Path(__file__).resolve().parents[1] / 'schemas'
string = {'type': 'string', 'minLength': 1}
nullable_string = {'type': ['string', 'null']}
strings = {'type': ['array', 'null'], 'items': string, 'minItems': 1}
cursor = {'type': ['string', 'null'], 'maxLength': 16384}
params = {name: string for name in ('database', 'table', 'dataset_id', 'asset_id', 'item_id', 'output', 'cell_ref', 'conversation_id', 'favorite_id')}
params.update({name: strings for name in ('conversation_ids', 'author_ids', 'kinds')})
params.update({name: nullable_string for name in ('query', 'category', 'previous')})
params.update({name: {'type': ['string', 'null'], 'format': 'date-time'} for name in ('start', 'end')})
params.update({name: {'type': 'boolean'} for name in ('allow_partial', 'include_raw', 'include_internal', 'require_details')})
params.update(mode={'enum': ['full', 'incremental']}, limit={'type': 'integer', 'minimum': 1, 'maximum': 5000},
    offset={'type': 'integer', 'minimum': 0}, length={'type': 'integer', 'minimum': 1, 'maximum': 1048576}, cursor=cursor,
    equals={'type': ['object', 'null'], 'additionalProperties': {'oneOf': [
        {'type': ['string', 'number', 'null']}, {'type': 'object', 'required': ['type'], 'properties': {
            'type': {'enum': ['null', 'integer', 'real', 'text', 'blob']},
            'value': {'type': ['string', 'null']}, 'encoding': {'enum': ['base64', 'utf8']}, 'data': {'type': 'string'}}, 'additionalProperties': False}]}},
    directions={**strings, 'items': {'enum': ['incoming', 'outgoing', 'unknown']}},
    qualities={**strings, 'items': {'enum': ['complete', 'metadata_only', 'unsupported', 'decode_error']}},
    entity_types={**strings, 'items': {'enum': ['actors', 'conversations', 'messages', 'memberships', 'databases', 'assets', 'favorites']}})
required = {'groups.members': ['conversation_id'], 'messages.search': ['query'], 'messages.export': ['output'],
    'messages.get': ['item_id'], 'tables.list': ['database'], 'records.read': ['database', 'table'],
    'records.export': ['database', 'table', 'output'], 'cells.read': ['cell_ref'],
    'assets.read': ['asset_id'], 'datasets.read': ['dataset_id'], 'favorites.get': ['favorite_id'], 'favorites.export': ['output']}
envelope = json.loads((ROOT / 'qq.local.protocol.v1.json').read_text())['$defs']['request']
branches = []
for operation, names in OPERATIONS.items():
    properties = {n: params[n] for n in sorted(names)}
    if operation == 'messages.search':
        properties['query'] = string
    if operation.startswith('conversations.'):
        properties['kinds'] = {**strings, 'items': {'enum': ['person', 'group']}}
    branch = {**envelope, 'properties': {**envelope['properties'], 'operation': {'const': operation},
        'params': {'type': 'object', 'properties': properties, 'required': required.get(operation, []), 'additionalProperties': False}},
        'required': envelope['required'] + (['params'] if operation in required else [])}
    branches.append(branch)
schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'urn:qq:local:operations:v1',
          'title': 'Operation-specific QQ Local Interface requests', 'oneOf': branches}
(ROOT / 'qq.local.operations.v1.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
