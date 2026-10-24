"""Ordinary MCP tools for external agents, with a host-owned task and catalog.

This observes tool-boundary history, not an external model's hidden context.
One bridge belongs to one task/connection. There is deliberately no reset tool.
"""
import asyncio
import re

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from ..common import RuntimeFault, canonical
from ..contracts import (BatchRequest, FinalizeRequest, Message, ModelOutputRequest,
    PrepareTurnRequest, ToolProposal, make)


class ExternalAgentBridge:
    def __init__(self, runtime, request, *, model_destination_id, on_change=None, on_final=None):
        self.runtime, self.request = runtime, request
        self.destination = model_destination_id
        self.on_change = on_change
        self.on_final = on_final
        self.session_id = None
        self.terminal = None
        self.last_reason_code = None
        self.messages = []
        self.client_id = None
        self.lock = asyncio.Lock()
        names = [m.tool_id for m in request.tool_manifests]
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', n) for n in names):
            raise ValueError('External MCP tool names must match [A-Za-z0-9_-]{1,64}')
        if {'guard_status', 'guard_finalize'} & set(names):
            raise ValueError('Reserved MCP tool name')
        if any(m.artifact_parameters for m in request.tool_manifests):
            # Opaque capability tokens need a host adapter, not string guessing.
            raise ValueError('Artifact-parameter tools require the RuntimePort host adapter')

    def status(self):
        return {'session_id': self.session_id, 'terminal': self.terminal,
            'last_reason_code': self.last_reason_code,
            'scope': 'MCP tools and explicitly submitted final text only',
            'released_observations': len(self.messages)}

    def changed(self):
        if self.on_change:
            self.on_change(self.status())

    async def prepare(self, content, proposals):
        if self.session_id is None:
            session = await self.runtime.create_session(self.request)
            self.session_id = session.session_id
        prepared = await self.runtime.prepare_model_turn(make(PrepareTurnRequest,
            session_id=self.session_id, messages=self.messages, model_destination_id=self.destination))
        output = await self.runtime.record_model_output(make(ModelOutputRequest,
            session_id=self.session_id, model_call_id=prepared.model_call_id,
            context_version=prepared.context_version, content=content, tool_proposals=proposals))
        return prepared, output

    async def call(self, name, arguments, *, client_id=None):
        async with self.lock:
            try:
                # When the host supplies a session ID, never mix its chats.
                if self.client_id is not None and client_id != self.client_id:
                    raise RuntimeFault('MCP_CLIENT_SESSION_MISMATCH')
                if client_id is not None:
                    self.client_id = client_id
                if name == 'guard_status':
                    if arguments:
                        raise RuntimeFault('INVALID_ARGUMENTS')
                    return self.result({'status': 'RELEASED', **self.status()})
                if self.terminal:
                    raise RuntimeFault(self.terminal)
                # Validation errors never start a paid model call or dispatch.
                if name == 'guard_finalize':
                    if (not isinstance(arguments, dict) or set(arguments) != {'text'}
                            or not isinstance(arguments['text'], str)):
                        raise RuntimeFault('INVALID_ARGUMENTS')
                else:
                    entry = self.runtime.registry.get(name)
                    self.runtime.registry.validate_args(entry.manifest, arguments)
            except Exception as exc:
                return self.error(exc)
            try:
                if name == 'guard_finalize':
                    prepared, output = await self.prepare(arguments['text'], [])
                    value = await self.runtime.finalize(make(FinalizeRequest,
                        session_id=self.session_id, turn_id=prepared.turn_id,
                        context_version=prepared.context_version, output_ref=output.output_ref))
                    self.terminal = 'MCP_TASK_FINISHED' if value.status == 'RELEASED' else 'MCP_FINAL_NOT_RELEASED'
                    if value.status == 'RELEASED' and self.on_final:
                        self.on_final({'status': value.status, 'reason_code': value.reason_code,
                            'content': value.content, 'session_id': self.session_id,
                            'artifact_ref': value.artifact_ref.model_dump(mode='json')})
                else:
                    effects = entry.manifest.effects
                    use = 'send' if 'SEND' in effects else 'write' if set(effects) & {'WRITE', 'DELETE'} else 'analyze'
                    proposal = make(ToolProposal, tool_id=name, manifest_version=entry.manifest.manifest_version,
                        args=arguments, artifact_refs=[], use=use)
                    prepared, output = await self.prepare(canonical({'tool': name, 'args': arguments}), [proposal])
                    batch = await self.runtime.execute_batch(make(BatchRequest,
                        session_id=self.session_id, turn_id=prepared.turn_id,
                        context_version=prepared.context_version, calls=output.calls))
                    value = batch.results[output.calls[0].call_id]
                    if value.status == 'RELEASED':
                        self.messages.append(make(Message, role='user', ref=value.artifact_ref))
                    elif value.status in ('UNKNOWN', 'REVIEW_PENDING') or value.risk_refs:
                        self.terminal = 'MCP_NEW_TASK_REQUIRED'
                self.last_reason_code = value.reason_code
                self.changed()
                return self.result({'status': value.status, 'reason_code': value.reason_code,
                    'content': value.content if value.status == 'RELEASED' else None,
                    'session_id': self.session_id, 'terminal': self.terminal})
            except BaseException as exc:
                # Cancellation may happen after a side effect. Never permit replay.
                self.terminal = 'MCP_NEW_TASK_REQUIRED'
                self.last_reason_code = exc.code if isinstance(exc, RuntimeFault) else 'MCP_SERVICE_UNAVAILABLE'
                self.changed()
                if not isinstance(exc, Exception):
                    raise
                return self.error(exc)

    @staticmethod
    def result(value):
        return CallToolResult(content=[TextContent(type='text', text=canonical(value))],
            structuredContent=value, isError=value['status'] != 'RELEASED')

    @classmethod
    def error(cls, exc):
        # Never echo schema errors, provider responses, arguments or tool errors.
        code = exc.code if isinstance(exc, RuntimeFault) else 'MCP_SERVICE_UNAVAILABLE'
        return cls.result({'status': 'ERROR', 'reason_code': code, 'content': None})


def create_external_server(bridge):
    server = Server('agentguard', instructions='Use the guarded tools for this host-authorized task. '
        'Use absolute file paths. Submit your final text to guard_finalize. '
        'After a terminal result, stop and ask the host to start a new task. '
        'The guard does not control your hidden context or text outside these tools.')

    @server.list_tools()
    async def listing():
        tools = [Tool(name=m.tool_id, description=f'Policy and review checked {m.tool_id}; effects: {", ".join(m.effects)}.',
            inputSchema=m.parameters_schema) for m in bridge.request.tool_manifests]
        tools.extend([
            Tool(name='guard_status', description='Get task status without calling a model.',
                inputSchema={'type': 'object', 'properties': {}, 'additionalProperties': False}),
            Tool(name='guard_finalize', description='Review submitted final text and close this task. Return only released content.',
                inputSchema={'type': 'object', 'properties': {'text': {'type': 'string'}},
                    'required': ['text'], 'additionalProperties': False})])
        return tools

    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        meta = server.request_context.meta
        values = meta.model_dump() if meta is not None else {}
        client_id = values.get('ai.opencode/sessionID')
        return await bridge.call(name, arguments, client_id=client_id)

    return server
