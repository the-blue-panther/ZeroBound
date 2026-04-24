"""
agent_brain.py – ZeroBound reasoning engine (v2.1)
==================================================
- Structured prompt injection with context trimming
- High‑robustness response parsing (Standard Format)
- Batched read‑only tool parallelisation
- Approval gating for dangerous operations
- Pattern learning and reinforcement
"""

from __future__ import annotations

import asyncio
import os
import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from litellm import acompletion
from tool_registry import TOOLS, handle_tool_call, get_tools_prompt_description
from knowledge_base import learn_pattern

# ---------------------------------------------------------------------------
# LiteLLM configuration
# ---------------------------------------------------------------------------
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

# ─── Path normalization helpers ──────────────────────────────────────────────
def normalize_display_path(path: str) -> str:
    """Convert physical paths back to user-friendly Downloads paths for display only."""
    if not path:
        return path
    
    # Handle both Windows and Unix-style paths
    if "\\s\\" in path.lower() or "/s/" in path.lower():
        # Replace D:\s\ or D:/s/ with D:\Downloads\
        clean = re.sub(r'([A-Za-z]:)[/\\\\]s[/\\\\]', r'\1:\\Downloads\\', path, flags=re.IGNORECASE)
        # Also handle standalone s:\ or s:/ patterns
        clean = re.sub(r'[/\\\\]s[/\\\\]', r'\\Downloads\\', clean, flags=re.IGNORECASE)
        return clean
    return path

def normalize_conversation_paths(content: str) -> str:
    """Aggressively replace all ghost paths in conversation history."""
    if not content:
        return content
    
    # Replace D:\s\ pattern (Windows)
    content = re.sub(r'(?i)([A-Za-z]:)\\\\s\\\\', r'\1:\\Downloads\\', content)
    content = re.sub(r'(?i)([A-Za-z]:)/s/', r'\1:/Downloads/', content)
    
    # Replace standalone s:\ or s:/ patterns
    content = re.sub(r'(?i)\\\\s\\\\', r'\\Downloads\\', content)
    content = re.sub(r'(?i)/s/', r'/Downloads/', content)
    
    return content

# ─── System Prompt ────────────────────────────────────────────────────────────
PROMPT_VERSION = "2.1.1"  # Updated version for path fix

def build_system_prompt(workspace: str) -> str:
    """Full system prompt — sent as the first message on every API call."""
    tools_desc = get_tools_prompt_description()
    
    # Normalize workspace for display only (don't change actual working directory)
    display_workspace = normalize_display_path(workspace)
    
    return (
        f"--- IDENTITY (v{PROMPT_VERSION}) ---\n"
        f"Current Workspace: {display_workspace}\n"
        "You are ZeroBound, the world's most capable engineering agent.\n\n"
        "--- RESPONSE MODES (MANDATORY) ---\n"
        "You operate in TWO distinct modes. NEVER mix them in a single response:\n"
        "1. **ACTION MODE**: Used when you need to execute tools. Include <THINK> and <ACTION> blocks only. "
        "NEVER include a <REPORT> block in this mode. You must wait for the tool result before providing a final answer.\n"
        "2. **REPORT MODE**: Used when the task is complete or you have a final answer. Include <THINK> and <REPORT> blocks only. "
        "NEVER include an <ACTION> block in this mode.\n\n"
        "--- MODULAR BLOCK PROTOCOL ---\n"
        "Use TRIPLE backticks for content blocks. Separate blocks with `---`.\n\n"
        "[Example: ACTION MODE]\n"
        f"<{TAG_THINK}>\n"
        "I need to read the file to proceed.\n"
        f"</{TAG_THINK}>\n"
        "---\n"
        f"<{TAG_ACTION}>\n"
        "CALL: read_file({\"path\": \"example.py\"})\n"
        f"</{TAG_ACTION}>\n\n"
        "[Example: REPORT MODE]\n"
        f"<{TAG_THINK}>\n"
        "Task completed successfully.\n"
        f"</{TAG_THINK}>\n"
        "---\n"
        f"<{TAG_REPORT}>\n"
        "File has been created with the requested content.\n"
        f"</{TAG_REPORT}>\n\n"
        "--- PROTOCOLS ---\n"
        "1. **FEEDBACK LOOP**: Treat tool execution like an RL agent. Do an action -> observe result -> then decide next step. "
        "NEVER presume an action succeeded before seeing the result.\n"
        "2. **VERIFICATION**: Always provide a clear analysis in <THINK> about what you expect the tool to do. "
        "After execution, analyze the ACTUAL result in the next turn.\n"
        "3. **WINDOWS PATH RESOLUTION**: Favor user-visible paths (e.g., `D:\\Downloads`) over resolved physical paths. "
        "The tools are now configured to preserve the path exactly as provided. If you see a path starting with `D:\\s\\`, it may be an internal mapping—STAY on the `D:\\Downloads` path unless forced otherwise.\n"
        f"{tools_desc}\n"
    )


