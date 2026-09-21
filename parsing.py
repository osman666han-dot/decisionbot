import re
from dataclasses import dataclass, field

import config

_PREFIX = re.compile(r"^\s*(?:\d{1,2}\s*[.)]|[-–—•*·])\s*")


@dataclass
class Parsed:
    question: str = ""
    options: list[str] = field(default_factory=list)
    error: str | None = None  # "few" | "many" | "long"

    @property
    def ok(self) -> bool:
        return self.error is None


def parse(text: str) -> Parsed:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return Parsed(error="few")
    question = lines[0]
    options = [_PREFIX.sub("", ln).strip() for ln in lines[1:]]
    options = [o for o in options if o]
    if len(options) < config.MIN_OPTIONS:
        return Parsed(question, options, "few")
    if len(options) > config.MAX_OPTIONS:
        return Parsed(question, options, "many")
    if (
        len(text) > config.MAX_TOTAL_LEN
        or len(question) > config.MAX_QUESTION_LEN
        or any(len(o) > config.MAX_OPTION_LEN for o in options)
    ):
        return Parsed(question, options, "long")
    return Parsed(question, options, None)
