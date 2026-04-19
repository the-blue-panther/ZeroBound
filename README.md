# 🌀 ZeroBound

**ZeroBound** is an ultra-robust, state-driven autonomous coding assistant. It bridges the gap between raw LLM capabilities and reliable engineering workflows by implementing a "Zero-Error" philosophy for agentic tool use.

## 🚀 Key Features

- **🧠 State-Driven Completion**: Unlike traditional agents that rely on flaky "stability timers," ZeroBound monitors the deep internal UI state of web-based LLMs (like DeepSeek) to detect exactly when generation is finished. No more truncated responses.
- **🛠️ Greedy JSON Recovery**: Built-in "auto-repair" logic for truncated tool calls. If a response is cut off due to token limits or network glitches, ZeroBound salvages the partial content and reconstructs the JSON payload.
- **📚 Multi-Turn Assembly**: Natively supports `append_file` operations, allowing the agent to assembly massive files (like distribution guides or entire modules) across multiple conversation turns.
- **🏗️ Structured Thinking**: Enforces a mandatory `<THINK>/<ACTION>/<REPORT>` architecture, ensuring every tool call is backed by explicit reasoning.
- **🖥️ Premium IDE**: A sleek, dark-mode web interface with real-time Markdown rendering, KaTeX math support, and integrated file explorer/terminal.

## 🛠️ Tech Stack

- **Backend**: Python, FastAPI, Uvicorn.
- **Agent Logic**: LiteLLM (for model routing), Custom Parser.
- **Browser Automation**: Playwright (via ZeroBound-Router).
- **Frontend**: HTML5, Vanilla JS, CSS3, Monaco Editor, KaTeX.

## 📦 Project Structure

```text
ZeroBound/
├── lean-agent/          # Core Agent Brain & IDE UI
│   ├── agent_brain.py   # Reasoning loop & history management
│   ├── tool_registry.py # Available agent tools
│   ├── server_bridge.py # WebSocket bridge
│   └── ui/              # Frontend IDE
└── llm-web-router/      # Playwright-based browser driver
    ├── server.py        # Controller for web-based LLMs
    └── config.py        # Model selectors & liveness indicators
```

## 🏁 Getting Started

1. **Clone the Repo**:
   ```bash
   git clone https://github.com/the-blue-panther/ZeroBound.git
   ```
2. **Setup Router**:
   - `pip install -r llm-web-router/requirements.txt`
   - `python llm-web-router/server.py`
3. **Setup Agent**:
   - `pip install -r lean-agent/requirements.txt`
   - `python lean-agent/server_bridge.py`
4. **Launch**:
   Open `lean-agent/ui/index.html` in your browser.

---

*Built with precision for the modern autonomous engineering workflow.*
