# AntiGravity Lean – Project Architecture

> **Version:** Post-v1 (April 2026 — Live Terminal, Async Agent, Markdown Rendering)  
> This document is the single source of truth for the internal design and data flow of the AntiGravity Lean platform.

---

## 1. High-Level Concept

AntiGravity Lean is a **native, zero-containerization autonomous engineering platform**. It replaces expensive cloud APIs by automating a *real browser session* on sites like DeepSeek and Claude, turning free web AI into a full-capability local agent with:

- **File system read/write access** on your machine
- **Live shell command execution** with real-time streaming output
- **Human-in-the-loop approval** for risky operations
- **A full IDE dashboard** with Monaco editor, file explorer, and integrated terminal

The system has three major runtime processes:

| Process | Entry | Port | Role |
|---|---|---|---|
| **LLM-Web-Router** | `llm-web-router/server.py` | `8000` | Browser automation – drives DeepSeek/Claude and returns AI responses |
| **Agent Bridge** | `lean-agent/server_bridge.py` | `8001` | WebSocket gateway between the UI and the Agent Brain |
| **Dashboard** | `lean-agent/ui/index.html` | — (static file) | User-facing IDE – chat, file explorer, Monaco editor, live terminal |

---

## 2. System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    USER'S BROWSER (index.html)                   │
│  ┌─────────────┐  ┌───────────────┐  ┌──────────────────────┐  │
│  │ Chat Panel  │  │ Monaco Editor │  │   Agent Shell / Term │  │
│  │ (Markdown,  │  │ (File viewer) │  │   (Live streaming    │  │
│  │  KaTeX,     │  │               │  │    subprocess output)│  │
│  │  Highlight) │  │               │  │                      │  │
│  └─────────────┘  └───────────────┘  └──────────────────────┘  │
└──────────────────────────┬───────────────────────────────────────┘
                           │ WebSocket ws://localhost:8001/ws
┌──────────────────────────▼───────────────────────────────────────┐
│               SERVER BRIDGE  (server_bridge.py)                  │
│  - Owns the WebSocket connection to the UI                       │
│  - Runs agent.run() as asyncio.create_task() (non-blocking)      │
│  - Handles: message | get_file | reset | pick_folder |           │
│             refresh_tree | load_history | approval_decision       │
│  - Calls broadcast_state() after every file/workspace change     │
└──────────────────────────┬───────────────────────────────────────┘
                           │ Python function calls
┌──────────────────────────▼───────────────────────────────────────┐
│                   AGENT BRAIN  (agent_brain.py)                  │
│  - Maintains conversation history (messages[])                   │
│  - Calls LiteLLM → LLM-Web-Router for completions               │
│  - Parses CALL: tool_name({...}) text-based tool calls           │
│  - Brace-counting JSON extractor prevents greedy regex bugs      │
│  - Truncates multi-call blocks to ONE call per turn              │
│  - Gated by requires_approval() before dangerous tool execution  │
│  - Awaits asyncio.Future for human approval before proceeding    │
└───────────────┬───────────────────────────┬──────────────────────┘
                │ HTTP POST /v1/chat/        │ function call
                │ completions               │
