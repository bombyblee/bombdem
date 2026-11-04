from __future__ import annotations

from collections import Counter
from itertools import combinations

from .cards import Card

HIGH_CARD, ONE_PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)

HAND_NAMES = [
    "하이카드",
    "원페어",
    "투페어",
    "트리플",
    "스트레이트",
    "플러시",
    "풀하우스",
    "포카드",
    "스트레이트 플러시",
]

Score = tuple  # (category, (tiebreak ranks...)) — compares correctly with < / >


def evaluate5(cards: list[Card]) -> Score:
    ranks = sorted((c.rank for c in cards), reverse=True)
    groups = sorted(Counter(ranks).items(), key=lambda x: (x[1], x[0]), reverse=True)
    is_flush = len({c.suit for c in cards}) == 1

    uniq = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:  # wheel: A-2-3-4-5
            straight_high = 5

    if straight_high and is_flush:
        return (STRAIGHT_FLUSH, (straight_high,))
    if groups[0][1] == 4:
        return (QUADS, (groups[0][0], groups[1][0]))
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (FULL_HOUSE, (groups[0][0], groups[1][0]))
    if is_flush:
        return (FLUSH, tuple(ranks))
    if straight_high:
        return (STRAIGHT, (straight_high,))
    if groups[0][1] == 3:
        return (TRIPS, tuple(r for r, _ in groups))
    if groups[0][1] == 2 and groups[1][1] == 2:
        return (TWO_PAIR, tuple(r for r, _ in groups))
    if groups[0][1] == 2:
        return (ONE_PAIR, tuple(r for r, _ in groups))
    return (HIGH_CARD, tuple(ranks))


def best_hand(cards: list[Card]) -> tuple[Score, list[Card]]:
    """Best 5-card hand out of 5..7 cards. Returns (score, the five cards)."""
    if len(cards) < 5:
        raise ValueError("need at least 5 cards")
    best = None
    for combo in combinations(cards, 5):
        score = evaluate5(list(combo))
        if best is None or score > best[0]:
            best = (score, list(combo))
    return best


def hand_name(score: Score) -> str:
    if score[0] == STRAIGHT_FLUSH and score[1][0] == 14:
        return "로열 플러시"
    return HAND_NAMES[score[0]]


def partial_hand_name(cards: list[Card]) -> str:
    """Name for fewer than 5 cards (e.g. preflop hole cards)."""
    if len(cards) >= 5:
        return hand_name(best_hand(cards)[0])
    counts = sorted(Counter(c.rank for c in cards).values(), reverse=True)
    if counts and counts[0] >= 3:
        return HAND_NAMES[TRIPS]
    if counts.count(2) >= 2:
        return HAND_NAMES[TWO_PAIR]
    if counts and counts[0] == 2:
        return HAND_NAMES[ONE_PAIR]
    return HAND_NAMES[HIGH_CARD]