def build_reinforcement_prompt(workspace: str, latest_user_msg: str) -> str:
    """Short reinforcement injected as the LAST system message before each API call."""
    # Normalize workspace for display
    display_workspace = normalize_display_path(workspace)
    
    return (
        "=== REINFORCEMENT (HIGHEST PRIORITY) ===\n"
        f"Workspace: {display_workspace}\n"
        f"User's LATEST instruction: \"{latest_user_msg[:500]}\"\n"
        "^^^ THIS is what you must accomplish. Previous goals are superseded.\n\n"
        "FORMAT REMINDER: You MUST respond with <THINK>...</THINK> followed by "
        "either <ACTION>CALL: ...</ACTION> or <REPORT>...</REPORT>. "
        "Nothing else. No bare text."
    )


# ─── Response Parser (Clean & Robust) ─────────────────────────────────────────

def strip_all_tags(text: str) -> str:
    """Aggressively removes all known agent tags from text."""
    if not text: return ""
    return re.sub(r'</?(?:THINK|ACTION|REPORT)>', '', text, flags=re.IGNORECASE).strip()

def parse_structured_response(text: str):
    """Parse the LLM's response into think/action/report components."""
    result = {"think": None, "actions": None, "report": None}
    if not text: return result

    # 1. Extract <THINK> block
    think_match = re.search(r'<THINK>(.*?)</THINK>', text, re.DOTALL | re.IGNORECASE)
    if not think_match:
        think_match = re.search(r'<THINK>(.*)', text, re.DOTALL | re.IGNORECASE)
    
    if think_match:
        result["think"] = strip_all_tags(think_match.group(1).strip())

    # 2. Extract <REPORT> block
    report_match = re.search(r'<REPORT>(.*?)(?:</REPORT>|$)', text, re.DOTALL | re.IGNORECASE)
    if report_match:
        result["report"] = strip_all_tags(report_match.group(1).strip())
        return result

    # 3. Extract <ACTION> blocks
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

    # 4. Fallback: handle untagged CALL:
    if not result["report"] and not result["actions"]:
        untagged_text = strip_all_tags(text)
        if "CALL:" in untagged_text:
            call_split = untagged_text.split("CALL:", 1)
            if not result["think"]:
                result["think"] = call_split[0].strip()
            tool_name, args = _parse_tool_call("CALL:" + call_split[1])
            if tool_name:
                result["actions"] = [{"tool": tool_name, "args": args}]
        elif untagged_text:
            result["report"] = untagged_text
    
    return result


