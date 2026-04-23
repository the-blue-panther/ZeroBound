# ZeroBound – Lightweight Browser-Based Coding Agent

https://img.shields.io/badge/License-MIT-yellow.svg
https://img.shields.io/badge/python-3.11+-blue.svg
https://img.shields.io/badge/status-active-success.svg

ZeroBound is an ultra-robust, state-driven autonomous coding assistant. It bridges the gap between raw LLM capabilities and reliable engineering workflows by automating real browser sessions on free AI platforms (DeepSeek, Claude) and turning them into a full-capability local agent with:

- File system read/write access on your machine
- Live shell command execution with real-time streaming output
- Human-in-the-loop approval for risky operations
- Full IDE dashboard with Monaco editor, file explorer, integrated terminal, and chat
- Persistent memory across sessions using SQLite knowledge base
- Session persistence – resume conversations exactly where you left off

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11 or higher
- Microsoft Edge browser (for LLM-Web-Router automation)
- Git (optional, for version control integration)

### Installation

1. Clone the repository
   bash
 git clone https://github.com/yourusername/zero-bound.git
 cd zero-bound


2. Set up Python virtual environment
   bash
 python -m venv venv
 # On Windows:
 venv\\Scripts\\activate
 # On Linux/Mac:
 source venv/bin/activate


3. Install dependencies
   bash
 pip install -r requirements.txt


   Key dependencies include:
   - litellm – LLM API abstraction
   - fastapi + uvicorn – WebSocket server bridge
   - playwright – Browser automation
   - httpx – Async HTTP client

4. Install Playwright browsers (for LLM-Web-Router)
   bash
 playwright install msedge


5. Authenticate with your AI provider
   bash
 cd llm-web-router
 python manual_login.py

   This opens a browser window where you log into DeepSeek (or Claude). The session cookies are saved for future use.

---

## 🏃 Running ZeroBound

### 1. Start the LLM-Web-Router
bash
cd llm-web-router
python server.py

This launches a FastAPI server on http://localhost:8000 and opens an Edge browser window. Keep this terminal open.

### 2. Start the Agent Bridge
bash
cd lean-agent
python server_bridge.py

Launches WebSocket server on ws://localhost:8001 and serves the UI.

### 3. Open the Dashboard
Navigate to http://localhost:8001 in your browser.

---

## 🎯 Features

### 💬 Chat Panel
- Markdown rendering with GitHub Flavored Markdown
- Syntax highlighting via highlight.js (Atom One Dark theme)
- LaTeX math support (both $$block$$ and $inline$ via KaTeX)
- Agent responses are fully rendered; user input appears as plain text

### 📁 File Explorer
- Recursive tree view of your workspace
- Click any file to open in Monaco editor
- Right-click for context menu (delete, rename, reveal in OS)
- Automatic refresh after file operations

### ✏️ Monaco Editor
- Full IDE-grade code editor
- Syntax highlighting for 100+ languages
- File saving (Ctrl+S) writes back to disk
- Diff view for pending changes

### 🖥️ Integrated Terminal
- User terminals – manual command execution
- Agent Shell – auto-created for agent commands, streams live output
- Real-time streaming using threaded subprocess + asyncio.run_coroutine_threadsafe
- Persistent across agent sessions

### 🧠 Autonomous Agent
- ReAct loop – Reason + Act architecture
- Tool calling via text-based CALL: tool_name({...}) format
- Multi-call truncation – prevents hallucinated tool results
- Brace-counting JSON parser – robust against malformed JSON
- Human-in-the-loop approval for dangerous operations (shell commands, file writes)

### 📚 Knowledge Base
- Persistent memory across sessions using SQLite (knowledge.db)
- Pattern learning – stores successful solutions
- Pattern recall – retrieves relevant past solutions based on keyword matching
- Success/failure tracking for continuous improvement

### 💾 Session Persistence
- Automatic saving after each agent response
- Restore previous sessions with full context (workspace + DeepSeek chat URL)
- History panel for browsing and loading past sessions
- Manual deletion of unwanted sessions

---

## 🛠️ Available Tools

