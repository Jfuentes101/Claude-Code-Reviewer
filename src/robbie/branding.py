"""The bot's byline. The engine stays robbie; what humans read is config.

`config.load` calls `set_name` once with `bot_name` from robbie.yaml, and every
string a person sees goes through here — so two instances of the same code can
sign differently (JM's as robbie, Jimmy's as PR Ops). Machine markers
(`<!-- robbie-* -->`) are dedupe keys, not branding, and never change.

Both signature forms are functions on purpose: publish appends them and
contract._strip matches them, and those must be the same string at runtime —
a constant captured at import would freeze one side out of a rename.
"""

_NAME = "robbie"


def set_name(name: str) -> None:
    global _NAME
    _NAME = name


def name() -> str:
    return _NAME


def signature() -> str:
    return f"🤖 **Automated pre-review by {_NAME}**"


def signature_sub() -> str:
    return f"<sub>🤖 automated pre-review by {_NAME}</sub>"
