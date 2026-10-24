"""Bounded regeneration of malformed model output, never correction of a verdict.

Only protocol errors retry. Each request uses the original authorized input plus
host-authored format feedback; invalid model text is never promoted into input.
Transport attempts remain charged to the same client budget.
"""
from copy import deepcopy
from .errors import GuardError

REPAIRABLE = frozenset({'INVALID_SCHEMA', 'INVALID_REFERENCE', 'MISSING_EVIDENCE', 'INVALID_MODEL_OUTPUT'})


def combined_usage(usages):
    if not usages:
        return None
    result = {k: usages[-1].get(k, '') for k in ('provider', 'model')}
    for key in ('input_tokens', 'output_tokens'):
        result[key] = (sum(u[key] for u in usages) if all(type(u.get(key)) is int for u in usages) else 'unavailable')
    result['attempts'] = sum(u.get('attempts', 0) for u in usages)
    result['latency_ms'] = sum(u.get('latency_ms', 0) for u in usages)
    return result


async def complete_validated(client, messages, *, purpose, prompt_version, validate, attempts=None):
    retries = getattr(getattr(client, 'config', None), 'protocol_retries', 0)
    records = attempts if attempts is not None else []
    usages = []
    for index in range(retries + 1):
        current = deepcopy(messages)
        if index:
            current[0]['content'] += (
                '\nThe previous response failed protocol validation (' + records[-1]['reason_code'] + '). '
                'Regenerate valid JSON. Copy all required binding IDs and references exactly. '
                'For compact evidence format use only ref_id/span/json_pointer and cite E0. '
                'For the full format include the current subject_ref in evidence_refs. '
                'Do not change the task, relax a security decision, invent references, or execute anything.')
        record = {'protocol_attempt': index, 'messages': current, 'reason_code': None, 'usage': None}
        records.append(record)
        try:
            reply = await client.complete(messages=current, purpose=purpose, prompt_version=prompt_version)
            record['response'] = reply.content  # private trace only, never ordinary logs
            usages.append(reply.usage)
            record['usage'] = reply.usage
            result = validate(reply.content)
            return result, combined_usage(usages)
        except GuardError as exc:
            record['reason_code'] = exc.reason_code
            if record['usage'] is None and exc.usage:
                usages.append(exc.usage)
                record['usage'] = exc.usage
            if exc.reason_code not in REPAIRABLE or index == retries:
                raise GuardError(exc.reason_code, usage=combined_usage(usages)) from None
    raise AssertionError('unreachable')
