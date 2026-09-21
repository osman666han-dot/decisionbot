"""Склейка живых событий в одну строку. Формат неизменен, иначе бросок не проверить."""

VERSION = "decision-bot/v1"


def build(request_id: str, t0_ns: int, source: str, events: list[str]) -> str:
    lines = [VERSION, f"req={request_id}", f"t0={t0_ns}", f"src={source}"]
    lines += [f"ev{i}={e}" for i, e in enumerate(events)]
    return "\n".join(lines)
