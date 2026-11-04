"""Drives bot.py handlers with fake Telegram objects — no network or token needed."""
import asyncio
import itertools
from types import SimpleNamespace

import pytest

import bot
from telegram.error import Forbidden

GROUP_ID = -1001


class FakeBot:
    username = "holdem_dealer_bot"

    def __init__(self, blocked=()):
        self.sent = []  # (chat_id, text, reply_markup)
        self.blocked = set(blocked)
        self._ids = itertools.count(1)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked:
            raise Forbidden("bot can't initiate conversation with a user")
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=next(self._ids))

    async def edit_message_reply_markup(self, *a, **kw):
        return True

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, data=None, name=None):
        job = SimpleNamespace(callback=cb, data=data, name=name, removed=False)
        job.schedule_removal = lambda j=job: setattr(j, "removed", True)
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def pop(self, prefix):
        for j in self.jobs:
            if j.name.startswith(prefix) and not j.removed:
                j.removed = True
                return j
        return None


def user(uid):
    return SimpleNamespace(id=uid, full_name=f"User{uid}", username=None)


class Harness:
    def __init__(self, blocked=()):
        self.bot = FakeBot(blocked)
        self.jq = FakeJobQueue()
        self.replies = []
        self.edited_markups = 0

    def ctx(self, args=(), job=None):
        return SimpleNamespace(bot=self.bot, job_queue=self.jq, args=list(args), job=job)

    def update(self, uid, chat_id=GROUP_ID, text=""):
        async def reply_text(text, **kw):
            self.replies.append(text)

        kind = "private" if chat_id > 0 else "supergroup"
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, title="테스트방", type=kind),
            effective_user=user(uid),
            effective_message=SimpleNamespace(reply_text=reply_text, text=text),
        )

    async def cmd(self, fn, uid, *args, chat_id=GROUP_ID):
        await fn(self.update(uid, chat_id), self.ctx(args))

    async def say(self, uid, text, chat_id=GROUP_ID):
        """Send a plain text message; routes through the Korean-shortcut regex like PTB would."""
        import re

        if re.match(bot.KOREAN_RE, text):
            await bot.on_korean_shortcut(self.update(uid, chat_id, text), self.ctx())
            return True
        return False

    async def press(self, uid, data):
        answers = []

        async def answer(text=None, show_alert=False):
            answers.append(text)

        async def edit_message_reply_markup(reply_markup=None):
            self.edited_markups += 1

        q = SimpleNamespace(
            data=data,
            from_user=user(uid),
            message=SimpleNamespace(chat=SimpleNamespace(id=GROUP_ID)),
            answer=answer,
            edit_message_reply_markup=edit_message_reply_markup,
        )
        await bot.on_callback(SimpleNamespace(callback_query=q), self.ctx())
        return answers

    def last_keyboard(self):
        for chat_id, text, kb in reversed(self.bot.sent):
            if chat_id == GROUP_ID and kb is not None:
                return kb
        return None

    async def fire(self, prefix):
        job = self.jq.pop(prefix)
        assert job, f"no job {prefix}"
        await job.callback(self.ctx(job=job))


def button(kb, startswith):
    for row in kb.inline_keyboard:
        for b in row:
            if b.text.startswith(startswith):
                return b.callback_data
    raise AssertionError(f"no button {startswith}: {[b.text for r in kb.inline_keyboard for b in r]}")


OWNER = 1  # user 1 is the bot owner in every test


@pytest.fixture(autouse=True)
def clean_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "OWNER_IDS", {OWNER})
    monkeypatch.setattr(bot, "TABLES_FILE", str(tmp_path / "tables.json"))
    bot.SESSIONS.clear()
    yield
    bot.SESSIONS.clear()


def run(coro):
    return asyncio.run(coro)


