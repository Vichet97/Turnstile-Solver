<div align="center">
 
  <h2 align="center">Cloudflare - Turnstile Solver</h2>
  <p align="center">
A Python-based Turnstile solver using the patchright library, featuring multi-threaded execution, API integration, and support for different browsers. It solves CAPTCHAs quickly and efficiently, with customizable configurations and detailed logging.
    <br />
    <br />
    <a href="https://github.com/Theyka/Turnstile-Solver#-changelog">📜 ChangeLog</a>
    ·
    <a href="https://github.com/Theyka/Turnstile-Solver/issues">⚠️ Report Bug</a>
    ·
    <a href="https://github.com/Theyka/Turnstile-Solver/issues">💡 Request Feature</a>
  </p>

  <p align="center">
    <img src="https://img.shields.io/badge/LICENSE-CC%20BY%20NC%204.0-red?style=for-the-badge"/>
    <img src="https://img.shields.io/github/stars/Theyka/Turnstile-Solver.svg?style=for-the-badge&color=red"/>
    <img src="https://img.shields.io/github/issues/Theyka/Turnstile-Solver?style=for-the-badge&color=red"/>
    <a href="https://t.me/codarea">
     <img src="https://img.shields.io/badge/Telegram%20Channel-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white"/>
    </a>
  </p>
</div>

---

### 🎁 Donation

- **USDT (TRC20)**: ``TWXNQCnJESt6gxNMX5oHKwQzq4gsbdLNRh``
- **USDT (Arbitrum One)**: ``0xd8fd1e91c8af318a74a0810505f60ccca4ca0f8c``
- **BTC**: ``13iiMaYFpCfNdcyFycSdSVmD2yfQciD7AQ``
- **LTC**: ``LSrLQe2dfpDhGgVvDTRwW72fSyC9VsXp9g``

---

### ❓ Looking for a Cheap or Custom CAPTCHA Solution?
- Need cheap captcha solution as low as 0.1$ per 1k ? Contact me on Telegram:

  <a href="https://t.me/tlb_sh">
    <img src="https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white"/>
  </a>

---

