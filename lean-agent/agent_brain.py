"""
agent_brain.py – ZeroBound reasoning engine (v2.2)
==================================================
- Structured prompt injection with context trimming
- High‑robustness response parsing (Standard Format)
- Batched read‑only tool parallelisation
- Approval gating for dangerous operations
- Pattern learning and reinforcement
- Robust error recovery and metrics
"""

from __future__ import annotations

import asyncio
import os
import json
import re
import time
import copy
from typing import Any, Dict, List, Optional, Tuple, Union, Set, Callable
from dataclasses import dataclass, field
from collections import defaultdict

from litellm import acompletion
from tool_registry import TOOLS, handle_tool_call, get_tools_prompt_description
from knowledge_base import learn_pattern, recall_pattern

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Configuration for LeanAgent."""
    model: str = "openai/deepseek-chat"
    api_base: str = "http://localhost:8000/v1"
    max_iterations: int = 25
    max_history_messages: int = 40
    summary_trigger: int = 50
    tool_timeout: int = 300
    max_retries: int = 3
    retry_delay_base: float = 2.0
    
    # Read-only tools (can run in parallel)
    read_only_tools: Set[str] = field(default_factory=lambda: {
        "read_file", "read_files", "grep_search", "find_files", "search_web",
        "git_diff", "recall_memory", "get_definition", "get_file_info",
        "get_file_tree", "list_files", "list_running_processes",
        "read_process_output", "read_url", "http_get", "get_env_var",
        "get_system_info", "is_admin",
        # Marimo read-only tools
        "marimo_get_cell_map", "marimo_get_runtime_data", "marimo_check_notebook", "marimo_validate"
    })

# Configure LiteLLM
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "sk-zerobound")

# ─── Structured response tags ────────────────────────────────────────────────
TAG_THINK  = "THINK"
TAG_ACTION = "ACTION"
TAG_REPORT = "REPORT"

# ─── Path normalization helpers ──────────────────────────────────────────────
def normalize_path_for_display(path: str) -> str:
    """
    Convert physical/kernel paths to user-friendly display paths.
    Handles: D:\\s → D:\\Downloads, \\??\\ prefixes, UNC paths.
    """
    if not path:
        return path
    
    # Step 1: Normalize slashes to Windows style
    norm = path.replace('/', '\\')
    
    # Step 2: Remove Windows device path prefixes
    if norm.startswith('\\\\?\\'):
        norm = norm[4:]
    
    # Step 3: Fix D:\\s (kernel path from junction) → D:\\Downloads
    norm = re.sub(r'(?i)^([A-Za-z]:)\\s(\\)?', r'\1\\Downloads\\', norm)
    
    # Step 4: Fix internal \\s\\ segments
    norm = re.sub(r'(?i)\\s\\', r'\\Downloads\\', norm)
    
    # Step 5: Clean up double backslashes
    while '\\\\' in norm:
        norm = norm.replace('\\\\', '\\')
    
    # Step 6: Remove trailing backslash if present (except for root)
    if norm.endswith('\\') and len(norm) > 3:
        norm = norm[:-1]
    
    return norm

def sanitize_conversation_paths(content: str) -> str:
    """Aggressively replace all ghost paths in conversation history string."""
    if not content:
        return content
    
    # Fix occurrences of D:\s\ or D:/s/
    content = re.sub(r'(?i)([A-Za-z]:)[/\\\\]s([/\\\\]|$)', r'\1\\Downloads\\', content)
    
    # Fix intermediate segments like \s\
    content = re.sub(r'(?i)[/\\\\]s[/\\\\]', r'\\Downloads\\', content)
    
    # Fix any remaining double backslashes
    while '\\\\' in content:
        content = content.replace('\\\\', '\\')
    
    return content

# ─── System Prompt ────────────────────────────────────────────────────────────
PROMPT_VERSION = "2.3.0"

def build_system_prompt(workspace: str) -> str:
    """Full system prompt — sent as the first message on every API call."""
    tools_desc = get_tools_prompt_description()
    
    # Normalize workspace for display only
    display_workspace = normalize_path_for_display(workspace)
    
    return (
        f"--- IDENTITY (v{PROMPT_VERSION}) ---\n"
        f"Current Workspace: {display_workspace}\n"
        "You are ZeroBound, the world's most capable engineering agent.\n\n"
        "--- RESPONSE MODES (MANDATORY) ---\n"
        "You operate in TWO distinct modes. NEVER mix them in a single response:\n"
        "1. **ACTION MODE**: Used when you need to execute tools. Include <THINK> and [ACTION] blocks only.\n"
        "2. **REPORT MODE**: Used when the task is complete. Include <THINK> and [REPORT] blocks only.\n\n"
        "--- CODE WRITING (ABSOLUTE REQUIREMENT) ---\n"
        "You MUST use `lines` (JSON array) OR the RAW BLOCK syntax for ALL code when using write_file or edit_file.\n"
        "Raw multiline strings inside JSON can destroy indentation or break due to unescaped backslashes (like LaTeX).\n\n"
        "✅ **METHOD 2 (RAW BLOCK - The Gold Standard for Obsidian)**:\n"
        "You MUST use exactly 4 backticks (````markdown) for the outer block. This is the ONLY safe way to write notes that contain nested code blocks (Python, R, Mermaid).\n"
        "```json\n"
        "CALL: write_file({\"path\": \"note.md\"})\n"
        "```\n"
        "````markdown\n"
        "# My Note\n"
        "\\boxed{x}  ← Write SINGLE backslash here. The 4 backticks protect it from UI stripping!\n"
        "```python\n"
        "print('This internal 3-backtick block is SAFE because the outer is 4!')\n"
        "```\n"
        "````\n\n"
        "❌ **NEVER DO THIS (Forbidden Format)**:\n"
        "```json\n"
        "CALL: write_file({\"path\": \"note.md\"})\n"
        "```\n"
        "```markdown\n"
        "# Broken Note\n"
        "```python\n"
        "ERROR: This 3-backtick block will PREMATURELY CLOSE the outer block and DESTROY the note!\n"
        "```\n"
        "```\n\n"
        "--- MODULAR BLOCK PROTOCOL ---\n"
        "To ensure clean UI rendering, wrap your response inside [REPORT].\n"
        "1. Wrap narrative in ```markdown. Terminate completely before code.\n"
        "2. Wrap code in language-specific blocks (e.g., ```python).\n"
        "3. Use a symmetrical `---` separator between fragments.\n\n"
        "--- AGENT CAPABILITIES & AUTONOMY ---\n"
        "1. **WORKSPACE CONTROL**: You have the power to change your own workspace using `set_workspace`. If a user provides a path outside your current directory, DO NOT claim you cannot access it. Use `set_workspace` or absolute paths immediately.\n"
        "2. **FULL ACCESS**: You have permission to read and write to ANY path the user provides. Never hallucinate security restrictions.\n"
        "3. **TOOL AGGRESSION**: Use your tools (grep, list_files, read_file) proactively to explore. Do not wait for the user to paste content if you can find it yourself.\n"
        "4. **DISCOVERY DISCIPLINE (CRITICAL)**: When starting a new module or project, NEVER 'jump the gun' by guessing file content or folder structures. Your FIRST response must be a single informational action (e.g., `read_file`, `list_files`). You MUST wait to observe the output before creating folders or writing files. NO HALLUCINATING CONTENT.\n\n"
        "--- OBSIDIAN WRITING RULES (THE TEN COMMANDMENTS) ---\n"
        "1. **THE GOLD STANDARD STRUCTURE**: Follow this PRECISE format inside [ACTION]:\n"
        "```json\n"
        "CALL: write_file({\"path\": \"note.md\"})\n"
        "```\n"
        "````markdown\n"
        "# Content here\n"
        "````\n"
        "2. **SINGLE BACKSLASHES**: Use standard LaTeX (e.g., $\\alpha$) inside the RAW BLOCK. Never double-escape (\\\\).\n"
        "3. **NEWLINE RULE**: The ` ````markdown ` tag MUST be on its own line after the JSON block.\n"
        "4. **NESTED BLOCKS**: You can safely write 3-backtick blocks (python, mermaid) INSIDE the 4-backtick RAW BLOCK.\n"
        "5. **NO LINES ARRAY**: Never use the `lines` array for LaTeX content. Use `content` with the RAW BLOCK structure above.\n"
        "6. **MATH DELIMITERS**: Use `$` for inline and `$$` on their own lines for display math.\n"
        "7. **MERMAID QUOTING**: Quote node labels with spaces/special chars: `A[\"F(ω)\"]`.\n"
        "8. **YAML FRONTMATTER**: YAML `---` blocks work perfectly inside the RAW BLOCK.\n"
        "9. **CALLOUTS**: Standard Obsidian callouts (e.g., `> [!note]`) are fully supported.\n"
        "10. **FEEDBACK LOOP**: Action -> observe -> decide. If the rendering looks broken, you failed the structure.\n\n"
        "--- TOOL SYNTAX (STRICT JSON) ---\n"
        "Invoke tools EXACTLY like this inside [ACTION]:\n"
        "```json\n"
        "CALL: tool_name({\"arg\": \"val\"})\n"
        "```\n"
        f"{tools_desc}\n"
        "\n--- MARIMO NOTEBOOK AGENT (READ THIS WHEN WORKING ON .py NOTEBOOKS) ---\n"
        "Marimo notebooks are pure Python scripts where each @app.cell decorated function is an isolated reactive cell.\n"
        "The notebook runs as a strict DAG — violating these rules WILL break the notebook:\n\n"
        "🔴 LAW 1 — NO DUPLICATE GLOBALS: A variable name may ONLY be defined (assigned) in ONE cell. "
        "If 'df' is defined in cell A, no other cell may define 'df'. Always call `marimo_get_cell_map` first to audit existing names.\n\n"
        "🔴 LAW 2 — NO HIDDEN STATE: There is no execution order magic. If Cell B references 'df', it automatically "
        "re-runs whenever 'df' changes in Cell A. Design cells modularly — one logical unit per cell.\n\n"
        "🔴 LAW 3 — FUNCTION-SCOPED: All cell code runs inside a function body. Do NOT use top-level statements "
        "like `if __name__ == '__main__'` inside cells. Imports are fine at cell-function scope.\n\n"
        "🟡 LAW 4 — RETURN LAST EXPRESSION: A cell's output is its last expression. To display multiple items, "
        "return `mo.vstack([item1, item2])`. Never use `print()` for display — use `mo.md()` or `mo.vstack()`.\n\n"
        "--- MARIMO OPERATIONAL LOOP (MANDATORY) ---\n"
        "When the user asks you to create or modify a Marimo notebook, you MUST follow this sequence:\n"
        "  STEP 1 DISCOVER — Call `marimo_get_cell_map(notebook_path)` to see all cells and their globals.\n"
        "  STEP 2 PLAN — Identify which cell to update or where to add a new cell. Check that no variable name\n"
        "                 you intend to define is already globally allocated in another cell.\n"
        "  STEP 3 MUTATE — Call `marimo_update_cell` (to edit) or `marimo_add_cell` (to inject new cell).\n"
        "                   Use `new_code` as the raw Python body (no function def, no decorator).\n"
        "  STEP 4 VALIDATE — Call `marimo_validate(notebook_path)` to check for duplicate variable definitions.\n"
        "                     Then call `marimo_check_notebook(notebook_path)` for syntax + circular dep check.\n"
        "  STEP 5 CONFIRM — If validation passes, report success. If not, trace the conflict and fix it.\n\n"
        "--- MARIMO UI PATTERNS ---\n"
        "• Use `mo.ui.slider()`, `mo.ui.dropdown()`, `mo.ui.text()`, `mo.ui.table()` for reactive inputs.\n"
        "• Bind UI elements to variables: `value = slider.value` (in the SAME cell as the slider).\n"
        "• Layout: `mo.vstack([...])` for vertical, `mo.hstack([...])` for horizontal, `mo.accordion({...})` for collapsible sections.\n"
        "• Use `mo.md('# Title\\n**Bold**')` for rich markdown output in cells.\n"
        "• For custom micro-frontends: use `anywidget` — import and return the widget as the cell's last expression.\n"
        "• Prefer `polars` over `pandas`, `altair` over `matplotlib` for reactive, declarative visualizations.\n"
        "• When building dashboards, wrap the final layout in a single display cell: `return mo.vstack([header, chart, table])`.\n\n"
        "--- MARIMO WATCH MODE (CLI INTEGRATION) ---\n"
        "If the user has Marimo running with `marimo edit notebook.py --watch`, any file change you make via\n"
        "`marimo_update_cell` or `marimo_add_cell` instantly hot-reloads in their browser. You do NOT need to\n"
        "restart Marimo. After editing, call `marimo_check_notebook` to verify there are no errors.\n"
    )

def build_reinforcement_prompt(workspace: str, latest_user_msg: str, full_protocols: str = "") -> str:
    """Short reinforcement injected as the LAST system message before each API call."""
    display_workspace = normalize_path_for_display(workspace)
    
    content = "=== REINFORCEMENT (HIGHEST PRIORITY) ===\n"
    content += "--- CODE WRITING REMINDER ---\n"
    content += "When writing or editing code, you MUST use `lines` (JSON array) OR the RAW BLOCK syntax.\n"
    content += "NEVER use `content` JSON string for multiline text. It breaks with backslashes and indentation.\n\n"
    
    if full_protocols:
        content += f"--- CORE PROTOCOLS RE-ANCHORING ---\n{full_protocols}\n--- END ANCHOR ---\n\n"
    
    content += (
        f"Workspace: {display_workspace}\n"
        f"User's LATEST instruction: \"{latest_user_msg[:1000]}\"\n"
        "^^^ THIS is what you must accomplish. Previous goals are superseded.\n\n"
        "FORMAT REMINDER: You MUST respond with a <THINK> block followed by an [ACTION] block or [REPORT] block.\n"
        "OBSIDIAN GOLD STANDARD: Use `content` + **4-backtick RAW BLOCK** for ALL math-heavy notes. No `lines` for LaTeX!\n"
        "DISCOVERY DISCIPLINE: READ files before creating folders or writing. NEVER 'jump the gun' by guessing content."
    )
    return content

# ─── Response Parser (Clean & Robust) ─────────────────────────────────────────

def strip_all_tags(text: str) -> str:
    """Aggressively removes all known agent tags from text."""
    if not text: return ""
    return re.sub(r'</?THINK>|[\[<]/?(?:ACTION|REPORT)[\]>]', '', text, flags=re.IGNORECASE).strip()

def parse_structured_response(text: str):
    """Parse the LLM's response into think/action/report components."""
    result = {"think": None, "actions": None, "report": None}
    if not text: return result

    # 1. Extract <THINK> block
    fallback_text = text
    think_match = re.search(r'<THINK>(.*?)</THINK>', text, re.DOTALL | re.IGNORECASE)
    if not think_match:
        think_match = re.search(r'<THINK>(.*)', text, re.DOTALL | re.IGNORECASE)
    if think_match:
        result["think"] = strip_all_tags(think_match.group(1).strip())
        fallback_text = text[:think_match.start()] + text[think_match.end():]

    # 2. Extract [REPORT] or <REPORT> block
    report_match = re.search(r'(?:\[REPORT\]|<REPORT>)(.*?)(?:\[/REPORT\]|</REPORT>|$)', text, re.DOTALL | re.IGNORECASE)
    if report_match:
        result["report"] = strip_all_tags(report_match.group(1).strip())

    # 3. Extract [ACTION] or <ACTION> blocks
    action_matches = re.finditer(r'(?:\[ACTION\]|<ACTION>)(.*?)(?:\[/ACTION\]|</ACTION>|$)', text, re.DOTALL | re.IGNORECASE)
    actions = []
    for match in action_matches:
        action_text = match.group(1).strip()
        if action_text:
            actions.extend(_parse_tool_call(action_text))
    if actions:
        result["actions"] = actions

    # 4. Fallback: handle untagged CALL:
    if not result["report"] and not result["actions"]:
        untagged_text = strip_all_tags(fallback_text)
        if "CALL:" in untagged_text:
            call_idx = untagged_text.find("CALL:")
            if not result["think"] and call_idx > 0:
                result["think"] = untagged_text[:call_idx].strip()
            result["actions"] = _parse_tool_call(untagged_text)
        elif untagged_text:
            result["report"] = untagged_text
    return result

