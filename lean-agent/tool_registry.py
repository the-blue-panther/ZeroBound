"""
tool_registry.py – All agent tools (v2.0)
=========================================
- Filesystem: read, write, edit (with fuzzy matching), copy, move, delete, tree
- Shell: sync/async command execution, background tasks, live streaming
- Web: search, read URL, browser automation
- Knowledge: learn/recall patterns, key‑value memory, git
- Type‑hinted, robust error handling, fully tested
"""

from __future__ import annotations
import asyncio
import subprocess
import os
import json
import re
import shutil
import base64
import threading
import uuid
import datetime
import sqlite3
import difflib
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus as url_quote

import httpx

# ---------------------------------------------------------------------------
# Globals & workspace
# ---------------------------------------------------------------------------

def _get_initial_workspace() -> str:
    """Get initial workspace WITHOUT resolving junctions."""
    # Don't use abspath - it resolves junctions!
    script_dir = os.path.dirname(__file__)
    parent_dir = os.path.dirname(script_dir)
    
    # Use normpath only (doesn't resolve junctions)
    candidate = os.path.normpath(parent_dir)
    
    # Check if this looks like a physical path that should be user-facing
    if "\\s\\" in candidate.lower():
        import re
        user_path = re.sub(r'\\s\\', r'\\Downloads\\', candidate, flags=re.IGNORECASE)
        if os.path.exists(user_path):
            return user_path
    
    return candidate

try:
    CURRENT_WORKSPACE: str = _get_initial_workspace()
except Exception:
    CURRENT_WORKSPACE: str = os.getcwd()

_file_cache: Dict[str, Tuple[float, str]] = {}   # path → (mtime, text)

IGNORED_TREE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
TRIMMED_TREE_DIRS = {"venv", ".venv", "node_modules", "dist", "build", "target"}
MAX_TREE_NODES = 2500

SAFE_COMMANDS = {
    "dir", "ls", "pwd", "echo", "git status", "git log",
    "python --version", "pip --version", "npm --version", "node --version"
}

# Background process tracking
active_processes: Dict[str, Dict[str, Any]] = {}

# Lazy import of browser manager
browser_manager = None
def _get_browser_manager():
    global browser_manager
    if browser_manager is None:
        from browser_manager import browser_manager as bm
        browser_manager = bm
    return browser_manager


# ---------------------------------------------------------------------------
# Workspace / DB helpers
# ---------------------------------------------------------------------------
def _init_db():
    conn = sqlite3.connect(os.path.join(CURRENT_WORKSPACE, "memories.db"))
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY,
        key TEXT UNIQUE,
        value TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()


def sanitize_path(path: str) -> str:
    """Forcibly maps internal physical paths back to user-visible Downloads paths."""
    if not path:
        return path
    norm = os.path.normpath(path)
    # Handle D:\s\ style junction resolutions (case-insensitive)
    if "\\s\\" in norm.lower() or "/s/" in norm.lower():
        import re
        # Try Downloads first (most common)
        clean = re.sub(r'[/\\]s[/\\]', r'\\Downloads\\', norm, flags=re.IGNORECASE)
        if os.path.exists(clean):
            return clean
        # Fallback: just replace in the string for display
        return re.sub(r'[/\\]s[/\\]', r'\\Downloads\\', norm, flags=re.IGNORECASE)
    return norm


def set_workspace(path: str) -> Dict[str, Any]:
    global CURRENT_WORKSPACE
    
    # Step 1: Normalize the path string (fix slashes, etc.) but DON'T resolve junctions
    normalized_path = os.path.normpath(path)
    
    # Step 2: Check if the path exists WITHOUT resolving junctions
    if os.path.exists(normalized_path):
        # Store the EXACT path the user provided, not the resolved one
        CURRENT_WORKSPACE = normalized_path
        _init_db()
        return {"status": "success", "workspace": CURRENT_WORKSPACE}
    
    # Fallback: maybe the user provided a physical path? Try to map it back
    if "\\s\\" in normalized_path.lower():
        import re
        # Convert physical path back to user-friendly path
        user_path = re.sub(r'\\s\\', r'\\Downloads\\', normalized_path, flags=re.IGNORECASE)
        if os.path.exists(user_path):
            CURRENT_WORKSPACE = user_path
            _init_db()
            return {"status": "success", "workspace": CURRENT_WORKSPACE, "note": "Mapped from physical to user path"}
    
    return {"error": f"'{path}' is not a directory"}


