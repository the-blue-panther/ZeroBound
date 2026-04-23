import subprocess
import os
import json
import shutil
import httpx
import sqlite3

_file_cache = {}

def _init_db():
    try:
        conn = sqlite3.connect(os.path.join(CURRENT_WORKSPACE, "memories.db"))
        conn.execute('''CREATE TABLE IF NOT EXISTS memories (
                            id INTEGER PRIMARY KEY,
                            key TEXT UNIQUE,
                            value TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )''')
        conn.commit()
        conn.close()
    except:
        pass

try:
    from browser_manager import browser_manager
except ImportError:
    browser_manager = None

# Commands that are always allowed without asking
SAFE_COMMANDS = [
    'dir', 'ls', 'pwd', 'echo', 'git status', 'git log', 
    'python --version', 'pip --version', 'npm --version', 'node --version'
]

# The active workspace (defaults to current project root)
# Fallback to current directory if __file__ is not available
try:
    CURRENT_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
except:
    CURRENT_WORKSPACE = os.getcwd()

IGNORED_TREE_DIRS = {
    '.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'
}

# Keep common dependency/cache folders visible as folders, but don't recursively
# expand them into the initial websocket payload.
TRIMMED_TREE_DIRS = {
    'venv', '.venv', 'node_modules', 'dist', 'build', 'target'
}

MAX_TREE_NODES = 2500

def requires_approval(tool_name, args):
    """Determines if a tool call requires user permission."""
    if tool_name in ["run_command", "run_shell_command", "start_background_command"]:
        cmd = args.get("command", "").lower()
        # If it's not in the safe list, it needs approval
        return not any(cmd.startswith(safe) for safe in SAFE_COMMANDS)
    
    if tool_name in ["write_file", "edit_file", "append_file", "move_file", "copy_file", "delete_file"]:
        # We ALWAYS require approval for writing or deleting files now
        return True
        
    return False

def get_diff(path, new_content):
    """Generates a text-based diff between the existing file and new content."""
    import difflib
    
    if not path or not isinstance(path, str):
        return f"--- (Missing or invalid path for diff: {path})\n+++ new content\n" + (new_content or "")

    full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
    old_content = ""
    if os.path.exists(full_path):
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                old_content = f.read()
        except:
            pass
            
    if new_content is None:
        new_content = ""
    if not isinstance(new_content, str):
        new_content = str(new_content)
        
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile='original',
        tofile='proposed'
    )
    return "".join(diff)

def set_workspace(path: str):
    """Changes the active working directory for the agent."""
    global CURRENT_WORKSPACE
    abs_path = os.path.abspath(path)
    if os.path.exists(abs_path) and os.path.isdir(abs_path):
        CURRENT_WORKSPACE = abs_path
        _init_db()  # Re-init db for new workspace
        return {"status": "success", "workspace": CURRENT_WORKSPACE}
    else:
        return {"error": f"Path '{path}' does not exist or is not a directory."}

def run_command(command: str):
    """Executes a shell command in the CURRENT_WORKSPACE."""
    # Note: We skip the safe check here because requires_approval handles the interlock.
    # If it reached here, it's either safe or approved.
    try:
        # Run in the active workspace
        result = subprocess.run(command, shell=True, capture_output=True, encoding='utf-8', errors='replace', timeout=120, cwd=CURRENT_WORKSPACE)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "code": result.returncode,
            "cwd": CURRENT_WORKSPACE
        }
    except Exception as e:
        return {"error": str(e)}