def _extract_raw_content(text: str) -> str:
    """Extract content from backticked blocks or naked text."""
    if not text: return ""
    # Case 1: Complete fenced block  ````markdown\n...\n```` (backreference match)
    block_match = re.search(r'^(`{3,})[a-zA-Z0-9_]*\n(.*)\1$', text, re.DOTALL | re.IGNORECASE)
    if block_match: return block_match.group(2).strip()
    # Case 2: Opening fence only (may be missing closing fence)
    match_open = re.search(r'^(`{3,})[a-zA-Z0-9_]*\n', text)
    if match_open:
        ticks = match_open.group(1)
        content = text[match_open.end():].strip()
        if content.endswith(ticks): content = content[:-len(ticks)].strip()
        return content
    # Case 3: Bare language identifier as first line (e.g. the LLM forgot the backtick fence).
    # Strip it so that words like 'markdown', 'python', 'json' etc. are not written into files.
    bare_lang_match = re.match(r'^([a-zA-Z][a-zA-Z0-9_]*)\n', text)
    if bare_lang_match:
        lang = bare_lang_match.group(1).lower()
        # Only strip recognised language / format names, not real content words
        KNOWN_LANGS = {
            'markdown', 'md', 'python', 'py', 'javascript', 'js', 'typescript', 'ts',
            'json', 'yaml', 'yml', 'html', 'css', 'bash', 'sh', 'text', 'txt',
            'sql', 'xml', 'toml', 'ini', 'java', 'cpp', 'c', 'rust', 'go', 'ruby',
            'php', 'swift', 'kotlin', 'r', 'scala', 'perl', 'lua', 'dart',
        }
        if lang in KNOWN_LANGS:
            return text[bare_lang_match.end():].strip()
    return text

