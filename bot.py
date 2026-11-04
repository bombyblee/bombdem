"""Telegram Texas Hold'em dealer bot.

Group chat  : table management, betting buttons, public board/results.
Private chat: each player's hole cards are DM'd at the start of every hand.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from holdem.cards import format_cards
from holdem.evaluator import arrange_five, partial_hand_name
from holdem.game import Event, GameError, HandResult, Player, Table

import storage

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("holdem-bot")

TURN_TIMEOUT = int(os.getenv("TURN_TIMEOUT", "60"))
DEFAULT_STACK = int(os.getenv("DEFAULT_STACK", "30000"))
DEFAULT_SB = int(os.getenv("DEFAULT_SB", "200"))
DEFAULT_BB = int(os.getenv("DEFAULT_BB", "500"))
REBUY_AMOUNT = int(os.getenv("REBUY_AMOUNT", "30000"))
TIMER_MIN, TIMER_MAX = 5, 120
AWAY_AFTER = max(1, int(os.getenv("AWAY_AFTER", "2")))  # consecutive timeouts → sit out
TABLES_FILE = os.getenv(
    "TABLES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tables.json")
)
# Telegram user IDs allowed to create/load/close tables (comma-separated). Find yours with /myid.
OWNER_IDS = {int(x) for x in os.getenv("TELEGRAM_CHAT_IDS", "").replace(" ", "").split(",") if x}

# Telegram only allows [a-z0-9_] in real commands, so Korean shortcuts arrive as
# plain text and are routed by regex (see on_korean_shortcut).
KOREAN_SHORTCUTS = {"ㄹㄷ": "join", "ㅂㅂ": "leave", "쉬야": "leave", "ㄱㄱ": "begin", "ㅎㅇ": "start"}
KOREAN_RE = r"^/(" + "|".join(KOREAN_SHORTCUTS) + r")(?:@\w+)?(?:\s|$)"

STREET_KO = {"preflop": "프리플랍", "flop": "플랍", "turn": "턴", "river": "리버"}
GROUP = filters.ChatType.GROUPS
PRIVATE = filters.ChatType.PRIVATE


class Session:
    """A table bound to one group chat, plus bot-side state."""

    def __init__(self, table: Table, title: str, creator_id: int):
        self.table = table
        self.title = title
        self.creator_id = creator_id
        self.lock = asyncio.Lock()  # serialises every state change for this chat
        self.turn_msg_id: int | None = None
        self.turn_timeout = TURN_TIMEOUT


SESSIONS: dict[int, Session] = {}


# ====================================================================== formatting
def esc(s: str) -> str:
    return html.escape(s, quote=False)


def mention(p: Player) -> str:
    return f'<a href="tg://user?id={p.user_id}">{esc(p.name)}</a>'


def display_name(user) -> str:
    name = user.full_name or user.username or str(user.id)
    return name[:20]


def chips(n: int) -> str:
    return f"{n:,}"


def render_table(t: Table) -> str:
    lines = []
    if t.in_progress:
        lines.append(f"<b>🃏 핸드 #{t.hand_no} · {STREET_KO[t.street]}</b>")
        lines.append(f"보드: <b>{format_cards(t.board)}</b>")
        lines.append(f"팟: <b>{chips(t.pot_total)}</b>")
    else:
        lines.append(f"<b>🃏 테이블</b> · 블라인드 {chips(t.small_blind)}/{chips(t.big_blind)}")
    lines.append("")
    cur = t.current_player
    for i, p in enumerate(t.players):
        tags = []
        if t.in_progress or t.hand_no:
            if i == t.button:
                tags.append("D")
            if t.in_progress and i == t.sb_index and i != t.button:
                tags.append("SB")
            if t.in_progress and i == t.bb_index:
                tags.append("BB")
        tag = f" [{'/'.join(tags)}]" if tags else ""
        marker = "👉" if p is cur else "▫️"
        status = ""
        if t.in_progress:
            if not p.in_hand:
                status = " · 대기"
            elif p.folded:
                status = " · 폴드"
            elif p.all_in:
                status = " · <b>올인</b>"
            if p.in_hand and p.bet:
                status += f" · 베팅 {chips(p.bet)}"
        if p.leaving:
            status += " · 퇴장예정"
        elif p.stack == 0 and not (t.in_progress and p.live):
            status += " · 칩 없음 (/rebuy)"
        if p.away and not p.leaving:
            status = status.replace(" · 대기", "") + " · 💤 자리비움"
        lines.append(f"{marker} {esc(p.name)}{tag} — {chips(p.stack)}{status}")
    if not t.players:
        lines.append("(아직 아무도 앉지 않았습니다 — /join)")
    return "\n".join(lines)


def describe(ev: Event) -> str:
    if ev.kind == "street":
        return f"🂠 <b>{STREET_KO[ev.street]}</b>: {format_cards(ev.cards)}"
    name = esc(ev.player.name)
    allin = " <b>(올인)</b>" if ev.all_in else ""
    return {
        "fold": f"❌ {name} 폴드",
        "check": f"✔️ {name} 체크",
        "call": f"📞 {name} 콜 {chips(ev.amount)}{allin}",
        "bet": f"💵 {name} 벳 {chips(ev.amount)}{allin}",
        "raise": f"⬆️ {name} 레이즈 → {chips(ev.amount)}{allin}",
    }[ev.action]


def describe_best_five(best_five, hole) -> str:
    """'🃏 K♠️ K♦️ + A♠️ 9♣️ 4♥️ · 손패 2장' — made cards, then kickers, then hole cards used."""
    made, kickers = arrange_five(best_five)
    text = f"🃏 BEST 5: <b>{format_cards(made)}</b>"
    if kickers:
        text += f" + {format_cards(kickers)}"
    used = sum(1 for c in best_five if c in hole)
    return text + (f" · 손패 {used}장 사용" if used else " · 보드 플레이")


def render_result(r: HandResult, t: Table) -> str:
    lines = [f"🏁 <b>핸드 #{r.hand_no} 결과</b>"]
    if r.uncontested:
        w = r.pots[0].winners[0]
        lines.append(f"🏆 {mention(w)} 팟 <b>{chips(r.pots[0].amount)}</b> 획득 (상대 모두 폴드)")
    else:
        lines.append(f"보드: <b>{format_cards(r.board)}</b>")
        lines.append("")
        for e in r.showdown:
            lines.append(f"• {esc(e.player.name)}: {format_cards(e.hole)} → <b>{e.hand_label}</b>")
            lines.append(f"   {describe_best_five(e.best_five, e.hole)}")
        lines.append("")
        contested = [p for p in r.pots if len(p.eligible) > 1]
        for i, pot in enumerate(r.pots):
            names = ", ".join(mention(w) for w in pot.winners)
            if len(pot.eligible) == 1:
                lines.append(f"↩️ 콜되지 않은 {chips(pot.amount)} → {names} 반환")
                continue
            label = "메인 팟" if i == 0 else f"사이드 팟 {i}"
            if len(contested) == 1:
                label = "팟"
            split = " (분할)" if len(pot.winners) > 1 else ""
            lines.append(f"💰 {label} {chips(pot.amount)} → {names} · {pot.hand_label}{split}")
    for p in r.removed:
        lines.append(f"🚪 {esc(p.name)} 퇴장 (칩 {chips(p.stack)})")
    for p in r.busted:
        lines.append(f"💀 {esc(p.name)} 칩 소진 — /rebuy 로 {chips(REBUY_AMOUNT)} 리바인")
    lines.append("")
    lines.append("칩: " + " · ".join(f"{esc(p.name)} {chips(p.stack)}" for p in t.players))
    return "\n".join(lines)


def turn_keyboard(t: Table) -> InlineKeyboardMarkup:
    p = t.current_player
    la = t.legal_actions()
    h, s = t.hand_no, t.action_seq

    def btn(label, action, amount=0):
        return InlineKeyboardButton(label, callback_data=f"a:{h}:{s}:{action}:{amount}")

    row1 = [btn("❌ 폴드", "fold")]
    if la.can_check:
        row1.append(btn("✔️ 체크", "check"))
    else:
        allin = " (올인)" if la.to_call >= p.stack else ""
        row1.append(btn(f"📞 콜 {chips(la.to_call)}{allin}", "call"))

    rows = [row1]
    if la.can_raise:
        verb = "벳" if t.current_bet == 0 else "레이즈"
        pot_after_call = t.pot_total + (t.current_bet - p.bet)
        options = [la.min_raise_to]
        for frac in (0.5, 1.0):  # half-pot and pot-size raises
            target = t.current_bet + int(pot_after_call * frac)
            target -= target % t.small_blind
            options.append(target)
        targets = sorted({x for x in options if la.min_raise_to <= x < la.max_raise_to})
        row2 = [btn(f"{verb} {chips(x)}", "raise", x) for x in targets[:3]]
        if row2:
            rows.append(row2)
        rows.append([btn(f"🔥 올인 {chips(la.max_raise_to)}", "allin")])
    rows.append([InlineKeyboardButton("🃏 내 패 보기", callback_data=f"c:{h}")])
    return InlineKeyboardMarkup(rows)


# ====================================================================== flow helpers
def cancel_jobs(ctx: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    for job in ctx.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


async def clear_turn_keyboard(ctx, chat_id: int, s: Session) -> None:
    if s.turn_msg_id is None:
        return
    try:
        await ctx.bot.edit_message_reply_markup(chat_id, s.turn_msg_id, reply_markup=None)
    except TelegramError:
        pass
    s.turn_msg_id = None


async def send(ctx, chat_id: int, text: str, **kw):
    return await ctx.bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw
    )


def persist(*sessions: Session) -> None:
    """Save stacks to disk. Never let a disk problem break the game."""
    try:
        storage.save(
            TABLES_FILE,
            {
                s.table.chat_id: storage.snapshot(s.table, s.title, s.creator_id, s.turn_timeout)
                for s in sessions
            },
        )
    except Exception:
        log.exception("failed to save tables to %s", TABLES_FILE)


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


async def require_owner(update: Update) -> bool:
    if is_owner(update.effective_user.id):
        return True
    if not OWNER_IDS:
        text = (
            "봇 관리자가 설정되지 않았습니다.\n"
            "봇 DM에서 /myid 로 내 ID를 확인한 뒤 .env 의 TELEGRAM_CHAT_IDS 에 넣고 봇을 다시 실행하세요."
        )
    else:
        text = "테이블 생성/불러오기/닫기는 봇 관리자만 할 수 있습니다."
    await update.effective_message.reply_text(text)
    return False


async def handle_events(ctx, chat_id: int, s: Session, events: list[Event], prefix=()) -> None:
    """Publish what just happened, then either prompt the next player or wrap up the hand."""
    t = s.table
    persist(s)  # after every action, so even a hard kill loses at most the running hand
    cancel_jobs(ctx, f"turn:{chat_id}")
    await clear_turn_keyboard(ctx, chat_id, s)
    lines = list(prefix)
    result = None
    for ev in events:
        if ev.kind == "result":
            result = ev.result
        else:
            lines.append(describe(ev))

    if result is None:
        await prompt_turn(ctx, chat_id, s, lines)
        return

    text = "\n".join(lines + ([""] if lines else []) + [render_result(result, t)])
    footer, can_start = after_hand_footer(s)
    text += "\n\n" + footer
    await send(ctx, chat_id, text, reply_markup=next_hand_keyboard(t) if can_start else None)


def next_hand_keyboard(t: Table) -> InlineKeyboardMarkup:
    # Carries the finished hand's number so a second click can't deal two hands.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ 다음 핸드 시작", callback_data=f"b:{t.hand_no}")]]
    )


def after_hand_footer(s: Session) -> tuple[str, bool]:
    t = s.table
    ready = sum(1 for p in t.players if t.can_be_dealt(p))
    away = [p for p in t.players if p.away and not p.leaving]
    note = ""
    if away:
        note = "\n💤 자리비움: " + ", ".join(esc(p.name) for p in away) + " — 복귀: /back"
    if ready >= 2:
        return "▶️ 다음 핸드: 아래 버튼 또는 /ㄱㄱ" + note, True
    return "⏸ 참여 가능한 플레이어가 부족합니다. /join · /rebuy · /back 후 /ㄱㄱ" + note, False


async def prompt_turn(ctx, chat_id: int, s: Session, lines: list[str]) -> None:
    t = s.table
    p = t.current_player
    la = t.legal_actions()
    need = "체크 가능" if la.can_check else f"콜 {chips(la.to_call)}"
    # Whose turn it is always comes first, so it shows in notification previews.
    status = f"👉 {mention(p)} 차례 · {need} · ⏱ {s.turn_timeout}초"
    body = "\n".join([status, ""] + lines + ([""] if lines else []) + [render_table(t)])
    msg = await send(ctx, chat_id, body, reply_markup=turn_keyboard(t))
    s.turn_msg_id = msg.message_id
    ctx.job_queue.run_once(
        turn_timeout_job,
        s.turn_timeout,
        data={"chat_id": chat_id, "hand": t.hand_no, "seq": t.action_seq},
        name=f"turn:{chat_id}",
    )


async def deal_new_hand(ctx, chat_id: int, s: Session) -> None:
    """Must be called with s.lock held."""
    t = s.table
    if t.in_progress:
        return
    try:
        events = t.start_hand()
    except GameError as e:
        await send(ctx, chat_id, f"⚠️ {esc(str(e))}")
        return

    failed = []
    for p in t.players:
        if not p.in_hand:
            continue
        try:
            await send(
                ctx,
                p.user_id,
                f"🃏 <b>{esc(s.title)}</b> · 핸드 #{t.hand_no}\n"
                f"내 패: <b>{format_cards(p.hole)}</b>  ({partial_hand_name(p.hole)})\n"
                f"스택: {chips(p.stack + p.bet)}",
            )
        except (Forbidden, BadRequest):
            failed.append(p)

    sb, bb = t.players[t.sb_index], t.players[t.bb_index]
    prefix = [
        f"🎲 <b>핸드 #{t.hand_no} 시작!</b> 패를 개인 메시지로 보냈습니다.",
        f"딜러 {esc(t.players[t.button].name)} · SB {esc(sb.name)} {chips(sb.bet)} · "
        f"BB {esc(bb.name)} {chips(bb.bet)}",
    ]
    if failed:
        names = ", ".join(mention(p) for p in failed)
        prefix.append(
            f"⚠️ {names} 님께 DM을 보내지 못했습니다. 봇과 1:1 대화에서 /start 를 눌러주세요. "
            "그 전까지는 '🃏 내 패 보기' 버튼으로 확인할 수 있습니다."
        )
    await handle_events(ctx, chat_id, s, events, prefix=prefix)


async def apply_action(ctx, chat_id: int, s: Session, user_id: int, action: str, amount=None):
    """Run a player action under the lock. Returns an error string or None."""
    async with s.lock:
        try:
            events = s.table.act(user_id, action, amount)
        except GameError as e:
            return str(e)
        reset_timeouts(s, user_id)
        await handle_events(ctx, chat_id, s, events)
    return None


def reset_timeouts(s: Session, user_id: int) -> None:
    """A real action proves the player is present."""
    p = s.table.get_player(user_id)
    if p:
        p.timeouts = 0


# ====================================================================== jobs
async def turn_timeout_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    d = ctx.job.data
    chat_id = d["chat_id"]
    s = SESSIONS.get(chat_id)
    if not s:
        return
    async with s.lock:
        t = s.table
        if not t.in_progress or t.hand_no != d["hand"] or t.action_seq != d["seq"]:
            return
        p = t.current_player
        p.timeouts += 1
        going_away = p.timeouts >= AWAY_AFTER
        # Once marked away, fold rather than check so they don't stall later streets too.
        action = "check" if t.legal_actions().can_check and not going_away else "fold"
        events = t.act(p.user_id, action)
        word = "체크" if action == "check" else "폴드"
        prefix = [f"⏰ {esc(p.name)} 시간 초과 → 자동 {word}"]
        if going_away:
            t.set_away(p.user_id, True)
            prefix.append(
                f"💤 {esc(p.name)} 연속 {AWAY_AFTER}회 시간 초과 → <b>자리비움</b> "
                "(다음 핸드부터 제외, 복귀: /back)"
            )
        await handle_events(ctx, chat_id, s, events, prefix=prefix)


# ====================================================================== group commands
HELP_GROUP = (
    "<b>📖 홀덤봇 명령어 목록</b>\n"
    "처음 한 번은 봇과 1:1 대화에서 /start (또는 /ㅎㅇ) — 패를 DM으로 받기 위해 필요\n\n"
    "<b>👥 참가 관련</b>\n"
    "/join — 게임에 참가 (시작 칩 {stack})\n"
    "/leave — 게임에서 나가기 (핸드 중이면 폴드 처리 후 퇴장)\n"
    "/players — 현재 참가자 목록 확인\n"
    "/back — 자리비움에서 복귀 (연속 {away}회 시간 초과 시 자동 자리비움)\n"
    "/rebuy — 칩이 부족할 때 리바인 ({rebuy} 추가)\n\n"
    "<b>🎮 게임 진행</b>\n"
    "/begin — 핸드 시작 (2명 이상 필요, 매 핸드마다 /ㄱㄱ)\n"
    "/status — 현재 게임/로비 상태 확인\n\n"
    "<b>🃏 액션 (핸드 진행 중)</b>\n"
    "/call — 콜 · /check — 체크 · /fold — 폴드\n"
    "/raise 금액 — 레이즈 (예: /raise 1000, 총 베팅액 기준)\n"
    "/allin — 올인\n"
    "(그룹 메시지의 버튼으로도 가능)\n\n"
    "<b>⚙️ 설정</b>\n"
    "/blinds SB BB — 블라인드 설정 (예: /blinds 200 400)\n"
    "/timer 초 — 액션 타임아웃 설정 ({tmin}~{tmax}초, 예: /timer 30)\n"
    "\n<b>🔑 봇 관리자 전용</b>\n"
    "/newtable [스택] [SB] [BB] — 테이블 만들기\n"
    "/loadtable — 저장된 테이블(스택) 불러오기\n"
    "/closetable — 테이블 닫기 (저장 기록도 삭제)\n"
    "/myid — 내 텔레그램 ID 확인\n\n"
    "<b>🔍 기록</b>\n"
    "/hand — 전판 내 패를 그룹에 공개\n"
    "/open — 전판 커뮤니티 카드 전체 공개 (리버까지)\n\n"
    "<b>⌨️ 한글 단축키</b>\n"
    "/ㄹㄷ → /join\n"
    "/ㅂㅂ, /쉬야 → /leave\n"
    "/ㄱㄱ → /begin\n"
    "/ㅎㅇ → /start (봇 DM 활성화)"
)


def help_text() -> str:
    return HELP_GROUP.format(
        stack=chips(DEFAULT_STACK),
        rebuy=chips(REBUY_AMOUNT),
        tmin=TIMER_MIN,
        tmax=TIMER_MAX,
        away=AWAY_AFTER,
    )


def create_session(update: Update, stack: int, sb: int, bb: int) -> Session:
    chat = update.effective_chat
    table = Table(chat.id, starting_stack=stack, small_blind=sb, big_blind=bb)
    s = Session(table, chat.title or "홀덤", update.effective_user.id)
    SESSIONS[chat.id] = s
    return s


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML)


async def cmd_newtable(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not await require_owner(update):
        return
    if chat.id in SESSIONS:
        await msg.reply_text("이미 이 방에 테이블이 있습니다. /status 로 확인하세요.")
        return
    args = list(ctx.args)
    force = bool(args) and args[0].lower() == "new"
    if force:
        args = args[1:]
    saved = storage.get(TABLES_FILE, chat.id)
    if saved and not force:
        await msg.reply_text(
            f"이 방에 저장된 테이블이 있습니다 ({len(saved['players'])}명, {saved['saved_at'][:10]}).\n"
            "• 이어서 하기: /loadtable\n"
            "• 저장된 스택을 버리고 새로 만들기: /newtable new [스택] [SB] [BB]"
        )
        return
    try:
        nums = [int(x.replace(",", "")) for x in args]
    except ValueError:
        await msg.reply_text("사용법: /newtable [시작스택] [SB] [BB]  예) /newtable 30000 200 500")
        return
    if not nums:
        stack, sb, bb = DEFAULT_STACK, DEFAULT_SB, DEFAULT_BB
    else:
        stack = nums[0]
        sb = nums[1] if len(nums) > 1 else max(1, stack // 200)
        bb = nums[2] if len(nums) > 2 else sb * 2
    try:
        s = create_session(update, stack, sb, bb)
    except GameError as e:
        await msg.reply_text(str(e))
        return
    persist(s)
    await send(
        ctx,
        chat.id,
        f"🃏 <b>테이블 생성!</b>\n시작 스택 {chips(stack)} · 블라인드 {chips(sb)}/{chips(bb)}\n\n"
        f"참가: /join  (먼저 @{ctx.bot.username} 와 1:1 대화에서 /start)\n"
        "2명 이상 모이면 /begin",
    )


async def cmd_loadtable(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not await require_owner(update):
        return
    if chat.id in SESSIONS:
        await msg.reply_text("이미 이 방에 테이블이 열려 있습니다. /status 로 확인하세요.")
        return
    try:
        snap = storage.get(TABLES_FILE, chat.id)
    except (OSError, ValueError):
        log.exception("failed to read %s", TABLES_FILE)
        await msg.reply_text("⚠️ 저장 파일을 읽지 못했습니다. 봇 로그를 확인하세요.")
        return
    if snap is None:
        await msg.reply_text("이 방에 저장된 테이블이 없습니다. /newtable 로 새로 만드세요.")
        return
    table = storage.restore(chat.id, snap)
    s = Session(table, chat.title or snap.get("title") or "홀덤", snap.get("owner_id") or update.effective_user.id)
    s.turn_timeout = snap.get("turn_timeout", TURN_TIMEOUT)
    SESSIONS[chat.id] = s
    lines = [
        f"📂 <b>테이블 불러오기 완료</b> ({snap['saved_at'][:16].replace('T', ' ')} UTC 저장)",
        f"블라인드 {chips(table.small_blind)}/{chips(table.big_blind)} · 타이머 {s.turn_timeout}초",
        "",
    ]
    for p in table.players:
        tag = " 💤" if p.away else ""
        lines.append(f"• {mention(p)} {chips(p.stack)}{tag}")
    lines += ["", "새로 앉으려면 /join · 시작은 /ㄱㄱ"]
    await send(ctx, chat.id, "\n".join(lines))


def get_session(update: Update) -> Session | None:
    return SESSIONS.get(update.effective_chat.id)


async def require_session(update: Update) -> Session | None:
    s = get_session(update)
    if s is None:
        await update.effective_message.reply_text(
            "테이블이 없습니다. 봇 관리자가 /newtable 또는 /loadtable 로 열어야 합니다."
        )
    return s


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    user = update.effective_user
    msg = update.effective_message
    existing = s.table.get_player(user.id)
    if existing and existing.away and not existing.leaving:
        await cmd_back(update, ctx)  # /join while sitting out = come back
        return
    if existing and not existing.leaving:
        await msg.reply_text("이미 테이블에 앉아 있습니다.")
        return
    # Verify we can DM this user before seating them — hole cards go by DM.
    try:
        await send(
            ctx,
            user.id,
            f"✅ <b>{esc(s.title)}</b> 테이블에 참가했습니다.\n핸드가 시작되면 여기로 패를 보내드립니다.",
        )
    except (Forbidden, BadRequest):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🤖 봇과 대화 시작", url=f"https://t.me/{ctx.bot.username}?start=join")]]
        )
        await msg.reply_text(
            "패를 개인 메시지로 받으려면 먼저 봇과 1:1 대화를 시작해야 합니다.\n"
            "아래 버튼 → 시작(START) 누른 뒤 여기서 다시 /join 해주세요.",
            reply_markup=kb,
        )
        return
    async with s.lock:
        try:
            p = s.table.add_player(user.id, display_name(user))
        except GameError as e:
            await msg.reply_text(str(e))
            return
        t = s.table
        persist(s)
        note = " (다음 핸드부터 참여)" if t.in_progress and not p.in_hand else ""
        if p.returning:
            note = " (이전 칩 복원)" + note
            if p.stack == 0:
                note += f"\n💀 칩이 없습니다 — /rebuy 로 {chips(REBUY_AMOUNT)} 리바인"
        await send(
            ctx,
            update.effective_chat.id,
            f"🪑 {mention(p)} 착석 — 칩 {chips(p.stack)}{note}\n현재 {len(t.players)}명",
        )


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    async with s.lock:
        t = s.table
        p = t.get_player(user.id)
        if p is None:
            await update.effective_message.reply_text("테이블에 앉아 있지 않습니다.")
            return
        was_live = t.in_progress and p.live
        events = t.remove_player(user.id)
        if was_live:  # their fold may have moved the action or even ended the hand
            await handle_events(
                ctx, chat_id, s, events, prefix=[f"🚪 {esc(p.name)} 님이 폴드하고 테이블을 떠납니다."]
            )
        elif p in t.players:  # mid-hand but not dealt in / already folded
            persist(s)
            await send(ctx, chat_id, f"🚪 {esc(p.name)} 님은 이번 핸드가 끝나면 퇴장합니다.")
        else:
            persist(s)
            await send(ctx, chat_id, f"🚪 {esc(p.name)} 퇴장 (칩 {chips(p.stack)})")


async def cmd_startgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    async with s.lock:
        if s.table.in_progress:
            await update.effective_message.reply_text("이미 핸드가 진행 중입니다.")
            return
        await deal_new_hand(ctx, update.effective_chat.id, s)


async def is_admin(update: Update, ctx) -> bool:
    try:
        m = await ctx.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False


async def cmd_closetable(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    chat_id = update.effective_chat.id
    if not await require_owner(update):
        return
    async with s.lock:
        if s.table.in_progress:
            await update.effective_message.reply_text(
                "핸드가 진행 중입니다. 핸드가 끝난 뒤 다시 시도하세요."
            )
            return
        cancel_jobs(ctx, f"turn:{chat_id}")
        del SESSIONS[chat_id]
        try:
            storage.delete(TABLES_FILE, chat_id)  # finished table: nothing left to /loadtable
        except Exception:
            log.exception("failed to delete saved table %s", chat_id)
        standings = sorted(s.table.players, key=lambda p: -p.stack)
        lines = ["🏁 <b>테이블 종료</b> — 최종 칩"]
        for i, p in enumerate(standings, 1):
            diff = p.stack - p.buy_in
            rebuy = f" · 리바인 {p.rebuys}회" if p.rebuys else ""
            lines.append(f"{i}. {esc(p.name)} {chips(p.stack)} ({diff:+,}){rebuy}")
        await send(ctx, chat_id, "\n".join(lines))


async def cmd_players(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    t = s.table
    if not t.players:
        await update.effective_message.reply_text("참가자가 없습니다. /join 으로 참가하세요.")
        return
    lines = [f"👥 <b>참가자 {len(t.players)}명</b> · 블라인드 {chips(t.small_blind)}/{chips(t.big_blind)}"]
    for i, p in enumerate(t.players, 1):
        extra = []
        if p.rebuys:
            extra.append(f"리바인 {p.rebuys}회")
        if p.stack == 0 and not (t.in_progress and p.live):
            extra.append("칩 없음")
        if t.in_progress and not p.in_hand:
            extra.append("다음 핸드부터")
        if p.leaving:
            extra.append("퇴장예정")
        elif p.away:
            extra.append("💤 자리비움")
        tail = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"{i}. {mention(p)} — {chips(p.stack)}{tail}")
    await send(ctx, update.effective_chat.id, "\n".join(lines))


async def cmd_rebuy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    async with s.lock:
        try:
            p = s.table.rebuy(update.effective_user.id, REBUY_AMOUNT)
        except GameError as e:
            await update.effective_message.reply_text(f"⚠️ {e}")
            return
        persist(s)
        await send(
            ctx,
            update.effective_chat.id,
            f"💳 {mention(p)} 리바인 +{chips(REBUY_AMOUNT)} → {chips(p.stack)} (총 {p.rebuys}회)",
        )


async def cmd_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    async with s.lock:
        t = s.table
        p = t.get_player(update.effective_user.id)
        if p is None:
            await update.effective_message.reply_text("테이블에 앉아 있지 않습니다. /join 으로 참가하세요.")
            return
        if not p.away:
            await update.effective_message.reply_text("자리비움 상태가 아닙니다.")
            return
        t.set_away(p.user_id, False)
        persist(s)
        when = "다음 핸드부터" if t.in_progress else "이번 /ㄱㄱ 부터"
        await send(
            ctx,
            update.effective_chat.id,
            f"🙋 {mention(p)} 복귀 — {when} 참여합니다. (칩 {chips(p.stack)})",
        )


async def cmd_blinds(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    msg = update.effective_message
    try:
        sb, bb = (int(x.replace(",", "")) for x in ctx.args[:2])
        if len(ctx.args) != 2:
            raise ValueError
    except ValueError:
        t = s.table
        await msg.reply_text(
            f"사용법: /blinds SB BB  예) /blinds 200 400\n현재 {t.small_blind:,}/{t.big_blind:,}"
        )
        return
    if not s.table.get_player(update.effective_user.id) and not await is_admin(update, ctx):
        await msg.reply_text("테이블 참가자 또는 방 관리자만 블라인드를 바꿀 수 있습니다.")
        return
    async with s.lock:
        try:
            now = s.table.set_blinds(sb, bb)
        except GameError as e:
            await msg.reply_text(f"⚠️ {e}")
            return
        persist(s)
    when = "" if now else " (다음 핸드부터 적용)"
    await send(ctx, update.effective_chat.id, f"⚙️ 블라인드 {chips(sb)}/{chips(bb)}{when}")


async def cmd_timer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    msg = update.effective_message
    try:
        sec = int(ctx.args[0])
    except (IndexError, ValueError):
        await msg.reply_text(f"사용법: /timer 초 ({TIMER_MIN}~{TIMER_MAX})  현재 {s.turn_timeout}초")
        return
    if not TIMER_MIN <= sec <= TIMER_MAX:
        await msg.reply_text(f"⚠️ 타임아웃은 {TIMER_MIN}~{TIMER_MAX}초 사이로 설정하세요.")
        return
    s.turn_timeout = sec
    persist(s)
    await send(ctx, update.effective_chat.id, f"⏱ 액션 타임아웃 {sec}초 (다음 차례부터 적용)")


def last_hand_text(s: Session, user_id: int) -> str | None:
    r = s.table.last_result
    if r is None or user_id not in r.holes:
        return None
    hole = r.holes[user_id]
    text = (
        f"🔍 <b>{esc(s.title)}</b> · 핸드 #{r.hand_no} (전판)\n"
        f"내 패: <b>{format_cards(hole)}</b>\n보드: {format_cards(r.board)}"
    )
    if r.board:
        text += f"\n족보: {partial_hand_name(hole + r.board)}"
    return text


async def cmd_hand(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    msg = update.effective_message
    if update.effective_chat.type == "private":
        texts = [x for s in SESSIONS.values() if (x := last_hand_text(s, uid))]
        await msg.reply_text(
            "\n\n".join(texts) or "확인할 전판 기록이 없습니다.", parse_mode=ParseMode.HTML
        )
        return
    s = await require_session(update)
    if not s:
        return
    r = s.table.last_result
    if r is None or uid not in r.holes:
        await msg.reply_text("전판에 참여한 기록이 없습니다.")
        return
    # Public reveal in the group: the player chooses to show their own previous hand.
    hole = r.holes[uid]
    p = s.table.get_player(uid)
    who = mention(p) if p else esc(display_name(update.effective_user))
    lines = [
        f"🔍 {who} 님의 핸드 #{r.hand_no} 패 공개",
        f"패: <b>{format_cards(hole)}</b>",
        f"보드: {format_cards(r.board)}",
    ]
    if r.board:
        lines.append(f"족보: {partial_hand_name(hole + r.board)}")
    await send(ctx, update.effective_chat.id, "\n".join(lines))


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    owner = " (봇 관리자)" if is_owner(uid) else ""
    await update.effective_message.reply_text(
        f"내 텔레그램 ID: <code>{uid}</code>{owner}\n"
        "봇 관리자로 지정하려면 .env 에 <code>TELEGRAM_CHAT_IDS=" + str(uid) + "</code> 를 넣고 봇을 다시 실행하세요.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    r = s.table.last_result
    if r is None:
        await update.effective_message.reply_text("공개할 전판 기록이 없습니다.")
        return
    lines = [f"🔓 <b>핸드 #{r.hand_no} 전체 보드</b>", f"<b>{format_cards(r.full_board)}</b>"]
    if len(r.board) < 5:
        unseen = r.full_board[len(r.board):]
        lines.append(f"실제 진행: {format_cards(r.board)} · 미공개였던 카드: {format_cards(unseen)}")
    else:
        lines.append("(리버까지 모두 진행된 핸드입니다)")
    await send(ctx, update.effective_chat.id, "\n".join(lines))


async def on_korean_shortcut(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    key = re.match(KOREAN_RE, update.effective_message.text).group(1)
    target = KOREAN_SHORTCUTS[key]
    ctx.args = update.effective_message.text.split()[1:]
    private = update.effective_chat.type == "private"
    if target == "start":
        if private:
            await cmd_start_private(update, ctx)
        else:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🤖 봇 DM 활성화", url=f"https://t.me/{ctx.bot.username}?start=join")]]
            )
            await update.effective_message.reply_text(
                "아래 버튼을 눌러 봇과 1:1 대화를 시작(START)하세요.", reply_markup=kb
            )
        return
    if private:
        await update.effective_message.reply_text("이 명령은 게임 그룹방에서 사용하세요.")
        return
    await {"join": cmd_join, "leave": cmd_leave, "begin": cmd_startgame}[target](update, ctx)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await require_session(update)
    if not s:
        return
    t = s.table
    text = render_table(t)
    if t.in_progress:
        text += f"\n\n👉 {mention(t.current_player)} 차례"
    else:
        text += "\n\n대기 중 — /ㄱㄱ (/begin) 으로 핸드 시작"
    await send(ctx, update.effective_chat.id, text)


def make_action_cmd(action: str):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = await require_session(update)
        if not s:
            return
        amount = None
        if action == "raise":
            try:
                amount = int(ctx.args[0].replace(",", ""))
            except (IndexError, ValueError):
                await update.effective_message.reply_text(
                    "사용법: /raise 금액 (이번 스트릿 총 베팅액 기준)  예) /raise 600"
                )
                return
        err = await apply_action(
            ctx, update.effective_chat.id, s, update.effective_user.id, action, amount
        )
        if err:
            await update.effective_message.reply_text(f"⚠️ {err}")

    return handler


# ====================================================================== callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = q.message.chat.id if q.message else None
    s = SESSIONS.get(chat_id)
    if s is None:
        await q.answer("테이블이 없습니다.")
        return
    parts = q.data.split(":")

    if parts[0] == "c":  # peek own hole cards (alert visible only to the presser)
        t = s.table
        p = t.get_player(q.from_user.id)
        if p is None or not p.hole or not p.in_hand or t.hand_no != int(parts[1]):
            await q.answer("이번 핸드에 참여하고 있지 않습니다.", show_alert=True)
            return
        text = f"내 패: {format_cards(p.hole)}"
        if t.board:
            text += f"\n보드: {format_cards(t.board)}"
        text += f"\n현재: {partial_hand_name(p.hole + t.board)}"
        if p.folded:
            text += "\n(폴드함)"
        await q.answer(text, show_alert=True)
        return

    if parts[0] == "b":  # "next hand" button on a result message
        async with s.lock:
            t = s.table
            stale = t.in_progress or t.hand_no != int(parts[1])
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
            if stale:
                await q.answer("이미 다음 핸드가 시작되었습니다.")
                return
            await q.answer()
            await deal_new_hand(ctx, chat_id, s)
        return

    if parts[0] == "a":
        _, hand, seq, action, amount = parts
        t = s.table
        if not t.in_progress or t.hand_no != int(hand) or t.action_seq != int(seq):
            await q.answer("이미 지나간 차례입니다.")
            return
        if t.current_player.user_id != q.from_user.id:
            await q.answer("당신의 차례가 아닙니다.")
            return
        # Re-validated inside the lock by Table.act (hand/seq may change meanwhile).
        async with s.lock:
            if t.action_seq != int(seq) or t.hand_no != int(hand):
                await q.answer("이미 지나간 차례입니다.")
                return
            try:
                events = t.act(q.from_user.id, action, int(amount) or None)
                reset_timeouts(s, q.from_user.id)
            except GameError as e:
                await q.answer(str(e), show_alert=True)
                return
            await q.answer()
            await handle_events(ctx, chat_id, s, events)
        return

    await q.answer()


# ====================================================================== private chat
async def cmd_start_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    extra = "\n\n✅ 준비 완료! 그룹으로 돌아가 /join 을 다시 입력하세요." if ctx.args else ""
    await update.effective_message.reply_text(
        "🃏 <b>홀덤 딜러 봇</b>입니다.\n"
        "이 대화창으로 매 핸드 당신의 패를 보내드립니다.\n\n"
        "게임 방법: 그룹을 만들고 이 봇을 초대한 뒤 /join (/ㄹㄷ) → /begin (/ㄱㄱ)\n"
        "/mycards — 지금 진행 중인 내 패 다시 보기\n"
        "/hand — 전판 내 패 확인" + extra,
        parse_mode=ParseMode.HTML,
    )


async def cmd_mycards(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    lines = []
    for s in SESSIONS.values():
        t = s.table
        p = t.get_player(uid)
        if p and t.in_progress and p.in_hand:
            state = " (폴드)" if p.folded else ""
            lines.append(
                f"<b>{esc(s.title)}</b> 핸드 #{t.hand_no}{state}\n"
                f"내 패: <b>{format_cards(p.hole)}</b> · 보드: {format_cards(t.board)}\n"
                f"현재: {partial_hand_name(p.hole + t.board)}"
            )
    await update.effective_message.reply_text(
        "\n\n".join(lines) or "진행 중인 핸드가 없습니다.", parse_mode=ParseMode.HTML
    )


# ====================================================================== misc
async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cmu: ChatMemberUpdated = update.my_chat_member
    if cmu.chat.type not in ("group", "supergroup"):
        return
    was_in = cmu.old_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    now_in = cmu.new_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    if now_in and not was_in:
        await send(ctx, cmu.chat.id, "안녕하세요! 딜러 봇입니다 🃏\n\n" + help_text())
    elif was_in and not now_in:
        SESSIONS.pop(cmu.chat.id, None)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=ctx.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("join", "게임에 참가"),
            BotCommand("leave", "게임에서 나가기"),
            BotCommand("players", "참가자 목록"),
            BotCommand("rebuy", f"리바인 ({REBUY_AMOUNT:,} 추가)"),
            BotCommand("back", "자리비움에서 복귀"),
            BotCommand("begin", "핸드 시작"),
            BotCommand("status", "게임/로비 상태"),
            BotCommand("call", "콜"),
            BotCommand("check", "체크"),
            BotCommand("fold", "폴드"),
            BotCommand("raise", "레이즈 (예: /raise 1000)"),
            BotCommand("allin", "올인"),
            BotCommand("blinds", "블라인드 설정 (예: /blinds 200 400)"),
            BotCommand("timer", "액션 타임아웃 (예: /timer 30)"),
            BotCommand("hand", "전판 내 패 그룹에 공개"),
            BotCommand("open", "전판 커뮤니티 카드 전체 공개"),
            BotCommand("newtable", "[관리자] 테이블 만들기 [스택] [SB] [BB]"),
            BotCommand("loadtable", "[관리자] 저장된 테이블 불러오기"),
            BotCommand("closetable", "[관리자] 테이블 닫기"),
            BotCommand("help", "명령어 목록"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    await app.bot.set_my_commands(
        [
            BotCommand("start", "봇 DM 활성화"),
            BotCommand("mycards", "진행 중인 내 패 보기"),
            BotCommand("hand", "전판 내 패 확인"),
            BotCommand("myid", "내 텔레그램 ID 확인"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )


async def post_shutdown(app: Application) -> None:
    """Ctrl+C / normal stop: save every open table one last time."""
    if SESSIONS:
        persist(*SESSIONS.values())
        log.info("saved %d table(s) to %s", len(SESSIONS), TABLES_FILE)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN 환경변수를 설정하세요 (.env 파일 또는 환경변수).")
    if not OWNER_IDS:
        log.warning("TELEGRAM_CHAT_IDS 미설정: /newtable·/loadtable 을 아무도 쓸 수 없습니다. 봇 DM에서 /myid 로 확인하세요.")
    app = (
        Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    )

    app.add_handler(CommandHandler("start", cmd_start_private, filters=PRIVATE))
    app.add_handler(CommandHandler("mycards", cmd_mycards, filters=PRIVATE))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("hand", cmd_hand))  # group: public reveal; private: lists them
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(KOREAN_RE), on_korean_shortcut))

    group_cmds = {
        "newtable": cmd_newtable,
        "loadtable": cmd_loadtable,
        "join": cmd_join,
        "leave": cmd_leave,
        "begin": cmd_startgame,
        "startgame": cmd_startgame,
        "players": cmd_players,
        "rebuy": cmd_rebuy,
        "back": cmd_back,
        "blinds": cmd_blinds,
        "timer": cmd_timer,
        "open": cmd_open,
        "closetable": cmd_closetable,
        "status": cmd_status,
        "raise": make_action_cmd("raise"),
        "bet": make_action_cmd("raise"),
        "call": make_action_cmd("call"),
        "check": make_action_cmd("check"),
        "fold": make_action_cmd("fold"),
        "allin": make_action_cmd("allin"),
    }
    for name, fn in group_cmds.items():
        app.add_handler(CommandHandler(name, fn, filters=GROUP))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)

    log.info("Hold'em dealer bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