def test_full_game_flow():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, 1)
        for uid in (1, 2, 3):
            await h.cmd(bot.cmd_join, uid)
        t = bot.SESSIONS[GROUP_ID].table
        assert len(t.players) == 3

        await h.cmd(bot.cmd_startgame, 1)
        assert t.in_progress
        dms = [c for c, *_ in h.bot.sent if c > 0 and "내 패" in _[0]]
        assert sorted(dms) == [1, 2, 3]  # each player got their hole cards privately
        group_texts = [txt for c, txt, _ in h.bot.sent if c == GROUP_ID]
        for p in t.players:  # hole cards never posted in the group before showdown
            assert not any(f"{p.hole[0]} {p.hole[1]}" in g for g in group_texts)

        # wrong player presses a button
        kb = h.last_keyboard()
        cur = t.current_player.user_id
        other = next(p.user_id for p in t.players if p.user_id != cur)
        ans = await h.press(other, button(kb, "📞 콜"))
        assert ans == ["당신의 차례가 아닙니다."]

        # peek works for anyone in the hand
        ans = await h.press(other, f"c:{t.hand_no}")
        assert ans[0].startswith("내 패:")

        # everybody calls/checks down via buttons
        guard = 0
        while t.in_progress:
            guard += 1
            assert guard < 30
            kb = h.last_keyboard()
            uid = t.current_player.user_id
            # every turn message opens with whose turn it is
            turn_msg = next(txt for c, txt, k in reversed(h.bot.sent) if k is not None)
            assert turn_msg.startswith(f'👉 <a href="tg://user?id={uid}">')
            label = "✔️ 체크" if t.legal_actions().can_check else "📞 콜"
            await h.press(uid, button(kb, label))

        last = h.bot.sent[-1][1]
        assert "결과" in last and "다음 핸드: 아래 버튼 또는 /ㄱㄱ" in last
        assert sum(p.stack for p in t.players) == 3 * bot.DEFAULT_STACK

        # stale button from the finished hand
        ans = await h.press(uid, button(kb, label))
        assert ans == ["이미 지나간 차례입니다."]

        # no auto-start: only a turn timer may ever be scheduled, never a next-hand job
        assert not t.in_progress
        assert all(j.name.startswith("turn:") for j in h.jq.jobs)
        await h.say(2, "/ㄱㄱ")
        assert t.in_progress and t.hand_no == 2
        await h.say(2, "/ㄱㄱ")  # can't start a second hand on top of a running one
        assert h.replies[-1] == "이미 핸드가 진행 중입니다." and t.hand_no == 2

        # raise via text command, then timeout auto-folds
        uid = t.current_player.user_id
        await h.cmd(bot.make_action_cmd("raise"), uid, "1500")
        assert t.current_bet == 1500
        await h.fire("turn:")
        assert any("시간 초과" in txt for _, txt, _ in h.bot.sent[-2:])

        # finish the hand and close
        while t.in_progress:
            await h.cmd(bot.make_action_cmd("fold"), t.current_player.user_id)
        assert "다음 핸드:" in h.bot.sent[-1][1] and not t.in_progress
        await h.cmd(bot.cmd_closetable, 1)
        assert GROUP_ID not in bot.SESSIONS

    run(scenario())


def test_join_requires_dm():
    async def scenario():
        h = Harness(blocked={5})
        await h.cmd(bot.cmd_newtable, 1)
        await h.cmd(bot.cmd_join, 5)
        assert not bot.SESSIONS[GROUP_ID].table.players
        assert "1:1 대화" in h.replies[-1]

    run(scenario())


def test_korean_shortcuts():
    async def scenario():
        h = Harness()
        assert await h.say(2, "/ㄹㄷ")  # no table yet: /join doesn't create one any more
        assert GROUP_ID not in bot.SESSIONS and "/newtable" in h.replies[-1]
        await h.cmd(bot.cmd_newtable, OWNER)
        assert await h.say(1, "/ㄹㄷ")
        assert await h.say(2, "/ㄹㄷ@holdem_dealer_bot")
        t = bot.SESSIONS[GROUP_ID].table
        assert [p.user_id for p in t.players] == [1, 2]
        assert not await h.say(3, "/ㄹㄷㄹㄷ")  # not a shortcut
        assert not await h.say(3, "ㄹㄷ")  # needs the slash

        await h.say(2, "/쉬야")
        assert [p.user_id for p in t.players] == [1]
        await h.say(2, "/ㄹㄷ")
        await h.say(3, "/ㄹㄷ")
        await h.say(3, "/ㅂㅂ")
        assert [p.user_id for p in t.players] == [1, 2]

        await h.say(1, "/ㄱㄱ")
        assert t.in_progress

        await h.say(1, "/ㅎㅇ")  # in a group: link button to the DM
        assert "1:1 대화" in h.replies[-1]
        await h.say(1, "/ㅎㅇ", chat_id=1)  # in a DM: acts as /start
        assert "홀덤 딜러 봇" in h.replies[-1]

    run(scenario())


