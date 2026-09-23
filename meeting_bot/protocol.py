"""Shared bilingual protocol format for cloud and local processing."""

PROTOCOL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "summary_kk": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "decisions_kk": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "action_kk": {"type": "string"},
                    "assignee": {"type": ["string", "null"]},
                    "deadline": {"type": ["string", "null"]},
                    "deadline_kk": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "needs_review": {"type": "boolean"},
                },
                "required": ["action", "action_kk", "assignee", "deadline", "deadline_kk",
                             "evidence", "needs_review"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "summary_kk", "decisions", "decisions_kk", "tasks"],
    "additionalProperties": False,
}


def validate_protocol(protocol: dict) -> None:
    """Reject incomplete translations before writing JSON or DOCX."""
    if (not isinstance(protocol, dict)
            or any(not isinstance(protocol.get(key), str) for key in ("summary", "summary_kk"))
            or not isinstance(protocol.get("decisions"), list)
            or not isinstance(protocol.get("decisions_kk"), list)
            or not isinstance(protocol.get("tasks"), list)):
        raise ValueError("Invalid bilingual protocol")
    decisions = protocol["decisions"]
    decisions_kk = protocol["decisions_kk"]
    if (len(decisions) != len(decisions_kk)
            or any(not isinstance(item, str) or not item.strip()
                   for item in decisions + decisions_kk)
            or (bool(protocol["summary"].strip()) != bool(protocol["summary_kk"].strip()))):
        raise ValueError("Russian and Kazakh protocol sections do not match")
    for task in protocol["tasks"]:
        if (not isinstance(task, dict)
                or any(not isinstance(task.get(key), str) or not task[key].strip()
                       for key in ("action", "action_kk", "evidence"))
                or not isinstance(task.get("needs_review"), bool)
                or any(key not in task or (task[key] is not None and not isinstance(task[key], str))
                       for key in ("assignee", "deadline", "deadline_kk"))
                or (task["deadline"] is None) != (task["deadline_kk"] is None)):
            raise ValueError("Invalid bilingual task")
        if not task["assignee"] or not task["deadline"]:
            task["needs_review"] = True
