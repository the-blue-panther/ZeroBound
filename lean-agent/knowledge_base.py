"""
Knowledge Base Module for ZeroBound.
Provides persistent, self-improving memory across sessions.
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

# Current workspace (set by tool_registry)
CURRENT_WORKSPACE = None

def _get_db_path():
    """Return the path to the knowledge base SQLite DB."""
    if CURRENT_WORKSPACE:
        return os.path.join(CURRENT_WORKSPACE, "knowledge.db")
    return "knowledge.db"

def _init_db():
    """Initialize the knowledge base schema if it doesn't exist."""
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT,
            query TEXT,
            solution TEXT,
            tool_sequence TEXT,
            preconditions TEXT,
            tags TEXT,
            success_count INTEGER DEFAULT 1,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            version INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def _extract_keywords(text: str) -> List[str]:
    """Simple keyword extraction (split by spaces, punctuation)."""
    import re
    words = re.findall(r'\b\w{3,}\b', text.lower())
    return list(set(words))[:10]  # Return up to 10 unique keywords

def learn_pattern(
    task_type: str,
    query: str,
    solution: str,
    tool_sequence: List[Dict],
    tags: Optional[List[str]] = None,
    preconditions: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    Store a successful solution pattern.
    
    Args:
        task_type: Category of task (e.g., 'fix_import', 'setup_react')
        query: The original user request
        solution: Description of what solved it
        tool_sequence: List of tool calls that were executed
        tags: Optional list of keywords for better recall
        preconditions: Optional dict describing required state before applying
    
    Returns:
        dict with status and pattern ID
    """
    _init_db()
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Check for similar existing pattern
    keywords = _extract_keywords(query)
    tag_str = ','.join(tags or keywords)
    
    # Try to find existing pattern with same task_type and similar tags
    cursor.execute("""
        SELECT id, solution, tool_sequence, success_count
        FROM knowledge_patterns
        WHERE task_type = ?
        ORDER BY success_count DESC, last_used DESC
        LIMIT 1
    """, (task_type,))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing pattern (increment success count)
        pattern_id = existing[0]
        new_success = existing[3] + 1
        cursor.execute("""
            UPDATE knowledge_patterns
            SET success_count = ?, last_used = ?, updated_at = ?
            WHERE id = ?
        """, (new_success, datetime.now().isoformat(), datetime.now().isoformat(), pattern_id))
        conn.commit()
        conn.close()
        return {"status": "updated", "id": pattern_id, "increment": True}
    else:
        # Insert new pattern
        cursor.execute("""
            INSERT INTO knowledge_patterns
            (task_type, query, solution, tool_sequence, tags, preconditions, success_count, last_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_type, query, solution, json.dumps(tool_sequence),
            tag_str, json.dumps(preconditions or {}), 1, datetime.now().isoformat()
        ))
        pattern_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {"status": "learned", "id": pattern_id}

def recall_pattern(query: str, limit: int = 3) -> Dict[str, Any]:
    """
    Find similar solution patterns based on keyword matching.
    
    Args:
        query: The user's current request
        limit: Maximum number of patterns to return
    
    Returns:
        dict with list of matching patterns
    """
    _init_db()
    keywords = _extract_keywords(query)
    if not keywords:
        return {"patterns": []}
    
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    # Build a simple keyword search
    like_conditions = []
    params = []
    for kw in keywords[:5]:  # Limit to 5 keywords for performance
        like_conditions.append("(tags LIKE ? OR query LIKE ? OR solution LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
    
    where_clause = " OR ".join(like_conditions)
    query_sql = f"""
        SELECT id, task_type, query, solution, tool_sequence, tags, success_count, failure_count
        FROM knowledge_patterns
        WHERE {where_clause}
        ORDER BY success_count DESC, failure_count ASC, last_used DESC
        LIMIT ?
    """
    params.append(limit)
    
    cursor.execute(query_sql, params)
    rows = cursor.fetchall()
    conn.close()
    
    patterns = []
    for row in rows:
        patterns.append({
            "id": row[0],
            "task_type": row[1],
            "query": row[2],
            "solution": row[3],
            "tool_sequence": json.loads(row[4]) if row[4] else [],
            "tags": row[5],
            "success_count": row[6],
            "failure_count": row[7]
        })
    
    return {"patterns": patterns}

def update_pattern_success(pattern_id: int, success: bool) -> Dict[str, Any]:
    """
    Update the success/failure count of a pattern.
    
    Args:
        pattern_id: The pattern ID to update
        success: True if the pattern worked, False if it failed
    
    Returns:
        dict with status
    """
    _init_db()
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    if success:
        cursor.execute("""
            UPDATE knowledge_patterns
            SET success_count = success_count + 1, last_used = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), pattern_id))
    else:
        cursor.execute("""
            UPDATE knowledge_patterns
            SET failure_count = failure_count + 1
            WHERE id = ?
        """, (pattern_id,))
    
    conn.commit()
    conn.close()
    return {"status": "updated", "id": pattern_id, "success": success}

def get_knowledge_stats() -> Dict[str, Any]:
    """Get statistics about the knowledge base."""
    _init_db()
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM knowledge_patterns")
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(success_count) FROM knowledge_patterns")
    total_successes = cursor.fetchone()[0] or 0
    
    conn.close()
    
    return {
        "total_patterns": total,
        "total_successes": total_successes,
        "average_success_per_pattern": total_successes / total if total > 0 else 0
    }