def test_settings_and_records():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        await h.cmd(bot.cmd_join, 1)
        await h.cmd(bot.cmd_join, 2)
        s = bot.SESSIONS[GROUP_ID]
        t = s.table

        await h.cmd(bot.cmd_timer, 1, "3")
        assert "5~120" in h.replies[-1] and s.turn_timeout == bot.TURN_TIMEOUT
        await h.cmd(bot.cmd_timer, 1, "30")
        assert s.turn_timeout == 30
        await h.cmd(bot.cmd_blinds, 1, "200", "400")
        assert (t.small_blind, t.big_blind) == (200, 400)
        await h.cmd(bot.cmd_blinds, 9, "1", "2")  # outsider, not admin
        assert t.big_blind == 400

        await h.cmd(bot.cmd_hand, 1)
        assert "기록이 없습니다" in h.replies[-1]

        await h.cmd(bot.cmd_startgame, 1)
        assert "⏱ 30초" in h.bot.sent[-1][1]
        holes = {p.user_id: list(p.hole) for p in t.players}
        await h.cmd(bot.make_action_cmd("fold"), t.current_player.user_id)  # preflop fold
        assert not t.in_progress

        await h.cmd(bot.cmd_hand, 1)  # posted publicly in the group, not DM'd
        chat_id, text, _ = h.bot.sent[-1]
        assert chat_id == GROUP_ID and "패 공개" in text
        assert f"{holes[1][0]} {holes[1][1]}" in text
        assert f"{holes[2][0]} {holes[2][1]}" not in text  # only the requester's cards

        await h.cmd(bot.cmd_hand, 2, chat_id=2)  # in DM
        assert str(holes[2][0]) in h.replies[-1]

        await h.cmd(bot.cmd_open, 1)
        out = h.bot.sent[-1][1]
        assert "전체 보드" in out and "미공개" in out
        assert all(str(c) in out for c in t.last_result.full_board)

        await h.cmd(bot.cmd_players, 1)
        assert "참가자 2명" in h.bot.sent[-1][1]

        winner = max(t.players, key=lambda p: p.stack)
        loser = min(t.players, key=lambda p: p.stack)  # lost the small blind
        await h.cmd(bot.cmd_rebuy, winner.user_id)
        assert "미만일 때만" in h.replies[-1] and winner.rebuys == 0
        await h.cmd(bot.cmd_rebuy, loser.user_id)
        assert loser.stack > bot.REBUY_AMOUNT and loser.rebuys == 1

    run(scenario())


async def finish_hand(h, t):
    while t.in_progress:
        await h.cmd(bot.make_action_cmd("fold"), t.current_player.user_id)


async def wait_for_turn(h, t, uid):
    """Others call/check until it's uid's turn (calling keeps the hand alive)."""
    while t.current_player.user_id != uid:
        await h.cmd(bot.make_action_cmd("call"), t.current_player.user_id)


def test_auto_away_after_two_timeouts():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        for uid in (1, 2, 3, 4):
            await h.cmd(bot.cmd_join, uid)
        t = bot.SESSIONS[GROUP_ID].table
        afk = t.get_player(1)

        for hand in (1, 2):
            await h.say(2, "/ㄱㄱ")
            if afk.in_hand:
                await wait_for_turn(h, t, 1)
                await h.fire("turn:")
            await finish_hand(h, t)
        assert afk.timeouts == 0 and afk.away  # 2nd timeout → away (counter reset)
        texts = [txt for c, txt, _ in h.bot.sent if c == GROUP_ID]
        assert any("연속 2회 시간 초과" in x and "자리비움" in x for x in texts)
        assert "💤 자리비움: User1" in texts[-1]

        await h.say(2, "/ㄱㄱ")
        assert not afk.in_hand and t.in_progress  # sat out: not dealt, no blinds
        assert afk.hole == []
        stack_before = afk.stack
        await finish_hand(h, t)
        assert afk.stack == stack_before

        await h.cmd(bot.cmd_players, 2)
        assert "💤 자리비움" in h.bot.sent[-1][1]

        await h.cmd(bot.cmd_back, 1)
        assert not afk.away and "복귀" in h.bot.sent[-1][1]
        await h.cmd(bot.cmd_back, 1)
        assert h.replies[-1] == "자리비움 상태가 아닙니다."
        await h.say(2, "/ㄱㄱ")
        assert afk.in_hand

    run(scenario())


