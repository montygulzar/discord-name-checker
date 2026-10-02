"""Username generation for short Discord names (3L/4L, 3N/4N, 3C/4C)."""

import itertools
import random
import re
import string
from typing import Iterable, Iterator

CHARSETS = {
    "letters": string.ascii_lowercase,
    "numbers": string.digits,
    "chars": string.ascii_lowercase + string.digits + "_.",
}

# Discord usernames: 2-32 chars, lowercase a-z, 0-9, underscore and period,
# with no two periods in a row.
_VALID_RE = re.compile(r"^[a-z0-9_.]{2,32}$")


def is_valid(name: str) -> bool:
    return bool(_VALID_RE.match(name)) and ".." not in name


def classify(name: str) -> str:
    """Short label like '3L', '4N' or '3C'."""
    if name.isalpha():
        kind = "L"
    elif name.isdigit():
        kind = "N"
    else:
        kind = "C"
    return f"{len(name)}{kind}"


def count(lengths: Iterable[int], charset: str) -> int:
    """Number of names generate() will yield (only exact for small lengths)."""
    return sum(1 for _ in generate(lengths, charset))


def generate(lengths: Iterable[int], charset: str, shuffle: bool = False) -> Iterator[str]:
    """Yield every valid name of the given lengths built from charset.

    Lazy unless shuffle is set, in which case each length's names are
    materialised so they can be randomised (fine for lengths <= 4).
    """
    chars = "".join(sorted(set(charset.lower())))
    for length in lengths:
        names = ("".join(p) for p in itertools.product(chars, repeat=length))
        names = (n for n in names if is_valid(n))
        if shuffle:
            names = list(names)
            random.shuffle(names)
        yield from names


def load_wordlist(path: str) -> list[str]:
    """Read names from a file, one per line; lowercase, dedupe, drop invalid."""
    seen: dict[str, None] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            name = line.strip().lower()
            if name and is_valid(name):
                seen.setdefault(name, None)
    return list(seen)
