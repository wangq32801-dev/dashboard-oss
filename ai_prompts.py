"""Versioned prompt contracts for the private cockpit AI features.

The actual prose prompts intentionally remain close to the feature code for now;
this module is the stable contract/metadata layer.  It gives every caller the
same evidence boundary, input/output shape and a small, auditable header without
ever embedding user data or secrets.
"""

from copy import deepcopy

PROMPT_CATALOG_VERSION = "ai-contract-v1"

_BASE_BOUNDARY = (
    "只把当前请求附带的证据包中明确出现的内容当作事实；缺失、过期或未观测项必须直说暂无证据。"
    "推断必须标为推断，建议必须标为建议，不得用常识或旧对话补写数字。"
)

PROMPT_CATALOG = {
    "butler": {
        "version": "butler.v2",
        "input_schema": {"messages": "array", "evidence_bundle": "object"},
        "output_schema": {"text": "string", "actions": "confirmed_action[]"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
    "advisor": {
        "version": "advisor.v2",
        "input_schema": {"scene": "string", "snapshot": "object", "mode": "suggest|ask"},
        "output_schema": {"text": "string", "actions": "proposal[]", "evidence": "evidence_bundle"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
    "oracle": {
        "version": "oracle.v2",
        "input_schema": {"dashboard_snapshot": "object", "health_snapshot": "object"},
        "output_schema": {"oracle": "string", "sleep_brief": "string", "ppc_brief": "string", "focus_tip": "string"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
    "night_watch": {
        "version": "night-watch.v1",
        "input_schema": {"role_snapshot": "object", "window_days": "integer"},
        "output_schema": {"insight": "string", "source_window": "string"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
    "weekly_review": {
        "version": "weekly-review.v1",
        "input_schema": {"week_snapshot": "object", "role": "string"},
        "output_schema": {"draft": "string", "next_action": "string"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
    "health_briefing": {
        "version": "health-briefing.v2",
        "input_schema": {"health_snapshot": "object", "window_days": "integer"},
        "output_schema": {"text": "string", "date": "string"},
        "evidence_boundary": (
            _BASE_BOUNDARY + " 健康内容不是诊断；只有出现明确风险信号时才给出就医提醒。"
        ),
    },
    "draft": {
        "version": "draft.v2",
        "input_schema": {"scene": "string", "context": "object", "input": "string"},
        "output_schema": {"draft": "string"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
    "profile": {
        "version": "profile.v1",
        "input_schema": {"conversation": "array"},
        "output_schema": {"items": "profile_fact[]"},
        "evidence_boundary": "只提取用户明确说出的长期事实，不把模型推断写入画像。",
    },
    "memory_digest": {
        "version": "memory-digest.v1",
        "input_schema": {"conversation": "array"},
        "output_schema": {"points": "string[]"},
        "evidence_boundary": "只沉淀用户明确表达且值得跨会话记住的内容，过滤敏感信息和噪声。",
    },
    "socratic": {
        "version": "socratic.v1",
        "input_schema": {"scene": "string", "answers": "string[]"},
        "output_schema": {"draft": "string"},
        "evidence_boundary": _BASE_BOUNDARY,
    },
}


def prompt_meta(scene):
    """Return a defensive copy so feature code cannot mutate the catalog."""
    key = str(scene or "").strip()
    meta = PROMPT_CATALOG.get(key) or PROMPT_CATALOG["draft"]
    out = deepcopy(meta)
    out["scene"] = key or "draft"
    out["catalog"] = PROMPT_CATALOG_VERSION
    return out


def prompt_header(scene):
    """Small, deterministic system-prefix shared by all AI calls."""
    meta = prompt_meta(scene)
    return (
        "【驾驶舱 AI 契约 %s · %s】\n%s\n"
        "输出必须符合场景约定的结构；任何待执行动作只能作为待确认提案，不能声称已经执行。\n"
        % (meta["catalog"], meta["version"], meta["evidence_boundary"])
    )


def catalog_scenes():
    return tuple(PROMPT_CATALOG.keys())
