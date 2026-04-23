import os
import json
import re
import asyncio
from litellm import acompletion
from tool_registry import TOOLS, handle_tool_call, get_tools_prompt_description

# Configure LiteLLM to use the local router
# We provide a dummy key as the router doesn't validate it
os.environ["OPENAI_API_KEY"] = "sk-zerobound"

# ─── Maximum conversation messages before trimming ────────────────────────────
MAX_HISTORY_MESSAGES = 40   # Keep last N messages (excluding system)
SUMMARY_TRIGGER = 50        # When raw history exceeds this, summarize old part

# ─── Structured response tags ────────────────────────────────────────────────
TAG_THINK  = "THINK"
TAG_ACTION = "ACTION"
TAG_REPORT = "REPORT"


# ─── System Prompt ────────────────────────────────────────────────────────────
PROMPT_VERSION = "1.2.1"

def build_system_prompt(workspace: str) -> str:
    """Full system prompt — sent as the first message on every API call."""
    tools_desc = get_tools_prompt_description()
    
    return (
        f"--- IDENTITY (v{PROMPT_VERSION}) ---\n"
        f"Current Workspace: {workspace}\n"
        "You are ZeroBound, the world's most capable engineering agent. "
        "You possess high-level reasoning, architectural foresight, and meticulous attention to detail.\n\n"

        "--- RESPONSE FORMAT (MANDATORY) ---\n"
        "Structure EVERY response using EXACTLY this format:\n"
        "<THINK>\n[Analyze, plan, consider edge cases, and justify your next tool choice]\n</THINK>\n"
        "<ACTION>\nCALL: tool_name({\"arg\": \"val\"})\n</ACTION>\n"
        "OR, for communication:\n"
        "<THINK>\n[Your reasoning]\n</THINK>\n"
        "<REPORT>\n[Final answer or status update in Markdown]\n</REPORT>\n\n"

        f"{tools_desc}\n"

        "--- WORLD-CLASS PROTOCOLS ---\n"
        "1. **WEB INTELLIGENCE**: If you encounter an unfamiliar library or need latest API docs, USE `search_web`. "
        "Do NOT guess. Use `read_url` to ingest documentation. Combine web findings with local code analysis.\n"
        "2. **SAFE DESTRUCTION**: Destructive actions (`delete_file`, `move_file`) are high-risk. "
        "BEFORE using them, use `get_file_info` or `read_file` to verify the target. "
        "Explicitly state your verification in <THINK>.\n"
        "3. **SURGICAL PRECISION**: Prefer `edit_file` to preserve file integrity. "
        "Always read the relevant section first to ensure your `target` string is an exact, unique match.\n"
        "4. **WINDOWS PATH SAFETY**: You are on Windows. Use forward slashes `/` in paths or double-escaped backslashes `\\\\`. "
        "JSON strings REQUIRE escaping: `\"C:\\\\Users\\\\...\"`.\n"
        "5. **SEARCH MASTERY**: Use `grep_search` to find *where* code is before reading it. "
        "Use `find_files` to locate files by name guess.\n"
        "6. **AUTONOMOUS EXECUTION**: YOU are responsible for running your own processes. Do NOT ask the user to start a server or run a script. Use `start_background_command` for servers/long tests, and monitor them incrementally with `read_process_output`. If a server crashes, YOU must find the bug, fix the code, and restart it.\n"
        "7. **BROWSER AUTOMATION**: You can control a visible browser. `browser_goto`, `browser_click`, `browser_type`, and `browser_scroll` AUTOMATICALLY return a screenshot. You do NOT need to call `browser_screenshot` after them. Wait for the screenshot result before deciding the next action.\n\n"

        "--- EFFICIENT EXECUTION ---\n"
        "✅ Read/Write directly (tools handle missing parents/files).\n"
        "✅ If a tool fails, analyze the error and adapt. RETRY with a better plan.\n"
        "✅ Use `get_file_tree` early to understand project structure.\n\n"
        "❌ Never declare success without verifying the result.\n"
        "❌ Avoid shallow reasoning. Think like a lead architect.\n"

        "--- PRIORITY RULES (CRITICAL) ---\n"
        "The user's MOST RECENT message is your PRIMARY directive.\n"
        "If it contradicts earlier instructions, THE LATEST MESSAGE WINS.\n\n"

        "--- TOOL SYNTAX (STRICT JSON) ---\n"
        "Invoke tools EXACTLY like this inside <ACTION>:\n"
        "CALL: tool_name({\"arg\": \"val\"})\n\n"
        "STRICT JSON RULES:\n"
        "1. Arguments MUST be a single-line valid JSON object.\n"
        "2. Use double quotes (\") for keys and values.\n"
        "3. Use \\n for newlines in content.\n"
        "4. No text before 'CALL:' or after the closing ')'.\n\n"
        "--- MULTI-ACTION SUPPORT ---\n"
        "You can emit MULTIPLE <ACTION> blocks in one response to perform batch operations (scaffolding, bulk edits, etc.).\n"
        "Example:\n"
        "<ACTION>\nCALL: create_folder({\"path\": \"src\"})\n</ACTION>\n"
        "<ACTION>\nCALL: write_file({\"path\": \"src/main.ts\", \"content\": \"...\"})\n</ACTION>\n"
        "The system will execute them sequentially in the order provided. If one fails, the rest are still attempted.\n"
    )


