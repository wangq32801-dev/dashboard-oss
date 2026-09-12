"""Stable, source-aware entity projections used by the migration shadow layer."""
from __future__ import annotations
from typing import Any

def task(t: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': str(t.get('id', '')), 'title': str(t.get('title', '')).strip(),
        'project_id': t.get('pid'), 'priority': int(t.get('priority') or 0),
        'due': t.get('due'), 'tags': sorted(set(t.get('tags') or [])),
        'content': t.get('content') or '', 'archived': 'archived' in (t.get('tags') or []),
        'source': 'ticktick',
    }

def role(name: str, markdown: str) -> dict[str, Any]:
    return {'id': name, 'name': name, 'markdown': markdown, 'source': 'obsidian'}

def local_state(state: dict[str, Any]) -> dict[str, Any]:
    return {'id': 'dash-state', 'state': state, 'source': 'local_json'}

def dated_note(item: dict[str, Any], kind_name: str, source: str) -> dict[str, Any]:
    """Project notes while retaining every unknown metric/field in payload."""
    ident = item.get('id') or item.get('ts') or item.get('date')
    return {'id': str(ident or ''), 'kind': kind_name, 'date': item.get('date') or item.get('week'),
            'deleted': bool(item.get('deleted')), 'payload': item, 'source': source}

def normalize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize exported TickTick-shaped task rows; preserve unknown entities."""
    out = []
    for item in items:
        out.append(task(item) if 'title' in item and 'id' in item else item)
    return out