def write_file(path: str, content: str):
    """Writes content to a file relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str) or not isinstance(content, str):
        return {"error": "write_file requires both 'path' and 'content' to be strings."}
    try:
        # Resolve path
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(full_path)), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        if full_path in _file_cache:
            del _file_cache[full_path]
        return {"status": "success", "path": full_path}
    except Exception as e:
        return {"error": str(e)}

def edit_file(path: str, target: str, replacement: str):
    """Replaces a specific block of text in a file with new text."""
    if not isinstance(path, str) or not isinstance(target, str) or not isinstance(replacement, str):
        return {"error": "path, target, and replacement must be strings."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        if not os.path.exists(full_path):
            return {"error": f"File '{path}' does not exist. Use write_file to create it."}
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        import re
        count = content.count(target)
        matched_text = target

        if count == 0:
            # Fallback to fuzzy match (ignoring leading/trailing whitespace differences)
            target_lines = [line.strip() for line in target.splitlines() if line.strip()]
            if target_lines:
                escaped_lines = [re.escape(line) for line in target_lines]
                pattern = r'^[ \t]*' + r'\s*'.join(escaped_lines) + r'[ \t]*$'
                matches = list(re.finditer(pattern, content, flags=re.MULTILINE))
                if len(matches) == 1:
                    matched_text = matches[0].group(0)
                    count = 1
                elif len(matches) > 1:
                    count = len(matches)

        if count == 0:
            return {"error": "Target content not found in the file. Tried exact and fuzzy matching."}
        elif count > 1:
            return {"error": f"Target content found {count} times. Please provide a larger block of code to uniquely identify the section being replaced."}
            
        new_content = content.replace(matched_text, replacement, 1)
        
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        if full_path in _file_cache:
            del _file_cache[full_path]
            
        return {"status": "success", "path": full_path, "note": "Successfully replaced the specified text block."}
    except Exception as e:
        return {"error": str(e)}

def read_file(path: str, start_line: int = None, end_line: int = None):
    """Reads content from a file relative to CURRENT_WORKSPACE. Supports optional line range."""
    if not isinstance(path, str):
        return {"error": "read_file requires 'path' to be a string."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        if not os.path.exists(full_path):
            return {"error": "File not found"}
        
        IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.svg'}
        ext = os.path.splitext(full_path)[1].lower()
        if ext in IMAGE_EXTS:
            import base64
            with open(full_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode('ascii')
            return {"content": b64, "is_image": True, "ext": ext.lstrip('.')}
        
        content = None
        mtime = os.path.getmtime(full_path)
        
        if full_path in _file_cache and _file_cache[full_path][0] == mtime:
            content = _file_cache[full_path][1]
        else:
            # Try encodings in order
            for enc in ('utf-8-sig', 'utf-8', 'utf-16', 'latin-1'):
                try:
                    with open(full_path, 'r', encoding=enc) as f:
                        content = f.read()
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            
            if content is None:
                # Final fallback: read as binary and decode lossy
                with open(full_path, 'rb') as f:
                    content = f.read().decode('utf-8', errors='replace')
                    
            _file_cache[full_path] = (mtime, content)
        
        lines = content.splitlines()
        total_lines = len(lines)
        
        MAX_READ_LINES = 500
        
        s = (start_line - 1) if start_line else 0
        e = end_line if end_line else total_lines
        
        # Bounds check
        s = max(0, min(s, total_lines))
        e = max(0, min(e, total_lines))
        
        note = ""
        if e - s > MAX_READ_LINES:
            e = s + MAX_READ_LINES
            if e > total_lines:
                e = total_lines
            note = f"⚠️ FILE TOO LARGE. Truncated to {e - s} lines (showing lines {s+1} to {e} out of {total_lines}). Use start_line and end_line arguments to paginate through the file."
        elif start_line is not None or end_line is not None:
             note = f"Showing lines {s+1} to {e} of {total_lines}."
        elif total_lines > 0:
             note = f"Showing full file (1 to {total_lines} of {total_lines} lines)."
            
        content_chunk = "\n".join(lines[s:e])
        
        result = {
            "content": content_chunk,
            "range": [s+1, e],
            "total_lines": total_lines
        }
        if note:
            result["note"] = note
            
        return result
    except Exception as e:
        return {"error": str(e)}

def list_files(path: str = "."):
    """Lists files in a directory relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str):
        return {"error": "list_files requires 'path' to be a string."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        files = os.listdir(full_path)
        return {"files": files, "path": full_path}
    except Exception as e:
        return {"error": str(e)}

def get_file_tree(startpath: str = None):
    """Generates a recursive JSON tree of the workspace."""
    if startpath is None:
        startpath = CURRENT_WORKSPACE

    workspace_root = os.path.abspath(CURRENT_WORKSPACE)
    node_budget = {"remaining": MAX_TREE_NODES}
    
    def build_tree(path):
        if node_budget["remaining"] <= 0:
            return None

        name = os.path.basename(path)
        if not name: # Handle root case
            name = os.path.basename(path.rstrip(os.sep))

        if name in IGNORED_TREE_DIRS:
            return None

        abs_path = os.path.abspath(path)
        rel_path = "." if abs_path == workspace_root else os.path.relpath(abs_path, workspace_root)
        tree = {"name": name, "path": rel_path}
        node_budget["remaining"] -= 1
        
        if os.path.isdir(path):
            tree["type"] = "folder"
            if abs_path != workspace_root and name in TRIMMED_TREE_DIRS:
                tree["children"] = []
                tree["trimmed"] = True
                return tree

            children = []
            try:
                for entry in sorted(os.listdir(path)):
                    if node_budget["remaining"] <= 0:
                        tree["trimmed"] = True
                        break
                    child = build_tree(os.path.join(path, entry))
                    if child:
                        children.append(child)
            except (PermissionError, OSError):
                pass
            tree["children"] = children
        else:
            tree["type"] = "file"
            
        return tree

    return build_tree(startpath)

def grep_search(pattern: str, path: str = ".", case_insensitive: bool = True):
    """Recursively search for text patterns in the workspace."""
    results = []
    root = os.path.abspath(os.path.join(CURRENT_WORKSPACE, path))
    
    # Flags for case sensitivity
    import re
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except Exception as e:
        return {"error": f"Invalid regex pattern: {e}"}

    max_results = 100
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories
        dirnames[:] = [d for d in dirnames if d not in IGNORED_TREE_DIRS and d not in TRIMMED_TREE_DIRS]
        
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, CURRENT_WORKSPACE)
            
            # Skip binary files/known large types
            ext = os.path.splitext(filename)[1].lower()
            if ext in {'.png', '.jpg', '.exe', '.dll', '.pyc', '.o', '.bin', '.pdf'}:
                continue

            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append({
                                "file": rel_path,
                                "line": i,
                                "content": line.strip()
                            })
                            if len(results) >= max_results:
                                return {"results": results, "note": f"Capped at {max_results} results."}
            except:
                continue
                
    return {"results": results}

