"""Unit tests for the structured response parser."""
from agent_brain import parse_structured_response

# Test 1: Well-formed structured response with ACTION
t1 = '<THINK>\nI need to read the file first.\n</THINK>\n\n<ACTION>\nCALL: read_file({"path": "test.md"})\n</ACTION>'
r1 = parse_structured_response(t1)
assert r1['think'] == 'I need to read the file first.', f'FAIL t1 think: {r1}'
assert r1['action']['tool'] == 'read_file', f'FAIL t1 action: {r1}'
assert r1['action']['args']['path'] == 'test.md', f'FAIL t1 args: {r1}'
print('Test 1 PASS: structured ACTION')

# Test 2: Well-formed REPORT
t2 = '<THINK>\nTask complete.\n</THINK>\n\n<REPORT>\nHere is your file content with $$E = mc^2$$\n</REPORT>'
r2 = parse_structured_response(t2)
assert r2['think'] == 'Task complete.', f'FAIL t2 think: {r2}'
assert '$$E = mc^2$$' in r2['report'], f'FAIL t2 report: {r2}'
assert r2['action'] is None, f'FAIL t2 should have no action: {r2}'
print('Test 2 PASS: structured REPORT')

# Test 3: Legacy fallback (no tags, just CALL:)
t3 = 'Let me read the file.\nCALL: read_file({"path": "x.py"})'
r3 = parse_structured_response(t3)
assert r3['action']['tool'] == 'read_file', f'FAIL t3 action: {r3}'
assert r3['think'] == 'Let me read the file.', f'FAIL t3 think: {r3}'
print('Test 3 PASS: legacy CALL: fallback')

# Test 4: Plain text (no tags, no CALL)
t4 = 'I have finished the task. The file is ready.'
r4 = parse_structured_response(t4)
assert r4['report'] == t4, f'FAIL t4 report: {r4}'
print('Test 4 PASS: plain text fallback')

# Test 5: Nested braces in content
t5 = '<THINK>Writing code</THINK>\n<ACTION>\nCALL: write_file({"path": "t.py", "content": "def f():\\n    d = {1: 2}\\n    return d"})\n</ACTION>'
r5 = parse_structured_response(t5)
assert r5['action']['tool'] == 'write_file', f'FAIL t5 tool: {r5}'
assert '{1: 2}' in r5['action']['args']['content'], f'FAIL t5 nested: {r5}'
print('Test 5 PASS: nested braces in content')

# Test 6: Context trimming
from agent_brain import trim_conversation
msgs = [{"role": "system", "content": "system prompt"}]
for i in range(60):
    msgs.append({"role": "user", "content": f"message {i}"})
    msgs.append({"role": "assistant", "content": f"response {i}"})
trimmed = trim_conversation(msgs, max_messages=30)
# Should have system + preserved old + summary + recent 30
assert len(trimmed) < len(msgs), f'FAIL t6: trimmed={len(trimmed)} >= original={len(msgs)}'
# Last message should be the most recent
last_user = [m for m in trimmed if m.get("role") == "user"][-1]
assert "message 59" in str(last_user['content']), f'FAIL t6 last msg: {last_user}'
print('Test 6 PASS: context trimming')

# Test 7: Reinforcement prompt builder
from agent_brain import build_reinforcement_prompt
rp = build_reinforcement_prompt("C:\\workspace", "fix the LaTeX in my file")
assert "fix the LaTeX" in rp, f'FAIL t7: {rp}'
assert "LATEST" in rp, f'FAIL t7 priority: {rp}'
print('Test 7 PASS: reinforcement prompt')

print('\n=== All 7 tests PASSED! ===')
