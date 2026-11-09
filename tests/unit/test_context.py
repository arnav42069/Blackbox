import json
from blackbox.core.context import complete_tool_messages,bound_context
from conftest import call


def test_partial_multicall_is_repaired_without_orphan_results():
    a=call('host.preflight')
    a['role']='assistant'
    a['tool_calls']+=call('net.ping',target='127.0.0.1')['tool_calls']
    tool={'role':'tool','tool_name':'host.preflight','content':'{}'}
    result=complete_tool_messages([{'role':'user','content':'goal'},a,tool])
    assert len(result[1]['tool_calls'])==1
    assert result[2]==tool
    assert complete_tool_messages([a])==[]


def test_context_trims_complete_old_groups_and_compacts_observations():
    history=[{'role':'system','content':'system'},{'role':'user','content':'old'*4000},{'role':'assistant','content':'answer'},{'role':'user','content':'current'},call('host.preflight'),{'role':'tool','tool_name':'host.preflight','content':json.dumps({'tool_id':'real-id','stdout':'x'*15000})}]
    bounded=bound_context(history,2048)
    assert bounded[0]['role']=='system'
    assert bounded[1]['content']=='current'
    assert json.loads(bounded[-1]['content'])['tool_id']=='real-id'
    assert len(bounded[-1]['content'])<2000
