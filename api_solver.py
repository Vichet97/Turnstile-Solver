import os
import sys
import time
import uuid
import json
import random
import logging
import asyncio
import argparse
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from quart import Quart, request, jsonify
from patchright.async_api import async_playwright

# Optional camoufox import
try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    AsyncCamoufox = None
    CAMOUFOX_AVAILABLE = False


COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("TurnstileAPIServer")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Turnstile Solver</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
        <script>
            async function fetchIP() {
                try {
                    const response = await fetch('https://api64.ipify.org?format=json');
                    const data = await response.json();
                    document.getElementById('ip-display').innerText = `Your IP: ${data.ip}`;
                } catch (error) {
                    console.error('Error fetching IP:', error);
                    document.getElementById('ip-display').innerText = 'Failed to fetch IP';
                }
            }
            window.onload = fetchIP;
        </script>
    </head>
    <body>
        <!-- cf turnstile -->
        <p id="ip-display">Fetching your IP...</p>
    </body>
    </html>
    """

    def __init__(self, headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool):
        self.app = Quart(__name__)
        self.debug = debug
        self.results = self._load_results()
        self.browser_type = browser_type
        self.headless = headless
        self.useragent = useragent
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.browser_args = ["--disable-blink-features=AutomationControlled"]
        if useragent:
            self.browser_args.append(f"--user-agent={useragent}")

        self._setup_routes()

    @staticmethod
    def _normalize_page_url(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return url
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    def _browser_context_options(self, proxy: Optional[Dict[str, str]]) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "viewport": {"width": 1920, "height": 1080},
            "screen": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "light",
            "device_scale_factor": 1,
            "has_touch": False,
            "is_mobile": False,
        }
        if self.useragent:
            opts["user_agent"] = self.useragent
        if proxy:
            opts["proxy"] = proxy
        return opts

    @staticmethod
    async def _try_click_turnstile(page) -> None:
        iframe_selectors = (
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
            "iframe[title*='Cloudflare']",
        )
        for sel in iframe_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                await loc.wait_for(state="visible", timeout=5000)
                box = await loc.bounding_box()
                if box:
                    await page.mouse.click(
                        box["x"] + min(box["width"] / 2, 40),
                        box["y"] + min(box["height"] / 2, 35),
                    )
                    return
            except Exception:
                continue
        for sel in ("div.cf-turnstile", "[data-sitekey]", ".cf-turnstile"):
            try:
                await page.locator(sel).first.click(timeout=1200)
                return
            except Exception:
                continue

    @staticmethod
    def _format_cookie_header(cookies: List[Dict[str, Any]]) -> str:
        if not cookies:
            return ""
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))

    @staticmethod
    def _has_d_and_locl(cookies: List[Dict[str, Any]]) -> bool:
        names = {c.get("name") for c in cookies}
        return "d" in names and "locl" in names

    @staticmethod
    def _d_locl_cookie_header(cookies: List[Dict[str, Any]]) -> str:
        by = {c.get("name"): c.get("value", "") for c in cookies if c.get("name") in ("d", "locl")}
        parts = []
        if "d" in by:
            parts.append(f"d={by['d']}")
        if "locl" in by:
            parts.append(f"locl={by['locl']}")
        return "; ".join(parts)

    @staticmethod
    def _attach_http_capture(
        target: Dict[str, Any],
        last_document_request_headers: Dict[str, str],
        post_data_holder: List[Optional[str]],
    ) -> None:
        """`headers` = exact request headers from the last document navigation (e.g. after refresh)."""
        target["request_body"] = post_data_holder[0]
        target["headers"] = dict(last_document_request_headers)

    @staticmethod
    async def _read_turnstile_token(page) -> str:
        selectors = (
            '[name="cf-turnstile-response"]',
            "textarea[name='cf-turnstile-response']",
            "input[name='cf-turnstile-response']",
        )
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                v = await loc.input_value(timeout=1500)
                if v:
                    return v
            except Exception:
                continue
        try:
            v = await page.evaluate(
                """() => {
                    const el = document.querySelector('[name="cf-turnstile-response"]');
                    return el && el.value ? el.value : '';
                }"""
            )
            return v or ""
        except Exception:
            return ""

    @staticmethod
    def _load_results():
        """Load previous results from results.json."""
        try:
            if os.path.exists("results.json"):
                with open("results.json", "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading results: {str(e)}. Starting with an empty results dictionary.")
        return {}

    def _save_results(self):
        """Save results to results.json."""
        try:
            with open("results.json", "w") as result_file:
                json.dump(self.results, result_file, indent=4)
        except IOError as e:
            logger.error(f"Error saving results to file: {str(e)}")

    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Starting browser initialization")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            if not CAMOUFOX_AVAILABLE:
                raise ValueError("Camoufox is not available. Please install camoufox or use a different browser type.")
            camoufox = AsyncCamoufox(headless=self.headless)

        for _ in range(self.thread_count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                browser = await playwright.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=self.browser_args
                )

            elif self.browser_type == "camoufox":
                browser = await camoufox.start()

            await self.browser_pool.put((_+1, browser))

            if self.debug:
                logger.success(f"Browser {_ + 1} initialized successfully")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

    async def _solve_turnstile_embedded(
        self,
        task_id: str,
        url: str,
        sitekey: str,
        action: Optional[str],
        cdata: Optional[str],
        solve_timeout: Optional[float],
    ) -> None:
        """Serve local HTML with an embedded Turnstile widget (legacy flow when ``sitekey`` is provided)."""
        proxy_url = None
        proxy_cfg: Optional[Dict[str, str]] = None

        index, browser = await self.browser_pool.get()

        if self.proxy_support:
            proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
            with open(proxy_file_path) as proxy_file:
                proxies = [line.strip() for line in proxy_file if line.strip()]
            proxy_url = random.choice(proxies) if proxies else None
            if proxy_url:
                parts = proxy_url.split(":")
                if len(parts) == 3:
                    proxy_cfg = {"server": f"{proxy_url}"}
                elif len(parts) == 5:
                    proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
                    proxy_cfg = {
                        "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                        "username": proxy_user,
                        "password": proxy_pass,
                    }
                else:
                    await self.browser_pool.put((index, browser))
                    raise ValueError("Invalid proxy format")

        start_time = time.time()
        context = None

        async def _run_embedded() -> None:
            nonlocal context
            context = await browser.new_context(**self._browser_context_options(proxy_cfg))
            page = await context.new_page()
            base = self._normalize_page_url(url)
            url_with_slash = base + "/" if not base.endswith("/") else base
            turnstile_div = (
                '<div class="cf-turnstile" style="background: white;" data-sitekey="' + sitekey + '"'
                + (f' data-action="{action}"' if action else "")
                + (f' data-cdata="{cdata}"' if cdata else "")
                + "></div>"
            )
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)

            async def fulfill_route(route) -> None:
                await route.fulfill(body=page_data, status=200)

            await page.route(url_with_slash, fulfill_route)
            if self.debug:
                logger.debug(
                    f"Browser {index}: Embedded solve | url={url_with_slash!r} sitekey={sitekey!r} proxy={proxy_url!r}"
                )

            await page.goto(url_with_slash, wait_until="domcontentloaded", timeout=120000)

            await page.eval_on_selector("//div[@class='cf-turnstile']", "el => el.style.width = '70px'")

            for attempt in range(10):
                try:
                    turnstile_check = await page.input_value("[name=cf-turnstile-response]", timeout=2000)
                    if turnstile_check == "":
                        if self.debug:
                            logger.debug(f"Browser {index}: Embedded attempt {attempt} - no response yet")
                        await page.locator("//div[@class='cf-turnstile']").click(timeout=1000)
                        await asyncio.sleep(0.5)
                    else:
                        elapsed_time = round(time.time() - start_time, 3)
                        logger.success(
                            f"Browser {index}: Solved (embedded) — "
                            f"{COLORS.get('MAGENTA')}{turnstile_check[:10]}…{COLORS.get('RESET')} in "
                            f"{COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s"
                        )
                        self.results[task_id] = {"value": turnstile_check, "elapsed_time": elapsed_time}
                        self._save_results()
                        return
                except Exception:
                    pass

            if self.results.get(task_id) == "CAPTCHA_NOT_READY":
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                self._save_results()
                logger.error(
                    f"Browser {index}: Embedded Turnstile failed in "
                    f"{COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')}s"
                )

        try:
            if solve_timeout is not None:
                await asyncio.wait_for(_run_embedded(), timeout=solve_timeout)
            else:
                await _run_embedded()
        except asyncio.TimeoutError:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {
                "value": "CAPTCHA_FAIL",
                "elapsed_time": elapsed_time,
                "reason": "solve_timeout",
                "timeout_seconds": solve_timeout,
                "message": f"Solve exceeded time limit of {solve_timeout} second(s).",
            }
            self._save_results()
            logger.error(f"Browser {index}: Embedded solve timeout (limit {solve_timeout}s)")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            self._save_results()
            logger.exception(f"Browser {index}: Embedded solve error: {str(e)}")
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            await self.browser_pool.put((index, browser))

    async def _solve_turnstile(
        self,
        task_id: str,
        url: str,
        sitekey: Optional[str] = None,
        action: str = None,
        cdata: str = None,
        solve_timeout: Optional[float] = None,
    ):
        """Load the real page, pass Turnstile like a normal browser, then return token + cookies."""
        if sitekey:
            await self._solve_turnstile_embedded(
                task_id, url, sitekey, action, cdata, solve_timeout
            )
            return

        proxy_url = None
        proxy_cfg: Optional[Dict[str, str]] = None

        index, browser = await self.browser_pool.get()

        if self.proxy_support:
            proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
            with open(proxy_file_path) as proxy_file:
                proxies = [line.strip() for line in proxy_file if line.strip()]
            proxy_url = random.choice(proxies) if proxies else None
            if proxy_url:
                parts = proxy_url.split(":")
                if len(parts) == 3:
                    proxy_cfg = {"server": f"{proxy_url}"}
                elif len(parts) == 5:
                    proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
                    proxy_cfg = {
                        "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                        "username": proxy_user,
                        "password": proxy_pass,
                    }
                else:
                    await self.browser_pool.put((index, browser))
                    raise ValueError("Invalid proxy format")

        start_time = time.time()
        context = None

        async def _run_solve():
            nonlocal context
            context = await browser.new_context(**self._browser_context_options(proxy_cfg))
            page = await context.new_page()
            set_cookie_headers: List[str] = []
            last_document_request_headers: Dict[str, str] = {}
            last_document_response_headers: Dict[str, str] = {}
            last_document_request_body: List[Optional[str]] = [None]

            def _on_response(response):
                try:
                    h = response.headers
                    sc = h.get("set-cookie") or h.get("Set-Cookie")
                    if sc and sc not in set_cookie_headers:
                        set_cookie_headers.append(sc)
                    req = response.request
                    if req.resource_type == "document":
                        last_document_request_headers.clear()
                        last_document_request_headers.update(dict(req.headers))
                        last_document_response_headers.clear()
                        last_document_response_headers.update(dict(h))
                        try:
                            last_document_request_body[0] = req.post_data
                        except Exception:
                            last_document_request_body[0] = None
                except Exception:
                    pass

            page.on("response", _on_response)

            page_url = self._normalize_page_url(url)

            try:
                if self.debug:
                    logger.debug(
                        f"Browser {index}: Real page solve | url={page_url} sitekey={sitekey!r} proxy={proxy_url!r}"
                    )

                await page.goto(page_url, wait_until="domcontentloaded", timeout=120000)
                await page.wait_for_load_state("domcontentloaded")

                await asyncio.sleep(1.5)
                await self._try_click_turnstile(page)

                init_host = (urlparse(page_url).hostname or "").lower()

                def _cookie_matches(domains: set, domain: str) -> bool:
                    d = (domain or "").lstrip(".").lower()
                    if not d:
                        return False
                    return any(h == d or h.endswith("." + d) for h in domains)

                async def _filtered_jar() -> List[Dict[str, Any]]:
                    fh = (urlparse(page.url).hostname or "").lower()
                    hs = {h for h in (fh, init_host) if h}
                    raw = await context.cookies()
                    if not hs:
                        return list(raw)
                    return [c for c in raw if _cookie_matches(hs, c.get("domain", ""))]

                turnstile_check = ""
                session_via_dl = False
                for attempt in range(200):
                    jar = await _filtered_jar()

                    if self._has_d_and_locl(jar):
                        session_via_dl = True
                        if self.debug:
                            logger.debug(f"Browser {index}: d + locl detected (attempt {attempt}), capturing now")
                        break

                    turnstile_check = await self._read_turnstile_token(page)
                    if turnstile_check:
                        break

                    if self.debug and attempt % 25 == 0:
                        logger.debug(f"Browser {index}: Waiting for d/locl or Turnstile token (attempt {attempt})")

                    if attempt % 4 == 0:
                        await self._try_click_turnstile(page)
                    await asyncio.sleep(0.35)

                if not turnstile_check and not session_via_dl:
                    elapsed_time = round(time.time() - start_time, 3)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)

                    sess: Dict[str, Any] = {
                        "value": "CAPTCHA_FAIL",
                        "elapsed_time": elapsed_time,
                        "url_final": page.url,
                    }
                    try:
                        cookies = await context.cookies()
                        final_host = (urlparse(page.url).hostname or "").lower()
                        init_host_cookie = (urlparse(page_url).hostname or "").lower()
                        hosts = {h for h in (final_host, init_host_cookie) if h}

                        def _cookie_matches_inner(domains: set, domain: str) -> bool:
                            d = (domain or "").lstrip(".").lower()
                            if not d:
                                return False
                            return any(h == d or h.endswith("." + d) for h in domains)

                        if hosts:
                            cookies = [c for c in cookies if _cookie_matches_inner(hosts, c.get("domain", ""))]
                        if self._has_d_and_locl(cookies):
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=90000)
                            except Exception:
                                pass
                            await asyncio.sleep(0.35)
                            cookies = await context.cookies()
                            if hosts:
                                cookies = [c for c in cookies if _cookie_matches_inner(hosts, c.get("domain", ""))]
                        ch = self._format_cookie_header(cookies)
                        req_snap = dict(last_document_request_headers)
                        if ch and "cookie" not in {k.lower() for k in req_snap}:
                            req_snap["cookie"] = ch
                        sess["cookies"] = cookies
                        sess["cookie_header"] = ch
                        sess["d_locl_cookie_header"] = self._d_locl_cookie_header(cookies)
                        sess["request_headers"] = req_snap
                        sess["response_headers"] = dict(last_document_response_headers)
                        sess["set_cookie_headers"] = list(set_cookie_headers)
                        self._attach_http_capture(sess, dict(last_document_request_headers), last_document_request_body)
                    except Exception:
                        pass

                    if sess.get("cookie_header"):
                        sess["value"] = ""
                        sess["turnstile_token"] = None
                        sess["note"] = (
                            "No cf-turnstile-response field found; session cookies and request headers were captured "
                            "(e.g. Cloudflare clearance / site cookies only)."
                        )
                        logger.success(
                            f"Browser {index}: Session cookies captured (no Turnstile widget token) in "
                            f"{COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s — {page.url}"
                        )
                    else:
                        logger.error(
                            f"Browser {index}: No Turnstile token or cookies in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')}s"
                        )

                    self.results[task_id] = sess
                    self._save_results()
                else:
                    if session_via_dl and not turnstile_check:
                        await asyncio.sleep(0.3)
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=90000)
                        except Exception:
                            pass
                        await asyncio.sleep(0.35)
                    else:
                        try:
                            await page.wait_for_load_state("networkidle", timeout=20000)
                        except Exception:
                            pass
                        await asyncio.sleep(2.5)

                    cookies = await _filtered_jar()
                    cookie_header = self._format_cookie_header(cookies)
                    elapsed_time = round(time.time() - start_time, 3)

                    if turnstile_check:
                        logger.success(
                            f"Browser {index}: Solved — token {COLORS.get('MAGENTA')}{turnstile_check[:12]}…{COLORS.get('RESET')} in "
                            f"{COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s | final URL {page.url}"
                        )
                    else:
                        logger.success(
                            f"Browser {index}: d + locl captured in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s — {page.url}"
                        )

                    req_hdrs = dict(last_document_request_headers)
                    if cookie_header and "cookie" not in {k.lower() for k in req_hdrs}:
                        req_hdrs["cookie"] = cookie_header

                    payload: Dict[str, Any] = {
                        "value": turnstile_check or "",
                        "elapsed_time": elapsed_time,
                        "url_initial": page_url,
                        "url_final": page.url,
                        "cookies": cookies,
                        "cookie_header": cookie_header,
                        "d_locl_cookie_header": self._d_locl_cookie_header(cookies),
                        "request_headers": req_hdrs,
                        "response_headers": dict(last_document_response_headers),
                        "set_cookie_headers": list(set_cookie_headers),
                    }
                    if session_via_dl and not turnstile_check:
                        payload["turnstile_token"] = None
                        payload["note"] = (
                            "Session cookies `d` and `locl` were detected in the jar; headers captured immediately."
                        )

                    self._attach_http_capture(payload, dict(last_document_request_headers), last_document_request_body)

                    self.results[task_id] = payload
                    self._save_results()

            except Exception as e:
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                logger.exception(f"Browser {index}: Error solving Turnstile: {str(e)}")

        try:
            if solve_timeout is not None:
                await asyncio.wait_for(_run_solve(), timeout=solve_timeout)
            else:
                await _run_solve()
        except asyncio.TimeoutError:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {
                "value": "CAPTCHA_FAIL",
                "elapsed_time": elapsed_time,
                "reason": "solve_timeout",
                "timeout_seconds": solve_timeout,
                "message": f"Solve exceeded time limit of {solve_timeout} second(s).",
            }
            self._save_results()
            logger.error(
                f"Browser {index}: Solve timeout after {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')}s "
                f"(limit {solve_timeout}s)"
            )
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Clearing page state")
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey_raw = request.args.get('sitekey')
        sitekey = (sitekey_raw or "").strip() or None
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        timeout_raw = request.args.get('timeout')

        if not url:
            return jsonify({
                "status": "error",
                "error": "'url' is required"
            }), 400

        solve_timeout = None
        if timeout_raw is not None and str(timeout_raw).strip() != "":
            try:
                solve_timeout = float(timeout_raw)
            except (TypeError, ValueError):
                return jsonify({"status": "error", "error": "Invalid 'timeout': expected a number of seconds"}), 400
            if solve_timeout <= 0:
                return jsonify({"status": "error", "error": "'timeout' must be greater than 0"}), 400
            if solve_timeout > 86400:
                solve_timeout = 86400.0

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"

        try:
            self.app.add_background_task(
                self._solve_turnstile,
                task_id,
                url,
                sitekey,
                action,
                cdata,
                solve_timeout,
            )

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({"task_id": task_id}), 202
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "status": "error",
                "error": str(e)
            }), 500

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id or task_id not in self.results:
            return jsonify({"status": "error", "error": "Invalid task ID/Request parameter"}), 400

        result = self.results[task_id]
        status_code = 200

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            status_code = 422

        return result, status_code


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run the browser headless (requires --useragent unless using camoufox)',
    )
    parser.add_argument('--useragent', type=str, default=None, help='Specify a custom User-Agent string for the browser. If not provided, the default User-Agent is used')
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Verbose solver logging (browser steps, waits, errors)',
    )
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=1, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Pick a random proxy from proxies.txt for each solve')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5000', help='Set the port for the API solver to listen on. (Default: 5000)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = ['chromium', 'chrome', 'msedge']
    if CAMOUFOX_AVAILABLE:
        browser_types.append('camoufox')
    
    if args.browser_type not in browser_types:
        if args.browser_type == 'camoufox' and not CAMOUFOX_AVAILABLE:
            logger.error(f"Camoufox is not available. Please install camoufox or use a different browser type. Available browser types: {browser_types}")
        else:
            logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    elif args.headless is True and args.useragent is None and args.browser_type != 'camoufox':
        if CAMOUFOX_AVAILABLE:
            logger.error(f"You must specify a {COLORS.get('YELLOW')}User-Agent{COLORS.get('RESET')} for Turnstile Solver or use {COLORS.get('GREEN')}camoufox{COLORS.get('RESET')} without useragent")
        else:
            logger.error(f"You must specify a {COLORS.get('YELLOW')}User-Agent{COLORS.get('RESET')} for Turnstile Solver when using headless mode")
    else:
        app = create_app(headless=args.headless, debug=args.debug, useragent=args.useragent, browser_type=args.browser_type, thread=args.thread, proxy_support=args.proxy)
        app.run(host=args.host, port=int(args.port))