def test_real_action_resets_timeout_counter():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        for uid in (1, 2, 3):
            await h.cmd(bot.cmd_join, uid)
        t = bot.SESSIONS[GROUP_ID].table
        p = t.get_player(1)
        p.timeouts = 1  # one strike already
        await h.say(2, "/ㄱㄱ")
        await wait_for_turn(h, t, 1)
        label = "✔️ 체크" if t.legal_actions().can_check else "📞 콜"
        await h.press(1, button(h.last_keyboard(), label))
        assert p.timeouts == 0 and not p.away

    run(scenario())


def test_away_join_returns_and_too_few_players():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        await h.cmd(bot.cmd_join, 1)
        await h.cmd(bot.cmd_join, 2)
        t = bot.SESSIONS[GROUP_ID].table
        t.set_away(1, True)
        await h.say(2, "/ㄱㄱ")
        assert not t.in_progress and "최소 2명" in h.bot.sent[-1][1]
        await h.say(1, "/ㄹㄷ")  # /join while away = /back
        assert not t.get_player(1).away and len(t.players) == 2
        await h.say(2, "/ㄱㄱ")
        assert t.in_progress

    run(scenario())


def test_leave_on_turn_ends_hand():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, 1)
        await h.cmd(bot.cmd_join, 1)
        await h.cmd(bot.cmd_join, 2)
        await h.cmd(bot.cmd_startgame, 1)
        t = bot.SESSIONS[GROUP_ID].table
        leaver = t.current_player.user_id
        await h.cmd(bot.cmd_leave, leaver)
        assert not t.in_progress
        text = h.bot.sent[-1][1]
        assert "떠납니다" in text and "결과" in text and "퇴장" in text
        assert [p.user_id for p in t.players] != [] and t.get_player(leaver) is None
        assert "플레이어가 부족" in text

    run(scenario())


# ---------------------------------------------------------------- owner & persistence
import json

import storage


def saved():
    with open(bot.TABLES_FILE, encoding="utf-8") as f:
        return json.load(f)["tables"]


def test_only_owner_manages_tables():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, 2)
        assert GROUP_ID not in bot.SESSIONS and "봇 관리자만" in h.replies[-1]
        await h.cmd(bot.cmd_loadtable, 2)
        assert "봇 관리자만" in h.replies[-1]
        await h.cmd(bot.cmd_newtable, OWNER)
        await h.cmd(bot.cmd_closetable, 2)
        assert GROUP_ID in bot.SESSIONS and "봇 관리자만" in h.replies[-1]
        await h.cmd(bot.cmd_closetable, OWNER)
        assert GROUP_ID not in bot.SESSIONS

    run(scenario())


def test_no_owner_configured(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bot, "OWNER_IDS", set())
        h = Harness()
        await h.cmd(bot.cmd_newtable, 1)
        assert GROUP_ID not in bot.SESSIONS and "/myid" in h.replies[-1]
        await h.cmd(bot.cmd_myid, 1, chat_id=1)
        assert "<code>1</code>" in h.replies[-1]

    run(scenario())


