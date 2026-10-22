import asyncio
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


ROULETTE_REDS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
SYS_RNG = random.SystemRandom()


class EconomyStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        starting_credits: int = 10000,
        daily_credits: int = 500,
        min_casino_bet: int = 10,
        max_casino_bet: int = 1_000_000,
        min_coupon_stake: int = 50,
    ):
        self.db_path = Path(data_dir) / "economy.sqlite3"
        self.lock = threading.RLock()
        self.starting_credits = int(starting_credits)
        self.daily_credits = int(daily_credits)
        self.min_casino_bet = int(min_casino_bet)
        self.max_casino_bet = int(max_casino_bet)
        self.min_coupon_stake = int(min_coupon_stake)
        self.init_db()

    def conn(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        return con

    def init_db(self):
        with self.lock:
            with self.conn() as con:
                con.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users(
                        user_id TEXT PRIMARY KEY,
                        balance INTEGER NOT NULL,
                        last_daily INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS transactions(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        amount INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        ref TEXT,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS casino_stats(
                        user_id TEXT PRIMARY KEY,
                        games INTEGER NOT NULL DEFAULT 0,
                        wins INTEGER NOT NULL DEFAULT 0,
                        wagered INTEGER NOT NULL DEFAULT 0,
                        profit INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS matches(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id TEXT NOT NULL,
                        home TEXT NOT NULL,
                        away TEXT NOT NULL,
                        start_time TEXT,
                        status TEXT NOT NULL DEFAULT 'open',
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS markets(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id INTEGER NOT NULL,
                        market_name TEXT NOT NULL,
                        option_name TEXT NOT NULL,
                        odds REAL NOT NULL,
                        result TEXT NOT NULL DEFAULT 'pending',
                        created_at INTEGER NOT NULL,
                        FOREIGN KEY(match_id) REFERENCES matches(id)
                    );
                    CREATE TABLE IF NOT EXISTS coupons(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        guild_id TEXT NOT NULL,
                        stake INTEGER NOT NULL,
                        total_odds REAL NOT NULL,
                        potential_win INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        created_at INTEGER NOT NULL,
                        settled_at INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS coupon_items(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        coupon_id INTEGER NOT NULL,
                        market_id INTEGER NOT NULL,
                        option_name TEXT NOT NULL,
                        odds REAL NOT NULL,
                        FOREIGN KEY(coupon_id) REFERENCES coupons(id),
                        FOREIGN KEY(market_id) REFERENCES markets(id)
                    );
                    CREATE TABLE IF NOT EXISTS blackjack_sessions(
                        user_id TEXT PRIMARY KEY,
                        stake INTEGER NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    """
                )

                # Recover locked blackjack stakes after an unexpected restart.
                pending = con.execute(
                    "SELECT user_id, stake FROM blackjack_sessions"
                ).fetchall()
                for row in pending:
                    self._ensure_user(row["user_id"], con)
                    con.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (int(row["stake"]), str(row["user_id"])),
                    )
                    con.execute(
                        "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,?,?)",
                        (
                            str(row["user_id"]),
                            int(row["stake"]),
                            "blackjack_restart_refund",
                            None,
                            int(time.time()),
                        ),
                    )
                con.execute("DELETE FROM blackjack_sessions")

    def _ensure_user(self, user_id, con):
        uid = str(user_id)
        row = con.execute(
            "SELECT user_id FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        if row is None:
            now = int(time.time())
            con.execute(
                "INSERT INTO users(user_id,balance,last_daily,created_at) VALUES(?,?,0,?)",
                (uid, self.starting_credits, now),
            )
            con.execute(
                "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,NULL,?)",
                (uid, self.starting_credits, "starting_credits", now),
            )

    def balance(self, user_id) -> int:
        with self.lock:
            with self.conn() as con:
                self._ensure_user(user_id, con)
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (str(user_id),)
                ).fetchone()
                return int(row["balance"])

    def change_balance(self, user_id, amount: int, reason: str, ref: Optional[str] = None) -> int:
        amount = int(amount)
        with self.lock:
            with self.conn() as con:
                self._ensure_user(user_id, con)
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (str(user_id),)
                ).fetchone()
                new_balance = int(row["balance"]) + amount
                if new_balance < 0:
                    raise RuntimeError("Yetersiz kredi.")
                con.execute(
                    "UPDATE users SET balance=? WHERE user_id=?",
                    (new_balance, str(user_id)),
                )
                con.execute(
                    "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,?,?)",
                    (str(user_id), amount, reason, ref, int(time.time())),
                )
                return new_balance

    def validate_bet(self, user_id, bet: int) -> int:
        bet = int(bet)
        if bet < self.min_casino_bet:
            raise RuntimeError(f"Minimum bahis {fmt_credit(self.min_casino_bet)} kredi.")
        if bet > self.max_casino_bet:
            raise RuntimeError(f"Maksimum bahis {fmt_credit(self.max_casino_bet)} kredi.")
        balance = self.balance(user_id)
        if bet > balance:
            raise RuntimeError(f"Yetersiz kredi. Bakiyen: {fmt_credit(balance)}")
        return bet

    def update_stats(self, user_id, wager: int, payout: int):
        won = 1 if payout > wager else 0
        profit = int(payout) - int(wager)
        with self.lock:
            with self.conn() as con:
                con.execute(
                    """
                    INSERT INTO casino_stats(user_id,games,wins,wagered,profit)
                    VALUES(?,1,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        games=games+1,
                        wins=wins+excluded.wins,
                        wagered=wagered+excluded.wagered,
                        profit=profit+excluded.profit
                    """,
                    (str(user_id), won, int(wager), profit),
                )

    def instant_game(self, user_id, stake: int, payout: int, game: str) -> int:
        self.change_balance(user_id, -stake, f"{game}_stake")
        balance = (
            self.change_balance(user_id, payout, f"{game}_payout")
            if payout
            else self.balance(user_id)
        )
        self.update_stats(user_id, stake, payout)
        return balance

    def daily(self, user_id):
        now = int(time.time())
        with self.lock:
            with self.conn() as con:
                self._ensure_user(user_id, con)
                row = con.execute(
                    "SELECT balance,last_daily FROM users WHERE user_id=?",
                    (str(user_id),),
                ).fetchone()
                remaining = int(row["last_daily"]) + 86400 - now
                if remaining > 0:
                    return False, int(row["balance"]), remaining
                new_balance = int(row["balance"]) + self.daily_credits
                con.execute(
                    "UPDATE users SET balance=?,last_daily=? WHERE user_id=?",
                    (new_balance, now, str(user_id)),
                )
                con.execute(
                    "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,NULL,?)",
                    (str(user_id), self.daily_credits, "daily_bonus", now),
                )
                return True, new_balance, 0

    def leaderboard(self, limit=10):
        with self.lock:
            with self.conn() as con:
                return con.execute(
                    "SELECT user_id,balance FROM users ORDER BY balance DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()

    def casino_stats(self, user_id):
        with self.lock:
            with self.conn() as con:
                return con.execute(
                    "SELECT games,wins,wagered,profit FROM casino_stats WHERE user_id=?",
                    (str(user_id),),
                ).fetchone()

    # Blackjack stake lock is atomic with session creation.
    def reserve_blackjack(self, user_id, stake: int):
        with self.lock:
            with self.conn() as con:
                self._ensure_user(user_id, con)
                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (str(user_id),)
                ).fetchone()
                if int(row["balance"]) < stake:
                    raise RuntimeError("Yetersiz kredi.")
                con.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=?",
                    (stake, str(user_id)),
                )
                con.execute(
                    "INSERT OR REPLACE INTO blackjack_sessions(user_id,stake,created_at) VALUES(?,?,?)",
                    (str(user_id), stake, int(time.time())),
                )
                con.execute(
                    "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,?,?)",
                    (str(user_id), -stake, "blackjack_stake", None, int(time.time())),
                )

    def update_blackjack_stake(self, user_id, stake: int):
        with self.lock:
            with self.conn() as con:
                con.execute(
                    "UPDATE blackjack_sessions SET stake=? WHERE user_id=?",
                    (int(stake), str(user_id)),
                )

    def clear_blackjack(self, user_id):
        with self.lock:
            with self.conn() as con:
                con.execute(
                    "DELETE FROM blackjack_sessions WHERE user_id=?",
                    (str(user_id),),
                )

    # Coupon administration / settlement.
    def create_match(self, guild_id: int, home: str, away: str, start_time: str) -> int:
        with self.lock:
            with self.conn() as con:
                cur = con.execute(
                    "INSERT INTO matches(guild_id,home,away,start_time,status,created_at) VALUES(?,?,?,?, 'open', ?)",
                    (str(guild_id), home.strip(), away.strip(), start_time.strip(), int(time.time())),
                )
                return int(cur.lastrowid)

    def create_market(self, match_id: int, market: str, option: str, odds: float) -> int:
        odds = float(odds)
        if odds < 1.01:
            raise RuntimeError("Oran en az 1.01 olmalı.")
        with self.lock:
            with self.conn() as con:
                match = con.execute(
                    "SELECT status FROM matches WHERE id=?", (int(match_id),)
                ).fetchone()
                if not match:
                    raise RuntimeError("Maç bulunamadı.")
                if match["status"] != "open":
                    raise RuntimeError("Maç açık değil.")
                cur = con.execute(
                    "INSERT INTO markets(match_id,market_name,option_name,odds,result,created_at) VALUES(?,?,?,?, 'pending', ?)",
                    (int(match_id), market.strip(), option.strip(), odds, int(time.time())),
                )
                return int(cur.lastrowid)

    def open_markets(self, guild_id: int):
        with self.lock:
            with self.conn() as con:
                return con.execute(
                    """
                    SELECT m.id match_id,m.home,m.away,m.start_time,
                           mk.id market_id,mk.market_name,mk.option_name,mk.odds
                    FROM matches m JOIN markets mk ON mk.match_id=m.id
                    WHERE m.guild_id=? AND m.status='open' AND mk.result='pending'
                    ORDER BY m.id DESC,mk.market_name,mk.id LIMIT 100
                    """,
                    (str(guild_id),),
                ).fetchall()

    def place_coupon(self, user_id: int, guild_id: int, stake: int, market_ids: list[int]):
        stake = int(stake)
        if stake < self.min_coupon_stake:
            raise RuntimeError(f"Minimum kupon {fmt_credit(self.min_coupon_stake)} kredi.")
        ids = list(dict.fromkeys(int(x) for x in market_ids))
        if not ids:
            raise RuntimeError("En az bir market seç.")
        if len(ids) > 15:
            raise RuntimeError("Bir kuponda en fazla 15 seçim olabilir.")

        placeholders = ",".join("?" for _ in ids)
        with self.lock:
            with self.conn() as con:
                self._ensure_user(user_id, con)
                rows = con.execute(
                    f"""
                    SELECT mk.id,mk.match_id,mk.market_name,mk.option_name,mk.odds,mk.result,
                           m.guild_id,m.status,m.home,m.away
                    FROM markets mk JOIN matches m ON m.id=mk.match_id
                    WHERE mk.id IN ({placeholders})
                    """,
                    tuple(ids),
                ).fetchall()
                if len(rows) != len(ids):
                    raise RuntimeError("Marketlerden biri bulunamadı.")

                seen = set()
                total = 1.0
                for row in rows:
                    if str(row["guild_id"]) != str(guild_id):
                        raise RuntimeError("Başka sunucunun marketi seçilemez.")
                    if row["status"] != "open" or row["result"] != "pending":
                        raise RuntimeError(f"Market #{row['id']} artık açık değil.")
                    key = (int(row["match_id"]), str(row["market_name"]).lower())
                    if key in seen:
                        raise RuntimeError("Aynı maçın aynı marketinden iki seçim yapılamaz.")
                    seen.add(key)
                    total *= float(row["odds"])

                bal = con.execute(
                    "SELECT balance FROM users WHERE user_id=?", (str(user_id),)
                ).fetchone()
                if int(bal["balance"]) < stake:
                    raise RuntimeError(f"Yetersiz kredi. Bakiyen: {fmt_credit(int(bal['balance']))}")

                total = round(total, 4)
                potential = int(round(stake * total))
                new_balance = int(bal["balance"]) - stake
                con.execute(
                    "UPDATE users SET balance=? WHERE user_id=?",
                    (new_balance, str(user_id)),
                )
                cur = con.execute(
                    """
                    INSERT INTO coupons(user_id,guild_id,stake,total_odds,potential_win,status,created_at)
                    VALUES(?,?,?,?,?,'open',?)
                    """,
                    (str(user_id), str(guild_id), stake, total, potential, int(time.time())),
                )
                coupon_id = int(cur.lastrowid)
                for row in rows:
                    con.execute(
                        "INSERT INTO coupon_items(coupon_id,market_id,option_name,odds) VALUES(?,?,?,?)",
                        (coupon_id, int(row["id"]), str(row["option_name"]), float(row["odds"])),
                    )
                con.execute(
                    "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,?,?)",
                    (str(user_id), -stake, "coupon_stake", str(coupon_id), int(time.time())),
                )
                return coupon_id, total, potential, new_balance, rows

    def settle_open_coupons(self):
        settled = []
        with self.lock:
            with self.conn() as con:
                coupons = con.execute(
                    "SELECT * FROM coupons WHERE status='open'"
                ).fetchall()
                now = int(time.time())
                for coupon in coupons:
                    items = con.execute(
                        """
                        SELECT ci.odds,mk.result FROM coupon_items ci
                        JOIN markets mk ON mk.id=ci.market_id WHERE ci.coupon_id=?
                        """,
                        (int(coupon["id"]),),
                    ).fetchall()
                    if not items:
                        continue
                    results = [x["result"] for x in items]
                    if "loss" in results:
                        con.execute(
                            "UPDATE coupons SET status='lost',settled_at=? WHERE id=?",
                            (now, int(coupon["id"])),
                        )
                        settled.append((int(coupon["id"]), "lost", 0))
                        continue
                    if "pending" in results:
                        continue

                    effective = 1.0
                    for item in items:
                        if item["result"] == "win":
                            effective *= float(item["odds"])
                    payout = int(round(int(coupon["stake"]) * effective))
                    self._ensure_user(coupon["user_id"], con)
                    con.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (payout, str(coupon["user_id"])),
                    )
                    con.execute(
                        "INSERT INTO transactions(user_id,amount,reason,ref,created_at) VALUES(?,?,?,?,?)",
                        (str(coupon["user_id"]), payout, "coupon_win", str(coupon["id"]), now),
                    )
                    con.execute(
                        "UPDATE coupons SET status='won',settled_at=? WHERE id=?",
                        (now, int(coupon["id"])),
                    )
                    settled.append((int(coupon["id"]), "won", payout))
        return settled

    def set_market_result(self, market_id: int, result: str):
        result = result.lower().strip()
        if result not in {"win", "loss", "void"}:
            raise RuntimeError("Sonuç win / loss / void olmalı.")
        with self.lock:
            with self.conn() as con:
                row = con.execute(
                    "SELECT id FROM markets WHERE id=?", (int(market_id),)
                ).fetchone()
                if not row:
                    raise RuntimeError("Market bulunamadı.")
                con.execute(
                    "UPDATE markets SET result=? WHERE id=?",
                    (result, int(market_id)),
                )
        return self.settle_open_coupons()

    def user_coupons(self, user_id: int, limit=10):
        with self.lock:
            with self.conn() as con:
                return con.execute(
                    """
                    SELECT id,stake,total_odds,potential_win,status
                    FROM coupons WHERE user_id=? ORDER BY id DESC LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()


def fmt_credit(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def remaining_text(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def is_admin(interaction: discord.Interaction, owner_id: int) -> bool:
    if owner_id and interaction.user.id == owner_id:
        return True
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


BLACKJACK_RANKS = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
BLACKJACK_SUITS = ["♠","♥","♦","♣"]


def bj_deck():
    deck = [f"{r}{s}" for s in BLACKJACK_SUITS for r in BLACKJACK_RANKS]
    SYS_RNG.shuffle(deck)
    return deck


def bj_value(card: str) -> int:
    rank = card[:-1]
    if rank == "A": return 11
    if rank in {"J", "Q", "K"}: return 10
    return int(rank)


def bj_hand(hand: list[str]) -> int:
    value = sum(bj_value(x) for x in hand)
    aces = sum(1 for x in hand if x[:-1] == "A")
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


@dataclass
class BlackjackGame:
    user_id: int
    channel_id: int
    stake: int
    deck: list[str]
    player: list[str]
    dealer: list[str]
    message_id: Optional[int] = None
    finished: bool = False
    result_text: str = ""


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "EconomyCog", user_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Bu blackjack masası sana ait değil.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        game = self.cog.blackjack_games.get(self.user_id)
        if not game or game.finished:
            return
        balance = self.cog.store.change_balance(game.user_id, game.stake, "blackjack_timeout_refund")
        game.finished = True
        game.result_text = f"⌛ Süre doldu — bahis iade edildi. Bakiye: {fmt_credit(balance)}"
        self.cog.store.clear_blackjack(game.user_id)
        self.cog.blackjack_games.pop(game.user_id, None)
        channel = self.cog.bot.get_channel(game.channel_id)
        if channel and game.message_id:
            try:
                msg = await channel.fetch_message(game.message_id)
                await msg.edit(embed=self.cog.bj_embed(game, True), view=None)
            except Exception:
                pass

    @discord.ui.button(label="Kart Çek", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.blackjack_games.get(self.user_id)
        if not game or game.finished:
            await interaction.response.send_message("Oyun aktif değil.", ephemeral=True)
            return
        game.player.append(game.deck.pop())
        value = bj_hand(game.player)
        if value > 21:
            self.cog.finish_bj(game, 0, f"💥 Bust ({value}). **-{fmt_credit(game.stake)}** kredi.")
            await interaction.response.edit_message(embed=self.cog.bj_embed(game, True), view=None)
        elif value == 21:
            await self.cog.bj_stand(game)
            await interaction.response.edit_message(embed=self.cog.bj_embed(game, True), view=None)
        else:
            await interaction.response.edit_message(embed=self.cog.bj_embed(game), view=self)

    @discord.ui.button(label="Dur", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.blackjack_games.get(self.user_id)
        if not game or game.finished:
            await interaction.response.send_message("Oyun aktif değil.", ephemeral=True)
            return
        await self.cog.bj_stand(game)
        await interaction.response.edit_message(embed=self.cog.bj_embed(game, True), view=None)

    @discord.ui.button(label="Double", emoji="✖️", style=discord.ButtonStyle.success)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.blackjack_games.get(self.user_id)
        if not game or game.finished:
            await interaction.response.send_message("Oyun aktif değil.", ephemeral=True)
            return
        if len(game.player) != 2:
            await interaction.response.send_message("Double sadece ilk iki kartta.", ephemeral=True)
            return
        if self.cog.store.balance(game.user_id) < game.stake:
            await interaction.response.send_message("Double için kredi yetersiz.", ephemeral=True)
            return
        extra = game.stake
        self.cog.store.change_balance(game.user_id, -extra, "blackjack_double_stake")
        game.stake *= 2
        self.cog.store.update_blackjack_stake(game.user_id, game.stake)
        game.player.append(game.deck.pop())
        if bj_hand(game.player) > 21:
            self.cog.finish_bj(game, 0, f"💥 Double sonrası bust. **-{fmt_credit(game.stake)}** kredi.")
        else:
            await self.cog.bj_stand(game)
        await interaction.response.edit_message(embed=self.cog.bj_embed(game, True), view=None)


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: EconomyStore, owner_id: int):
        self.bot = bot
        self.store = store
        self.owner_id = int(owner_id or 0)
        self.blackjack_games: dict[int, BlackjackGame] = {}

    def bj_embed(self, game: BlackjackGame, reveal=False):
        pv = bj_hand(game.player)
        dealer_cards = " ".join(game.dealer) if reveal or game.finished else f"{game.dealer[0]} 🂠"
        dv = str(bj_hand(game.dealer)) if reveal or game.finished else "?"
        e = discord.Embed(
            title="🃏 Blackjack",
            description=f"**Sen:** {' '.join(game.player)} → **{pv}**\n\n**Dealer:** {dealer_cards} → **{dv}**",
        )
        e.add_field(name="Bahis", value=f"{fmt_credit(game.stake)} kredi", inline=True)
        if game.finished:
            e.add_field(name="Sonuç", value=game.result_text, inline=False)
        return e

    def finish_bj(self, game: BlackjackGame, payout: int, result: str):
        if game.finished:
            return
        game.finished = True
        game.result_text = result
        if payout:
            self.store.change_balance(game.user_id, payout, "blackjack_payout")
        self.store.update_stats(game.user_id, game.stake, payout)
        self.store.clear_blackjack(game.user_id)
        self.blackjack_games.pop(game.user_id, None)

    async def bj_stand(self, game: BlackjackGame):
        while bj_hand(game.dealer) < 17:
            game.dealer.append(game.deck.pop())
        pv, dv = bj_hand(game.player), bj_hand(game.dealer)
        if dv > 21 or pv > dv:
            self.finish_bj(game, game.stake * 2, f"✅ Kazandın. Net **+{fmt_credit(game.stake)}** kredi.")
        elif pv == dv:
            self.finish_bj(game, game.stake, "🤝 Push — bahis iade edildi.")
        else:
            self.finish_bj(game, 0, f"❌ Dealer kazandı. **-{fmt_credit(game.stake)}** kredi.")

    @app_commands.command(name="bakiye", description="Sanal kredi bakiyeni gösterir.")
    async def bakiye(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        balance = self.store.balance(target.id)
        e = discord.Embed(title="💰 Bakiye", description=f"**{target.display_name}**\n{fmt_credit(balance)} kredi")
        e.set_footer(text="Krediler yalnızca sunucu içi sanal puandır.")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="gunluk", description="24 saatte bir günlük kredi bonusu al.")
    async def gunluk(self, interaction: discord.Interaction):
        ok, balance, remaining = self.store.daily(interaction.user.id)
        if ok:
            await interaction.response.send_message(
                f"🎁 **+{fmt_credit(self.store.daily_credits)}** kredi. Yeni bakiye: **{fmt_credit(balance)}**"
            )
        else:
            await interaction.response.send_message(
                f"⌛ Tekrar almak için **{remaining_text(remaining)}** bekle.", ephemeral=True
            )

    @app_commands.command(name="liderlik", description="Kredi liderlik tablosu.")
    async def liderlik(self, interaction: discord.Interaction):
        rows = self.store.leaderboard(10)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            uid = int(row["user_id"])
            member = interaction.guild.get_member(uid) if interaction.guild else None
            name = member.display_name if member else f"User {uid}"
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{prefix} **{name}** — {fmt_credit(row['balance'])}")
        await interaction.response.send_message(
            embed=discord.Embed(title="🏆 Kredi Liderliği", description="\n".join(lines) or "Henüz kayıt yok.")
        )

    @app_commands.command(name="casino_stats", description="Casino istatistiklerini gösterir.")
    async def casino_stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        row = self.store.casino_stats(target.id)
        games, wins, wagered, profit = (0,0,0,0) if not row else tuple(int(row[x]) for x in ("games","wins","wagered","profit"))
        await interaction.response.send_message(embed=discord.Embed(
            title=f"🎰 {target.display_name} — Casino",
            description=(f"Oyun: **{games}**\nKazanan oyun: **{wins}**\nToplam bahis: **{fmt_credit(wagered)}**\nNet: **{profit:+,}** kredi".replace(",", ".")),
        ))

    @app_commands.command(name="coinflip", description="Yazı/tura oyna.")
    async def coinflip(self, interaction: discord.Interaction, bahis: int, secim: str):
        try:
            stake = self.store.validate_bet(interaction.user.id, bahis)
            choice = {"yazı":"yazi","yazi":"yazi","tura":"tura"}.get(secim.lower().strip(), secim.lower().strip())
            if choice not in {"yazi", "tura"}:
                raise RuntimeError("Seçim yazi veya tura olmalı.")
            result = SYS_RNG.choice(["yazi", "tura"])
            payout = stake * 2 if result == choice else 0
            balance = self.store.instant_game(interaction.user.id, stake, payout, "coinflip")
            net = payout - stake
            await interaction.response.send_message(
                f"🪙 **{'YAZI' if result=='yazi' else 'TURA'}**\nNet: **{net:+,}** kredi\nBakiye: **{fmt_credit(balance)}**".replace(",", ".")
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="slots", description="3 makaralı slot oyna.")
    async def slots(self, interaction: discord.Interaction, bahis: int):
        try:
            stake = self.store.validate_bet(interaction.user.id, bahis)
            symbols = ["🍒","🍋","🔔","⭐","7️⃣"]
            reels = [SYS_RNG.choices(symbols, weights=[32,28,20,14,6], k=1)[0] for _ in range(3)]
            if reels == ["7️⃣","7️⃣","7️⃣"]: mult = 10.0
            elif reels[0] == reels[1] == reels[2]: mult = 5.0
            elif reels.count("7️⃣") == 2: mult = 2.5
            elif len(set(reels)) == 2: mult = 1.5
            else: mult = 0.0
            payout = int(round(stake * mult))
            balance = self.store.instant_game(interaction.user.id, stake, payout, "slots")
            net = payout - stake
            await interaction.response.send_message(
                f"🎰 **{' | '.join(reels)}**\nÇarpan: **x{mult:g}**\nNet: **{net:+,}** kredi\nBakiye: **{fmt_credit(balance)}**".replace(",", ".")
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="roulette", description="Rulette renk, tek/çift veya sayı seç.")
    async def roulette(self, interaction: discord.Interaction, bahis: int, secim: str):
        try:
            stake = self.store.validate_bet(interaction.user.id, bahis)
            choice = {"kırmızı":"red","kirmizi":"red","siyah":"black","tek":"odd","çift":"even","cift":"even"}.get(secim.lower().strip(), secim.lower().strip())
            number = SYS_RNG.randrange(37)
            color = "green" if number == 0 else ("red" if number in ROULETTE_REDS else "black")
            payout = 0
            if choice.isdigit():
                picked = int(choice)
                if not 0 <= picked <= 36:
                    raise RuntimeError("Sayı 0-36 arasında olmalı.")
                if picked == number: payout = stake * 36
            elif choice in {"red", "black"}:
                if choice == color: payout = stake * 2
            elif choice == "odd":
                if number and number % 2: payout = stake * 2
            elif choice == "even":
                if number and number % 2 == 0: payout = stake * 2
            else:
                raise RuntimeError("Seçim red, black, odd, even veya 0-36 sayı olmalı.")
            balance = self.store.instant_game(interaction.user.id, stake, payout, "roulette")
            emoji = "🟢" if color == "green" else ("🔴" if color == "red" else "⚫")
            net = payout - stake
            await interaction.response.send_message(
                f"🎡 **{number} {emoji}**\nNet: **{net:+,}** kredi\nBakiye: **{fmt_credit(balance)}**".replace(",", ".")
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="blackjack", description="Dealer'a karşı blackjack oyna.")
    async def blackjack(self, interaction: discord.Interaction, bahis: int):
        try:
            if interaction.user.id in self.blackjack_games:
                raise RuntimeError("Zaten aktif blackjack oyunun var.")
            stake = self.store.validate_bet(interaction.user.id, bahis)
            self.store.reserve_blackjack(interaction.user.id, stake)
            deck = bj_deck()
            game = BlackjackGame(interaction.user.id, interaction.channel_id, stake, deck, [deck.pop(),deck.pop()], [deck.pop(),deck.pop()])
            self.blackjack_games[interaction.user.id] = game
            p_bj = len(game.player) == 2 and bj_hand(game.player) == 21
            d_bj = len(game.dealer) == 2 and bj_hand(game.dealer) == 21
            if p_bj or d_bj:
                if p_bj and d_bj:
                    payout, text = stake, "🤝 İki taraf da blackjack — push."
                elif p_bj:
                    payout = int(round(stake * 2.5)); text = f"🖤 Blackjack! Net **+{fmt_credit(payout-stake)}** kredi."
                else:
                    payout, text = 0, f"❌ Dealer blackjack. **-{fmt_credit(stake)}** kredi."
                self.finish_bj(game, payout, text)
                await interaction.response.send_message(embed=self.bj_embed(game, True))
                return
            view = BlackjackView(self, interaction.user.id)
            await interaction.response.send_message(embed=self.bj_embed(game), view=view)
            msg = await interaction.original_response(); game.message_id = msg.id
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="maclar", description="Açık kupon marketlerini listeler.")
    async def maclar(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Sunucuda kullanılmalı.", ephemeral=True); return
        rows = self.store.open_markets(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("📭 Açık market yok."); return
        lines, current = [], None
        for row in rows:
            if current != int(row["match_id"]):
                current = int(row["match_id"])
                lines.append(f"\n**#{current} · {row['home']} - {row['away']}**" + (f" · {row['start_time']}" if row['start_time'] else ""))
            lines.append(f"`M{row['market_id']}` {row['market_name']} → **{row['option_name']} @{float(row['odds']):.2f}**")
        desc = "\n".join(lines)
        await interaction.response.send_message(embed=discord.Embed(title="🎫 Açık Marketler", description=desc[:4000]))

    @app_commands.command(name="kupon", description="Market ID'lerinden kupon yap. Örn: 12,15,18")
    async def kupon(self, interaction: discord.Interaction, bahis: int, marketler: str):
        if not interaction.guild:
            await interaction.response.send_message("Sunucuda kullanılmalı.", ephemeral=True); return
        try:
            ids = [int(x.strip().lstrip("mM")) for x in marketler.split(",") if x.strip()]
            cid,total,potential,newbal,rows = self.store.place_coupon(interaction.user.id, interaction.guild.id, bahis, ids)
            selections = "\n".join(
                f"• {r['home']} - {r['away']} · {r['market_name']}: **{r['option_name']} @{float(r['odds']):.2f}**" for r in rows
            )
            e = discord.Embed(title=f"🎫 Kupon #{cid}", description=selections)
            e.add_field(name="Bahis", value=f"{fmt_credit(bahis)} kredi", inline=True)
            e.add_field(name="Toplam oran", value=f"{total:.2f}", inline=True)
            e.add_field(name="Potansiyel", value=f"{fmt_credit(potential)} kredi", inline=True)
            e.add_field(name="Yeni bakiye", value=f"{fmt_credit(newbal)} kredi", inline=False)
            await interaction.response.send_message(embed=e)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="kuponlarim", description="Son kuponlarını gösterir.")
    async def kuponlarim(self, interaction: discord.Interaction):
        rows = self.store.user_coupons(interaction.user.id, 10)
        emojis = {"open":"🟡","won":"✅","lost":"❌"}
        lines = [
            f"{emojis.get(r['status'],'•')} **#{r['id']}** · {fmt_credit(r['stake'])} · @{float(r['total_odds']):.2f} · pot. {fmt_credit(r['potential_win'])}"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=discord.Embed(title="🎟️ Kuponlarım", description="\n".join(lines) or "Henüz kupon yok."),
            ephemeral=True,
        )

    @app_commands.command(name="mac_ekle", description="ADMIN: Kupon sistemi için maç ekler.")
    async def mac_ekle(self, interaction: discord.Interaction, ev: str, deplasman: str, baslangic: str = ""):
        if not interaction.guild or not is_admin(interaction, self.owner_id):
            await interaction.response.send_message("❌ Adminlere özel.", ephemeral=True); return
        mid = self.store.create_match(interaction.guild.id, ev, deplasman, baslangic)
        await interaction.response.send_message(f"✅ Maç: **#{mid} {ev} - {deplasman}**")

    @app_commands.command(name="market_ekle", description="ADMIN: Maça market seçeneği ekler.")
    async def market_ekle(self, interaction: discord.Interaction, mac_id: int, market: str, secenek: str, oran: float):
        if not is_admin(interaction, self.owner_id):
            await interaction.response.send_message("❌ Adminlere özel.", ephemeral=True); return
        try:
            market_id = self.store.create_market(mac_id, market, secenek, oran)
            await interaction.response.send_message(f"✅ `M{market_id}` **{market} → {secenek} @{oran:.2f}**")
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="market_sonuc", description="ADMIN: Marketi win/loss/void sonuçlandırır.")
    async def market_sonuc(self, interaction: discord.Interaction, market_id: int, sonuc: str):
        if not is_admin(interaction, self.owner_id):
            await interaction.response.send_message("❌ Adminlere özel.", ephemeral=True); return
        try:
            settled = self.store.set_market_result(market_id, sonuc)
            detail = "\n".join(
                f"Kupon #{cid}: {status}" + (f" · +{fmt_credit(payout)}" if payout else "")
                for cid,status,payout in settled[:20]
            ) or "Henüz sonuçlanan kupon yok."
            await interaction.response.send_message(f"✅ M{market_id} → **{sonuc.lower()}**\n{detail}")
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @app_commands.command(name="kredi_ver", description="ADMIN: Kullanıcıya sanal kredi verir.")
    async def kredi_ver(self, interaction: discord.Interaction, member: discord.Member, miktar: int):
        if not is_admin(interaction, self.owner_id):
            await interaction.response.send_message("❌ Adminlere özel.", ephemeral=True); return
        if miktar <= 0:
            await interaction.response.send_message("Miktar pozitif olmalı.", ephemeral=True); return
        bal = self.store.change_balance(member.id, miktar, "admin_credit_add", str(interaction.user.id))
        await interaction.response.send_message(f"✅ {member.mention} +{fmt_credit(miktar)} · bakiye **{fmt_credit(bal)}**")

    @app_commands.command(name="kredi_al", description="ADMIN: Kullanıcıdan sanal kredi düşer.")
    async def kredi_al(self, interaction: discord.Interaction, member: discord.Member, miktar: int):
        if not is_admin(interaction, self.owner_id):
            await interaction.response.send_message("❌ Adminlere özel.", ephemeral=True); return
        try:
            if miktar <= 0: raise RuntimeError("Miktar pozitif olmalı.")
            bal = self.store.change_balance(member.id, -miktar, "admin_credit_remove", str(interaction.user.id))
            await interaction.response.send_message(f"✅ {member.mention} -{fmt_credit(miktar)} · bakiye **{fmt_credit(bal)}**")
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)


async def setup_economy(bot: commands.Bot, data_dir: Path, owner_id: int, env: dict):
    store = EconomyStore(
        data_dir,
        starting_credits=int(env.get("STARTING_CREDITS", 10000)),
        daily_credits=int(env.get("DAILY_CREDITS", 500)),
        min_casino_bet=int(env.get("MIN_CASINO_BET", 10)),
        max_casino_bet=int(env.get("MAX_CASINO_BET", 1_000_000)),
        min_coupon_stake=int(env.get("MIN_COUPON_STAKE", 50)),
    )
    await bot.add_cog(EconomyCog(bot, store, owner_id))
    return store