The agent can invoke the following tools (all implemented in tool_registry.py):

| Tool | Requires Approval | Description |
|------|------------------|-------------|
| read_file | ❌ No | Read file contents (supports line ranges, pagination) |
| write_file | ✅ Yes | Write or overwrite a file (shows diff preview) |
| edit_file | ✅ Yes | Surgical block replacement with fuzzy matching |
| append_file | ✅ Yes | Append content to existing file |
| run_shell_command | ⚠️ Safe commands only | Execute shell command with live streaming |
| list_files | ❌ No | List directory contents |
| find_files | ❌ No | Search for files by glob pattern |
| grep_search | ❌ No | Search file contents with regex |
| create_folder | ❌ No | Create directory (automatic parents) |
| delete_file | ✅ Yes | Delete file or folder |
| move_file | ✅ Yes | Move or rename |
| copy_file | ✅ Yes | Copy file or folder |
| set_workspace | ❌ No | Change active working directory |
| get_file_info | ❌ No | Get metadata (size, type, modified time) |
| start_background_command| ✅ Yes | Long-running background process |
| read_process_output | ❌ No | Read output from background process |
| kill_process | ✅ Yes | Terminate background process |
| search_web | ❌ No | DuckDuckGo web search |
| read_url | ❌ No | Fetch URL content as plain text |
| browser_* | ⚠️ Varies | Browser automation (click, type, screenshot, etc.) |
| store_memory | ❌ No | Store key-value in persistent memory |
| recall_memory | ❌ No | Search persistent memory |
| learn_pattern | ❌ No | Store solution pattern in knowledge base |
| recall_pattern | ❌ No | Retrieve similar patterns |

Safe commands (no approval needed for run_shell_command):
- dir, ls, pwd, echo
- git status, git log
- python --version, pip --version, npm --version, node --version

---

## 🏗️ Architecture

ZeroBound runs as three coordinated processes:


┌─────────────────────────────────────────────────────────────┐
│ USER'S BROWSER (Dashboard) │
│ ┌──────────┐ ┌──────────────┐ ┌──────────────────────┐ │
│ │ Chat │ │Monaco Editor │ │ Agent Shell / │ │
│ │ Panel │ │ (File Viewer)│ │ Terminal (Live) │ │
│ └──────────┘ └──────────────┘ └──────────────────────┘ │
└──────────────────────────┬──────────────────────────────────┘
 │ WebSocket (port 8001)
┌──────────────────────────▼──────────────────────────────────┐
│ SERVER BRIDGE (server_bridge.py) │
│ - WebSocket hub (non-blocking agent execution) │
│ - Session persistence & history management │
│ - Approval future resolution │
└──────────────────────────┬──────────────────────────────────┘
 │ Function calls
┌──────────────────────────▼──────────────────────────────────┐
│ AGENT BRAIN (agent_brain.py) │
│ - Conversation history (trimmed for context) │
│ - ReAct loop + tool call parsing │
│ - Multi-call truncation + brace-counting JSON extraction │
│ - Approval gating & future awaiting │
└──────────┬───────────────────────────────┬──────────────────┘
 │ HTTP (port 8000) │ Tool execution
┌──────────▼──────────┐ ┌──────────▼──────────────────┐
│ LLM-WEB-ROUTER │ │ TOOL REGISTRY │
│ (server.py) │ │ (tool_registry.py) │
│ - FastAPI + │ │ - File system ops │
│ Playwright │ │ - Shell execution (threaded)│
│ - Edge automation │ │ - Web search/URL fetch │
│ - Response polling │ │ - Browser automation │
│ & stabilization │ │ - Memory & knowledge base │
└─────────────────────┘ └─────────────────────────────┘


Key design decisions:
- Non-blocking agent – runs as asyncio.create_task() so approval messages can be received
- Live terminal streaming – subprocess.Popen + reader threads + run_coroutine_threadsafe (workaround for Windows create_subprocess_shell limitation)
- Session restoration – saves workspace + DeepSeek URL; restores by navigating browser
- Human-in-the-loop – review cards with diffs for file writes; explicit accept/reject

