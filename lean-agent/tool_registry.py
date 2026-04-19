import subprocess
import os
import json

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
    if tool_name in ["run_command", "run_shell_command"]:
        cmd = args.get("command", "").lower()
        # If it's not in the safe list, it needs approval
        return not any(cmd.startswith(safe) for safe in SAFE_COMMANDS)
    
    if tool_name == "write_file":
        # We ALWAYS require approval for writing files now
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
        return {"status": "success", "workspace": CURRENT_WORKSPACE}
    else:
        return {"error": f"Path '{path}' does not exist or is not a directory."}

def run_command(command: str):
    """Executes a shell command in the CURRENT_WORKSPACE."""
    # Note: We skip the safe check here because requires_approval handles the interlock.
    # If it reached here, it's either safe or approved.
    try:
        # Run in the active workspace
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120, cwd=CURRENT_WORKSPACE)
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
        return {"status": "success", "path": full_path}
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
        
        lines = content.splitlines()
        total_lines = len(lines)
        
        if start_line is not None or end_line is not None:
            # Shift to 1-indexed for the user's mental model if needed, but here we assume LLM sends 1-indexed
            s = (start_line - 1) if start_line else 0
            e = end_line if end_line else total_lines
            # Bounds check
            s = max(0, min(s, total_lines))
            e = max(0, min(e, total_lines))
            
            content = "\n".join(lines[s:e])
            return {
                "content": content,
                "range": [s+1, e],
                "total_lines": total_lines,
                "note": f"Showing lines {s+1} to {e} of {total_lines}."
            }
            
        return {"content": content, "total_lines": total_lines}
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

import asyncio
import subprocess
import threading

# Track the currently running subprocess so it can be killed on stop
current_process = None

async def run_command_async(command: str, callback=None):
    if callback:
        await callback({
            "type": "direct_terminal_result",
            "html": f"<div style='color:#3fb950;margin:10px 0 5px;'>▶ Executing: {command}</div>",
            "agent_controlled": True
        })
        
    loop = asyncio.get_running_loop()
    
    def run_proc():
        global current_process
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=CURRENT_WORKSPACE,
                text=False
            )
            current_process = process
            
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
            t1.join()
            t2.join()
            process.wait()
            
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

async def handle_tool_call(tool_name, args, callback=None):
    """Main dispatcher for tool calls."""
    if tool_name == "set_workspace":
        return set_workspace(args.get("path"))
    elif tool_name == "create_folder":
        return create_folder(args.get("path"))
    elif tool_name in ["run_command", "run_shell_command"]:
        return await run_command_async(args.get("command"), callback)
    elif tool_name == "write_file":
        return write_file(args.get("path"), args.get("content"))
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
        "description": "Read a file's content. Supports mandatory full read or optional line range for large files.",
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
