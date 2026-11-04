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

        q = SimpleNamespace(
            data=data,
            from_user=user(uid),
            message=SimpleNamespace(chat=SimpleNamespace(id=GROUP_ID)),
            answer=answer,
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


@pytest.fixture(autouse=True)
def clean_sessions():
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
        assert "결과" in last and "다음 핸드 시작: /ㄱㄱ" in last
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
        assert "다음 핸드 시작" in h.bot.sent[-1][1] and not t.in_progress
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
        assert await h.say(1, "/ㄹㄷ")  # auto-creates the table
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

        await h.cmd(bot.cmd_hand, 1)
        assert h.replies[-1].startswith("📩")
        dm = h.bot.sent[-1]
        assert dm[0] == 1 and "전판" in dm[1] and str(holes[1][0]) in dm[1]

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
