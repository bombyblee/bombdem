"""Settle up at the end of a session with as few transfers as possible.

Each player's net is (final chips - chips bought). Splitting the players into the
largest number of groups whose nets sum to zero gives the minimum number of transfers:
a zero-sum group of k players always settles in k-1 transfers, and no fewer.
Finding that split is exponential, so it's exact up to EXACT_LIMIT players with a
non-zero net (plenty for a 9-seat table plus a few who left) and greedy beyond that.
"""
from __future__ import annotations

from typing import Hashable

EXACT_LIMIT = 14  # 2^14 subsets: well under a second, so it won't stall the bot

Transfer = tuple[Hashable, Hashable, int]  # (payer, receiver, amount)


def settle(nets: dict[Hashable, int]) -> list[Transfer]:
    items = [(k, v) for k, v in nets.items() if v]
    if sum(v for _, v in items) != 0:
        raise ValueError("nets must sum to zero")
    if not items:
        return []
    groups = _zero_sum_groups(items) if len(items) <= EXACT_LIMIT else [items]
    transfers: list[Transfer] = []
    for g in groups:
        transfers += _greedy(g)
    return transfers


def _zero_sum_groups(items: list[tuple[Hashable, int]]) -> list[list[tuple[Hashable, int]]]:
    n = len(items)
    full = (1 << n) - 1
    sums = [0] * (1 << n)
    for mask in range(1, 1 << n):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + items[low.bit_length() - 1][1]
    # best[mask] = most zero-sum groups the players in `mask` can be split into
    best = [0] * (1 << n)
    for mask in range(1, 1 << n):
        m = 0
        bits = mask
        while bits:
            low = bits & -bits
            bits ^= low
            m = max(m, best[mask ^ low])
        best[mask] = m + (sums[mask] == 0)

    # Walk back from the full set; players removed between two zero-sum masks form a group.
    groups, current, mask = [], [], full
    while mask:
        bits, pick = mask, None
        while bits:
            low = bits & -bits
            bits ^= low
            if pick is None or best[mask ^ low] > best[mask ^ pick]:
                pick = low
        current.append(items[pick.bit_length() - 1])
        mask ^= pick
        if sums[mask] == 0:
            groups.append(current)
            current = []
    return groups


def _greedy(group: list[tuple[Hashable, int]]) -> list[Transfer]:
    """Biggest loser pays biggest winner until everyone is square (k-1 transfers for k players)."""
    debtors = sorted(([k, -v] for k, v in group if v < 0), key=lambda x: -x[1])
    creditors = sorted(([k, v] for k, v in group if v > 0), key=lambda x: -x[1])
    out: list[Transfer] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        out.append((debtors[i][0], creditors[j][0], amount))
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return out
