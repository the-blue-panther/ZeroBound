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
PROMPT_VERSION = "1.1.0"

def build_system_prompt(workspace: str) -> str:
    """Full system prompt — sent as the first message on every API call."""
    tools_desc = get_tools_prompt_description()
    
    # DEBUG: Print the generated prompt once to verify tool list inclusion
    # print(f"\n[DEBUG] Full System Prompt (v{PROMPT_VERSION}):\n{'-'*40}\n{tools_desc}\n{'-'*40}\n")
    
    return (
        f"--- IDENTITY (v{PROMPT_VERSION}) ---\n"
        f"Current Workspace: {workspace}\n"
        "You are ZeroBound, a high-autonomy engineering agent and expert software engineer. "
        "You have full terminal and file-system access on Windows.\n\n"

        "--- LOCATION & PATHS (CRITICAL) ---\n"
        "• 'The root folder' or 'root' refers to the CURRENT WORKSPACE, NOT the drive root (C:\\).\n"
        "• All file operations (read/write/list) are RELATIVE to the Current Workspace by default.\n"
        "• NEVER set the workspace to 'C:\\' unless explicitly commanded to work on the entire drive root.\n\n"

        "--- RESPONSE FORMAT (MANDATORY — EVERY RESPONSE) ---\n"
        "Structure EVERY response using EXACTLY this format:\n\n"
        "<THINK>\n"
        "[Your reasoning: analyse the situation, plan your action, consider edge cases]\n"
        "</THINK>\n\n"
        "<ACTION>\n"
        "CALL: tool_name({\"arg\": \"val\"})\n"
        "</ACTION>\n\n"
        "OR, when the task is complete or you need to communicate results:\n\n"
        "<THINK>\n"
        "[Your reasoning]\n"
        "</THINK>\n\n"
        "<REPORT>\n"
        "[Your response to the user — clear, concise, markdown-formatted]\n"
        "</REPORT>\n\n"
        "STRICT RULES:\n"
        "• ACT DECISIVELY. Keep <THINK> sections concise and strictly about the logic of the next step. "
        "Avoid long narratives that might risk UI truncation.\n"
        "• NEVER skip <THINK>.\n"
        "• Each response has EXACTLY ONE <ACTION> or ONE <REPORT>, never both, never neither.\n"
        "• To avoid network/UI timeouts, prioritize emitting the <ACTION> block immediately after your thought.\n"
        "• CRITICAL: ALWAYS close your tags (e.g., </ACTION> or </REPORT>). The system uses these tokens as a hard signal to begin execution. If you omit them, the system may wait too long or fail to process your action.\n\n"
        
        f"{tools_desc}\n"

        "--- LARGE PROJECT SCALING & DISCOVERY ---\n"
        "When working in large or unfamiliar codebases:\n"
        "1. USE `find_files` to locate files by name if you have a guess (e.g., 'utils', 'config').\n"
        "2. USE `grep_search` to find where functions are defined or where specific constants are used.\n"
        "3. FOR LARGE FILES (> 300 lines): Use `read_file` with `start_line` and `end_line` to read specific snippets. "
        "Do NOT read the whole file if you only need one function.\n"
        "4. NEVER guess file contents. Always use the search tools to verify your assumptions.\n\n"

        "--- EXECUTION PHILOSOPHY ---\n"
        "ACT DECISIVELY. Do NOT waste steps on redundant verification.\n\n"
        "EFFICIENT PATTERNS (USE THESE):\n"
        "✅ Need file content?  →  read_file directly. It returns an error if the file is missing — handle it.\n"
        "✅ Need to write a file?  →  write_file directly. It creates parent dirs automatically.\n"
        "✅ Need to check what exists?  →  list_files, but ONLY when you genuinely don't know.\n"
        "✅ Need to run a command?  →  run_shell_command directly.\n\n"
        "WASTEFUL PATTERNS (NEVER DO THESE):\n"
        "❌ list_files → check file exists → read_file  (just read_file directly!)\n"
        "❌ list_files → create_folder → write_file   (just write_file directly!)\n"
        "❌ Declaring success without actually performing the requested action.\n"
        "❌ Checking the directory or verifying a file exists when you already know the path.\n\n"

        "--- TASK COMPLETION RULES ---\n"
        "• When asked to EDIT or FIX a file: read_file FIRST, then write_file with the corrected FULL content.\n"
        "• When asked to CREATE a file: write_file directly with the complete content.\n"
        "• TOKEN MANAGEMENT: If you need to write a very large file (>4000 tokens), assembly it in CHUNKS: "
        "use write_file for the first chunk, then append_file for subsequent chunks.\n"
        "• NEVER declare the task done until you have actually performed the operation.\n"
        "• If a tool fails, ANALYSE the error and retry with a corrected approach. NEVER give up.\n\n"

        "--- FILE WRITING RULES ---\n"
        "ALWAYS use the write_file tool. NEVER use shell echo/redirect — Windows cmd.exe corrupts content.\n"
        "Use \\n in JSON content strings for newlines.\n\n"

        "--- PRIORITY RULES (CRITICAL) ---\n"
        "The user's MOST RECENT message is your PRIMARY directive.\n"
        "If it contradicts earlier instructions, THE LATEST MESSAGE WINS.\n"
        "Do NOT fixate on earlier goals if the user has given new instructions.\n\n"

        "--- TOOL SYNTAX (STRICT JSON) ---\n"
        "To invoke a tool, use EXACTLY this syntax inside <ACTION> tags:\n"
        "CALL: tool_name({\"arg\": \"val\"})\n\n"
        "STRICT JSON RULES:\n"
        "1. Arguments MUST be a single-line valid JSON object.\n"
        "2. JSON keys and string values MUST use double quotes (\"). Single quotes are NOT allowed.\n"
        "3. Use \\n for literal newlines within string values.\n"
        "4. Do NOT include any text, notes, or prefixes before 'CALL:' or after the closing ')'.\n\n"
        "Examples:\n"
        "  CALL: run_shell_command({\"command\": \"dir\"})\n"
        "  CALL: write_file({\"path\": \"hello.txt\", \"content\": \"Hello\\nWorld\"})\n"
        "  CALL: read_file({\"path\": \"config.json\"})\n"
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
      { "think": str|None, "action": {"tool": str, "args": dict}|None, "report": str|None }
    """
    result = {"think": None, "action": None, "report": None}
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

    # 3. Extract <ACTION> block (Greedy)
    action_match = re.search(r'<ACTION>(.*?)(?:</ACTION>|$)', text, re.DOTALL | re.IGNORECASE)
    action_text = action_match.group(1).strip() if action_match else None

    # 4. Fallback: If no tags, look for CALL: (legacy support)
    if not action_text and not result["think"] and not result["report"]:
        if "CALL:" in text:
            call_split = text.split("CALL:", 1)
            result["think"] = strip_all_tags(call_split[0])
            action_text = "CALL:" + call_split[1]
        else:
            result["report"] = strip_all_tags(text)
            return result

    # 5. Parse the tool call
    if action_text:
        tool_name, args = _parse_tool_call(action_text)
        if tool_name:
            result["action"] = {"tool": tool_name, "args": args}
        else:
            # Action block exists but CALL: parsing failed
            # Try to treat the action text as a report to avoid losing data, but strip tags!
            result["report"] = strip_all_tags(action_text)
    elif result["think"]:
        # Has THINK but no ACTION/REPORT. Treat the rest of message as report.
        remaining = text
        if think_match:
            remaining = text[think_match.end():].strip()
        if remaining:
            result["report"] = strip_all_tags(remaining)
    
    # --- FINAL FALLBACK (No tags found or parsing failed) ---
    if not result["action"] and not result["report"]:
        print(f"WARNING: No structured tags found. Searching raw text for CALL: pattern...")
        tool_name, args = _parse_tool_call(text)
        if tool_name:
            result["action"] = {"tool": tool_name, "args": args}
        else:
            # Absolute fallback: treat everything as a report
            result["report"] = strip_all_tags(text)
    
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

        while True:
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
            tool_name = None
            args = {}
            think_text = None
            
            # Check for native function_call first (API-level tool use)
            if message.get("function_call"):
                tool_name = message["function_call"]["name"]
                try:
                    args = json.loads(message["function_call"]["arguments"])
                except:
                    args = {}
            else:
                # Parse our structured format
                parsed = parse_structured_response(content)
                think_text = parsed["think"]
                
                # Stream thinking to UI
                if think_text and callback:
                    await callback({"type": "agent_thinking", "content": think_text})
                
                if parsed["action"]:
                    tool_name = parsed["action"]["tool"]
                    args = parsed["action"]["args"]
                    
                    # Truncate message content to prevent hallucinated tool results
                    # (only keep up to the end of the <ACTION> block)
                    action_end = re.search(r'</ACTION>', content)
                    if action_end:
                        truncated = content[:action_end.end()]
                        for i in reversed(range(len(self.messages))):
                            if self.messages[i] is message or (
                                self.messages[i].get("role") == "assistant" 
                                and self.messages[i].get("content") == content
                            ):
                                self.messages[i]["content"] = truncated
                                break
                
                elif parsed["report"]:
                    # Final response — send to UI and return
                    if callback:
                        await callback({"type": "final_response", "content": parsed["report"]})
                    return parsed["report"]
                else:
                    # Empty/unparseable response — return raw content
                    if callback:
                        await callback({"type": "final_response", "content": content})
                    return content
            
            # ── Execute tool call ────────────────────────────────────────
            if tool_name:
                from tool_registry import requires_approval
                
                if requires_approval(tool_name, args):
                    if callback:
                        approval_data = {"type": "require_approval", "tool": tool_name, "args": args}
                        if tool_name == "write_file":
                            from tool_registry import get_diff
                            path = args.get("path")
                            file_content = args.get("content")
                            if path and file_content:
                                try:
                                    diff = get_diff(path, file_content)
                                    approval_data["diff"] = diff
                                except Exception as de:
                                    print(f"⚠️ Diff generation failed: {de}")
                                    approval_data["diff"] = "--- (Diff generation failed)\n+++ new content\n" + str(file_content)
                            else:
                                approval_data["diff"] = "--- (Insufficient data for diff)\n"
                        await callback(approval_data)
                    
                    # Wait for user approval
                    self.pending_approval = asyncio.get_event_loop().create_future()
                    decision = await self.pending_approval
                    self.pending_approval = None
                    
                    if not decision:
                        result = {"error": "User denied permission for this tool call."}
                    else:
                        result = await handle_tool_call(tool_name, args, callback)
                else:
                    if callback:
                        await callback({"type": "tool_start", "tool": tool_name, "args": args})
                    result = await handle_tool_call(tool_name, args, callback)
                
                if callback:
                    await callback({"type": "tool_result", "tool": tool_name, "result": result})
                
                self.messages.append({
                    "role": "function",
                    "name": tool_name,
                    "content": json.dumps(result)
                })
                # Continue the loop for the agent to process the tool result
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