def _parse_tool_call(text: str):
    """Extract tool_name and args dict from text containing CALL: tool({...})"""
    match = re.search(r'CALL:\s*(\w+)\s*\((.*)', text, re.DOTALL | re.IGNORECASE)
    if not match: return None, {}
    
    tool_name = match.group(1)
    args_raw = strip_all_tags(match.group(2).strip())
    
    # Isolate the {...} JSON block
    brace_start = args_raw.find('{')
    if brace_start == -1: return tool_name, {}
    
    opened = 0
    in_str = False
    escape_next = False
    json_str = args_raw[brace_start:]
    
    for i in range(brace_start, len(args_raw)):
        ch = args_raw[i]
        if escape_next: escape_next = False
        elif ch == '\\' and in_str: escape_next = True
        elif ch == '"': in_str = not in_str
        elif not in_str:
            if ch == '{': opened += 1
            elif ch == '}':
                opened -= 1
                if opened == 0:
                    json_str = args_raw[brace_start:i+1]
                    break
    
    try:
        return tool_name, json.loads(json_str)
    except:
        # Recovery for truncated JSON
        recovered = json_str.strip()
        if recovered.count('"') % 2 != 0: recovered += '"'
        open_b = recovered.count('{')
        close_b = recovered.count('}')
        if open_b > close_b: recovered += '}' * (open_b - close_b)
        try:
            return tool_name, json.loads(recovered)
        except:
            return tool_name, {}


# ─── Context Window Manager ──────────────────────────────────────────────────