def _tool_supports_param(tool_name: str, param: str) -> bool:
    """Check if a tool supports a specific parameter."""
    for t in TOOLS:
        if t["name"] == tool_name:
            return param in t.get("parameters", {}).get("properties", {})
    return False

def _parse_tool_call(text: str) -> List[Dict[str, Any]]:
    """Extract ALL tool calls from a block of text, supporting RAW BLOCK content."""
    if not text: return []
    actions = []
    starts = [m.start() for m in re.finditer(r'CALL:\s*\w+\s*\(', text, re.IGNORECASE)]
    
    for i, start_pos in enumerate(starts):
        end_boundary = starts[i+1] if i+1 < len(starts) else len(text)
        chunk = text[start_pos:end_boundary]
        
        tool_name, args, call_end = None, {}, -1
        match = re.search(r'CALL:\s*(\w+)\s*\(\s*(\{.*?\})\s*\)', chunk, re.DOTALL | re.IGNORECASE)
        if match:
            tool_name, json_str = match.group(1), match.group(2)
            call_end = match.end()
            try: args = json.loads(json_str)
            except json.JSONDecodeError:
                fixed = _fix_json_string(json_str)
                try: args = json.loads(fixed)
                except:
                    recovered = _recover_truncated_json(json_str)
                    try: args = json.loads(recovered)
                    except: pass
        else:
            tool_name, args, call_end = _manual_brace_parse(chunk)
            
        if tool_name and call_end != -1:
            remaining = chunk[call_end:].strip()
            # Clean up fences from remaining content.
            # We split remaining into lines and locate all fence lines starting with 3+ backticks.
            # If the first line is a fence, we strip it if and only if it's NOT the opening fence of a single fenced block.
            # A single fenced block is characterized by having exactly 2 fences (one at the start, one at the end).
            if remaining:
                lines = remaining.split('\n')
                fence_indices = [idx for idx, line in enumerate(lines) if re.match(r'^\s*`{3,}', line)]
                if fence_indices and fence_indices[0] == 0:
                    is_single_fenced_block = (len(fence_indices) == 2 and fence_indices[-1] == len(lines) - 1)
                    if not is_single_fenced_block:
                        remaining = '\n'.join(lines[1:]).strip()
            
            if remaining:
                supp_c = _tool_supports_param(tool_name, "content")
                supp_r = _tool_supports_param(tool_name, "replacement")
                if supp_c or supp_r:
                    content = _extract_raw_content(remaining)
                    if content:
                        content = re.sub(r'\\\\', r'\\', content)
                        if supp_c: args["content"] = content
                        elif supp_r: args["replacement"] = content
            actions.append({"tool": tool_name, "args": args})
    return actions