### ❗ Disclaimers
- I am not responsible for anything that may happen, such as API Blocking, IP ban, etc.
- This was a quick project that was made for fun and personal use if you want to see further updates, star the repo & create an "issue" [here](https://github.com/Theyka/Turnstile-Solver/issues/)

---

### ⚙️ Installation Instructions

1. **Ensure Python 3.8+ is installed** on your system.

2. **Create a Python virtual environment**:
   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment**:
   - On **Windows**:
     ```bash
     venv\Scripts\activate
     ```
   - On **macOS/Linux**:
     ```bash
     source venv/bin/activate
     ```

4. **Install required dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

5. **Select the browser to install**:
   You can choose between **Chromium**, **Chrome**, **Edge**, **Camoufox**, or **SeleniumBase (Chrome backend)**:
   - To install **Chromium**:
     ```bash
     python -m patchright install chromium
     ```
   - To install **Chrome**:
     - On **macOS/Windows**: [Click here](https://www.google.com/chrome/)  
     - On **Linux (Debian/Ubuntu-based)**:
       ```bash
       apt update
       wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
       apt install -y ./google-chrome-stable_current_amd64.deb
       apt -f install -y  # Fix dependencies if needed
       rm ./google-chrome-stable_current_amd64.deb
       ```
   - To install **Edge**:
     ```bash
     python -m patchright install msedge
     ```
   - To install **Camoufox**:
     ```bash
     python -m camoufox fetch
     ```
   - To install **SeleniumBase**:
     ```bash
     pip install seleniumbase
     ```

6. **Start testing**:
   - Run the script (Check [🔧 Command line arguments](#-command-line-arguments) for better setup):
     ```bash
     python api_solver.py
     ```
     
---

### 🔧 Command line arguments
| Parameter     | Default   | Type      | Description                                                                                   |
|--------------|-----------|-----------|-----------------------------------------------------------------------------------------------|
| `--headless`   | `False`  | `boolean` | Runs the browser in headless mode. Requires the `--useragent` argument to be set.             |
| `--useragent`  | `None`   | `string`  | Specifies a custom User-Agent string for the browser. (No need to set if camoufox used)                                        |
| `--debug`      | `False`  | `boolean` | Enables debug mode for solver logs and Quart server diagnostics.                   |
| `--browser_type` | `seleniumbase`  | `string` | Specify the browser type for the solver. Supported options: chromium, playwright, chrome, msedge, camoufox, seleniumbase      |
| `--thread`     | `1`      | `integer` | Sets the number of browser threads to use in multi-threaded mode.                           |
| `--host`       | `127.0.0.1` | `string`  | Specifies the IP address the API solver runs on.                                            |
| `--port`       | `5000`   | `integer` | Sets the port the API solver listens on.                                                    |
| `--proxy`       | `False`   | `boolean` | Select a random proxy from proxies.txt for solving captchas                                                   |

**SeleniumBase notes:** `--browser_type seleniumbase` uses SeleniumBase's Chrome backend and launches browsers per request (no persistent pool). `reverse_proxy` is supported in best-effort mode for SeleniumBase by bootstrapping through the worker URL and applying target-domain cookies from worker `Set-Cookie` headers when needed.

---

### 🐳 Docker Image
#### Running the Container
To start the container, use:
- Change the TZ environment variable and ports to the correct one for yourself:
```sh
docker run -d --user 0:0 -p 3389:3389 -p 5000:5000 \
  -e TZ=Asia/Baku \
  -e RUN_API_SOLVER=true \
  -e ENABLE_RDP_WITH_API=true \
  --name turnstile_solver theyka/turnstile_solver:latest
```

Virtual GUI defaults for Docker API mode (`RUN_API_SOLVER=true`) are tuned for Turnstile visibility:
- `XVFB_SCREEN_WIDTH=1920`
- `XVFB_SCREEN_HEIGHT=1080`
- `XVFB_SCREEN_DEPTH=24`
- `XVFB_DPI=96`
- `SOLVER_VIEWPORT_WIDTH=1920`
- `SOLVER_VIEWPORT_HEIGHT=1080`
- `SOLVER_DEVICE_SCALE_FACTOR=1.0`
- `SELENIUMBASE_PREWARM=true` (startup prewarm)
- `SELENIUMBASE_PREFETCH_DRIVER=true` (prefetch chromedriver/uc_driver in `run.sh`)
- `SELENIUMBASE_UC` optional override (`true`/`false`, default `false` for stability)

If you need to view the GUI via RDP while API mode is running, enable:
- `ENABLE_RDP_WITH_API=true`
- run container as root (`user: "0:0"` in compose)

#### Connecting to the Container
1. Use an **RDP client** (like Windows Remote Desktop, Remmina, or FreeRDP)
2. Connect to `localhost:3389`
3. Login with the default user:
   - **Username:** root
   - **Password:** root
4. After this, you can start the solver by navigating to the `Turnstile-Solver` folder.

---

### 📡 API Documentation
#### Solve turnstile
```http
  GET /turnstile?url=https://example.com&sitekey=0x4AAAAAAA
```
#### Request Parameters:
| Parameter  | Type    | Description                                                                 | Required |
|------------|---------|-----------------------------------------------------------------------------|----------|
| `url`      | string  | The target URL containing the CAPTCHA. (e.g., `https://example.com`) | Yes      |
| `sitekey`  | string  | The site key for the CAPTCHA to be solved. (e.g., `0x4AAAAAAA`) | Yes      |
| `action`   | string  | Action to trigger during CAPTCHA solving, e.g., `login`            | No       |
| `cdata`    | string  | Custom data that can be used for additional CAPTCHA parameters.    | No       |
| `timeout`  | number  | Max solve time in seconds. | No |
| `proxy`    | string  | Outbound proxy for this job. Supported schemes: `http`, `https`, `socks5`, and `socks4`. Supported formats: `scheme://host:port`, `scheme://user:pass@host:port`, `scheme:host:port`, `scheme:host:port:user:pass`, or `scheme:user:pass:host:port` (examples: `?proxy=http:user:pass:ip:port`, `?proxy=http://user:pass@ip:port`, `?proxy=https:user:pass:ip:port`). SOCKS auth like `?proxy=socks5:user:pass:ip:port` is parsed, but Chromium cannot use authenticated SOCKS; use unauthenticated `socks5:ip:port`, HTTP(S) auth, or camoufox. | No |
| `reverse_proxy` | string | Worker base URL. Optional path suffix **`/SCHEMA`** (case-sensitive): strip it from the base and force **full** tails (`…/https://goplay.ml/`). Without **`/SCHEMA`**, default tails omit the scheme (`…/goplay.ml/`); override with `reverse_proxy_style=full` if needed. | No |
| `reverse_proxy_style` | string | `host` (default) = tail is `hostname/path…` (no `https://` in path). `full` = tail includes scheme. Ignored when `reverse_proxy` ends with **`/SCHEMA`**. | No |

**Environment:** Optional `ALLOWED_REVERSE_PROXY_HOSTS` — comma-separated hostnames allowed for `reverse_proxy` (e.g. `as.mykhcdn.workers.dev`). If unset, any host is allowed.

`REVERSE_PROXY_BYPASS_HOSTS` controls hosts that stay direct even when `reverse_proxy` is enabled. Default: `challenges.cloudflare.com`, because Cloudflare Turnstile assets commonly return `403` when fetched through a generic worker prefix.

When `reverse_proxy` is enabled, the solver fetches the worker URL internally and fulfills the original browser request instead of navigating the browser to the worker URL. This keeps cookies scoped to the protected target host (for example `.goplay.ml`) so target-domain session cookies are accepted by Chromium.

Prefix-style reverse proxy examples:

```http
GET /turnstile?url=https%3A%2F%2Fhttpbin.org%2Fheaders&timeout=90&reverse_proxy=https%3A%2F%2Fas.mykhcdn.workers.dev%2F
```

With the default `reverse_proxy_style=host`, browser requests are routed like:

```text
https://httpbin.org/headers -> https://as.mykhcdn.workers.dev/httpbin.org/headers
```

Use `reverse_proxy_style=full` (or end `reverse_proxy` with `/SCHEMA`) only when the worker expects the scheme in the path:

```text
https://httpbin.org/headers -> https://as.mykhcdn.workers.dev/https://httpbin.org/headers
```

#### Response:

If the request is successfully received, the server will respond with a `task_id` for the CAPTCHA solving task:

```json
{
  "task_id": "d2cbb257-9c37-4f9c-9bc7-1eaee72d96a8"
}
```

#### Get Result
```http
  GET /result?id=f0dbe75b-fa76-41ad-89aa-4d3a392040af
```

#### Request Parameters:

| Parameter  | Type    | Description                                                                 | Required |
|------------|---------|-----------------------------------------------------------------------------|----------|
| `id`       | string  | The unique task ID returned from the `/turnstile` request.                   | Yes      |

#### Response:

If the CAPTCHA is solved successfully, the server will respond with the following information:

```json
{
  "elapsed_time": 7.625,
  "value": "0.KBtT-r"
}
```

---

### 🎉 Sponsor
<a href="https://dashboard.capsolver.com/passport/register?inviteCode=7_Dvkat0RVqc">
    <img src="https://github.com/user-attachments/assets/176d2a43-2d08-4aa6-bc9d-5e1eb5c3d1a4" alt="Description">
</a>

---

Inspired by [Turnaround](https://github.com/Body-Alhoha/turnaround)
Original code by [Theyka](https://github.com/Theyka/Turnstile-Solver)
Changes by [Sexfrance](https://github.com/sexfrance)