def test_save_and_load_after_restart():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        for uid in (1, 2, 3):
            await h.cmd(bot.cmd_join, uid)
        await h.cmd(bot.cmd_blinds, 1, "300", "600")
        await h.cmd(bot.cmd_timer, 1, "20")
        t = bot.SESSIONS[GROUP_ID].table

        await h.say(1, "/ㄱㄱ")  # hand 1: play it out
        await finish_hand(h, t)
        after_hand1 = {p.user_id: p.stack for p in t.players}
        assert sum(after_hand1.values()) == 3 * bot.DEFAULT_STACK
        assert {d["user_id"]: d["stack"] for d in saved()[str(GROUP_ID)]["players"]} == after_hand1

        t.get_player(3).away = True
        await h.say(1, "/ㄱㄱ")  # hand 2: chips go in, then the bot "crashes" mid-hand
        uid = t.current_player.user_id
        await h.cmd(bot.make_action_cmd("raise"), uid, "5000")
        assert t.in_progress and t.pot_total > 0
        # Shutdown hook saves; the running hand is cancelled and bets refunded.
        await bot.post_shutdown(None)
        bot.SESSIONS.clear()  # restart: memory is gone

        await h.cmd(bot.cmd_join, 2)
        assert "/loadtable" in h.replies[-1]  # nothing open until the owner loads
        await h.cmd(bot.cmd_loadtable, OWNER)
        s = bot.SESSIONS[GROUP_ID]
        t2 = s.table
        assert {p.user_id: p.stack for p in t2.players} == after_hand1
        assert (t2.small_blind, t2.big_blind, s.turn_timeout) == (300, 600, 20)
        assert t2.get_player(3).away and not t2.in_progress
        assert "불러오기 완료" in h.bot.sent[-1][1]

        await h.cmd(bot.cmd_loadtable, OWNER)
        assert "이미" in h.replies[-1]
        await h.say(1, "/ㄱㄱ")  # carries on with restored stacks
        assert t2.in_progress and t2.players[t2.bb_index].bet == 600

    run(scenario())


def test_newtable_protects_saved_table():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        await h.cmd(bot.cmd_join, 1)
        bot.SESSIONS.clear()  # restart
        await h.cmd(bot.cmd_newtable, OWNER)
        assert GROUP_ID not in bot.SESSIONS and "/loadtable" in h.replies[-1]
        await h.cmd(bot.cmd_newtable, OWNER, "new", "50000", "500", "1000")
        t = bot.SESSIONS[GROUP_ID].table
        assert (t.starting_stack, t.big_blind, t.players) == (50_000, 1000, [])
        assert saved()[str(GROUP_ID)]["players"] == []

    run(scenario())


def test_close_deletes_save_but_keeps_other_groups():
    async def scenario():
        h = Harness()
        other = storage.snapshot(storage.restore(-999, {
            "starting_stack": 1000, "small_blind": 5, "big_blind": 10,
            "players": [{"user_id": 7, "name": "X", "stack": 1234}],
        }), "다른방", OWNER, 60)
        storage.save(bot.TABLES_FILE, {-999: other})
        await h.cmd(bot.cmd_newtable, OWNER)
        await h.cmd(bot.cmd_join, 1)
        assert set(saved()) == {"-999", str(GROUP_ID)}
        await h.cmd(bot.cmd_closetable, OWNER)
        assert set(saved()) == {"-999"}  # the unloaded table is untouched
        assert saved()["-999"]["players"][0]["stack"] == 1234

    run(scenario())


def test_next_hand_button():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        for uid in (1, 2):
            await h.cmd(bot.cmd_join, uid)
        t = bot.SESSIONS[GROUP_ID].table
        await h.say(1, "/ㄱㄱ")
        await finish_hand(h, t)
        _, text, kb = h.bot.sent[-1]
        assert "결과" in text
        data = button(kb, "▶️ 다음 핸드 시작")
        assert data == "b:1"

        await h.press(2, data)  # any seated player can start the next hand
        assert t.in_progress and t.hand_no == 2 and h.edited_markups == 1
        ans = await h.press(1, data)  # double click / old message
        assert ans == ["이미 다음 핸드가 시작되었습니다."] and t.hand_no == 2

        await finish_hand(h, t)
        t.set_away(2, True)
        await h.say(1, "/ㄱㄱ")  # only 1 player present → no hand, no button
        await h.cmd(bot.cmd_back, 2)
        await h.say(1, "/ㄱㄱ")
        await finish_hand(h, t)
        await h.say(1, "/ㄱㄱ")  # started by text: the result's button goes stale
        ans = await h.press(2, f"b:{t.hand_no - 1}")
        assert ans == ["이미 다음 핸드가 시작되었습니다."]

    run(scenario())


def test_no_button_when_not_enough_players():
    async def scenario():
        h = Harness()
        await h.cmd(bot.cmd_newtable, OWNER)
        for uid in (1, 2):
            await h.cmd(bot.cmd_join, uid)
        t = bot.SESSIONS[GROUP_ID].table
        await h.say(1, "/ㄱㄱ")
        await h.cmd(bot.cmd_leave, t.current_player.user_id)  # leaving ends the hand
        _, text, kb = h.bot.sent[-1]
        assert "결과" in text and "부족" in text and kb is None

    run(scenario())