┌───────────────▼──────────┐  ┌─────────────▼────────────────────┐
│  LLM-WEB-ROUTER          │  │  TOOL REGISTRY (tool_registry.py) │
│  (server.py, port 8000)  │  │                                    │
│  - FastAPI + Playwright  │  │  Safe tools (no approval):         │
│  - Drives Edge browser   │  │   - list_files, read_file          │
│  - Injects system prompt │  │   - set_workspace, create_folder   │
│    on first turn         │  │                                    │
│  - Sends only delta      │  │  Approval-required tools:          │
│    messages on follow-up │  │   - run_shell_command (Popen +     │
│  - Returns stable AI     │  │     thread streaming)              │
│    response text         │  │   - write_file (shows diff)        │
│  - /v1/current_url       │  │                                    │
│  - /v1/navigate          │  │  get_file_tree() for Explorer      │
└──────────────────────────┘  └────────────────────────────────────┘
```

---

## 3. Component Deep-Dive

### 3.1. LLM-Web-Router (`llm-web-router/`)

**`server.py`** — FastAPI server on port 8000.

On startup it opens a **non-headless Microsoft Edge** window and authenticates using the saved Playwright storage state (`profiles/deepseek/state.json`). This browser window stays alive as long as the server runs.

**Key design decisions:**

- **Turn 1 (First message):** The system prompt from `agent_brain.py` is injected *inline* into the user message because DeepSeek's web interface has no API-level system slot. The prompt explains the agent's role, tool format, and workspace.
- **Turn N (Follow-up):** Only the content *since the last assistant message* is sent — tool results, error outputs, and new user messages. This prevents the context from growing unboundedly.
- **Poll-based stability detection:** The router waits for the AI to stop changing its response for 2 consecutive seconds before returning it. This avoids cutting off mid-thought responses.

**`config.py`** — Model registry. Maps model name strings to browser selectors and URLs. Currently supports `deepseek` and `claude`. 

**`manual_login.py`** — One-time script that opens a browser for you to manually log in. Saves the session cookie state to `profiles/<model>/state.json` for all future use.

---

### 3.2. Agent Brain (`lean-agent/agent_brain.py`)

The `LeanAgent` class is a **ReAct loop** (Reason + Act). It:

1. Sends the full `messages` history to the LLM-Web-Router via LiteLLM
2. Parses the response for a `CALL: tool_name({...})` pattern
3. If found, either awaits user approval or executes immediately
4. Appends the tool result back to `messages` as a `function` role entry
5. Loops back to step 1 until the agent produces a response with no tool call

**Critical implementation details:**

- **Multi-call Truncation:** When the LLM outputs multiple `CALL:` blocks in one response (e.g. 8 commands at once), the brain strips everything after the first one from the stored history. This prevents the LLM from "hallucinating" fake tool results for the unexecuted calls.
- **Brace-counting JSON Parser:** Uses a manual `{` / `}` counter to extract the exact argument JSON blob, so trailing prose (`", "timeout": 120`) never contaminates the parsed command.
- **Approval Future:** For dangerous tools, a `asyncio.Future` is stored as `self.pending_approval`. The agent *awaits* it, freezing execution until the bridge calls `.set_result(True/False)` upon receiving the user's click.
- **Multimodal Support:** Images are sent as `image_url` parts in the user content array (base64 encoded).

**System Prompt:** Instructs the agent to never ask the user to run commands manually, always verify files before claiming they exist, and use the `CALL:` format for every tool.

---

### 3.3. Server Bridge (`lean-agent/server_bridge.py`)

The central **WebSocket hub** connecting the UI to the agent. Runs on port 8001 via Uvicorn.

**Key design: Non-blocking Agent Execution**

The agent runs as an `asyncio.create_task()` background task — **never** as a blocking `await`. This is critical because:
- The bridge must keep the WebSocket read loop alive to catch `approval_decision` messages
- Without this, clicking "Accept" would never be received while the agent was waiting for approval, creating a permanent deadlock

**WebSocket message types handled:**

| Incoming Type | Action |
|---|---|
| `message` | Spawns agent task with `asyncio.create_task()` |
| `get_file` | Reads file from disk, sends `file_content` to UI |
| `reset` | Clears agent memory and session state |
| `refresh_tree` | Re-broadcasts file tree (Explorer ↺ button) |
| `pick_folder` | Opens OS folder dialog, changes workspace |
| `load_history` | Restores a saved session, navigates browser to saved URL |
| `delete_history` | Removes a saved session JSON |
| `direct_command` | Runs a manual user-typed shell command |
| `approval_decision` | Resolves the `pending_approval` future (Accept/Reject) |

**Session Persistence:** After every final response, the bridge saves a session JSON containing the workspace path and the DeepSeek chat URL. Sessions are listed in the History panel and can be fully restored.

---

### 3.4. Tool Registry (`lean-agent/tool_registry.py`)

All agent capabilities are declared here as both **Python functions** and **OpenAI-style function schemas** (for LiteLLM).

**Tools table:**

| Tool | Requires Approval? | Description |
|---|---|---|
| `run_shell_command` | ✅ Yes (unless on safe list) | Executes any shell command via `subprocess.Popen` with live streaming |
| `write_file` | ✅ Always | Writes a file; shows a unified diff before applying |
| `read_file` | No | Reads a file's content |
| `list_files` | No | Lists directory contents |
| `create_folder` | No | Creates a directory tree |
| `set_workspace` | No | Changes the agent's active working directory |

**Safe command list (no approval needed):**
```python
SAFE_COMMANDS = ['dir', 'ls', 'pwd', 'echo', 'git status', 'git log',
                 'python --version', 'pip --version', 'npm --version', 'node --version']
```

**Live Terminal Streaming Architecture:**

Because Uvicorn on Windows uses the `WindowsSelectorEventLoopPolicy` which *cannot* use `asyncio.create_subprocess_shell` (raises `NotImplementedError`), the async shell is implemented using:
1. A standard `subprocess.Popen` in a dedicated system thread
2. Two parallel reader threads (stdout + stderr), each streaming 1024-byte chunks
3. `asyncio.run_coroutine_threadsafe()` to bounce the chunks back onto the event loop and send them to the frontend WebSocket

