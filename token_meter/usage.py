"""Parse usage records out of Claude Code session log lines."""

import json
from dataclasses import dataclass
from datetime import datetime

from . import pricing


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    output: int = 0
    write_5m: int = 0
    write_1h: int = 0
    cache_read: int = 0

    @property
    def cache_write(self):
        return self.write_5m + self.write_1h

    @property
    def prompt(self):
        return self.input + self.cache_read + self.cache_write

    @property
    def total(self):
        return self.prompt + self.output


@dataclass(frozen=True)
class Call:
    """One billed API call."""

    key: tuple
    ts: float
    model: str
    tokens: Tokens
    cost: dict  # per-category dollars; all zero for an unknown model
    priced: bool
    effort: str = ""  # effort recorded on the usage line itself, if any
    request_ref: str = None  # id of the "api-request" line this call answers


def _int(value):
    return value if isinstance(value, int) else 0


def tokens_from_usage(usage):
    cc = usage.get("cache_creation")
    if isinstance(cc, dict) and cc:
        w5 = _int(cc.get("ephemeral_5m_input_tokens"))
        w1 = _int(cc.get("ephemeral_1h_input_tokens"))
    else:
        # Older logs carry only the total; Claude Code's default cache TTL is 5 minutes.
        w5, w1 = _int(usage.get("cache_creation_input_tokens")), 0
    return Tokens(
        input=_int(usage.get("input_tokens")),
        output=_int(usage.get("output_tokens")),
        write_5m=w5,
        write_1h=w1,
        cache_read=_int(usage.get("cache_read_input_tokens")),
    )


ZERO_COST = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}


def parse_line(line):
    """Return a :class:`Call` for an assistant line with usage, else None.

    ``line`` may be ``bytes`` or ``str``.
    """
    marker = b'"usage"' if isinstance(line, bytes) else '"usage"'
    if marker not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    msg = d.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
        return None
    try:
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError):
        return None
    msg_id, req_id = msg.get("id"), d.get("requestId")
    # One API call can be logged on several lines; it is identified by (message id, request id).
    key = (msg_id, req_id) if (msg_id or req_id) else ("line", d.get("uuid") or line)
    model = msg.get("model") or ""
    tokens = tokens_from_usage(msg["usage"])
    c = pricing.cost(model, tokens)
    effort = d.get("effort") if isinstance(d.get("effort"), str) else ""
    ref = d.get("requestRef") if isinstance(d.get("requestRef"), str) else None
    return Call(key, ts, model, tokens, c or dict(ZERO_COST), c is not None, effort, ref)


def parse_request(line):
    """Return ``(request id, effort, timestamp)`` for an ``api-request`` line, else None.

    Claude Code logs each API request as an ``api-request`` line whose ``id`` the
    answering assistant lines carry as ``requestRef``; the effort level is in
    ``params.output_config.effort``.
    """
    markers = (b'"type":"api-request"', b'"type": "api-request"') if isinstance(line, bytes) \
        else ('"type":"api-request"', '"type": "api-request"')
    if not any(m in line for m in markers):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "api-request" or not isinstance(d.get("id"), str):
        return None
    params = d.get("params") if isinstance(d.get("params"), dict) else {}
    config = params.get("output_config") if isinstance(params.get("output_config"), dict) else {}
    effort = config.get("effort") if isinstance(config.get("effort"), str) else ""
    try:
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError):
        ts = None
    return d["id"], effort, ts
