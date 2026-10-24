import pytest
from conftest import build_harness, proposal
from agentguard.v2.adapters.host import HostAdapter, AgentReply
from agentguard.v2.common import RuntimeFault, canonical


class RepeatingModel:
    destination_id='agent'
    async def complete(self,prepared):
        self.last_input=prepared
        return AgentReply('private model reasoning',(proposal(),))
    async def reset(self,sid): return True


@pytest.mark.asyncio
async def test_repeated_completed_call_is_not_executed_and_stop_survives_restart(tmp_path):
    h=build_harness(tmp_path/'runtime'); model=RepeatingModel()
    host=HostAdapter(h.runtime,model,state_directory=tmp_path/'host')
    session=await host.create(h.request)
    with pytest.raises(RuntimeFault,match='HOST_REPEATED_CALL_LIMIT'):
        await host.run(session.session_id)
    assert h.reads==['docs']
    assert host.states[session.session_id].duplicate_proposals_stopped==2
    messages=model.last_input.messages
    assert messages[0].role=='system'
    assert 'host_tool_receipt' in canonical([m.model_dump(mode='json') for m in messages])
    assert 'private model reasoning' not in canonical([m.model_dump(mode='json') for m in messages])
    restored=HostAdapter(h.runtime,model,state_directory=tmp_path/'host')
    with pytest.raises(RuntimeFault,match='HOST_REPEATED_CALL_LIMIT'):
        await restored.run(session.session_id)
    assert h.reads==['docs']
