# 📦 Package Tracker

A self-hosted shipment tracker for a Proxmox LXC. Tracks deliveries, scrapes
carrier status pages **through your MCP browser server**, and sends **Telegram**
notifications when a package changes status.

No build step, no external tracking API, no cloud account. One Python process and
a SQLite file.

![status](https://img.shields.io/badge/status-working-brightgreen)

---

## Features

| | |
|---|---|
| **Status tracking** | Ordered → Label created → Shipped → In transit → Out for delivery → Delivered, plus Delayed / Exception / Returned / Cancelled |
| **Scraping via MCP** | Drives your `picoclaw-browser` MCP server (`browser_navigate`, `browser_read`) to read carrier tracking pages |
| **Telegram alerts** | HTML-formatted notification with carrier, tracking number and a link on every status change |
| **Summary dashboard** | In flight · Out for delivery · Delivered · Needs attention · Total, plus "Arriving soon" |
| **Search & filter** | Live search across name / tracking / notes, filter by status and carrier, 4 sort modes |
| **Auto tracking links** | Click through to the carrier's own tracking page |
| **Import / export** | Back up and restore the whole database as JSON |
| **History timeline** | Every status change and scrape is logged per package |
| **Diagnostics** | Built-in MCP + Telegram connectivity checks and a Telegram test button |
| **Optional auth** | Shared-secret `X-Api-Key` gate for the API |
| **PWA-friendly** | Manifest + favicon so it installs cleanly on a phone |

---

## How it works

```
Browser ──HTTP──> FastAPI ──> SQLite
                     │
                     ├──> MCP browser server (192.168.3.137:8000/mcp)
                     │       browser_navigate → carrier page or 17track
                     │       browser_read     → visible page text
                     │       parser           → normalized Status
                     │
                     └──> Telegram Bot API  → chat notification
```

1. A background loop (default: hourly) picks up every package that is still in
   flight and has a tracking number.
2. For each one it opens a short-lived MCP session, navigates to the carrier's
   tracking page and reads the visible text.
3. The text is parsed into a normalized status, ETA and summary.
4. If the status changed, an event is logged and a Telegram message is sent.
5. Terminal states (delivered/cancelled/returned) stop being polled.

---

## Quick start (local)

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env      # then edit MCP_URL and Telegram settings
python run.py
```

Open <http://localhost:8080>.

Verify your MCP server is reachable and the scrape pipeline works:

```bash
python scripts/verify_mcp.py
```
---

## Deploy to a Proxmox LXC

Create a Debian 12 or Ubuntu 22.04+ LXC, then:

```bash
# copy the project into the container, e.g. to /root/package-tracker
cd /root/package-tracker
chmod +x install.sh
sudo ./install.sh
```

The installer:

- installs Python 3 + venv,
- creates a `package-tracker` system user,
- copies the app to `/opt/package-tracker`,
- builds a virtualenv and installs dependencies,
- creates `/opt/package-tracker/.env` from the example,
- registers and starts a hardened `systemd` unit.

Running it again over an existing install behaves like an update: it keeps
your `data/` and `.env`.

### Updating

```bash
cd /root/package-tracker   # the project dir you installed from
git pull                   # or copy the new version in
sudo ./install.sh --update
```

`--update` is safe by design:

- **Your data is never touched.** `data/tracker.db` and `.env` are preserved
  exactly as they are; only code (`app/`, `scripts/`, `run.py`,
  `requirements.txt`) is replaced.
- **A snapshot is taken first.** The database, `.env` and the old code go to
  `/var/backups/package-tracker/<timestamp>/` (the 5 most recent are kept).
- **It verifies the new version actually starts.** After restarting, the
  installer polls `/api/health`. If it doesn't come up healthy, it
  automatically **rolls back** to the previous code and database and restarts
  the old version.

Other modes:

```bash
sudo ./install.sh --uninstall   # stop + remove the unit, keep data/.env
sudo ./install.sh --help
```

Then edit the config and restart:

```bash
sudo nano /opt/package-tracker/.env
sudo systemctl restart package-tracker
journalctl -u package-tracker -f
```

The service is bound to `0.0.0.0:8080` by default, so reach it at
`http://<lxc-ip>:8080`.

The install is clean: `data/` starts empty, so the tracker opens with no
packages. Cached bytecode from the source machine is stripped during install.

To check MCP connectivity from the LXC, use the installed virtualenv:

```bash
cd /opt/package-tracker && ./venv/bin/python scripts/verify_mcp.py
```

(or use **Settings → MCP browser server** in the dashboard, which does the same
check.)

---

## Configuration

All settings live in `.env` (see `.env.example`). The file is read on startup.

### MCP browser server

```ini
MCP_URL=http://192.168.3.137:8000/mcp
MCP_TIMEOUT_SECONDS=60
```

| Variable | Default | Purpose |
|---|---|---|
| `MCP_URL` | `http://192.168.3.137:8000/mcp` | Streamable-HTTP MCP endpoint |
| `MCP_TIMEOUT_SECONDS` | `60` | Per-request timeout driving the browser |
| `MCP_PROTOCOL_VERSION` | `2024-11-05` | Handshake protocol version |

### Scraping

| Variable | Default | Purpose |
|---|---|---|
| `SCRAPE_ENABLED` | `true` | Set `false` for manual-only mode (no outbound requests) |
| `POLL_INTERVAL_SECONDS` | `3600` | Background re-check interval |
| `MIN_SCRAPE_GAP_SECONDS` | `300` | Minimum gap between checks of the same package |
| `MAX_PAGE_CHARS` | `100000` | Cap on page text handed to the parser |

### Telegram

You need a bot token. Everything after that can be done from the dashboard —
no file editing and no shell access required.

**1. Create the bot.** Message [@BotFather](https://t.me/botfather), run
`/newbot`, and copy the token.

**2. Paste it into the dashboard.** Open **⚙︎ Settings → Telegram pairing** and
paste the token into the **Bot token** box, then press **Save token**. The
server verifies it against Telegram straight away, so a typo is reported
immediately rather than silently failing later.

The token is saved in two places, which matters:

| Where | Why |
|---|---|
| the database (`data/tracker.db`) | the running process picks it up on restart even if nothing else changed |
| `TELEGRAM_BOT_TOKEN` in `.env` | survives a full reinstall, and `systemctl restart` / the installer see it |

The field is masked, the token is never sent back to the browser, and
**Forget token** removes the dashboard copy (falling back to whatever `.env`
provides).

**3. Pair a chat.** Send `/start` to your bot in Telegram, then press
**Pair automatically**. The server watches `getUpdates` for up to 20 seconds,
links whichever chat wrote in, stores the chat id in the database, and sends a
confirmation message.

You can also press **Enter chat ID** and paste a numeric id if you already know
it (useful for group chats).

**Prefer .env?** Set the token there before first start and it is used as-is:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
```

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather; the dashboard can set this instead |
| `TELEGRAM_CHAT_ID` | *(empty)* | Chat to notify; only needed if you skip dashboard pairing |
| `TELEGRAM_ENABLED` | *(auto)* | Setting `false` explicitly keeps alerts off even when paired |
| `TELEGRAM_TIMEOUT_SECONDS` | `15` | Per-request timeout for the Bot API |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | Point at a self-hosted Bot API server |

Leave `TELEGRAM_CHAT_ID` blank and leave `TELEGRAM_ENABLED` unset — pairing
turns alerts on for you. Alternatively set the chat explicitly:

```ini
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ENABLED=true
```

**Notes**

- A token saved from the dashboard **takes priority over** `TELEGRAM_BOT_TOKEN`
  in `.env`, and a chat paired from the dashboard **takes priority over**
  `TELEGRAM_CHAT_ID`. Both survive restarts.
- **Unpair** clears the dashboard pairing and falls back to `TELEGRAM_CHAT_ID`.
- **Forget token** clears the dashboard token and falls back to `.env`.
- Pairing auto-enables notifications *unless* you explicitly set
  `TELEGRAM_ENABLED=false`, which is treated as a deliberate "keep alerts off".
- Use **Settings → Send test message** to confirm delivery.
- `TELEGRAM_API_BASE` (default `https://api.telegram.org`) points the
  integration at a different Bot API server if you run one locally.

### Optional API key

```ini
API_KEY=some-long-random-string
```

When set, every `/api/*` route requires `X-Api-Key: <value>` — including the
Telegram pairing routes. `/api/health` stays public so uptime checks keep
working.

---

## Scraping notes

This project reads **visible page text**, so what it can see depends on how each
carrier renders its tracking page.

- **17track is the reliable source.** It renders results server-side, and the
  parser is tested against its real output. Packages with an unlisted carrier are
  routed here automatically.
- **Direct carrier pages** are attempted first for FedEx, Royal Mail, DHL,
  DPD and 17track itself, then fall back to 17track.
- **USPS, UPS and some others render results inside an iframe** or behind
  anti-bot checks. The MCP server has no iframe-capable tool, so those pages
  read as empty and fall back to 17track. This is a limitation of the browser
  server, not of the parser.
- 17track prints a status legend (`Delivered (0)`, `In transit (1)`) on every
  page. The parser strips it so a zero-count legend is never mistaken for a real
  status — this is covered by `tests/test_17track.py`.

Because carrier markup changes, treat scraped statuses as best-effort. The UI
tells you when a scrape was inconclusive and never downgrades a status on an
inconclusive read. Set `SCRAPE_ENABLED=false` if you would rather update statuses
by hand.

---

## API

All routes are under `/api`. Add `X-Api-Key` when `API_KEY` is set.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + capability summary (always public) |
| `GET` | `/api/config` | Carriers, statuses, feature flags |
| `GET` | `/api/packages` | List, with `?q=`, `?status=`, `?carrier=` |
| `POST` | `/api/packages` | Create |
| `GET` | `/api/packages/{id}` | Fetch one |
| `PATCH` | `/api/packages/{id}` | Update fields (status routed through the change pipeline) |
| `DELETE` | `/api/packages/{id}` | Delete |
| `POST` | `/api/packages/{id}/status` | Set status, optionally notify |
| `GET` | `/api/packages/{id}/events` | Per-package history |
| `POST` | `/api/packages/{id}/refresh` | Scrape one (`?force=`, `?notify=`) |
| `POST` | `/api/refresh` | Scrape all active packages |
| `GET` | `/api/stats` | Dashboard counts |
| `GET` | `/api/events` | Recent activity feed |
| `GET` | `/api/diagnostics/mcp` | MCP reachability + tool list |
| `GET` | `/api/diagnostics/telegram` | Bot + chat validation |
| `POST` | `/api/diagnostics/telegram/test` | Send a test message |
| `GET` | `/api/telegram/pair` | Current pairing state |
| `PUT` | `/api/telegram/token` | Save + verify the bot token (dashboard path) |
| `DELETE` | `/api/telegram/token` | Forget the dashboard-saved token |
| `POST` | `/api/telegram/pair/auto` | Wait for a message, then pair that chat |
| `POST` | `/api/telegram/pair` | Pair a specific chat id (may include a token) |
| `DELETE` | `/api/telegram/pair` | Unpair (fall back to `TELEGRAM_CHAT_ID`) |
| `GET` | `/api/export` | Download JSON |
| `POST` | `/api/import` · `/api/import/raw` | Restore JSON |

Example:

```bash
curl -X POST http://localhost:8080/api/packages \
  -H 'Content-Type: application/json' \
  -d '{"name":"Raspberry Pi 5","tracking_number":"1Z999AA10123456784","carrier":"ups"}'
```

---

## Development

```bash
# run all tests
.venv/Scripts/python -m unittest discover -s tests -v      # Windows
.venv/bin/python -m unittest discover -s tests -v          # Linux/macOS

# run the app with reload
.venv/Scripts/python -m uvicorn app.main:app --reload
```

### Project layout

```
app/
  config.py          environment-driven settings
  db.py              SQLite schema + access (WAL, foreign keys)
  models.py          Status/Carrier enums, tracking URLs
  mcp_client.py      MCP streamable-HTTP client (handshake, SSE, tools/call)
  notifier.py        Telegram Bot API
  service.py         business logic: status pipeline, notifications, polling
  tracker/
    parser.py        page text -> normalized status, ETA, summary
    scraper.py       drives the MCP browser tools
  static/            frontend (vanilla JS, no build step)
  main.py            FastAPI app + static hosting
scripts/verify_mcp.py  live MCP pipeline check
tests/                 42 unit tests
install.sh             LXC systemd installer / updater (--update)
```

---

## Troubleshooting

**"MCP unreachable" in Settings**
Check `MCP_URL` and that the MCP server is listening. Confirm the host can reach
it: `curl http://192.168.3.137:8000/mcp` (a `400` about a missing session id
still proves it is up). Run `python scripts/verify_mcp.py`.

**Scrapes return "no readable text"**
That carrier renders in an iframe. It should fall back to 17track; check the
package's History panel to see which URL was actually read.

**No Telegram messages**
Pair a chat first (**Settings → Telegram pairing**, or set
`TELEGRAM_CHAT_ID`). Test with **Settings → Send test message**. Notifications
fire only on an actual status *change*, not on every poll.

Pairing errors are spelled out in the dialog — a `401` means the bot token in
`.env` is wrong or was revoked, and a `409` means something else is already
polling that bot with `getUpdates`.

**Status never advances**
Background polling also skips terminal states and honours
`MIN_SCRAPE_GAP_SECONDS`. Use **Check now** to force a scrape.

**Reset everything**
Stop the service and delete `/opt/package-tracker/data/tracker.db`.

---

## License

MIT — do whatever you like with it.
