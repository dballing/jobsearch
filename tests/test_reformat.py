"""reformat_description engine selection by model generation.

A faithful reformat wants deterministic output, so on a non-reasoning model (the Haiku default)
it runs at temperature 0. Reasoning models (Claude 4.6+/5) reject `temperature`, so there it
drops temperature and runs adaptive thinking at the configured effort instead. Only the create()
kwargs are asserted (no live call); the integrity check lives in ingest, not here.
"""
import reformat


class _FakeClient:
    def __init__(self, reply="**Clean** markdown."):
        self._reply = reply
        self.last_kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        msg = type("M", (), {})()
        msg.content = [type("C", (), {"type": "text", "text": self._reply})()]
        msg.stop_reason = "end_turn"
        msg.usage = type("U", (), {"input_tokens": 0, "output_tokens": 0,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})()
        return msg


def test_reformat_non_reasoning_model_uses_temperature_zero_and_ignores_effort():
    c = _FakeClient()
    reformat.reformat_description(c, "Some job description text.", model="claude-haiku-4-5", effort="high")
    assert c.last_kwargs["temperature"] == 0
    assert "thinking" not in c.last_kwargs and "output_config" not in c.last_kwargs


def test_reformat_reasoning_model_drops_temperature_for_thinking_at_effort():
    c = _FakeClient()
    reformat.reformat_description(c, "Some job description text.", model="claude-sonnet-5", effort="low")
    assert "temperature" not in c.last_kwargs          # would 400 on a reasoning model
    assert c.last_kwargs["thinking"] == {"type": "adaptive"}
    assert c.last_kwargs["output_config"] == {"effort": "low"}


def test_reformat_empty_text_makes_no_call():
    c = _FakeClient()
    assert reformat.reformat_description(c, "   ", model="claude-sonnet-5") == (None, None)
    assert c.last_kwargs is None
