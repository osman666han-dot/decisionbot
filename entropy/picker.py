import hashlib


def pick(base: str, n: int) -> tuple[int, str]:
    """SHA-256 от строки -> индекс 0..n-1 без перекоса (rejection sampling).

    Если число попало в «хвост», который не делится на n нацело, оно отбрасывается
    и хэш пересчитывается с увеличенным счётчиком.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    limit = (1 << 256) // n * n
    counter = 0
    while True:
        digest = hashlib.sha256(f"{base}\nctr={counter}".encode("utf-8")).digest()
        value = int.from_bytes(digest, "big")
        if value < limit:
            return value % n, digest.hex()
        counter += 1