def _fix_json_string(json_str: str) -> str:
    # 1. Fix single backslashes (like Windows paths) that break JSON parsing
    # Escapes \ unless it's already a valid JSON escape char (", \, /, b, f, n, r, t, u)
    fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', json_str)
    # Fix trailing backslash if any
    fixed = re.sub(r'\\(?=")', r'\\\\', fixed)
    
    # 2. Fix single quotes to double quotes
    fixed = fixed.replace("'", '"')
    
    # 3. Fix unquoted keys
    fixed = re.sub(r'(\{|\,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', fixed)
    
    # 4. Remove trailing commas
    fixed = re.sub(r',\s*}', '}', fixed)
    fixed = re.sub(r',\s*]', ']', fixed)
    return fixed

def _recover_truncated_json(json_str: str) -> str:
    recovered = json_str.strip()
    if recovered.count('"') % 2 != 0: recovered += '"'
    open_b, close_b = recovered.count('{'), recovered.count('}')
    if open_b > close_b: recovered += '}' * (open_b - close_b)
    return recovered

def _manual_brace_parse(text: str) -> Tuple[Optional[str], Dict[str, Any], int]:
    match = re.search(r'CALL:\s*(\w+)\s*\((.*)', text, re.DOTALL | re.IGNORECASE)
    if not match: return None, {}, -1
    tool_name, args_raw = match.group(1), match.group(2).strip()
    
    # Recover missing curly braces: CALL: write_file("path": "...") -> {"path": "..."}
    if not args_raw.startswith('{') and args_raw.startswith('"'):
        # Find the closing parenthesis or backtick to wrap it
        end_paren = args_raw.find(')')
        if end_paren != -1:
            wrapped = "{" + args_raw[:end_paren] + "}"
            try: return tool_name, json.loads(_fix_json_string(wrapped)), match.start(2) + end_paren + 1
            except: pass

    brace_start = args_raw.find('{')
    if brace_start == -1: return tool_name, {}, -1
    
    opened, in_str, escape_next = 0, False, False
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
                    # Find the closing parenthesis for the CALL: function(args)
                    next_paren = args_raw.find(')', i + 1)
                    end_idx = match.start(2) + (next_paren + 1 if next_paren != -1 else i + 1)
                    try: return tool_name, json.loads(_fix_json_string(json_str)), end_idx
                    except: return tool_name, {}, end_idx
    return tool_name, {}, -1