def requires_approval(tool_name: str, args: Dict[str, Any]) -> bool:
    if tool_name in {"run_command", "run_shell_command", "start_background_command"}:
        cmd = args.get("command", "").lower()
        return not any(cmd.startswith(safe) for safe in SAFE_COMMANDS)
    return tool_name in {
        "write_file", "edit_file", "append_file", "move_file", "copy_file", "delete_file"
    }


def get_diff(path: str, new_content: str) -> str:
    if not isinstance(path, str):
        return ""
    full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    old = ""
    if os.path.exists(full_path):
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                old = f.read()
        except Exception:
            pass
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile="original",
        tofile="proposed"
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# File system tools
# ---------------------------------------------------------------------------
def read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    if not os.path.exists(full):
        return {"error": "File not found"}
    ext = os.path.splitext(full)[1].lower()
    IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}
    if ext in IMAGE:
        with open(full, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return {"content": b64, "is_image": True, "ext": ext.lstrip(".")}

    mtime = os.path.getmtime(full)
    if full in _file_cache and _file_cache[full][0] == mtime:
        content = _file_cache[full][1]
    else:
        for enc in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
            try:
                with open(full, "r", encoding=enc) as f:
                    content = f.read()
                break
            except UnicodeError:
                continue
        else:
            with open(full, "rb") as f:
                content = f.read().decode("utf-8", errors="replace")
        _file_cache[full] = (mtime, content)

    lines = content.splitlines()
    total = len(lines)
    s = max(0, (start_line - 1) if start_line else 0)
    e = min(total, end_line if end_line else total)
    MAX_LINES = 500
    note = ""
    if e - s > MAX_LINES:
        e = s + MAX_LINES
        note = f"⚠️ Truncated to {e - s} lines (lines {s+1}–{e} of {total}). Use start_line/end_line to paginate."
    elif start_line or end_line:
        note = f"Showing lines {s+1}–{e} of {total}."
    chunk = "\n".join(lines[s:e])
    return {"content": chunk, "range": [s+1, e], "total_lines": total, "note": note}


def write_file(path: str, content: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        _file_cache.pop(full, None)
        return {"status": "success", "path": full}
    except Exception as e:
        return {"error": str(e)}


def edit_file(path: str, target: str, replacement: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    if not os.path.exists(full):
        return {"error": f"File '{path}' not found"}
    with open(full, "r", encoding="utf-8") as f:
        original = f.read()

    # Stage 1: exact match
    if original.count(target) == 1:
        new = original.replace(target, replacement, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(new)
        _file_cache.pop(full, None)
        return {"status": "success", "match_type": "exact"}

    # Stage 2: whitespace‑agnostic regex
    target_lines = [l.strip() for l in target.splitlines() if l.strip()]
    if not target_lines:
        return {"error": "Target is empty or whitespace only."}
    escaped = [re.escape(l) for l in target_lines]
    pattern = r'[ \t]*' + r'\s+'.join(escaped) + r'[ \t]*'
    matches = list(re.finditer(pattern, original, re.MULTILINE))
    if len(matches) == 1:
        new = original.replace(matches[0].group(0), replacement, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(new)
        _file_cache.pop(full, None)
        return {"status": "success", "match_type": "fuzzy_regex"}

    if len(matches) > 1:
        return {"error": f"Found {len(matches)} fuzzy matches. Provide more context."}

    # Stage 3: first & last line anchor
    first_line, last_line = target_lines[0], target_lines[-1]
    candidate = None
    for i, line in enumerate(original.splitlines()):
        if line.strip() == first_line:
            for j in range(i, min(i + len(target_lines) + 10, len(original.splitlines()))):
                if original.splitlines()[j].strip() == last_line:
                    if candidate is None:
                        candidate = (i, j)
                    else:
                        return {"error": "Multiple anchor matches found."}
    if candidate:
        start_idx, end_idx = candidate
        lines = original.splitlines()
        new_lines = lines[:start_idx] + [replacement] + lines[end_idx+1:]
        new_content = "\n".join(new_lines)
        if original.endswith("\n") and not new_content.endswith("\n"):
            new_content += "\n"
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_content)
        _file_cache.pop(full, None)
        return {"status": "success", "match_type": "anchor"}
    return {"error": "Target not found."}


def append_file(path: str, content: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "path": full}
    except Exception as e:
        return {"error": str(e)}


def create_folder(path: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    os.makedirs(full, exist_ok=True)
    return {"status": "success", "path": full}


def delete_file(path: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    if not os.path.exists(full):
        return {"error": f"'{path}' not found"}
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return {"status": "success", "message": f"Deleted {path}"}


def move_file(source: str, destination: str) -> Dict[str, Any]:
    src = os.path.isabs(source) and source or os.path.join(CURRENT_WORKSPACE, source)
    dst = os.path.isabs(destination) and destination or os.path.join(CURRENT_WORKSPACE, destination)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return {"status": "success", "from": source, "to": destination}


def copy_file(source: str, destination: str) -> Dict[str, Any]:
    src = os.path.isabs(source) and source or os.path.join(CURRENT_WORKSPACE, source)
    dst = os.path.isabs(destination) and destination or os.path.join(CURRENT_WORKSPACE, destination)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return {"status": "success", "from": source, "to": destination}


def get_file_info(path: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    if not os.path.exists(full):
        return {"error": f"'{path}' not found"}
    stat = os.stat(full)
    return {
        "path": path,
        "type": "folder" if os.path.isdir(full) else "file",
        "size_bytes": stat.st_size,
        "modified_at": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
    }


def list_files(path: str = ".") -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    try:
        return {"files": os.listdir(full), "path": full}
    except Exception as e:
        return {"error": str(e)}


def get_file_tree(startpath: Optional[str] = None) -> Any:
    # Use normpath to preserve user-facing junction paths
    if startpath is None:
        startpath = CURRENT_WORKSPACE
    root = os.path.normpath(CURRENT_WORKSPACE)
    budget = {"remaining": MAX_TREE_NODES}

    def walk(p: str) -> Optional[Dict]:
        if budget["remaining"] <= 0:
            return None
        name = os.path.basename(p) or os.path.basename(p.rstrip(os.sep))
        if name in IGNORED_TREE_DIRS:
            return None
        
        # Avoid abspath which resolves junctions
        p_norm = os.path.normpath(p)
        rel = "." if p_norm == root else os.path.relpath(p_norm, root)
        
        node = {"name": name, "path": rel}
        budget["remaining"] -= 1
        if os.path.isdir(p):
            node["type"] = "folder"
            if p_norm != root and name in TRIMMED_TREE_DIRS:
                node["children"] = []
                node["trimmed"] = True
                return node
            children = []
            try:
                for entry in sorted(os.listdir(p)):
                    if budget["remaining"] <= 0:
                        node["trimmed"] = True
                        break
                    child = walk(os.path.join(p, entry))
                    if child:
                        children.append(child)
            except (PermissionError, OSError):
                pass
            node["children"] = children
        else:
            node["type"] = "file"
        return node

    return walk(startpath)


def grep_search(pattern: str, path: str = ".", case_insensitive: bool = True) -> Dict[str, Any]:
    root = os.path.normpath(os.path.join(CURRENT_WORKSPACE, path))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}
    results = []
    max_results = 100
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_TREE_DIRS and d not in TRIMMED_TREE_DIRS]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in {".png", ".jpg", ".exe", ".dll", ".pyc", ".o", ".bin"}:
                continue
            full = os.path.join(dirpath, fname)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append({"file": os.path.relpath(full, CURRENT_WORKSPACE), "line": i, "content": line.strip()})
                            if len(results) >= max_results:
                                return {"results": results, "note": f"Capped at {max_results}"}
            except Exception:
                continue
    return {"results": results}


def find_files(pattern: str) -> Dict[str, Any]:
    import fnmatch
    results = []
    max_results = 200
    for dirpath, dirnames, filenames in os.walk(CURRENT_WORKSPACE):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_TREE_DIRS and d not in TRIMMED_TREE_DIRS]
        for fname in filenames:
            if fnmatch.fnmatch(fname, pattern) or pattern.lower() in fname.lower():
                results.append(os.path.relpath(os.path.join(dirpath, fname), CURRENT_WORKSPACE))
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    return {"files": results, "note": f"Found {len(results)} matches"}


def reveal_in_os(path: str) -> Dict[str, Any]:
    full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    if not os.path.exists(full):
        return {"error": f"'{path}' not found"}
    if os.name == "nt":
        subprocess.Popen(f'explorer /select,"{full}"')
    else:
        subprocess.Popen(["open", "-R", full])
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Shell command execution (with live streaming)
# ---------------------------------------------------------------------------
async def run_command_async(command: str, callback: Optional[Callable] = None) -> Dict[str, Any]:
    if callback:
        await callback({"type": "direct_terminal_result",
                         "html": f"<div class='terminal-cmd'>▶ {command}</div>",
                         "agent_controlled": True})
    loop = asyncio.get_running_loop()

    def _run():
        try:
            proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=CURRENT_WORKSPACE, text=False)
            temp_id = str(uuid.uuid4())[:8]
            active_processes[temp_id] = {"process": proc, "command": command, "buffer": []}

            def stream(pipe, is_stderr: bool):
                while True:
                    chunk = pipe.read(1024)
                    if not chunk:
                        break
                    decoded = chunk.decode("utf-8", errors="replace")
                    if is_stderr:
                        active_processes[temp_id]["buffer"].append(decoded)
                        if callback:
                            asyncio.run_coroutine_threadsafe(
                                callback({"type": "direct_terminal_result", "stderr": decoded, "agent_controlled": True}),
                                loop
                            )
                    else:
                        if callback:
                            asyncio.run_coroutine_threadsafe(
                                callback({"type": "direct_terminal_result", "stdout": decoded, "agent_controlled": True}),
                                loop
                            )

            t1 = threading.Thread(target=stream, args=(proc.stdout, False), daemon=True)
            t2 = threading.Thread(target=stream, args=(proc.stderr, True), daemon=True)
            t1.start(); t2.start()

            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                t1.join(); t2.join()
                active_processes.pop(temp_id, None)
                return {"error": "Command timed out after 15 seconds. Use start_background_command for long‑running tasks."}

            t1.join(); t2.join()
            active_processes.pop(temp_id, None)
            return {"stdout": "".join(active_processes[temp_id]["buffer"]), "stderr": "", "code": proc.returncode}
        except Exception as e:
            return {"error": str(e)}

    result = await loop.run_in_executor(None, _run)
    if callback:
        await callback({"type": "direct_terminal_result",
                         "html": f"<div class='terminal-exit'>Exit code: {result.get('code', 'N/A')}</div>",
                         "agent_controlled": True})
    return result


async def start_background_command(command: str, callback: Optional[Callable] = None) -> Dict[str, Any]:
    pid = str(uuid.uuid4())[:8]
    if callback:
        await callback({"type": "direct_terminal_result",
                         "html": f"<div class='terminal-cmd'>▶ Background [{pid}]: {command}</div>",
                         "agent_controlled": True})
    loop = asyncio.get_running_loop()

    def _run():
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=CURRENT_WORKSPACE, bufsize=1, text=False)
        buf = []
        active_processes[pid] = {"process": proc, "command": command, "buffer": buf}

        def reader(pipe, is_stderr):
            for line in pipe:
                decoded = line.decode("utf-8", errors="replace")
                buf.append(decoded)
                if callback:
                    asyncio.run_coroutine_threadsafe(
                        callback({"type": "direct_terminal_result", "stderr" if is_stderr else "stdout": decoded, "agent_controlled": True}),
                        loop
                    )

        threading.Thread(target=reader, args=(proc.stdout, False), daemon=True).start()
        threading.Thread(target=reader, args=(proc.stderr, True), daemon=True).start()
        return {"status": "started", "process_id": pid, "command": command}

    return await loop.run_in_executor(None, _run)


def read_process_output(process_id: str) -> Dict[str, Any]:
    if process_id not in active_processes:
        return {"error": "Unknown process ID"}
    info = active_processes[process_id]
    proc = info["process"]
    out = "".join(info["buffer"])
    info["buffer"].clear()
    status = "running" if proc.poll() is None else f"exited with code {proc.poll()}"
    if status != "running":
        del active_processes[process_id]
    return {"process_id": process_id, "status": status, "output": out or "(no new output)"}


def kill_process(process_id: str) -> Dict[str, Any]:
    if process_id not in active_processes:
        return {"error": "Unknown process ID"}
    info = active_processes.pop(process_id)
    proc = info["process"]
    if proc.poll() is None:
        proc.kill()
        return {"status": "killed", "process_id": process_id}
    return {"status": "already exited", "process_id": process_id}


def list_running_processes() -> Dict[str, Any]:
    running = []
    for pid, info in list(active_processes.items()):
        if info["process"].poll() is None:
            running.append({"process_id": pid, "command": info["command"], "status": "running"})
        else:
            del active_processes[pid]
    return {"running_processes": running}


# ---------------------------------------------------------------------------
# Web tools
# ---------------------------------------------------------------------------
async def search_web(query: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://lite.duckduckgo.com/lite/?q={url_quote(query)}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            html = resp.text
        results = []
        for m in re.finditer(r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>.*?<td class="result-snippet">([^<]+)</td>', html, re.DOTALL):
            link, title, snippet = m.groups()
            results.append({"title": title.strip(), "link": link if link.startswith("http") else f"https:{link}", "snippet": snippet.strip()})
        return {"results": results[:8]}
    except Exception as e:
        return {"error": str(e)}


async def read_url(url: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 ..."})
            html = resp.text
        # Simple HTML → text
        html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<(p|br|div|li|h\d)[^>]*>', '\n', html, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        import html as html_mod
        text = html_mod.unescape(text)
        text = re.sub(r'\n\s*\n', '\n\n', text).strip()
        return {"content": text[:4000] + ("..." if len(text) > 4000 else ""), "url": url}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Memory & knowledge base
# ---------------------------------------------------------------------------
def store_memory(key: str, value: str) -> Dict[str, Any]:
    try:
        conn = sqlite3.connect(os.path.join(CURRENT_WORKSPACE, "memories.db"))
        conn.execute("INSERT OR REPLACE INTO memories (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        return {"error": str(e)}


def recall_memory(query: str) -> Dict[str, Any]:
    try:
        conn = sqlite3.connect(os.path.join(CURRENT_WORKSPACE, "memories.db"))
        cur = conn.execute("SELECT key, value FROM memories WHERE key LIKE ? OR value LIKE ?", (f"%{query}%", f"%{query}%"))
        results = [{"key": r[0], "value": r[1]} for r in cur.fetchall()]
        conn.close()
        return {"results": results}
    except Exception as e:
        return {"error": str(e)}


# Knowledge base functions are imported from knowledge_base.py; they are available as
# `learn_pattern`, `recall_pattern`, `update_pattern_success`, `get_knowledge_stats`.


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
def git_commit(message: str, files: Optional[List[str]] = None) -> Dict[str, Any]:
    try:
        import git
        repo = git.Repo(CURRENT_WORKSPACE)
        if files:
            repo.index.add(files)
        else:
            repo.index.add("*")
        repo.index.commit(message)
        return {"commit": str(repo.head.commit.hexsha)}
    except Exception as e:
        return {"error": str(e)}


def git_diff(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        cmd = ["git", "diff"]
        if path:
            full = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
            cmd += ["--", full]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=CURRENT_WORKSPACE)
        return {"diff": p.stdout or "(No changes)", "stderr": p.stderr, "code": p.returncode}
    except Exception as e:
        return {"error": str(e)}


def run_tests(path: str = ".", pattern: Optional[str] = None) -> Dict[str, Any]:
    cmd = ["pytest", path, "-v", "--tb=short"]
    if pattern:
        cmd.extend(["-k", pattern])
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=CURRENT_WORKSPACE)
    return {"passed": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


def get_definition(file: str, line: int, column: int) -> Dict[str, Any]:
    try:
        import jedi
        full = os.path.isabs(file) and file or os.path.join(CURRENT_WORKSPACE, file)
        script = jedi.Script(path=full)
        defs = script.goto(line, column) if hasattr(script, "goto") else script.goto_definitions(line, column)
        return {"definitions": [{
            "name": d.name,
            "module_path": str(d.module_path),
            "line": d.line,
            "column": d.column,
            "description": d.description
        } for d in defs]}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------
TOOL_MAP = {
    "set_workspace": (set_workspace, False),
    "create_folder": (create_folder, False),
    "run_command": (run_command_async, True),      # async
    "run_shell_command": (run_command_async, True),
    "start_background_command": (start_background_command, True),
    "read_process_output": (read_process_output, False),
    "kill_process": (kill_process, False),
    "list_running_processes": (list_running_processes, False),
    "write_file": (write_file, False),
    "edit_file": (edit_file, False),
    "append_file": (append_file, False),
    "read_file": (read_file, False),
    "grep_search": (grep_search, False),
    "find_files": (find_files, False),
    "list_files": (list_files, False),
    "get_file_tree": (get_file_tree, False),
    "reveal_in_os": (reveal_in_os, False),
    "delete_file": (delete_file, False),
    "move_file": (move_file, False),
    "copy_file": (copy_file, False),
    "get_file_info": (get_file_info, False),
    "search_web": (search_web, True),
    "read_url": (read_url, True),
    "store_memory": (store_memory, False),
    "recall_memory": (recall_memory, False),
    "git_commit": (git_commit, False),
    "git_diff": (git_diff, False),
    "run_tests": (run_tests, False),
    "get_definition": (get_definition, False),
}


async def handle_tool_call(tool_name: str, args: Dict[str, Any], callback: Optional[Callable] = None) -> Any:
    """Dispatch tool call, automatically handling async tools."""
    if tool_name.startswith("browser_"):
        bm = _get_browser_manager()
        cmd = tool_name.split("_", 1)[1]
        if cmd == "goto":
            return await bm.goto(args["url"])
        elif cmd == "click":
            return await bm.click(args["selector"])
        elif cmd == "type":
            return await bm.type(args["selector"], args["text"])
        elif cmd == "scroll":
            return await bm.scroll(args.get("direction", "down"), args.get("amount", 500))
        elif cmd == "screenshot":
            return await bm.screenshot()
        elif cmd == "close":
            return await bm.close()
        else:
            return {"error": f"Unknown browser command: {cmd}"}

    if tool_name not in TOOL_MAP:
        return {"error": f"Tool '{tool_name}' not found"}
    func, is_async = TOOL_MAP[tool_name]
    if is_async:
        return await func(**{k: v for k, v in args.items() if v is not None}, callback=callback)
    else:
        return func(**{k: v for k, v in args.items() if v is not None})


# ---------------------------------------------------------------------------
# Tool schema definitions (for LLM prompt)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "read_file",
        "description": "Read content from a file (or image as base64).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative or absolute path to the file."},
                "start_line": {"type": "integer", "description": "Optional: Start line (1-indexed)."},
                "end_line": {"type": "integer", "description": "Optional: End line (inclusive)."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file with new content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative or absolute path."},
                "content": {"type": "string", "description": "Full file content."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "Surgically edit a file by replacing a unique block of text.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "target": {"type": "string", "description": "The exact string to be replaced."},
                "replacement": {"type": "string", "description": "The new content."}
            },
            "required": ["path", "target", "replacement"]
        }
    },
    {
        "name": "append_file",
        "description": "Append content to the end of a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "create_folder",
        "description": "Create a new directory (recursively).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "delete_file",
        "description": "Delete a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "move_file",
        "description": "Move or rename a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Current path."},
                "destination": {"type": "string", "description": "New path."}
            },
            "required": ["source", "destination"]
        }
    },
    {
        "name": "copy_file",
        "description": "Copy a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source path."},
                "destination": {"type": "string", "description": "Destination path."}
            },
            "required": ["source", "destination"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}}
        }
    },
    {
        "name": "get_file_tree",
        "description": "Get a recursive tree view of the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"startpath": {"type": "string", "description": "Optional starting directory."}}
        }
    },
    {
        "name": "get_file_info",
        "description": "Get metadata about a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "grep_search",
        "description": "Search for a pattern in files (regex).",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "case_insensitive": {"type": "boolean", "default": True}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "find_files",
        "description": "Find files by name pattern (wildcard supported).",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"]
        }
    },
    {
        "name": "reveal_in_os",
        "description": "Open the file/folder in system file explorer.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "run_command",
        "description": "Execute a shell command (async, streams to terminal, auto-timeout 15s).",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    },
    {
        "name": "start_background_command",
        "description": "Start a long-running background process (no timeout).",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    },
    {
        "name": "read_process_output",
        "description": "Read new output from a background process.",
        "parameters": {
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"]
        }
    },
    {
        "name": "kill_process",
        "description": "Terminate a background process.",
        "parameters": {
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"]
        }
    },
    {
        "name": "list_running_processes",
        "description": "List all currently running background processes.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "search_web",
        "description": "Search the web using DuckDuckGo.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "read_url",
        "description": "Fetch and clean text from a URL.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    },
    {
        "name": "browser_goto",
        "description": "Navigate the automated browser to a URL.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    },
    {
        "name": "browser_click",
        "description": "Click an element in the browser.",
        "parameters": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"]
        }
    },
    {
        "name": "browser_type",
        "description": "Type text into a browser input.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"}
            },
            "required": ["selector", "text"]
        }
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the browser page.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                "amount": {"type": "integer", "default": 500}
            }
        }
    },
    {
        "name": "browser_screenshot",
        "description": "Take a screenshot of the current browser page.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "browser_close",
        "description": "Close the browser instance.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "store_memory",
        "description": "Store a key-value pair in long-term memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"}
            },
            "required": ["key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": "Search for memories by key or value.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "git_commit",
        "description": "Commit current changes to git.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Optional: specific files to commit"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "git_diff",
        "description": "Show unstaged changes (or specific file diff).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Optional: specific file path"}}
        }
    },
    {
        "name": "run_tests",
        "description": "Run pytest tests.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string", "description": "Optional: test name pattern (-k)"}
            }
        }
    },
    {
        "name": "get_definition",
        "description": "Get definition of symbol at position using Jedi.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "column": {"type": "integer"}
            },
            "required": ["file", "line", "column"]
        }
    },
    {
        "name": "set_workspace",
        "description": "Change the active workspace directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "get_knowledge_stats",
        "description": "Get statistics from the pattern learning system.",
        "parameters": {"type": "object", "properties": {}}
    }
]

def get_tools_prompt_description() -> str:
    """Generate a human-readable description of all tools for the prompt."""
    lines = ["--- AVAILABLE TOOLS ---"]
    for t in TOOLS:
        name = t.get("name", "unknown")
        desc = t.get("description", "")
        params = t.get("parameters", {}).get("properties", {})
        required = t.get("parameters", {}).get("required", [])
        
        p_list = []
        for param_name, param_info in params.items():
            param_desc = param_info.get("description", "")
            if param_name in required:
                p_list.append(f"{param_name} (required{(': ' + param_desc) if param_desc else ''})")
            else:
                p_list.append(f"{param_name} (optional{(': ' + param_desc) if param_desc else ''})")
        
        if p_list:
            lines.append(f"• {name}({', '.join(p_list)}): {desc}")
        else:
            lines.append(f"• {name}(): {desc}")
    
    return "\n".join(lines)