This architecture produces true real-time streaming without blocking the event loop.

**File Tree:** `get_file_tree()` recursively builds a JSON node tree of the workspace. Hidden directories: `.git`, `__pycache__`, `.pytest_cache`. All other directories (including `venv`, `node_modules`) are shown, matching standard IDE behaviour.

---

### 3.5. Dashboard UI (`lean-agent/ui/index.html`)

A single-file SPA with no build step. Libraries loaded from CDN:

| Library | Purpose |
|---|---|
| Monaco Editor (v0.44) | Full IDE-grade code editor with syntax highlighting |
| `marked.js` | Markdown → HTML conversion (GFM + `breaks`) |
| `highlight.js` (v11.9, Atom One Dark) | Syntax-highlighted fenced code blocks |
| KaTeX (v0.16.9) | LaTeX math rendering — both `$$block$$` and `$inline$` |
| Google Fonts — Outfit + JetBrains Mono | Typography |

**Markdown Rendering Pipeline (`renderMarkdown`):**
1. Extract all `$$...$$` and `$...$` patterns into a placeholder array before `marked` runs (prevents marked from escaping LaTeX characters)
2. Pass processed text through `marked.parse()`
3. Substitute placeholders back with `katex.renderToString()` output

**Terminal:**
- **Shell 1, 2, ...** — user-controlled manual terminals
- **🤖 Agent Shell** — auto-created when the agent runs a shell command; streams live output; automatically switches focus to this tab

**Chat Panel:**
- User messages: rendered as plain pre-wrapped text (no markdown parsing, prevents injection)
- Agent messages: rendered through the full `renderMarkdown` pipeline with code highlighting, tables, math

**Review Cards (Human-in-the-Loop):**
- Appear when the agent wants to run a shell command or write a file
- Show a unified color-coded diff for file writes, or the raw command for shell runs
- "Accept" → sends `approval_decision: true` WebSocket message → `pending_approval.set_result(True)` in the bridge → agent proceeds
- "Reject" → agent receives `{"error": "User denied..."}`

---

## 4. Data & Control Flow (End-to-End)

```
User types "Create a Poisson plot" → sendMessage()
    ↓
WebSocket: {type: "message", content: "..."}
    ↓
server_bridge.py → asyncio.create_task(run_agent_task())
    ↓
agent_brain.py → messages.append({role:"user"}) → LiteLLM POST /v1/chat/completions
    ↓
llm-web-router/server.py → types in Edge browser → polls for stable response
    ↓
Agent parses: CALL: run_shell_command({"command": "python -m venv venv"})
    ↓
requires_approval() → True
    ↓
callback({type:"require_approval", ...}) → UI renders Review Card
    ↓                                        User clicks Accept
    ↓ ← ← ← ← ← ← WebSocket: {type:"approval_decision", decision:true} ← ← ←
    ↓
pending_approval.set_result(True)
    ↓
run_command_async() → subprocess.Popen → reader threads → run_coroutine_threadsafe
    ↓
callback({type:"direct_terminal_result", stdout:"..."}) → Agent Shell updates live
    ↓
result appended to messages as {role:"function", name:"run_shell_command", content:...}
    ↓
Loop back to LiteLLM for next step...
    ↓
... (multiple tool call cycles) ...
    ↓
No CALL: found in response → callback({type:"final_response", content:"..."})
    ↓
UI: renderMsg('assistant', content) → renderMarkdown() → displayed in chat
```

---

## 5. History & Session Persistence

Managed by `lean-agent/history_manager.py`. Sessions are stored as JSON files in `lean-agent/History/`.

Each session file stores:
- `workspace`: Last active workspace path
- `deepseek_url`: The DeepSeek browser chat URL (for resuming the same conversation thread)
- `updated_at`: Timestamp

When a session is loaded, the bridge calls `/v1/navigate` on the router to restore the exact DeepSeek conversation, allowing the agent to have full context of what was discussed.

---

## 6. Known Limitations & Design Constraints

| Constraint | Reason |
|---|---|
| Windows Selector Event Loop | Uvicorn + Windows forces `WindowsSelectorEventLoopPolicy`; `asyncio.create_subprocess_shell` raises `NotImplementedError`. Worked around via `subprocess.Popen` + threads. |
| DeepSeek response latency | Browser automation is ~3–5s slower than a direct API call. Mitigated by the poll-stability algorithm. |
| Single active conversation | The router manages one browser page per model. Concurrent requests would interleave. |
| No streaming tokens | The router waits for the full response before returning. True token streaming would require OCR/DOM diffing. |
| Tool call format | DeepSeek's web interface doesn't support OpenAI function calling natively; the agent uses text-based `CALL:` formatting instead. |
