# Turnstile Solver

A Python-based Cloudflare Turnstile solver with two usage styles:

1. **API mode** (recommended for integrations): async task endpoint + polling.
2. **Command mode** (direct solve): call sync/async solver functions directly.

---

## Table of Contents

- [What this project can return](#what-this-project-can-return)
- [Install](#install)
- [Mode 1: API mode](#mode-1-api-mode)
  - [Start API server](#start-api-server)
  - [API server CLI options](#api-server-cli-options)
  - [Endpoints](#endpoints)
  - [API request options (`/turnstile`)](#api-request-options-turnstile)
  - [API examples](#api-examples)
  - [Response/result modes](#responseresult-modes)
- [Mode 2: Command mode (direct solve)](#mode-2-command-mode-direct-solve)
  - [Interactive launcher](#interactive-launcher)
  - [Sync example](#sync-example)
  - [Async example](#async-example)
  - [Command-mode options](#command-mode-options)
- [Proxy and reverse-proxy details](#proxy-and-reverse-proxy-details)
- [Docker + RDP usage](#docker--rdp-usage)
  - [Compose defaults in this repo](#compose-defaults-in-this-repo)
  - [Docker runtime environment options](#docker-runtime-environment-options)
- [Troubleshooting](#troubleshooting)

---

## What this project can return

Depending on target behavior, the solver may return:

- **Turnstile token** (`value`)
- **Session cookies / headers capture** (when token is not directly exposed but useful cookies are available)
- **Failure payload** with reason + diagnostics

---

## Install

### 1) Python environment

```bash
python -m venv .venv
```

- Windows:
  ```bash
  .venv\Scripts\activate
  ```
- macOS/Linux:
  ```bash
  source .venv/bin/activate
  ```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

### 3) Install browser runtimes (as needed)

- Patchright Chromium (used by `chromium`, `chrome`, `msedge` modes):
  ```bash
  python -m patchright install chromium
  ```

- Native Playwright Chromium (used by `playwright` mode):
  ```bash
  python -m playwright install chromium
  ```

- Camoufox (optional):
  ```bash
  python -m camoufox fetch
  ```

- SeleniumBase (already in requirements):
  - driver binaries can auto-download on first run
  - optionally prefetch:
    ```bash
    sbase get chromedriver
    sbase get uc_driver
    ```

---

## Mode 1: API mode

### Start API server

```bash
python api_solver.py
```

Default bind is `127.0.0.1:5000`.

### API server CLI options

```bash
python api_solver.py --help
```

| Option | Default | Type | Description |
|---|---:|---|---|
| `--headless` | `False` | flag | Run browser headless. For non-camoufox browsers, you must also set `--useragent`. |
| `--useragent` | `None` | string | Custom User-Agent. |
| `--debug` | `False` | flag | Enable solver debug logs + Quart debug diagnostics. |
| `--browser_type` | `seleniumbase` | string | Default backend: `chromium`, `playwright`, `chrome`, `msedge`, `camoufox` (if installed), `seleniumbase` (if installed). |
| `--thread` | `1` | int | Browser worker count for pooled async backends. |
| `--proxy` | `False` | flag | Enable random proxy selection from `proxies.txt`. |
| `--host` | `127.0.0.1` | string | API bind host. |
| `--port` | `5000` | int/string | API bind port. |
| `--close-delay` | `0` | float seconds | Keep page open before cleanup (helps debugging visual flow). |

### Endpoints

- `GET /turnstile` → submit solve task (returns `202` + `task_id`)
- `GET /result?id=<task_id>` → fetch raw result payload
- `GET /describe?id=<task_id>` → fetch normalized summary/classification

### API request options (`/turnstile`)

| Query param | Required | Default | Description |
|---|---|---|---|
| `url` | Yes | — | Target URL. If scheme is omitted, `https://` is assumed. |
| `sitekey` | No | `None` | If provided, uses embedded widget flow. If omitted, solves on real page flow. |
| `action` | No | `None` | Extra Turnstile action (mainly for embedded/sitekey flow). |
| `cdata` | No | `None` | Extra Turnstile cdata (mainly for embedded/sitekey flow). |
| `timeout` | No | `45` | Per-task timeout in seconds. Clamped to max `86400`. |
| `browser` or `browser_type` | No | server default | Per-request backend override. Aliases accepted: `playwrite`→`playwright`, `selenium`/`sb`→`seleniumbase`. |
| `proxy` | No | `None` | Per-request outbound proxy override. Supports multiple formats (see [Proxy details](#proxy-and-reverse-proxy-details)). |
| `reverse_proxy` | No | `None` | Worker reverse-proxy base URL. Optional `/SCHEMA` suffix forces full-style tails. |
| `reverse_proxy_style` | No | `host` | `host` or `full`. Ignored when `reverse_proxy` ends with `/SCHEMA`. |

### API examples

#### A) Submit with explicit sitekey (embedded flow)

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://example.com&sitekey=0x4AAAAAAA"
```

#### B) Submit without sitekey (real-page flow)

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://goplay.ml"
```

#### C) Submit with timeout + browser override

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://example.com&sitekey=0x4AAAAAAA&timeout=90&browser=seleniumbase"
```

#### D) Submit with outbound proxy

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://example.com&sitekey=0x4AAAAAAA&proxy=http://user:pass@1.2.3.4:8080"
```

#### E) Submit with reverse proxy (host style)

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://goplay.ml&reverse_proxy=https://as.mykhcdn.workers.dev/"
```

#### F) Submit with reverse proxy (full style)

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://goplay.ml&reverse_proxy=https://as.mykhcdn.workers.dev/&reverse_proxy_style=full"
```

#### G) Submit with `/SCHEMA` marker (forces full style automatically)

```bash
curl "http://127.0.0.1:5000/turnstile?url=https://goplay.ml&reverse_proxy=https://as.mykhcdn.workers.dev/SCHEMA"
```

#### H) Poll result

```bash
curl "http://127.0.0.1:5000/result?id=YOUR_TASK_ID"
```

Simple bash polling example:

```bash
TASK_ID="YOUR_TASK_ID"
while true; do
  RES=$(curl -s "http://127.0.0.1:5000/result?id=${TASK_ID}")
  if [ "$RES" = "CAPTCHA_NOT_READY" ]; then
    sleep 1
    continue
  fi
  echo "$RES"
  break
done
```

Normalized summary:

```bash
curl "http://127.0.0.1:5000/describe?id=YOUR_TASK_ID"
```

### Response/result modes

Common `/result` states:

1. **Pending**
   - raw response can be string: `CAPTCHA_NOT_READY`
2. **Success token**
   - `result_mode: token_only`
3. **Session capture**
   - `result_mode: session_captured`
   - may contain cookies/headers even without Turnstile token field
4. **Failure**
   - HTTP `422`
   - `value: CAPTCHA_FAIL`
   - includes `reason`, `message`, and diagnostics when available

---

## Mode 2: Command mode (direct solve)

Command mode solves directly in process (no HTTP API).

### Interactive launcher

```bash
python main.py
```

You can choose:
1. Sync solver
2. Async solver
3. API server

### Sync example

```python
from sync_solver import get_turnstile_token

result = get_turnstile_token(
    url="https://www.crunchbase.com/login",
    sitekey="0x4AAAAAAAyJK2FfyvayqHnv",
    action=None,
    cdata=None,
    debug=True,
    headless=False,
    useragent=None,
    browser_type="seleniumbase",
)

print(result)
```

### Async example

```python
import asyncio
from async_solver import get_turnstile_token

async def main():
    result = await get_turnstile_token(
        url="https://www.crunchbase.com/login",
        sitekey="0x4AAAAAAAyJK2FfyvayqHnv",
        action=None,
        cdata=None,
        debug=True,
        headless=False,
        useragent=None,
        browser_type="seleniumbase",
    )
    print(result)

asyncio.run(main())
```

### Command-mode options

These are arguments for `sync_solver.get_turnstile_token()` and `async_solver.get_turnstile_token()`:

| Option | Required | Default | Description |
|---|---|---|---|
| `url` | Yes | — | Target page URL. |
| `sitekey` | Yes | — | Turnstile sitekey for embedded solve flow. |
| `action` | No | `None` | Optional Turnstile action. |
| `cdata` | No | `None` | Optional Turnstile cdata. |
| `debug` | No | `False` | Enable debug logs. |
| `headless` | No | `False` | Run headless (UA requirement still applies for non-camoufox). |
| `useragent` | No | `None` | Custom User-Agent. |
| `browser_type` | No | `seleniumbase` | `chromium`, `chrome`, `msedge`, `camoufox` (if installed), `seleniumbase` (if installed). |

---

## Proxy and reverse-proxy details

### Outbound proxy formats (`proxy`)

Supported schemes: `http`, `https`, `socks5`, `socks4`

Supported formats:

- `scheme://host:port`
- `scheme://user:pass@host:port`
- `scheme:host:port`
- `scheme:host:port:user:pass`
- `scheme:user:pass:host:port`

Examples:

- `http://1.2.3.4:8080`
- `http://user:pass@1.2.3.4:8080`
- `socks5:1.2.3.4:1080`

> Chromium-family backends (`chromium`, `chrome`, `msedge`, `seleniumbase`) do **not** support authenticated SOCKS proxies. Use unauthenticated SOCKS, HTTP(S) auth proxy, or `camoufox`.
>
> If your proxy username/password contains reserved URL characters (`@`, `:`, `#`, `&`, `%`, `+`, etc.), URL-encode the whole `proxy` query value when calling `/turnstile`.

### Reverse-proxy routing

- `reverse_proxy_style=host` (default):
  - `https://goplay.ml/path` → `https://worker/goplay.ml/path`
- `reverse_proxy_style=full`:
  - `https://goplay.ml/path` → `https://worker/https://goplay.ml/path`
- `reverse_proxy` ending with `/SCHEMA` forces `full` style.

Env controls:

- `ALLOWED_REVERSE_PROXY_HOSTS` (comma-separated whitelist)
- `REVERSE_PROXY_BYPASS_HOSTS` (default includes `challenges.cloudflare.com`)

---

## Docker + RDP usage

### Compose defaults in this repo

Current `docker-compose.yml` maps:

- API: `5510 -> 5000`
- RDP: `3333 -> 3389`
- UC mode: `SELENIUMBASE_UC=true` (explicit in compose)
- GUI click helper: `SELENIUMBASE_GUI_CLICK=false` (explicit in compose)

So use:

- API base URL: `http://localhost:5510`
- RDP target: `localhost:3333`

### Build + run

```bash
docker compose build --no-cache
docker compose up -d
docker compose logs -f --tail=200
```

### Docker runtime environment options

| Env var | Default | Description |
|---|---|---|
| `RUN_API_SOLVER` | `true/false` | `true`: run API server. `false`: start XRDP-only container mode. |
| `ENABLE_RDP_WITH_API` | `false` | Start XRDP services while API mode is running. |
| `XRDP_PASSWORD` | `root` | Sets root password for RDP login. |
| `SOLVER_DISPLAY_MODE` | `xvfb` | `xvfb`, `rdp`, or `auto`. `rdp` renders headed browser in XRDP session display. |
| `RDP_SESSION_WAIT_SECONDS` | `0` | Wait time for XRDP display detection in `rdp` mode. `0` = wait indefinitely. |
| `SOLVER_BROWSER_TYPE` | `seleniumbase` | Default solver backend for API startup. |
| `SOLVER_THREAD` | `1` | API worker count (for pooled async backends). |
| `SOLVER_HEADLESS` | `false` | Adds `--headless` to API command if true. |
| `SOLVER_DEBUG` | `false` | Adds `--debug` to API command if true. |
| `SOLVER_USERAGENT` | empty | Adds `--useragent` to API command when set. |
| `SELENIUMBASE_UC` | `false` (recommended) | UC mode toggle for SeleniumBase driver creation. |
| `SELENIUMBASE_PREWARM` | `true` | Prewarm SeleniumBase driver at startup. |
| `SELENIUMBASE_PREFETCH_DRIVER` | `true` | Run `sbase get chromedriver` / `uc_driver` prefetch on startup. |
| `SELENIUMBASE_GUI_CLICK` | `false` (recommended in Docker) | Enables SeleniumBase GUI click helpers; disabled by default for stability. |
| `XVFB_SCREEN_WIDTH` | `1920` | Xvfb virtual screen width. |
| `XVFB_SCREEN_HEIGHT` | `1080` | Xvfb virtual screen height. |
| `XVFB_SCREEN_DEPTH` | `24` | Xvfb color depth. |
| `XVFB_DPI` | `96` | Xvfb DPI. |
| `SOLVER_VIEWPORT_WIDTH` | follows `XVFB_SCREEN_WIDTH` | Browser viewport width. |
| `SOLVER_VIEWPORT_HEIGHT` | follows `XVFB_SCREEN_HEIGHT` | Browser viewport height. |
| `SOLVER_DEVICE_SCALE_FACTOR` | `1.0` | Browser DPR / scale factor. |
| `TURNSTILE_IP_PREFLIGHT_URL` | `https://api64.ipify.org?format=json` | Preflight URL used for IP routing diagnostics in Playwright-backed flows. |
| `ALLOWED_REVERSE_PROXY_HOSTS` | unset | Optional whitelist for `reverse_proxy` hostnames. |
| `REVERSE_PROXY_BYPASS_HOSTS` | `challenges.cloudflare.com` | Hosts that stay direct even in reverse-proxy mode. |

---

## Troubleshooting

### 1) `--headless` fails immediately
For non-camoufox backends, set a User-Agent:

```bash
python api_solver.py --headless --useragent "Mozilla/5.0 (...)" \
  --browser_type seleniumbase
```

### 2) API returns `CAPTCHA_NOT_READY` for a long time
- Poll `/result` until it changes.
- Increase `timeout` on `/turnstile`.
- Try another backend (`browser=playwright` or `browser=chromium`) for comparison.

### 3) SeleniumBase + UC instability in Docker
- Keep `SELENIUMBASE_GUI_CLICK=false` (recommended).
- Keep `SELENIUMBASE_PREWARM=true`.
- If needed, force `SELENIUMBASE_UC=false` for maximum stability.

### 4) Headed browser not visible in RDP
- Use `SOLVER_DISPLAY_MODE=rdp`.
- Ensure you are connected to XRDP first (or allow wait with `RDP_SESSION_WAIT_SECONDS=0`).
- Run non-headless mode.

### 5) Reverse-proxy issues
- Verify worker path style: `host` vs `full`.
- Try `/SCHEMA` suffix on `reverse_proxy` if worker expects `https://...` tail.
- Check `ALLOWED_REVERSE_PROXY_HOSTS` and `REVERSE_PROXY_BYPASS_HOSTS`.

### 6) SeleniumBase shows a proxy username/password popup
- Check startup/request logs for `proxy_auth=True` on accepted tasks.
- For authenticated HTTP(S) proxy with UC mode on recent Chrome, keep `SELENIUMBASE_UC=true` so SeleniumBase can use CDP-mode proxy-auth flow.
- If popup still appears, URL-encode the `proxy` value and retry.
- A/B test with `browser=playwright` using the same proxy to confirm credentials/network path.

---

## Notes

- API task state is persisted to `results.json`.
- `GET /describe` is useful for fast classification (`pending`, `ok`, `failure`) without parsing full raw payload.
- Browser availability depends on installed runtimes and optional packages.