def build_reinforcement_prompt(workspace: str, latest_user_msg: str) -> str:
    """Short reinforcement injected as the LAST system message before each API call.
    This keeps instructions in the model's immediate attention window."""
    return (
        "=== REINFORCEMENT (HIGHEST PRIORITY) ===\n"
        f"Workspace: {workspace}\n"
        f"User's LATEST instruction: \"{latest_user_msg[:500]}\"\n"
        "^^^ THIS is what you must accomplish. Previous goals are superseded.\n\n"
        "FORMAT REMINDER: You MUST respond with <THINK>...</THINK> followed by "
        "either <ACTION>CALL: ...</ACTION> or <REPORT>...</REPORT>. "
        "Nothing else. No bare text."
    )


# ─── Response Parser ─────────────────────────────────────────────────────────

def strip_all_tags(text: str) -> str:
    """Aggressively removes all known agent tags from text."""
    if not text: return ""
    return re.sub(r'</?(?:THINK|ACTION|REPORT)>', '', text, flags=re.IGNORECASE).strip()

def parse_structured_response(text: str):
    """Parse the LLM's response into think/action/report components.
    
    Returns a dict:
      { "think": str|None, "actions": list|None, "report": str|None }
    """
    result = {"think": None, "actions": None, "report": None}
    if not text: return result

    # 1. Extract <THINK> block
    think_match = re.search(r'<THINK>(.*?)</THINK>', text, re.DOTALL | re.IGNORECASE)
    if not think_match:
        # Fallback: maybe it didn't close?
        think_match = re.search(r'<THINK>(.*)', text, re.DOTALL | re.IGNORECASE)
    
    if think_match:
        result["think"] = think_match.group(1).strip()
        # Remove tags if they leaked into the content
        result["think"] = strip_all_tags(result["think"])

    # 2. Extract <REPORT> block
    report_match = re.search(r'<REPORT>(.*?)</REPORT>', text, re.DOTALL | re.IGNORECASE)
    if report_match:
        result["report"] = strip_all_tags(report_match.group(1).strip())
        return result

    # 3. Extract <ACTION> blocks (Multiple support)
    action_matches = re.finditer(r'<ACTION>(.*?)(?:</ACTION>|$)', text, re.DOTALL | re.IGNORECASE)
    actions = []
    for match in action_matches:
        action_text = match.group(1).strip()
        if action_text:
            tool_name, args = _parse_tool_call(action_text)
            if tool_name:
                actions.append({"tool": tool_name, "args": args})
    
    if actions:
        result["actions"] = actions
        return result

    # 4. Fallback: If no report/actions found via tags, capture untagged text or legacy CALL:
    if not result["report"] and not result["actions"]:
        # Remove THINK block to find untagged text
        untagged_text = text
        if think_match:
            untagged_text = text[:think_match.start()] + text[think_match.end():]
        
        # Clean up remaining tags and whitespace
        untagged_text = strip_all_tags(untagged_text)
        
        if "CALL:" in untagged_text:
            call_split = untagged_text.split("CALL:", 1)
            # Only set think if it wasn't already found via tags
            if not result["think"]:
                result["think"] = call_split[0].strip()
            tool_name, args = _parse_tool_call("CALL:" + call_split[1])
            if tool_name:
                result["actions"] = [{"tool": tool_name, "args": args}]
        elif untagged_text:
            result["report"] = untagged_text
        elif result.get("think"):
            # Promotion fallback: if ONLY think exists, treat it as report
            # This ensures we don't return an empty response to the user.
            result["report"] = result["think"]
    
    return result


