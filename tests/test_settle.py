import random
from collections import defaultdict
from itertools import combinations

import pytest

from holdem.settle import EXACT_LIMIT, settle


def apply(nets, transfers):
    bal = defaultdict(int, nets)
    for payer, receiver, amount in transfers:
        assert amount > 0 and payer != receiver
        assert nets[payer] < 0 < nets[receiver]  # losers pay, winners receive
        bal[payer] += amount
        bal[receiver] -= amount
    return {k: v for k, v in bal.items() if v}


def min_transfers_brute(values):
    """Reference: n - (max number of disjoint zero-sum groups), by brute force."""
    values = [v for v in values if v]
    if not values:
        return 0

    def max_groups(vals):
        if not vals:
            return 0
        first, rest = vals[0], vals[1:]
        best = 0
        for r in range(len(rest) + 1):
            for combo in combinations(range(len(rest)), r):
                if first + sum(rest[i] for i in combo) == 0:
                    left = [rest[i] for i in range(len(rest)) if i not in combo]
                    best = max(best, 1 + max_groups(left))
        return best

    return len(values) - max_groups(values)


def test_simple_cases():
    assert settle({"a": 0, "b": 0}) == []
    assert settle({"a": -100, "b": 100}) == [("a", "b", 100)]
    # Pairs that cancel out settle directly instead of chaining through others.
    t = settle({"a": -500, "b": 500, "c": -300, "d": 300})
    assert sorted(t) == [("a", "b", 500), ("c", "d", 300)]


def test_rejects_unbalanced():
    with pytest.raises(ValueError):
        settle({"a": -100, "b": 50})


def test_random_cases_are_exact_and_minimal():
    rng = random.Random(7)
    for _ in range(300):
        n = rng.randint(2, 7)
        vals = [rng.choice([-3, -2, -1, 0, 1, 2, 3]) * 5000 for _ in range(n - 1)]
        vals.append(-sum(vals))
        nets = {f"p{i}": v for i, v in enumerate(vals)}
        t = settle(nets)
        assert apply(nets, t) == {}
        assert len(t) == min_transfers_brute(vals)


def test_large_table_falls_back_but_still_settles():
    rng = random.Random(1)
    vals = [rng.randint(-30, 30) * 1000 for _ in range(EXACT_LIMIT + 4)]
    vals.append(-sum(vals))
    nets = {i: v for i, v in enumerate(vals)}
    t = settle(nets)
    assert apply(nets, t) == {}
    assert len(t) < len([v for v in vals if v])