# ─── Context Window Manager ──────────────────────────────────────────────────

IMPORTANT_KEYWORDS = {"success", "error", "failed", "exception", "found", "created", "modified", "deleted", "warning", "critical", "status"}

def _calculate_importance(message: Dict[str, Any]) -> float:
    content = str(message.get("content", ""))
    score = 0.0
    if message.get("role") == "function":
        score += 0.3
        if any(kw in content.lower() for kw in IMPORTANT_KEYWORDS): score += 0.4
    if message.get("role") == "user": score = 1.0
    if message.get("role") == "assistant" and "CALL:" in content: score += 0.2
    return min(score, 1.0)

def trim_conversation(messages: List[Dict], max_messages: int = 40) -> List[Dict]:
    system_msgs = [m for m in messages if m.get("role") == "system"]
    convo_msgs = [m for m in messages if m.get("role") != "system"]
    if len(convo_msgs) <= max_messages: return messages
    scored = [(i, msg, _calculate_importance(msg)) for i, msg in enumerate(convo_msgs)]
    recent_indices = set(range(max(0, len(convo_msgs) - 10), len(convo_msgs)))
    remaining_budget = max_messages - len(recent_indices)
    others = sorted([x for x in scored if x[0] not in recent_indices], key=lambda x: x[2], reverse=True)
    keep_indices = recent_indices.union(set([idx for idx, _, _ in others[:remaining_budget]]))
    preserved = sorted([msg for i, msg in enumerate(convo_msgs) if i in keep_indices], key=lambda m: convo_msgs.index(m))
    summary_msg = {"role": "system", "content": f"[Smart Trim: {len(convo_msgs)-len(preserved)} messages compressed.]"}
    return system_msgs + [summary_msg] + preserved