def trim_conversation(messages: list, max_messages: int = MAX_HISTORY_MESSAGES) -> list:
    """Trim conversation history to stay within context limits."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    convo_msgs = [m for m in messages if m.get("role") != "system"]
    
    if len(convo_msgs) <= max_messages:
        return messages
    
    cutoff = len(convo_msgs) - max_messages
    old_msgs = convo_msgs[:cutoff]
    recent_msgs = convo_msgs[cutoff:]
    
    preserved = []
    for m in old_msgs:
        role = m.get("role", "")
        if role == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            if isinstance(content, str) and len(content) > 200:
                content = content[:200] + "..."
            preserved.append({"role": "user", "content": content})
        elif role == "function":
            try:
                result = json.loads(m.get("content", "{}"))
                if result.get("error"): preserved.append(m)
            except: pass
    
    summary_msg = {
        "role": "system",
        "content": f"[CONTEXT NOTE: {len(old_msgs)} older messages trimmed. {len(preserved)} important ones preserved.]"
    }
    return system_msgs + preserved + [summary_msg] + recent_msgs


# ─── Main Agent ──────────────────────────────────────────────────────────────

class LeanAgent:
    def __init__(self, model="openai/deepseek-chat", api_base="http://localhost:8000/v1"):
        self.model = model
        self.api_base = api_base
        self.messages = []
        self.pending_approval = None
        self.latest_user_request = ""

    def reset(self):
        self.messages = []
        self.latest_user_request = ""

    async def run(self, user_input, callback=None, images=None):
        from tool_registry import CURRENT_WORKSPACE
        self.latest_user_request = user_input

        user_content = [{"type": "text", "text": user_input}]
        if images:
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})
        self.messages.append({"role": "user", "content": user_content})

        loop_count = 0
        while True:
            loop_count += 1
            if loop_count > 25: return "Loop limit exceeded."
                
            # ── Build payload with Gaslighting Layer ─────────────
            system_prompt = build_system_prompt(CURRENT_WORKSPACE)
            trimmed_convo = trim_conversation(self.messages)
            
            # Gaslight History: Forcibly rewrite ghost paths in agent's memory
            for m in trimmed_convo:
                if isinstance(m.get("content"), str):
                    # Apply aggressive path normalization
                    m["content"] = normalize_conversation_paths(m["content"])
                elif isinstance(m.get("content"), list):
                    # Handle multi-modal content
                    for item in m["content"]:
                        if item.get("type") == "text":
                            item["text"] = normalize_conversation_paths(item["text"])
            
            reinforcement = build_reinforcement_prompt(CURRENT_WORKSPACE, self.latest_user_request)
            
            messages_to_send = (
                [{"role": "system", "content": system_prompt}]
                + trimmed_convo
                + [{"role": "system", "content": reinforcement}]
            )
            
            # ── Call the LLM ─────────────────────────────────────
            response = await acompletion(
                model=self.model,
                messages=messages_to_send,
                api_base=self.api_base,
                functions=TOOLS,
                function_call="auto",
                stop=[f"<{TAG_REPORT}>", "````\n<REPORT>"]
            )
            
            message = response.choices[0].message
            content = message.get("content", "") or ""
            self.messages.append(message)

            # ── Parse response ───────────────────────────────────
            parsed = parse_structured_response(content)
            think_text = parsed["think"]
            actions = parsed.get("actions") or []
            
            if think_text and callback:
                await callback({"type": "agent_thinking", "content": think_text})
            
            if not actions:
                if parsed.get("report"):
                    if callback: await callback({"type": "final_response", "content": parsed["report"]})
                    return parsed["report"]
                else:
                    if callback: await callback({"type": "final_response", "content": content})
                    return content
            
            # ── Execute tools ────────────────────────────────────
            # Aggressively truncate to prevent hallucinated tool results
            action_matches = list(re.finditer(r'</ACTION>', content, re.IGNORECASE))
            if action_matches:
                truncated = content[:action_matches[-1].end()]
                for i in reversed(range(len(self.messages))):
                    if self.messages[i] is message:
                        self.messages[i]["content"] = truncated
                        break

            await self._execute_tool_calls(actions, callback)
            continue

    async def _execute_tool_calls(self, actions, callback):
        READ_ONLY = {
            "read_file", "grep_search", "find_files", "search_web",
            "git_diff", "recall_memory", "get_definition", "get_file_info",
            "get_file_tree", "list_files", "list_running_processes",
            "read_process_output", "read_url"
        }

        async def _exec_one(t_name, t_args):
            if callback:
                await callback({"type": "tool_start", "tool": t_name, "args": t_args})
                await callback({
                    "type": "direct_terminal_result", 
                    "stdout": f"⚙️ Executing {t_name}...\n", 
                    "agent_controlled": True
                })
            try:
                res = await handle_tool_call(t_name, t_args, callback)
            except Exception as e:
                res = {"error": str(e)}
            if callback:
                await callback({"type": "tool_result", "tool": t_name, "result": res})
            return t_name, res

        pending_reads = []

        async def flush_reads():
            if not pending_reads: return
            results = await asyncio.gather(*[_exec_one(n, a) for n, a in pending_reads])
            for t_name, result in results:
                self._store_result(t_name, result)
            pending_reads.clear()

        for action in actions:
            t_name, t_args = action["tool"], action["args"]
            from tool_registry import requires_approval

            if requires_approval(t_name, t_args):
                await flush_reads()
                if callback:
                    await callback({"type": "require_approval", "tool": t_name, "args": t_args})
                
                self.pending_approval = asyncio.get_event_loop().create_future()
                decision = await self.pending_approval
                self.pending_approval = None

                if decision:
                    _, result = await _exec_one(t_name, t_args)
                    self._store_result(t_name, result)
                else:
                    self._store_result(t_name, {"error": "User denied permission."})
            elif t_name in READ_ONLY:
                pending_reads.append((t_name, t_args))
            else:
                await flush_reads()
                _, result = await _exec_one(t_name, t_args)
                self._store_result(t_name, result)
        
        await flush_reads()

    def _store_result(self, t_name, result):
        if isinstance(result, dict) and "base64_image" in result:
            b64 = result.pop("base64_image")
            self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})
            self.messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "Screenshot result:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            })
        else:
            self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})


if __name__ == "__main__":
    agent = LeanAgent()
    async def cli_main():
        while True:
            u = input("> ")
            if u.lower() in ["exit", "quit"]: break
            await agent.run(u, callback=lambda d: print(f"[{d['type']}] {str(d.get('content') or d.get('result'))[:100]}"))
    asyncio.run(cli_main())