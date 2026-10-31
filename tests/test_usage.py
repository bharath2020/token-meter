import json
import unittest
from datetime import datetime, timezone

from token_meter import usage
from token_meter.usage import Tokens, parse_line, tokens_from_usage

TS = "2026-10-30T12:00:00.000Z"
TS_EPOCH = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def assistant(usage_dict, model="claude-opus-5-5", msg_id="msg_1", req_id="req_1", ts=TS, **extra):
    d = {"type": "assistant", "message": {"model": model, "usage": usage_dict}}
    if msg_id is not None:
        d["message"]["id"] = msg_id
    if req_id is not None:
        d["requestId"] = req_id
    if ts is not None:
        d["timestamp"] = ts
    d.update(extra)
    return json.dumps(d)


FULL_USAGE = {
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 999,  # ignored when cache_creation is present
    "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 50},
}


class TokensFromUsageTest(unittest.TestCase):
    def test_breakdown_from_cache_creation(self):
        t = tokens_from_usage(FULL_USAGE)
        self.assertEqual(t, Tokens(input=10, output=20, write_5m=40, write_1h=50, cache_read=30))
        self.assertEqual(t.cache_write, 90)
        self.assertEqual(t.prompt, 10 + 30 + 90)
        self.assertEqual(t.total, 10 + 30 + 90 + 20)

    def test_fallback_to_total_as_5m_when_cache_creation_absent(self):
        t = tokens_from_usage({"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 77})
        self.assertEqual(t.write_5m, 77)
        self.assertEqual(t.write_1h, 0)

    def test_fallback_when_cache_creation_empty(self):
        t = tokens_from_usage({"cache_creation": {}, "cache_creation_input_tokens": 5})
        self.assertEqual((t.write_5m, t.write_1h), (5, 0))

    def test_missing_and_non_int_values_are_zero(self):
        t = tokens_from_usage({"input_tokens": None, "output_tokens": "12"})
        self.assertEqual(t, Tokens())


class ParseLineTest(unittest.TestCase):
    def test_assistant_line_str(self):
        call = parse_line(assistant(FULL_USAGE))
        self.assertIsNotNone(call)
        self.assertEqual(call.tokens, Tokens(10, 20, 40, 50, 30))
        self.assertEqual(call.key, ("msg_1", "req_1"))
        self.assertEqual(call.ts, TS_EPOCH)
        self.assertEqual(call.model, "claude-opus-5-5")
        self.assertTrue(call.priced)
        self.assertGreater(call.cost["input"], 0)

    def test_assistant_line_bytes(self):
        line = assistant(FULL_USAGE)
        a, b = parse_line(line), parse_line(line.encode())
        self.assertIsNotNone(b)
        self.assertEqual(a.tokens, b.tokens)
        self.assertEqual(a.ts, b.ts)
        self.assertEqual(a.key, b.key)

    def test_fallback_cache_creation_input_tokens(self):
        call = parse_line(assistant({"input_tokens": 1, "cache_creation_input_tokens": 300}))
        self.assertEqual((call.tokens.write_5m, call.tokens.write_1h), (300, 0))

    def test_non_assistant_line(self):
        line = json.dumps({"type": "user", "timestamp": TS, "message": {"usage": FULL_USAGE}})
        self.assertIsNone(parse_line(line))
        self.assertIsNone(parse_line(line.encode()))

    def test_line_without_usage(self):
        line = json.dumps({"type": "assistant", "timestamp": TS, "message": {"id": "m", "model": "x"}})
        self.assertIsNone(parse_line(line))

    def test_usage_not_a_dict(self):
        line = json.dumps({"type": "assistant", "timestamp": TS, "message": {"usage": 5}})
        self.assertIsNone(parse_line(line))

    def test_bad_json(self):
        self.assertIsNone(parse_line('{"type": "assistant", "usage": '))
        self.assertIsNone(parse_line(b'{"usage" nonsense'))

    def test_non_dict_json(self):
        self.assertIsNone(parse_line('["usage"]'))

    def test_missing_timestamp(self):
        self.assertIsNone(parse_line(assistant(FULL_USAGE, ts=None)))

    def test_bad_timestamp(self):
        self.assertIsNone(parse_line(assistant(FULL_USAGE, ts="not a date")))
        self.assertIsNone(parse_line(assistant(FULL_USAGE, ts=12345)))

    def test_key_with_only_request_id(self):
        call = parse_line(assistant(FULL_USAGE, msg_id=None, req_id="req_9"))
        self.assertEqual(call.key, (None, "req_9"))

    def test_key_without_ids_uses_uuid_or_line(self):
        a = parse_line(assistant(FULL_USAGE, msg_id=None, req_id=None, uuid="u-1"))
        self.assertEqual(a.key, ("line", "u-1"))
        line = assistant(FULL_USAGE, msg_id=None, req_id=None)
        b = parse_line(line)
        self.assertEqual(b.key, ("line", line))

    def test_unknown_model_zero_cost_unpriced(self):
        call = parse_line(assistant(FULL_USAGE, model="gpt-9"))
        self.assertFalse(call.priced)
        self.assertEqual(call.cost, usage.ZERO_COST)
        self.assertEqual(call.tokens.input, 10)


if __name__ == "__main__":
    unittest.main()
