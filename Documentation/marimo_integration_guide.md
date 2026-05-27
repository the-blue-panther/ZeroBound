# ZeroBound API & Marimo Integration Guide

This guide describes how to run and manage the ZeroBound LLM Router as an isolated, OpenAI-compatible API provider to power your **Marimo** interactive notebooks. 

---

## 📋 Table of Contents
1. [Prerequisites](#-prerequisites)
2. [Phase 1: Before Using Marimo (One-Time Setup & Activation)](#-phase-1-before-using-marimo-one-time-setup--activation)
3. [Phase 2: While Using Marimo (Running the API Router)](#-phase-2-while-using-marimo-running-the-api-router)
4. [Phase 3: Configuring the Provider in Marimo](#-phase-3-configuring-the-provider-in-marimo)
5. [Phase 4: After Using Marimo (Stopping the Router)](#-phase-4-after-using-marimo-stopping-the-router)

---

## 🛠 Prerequisites

Make sure you have:
* Python 3.11+ configured in your virtual environment.
* The Playwright browser installed for Edge automation:
  ```powershell
  playwright install msedge
  ```

---

## 🔓 Phase 1: Before Using Marimo (One-Time Setup & Activation)

Before starting the router, you must ensure that your browser sessions are authenticated so that the API can reuse your cookies. 

### 1. Run Manual Authentication
Open a terminal and authenticate with your preferred models (DeepSeek or Claude):

```powershell
# Navigate to the router directory
cd llm-web-router

# Authenticate with DeepSeek (opens browser for manual login)
python manual_login.py deepseek
```

> [!NOTE]
> A browser window will open automatically. Log in to your account. Once you see the chat dashboard and the page is loaded, return to your terminal and press **ENTER** to save your session cookies.
> 
> *Repeat the step for `claude` if you intend to use Claude as well (`python manual_login.py claude`).*

---

## 🚀 Phase 2: While Using Marimo (Running the API Router)

### 1. Launch the LLM Router
To use Marimo, you only need to run the router backend server (the ZeroBound agent UI does not need to be active unless you want to code at the same time).

In your terminal:
```powershell
# Navigate to llm-web-router directory
cd llm-web-router

# Start the FastAPI server
python server.py
```

Upon launching, the router will:
1. Open a browser window with your authenticated sessions (loading one tab/window for standard mode and one separate tab/window for your API mode).
2. Start the API endpoint at `http://localhost:8000`.

### 2. Verify Server Status
You can check if your router is running and healthy by navigating to `http://localhost:8000/health` in any web browser. It should return a JSON state like:

```json
{
  "status": "running",
  "models": ["deepseek", "claude", "deepseek-api", "claude-api"],
  "browser": true
}
```

---

## ⚙ Phase 3: Configuring the Provider in Marimo

Now that the router is running on port 8000, you can configure it as a Custom AI Provider in Marimo.

### 1. Add Custom Provider in Marimo UI
Navigate to the AI settings menu in your Marimo notebook and set up a **Custom Provider**:

* **Provider Name:** `ZeroBound`
* **Base URL:** `http://localhost:8000/v1`
* **API Key (optional):** `sk-zerobound` (or leave blank)
* Click **Add Provider**.

### 2. Configure Models
Under your custom provider settings in Marimo, specify the models you wish to use:
* `deepseek-api` (or `deepseek-marimo`)
* `claude-api` (or `claude-marimo`)

> [!IMPORTANT]
> **Why use the `-api` variants?**
> The `deepseek-api` and `claude-api` models open separate tabs and use separate locks. They also automatically reset back to the homepage (`https://chat.deepseek.com/` or `https://claude.ai/`) whenever they receive a brand new completion request (where `turns == 0`). This isolates your notebook runs from other chats and prevents history accumulation slowdowns.

---

## 🛑 Phase 4: After Using Marimo (Stopping the Router)

When you are done using Marimo and want to close the automated browser windows:

### 1. Terminate the Router Process
Go back to the terminal where you ran `python server.py` and press:
```keyboard
Ctrl + C
```

### 2. Cleanup
The browser instances and all Playwright processes spawned by the FastAPI lifespan manager will shut down and close automatically.

---

## 🤖 Phase 5: Architecting AI Agents for Marimo (Advanced)

Giving an AI full context and execution authority over a Marimo notebook turns it from a generic code generator into a highly specialized data-science agent. Because Marimo is fundamentally reactive and stored as a pure Python script, it is mathematically predictable and significantly easier for an AI to manage without breaking state than standard Jupyter notebooks.

### 1. Architecting the Environment Connectors

To give an AI full control, it must be able to read runtime state and manipulate files. You have two main pathways to establish this handshake:

#### Strategy A: The Native Marimo MCP Server (Recommended)
Marimo includes native AI Tools and MCP Server Endpoints. If your custom AI API supports the Model Context Protocol (MCP), point your agent framework directly to Marimo’s running instance. This exposes programmatic schema hooks directly to your custom LLM.

*   **Ask Mode Tools (Read-Only):** Lets the AI read cells, view dependency graphs, and fetch active variables.
*   **Agent Mode Tools (Read/Write):** Lets the AI dynamically add, delete, or update cell structures and trigger executions.

#### Strategy B: The External CLI Workflow (Filesystem Agent)
If you are interacting with your custom API wrapper using external tools (like an autonomous agent system or custom IDE extensions), utilize Marimo’s terminal tooling:

1.  **The Watch Flag:** Launch the server using `marimo edit notebook.py --watch`.
2.  **The Feedback Loop:** Any edits your custom agent writes directly to `notebook.py` instantly update and execute inside the live browser window.
3.  **Validation Channel:** Have your agent run `marimo check notebook.py` via shell. This outputs syntactic or logic errors (like circular dependencies) directly back into your custom API prompt loop for self-correction.

### 2. Standard Tools to Expose to Your Custom API

When constructing your tool-calling JSON schema or function array for your custom API call, map out these core capabilities:

| Tool Name | Scope / Action | Payload / Target |
|---|---|---|
| `get_lightweight_cell_map` | Context | Returns cell IDs, preview strings, and runtime positions. |
| `get_cell_runtime_data` | Context | Inspects in-memory variable names, cell statuses (idle, running), and standard output/errors. |
| `update_cell_code` | Control | Replaces the Python string inside an explicit cell block. |
| `add_new_cell` | Control | Injects an empty code or markdown cell at an indexed location. |
| `execute_stale_cells` | Control | Forces Marimo to re-compute cells that require evaluation updates. |
| `read_local_file` | Filesystem | Reads text/CSV structures to understand structural schemas. |

### 3. The System Prompt (What to Tell the Agent)

Because Marimo uses functional reactivity (where changing a variable in one cell automatically pushes changes down a graph to children cells), standard Jupyter prompting patterns will break it. Provide your custom API with this structured system directive:

> You are an expert Data Science and Python Coding Agent executing inside a live Marimo notebook environment. 
> 
> **CRITICAL ENVIRONMENT RULES:**
> 1. **NO HIDDEN STATE:** Unlike Jupyter, Marimo enforces a strict DAG (Directed Acyclic Graph). Variables are shared globally between cells based on definition and reference.
> 2. **NO REPEATED VARIABLE DEFINITIONS:** You must NEVER define the same global variable name in two different cells. Doing so causes a duplicate definition error and halts notebook execution.
> 3. **IMMUTABILITY & REACTIVITY:** If Cell B depends on variable 'x' from Cell A, updating 'x' in Cell A automatically re-runs Cell B. Design your cell blocks modularly around data streams.
> 4. **UI & INTERACTIVITY:** Lean into Marimo's interactive capabilities. When generating user inputs, use `marimo.ui.slider()`, `marimo.ui.dropdown()`, or `marimo.ui.table()`, and bind them to global variables so the UI remains reactive.
> 
> **YOUR OPERATIONAL LOOP:**
> 1. Before writing code, use the `get_lightweight_cell_map` tool to discover existing definitions and dependencies.
> 2. Formulate your update or extension strategy. If you need to create a variable, check if that exact name is already allocated globally.
> 3. Apply code mutations using cell modification tools.
> 4. Check for state errors or runtime execution flags. If an execution yields a traceback, trace the dependency graph backward to fix the root variable.

### 4. Advanced Execution Strategy (How to Maximize Utility)

*   **Enforce Component Isolation:** Train the agent to wrap logical blocks (like loading data or training models) inside dedicated functions within a single cell, or keep them sequentially separated.
*   **Encourage Anywidget Customization:** Marimo has native support for micro-frontends and custom UI components via `anywidget`. Teach your agent to write layout modules using Marimo's declarative presentation layer (`mo.vstack`, `mo.hstack`, or `mo.accordion`). This turns your agent's notebook space into a functional internal tool or dashboard rather than raw unformatted terminal output.
*   **Inject Custom Directives to your Config:** You can explicitly append targeted global coding profiles by editing your local `marimo.toml` configuration under custom rules. For example, pre-prompting the model via configuration variables ensures it defaults to `polars` instead of `pandas`, or `altair` instead of `matplotlib` natively across all generation pipelines.
