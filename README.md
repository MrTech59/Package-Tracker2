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

Then edit the config and restart:

```bash
sudo nano /opt/package-tracker/.env
sudo systemctl restart package-tracker
journalctl -u package-tracker -f
```

The service is bound to `0.0.0.0:8080` by default, so reach it at
`http://<lxc-ip>:8080`.

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

1. Create a bot with [@BotFather](https://t.me/botfather) and copy the token.
2. Send your bot any message (a bot cannot message you first).
3. Get your chat id: `curl "https://api.telegram.org/bot<TOKEN>/getUpdates"` and
   look for `"chat":{"id":...}`.
4. Set the values:

```ini
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

Use **Settings → Send test message** in the UI to confirm.

### Optional API key

```ini
API_KEY=some-long-random-string
```

When set, every `/api/*` route requires `X-Api-Key: <value>`. `/api/health`
stays public so uptime checks keep working.

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
install.sh             LXC systemd installer
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
Confirm `TELEGRAM_ENABLED=true`, and that you have messaged the bot at least
once. Test with Settings → Send test message. Notifications fire only on an
actual status *change*, not on every poll.

**Status never advances**
Background polling also skips terminal states and honours
`MIN_SCRAPE_GAP_SECONDS`. Use **Check now** to force a scrape.

**Reset everything**
Stop the service and delete `/opt/package-tracker/data/tracker.db`.

---

## License

MIT — do whatever you like with it.
