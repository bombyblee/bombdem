"""No-limit Texas Hold'em table state machine.

The Table never talks to Telegram. Every state-changing call returns a list of
Event objects that the bot layer turns into chat messages.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .cards import Card, Deck
from .evaluator import best_hand, hand_name

PREFLOP, FLOP, TURN, RIVER = "preflop", "flop", "turn", "river"
MAX_PLAYERS = 9


class GameError(Exception):
    """A rule violation or invalid request; message is user-facing (Korean)."""


@dataclass(eq=False)  # identity equality: two players are never "equal"
class Player:
    user_id: int
    name: str
    stack: int
    hole: list[Card] = field(default_factory=list)
    bet: int = 0  # chips put in on the current street
    total_bet: int = 0  # chips put in during the whole hand
    in_hand: bool = False  # dealt into the current hand
    folded: bool = False
    all_in: bool = False
    acted: bool = False  # acted since the last full raise on this street
    leaving: bool = False  # will be removed when the hand ends
    away: bool = False  # sitting out: keeps seat and chips, not dealt in
    timeouts: int = 0  # consecutive turn timeouts (reset on any real action)
    buy_in: int = 0  # total chips bought (starting stack + rebuys)
    rebuys: int = 0
    returning: bool = False  # rejoined with the stack they left with

    @property
    def live(self) -> bool:
        return self.in_hand and not self.folded


@dataclass(eq=False)
class Pot:
    amount: int
    eligible: list[Player]
    winners: list[Player] = field(default_factory=list)
    hand_label: str | None = None


@dataclass
class ShowdownEntry:
    player: Player
    hole: list[Card]
    hand_label: str
    best_five: list[Card]


@dataclass
class HandResult:
    hand_no: int
    board: list[Card]
    uncontested: bool
    pots: list[Pot]
    showdown: list[ShowdownEntry]
    payouts: dict[int, int]  # user_id -> chips won
    removed: list[Player] = field(default_factory=list)  # left the table
    busted: list[Player] = field(default_factory=list)  # out of chips, still seated
    holes: dict[int, list[Card]] = field(default_factory=dict)  # user_id -> hole cards
    full_board: list[Card] = field(default_factory=list)  # board run out to the river


@dataclass
class Event:
    kind: str  # "action" | "street" | "result"
    player: Player | None = None
    action: str | None = None  # fold / check / call / bet / raise
    amount: int = 0  # call: chips added; bet/raise: total bet size ("raise to")
    all_in: bool = False
    street: str | None = None
    cards: list[Card] | None = None
    result: HandResult | None = None


@dataclass
class LegalActions:
    to_call: int
    can_check: bool
    can_raise: bool
    min_raise_to: int | None
    max_raise_to: int


class Table:
    def __init__(
        self,
        chat_id: int,
        starting_stack: int = 10_000,
        small_blind: int = 50,
        big_blind: int = 100,
        rng: random.Random | None = None,
    ):
        if not (0 < small_blind <= big_blind < starting_stack):
            raise GameError("블라인드/스택 설정이 올바르지 않습니다. (0 < SB ≤ BB < 시작 스택)")
        self.chat_id = chat_id
        self.starting_stack = starting_stack
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.rng = rng

        self.players: list[Player] = []
        self.button = -1  # index into players
        self.sb_index: int | None = None
        self.bb_index: int | None = None
        self.hand_no = 0
        self.in_progress = False
        self.deck: Deck | None = None
        self.board: list[Card] = []
        self.street: str | None = None
        self.current: int | None = None  # index of player to act
        self.current_bet = 0
        self.min_raise = big_blind
        self.action_seq = 0  # bumps on every action; used to expire stale buttons/timers
        self.last_result: HandResult | None = None  # most recently *finished* hand
        self.pending_blinds: tuple[int, int] | None = None
        # user_id -> {"stack", "buy_in", "rebuys"} of players who left the table
        self.departed: dict[int, dict] = {}

    # ------------------------------------------------------------------ settings
    def set_blinds(self, small_blind: int, big_blind: int) -> bool:
        """Change blinds. Returns True if applied now, False if deferred to next hand."""
        if not (0 < small_blind <= big_blind):
            raise GameError("블라인드는 0 < SB ≤ BB 이어야 합니다.")
        if self.in_progress:
            self.pending_blinds = (small_blind, big_blind)
            return False
        self.small_blind, self.big_blind = small_blind, big_blind
        self.pending_blinds = None
        return True

    def rebuy(self, user_id: int, amount: int) -> Player:
        p = self.get_player(user_id)
        if p is None:
            raise GameError("테이블에 앉아 있지 않습니다. 먼저 /join 하세요.")
        if self.in_progress and p.live:
            raise GameError("핸드 진행 중에는 리바인할 수 없습니다. 핸드가 끝난 뒤 다시 시도하세요.")
        if p.stack >= self.starting_stack:
            raise GameError(
                f"칩이 시작 스택({self.starting_stack:,}) 미만일 때만 리바인할 수 있습니다."
            )
        p.stack += amount
        p.buy_in += amount
        p.rebuys += 1
        return p

    # ------------------------------------------------------------------ seating
    def get_player(self, user_id: int) -> Player | None:
        return next((p for p in self.players if p.user_id == user_id), None)

    def add_player(self, user_id: int, name: str) -> Player:
        existing = self.get_player(user_id)
        if existing and not existing.leaving:
            raise GameError("이미 테이블에 앉아 있습니다.")
        if existing and existing.leaving:  # changed their mind before the hand ended
            existing.leaving = False
            return existing
        if len(self.players) >= MAX_PLAYERS:
            raise GameError(f"테이블이 가득 찼습니다. (최대 {MAX_PLAYERS}명)")
        prev = self.departed.pop(user_id, None)
        if prev is not None:  # returning player: restore the chips they left with
            p = Player(user_id, name, prev["stack"], buy_in=prev["buy_in"], rebuys=prev["rebuys"])
            p.returning = True
        else:
            p = Player(user_id, name, self.starting_stack, buy_in=self.starting_stack)
        self.players.append(p)  # joins mid-hand sit out until the next deal
        return p

    def remove_player(self, user_id: int) -> list[Event]:
        """Leave the table. Mid-hand the player folds now and is removed at hand end."""
        p = self.get_player(user_id)
        if p is None:
            raise GameError("테이블에 앉아 있지 않습니다.")
        if not self.in_progress:
            self._remove_index(self.players.index(p))
            return []
        p.leaving = True
        if not p.live:
            return []
        if self.current_player is p:
            return self.act(user_id, "fold")
        p.folded = True
        self.action_seq += 1
        events = [Event("action", p, "fold")]
        # The folding player wasn't on turn; re-check from the current player inclusive.
        self.current = (self.current - 1) % len(self.players)
        return self._advance(events)

    @staticmethod
    def can_be_dealt(p: Player) -> bool:
        return p.stack > 0 and not p.leaving and not p.away

    def set_away(self, user_id: int, away: bool) -> Player:
        """Sit out / come back. Takes effect from the next hand; the current hand is unaffected."""
        p = self.get_player(user_id)
        if p is None:
            raise GameError("테이블에 앉아 있지 않습니다.")
        p.away = away
        p.timeouts = 0
        return p

    def _remove_index(self, i: int) -> None:
        p = self.players[i]
        # Remember the stack so a player who leaves and rejoins gets their chips back.
        self.departed[p.user_id] = {
            "name": p.name,
            "stack": p.stack,
            "buy_in": p.buy_in,
            "rebuys": p.rebuys,
        }
        del self.players[i]
        if i <= self.button:
            self.button -= 1

    # ------------------------------------------------------------------ helpers
    @property
    def current_player(self) -> Player | None:
        if not self.in_progress or self.current is None:
            return None
        return self.players[self.current]

    @property
    def pot_total(self) -> int:
        return sum(p.total_bet for p in self.players)

    def _next_in_hand(self, after: int) -> int:
        n = len(self.players)
        for k in range(1, n + 1):
            j = (after + k) % n
            if self.players[j].in_hand:
                return j
        raise RuntimeError("no players in hand")

    def _order_from_button(self) -> list[Player]:
        n = len(self.players)
        return [self.players[(self.button + k) % n] for k in range(1, n + 1)]

    def _commit(self, p: Player, amount: int) -> int:
        amount = min(amount, p.stack)
        p.stack -= amount
        p.bet += amount
        p.total_bet += amount
        if p.stack == 0:
            p.all_in = True
        return amount

    def _find_next_actor(self, after: int) -> int | None:
        live = [p for p in self.players if p.live]
        can_act = [p for p in live if not p.all_in]
        if not can_act:
            return None
        # Only one player with chips left and nothing to call: no betting possible.
        if len(can_act) == 1 and can_act[0].bet >= self.current_bet:
            return None
        n = len(self.players)
        for k in range(1, n + 1):
            j = (after + k) % n
            p = self.players[j]
            if p.live and not p.all_in and (not p.acted or p.bet < self.current_bet):
                return j
        return None

    # ------------------------------------------------------------------ hand flow
    def start_hand(self) -> list[Event]:
        if self.in_progress:
            raise GameError("이미 핸드가 진행 중입니다.")
        seated = [p for p in self.players if self.can_be_dealt(p)]
        if len(seated) < 2:
            raise GameError(
                "핸드를 시작하려면 칩이 있고 자리에 있는 플레이어가 최소 2명 필요합니다."
            )
        if self.pending_blinds:
            self.small_blind, self.big_blind = self.pending_blinds
            self.pending_blinds = None

        self.hand_no += 1
        self.in_progress = True
        self.deck = Deck(self.rng)
        self.board = []
        self.street = PREFLOP
        for p in self.players:
            p.hole = []
            p.bet = p.total_bet = 0
            p.all_in = p.acted = False
            p.in_hand = self.can_be_dealt(p)
            p.folded = not p.in_hand

        self.button = self._next_in_hand(self.button)
        # Heads-up: the button posts the small blind and acts first preflop.
        sb = self.button if len(seated) == 2 else self._next_in_hand(self.button)
        bb = self._next_in_hand(sb)
        self.sb_index, self.bb_index = sb, bb
        self._commit(self.players[sb], self.small_blind)
        self._commit(self.players[bb], self.big_blind)

        order, i = [], sb
        for _ in range(len(seated)):
            order.append(self.players[i])
            i = self._next_in_hand(i)
        for _ in range(2):
            for p in order:
                p.hole.extend(self.deck.draw())

        self.current_bet = self.big_blind
        self.min_raise = self.big_blind
        self.current = bb
        self.action_seq += 1
        return self._advance([])

    def legal_actions(self) -> LegalActions:
        p = self.current_player
        if p is None:
            raise GameError("진행 중인 차례가 없습니다.")
        to_call = max(0, self.current_bet - p.bet)
        max_to = p.bet + p.stack
        others_can_act = any(q is not p and q.live and not q.all_in for q in self.players)
        # A player who already acted and now faces only an incomplete (short all-in)
        # raise may call or fold but not re-raise; `acted` is only reset by full raises.
        can_raise = p.stack > to_call and not p.acted and others_can_act
        min_to = min(self.current_bet + self.min_raise, max_to) if can_raise else None
        return LegalActions(
            to_call=min(to_call, p.stack),
            can_check=to_call == 0,
            can_raise=can_raise,
            min_raise_to=min_to,
            max_raise_to=max_to,
        )

    def act(self, user_id: int, action: str, amount: int | None = None) -> list[Event]:
        p = self.current_player
        if p is None:
            raise GameError("진행 중인 차례가 없습니다.")
        if p.user_id != user_id:
            raise GameError("당신의 차례가 아닙니다.")
        la = self.legal_actions()
        to_call = self.current_bet - p.bet

        if action == "allin" and not la.can_raise:
            action = "call"  # all-in that can't raise is just a (possibly all-in) call

        if action == "fold":
            p.folded = True
            ev = Event("action", p, "fold")
        elif action == "check" or (action == "call" and to_call <= 0):
            if to_call > 0:
                raise GameError(f"체크할 수 없습니다. 콜({min(to_call, p.stack):,}) 또는 폴드하세요.")
            ev = Event("action", p, "check")
        elif action == "call":
            added = self._commit(p, to_call)
            ev = Event("action", p, "call", added, all_in=p.all_in)
        elif action in ("raise", "bet", "allin"):
            if not la.can_raise:
                raise GameError("지금은 레이즈할 수 없습니다. 콜 또는 폴드만 가능합니다.")
            target = la.max_raise_to if action == "allin" else int(amount or 0)
            if target > la.max_raise_to:
                raise GameError(f"칩이 부족합니다. 최대 {la.max_raise_to:,}까지 가능합니다.")
            if target < la.min_raise_to:
                raise GameError(f"최소 {la.min_raise_to:,}까지 레이즈해야 합니다.")
            was_open = self.current_bet == 0
            increment = target - self.current_bet
            self._commit(p, target - p.bet)
            if increment >= self.min_raise:  # full raise reopens the action
                self.min_raise = increment
                for q in self.players:
                    if q is not p:
                        q.acted = False
            self.current_bet = target
            ev = Event("action", p, "bet" if was_open else "raise", target, all_in=p.all_in)
        else:
            raise GameError("알 수 없는 액션입니다.")

        p.acted = True
        self.action_seq += 1
        return self._advance([ev])

    def _advance(self, events: list[Event]) -> list[Event]:
        live = [p for p in self.players if p.live]
        if len(live) == 1:
            events.append(Event("result", result=self._award_uncontested(live[0])))
            return events
        nxt = self._find_next_actor(self.current)
        if nxt is not None:
            self.current = nxt
            return events
        return self._next_street(events)

    def _next_street(self, events: list[Event]) -> list[Event]:
        while True:
            for p in self.players:
                p.bet = 0
                p.acted = False
            self.current_bet = 0
            self.min_raise = self.big_blind
            if self.street == RIVER:
                events.append(Event("result", result=self._showdown()))
                return events
            self.deck.draw()  # burn
            if self.street == PREFLOP:
                self.street, new = FLOP, self.deck.draw(3)
            elif self.street == FLOP:
                self.street, new = TURN, self.deck.draw(1)
            else:
                self.street, new = RIVER, self.deck.draw(1)
            self.board.extend(new)
            events.append(Event("street", street=self.street, cards=list(new)))
            nxt = self._find_next_actor(self.button)
            if nxt is not None:
                self.current = nxt
                return events
            # Nobody can bet (all-in run-out): keep dealing.

    # ------------------------------------------------------------------ payouts
    def build_pots(self) -> list[Pot]:
        levels = sorted({p.total_bet for p in self.players if p.live and p.total_bet > 0})
        pots: list[Pot] = []
        prev = 0
        for lvl in levels:
            amount = sum(max(0, min(p.total_bet, lvl) - prev) for p in self.players)
            eligible = [p for p in self.players if p.live and p.total_bet >= lvl]
            if amount:
                if pots and set(map(id, pots[-1].eligible)) == set(map(id, eligible)):
                    pots[-1].amount += amount
                else:
                    pots.append(Pot(amount, eligible))
            prev = lvl
        # Chips from folded players above the highest live level go to the last pot.
        leftover = sum(max(0, p.total_bet - prev) for p in self.players)
        if leftover:
            pots[-1].amount += leftover
        return pots

    def _showdown(self) -> HandResult:
        order = self._order_from_button()  # odd chips go to first winner left of button
        evals = {p.user_id: best_hand(p.hole + self.board) for p in order if p.live}
        payouts: dict[int, int] = {}
        pots = self.build_pots()
        for pot in pots:
            best = max(evals[p.user_id][0] for p in pot.eligible)
            winners = [p for p in order if p in pot.eligible and evals[p.user_id][0] == best]
            share, rem = divmod(pot.amount, len(winners))
            for i, w in enumerate(winners):
                won = share + (1 if i < rem else 0)
                w.stack += won
                payouts[w.user_id] = payouts.get(w.user_id, 0) + won
            pot.winners = winners
            pot.hand_label = hand_name(best)
        showdown = [
            ShowdownEntry(p, list(p.hole), hand_name(evals[p.user_id][0]), evals[p.user_id][1])
            for p in order
            if p.live
        ]
        return self._finish(
            HandResult(self.hand_no, list(self.board), False, pots, showdown, payouts)
        )

    def _award_uncontested(self, winner: Player) -> HandResult:
        total = self.pot_total
        winner.stack += total
        pot = Pot(total, [winner], [winner])
        return self._finish(
            HandResult(self.hand_no, list(self.board), True, [pot], [], {winner.user_id: total})
        )

    def _finish(self, result: HandResult) -> HandResult:
        self.in_progress = False
        self.current = None
        for p in self.players:
            p.bet = 0
        result.holes = {p.user_id: list(p.hole) for p in self.players if p.in_hand}
        # Run the rest of the board out (with burns) so it can be revealed on request.
        full = list(self.board)
        while len(full) < 5 and self.deck is not None:
            self.deck.draw()  # burn
            full.extend(self.deck.draw(3 if not full else 1))
        result.full_board = full
        for i in reversed(range(len(self.players))):
            p = self.players[i]
            if p.leaving:
                result.removed.insert(0, p)
                self._remove_index(i)
            elif p.stack == 0:
                result.busted.insert(0, p)
        self.last_result = result
        return result
