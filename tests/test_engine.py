import random

import pytest

from holdem import game as game_mod
from holdem.cards import Card, parse_cards
from holdem.evaluator import best_hand, hand_name
from holdem.game import FLOP, PREFLOP, GameError, Table


# ---------------------------------------------------------------- evaluator
def name_of(text):
    return hand_name(best_hand(parse_cards(text))[0])


@pytest.mark.parametrize(
    "cards,expected",
    [
        ("As Ks Qs Js Ts 2d 3c", "로열 플러시"),
        ("9h 8h 7h 6h 5h Ad Ac", "스트레이트 플러시"),
        ("Ah 2h 3h 4h 5h Kd Kc", "스트레이트 플러시"),
        ("9c 9d 9h 9s 2c 3d 4h", "포카드"),
        ("Kc Kd Kh 2s 2c 3d 4h", "풀하우스"),
        ("2h 7h 9h Jh Kh Ac Ad", "플러시"),
        ("Ac 2d 3h 4s 5c Kd Kh", "스트레이트"),
        ("Tc Jd Qh Ks Ac 2d 2h", "스트레이트"),
        ("7c 7d 7h 2s 9c Jd Kh", "트리플"),
        ("7c 7d 9h 9s 2c Jd Kh", "투페어"),
        ("7c 7d 9h Ts 2c Jd Kh", "원페어"),
        ("7c 3d 9h Ts 2c Jd Kh", "하이카드"),
    ],
)
def test_hand_categories(cards, expected):
    assert name_of(cards) == expected


def test_kickers_and_wheel_ordering():
    score = lambda t: best_hand(parse_cards(t))[0]
    assert score("Ac Ad Kh 7s 2c") > score("Ac Ad Qh Js Tc")  # kicker
    assert score("2c 3d 4h 5s 6c") > score("Ac 2d 3h 4s 5c")  # 6-high beats wheel
    assert score("Kc Kd 2h 2s Ac") > score("Qc Qd Jh Js Ac")  # higher top pair
    assert score("Ac Ad Ah Ks Kc") == score("Ad Ac As Kh Kd")  # identical strength


# ---------------------------------------------------------------- table helpers
class StackedDeck:
    """Deals cards in a fixed order: hole cards (round-robin from SB), then burn/board."""

    def __init__(self, cards):
        self.cards = list(cards)

    def draw(self, n=1):
        drawn, self.cards = self.cards[:n], self.cards[n:]
        return drawn


def rig(monkeypatch, holes, board):
    """holes: list per player in deal order starting from SB (2 cards each)."""
    first = [h[0] for h in holes]
    second = [h[1] for h in holes]
    b = parse_cards(board)
    filler = Card(2, "c")
    seq = [Card.parse(c) for c in first + second]
    seq += [filler, *b[:3], filler, b[3], filler, b[4]]
    monkeypatch.setattr(game_mod, "Deck", lambda rng=None: StackedDeck(seq))


def make_table(n, stacks=None, **kw):
    t = Table(chat_id=1, rng=random.Random(0), **kw)
    for i in range(n):
        p = t.add_player(100 + i, f"P{i}")
        if stacks:
            p.stack = stacks[i]
    return t


def uid(t):
    return t.current_player.user_id


# ---------------------------------------------------------------- tests
def test_heads_up_blinds_and_order():
    t = make_table(2)
    t.start_hand()
    btn = t.players[t.button]
    assert t.sb_index == t.button  # heads-up: button is SB
    assert btn.bet == 50 and t.players[t.bb_index].bet == 100
    assert t.current_player is btn  # SB acts first preflop
    t.act(uid(t), "call")
    assert t.current_player is t.players[t.bb_index]  # BB gets the option
    t.act(uid(t), "check")
    assert t.street == FLOP
    assert t.current_player is t.players[t.bb_index]  # BB acts first postflop


def test_three_handed_order_and_fold_win():
    t = make_table(3)
    t.start_hand()
    # button=0, sb=1, bb=2 -> UTG is button (index 0)
    assert (t.button, t.sb_index, t.bb_index) == (0, 1, 2)
    assert t.current == 0
    t.act(100, "raise", 300)
    t.act(101, "fold")
    events = t.act(102, "fold")
    res = events[-1].result
    assert res.uncontested and res.payouts == {100: 450}
    assert [p.stack for p in t.players] == [10_150, 9_950, 9_900]
    assert sum(p.stack for p in t.players) == 30_000
    assert not t.in_progress


