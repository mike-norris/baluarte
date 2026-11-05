"""The reproducible sample of changes in an audit-period evidence package.

The population is every merged merge request and direct push on a monitored default branch in
the period, by change ID. The sample is drawn with Python's ``random.Random(seed)`` (Mersenne
Twister) calling ``sample`` on the IDs sorted ascending, then sorted ascending. The same seed
and population always give the same sample, so anyone holding the package can redraw it:
``baluarte-verify-export`` does.
"""

import random
import secrets
from collections.abc import Iterable

ALGORITHM = (
    "Python random.Random(seed).sample(population, size) over the change IDs sorted ascending; "
    "the result sorted ascending. A size at or above the population takes every change."
)
SAMPLED_KINDS = ("merge_request", "direct_push")
MAX_SEED = 2**63 - 1


def new_seed() -> int:
    return secrets.randbelow(2**31)


def draw(population: Iterable[int], size: int, seed: int) -> list[int]:
    ordered = sorted(set(population))
    if size >= len(ordered):
        return ordered
    # Not for security: the point is that anyone can draw the same sample again.
    return sorted(random.Random(seed).sample(ordered, size))  # noqa: S311
