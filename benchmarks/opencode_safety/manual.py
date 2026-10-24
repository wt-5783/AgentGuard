"""A free-form paired task using the existing isolated fixture and tool pack."""
import copy

from suite import catalog, materialize


def custom_case(value):
    if not isinstance(value, dict) or set(value)-{'prompt', 'authorizations', 'fixture', 'attack'}:
        raise ValueError('无效自定义任务')
    prompt=value.get('prompt')
    if not isinstance(prompt,str) or not prompt.strip() or '\0' in prompt or len(prompt.encode())>16000:
        raise ValueError('提示词不能为空，且不能超过 16000 字节')
    fixture=value.get('fixture','b-report')
    original=next((r for r in catalog() if r['id']==fixture),None)
    if original is None:
        raise ValueError('未知基础样例')
    attack=value.get('attack','native')
    if attack not in ('native','authority','encoded','plan'):
        raise ValueError('自定义任务请选择一种固定变体')
    row=materialize(original,attack)
    authority=value.get('authorizations',row['authorizations'])
    if not isinstance(authority,str) or not authority.strip() or len(authority.encode())>8000:
        raise ValueError('宿主授权不能为空，且不能超过 8000 字节')
    row=copy.deepcopy(row)
    row.update(id='manual',title='自定义提示词',kind='manual',group='manual',category='manual',
        channel='manual',task=prompt.strip(),authorizations=authority.strip(),oracle=None,
        source={'type':'manual','fixture':fixture,'fixture_variant':attack,
                'note':'自由提示词没有预设攻击目标，不纳入基准 ASR 或正常任务完成率。'})
    return row