def test_button_moves():
    t = make_table(3)
    t.start_hand()
    while t.in_progress:
        t.act(uid(t), "fold")
    t.start_hand()
    assert t.button == 1


def test_min_raise_validation():
    t = make_table(3)
    t.start_hand()
    with pytest.raises(GameError):
        t.act(100, "raise", 150)  # min is 200
    with pytest.raises(GameError):
        t.act(101, "call")  # not your turn
    t.act(100, "raise", 300)  # raise by 200
    with pytest.raises(GameError):
        t.act(101, "raise", 400)  # must raise to >= 500
    t.act(101, "raise", 500)
    assert t.min_raise == 200 and t.current_bet == 500


def test_bb_option_preflop():
    t = make_table(3)
    t.start_hand()
    t.act(100, "call")
    t.act(101, "call")
    assert t.current == 2 and t.street == PREFLOP  # BB still to act
    la = t.legal_actions()
    assert la.can_check and la.can_raise
    t.act(102, "raise", 300)
    assert t.current == 0  # action reopened


def test_side_pots_showdown(monkeypatch):
    # Deal order from SB: P1 (sb), P2 (bb), P0 (button)
    # P1 short stack has the best hand, P2 second best, P0 worst.
    rig(
        monkeypatch,
        holes=[["Ac", "Ad"], ["Kc", "Kd"], ["7h", "2s"]],
        board="As Kh 9c 4d 3s",
    )
    t = make_table(3, stacks=[10_000, 1_000, 3_000])
    t.start_hand()
    t.act(100, "allin")  # P0 10,000
    t.act(101, "call")  # P1 all-in 1,000
    t.act(102, "call")  # P2 all-in 3,000
    assert not t.in_progress
    res = t.last_result
    amounts = [p.amount for p in res.pots]
    assert amounts == [3_000, 4_000, 7_000]  # main, side, uncalled return
    assert res.pots[0].winners[0].user_id == 101
    assert res.pots[1].winners[0].user_id == 102
    assert res.pots[2].winners[0].user_id == 100
    assert len(res.board) == 5
    stacks = {p.user_id: p.stack for p in t.players}
    assert stacks == {100: 7_000, 101: 3_000, 102: 4_000}


def test_split_pot(monkeypatch):
    rig(monkeypatch, holes=[["2c", "3d"], ["2h", "3s"]], board="Ac Kd Qh Js Th")
    t = make_table(2)
    t.start_hand()
    t.act(uid(t), "call")
    t.act(uid(t), "check")
    for _ in range(3):
        t.act(uid(t), "check")
        t.act(uid(t), "check")
    res = t.last_result
    assert len(res.pots[0].winners) == 2
    assert sorted(p.stack for p in t.players) == [10_000, 10_000]


def test_short_allin_does_not_reopen(monkeypatch):
    t = make_table(3, stacks=[10_000, 10_000, 450])
    t.start_hand()
    # button 0 acts first: raise to 300
    t.act(100, "raise", 300)
    t.act(101, "call")
    t.act(102, "allin")  # BB to 450: +150 < min raise 200 -> incomplete
    assert t.current_bet == 450 and t.min_raise == 200
    la = t.legal_actions()  # P0 already acted -> call/fold only
    assert t.current == 0 and not la.can_raise and la.to_call == 150
    with pytest.raises(GameError):
        t.act(100, "raise", 1_000)
    t.act(100, "call")
    t.act(101, "call")
    assert t.street == FLOP


def test_busted_player_removed(monkeypatch):
    rig(monkeypatch, holes=[["Ac", "Ad"], ["7h", "2s"]], board="As Kh 9c 4d 3s")
    t = make_table(2, stacks=[500, 10_000])
    t.start_hand()  # button = P0 = SB
    t.act(100, "allin")
    t.act(101, "call")
    res = t.last_result
    assert not res.removed
    t2 = make_table(2, stacks=[500, 10_000])
    rig(monkeypatch, holes=[["7h", "2s"], ["Ac", "Ad"]], board="As Kh 9c 4d 3s")
    t2.start_hand()
    t2.act(100, "allin")
    t2.act(101, "call")
    # busted players stay seated (so they can /rebuy) but can't be dealt in
    assert [p.user_id for p in t2.last_result.busted] == [100]
    assert [p.user_id for p in t2.players] == [100, 101]
    with pytest.raises(GameError):
        t2.start_hand()
    t2.rebuy(100, 30_000)
    t2.start_hand()
    assert t2.get_player(100).in_hand