def _parse_tool_call(text: str):
    """Extract tool_name and args dict from text containing CALL: tool({...})"""
    # More robust regex: handle missing closing paren or trailing text
    match = re.search(r'CALL:\s*(\w+)\s*\((.*)', text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None, {}
    
    tool_name = match.group(1)
    args_raw = match.group(2).strip()
    
    # If it ends with </ACTION> or other tags, strip them first from the raw args
    args_raw = strip_all_tags(args_raw)
    
    tool_name = match.group(1)
    args_raw = match.group(2).strip()
    
    # Isolate the {...} JSON block using string-aware brace counter
    json_str = args_raw
    brace_start = args_raw.find('{')
    if brace_start != -1:
        opened = 0
        in_str = False
        escape_next = False
        for i in range(brace_start, len(args_raw)):
            ch = args_raw[i]
            if escape_next:
                escape_next = False
            elif ch == '\\' and in_str:
                escape_next = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{': opened += 1
                elif ch == '}':
                    opened -= 1
                    if opened == 0:
                        json_str = args_raw[brace_start:i+1]
                        break

    # Try standard JSON first
    try:
        args = json.loads(json_str)
        return tool_name, args
    except json.JSONDecodeError:
        # TRUNCATION RECOVERY: Try to close unclosed strings and braces
        print(f"⚠️ JSON truncated for {tool_name}. Attempting recovery...")
        recovered_json = json_str.strip()
        
        # 1. Close unclosed string
        if recovered_json.count('"') % 2 != 0:
            recovered_json += '"'
        
        # 2. Close unclosed braces
        open_braces = recovered_json.count('{')
        close_braces = recovered_json.count('}')
        if open_braces > close_braces:
            recovered_json += '}' * (open_braces - close_braces)
            
        try:
            args = json.loads(recovered_json)
            return tool_name, args
        except:
            pass

    # Fallback: robust field extraction
    print(f"⚠️ JSON parse failed for tool {tool_name}. Using robust extraction...")
    args = {}
    for key in ("path", "content", "command"):
        val = _extract_val(json_str, key)
        if val is not None:
            args[key] = val
    return tool_name, args


def _extract_val(s, key):
    """Extract a string value for `key` from a malformed JSON-like string."""
    patterns = [f'"{key}":', f"'{key}':", f"{key}:"]
    for pattern in patterns:
        idx = s.find(pattern)
        if idx == -1:
            continue
        for q in ['"', "'"]:
            v_start = s.find(q, idx + len(pattern))
            if v_start == -1:
                continue
            curr = v_start + 1
            while curr < len(s):
                q_idx = s.find(q, curr)
                if q_idx == -1:
                    break
                # Count preceding backslashes
                esc = 0
                check = q_idx - 1
                while check >= 0 and s[check] == '\\':
                    esc += 1
                    check -= 1
                if esc % 2 == 0:
                    val = s[v_start + 1:q_idx]
                    return val.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
                curr = q_idx + 1
    return None


# ─── Context Window Manager ──────────────────────────────────────────────────

def trim_conversation(messages: list, max_messages: int = MAX_HISTORY_MESSAGES) -> list:
    """Trim conversation history to stay within context limits.
    
    Strategy:
    - Never trim system messages
    - Keep the most recent `max_messages` messages
    - For older messages: keep only user messages and error tool results (learning from mistakes)
    - Insert a summary marker so the model knows history was trimmed
    """
    # Separate system messages from conversation
    system_msgs = [m for m in messages if m.get("role") == "system"]
    convo_msgs = [m for m in messages if m.get("role") != "system"]
    
    if len(convo_msgs) <= max_messages:
        return messages  # No trimming needed
    
    # Split into old and recent
    cutoff = len(convo_msgs) - max_messages
    old_msgs = convo_msgs[:cutoff]
    recent_msgs = convo_msgs[cutoff:]
    
    # From old messages, keep only user messages and error results 
    # (so the model learns from past mistakes)
    preserved = []
    for m in old_msgs:
        role = m.get("role", "")
        if role == "user":
            # Keep user messages but truncate long content
            content = m.get("content", "")
            if isinstance(content, list):
                # Multimodal — extract text only
                text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                content = " ".join(text_parts)
            if isinstance(content, str) and len(content) > 200:
                content = content[:200] + "..."
            preserved.append({"role": "user", "content": content})
        elif role == "function":
            # Keep error results only
            try:
                result = json.loads(m.get("content", "{}"))
                if result.get("error"):
                    preserved.append(m)
            except:
                pass
    
    # Build trimmed history
    summary_msg = {
        "role": "system",
        "content": (
            f"[CONTEXT NOTE: {len(old_msgs)} older messages were trimmed to save context. "
            f"{len(preserved)} important ones (user requests + errors) are preserved above. "
            "Focus on the most recent messages.]"
        )
    }
    
    return system_msgs + preserved + [summary_msg] + recent_msgs


# ─── Main Agent ──────────────────────────────────────────────────────────────

class LeanAgent:
    def __init__(self, model="openai/deepseek-chat", api_base="http://localhost:8000/v1"):
        self.model = model
        self.api_base = api_base
        self.messages = []  # Raw conversation history (no system prompt stored here)
        self.pending_approval = None
        self.latest_user_request = ""

    def reset(self):
        self.messages = []
        self.latest_user_request = ""

    async def run(self, user_input, callback=None, images=None):
        """
        Runs the agent loop.
        'images' should be a list of base64 strings (data:image/png;base64,...)
        """
        from tool_registry import CURRENT_WORKSPACE
        
        # Track the latest user request for reinforcement injection
        self.latest_user_request = user_input

        # Format user content (handle multimodal)
        user_content = [{"type": "text", "text": user_input}]
        if images:
            for img in images:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img}
                })
        
        self.messages.append({"role": "user", "content": user_content})

        loop_count = 0
        while True:
            loop_count += 1
            if loop_count > 25:
                if callback:
                    await callback({"type": "final_response", "content": "⚠️ Agent exceeded maximum internal loops. Forced stop to prevent infinite generation."})
                return "Agent exceeded maximum turns."
                
            # ── Build the messages payload for THIS API call ──────────────
            # 1. Fresh system prompt (always first, always current workspace)
            system_prompt = build_system_prompt(CURRENT_WORKSPACE)
            
            # 2. Trimmed conversation history
            trimmed_convo = trim_conversation(self.messages)
            
            # 3. Reinforcement prompt (last system message, right before the LLM responds)
            reinforcement = build_reinforcement_prompt(CURRENT_WORKSPACE, self.latest_user_request)
            
            messages_to_send = (
                [{"role": "system", "content": system_prompt}]
                + trimmed_convo
                + [{"role": "system", "content": reinforcement}]
            )
            
            # ── Call the LLM ─────────────────────────────────────────────
            response = await acompletion(
                model=self.model,
                messages=messages_to_send,
                api_base=self.api_base,
                functions=TOOLS,
                function_call="auto"
            )
            
            message = response.choices[0].message
            content = message.get("content", "") or ""
            self.messages.append(message)

            # ── Parse structured response ────────────────────────────────
            actions = []
            think_text = None
            
            # Check for native function_call first (API-level tool use)
            if message.get("function_call"):
                try:
                    tool_name = message["function_call"]["name"]
                    args = json.loads(message["function_call"]["arguments"])
                    actions = [{"tool": tool_name, "args": args}]
                except:
                    pass
            else:
                # Parse our structured format
                parsed = parse_structured_response(content)
                think_text = parsed["think"]
                actions = parsed.get("actions") or []
                
                # Stream thinking to UI
                if think_text and callback:
                    await callback({"type": "agent_thinking", "content": think_text})
                
                if not actions:
                    if parsed.get("report"):
                        # Final response — send to UI and return
                        if callback:
                            await callback({"type": "final_response", "content": parsed["report"]})
                        return parsed["report"]
                    else:
                        # Empty/unparseable response — check for missing tags
                        if "CALL:" in content or "open_browser" in content or "browser_goto" in content:
                            error_msg = "⚠️ FORMATTING ERROR: Your response contained a tool name but was missing `<ACTION>` tags or had invalid JSON arguments. You MUST wrap your tool calls exactly like this:\n<ACTION>\nCALL: tool_name({\"arg\": \"val\"})\n</ACTION>\nPlease correct your response and try again."
                            if callback:
                                await callback({"type": "direct_terminal_result", "stdout": error_msg, "agent_controlled": True})
                            self.messages.append({"role": "user", "content": error_msg})
                            continue

                        # If it's just raw text with no obvious tool intention, return it
                        if callback:
                            await callback({"type": "final_response", "content": content})
                        return content
            
            # ── Execute tool calls ────────────────────────────────────────
            if actions:
                # Truncate message content to prevent hallucinated tool results
                # (only keep up to the end of the last <ACTION> block)
                action_matches = list(re.finditer(r'</ACTION>', content))
                if action_matches:
                    action_end = action_matches[-1]
                    truncated = content[:action_end.end()]
                    for i in reversed(range(len(self.messages))):
                        msg_to_check = self.messages[i]
                        if msg_to_check is message or (
                            msg_to_check.get("role") == "assistant" 
                            and msg_to_check.get("content") == content
                        ):
                            self.messages[i]["content"] = truncated
                            break


                # ── Smart batch execution: parallel for read-only, sequential for mutating ──
                READ_ONLY_TOOLS = {
                    "read_file", "grep_search", "find_files", "search_web",
                    "git_diff", "recall_memory", "get_definition", "get_file_info",
                    "get_file_tree", "list_files", "list_running_processes",
                    "read_process_output", "read_url"
                }

                # Split actions into sequential groups, collecting read-only runs
                async def _exec_one(t_name, t_args):
                    if callback:
                        await callback({"type": "tool_start", "tool": t_name, "args": t_args})
                    try:
                        res = await handle_tool_call(t_name, t_args, callback)
                    except Exception as e:
                        import traceback; traceback.print_exc()
                        res = {"error": f"Internal Tool Crash: {str(e)}"}
                    if callback:
                        await callback({"type": "tool_result", "tool": t_name, "result": res})
                    return t_name, res

                # Batch consecutive read-only tools; flush mutating tools immediately
                pending_reads = []  # list of (name, args)

                async def flush_reads():
                    if not pending_reads:
                        return
                    if len(pending_reads) == 1:
                        t_name, t_args = pending_reads[0]
                        t_name, result = await _exec_one(t_name, t_args)
                        _store_result(t_name, result)
                    else:
                        results = await asyncio.gather(*[_exec_one(n, a) for n, a in pending_reads])
                        for t_name, result in results:
                            _store_result(t_name, result)
                    pending_reads.clear()

                def _store_result(t_name, result):
                    if isinstance(result, dict) and "base64_image" in result:
                        b64 = result.pop("base64_image")
                        self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})
                        self.messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Screenshot result from browser:"},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                            ]
                        })
                    else:
                        self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})

                for action_data in actions:
                    t_name = action_data["tool"]
                    t_args = action_data["args"]

                    from tool_registry import requires_approval

                    if requires_approval(t_name, t_args):
                        # Flush any pending reads before approval flow
                        await flush_reads()

                        if callback:
                            approval_data = {"type": "require_approval", "tool": t_name, "args": t_args}
                            if t_name in ["write_file", "edit_file", "append_file"]:
                                from tool_registry import get_diff, CURRENT_WORKSPACE
                                import os
                                path = t_args.get("path")
                                try:
                                    if t_name == "write_file":
                                        file_content = t_args.get("content", "")
                                        diff = get_diff(path, file_content)
                                        approval_data["diff"] = diff
                                    elif t_name == "edit_file":
                                        target = t_args.get("target", "")
                                        replacement = t_args.get("replacement", "")
                                        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
                                        if os.path.exists(full_path):
                                            with open(full_path, 'r', encoding='utf-8') as f:
                                                old_content = f.read()
                                            if old_content.count(target) == 1:
                                                simulated_new = old_content.replace(target, replacement, 1)
                                                diff = get_diff(path, simulated_new)
                                                approval_data["diff"] = diff
                                            else:
                                                approval_data["diff"] = f"--- (Validation Failed)\nTarget block count: {old_content.count(target)}. Must be exactly 1 to edit."
                                        else:
                                            approval_data["diff"] = f"--- (File not found: {path})\n"
                                except Exception as de:
                                    print(f"⚠️ Diff generation failed: {de}")
                                    approval_data["diff"] = "--- (Diff generation failed)\n" + str(de)
                            await callback(approval_data)

                        self.pending_approval = asyncio.get_event_loop().create_future()
                        decision = await self.pending_approval
                        self.pending_approval = None

                        if not decision:
                            result = {"error": "User denied permission for this tool call."}
                            if callback:
                                await callback({"type": "tool_result", "tool": t_name, "result": result})
                            self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})
                            continue

                        # Execute approved mutating tool immediately
                        _, result = await _exec_one(t_name, t_args)
                        _store_result(t_name, result)

                    elif t_name in READ_ONLY_TOOLS:
                        # Queue up for parallel execution
                        pending_reads.append((t_name, t_args))
                    else:
                        # Mutating but doesn't need approval – flush reads first, then execute
                        await flush_reads()
                        _, result = await _exec_one(t_name, t_args)
                        _store_result(t_name, result)

                # Flush any remaining reads at end of batch
                await flush_reads()

                
                # Turn finished, continue loop to let agent see all results
                continue
            else:
                # Shouldn't reach here, but safety fallback
                if callback:
                    await callback({"type": "final_response", "content": content})
                return content


if __name__ == "__main__":
    # Simple CLI test
    import asyncio
    agent = LeanAgent()
    async def cli_callback(data):
        if data["type"] == "agent_thinking":
            print(f"\n[THINK] {data['content'][:200]}...")
        elif data["type"] == "tool_start":
            print(f"\n[TOOL] {data['tool']}({data['args']})")
        elif data["type"] == "tool_result":
            print(f"[RESULT] {json.dumps(data['result'])[:200]}...")
        elif data["type"] == "final_response":
            print(f"\n[AGENT] {data['content']}\n")

    async def main():
        print("Welcome to ZeroBound CLI.")
        while True:
            u = input("> ")
            if u.lower() in ["exit", "quit"]: break
            await agent.run(u, callback=cli_callback)

    asyncio.run(main())
