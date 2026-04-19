import json
from agent_brain import parse_structured_response

def test_robust_parsing():
    print("Running Robust Parser Tests...")
    
    # Test 1: Truncated <ACTION> (Missing closing tag)
    t1 = '<THINK>Reasoning</THINK>\n<ACTION>\nCALL: write_file({"path": "t.txt", "content": "Hello World"})'
    r1 = parse_structured_response(t1)
    assert r1['action']['tool'] == 'write_file', f"FAIL t1: {r1}"
    assert r1['action']['args']['path'] == 't.txt', f"FAIL t1 args: {r1}"
    print("✅ Test 1: Greedy ACTION extraction passed.")

    # Test 2: Truncated JSON (Unclosed string)
    t2 = '<ACTION>\nCALL: write_file({"path": "t.txt", "content": "Partial content...'
    r2 = parse_structured_response(t2)
    assert r2['action']['args']['content'] == 'Partial content...', f"FAIL t2: {r2}"
    print("✅ Test 2: Truncated JSON recovery (string) passed.")

    # Test 3: Truncated JSON (Unclosed braces)
    t3 = '<ACTION>\nCALL: write_file({"path": "t.txt", "content": "Full"'
    # Note: _parse_tool_call expects {"path":...} so we test its fix
    from agent_brain import _parse_tool_call
    _, args = _parse_tool_call('CALL: write_file({"path": "t.txt", "content": "Full"')
    assert args['content'] == 'Full', f"FAIL t3: {args}"
    print("✅ Test 3: Truncated JSON recovery (braces) passed.")

    print("\n=== Robust Parser Tests PASSED! ===")

if __name__ == "__main__":
    test_robust_parsing()