def find_files(pattern: str):
    """Search for files by name globally in the workspace."""
    import fnmatch
    results = []
    max_results = 200
    
    for dirpath, dirnames, filenames in os.walk(CURRENT_WORKSPACE):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_TREE_DIRS and d not in TRIMMED_TREE_DIRS]
        
        for filename in filenames:
            if fnmatch.fnmatch(filename, pattern) or pattern.lower() in filename.lower():
                rel_path = os.path.relpath(os.path.join(dirpath, filename), CURRENT_WORKSPACE)
                results.append(rel_path)
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
            
    return {"files": results, "note": f"Found {len(results)} matches."}

def reveal_in_os(path: str):
    """Opens the host OS file explorer and selects the file/folder."""
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        full_path = os.path.normpath(full_path)
        if os.path.exists(full_path):
            # Windows specific explorer /select
            subprocess.Popen(f'explorer /select,"{full_path}"')
            return {"status": "success", "path": full_path}
        else:
            return {"error": f"Path '{path}' does not exist."}
    except Exception as e:
        return {"error": str(e)}

def create_folder(path: str):
    """Creates a new directory relative to CURRENT_WORKSPACE."""
    if not isinstance(path, str):
        return {"error": "create_folder requires 'path' to be a string."}
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        os.makedirs(full_path, exist_ok=True)
        return {"status": "success", "path": full_path}
    except Exception as e:
        return {"error": str(e)}

def delete_file(path: str):
    """Deletes a file or folder permanently."""
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        if not os.path.exists(full_path):
            return {"error": f"Path '{path}' not found."}
        
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)
        return {"status": "success", "message": f"Deleted {path}"}
    except Exception as e:
        return {"error": str(e)}

def move_file(source: str, destination: str):
    """Moves or renames a file or folder."""
    try:
        s_path = os.path.isabs(source) and source or os.path.join(CURRENT_WORKSPACE, source)
        d_path = os.path.isabs(destination) and destination or os.path.join(CURRENT_WORKSPACE, destination)
        
        # Ensure destination parent exists
        os.makedirs(os.path.dirname(d_path), exist_ok=True)
        shutil.move(s_path, d_path)
        return {"status": "success", "from": source, "to": destination}
    except Exception as e:
        return {"error": str(e)}