def test_away_player_not_dealt():
    t = make_table(3)
    t.set_away(100, True)
    t.start_hand()
    away = t.get_player(100)
    assert not away.in_hand and away.hole == [] and away.total_bet == 0
    assert t.players[t.button] is not away  # heads-up between the other two
    assert t.sb_index == t.button
    while t.in_progress:
        t.act(uid(t), "fold")
    assert away.stack == 10_000
    t.set_away(101, True)
    with pytest.raises(GameError):
        t.start_hand()  # only one player present
    t.set_away(100, False)
    t.start_hand()
    assert away.in_hand


def test_rebuy_rules():
    t = make_table(3)
    with pytest.raises(GameError):
        t.rebuy(100, 30_000)  # stack not below starting stack
    t.get_player(100).stack = 400
    p = t.rebuy(100, 30_000)
    assert (p.stack, p.buy_in, p.rebuys) == (30_400, 40_000, 1)
    t.get_player(101).stack = 400
    t.start_hand()
    with pytest.raises(GameError):
        t.rebuy(101, 30_000)  # live in the hand
    with pytest.raises(GameError):
        t.rebuy(999, 30_000)  # not seated


def test_blinds_deferred_until_next_hand():
    t = make_table(3)
    assert t.set_blinds(200, 400) is True
    t.start_hand()
    assert (t.players[t.sb_index].bet, t.players[t.bb_index].bet) == (200, 400)
    assert t.set_blinds(500, 1000) is False
    assert t.big_blind == 400  # unchanged mid-hand
    while t.in_progress:
        t.act(uid(t), "fold")
    t.start_hand()
    assert t.players[t.bb_index].bet == 1000
    with pytest.raises(GameError):
        t.set_blinds(300, 100)


def test_last_hand_record_and_full_board():
    t = make_table(3)
    t.start_hand()
    holes = {p.user_id: list(p.hole) for p in t.players}
    t.act(100, "fold")
    t.act(101, "fold")  # ends preflop, no board dealt
    r = t.last_result
    assert r.board == [] and len(r.full_board) == 5
    assert r.holes == holes
    dealt = {c for h in holes.values() for c in h}
    assert len(set(r.full_board)) == 5 and not dealt & set(r.full_board)
    t.start_hand()
    assert t.last_result is r  # previous hand stays available during the next one


def test_leave_out_of_turn_ends_hand():
    t = make_table(3)
    t.start_hand()
    t.act(100, "fold")
    events = t.remove_player(102)  # BB leaves while SB is to act
    assert events[-1].kind == "result"
    assert events[-1].result.payouts == {101: 150}
    assert [p.user_id for p in t.players] == [100, 101]


def test_leave_out_of_turn_continues():
    t = make_table(4)
    t.start_hand()
    assert t.current == 3
    t.remove_player(100)  # button folds/leaves out of turn
    assert t.current == 3 and t.in_progress
    t.act(103, "call")
    t.act(101, "call")
    t.act(102, "check")
    assert t.street == FLOP


def test_chip_conservation_random_play():
    rng = random.Random(42)
    for _ in range(300):
        t = make_table(rng.randint(2, 6), stacks=None)
        for p in t.players:
            p.stack = rng.randint(150, 3000)
        total = sum(p.stack for p in t.players)
        for _hand in range(5):
            if sum(1 for p in t.players if p.stack > 0) < 2:
                break
            t.start_hand()
            guard = 0
            while t.in_progress:
                guard += 1
                assert guard < 200
                la = t.legal_actions()
                r = rng.random()
                if r < 0.15:
                    t.act(uid(t), "fold")
                elif r < 0.3 and la.can_raise:
                    t.act(uid(t), "allin")
                elif r < 0.5 and la.can_raise:
                    target = rng.randint(la.min_raise_to, la.max_raise_to)
                    t.act(uid(t), "raise", target)
                else:
                    t.act(uid(t), "call")
            assert sum(p.stack for p in t.players) + sum(
                p.stack for p in t.last_result.removed
            ) == total
            total = sum(p.stack for p in t.players)
