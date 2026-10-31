"""API-equivalent pricing at list rates, in US dollars per million tokens."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float

    @property
    def write_5m(self):
        return self.input * 1.25

    @property
    def write_1h(self):
        return self.input * 2.0


PRICES = {
    "claude-opus-5-5": Rates(4.0, 20.0, 0.20),
    "claude-sonnet-5-5": Rates(2.0, 10.0, 0.20),
    "claude-haiku-5-5": Rates(0.10, 0.50, 0.01),
}
# Haiku prompts over this many tokens (input + cache read + cache writes) bill at the long rate.
HAIKU_LONG_PROMPT = 100_000
HAIKU_LONG = Rates(0.50, 2.50, 0.05)


def base_model(model):
    """Map a logged model name to a priced model, or None when unknown.

    Accepts suffixes such as a context tag (``claude-opus-5-5[1m]``) or a date.
    """
    if not model:
        return None
    name = model.split("[", 1)[0]
    for known in PRICES:
        if name == known or name.startswith(known + "-"):
            return known
    return None


def cost(model, tokens):
    """Return per-category dollar costs for one API call, or None for an unknown model.

    ``tokens`` is a :class:`token_meter.usage.Tokens`.
    """
    known = base_model(model)
    if known is None:
        return None
    rates = PRICES[known]
    if known == "claude-haiku-5-5" and tokens.prompt > HAIKU_LONG_PROMPT:
        rates = HAIKU_LONG
    m = 1e6
    return {
        "input": tokens.input * rates.input / m,
        "output": tokens.output * rates.output / m,
        "cache_write": (tokens.write_5m * rates.write_5m + tokens.write_1h * rates.write_1h) / m,
        "cache_read": tokens.cache_read * rates.cache_read / m,
    }
