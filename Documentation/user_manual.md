# AntiGravity Lean – User Manual

> **Platform:** Windows 10/11 · Python 3.11+ · Microsoft Edge required  
> **Version:** Post-v1 (April 2026)

---

## 1. What Is AntiGravity Lean?

AntiGravity Lean is a **local autonomous AI coding agent** that runs entirely on your machine — no cloud subscriptions, no API keys, no monthly bills. It drives your browser to use DeepSeek's (or Claude's) free web interface as its AI brain, while having full read/write/execute access to your local project files.

What makes it an **agent**, not a chatbot:
- It breaks your request into **steps** and executes them one-by-one
- It runs **real terminal commands** and reads the output before deciding the next step
- It **writes files** and shows you a diff to review before saving
- It **verifies its own work** by checking error messages and retrying

You stay in full control via the **Human-in-the-Loop** approval system — nothing dangerous runs without your click.

---

## 2. First-Time Setup

### 2.1. Prerequisites

- Python 3.11+ installed
- Microsoft Edge installed (required for the browser automation)
- A free DeepSeek account at [chat.deepseek.com](https://chat.deepseek.com)

### 2.2. Install Dependencies

Open two terminal windows in the project root:

**Terminal A — LLM Web Router:**
```powershell
cd llm-web-router
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium  # Downloads Playwright browser drivers
```

**Terminal B — Agent:**
```powershell
cd lean-agent
# Activate lean-agent's native virtual environment
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Important:** `lean-agent` uses its **own** virtual environment. Do **not** activate `..\llm-web-router\venv` when installing or starting the agent.

### 2.3. Log In to DeepSeek (One-Time Only)

This step saves your browser session so the router can reuse it automatically:

```powershell
cd llm-web-router
.\venv\Scripts\Activate.ps1
python manual_login.py deepseek
```

A browser window will open at `chat.deepseek.com`. Log in manually with your credentials, then press **Enter** in the terminal. Your session is saved to `profiles/deepseek/state.json` and will be reused on every future start.

> **You only need to do this once**, unless you log out or your session expires.

---

## 3. Starting the Platform

You need **two terminal windows** running simultaneously. Open them in this order:

### Step 1 — Start the AI Brain (LLM-Web-Router)

```powershell
cd llm-web-router
.\venv\Scripts\Activate.ps1
python server.py
```

A **Microsoft Edge window will open automatically** and navigate to DeepSeek. This is not a bug — it's intentional! The router drives this browser to communicate with the AI. 

> ⚠️ **Do not close this Edge window** while using the agent. You can minimize it.

You'll see output like:
```
🚀 Loading browser for deepseek...
🌐 Navigating to deepseek chat...
✅ deepseek ready and visible!
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Step 2 — Start the Agent Server

```powershell
cd lean-agent
.\venv\Scripts\Activate.ps1
python server_bridge.py
```

You'll see:
```
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8001
```

### Step 3 — Open the Dashboard

Open `lean-agent/ui/index.html` in any browser (Chrome, Edge, Firefox). The IDE will connect automatically.

> 💡 Tip: Drag the file into your browser or right-click → "Open with" your browser.

---

### 3.1. Startup Safety Rules

- Run **only one** `python server_bridge.py` process at a time.
- If the bridge is already running, do **not** start another copy in a different terminal.
- Always start the bridge from inside `lean-agent` using `.\venv\Scripts\Activate.ps1`.
- The current bridge is a **single-process** server. You should **not** see `Will watch for changes` or `Started reloader process`.
- Port `8001` is preferred, but if it is already occupied the bridge may start on `8010` or `8011`.
- If the status badge says `Connected :8001`, `Connected :8010`, or `Connected :8011`, the dashboard is connected correctly.
- After changing `index.html` or restarting the bridge, close and reopen the dashboard tab so the latest UI code is loaded.

## 4. Using the Dashboard

### 4.1. Layout Overview

```
┌─────────────┬──────────────────────────────┬─────────────┐
│  SIDEBAR    │       EDITOR                 │   CHAT      │
│  Explorer   │  (Monaco code editor)        │  (Agent     │
│  History    │                              │   output)   │
│             ├──────────────────────────────┤             │
│             │       TERMINAL               │  Input bar  │
└─────────────┴──────────────────────────────┴─────────────┘
```

| Panel | Description |
|---|---|
| **Explorer** | File tree of your current workspace. Click a file to open it in the editor. |
| **History** | List of previously saved agent sessions. Click to restore. |
| **Editor** | Monaco editor — the same engine as VS Code. Full syntax highlighting. |
| **Terminal** | Multi-tab terminal. 🖥 Shell tabs for manual commands, 🤖 Agent Shell for agent commands. |
| **Chat** | Two-way conversation with the agent. Renders full Markdown, code blocks, tables, and LaTeX math. |

### 4.2. Setting Your Workspace

The workspace is the folder the agent will read and write files in.

- Click **📂 Open Folder** in the top-left header to browse for a folder
- Or type in the chat: *"Set my workspace to D:\MyProject"* — the agent will call `set_workspace` automatically

The current path is shown in the top bar of the editor panel.

### 4.3. Sending a Request

Type your request in the chat input at the bottom right and press **Enter** (or **Shift+Enter** for a new line).

**Example requests:**
```
Create a Python venv, install pandas and matplotlib, then write a script
that plots a histogram of random data and saves it as output.png
```

```
Read my requirements.txt and check if all packages can be installed,
then fix any version conflicts you find.
```

```
Set up a React project with TypeScript in the current folder.
```

The agent will think through the task, execute it step by step, and update you in real-time.

---

## 5. The Human-in-the-Loop System

AntiGravity Lean **will not silently modify your files or run dangerous commands**. Before each sensitive action, it pauses and shows you a **Review Card** in the chat panel.

### 5.1. Shell Command Review

When the agent wants to run a terminal command, a card appears showing the exact command:

```
🛠  run_shell_command
cd "D:\MyProject" && pip install numpy scipy
```

Click **✓ Accept** to run it, or **✕ Reject** to skip it and tell the agent it was blocked.

### 5.2. File Write Review

When the agent wants to create or modify a file, it shows a **color-coded diff**:

```diff
- old line (removed)
+ new line (added)
  unchanged line
```

Click **✓ Accept** to save, or **✕ Reject** to discard the change.

### 5.3. Safe Commands (Auto-Approved)

A small set of read-only commands never require your approval:

| Safe Command | What it does |
|---|---|
| `dir` / `ls` | List directory contents |
| `pwd` | Print current path |
| `echo` | Print text |
| `git status` / `git log` | Check git state |
| `python --version` | Check Python |
| `pip --version` | Check pip |

---

## 6. The Live Agent Shell

The **🤖 Agent Shell** terminal tab appears automatically at the bottom of the IDE whenever the agent runs a shell command. It streams the command's output **line by line in real time** — so you can watch `pip install`, build scripts, and test runners as they execute.

- **Green** text = stdout (normal output)
- **Red** text = stderr (warnings/errors)
- **Gray** = exit code (`0` = success, non-zero = error)

You can also type commands directly in your own **🖥 Shell 1** terminal tab — these are separate from what the agent does.

---

## 7. Session History

After every completed conversation, the session is automatically saved to `lean-agent/History/`. Saved sessions store:
- Your workspace path
- The DeepSeek browser URL (so the AI remembers the conversation)

To restore a session:
1. Click **History** in the top of the sidebar
2. Click any saved session card
3. The workspace changes, and the DeepSeek browser navigates back to your previous conversation

To delete a session, hover over it and click the 🗑 trash icon.

---

## 8. Tips & Best Practices

### Be specific about file locations
Instead of: *"Create a script"*  
Say: *"Create a file called `analysis.py` in the current workspace that does X"*

### Let the agent verify
The agent should always run `list_files` after creating things to verify they exist. If it doesn't and seems confused, prompt: *"Verify the files were created."*

### Keep the Edge window visible on first run
For the first task in a session, watch the DeepSeek Edge window briefly. If there's a captcha or login prompt, solve it manually.

### Use Reset sparingly
The **Reset** button (top right) clears the agent's conversation memory. Only use it if the agent is deeply confused or stuck in a loop. The DeepSeek browser conversation is not cleared — only the local history.

### Workspace for complex projects
Set a dedicated workspace per project using **Open Folder**. The agent's file operations (read, write, list) are all relative to this workspace root.

---

## 9. Adding Models

To add Claude or another AI (if you have an account), edit `llm-web-router/config.py`:

```python
MODEL_CONFIG = {
    "deepseek": { ... },   # Already configured
    "claude": {
        "url": "https://claude.ai/new",
        "profile_dir": "profiles/claude",
        "input_selectors": ["div[contenteditable='true']"],
        "send_selectors": ["button[aria-label*='Send']"],
        "response_container": "div.prose",
        "model_name": "claude-3-5-sonnet",
    },
}
```

Then log in using:
```powershell
python manual_login.py claude
```

---

## 10. Stopping the Platform

1. Press **Ctrl+C** in the **lean-agent** terminal (stops the bridge)
2. Press **Ctrl+C** in the **llm-web-router** terminal (closes the browser and stops the router)
3. Close the Dashboard browser tab

> Your sessions are auto-saved, so no work is lost.

---

## 11. Troubleshooting

| Problem | Solution |
|---|---|
| Edge window doesn't open | Make sure Microsoft Edge is installed and `playwright install chromium` was run |
| "No session found" on startup | Run `python manual_login.py deepseek` again |
| Dashboard says `Disconnected` even though the router and bridge are running | Close the dashboard tab, make sure only **one** `python server_bridge.py` instance is running, then reopen `lean-agent/ui/index.html`. The active bridge may be on `8001`, `8010`, or `8011` |
| Bridge starts on `8010` or `8011` instead of `8001` | This is normal when `8001` is already in use. Do **not** keep launching extra bridge processes just to force `8001` |
| Bridge terminal shows `Will watch for changes` or `Started reloader process` | You are not using the correct startup path. Stop that process and start `python server_bridge.py` from `lean-agent` after activating `lean-agent\venv` |
| You pressed `Ctrl+C` because `python server_bridge.py` looked stuck | Wait for the `Uvicorn running on ...` line before assuming startup failed. If necessary, restart from one clean `lean-agent` terminal only |
| Agent says `{"error": ""}` repeatedly | The shell command failed silently. Check the Agent Shell tab for the actual error output |
| File tree not updating | Click the **↺** Refresh button at the top of the Explorer sidebar |
| Agent loops endlessly | Click **Reset** and try a more specific, step-by-step request |
| "Applying changes..." stuck | This was a deadlock bug (fixed). Ensure you are running the latest `server_bridge.py` which uses `asyncio.create_task()` |
| Can't see venv in file explorer | The explorer shows all folders. Click **↺** to refresh after the venv is created |
