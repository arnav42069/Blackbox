"""Keep structured tool messages valid after cancellation or early policy stops."""
import json


def complete_tool_messages(group):
    result = []
    i = 0
    while i < len(group):
        message = group[i]
        if message.get('tool_calls'):
            observations = []
            j = i + 1
            while j < len(group) and group[j]['role'] == 'tool':
                observations.append(group[j])
                j += 1
            remaining = list(observations)
            valid_calls = []
            for call in message['tool_calls']:
                name = call['function']['name']
                match = next((m for m in remaining if m.get('tool_name') == name), None)
                if match is not None:
                    valid_calls.append(call)
                    remaining.remove(match)
            if valid_calls:
                result.append({**message, 'tool_calls': valid_calls})
                result.extend(observations)
            elif message.get('content'):
                result.append({'role': 'assistant', 'content': message['content']})
            i = j
        elif message['role'] != 'tool':
            result.append(message)
            i += 1
        else:
            i += 1
    return result


def bound_context(messages, num_ctx):
    # Conservative character budget; keep the current user and structured call/result pairs.
    budget = max(4000, min(24000, num_ctx * 2))
    result = list(messages)
    def cost():
        return sum(len(m.get('content', '')) + len(json.dumps(m.get('tool_calls', []))) for m in result)
    while cost() > budget:
        users = [i for i,m in enumerate(result) if m['role'] == 'user']
        if len(users) < 2:
            break
        result = result[:users[0]] + result[users[1]:]
    if cost() > budget:
        # Older observations remain attributable, but their raw content need not fill context forever.
        for i,m in enumerate(result):
            if m['role'] == 'tool' and len(m.get('content', '')) > 1600:
                try:
                    record = json.loads(m['content'])
                    record['stdout'] = record.get('stdout', '')[:600]
                    record['stderr'] = record.get('stderr', '')[:200]
                    record['evidence'] = [{'excerpt': json.dumps(record.get('evidence', []))[:500]}]
                    record['context_compacted'] = True
                    result[i] = {**m, 'content': json.dumps(record)}
                except ValueError:
                    result[i] = {**m, 'content': m['content'][:1500]}
                if cost() <= budget:
                    break
    return result