def copy_file(source: str, destination: str):
    """Copies a file or folder."""
    try:
        s_path = os.path.isabs(source) and source or os.path.join(CURRENT_WORKSPACE, source)
        d_path = os.path.isabs(destination) and destination or os.path.join(CURRENT_WORKSPACE, destination)
        
        # Ensure destination parent exists
        os.makedirs(os.path.dirname(d_path), exist_ok=True)
        if os.path.isdir(s_path):
            shutil.copytree(s_path, d_path)
        else:
            shutil.copy2(s_path, d_path)
        return {"status": "success", "from": source, "to": destination}
    except Exception as e:
        return {"error": str(e)}

def get_file_info(path: str):
    """Returns size, type, and modification time of a path."""
    try:
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        if not os.path.exists(full_path):
            return {"error": f"Path '{path}' not found."}
            
        stats = os.stat(full_path)
        import datetime
        return {
            "path": path,
            "type": "folder" if os.path.isdir(full_path) else "file",
            "size_bytes": stats.st_size,
            "modified_at": datetime.datetime.fromtimestamp(stats.st_mtime).isoformat()
        }
    except Exception as e:
        return {"error": str(e)}

async def search_web(query: str):
    """Search DuckDuckGo for information."""
    try:
        # Using DDG Lite for cleaner parsing without JS
        url = f"https://duckduckgo.com/lite/?q={query.replace(' ', '+')}"
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            resp = await client.get(url, headers=headers)
            html = resp.text
            
            # Simple extraction of titles and snippets
            import re
            results = []
            matches = re.findall(r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>.*?<td class="result-snippet">([^<]+)</td>', html, re.DOTALL)
            
            for link, title, snippet in matches[:8]:
                results.append({
                    "title": title.strip(),
                    "link": link.strip() if link.startswith('http') else f"https:{link}",
                    "snippet": snippet.strip()
                })
            
            if not results:
                return {"results": [], "note": "No results found or parsing failed. Try a different query."}
            return {"results": results}
    except Exception as e:
        return {"error": str(e)}

async def read_url(url: str):
    """Fetches content from a URL and returns a text summary."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            resp = await client.get(url, headers=headers)
            html = resp.text
            
            # Very basic HTML to text conversion
            import re
            # Remove scripts and styles
            html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            # Replace common tags with newlines
            text = re.sub(r'<(p|br|div|li|h\d)[^>]*>', '\n', html, flags=re.IGNORECASE)
            # Remove all other tags
            text = re.sub(r'<[^>]+>', '', text)
            # Decode entities
            import html as html_lib
            text = html_lib.unescape(text)
            # Clean up whitespace
            text = re.sub(r'\n\s*\n', '\n\n', text).strip()
            
            # Return first 4000 chars to avoid context overflow
            return {"content": text[:4000] + ("..." if len(text) > 4000 else ""), "url": url}
    except Exception as e:
        return {"error": str(e)}

import asyncio
import subprocess
import threading
import uuid

# Track active background processes
active_processes = {}

async def run_command_async(command: str, callback=None):
    if callback:
        await callback({
            "type": "direct_terminal_result",
            "html": f"<div style='color:#3fb950;margin:10px 0 5px;'>▶ Executing: {command}</div>",
            "agent_controlled": True
        })
        
    loop = asyncio.get_running_loop()
    
    def run_proc():
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=CURRENT_WORKSPACE,
                text=False
            )
            
            temp_id = str(uuid.uuid4())[:8]
            active_processes[temp_id] = {"process": process, "command": command, "buffer": []}
            
            stdout_bytes = []
            stderr_bytes = []
            
            def stream_reader(stream, is_stderr):
                while True:
                    chunk = stream.read(1024)
                    if not chunk:
                        break
                    if is_stderr:
                        stderr_bytes.append(chunk)
                        if callback:
                            asyncio.run_coroutine_threadsafe(
                                callback({"type": "direct_terminal_result", "stderr": chunk.decode('utf-8', 'replace'), "agent_controlled": True}), 
                                loop
                            )
                    else:
                        stdout_bytes.append(chunk)
                        if callback:
                            asyncio.run_coroutine_threadsafe(
                                callback({"type": "direct_terminal_result", "stdout": chunk.decode('utf-8', 'replace'), "agent_controlled": True}), 
                                loop
                            )
            
            t1 = threading.Thread(target=stream_reader, args=(process.stdout, False), daemon=True)
            t2 = threading.Thread(target=stream_reader, args=(process.stderr, True), daemon=True)
            t1.start()
            t2.start()
            
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                t1.join()
                t2.join()
                if temp_id in active_processes:
                    del active_processes[temp_id]
                return {
                    "error": "Command timed out after 15 seconds. You MUST use 'start_background_command' for long-running servers (like npm run dev), otherwise you will freeze the system!",
                    "stdout_so_far": b"".join(stdout_bytes).decode('utf-8', 'replace'),
                    "stderr_so_far": b"".join(stderr_bytes).decode('utf-8', 'replace')
                }
                
            t1.join()
            t2.join()
            
            if temp_id in active_processes:
                del active_processes[temp_id]
            
            return {
                "stdout": b"".join(stdout_bytes).decode('utf-8', 'replace'),
                "stderr": b"".join(stderr_bytes).decode('utf-8', 'replace'),
                "code": process.returncode,
                "cwd": CURRENT_WORKSPACE
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e) or repr(e)}

    result = await loop.run_in_executor(None, run_proc)
    
    if callback:
         await callback({
             "type": "direct_terminal_result",
             "html": f"<div style='color:#8a8a9a;margin-bottom:10px;'>Exit code: {result.get('code', 'N/A')}</div>",
             "agent_controlled": True
         })
         
    return result

async def start_background_command(command: str, callback=None):
    process_id = str(uuid.uuid4())[:8]
    if callback:
        await callback({
            "type": "direct_terminal_result",
            "html": f"<div style='color:#3fb950;margin:10px 0 5px;'>▶ Background [{process_id}]: {command}</div>",
            "agent_controlled": True
        })
        
    loop = asyncio.get_running_loop()
    
    def run_proc():
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=CURRENT_WORKSPACE,
                text=False
            )
            
            output_buffer = []
            active_processes[process_id] = {
                "process": process,
                "command": command,
                "buffer": output_buffer
            }
            
            def stream_reader(stream, is_stderr):
                while True:
                    chunk = stream.read(1024)
                    if not chunk:
                        break
                    decoded = chunk.decode('utf-8', 'replace')
                    output_buffer.append(decoded)
                    
                    if callback:
                        asyncio.run_coroutine_threadsafe(
                            callback({"type": "direct_terminal_result", "stderr" if is_stderr else "stdout": decoded, "agent_controlled": True}), 
                            loop
                        )
                        
            t1 = threading.Thread(target=stream_reader, args=(process.stdout, False), daemon=True)
            t2 = threading.Thread(target=stream_reader, args=(process.stderr, True), daemon=True)
            t1.start()
            t2.start()
            
            return {
                "status": "started",
                "process_id": process_id,
                "command": command,
                "message": f"Process started in background. Use read_process_output to view output. Process ID: {process_id}"
            }
        except Exception as e:
            return {"error": str(e)}

    return await loop.run_in_executor(None, run_proc)

def read_process_output(process_id: str):
    if process_id not in active_processes:
        return {"error": f"Process {process_id} not found."}
    
    proc_info = active_processes[process_id]
    process = proc_info["process"]
    buffer = proc_info["buffer"]
    
    output = "".join(buffer)
    proc_info["buffer"] = [] # clear buffer after reading
    
    status = "running" if process.poll() is None else f"exited with code {process.poll()}"
    
    if status != "running":
        del active_processes[process_id]
        
    return {
        "process_id": process_id,
        "status": status,
        "output": output if output else "(No new output)"
    }

def kill_process(process_id: str):
    if process_id not in active_processes:
        return {"error": f"Process {process_id} not found."}
    
    proc_info = active_processes[process_id]
    process = proc_info["process"]
    
    if process.poll() is None:
        process.kill()
        status = "killed"
    else:
        status = "already exited"
        
    del active_processes[process_id]
    
    return {"status": status, "process_id": process_id}

def list_running_processes():
    running = []
    for pid, info in list(active_processes.items()):
        process = info["process"]
        if process.poll() is None:
            running.append({"process_id": pid, "command": info["command"], "status": "running"})
        else:
            del active_processes[pid]
            
    return {"running_processes": running}

def store_memory(key: str, value: str):
    try:
        conn = sqlite3.connect(os.path.join(CURRENT_WORKSPACE, "memories.db"))
        conn.execute("INSERT OR REPLACE INTO memories (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        return {"error": str(e)}

def recall_memory(query: str):
    try:
        conn = sqlite3.connect(os.path.join(CURRENT_WORKSPACE, "memories.db"))
        cursor = conn.execute("SELECT key, value FROM memories WHERE key LIKE ? OR value LIKE ?", (f"%{query}%", f"%{query}%"))
        results = [{"key": row[0], "value": row[1]} for row in cursor.fetchall()]
        conn.close()
        return {"results": results}
    except Exception as e:
        return {"error": str(e)}

def git_commit(message: str, files: list = None):
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

def git_diff(path: str = None):
    """Get git diff using subprocess — avoids gitpython path resolution quirks."""
    try:
        cmd = ["git", "diff"]
        if path:
            full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
            cmd += ["--", full_path]
        result = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace', cwd=CURRENT_WORKSPACE)
        return {"diff": result.stdout or "(No changes)", "stderr": result.stderr, "code": result.returncode}
    except Exception as e:
        return {"error": str(e)}

def run_tests(path: str = ".", pattern: str = None):
    """Run pytest. `pattern` is a -k substring expression, not a glob."""
    try:
        cmd = ["pytest", path, "-v", "--tb=short"]
        if pattern:
            # -k accepts substring/keyword expressions like 'test_login or test_signup'
            # NOT globs like 'test_*.py'
            cmd.extend(["-k", pattern])
        result = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace', cwd=CURRENT_WORKSPACE)
        return {"passed": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as e:
        return {"error": str(e)}

def get_definition(file: str, line: int, column: int):
    """Semantic Python code navigation using Jedi v0.19+ API."""
    try:
        import jedi
        full_path = os.path.isabs(file) and file or os.path.join(CURRENT_WORKSPACE, file)
        script = jedi.Script(path=full_path)
        # jedi v0.19+: use .goto() for definitions, .infer() for type inference
        try:
            defs = script.goto(line, column)
        except AttributeError:
            # Very old jedi fallback
            defs = script.goto_definitions(line, column)  # type: ignore
        return {
            "definitions": [
                {
                    "name": getattr(d, 'name', None),
                    "module_path": str(getattr(d, 'module_path', None)),
                    "line": getattr(d, 'line', None),
                    "column": getattr(d, 'column', None),
                    "description": getattr(d, 'description', None)
                }
                for d in defs
            ]
        }
    except Exception as e:
        return {"error": str(e)}

async def handle_tool_call(tool_name, args, callback=None):
    """Main dispatcher for tool calls."""
    if tool_name == "set_workspace":
        return set_workspace(args.get("path"))
    elif tool_name == "create_folder":
        return create_folder(args.get("path"))
    elif tool_name in ["run_command", "run_shell_command"]:
        return await run_command_async(args.get("command"), callback)
    elif tool_name == "start_background_command":
        return await start_background_command(args.get("command"), callback)
    elif tool_name == "read_process_output":
        return read_process_output(args.get("process_id"))
    elif tool_name == "kill_process":
        return kill_process(args.get("process_id"))
    elif tool_name == "list_running_processes":
        return list_running_processes()
    elif tool_name == "browser_goto":
        return await browser_manager.goto(args.get("url"))
    elif tool_name == "browser_click":
        return await browser_manager.click(args.get("selector"))
    elif tool_name == "browser_type":
        return await browser_manager.type(args.get("selector"), args.get("text"))
    elif tool_name == "browser_scroll":
        return await browser_manager.scroll(args.get("direction"), args.get("amount", 500))
    elif tool_name == "browser_screenshot":
        return await browser_manager.screenshot()
    elif tool_name == "browser_close":
        return await browser_manager.close()
    elif tool_name == "write_file":
        return write_file(args.get("path"), args.get("content"))
    elif tool_name == "edit_file":
        return edit_file(args.get("path"), args.get("target"), args.get("replacement"))
    elif tool_name == "append_file":
        path = args.get("path")
        content = args.get("content")
        if not path or content is None:
            return {"error": "Missing path or content"}
        
        full_path = os.path.isabs(path) and path or os.path.join(CURRENT_WORKSPACE, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            with open(full_path, "a", encoding="utf-8") as f:
                f.write(content)
            return {"status": "success", "message": f"Appended to {path}"}
        except Exception as e:
            return {"error": str(e)}
    elif tool_name == "read_file":
        return read_file(args.get("path"), args.get("start_line"), args.get("end_line"))
    elif tool_name == "grep_search":
        return grep_search(args.get("pattern"), args.get("path", "."), args.get("case_insensitive", True))
    elif tool_name == "find_files":
        return find_files(args.get("pattern"))
    elif tool_name == "list_files":
        return list_files(args.get("path", "."))
    elif tool_name == "reveal_in_os":
        return reveal_in_os(args.get("path"))
    elif tool_name == "delete_file":
        return delete_file(args.get("path"))
    elif tool_name == "move_file":
        return move_file(args.get("source"), args.get("destination"))
    elif tool_name == "copy_file":
        return copy_file(args.get("source"), args.get("destination"))
    elif tool_name == "get_file_info":
        return get_file_info(args.get("path"))
    elif tool_name == "search_web":
        return await search_web(args.get("query"))
    elif tool_name == "read_url":
        return await read_url(args.get("url"))
    elif tool_name == "store_memory":
        return store_memory(args.get("key"), args.get("value"))
    elif tool_name == "recall_memory":
        return recall_memory(args.get("query"))
    elif tool_name == "git_commit":
        return git_commit(args.get("message"), args.get("files"))
    elif tool_name == "git_diff":
        return git_diff(args.get("path"))
    elif tool_name == "run_tests":
        return run_tests(args.get("path", "."), args.get("pattern", "test_*.py"))
    elif tool_name == "get_definition":
        return get_definition(args.get("file"), args.get("line"), args.get("column"))
    else:
        return {"error": f"Tool {tool_name} not found"}

# Export tool definitions for LLM consumption
TOOLS = [
    {
        "name": "set_workspace",
        "description": "Change the active working directory for all subsequent tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the new workspace folder."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "run_shell_command",
        "description": "Run a shell command on Windows (cmd.exe). Use for installs, builds, git, etc. Do NOT use echo/redirect to write files — use write_file instead. One command per step.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."}
            },
            "required": ["command"]
        }
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file. Parent directories are created automatically — do NOT call create_folder first. When editing an existing file, read_file it first, then write_file with the COMPLETE corrected content. Use \\n for newlines in content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to workspace or absolute)."},
                "content": {"type": "string", "description": "Full text content to write. Use \\n for newlines."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "Surgically edit a specific block of text in an existing file. It replaces EXACTLY the 'target' string with the 'replacement' string. The 'target' string MUST uniquely match an existing block of code, including all whitespaces and indentation. Strongly preferred over write_file for modifying large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit."},
                "target": {"type": "string", "description": "The exact chunk of text to be replaced (include exact indentation)."},
                "replacement": {"type": "string", "description": "The new text chunk to replace the target."}
            },
            "required": ["path", "target", "replacement"]
        }
    },
    {
        "name": "append_file",
        "description": "Append text to the end of an existing file. Use for assembling large files in chunks. Parent directories are created automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "Text to append. Use \\n for newlines."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file's content. Automatically truncates to 500 lines to prevent context limits. For larger files, you MUST use start_line and end_line to paginate.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "start_line": {"type": "integer", "description": "1-indexed start line (optional)."},
                "end_line": {"type": "integer", "description": "1-indexed end line (optional)."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "grep_search",
        "description": "Aggressively search for text patterns across the entire project. Use this for discovery or finding function definitions in large codebases. Respects .git/venv ignores.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The text or regex to search for."},
                "path": {"type": "string", "description": "Subdirectory to search in (defaults to root)."},
                "case_insensitive": {"type": "boolean", "description": "Default true."}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "find_files",
        "description": "Find files by name or glob pattern across the entire project.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The filename pattern to find (e.g. 'auth*.py' or 'utils')."}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "create_folder",
        "description": "Create a new directory. Path is relative to current workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Name or path of the folder to create."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory. Use ONLY for discovery when you don't know what files exist. Do NOT use as a prerequisite before read_file or write_file — those tools handle missing files gracefully.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (defaults to workspace root)."}
            }
        }
    },
    {
        "name": "delete_file",
        "description": "Permanently delete a file or folder. Parent directories are NOT deleted. Requires confirmation via approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file or folder to delete."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "move_file",
        "description": "Move or rename a file or folder. Parent directories of the destination are created automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Current path of the file/folder."},
                "destination": {"type": "string", "description": "New path for the file/folder."}
            },
            "required": ["source", "destination"]
        }
    },
    {
        "name": "copy_file",
        "description": "Copy a file or folder. Destination parent directories are created automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Path of the file/folder to copy."},
                "destination": {"type": "string", "description": "Path where the copy should be created."}
            },
            "required": ["source", "destination"]
        }
    },
    {
        "name": "get_file_info",
        "description": "Get metadata for a file or folder (size, type, modification time).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file or folder."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "search_web",
        "description": "Search the web (DuckDuckGo) for documentation, code examples, or bug fixes. Returns a list of titles and URLs.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search term."}
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_url",
        "description": "Fetch the content of a web page as plain text. Useful for reading documentation or code from a search result link.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to read."}
            },
            "required": ["url"]
        }
    },
    {
        "name": "start_background_command",
        "description": "Start a long-running terminal command in the background (like a dev server or lengthy test script). Does not block. Returns a process_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to start."}
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_process_output",
        "description": "Reads the recent incremental output of a background process. Buffer clears after reading.",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "The process_id returned by start_background_command."}
            },
            "required": ["process_id"]
        }
    },
    {
        "name": "kill_process",
        "description": "Terminates a running background process.",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "The process_id of the background task."}
            },
            "required": ["process_id"]
        }
    },
    {
        "name": "list_running_processes",
        "description": "Lists all currently active background process IDs and their commands.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "browser_goto",
        "description": "Navigate the visible browser to a URL. Automatically captures and returns a screenshot of the loaded page.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to."}
            },
            "required": ["url"]
        }
    },
    {
        "name": "browser_click",
        "description": "Click an element on the current webpage. Automatically captures and returns a screenshot after clicking.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "The CSS selector of the element to click."}
            },
            "required": ["selector"]
        }
    },
    {
        "name": "browser_type",
        "description": "Fill a text field on the current webpage. Automatically captures and returns a screenshot after typing.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "The CSS selector of the text field."},
                "text": {"type": "string", "description": "The text to type into the field."}
            },
            "required": ["selector", "text"]
        }
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the current webpage up or down. Automatically captures and returns a screenshot after scrolling.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "Either 'up' or 'down'."},
                "amount": {"type": "integer", "description": "Amount of pixels to scroll. Default is 500."}
            },
            "required": ["direction"]
        }
    },
    {
        "name": "browser_screenshot",
        "description": "Takes a screenshot of the current visible browser page. This will feed the image directly into your vision engine so you can visually verify UI, layouts, or errors.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "browser_close",
        "description": "Close the visible browser.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "store_memory",
        "description": "Store a key-value pair in persistent SQLite memory to learn patterns across sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The memory key or topic."},
                "value": {"type": "string", "description": "The detailed memory content to store."}
            },
            "required": ["key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": "Search the SQLite memory for previously learned patterns or solutions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term to match against keys or values."}
            },
            "required": ["query"]
        }
    },
    {
        "name": "git_commit",
        "description": "Commit files to the local git repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of files to commit. Leave empty to commit all changes."
                }
            },
            "required": ["message"]
        }
    },
    {
        "name": "git_diff",
        "description": "Get the current git diff of the workspace or a specific file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional file path to diff. If empty, diffs the entire workspace."}
            }
        }
    },
    {
        "name": "run_tests",
        "description": "Run pytest on a given directory or pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to run tests in. Default '.'"},
                "pattern": {"type": "string", "description": "Test file pattern to run. Default 'test_*.py'"}
            }
        }
    },
    {
        "name": "get_definition",
        "description": "Find the semantic definition of a Python symbol (function, class, variable) using Jedi.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to the python file."},
                "line": {"type": "integer", "description": "Line number (1-indexed)."},
                "column": {"type": "integer", "description": "Column number (0-indexed)."}
            },
            "required": ["file", "line", "column"]
        }
    }
]

def get_tools_prompt_description():
    """Returns a string description of all available tools for inclusion in the system prompt."""
    desc = "--- AVAILABLE TOOLS ---\n"
    for tool in TOOLS:
        name = tool["name"]
        params = tool.get("parameters", {}).get("properties", {})
        required = tool.get("parameters", {}).get("required", [])
        
        param_list = []
        for p_name, p_info in params.items():
            req_star = "*" if p_name in required else ""
            p_type = p_info.get("type", "any")
            param_list.append(f"{p_name}{req_star} ({p_type})")
            
        params_str = ", ".join(param_list)
        desc += f"• {name}({params_str}): {tool['description']}\n"
    return desc