---

## 📂 Project Structure


zero-bound/
├── lean-agent/ # Core agent system
│ ├── agent_brain.py # ReAct loop + LLM orchestration
│ ├── server_bridge.py # WebSocket server (port 8001) + UI host
│ ├── tool_registry.py # All tool implementations + schemas
│ ├── knowledge_base.py # SQLite pattern learning/recall
│ ├── history_manager.py # Session save/load/delete
│ ├── browser_manager.py # Browser automation helpers
│ ├── ui/
│ │ └── index.html # Single-file SPA dashboard
│ ├── History/ # Session JSON files (auto-generated)
│ └── scratch/ # Test scripts
│
├── llm-web-router/ # Browser automation layer
│ ├── server.py # FastAPI server (port 8000)
│ ├── config.py # Model selectors & URLs
│ ├── manual_login.py # One-time authentication
│ └── profiles/ # Browser storage states
│
├── Documentation/
│ ├── project_architecture.md # Detailed internal design (276 lines)
│ └── user_manual.md # End-user guide
│
├── Grok Chat/ # Archived LLM interactions
├── Future Planned Features/ # Roadmap & ideas
├── workspaces/ # Example workspaces
├── knowledge.db # Pattern learning database (auto)
├── memories.db # Key-value memory storage (auto)
├── requirements.txt # Python dependencies
└── README.md # This file


---

## 🔧 Configuration

### Workspace Path
The active workspace defaults to the directory containing lean-agent/. Change it via:
- UI: Click "Pick Folder" button in the toolbar
- Agent: set_workspace({path: "C:/your/project"})

### Model Selection
Edit llm-web-router/config.py to switch between deepseek and claude. The router maps model names to:
- Browser URL
- CSS selectors for input box, send button, response container, stop button
- Upload selectors for images

### Context Window
agent_brain.py maintains conversation history with:
- MAX_HISTORY_MESSAGES = 40 (keeps last N messages)
- SUMMARY_TRIGGER = 50 (when raw history exceeds this, summarize old part)
- Trim strategy: preserve user messages + errors; compress older content

---

## 🤝 Human-in-the-Loop Workflow

When the agent attempts a dangerous operation (file write, shell command not on safe list):

1. Review Card appears in the chat panel showing:
   - For shell commands: The command string + working directory
   - For file writes: Unified diff (color-coded: red deletions, green additions)

2. Buttons:
   - ✅ Accept – Agent executes the operation
   - ❌ Reject – Agent receives {"error": "User denied..."} and can retry or change approach

3. Agent behavior after rejection:
   - The tool result shows the denial error
   - Agent can propose an alternative solution or ask clarifying questions

---

## 🧪 Testing

Run the test suite:
bash
cd lean-agent
pytest test_*.py


Available tests:
- test_parser.py – Response parsing from LLM
- test_robust_parser.py – Edge-case JSON extraction
- test_browser.py – Browser automation helpers
- test_server.py – WebSocket bridge functionality
- test_backend.py – Tool registry integration

---

## 🐛 Known Limitations

| Limitation | Workaround |
|------------|------------|
| Windows event loop doesn't support create_subprocess_shell | Use threaded subprocess.Popen + coroutine bridge |
| DeepSeek response latency (3–5s) | Poll-based stability detection; acceptable for autonomy |
| Single active conversation per model | One browser page per model; concurrent requests would interleave |
| No streaming tokens from browser | Full response only; true streaming would require DOM diffing |
| Text-based tool calling (not native function calling) | CALL: format with robust JSON extraction |

---

## 📄 License

MIT License – see LICENSE file for details.

---

## 🙏 Acknowledgments

- Built on LiteLLM for LLM abstraction
- Powered by Playwright for browser automation
- UI by Monaco Editor, marked.js, highlight.js, KaTeX

---

## 📞 Support & Contributing

- Issues: Open a GitHub issue
- Contributions: PRs welcome! See Future Planned Features/ for ideas
- Documentation: See Documentation/ for architecture deep-dive

Start your autonomous engineering journey today! 🚀
