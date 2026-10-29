"""Versioned, read-only English guide shared by CLI, MCP and website documentation."""
from __future__ import annotations
import copy
import hashlib
import json
from . import __version__

PRODUCT = "FrameBridge"
GUIDE_SCHEMA = 1
from .guide_data import GUIDE as _GUIDE

def topics():
    """Return the bounded public guide-topic names."""
    return tuple(_GUIDE)

def _digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()

def get_guide(topic="start"):
    """Read static help without touching profiles, credentials, processes or network."""
    if not isinstance(topic, str) or len(topic) > 64:
        return {"ok": False, "error": "HELP_TOPIC_INVALID", "topics": list(topics())}
    topic = topic.strip().lower()
    if topic == "all":
        sections = []
        for name, entry in _GUIDE.items():
            sections.append({"heading": entry["title"], "body": "Guide topic: " + name, "commands": []})
            sections.extend(copy.deepcopy(entry["sections"]))
        title = "FrameBridge complete command guide"
    elif topic in _GUIDE:
        entry = _GUIDE[topic]
        title, sections = entry["title"], copy.deepcopy(entry["sections"])
    else:
        return {"ok": False, "error": "HELP_TOPIC_UNKNOWN", "topic": topic, "topics": list(topics()),
                "product": PRODUCT, "version": __version__, "guide_schema": GUIDE_SCHEMA}
    value = {"ok": True, "product": PRODUCT, "version": __version__, "guide_schema": GUIDE_SCHEMA,
             "topic": topic, "title": title, "sections": sections, "topics": list(topics()),
             "read_only": True, "capabilities_verified": False}
    value["guide_sha256"] = _digest(value)
    return value

def render_text(topic="start"):
    """Render the same guide as a navigable offline text reference."""
    guide = get_guide(topic)
    if not guide["ok"]:
        raise ValueError("Unknown help topic. Choose: " + ", ".join(topics()))
    lines = [guide["title"], "FrameBridge " + guide["version"] + " | guide schema " + str(GUIDE_SCHEMA), ""]
    for section in guide["sections"]:
        lines.extend([section["heading"], section["body"], ""])
        lines.extend("  " + command for command in section["commands"])
        if section["commands"]:
            lines.append("")
    if topic != "all":
        lines.append("More help: fb help " + " | ".join(topics()))
    return "\n".join(lines).rstrip() + "\n"

def export_guide():
    """Return the versioned static export used to generate website documentation."""
    value = {"product": PRODUCT, "version": __version__, "guide_schema": GUIDE_SCHEMA,
             "topics": {name: get_guide(name) for name in topics()}}
    value["guide_sha256"] = _digest(value)
    return value
