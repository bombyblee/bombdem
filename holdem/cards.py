from __future__ import annotations

import random
from dataclasses import dataclass

RANK_CHARS = "23456789TJQKA"
SUITS = "shdc"
# U+FE0F asks for the colour emoji form (red ♥️♦️); without it Telegram may draw
# plain monochrome text glyphs, e.g. inside bold text.
SUIT_SYMBOLS = {"s": "♠️", "h": "♥️", "d": "♦️", "c": "♣️"}


@dataclass(frozen=True)
class Card:
    rank: int  # 2..14 (14 = Ace)
    suit: str  # one of "shdc"

    def __str__(self) -> str:
        r = RANK_CHARS[self.rank - 2]
        if r == "T":
            r = "10"
        return f"{r}{SUIT_SYMBOLS[self.suit]}"

    @classmethod
    def parse(cls, text: str) -> "Card":
        """Parse a short code such as 'As', 'Td', '9c'."""
        return cls(RANK_CHARS.index(text[0].upper()) + 2, text[1].lower())


def parse_cards(text: str) -> list[Card]:
    return [Card.parse(t) for t in text.split()]


def format_cards(cards) -> str:
    return " ".join(str(c) for c in cards) if cards else "-"


class Deck:
    def __init__(self, rng: random.Random | None = None):
        self.cards = [Card(r, s) for r in range(2, 15) for s in SUITS]
        # SystemRandom (OS entropy) by default so shuffles can't be predicted.
        (rng or random.SystemRandom()).shuffle(self.cards)

    def draw(self, n: int = 1) -> list[Card]:
        if n > len(self.cards):
            raise ValueError("deck exhausted")
        drawn, self.cards = self.cards[:n], self.cards[n:]
        return drawn
