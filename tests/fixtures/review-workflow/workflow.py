"""Synthetic source for read-only review-workflow evaluation cases.

This fixture is never a runtime service. It gives reviewers adjacent tests that
look consolidatable while preserving a small set of security and recovery
behaviors that must remain independently represented.
"""

from pathlib import PurePosixPath


def discover_commands(registry):
    """Expose only declared commands; experimental commands stay undiscoverable."""
    return sorted(name for name, command in registry.items() if command["public"])


def require_scope(scope):
    if not scope:
        raise ValueError("scope is required")
    if scope == "all":
        raise ValueError("unbounded scope is refused")
    return scope


def serve_http(body, close):
    try:
        return {"status": 200, "body": body}
    finally:
        close()


def send_tts(transport, audio):
    reply = transport.send(audio)
    if reply["status"] != 200:
        raise RuntimeError("tts transport failed")
    return reply["request_id"]


class VoicePool:
    def __init__(self, voices):
        self.available = list(voices)
        self.leased = set()

    def acquire(self):
        if not self.available:
            raise RuntimeError("no voice available")
        voice = self.available.pop(0)
        self.leased.add(voice)
        return voice

    def release(self, voice):
        if voice not in self.leased:
            raise ValueError("voice was not leased")
        self.leased.remove(voice)
        self.available.append(voice)


def summarize_usage(owner, grant, input_tokens, output_tokens):
    if owner == "_legacy":
        return {"owner_kind": "shared_legacy", "input_tokens": input_tokens,
                "output_tokens": output_tokens}
    return {"owner": owner, "grant": grant, "input_tokens": input_tokens,
            "output_tokens": output_tokens}


def require_token(token, expected):
    if not token or token != expected:
        raise PermissionError("authentication failed")
    return {"authenticated": True}


def drain_cancelled(events):
    drained = []
    for event in events:
        if event == "result":
            drained.append(event)
        if event == "done":
            return {"state": "cancelled", "drained": drained}
    raise RuntimeError("drain incomplete")


def recover_manifest(current, previous):
    if current.get("complete"):
        return current
    if previous and previous.get("complete"):
        return {"state": "recovered", "manifest": previous}
    raise RuntimeError("recovery required")


def safe_relative_path(path):
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path escapes fixture root")
    return candidate.as_posix()


def rest_record(core, payload):
    return core.record(payload)


def mcp_record(core, payload):
    return core.record(payload)


def normalize_tags(tags):
    return tuple(sorted(set(tags)))