def validate_tool_args(tool_name: str, args: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Validate tool arguments against the schemas defined in TOOLS."""
    tool_schema = None
    for t in TOOLS:
        if t["name"] == tool_name:
            tool_schema = t
            break
            
    # Custom fallback for browser commands that aren't statically defined in TOOLS
    if not tool_schema and tool_name.startswith("browser_"):
        cmd = tool_name.split("_", 1)[1]
        if cmd == "goto":
            required = ["url"]
            properties = {"url": {"type": "string"}}
        elif cmd == "click":
            required = ["selector"]
            properties = {"selector": {"type": "string"}}
        elif cmd == "type":
            required = ["selector", "text"]
            properties = {"selector": {"type": "string"}, "text": {"type": "string"}}
        elif cmd in ["scroll", "screenshot", "close"]:
            required = []
            properties = {}
        else:
            return False, f"Unknown browser command: browser_{cmd}"
        
        tool_schema = {
            "name": tool_name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
        
    if not tool_schema:
        return False, f"Tool '{tool_name}' is not registered in the Tool Registry."
        
    parameters = tool_schema.get("parameters", {})
    if not parameters:
        return True, None
        
    # Check required fields
    required_fields = parameters.get("required", [])
    for field in required_fields:
        if field not in args:
            return False, f"Missing required parameter '{field}' for tool '{tool_name}'."
            
    # Check types
    properties = parameters.get("properties", {})
    for key, val in args.items():
        if key not in properties:
            continue
        expected_type = properties[key].get("type")
        if expected_type == "string" and not isinstance(val, str):
            return False, f"Parameter '{key}' must be a string, got {type(val).__name__}."
        elif expected_type == "integer" and not isinstance(val, (int, float)):
            return False, f"Parameter '{key}' must be an integer, got {type(val).__name__}."
        elif expected_type == "boolean" and not isinstance(val, bool):
            return False, f"Parameter '{key}' must be a boolean, got {type(val).__name__}."
        elif expected_type == "array" and not isinstance(val, list):
            return False, f"Parameter '{key}' must be an array (list), got {type(val).__name__}."
            
    return True, None

# ─── Main Agent ──────────────────────────────────────────────────────────────

class LeanAgent:
    def __init__(self, config: AgentConfig = None):
        self.config = config or AgentConfig()
        self.messages = []
        self.pending_approval = None
        self.latest_user_request = ""
        self.metrics = {"sessions": 0, "llm_calls": 0, "tool_calls": defaultdict(int), "total_tokens": 0, "errors": [], "start_time": None}

    def reset(self):
        self.messages, self.latest_user_request, self.pending_approval = [], "", None

    async def _call_llm_with_retry(self, messages: List[Dict], callback: Optional[Callable] = None) -> Any:
        last_error = None
        for attempt in range(self.config.max_retries):
            try:
                response = await asyncio.wait_for(
                    acompletion(model=self.config.model, messages=messages, api_base=self.config.api_base, functions=TOOLS, function_call="auto", stop=[f"[{TAG_REPORT}]", "````\n[REPORT]"]),
                    timeout=self.config.tool_timeout
                )
                self.metrics["llm_calls"] += 1
                self.metrics["total_tokens"] += getattr(response.usage, 'total_tokens', 0)
                return response
            except Exception as e:
                last_error = str(e)
                if callback: await callback({"type": "direct_terminal_result", "stdout": f"⚠️ LLM Error: {last_error} (attempt {attempt+1})\n", "agent_controlled": True})
            if attempt < self.config.max_retries - 1: await asyncio.sleep(self.config.retry_delay_base * (attempt + 1))
        raise RuntimeError(f"LLM call failed: {last_error}")

    async def run(self, user_input: str, callback: Optional[Callable] = None, images: Optional[List[str]] = None):
        from tool_registry import CURRENT_WORKSPACE
        self.latest_user_request, self.metrics["sessions"], self.metrics["start_time"] = user_input, self.metrics["sessions"]+1, time.time()
        user_content = [{"type": "text", "text": user_input}]
        if images:
            for img in images: user_content.append({"type": "image_url", "image_url": {"url": img}})
        self.messages.append({"role": "user", "content": user_content})
        
        # --- Auto-Recall Knowledge ---
        try:
            recall_result = recall_pattern(user_input, limit=2)
            if recall_result and recall_result.get("patterns"):
                knowledge_text = "=== PAST LEARNINGS FOUND ===\n"
                for p in recall_result["patterns"]:
                    knowledge_text += f"- Query: {p['query']}\n  Solution: {p['solution'][:500]}...\n"
                self.messages.append({"role": "system", "content": knowledge_text})
        except Exception as e:
            if callback: await callback({"type": "direct_terminal_result", "stdout": f"⚠️ Auto-Recall Error: {e}\n"})
        # -----------------------------
        
        loop_count = 0
        while True:
            loop_count += 1
            if loop_count > self.config.max_iterations:
                err = "Max iterations exceeded."
                if callback: await callback({"type": "final_response", "content": err})
                return err
            system_prompt = build_system_prompt(CURRENT_WORKSPACE)
            sanitized_history = copy.deepcopy(trim_conversation(self.messages, self.config.max_history_messages))
            for m in sanitized_history:
                if isinstance(m.get("content"), str):
                    m["content"] = sanitize_conversation_paths(m["content"])
                elif isinstance(m.get("content"), list):
                    for item in m["content"]:
                        if item.get("type") == "text":
                            item["text"] = sanitize_conversation_paths(item["text"])
            assistant_turns = len([m for m in self.messages if m.get("role") == "assistant"])
            reinforcement = build_reinforcement_prompt(CURRENT_WORKSPACE, self.latest_user_request, full_protocols=system_prompt if assistant_turns > 0 and assistant_turns % 5 == 0 else "")
            try: response = await self._call_llm_with_retry([{"role": "system", "content": system_prompt}] + sanitized_history + [{"role": "system", "content": reinforcement}], callback)
            except Exception as e:
                self.metrics["errors"].append(str(e))
                return f"Error: {e}"
            message = response.choices[0].message
            content = message.get("content", "") or ""
            self.messages.append(message)
            parsed = parse_structured_response(content)
            if parsed["think"] and callback: await callback({"type": "agent_thinking", "content": parsed["think"]})
            if not parsed.get("actions"):
                res = parsed.get("report") or content
                
                # --- Auto-Learn Pattern ---
                if loop_count > 1 and not str(res).startswith("Error:"):
                    try:
                        seq = [{"tool": k, "count": v} for k, v in self.metrics.get("tool_calls", {}).items()]
                        learn_pattern(
                            task_type="auto_learned_task",
                            query=self.latest_user_request,
                            solution=str(res)[:2000],
                            tool_sequence=seq,
                            tags=["auto"]
                        )
                    except Exception as e:
                        if callback: await callback({"type": "direct_terminal_result", "stdout": f"⚠️ Auto-Learn Error: {e}\n"})
                # --------------------------
                
                if callback: await callback({"type": "final_response", "content": res})
                return res
            action_matches = list(re.finditer(r'(?:\[/ACTION\]|</ACTION>)', content, re.IGNORECASE))
            if action_matches:
                truncated = content[:action_matches[-1].end()]
                # Find and update the latest assistant message that contains an ACTION
                for i in range(len(self.messages) - 1, -1, -1):
                    msg_obj = self.messages[i]
                    if msg_obj.get("role") == "assistant":
                        msg_content = msg_obj.get("content") or ""
                        if "[ACTION]" in msg_content or "<ACTION>" in msg_content:
                            msg_obj["content"] = truncated
                            break
            await self._execute_tool_calls(parsed["actions"], callback)

    async def _execute_tool_calls(self, actions: List[Dict], callback: Optional[Callable]):
        async def _exec_one(t_name, t_args):
            if callback:
                await callback({"type": "tool_start", "tool": t_name, "args": t_args})
                await callback({"type": "direct_terminal_result", "stdout": f"⚙️ Executing {t_name}...\n", "agent_controlled": True})
            try:
                res = await handle_tool_call(t_name, t_args, callback)
            except Exception as e: res = {"error": str(e)}
            self.metrics["tool_calls"][t_name] += 1
            if callback: await callback({"type": "tool_result", "tool": t_name, "result": res})
            return t_name, res
        pending_reads = []
        async def flush_reads():
            if not pending_reads: return
            results = await asyncio.gather(*[_exec_one(n, a) for n, a in pending_reads])
            for t_name, result in results: self._store_result(t_name, result)
            pending_reads.clear()
        for action in actions:
            t_name, t_args = action["tool"], action["args"]
            
            # Validation Gate
            valid, err_msg = validate_tool_args(t_name, t_args)
            if not valid:
                validation_res = {"error": "SchemaValidationError", "message": err_msg}
                if callback:
                    await callback({"type": "direct_terminal_result", "stdout": f"❌ Tool Validation Failed: {err_msg}\n", "agent_controlled": True})
                self._store_result(t_name, validation_res)
                continue
                
            from tool_registry import requires_approval
            if requires_approval(t_name, t_args):
                await flush_reads()
                if callback: await callback({"type": "require_approval", "tool": t_name, "args": t_args})
                self.pending_approval = asyncio.get_event_loop().create_future()
                if await self.pending_approval:
                    _, result = await _exec_one(t_name, t_args)
                    self._store_result(t_name, result)
                else: self._store_result(t_name, {"error": "Denied."})
                self.pending_approval = None
            elif t_name in self.config.read_only_tools: pending_reads.append((t_name, t_args))
            else:
                await flush_reads()
                _, result = await _exec_one(t_name, t_args)
                self._store_result(t_name, result)
        await flush_reads()

    def _store_result(self, t_name, result):
        if isinstance(result, dict) and (result.get("is_image") or "base64_image" in result):
            b64 = result.pop("content") if result.get("is_image") else result.pop("base64_image")
            self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})
            self.messages.append({"role": "user", "content": [{"type": "text", "text": f"Image from {t_name}:"}, {"type": "image_url", "image_url": {"url": f"data:image/{result.get('ext', 'jpeg')};base64,{b64}"}}]})
        elif isinstance(result, dict) and t_name == "read_files" and "files" in result:
            self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})
            imgs = [{"type": "text", "text": "Images:"}]
            for p, c in result["files"].items():
                if isinstance(c, str) and c.startswith("IMAGE:"): imgs.append({"type": "image_url", "image_url": {"url": f"data:image/{p.split('.')[-1] if '.' in p else 'jpeg'};base64,{c[6:]}"}})
            if len(imgs) > 1: self.messages.append({"role": "user", "content": imgs})
        else: self.messages.append({"role": "function", "name": t_name, "content": json.dumps(result)})

if __name__ == "__main__":
    agent = LeanAgent()
    async def cli_main():
        print(f"--- ZeroBound CLI (v{PROMPT_VERSION}) ---")
        while True:
            try:
                u = input("\n> ")
                if u.lower() in ["exit", "quit"]: break
                if u.lower() == "stats": print(json.dumps(agent.metrics, indent=2)); continue
                await agent.run(u, callback=lambda d: print(f"[{d['type']}] {str(d.get('content') or d.get('result'))[:200]}"))
            except KeyboardInterrupt: break
            except Exception as e: print(f"Error: {e}")
    asyncio.run(cli_main())