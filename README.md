# Maqnobot

Discord bot with music, browser moderation panel, chat chess, virtual credits, coupons and casino-style minigames.

> Credits are server-only virtual points. The bot does not implement purchasing credits, cash-out, or real-world prize redemption.

## Features

- Music: play/search/queue/pause/resume/skip/loop/seek/volume/playlists
- Web moderation panel
- Chat chess: `/chess`, `/move`, PGN
- Virtual economy: `/bakiye`, `/gunluk`, `/liderlik`
- Casino minigames: `/blackjack`, `/roulette`, `/slots`, `/coinflip`
- Manual coupon system: `/maclar`, `/kupon`, `/kuponlarim`
- Admin coupon commands: `/mac_ekle`, `/market_ekle`, `/market_sonuc`
- Admin credits: `/kredi_ver`, `/kredi_al`

## Install

```bash
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the token, guild/owner IDs and panel settings.

## Run

```bash
python main.py
```

## Linux/VPS extras

Install FFmpeg with your distro package manager and set:

```env
FFMPEG_PATH=ffmpeg
```

Stockfish is optional. If installed, set `STOCKFISH_PATH`; otherwise the built-in lightweight chess opponent is used.

## Discord Developer Portal

Enable at least:
- Message Content Intent
- Server Members Intent

Give the bot the permissions needed for voice/music and any moderation actions you want to use.