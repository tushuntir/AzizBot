# Reel & Music Telegram Bot

- Send an Instagram reel/post link → video + a "Download audio" button (MP3).
- Send any text → paged music search results (YouTube); tap one to get the MP3.
- `/round` → bot asks for a video, then sends it back as a Telegram circle video (square-cropped, 640px, max 60 sec; input up to 20 MB).
- Languages: English, O'zbekcha, Русский. Users pick on first /start and can change with /lang.
  Their choice is saved in SQLite. To add a language, add a block to `strings.py` and a name in `LANG_NAMES`.

## Deploy on your server (Docker)
```bash
unzip reelbot.zip && cd reelbot
cp .env.example .env && nano .env      # paste BOT_TOKEN from @BotFather
docker compose up -d --build
docker compose logs -f                 # check it started
```
Update later: `docker compose up -d --build` (use `--no-cache` to pull the latest yt-dlp).
Stop: `docker compose down` (user language data stays in the `botdata` volume).

## Run without Docker
Install Python 3.10+ and ffmpeg, then:
```bash
pip install -r requirements.txt
cp .env.example .env   # set BOT_TOKEN
python bot.py
```

## Notes
- Telegram's cloud Bot API caps uploads at 50 MB.
- If Instagram blocks downloads, export cookies (Netscape format) from a throwaway account,
  save as `cookies.txt` next to docker-compose.yml, uncomment the volume line, and set
  `COOKIES_FILE=/app/cookies.txt`.
- yt-dlp breaks now and then; rebuild with `--no-cache` to update it.
- Only download content you have the right to download.
