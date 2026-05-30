import os
import sys
import time
import uuid
import json
import random
import logging
import asyncio
import argparse
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, unquote, urlunparse
from urllib import request as urllib_request
from urllib import error as urllib_error

from quart import Quart, request, jsonify
from patchright.async_api import async_playwright as async_patchright

try:
    from playwright.async_api import async_playwright as async_playwright_native
    PLAYWRIGHT_NATIVE_AVAILABLE = True
except ImportError:
    async_playwright_native = None
    PLAYWRIGHT_NATIVE_AVAILABLE = False

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
    DEFAULT_SOLVE_TIMEOUT_SECONDS = 45.0
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

    def __init__(
        self,
        headless: bool,
        useragent: str,
        debug: bool,
        browser_type: str,
        thread: int,
        proxy_support: bool,
        close_delay: float = 0,
    ):
        self.app = Quart(__name__)
        self.debug = debug
        self.results = self._load_results()
        self.browser_type = browser_type
        self.headless = headless
        self.close_delay = max(0.0, float(close_delay or 0))
        self.useragent = useragent
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.browser_runtimes: List[Any] = []
        self.browser_args = ["--disable-blink-features=AutomationControlled"]
        if useragent:
            self.browser_args.append(f"--user-agent={useragent}")

        self._setup_routes()

    async def _delay_before_close(self, browser_index: int) -> None:
        if self.close_delay <= 0:
            return
        logger.info(f"Browser {browser_index}: Keeping page open for {self.close_delay:g}s before cleanup")
        await asyncio.sleep(self.close_delay)

    @staticmethod
    def _normalize_page_url(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return url
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    _REVERSE_PROXY_SCHEMA_MARKER = "/SCHEMA"

    @staticmethod
    def _normalize_reverse_proxy_base(raw: str) -> str:
        """Strip trailing slash; ensure scheme (same as page URLs)."""
        u = TurnstileAPIServer._normalize_page_url((raw or "").strip())
        return u.rstrip("/")

    @staticmethod
    def _parse_reverse_proxy_param(raw: str) -> tuple[str, Optional[str]]:
        """
        Parse ``reverse_proxy`` query value.

        Returns ``(base_url, forced_style)`` where ``forced_style`` is ``\"full\"`` if the path ended
        with ``/SCHEMA`` (marker stripped from base; tail includes ``http(s)://...``). Otherwise
        ``forced_style`` is ``None`` and callers use ``reverse_proxy_style`` (default ``host``: no
        scheme in tail, e.g. ``…/goplay.ml/`` not ``…/https://goplay.ml/``).
        """
        u = TurnstileAPIServer._normalize_page_url((raw or "").strip())
        p = urlparse(u)
        path = p.path or ""
        path_norm = path.rstrip("/")
        if path_norm.endswith(TurnstileAPIServer._REVERSE_PROXY_SCHEMA_MARKER):
            prefix = path_norm[: -len(TurnstileAPIServer._REVERSE_PROXY_SCHEMA_MARKER)].rstrip("/")
            path_out = f"/{prefix}" if prefix else ""
            base = urlunparse((p.scheme, p.netloc, path_out, "", "", "")).rstrip("/")
            return base, "full"
        return u.rstrip("/"), None

    @staticmethod
    def _build_reverse_proxied_url(absolute_url: str, base: str, style: str) -> str:
        """Map an absolute http(s) URL to ``base/<tail>`` for worker-style reverse proxies."""
        base = base.rstrip("/")
        au = absolute_url or ""
        if au == base or au.startswith(base + "/"):
            return au
        p = urlparse(au)
        if p.scheme not in ("http", "https"):
            return au
        path_part = p.path if p.path else "/"
        query = f"?{p.query}" if p.query else ""
        fragment = f"#{p.fragment}" if p.fragment else ""
        if style == "host":
            tail = f"{p.netloc}{path_part}{query}{fragment}"
        else:
            tail = urlunparse((p.scheme, p.netloc, path_part, "", p.query, p.fragment))
        return f"{base}/{tail}"

    @staticmethod
    def _reverse_proxy_allowed_hosts_env() -> Optional[frozenset]:
        """If set, ``ALLOWED_REVERSE_PROXY_HOSTS`` is a comma-separated list of allowed proxy hostnames (lowercase)."""
        raw = (os.environ.get("ALLOWED_REVERSE_PROXY_HOSTS") or "").strip()
        if not raw:
            return None
        return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())

    @staticmethod
    def _reverse_proxy_bypass_hosts_env() -> frozenset:
        """Hosts that must stay direct even when reverse_proxy is enabled."""
        raw = os.environ.get("REVERSE_PROXY_BYPASS_HOSTS")
        if raw is None:
            raw = "challenges.cloudflare.com"
        return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())

    @staticmethod
    def _host_matches(host: str, patterns: frozenset) -> bool:
        h = (host or "").lower().strip(".")
        if not h:
            return False
        return any(h == p or h.endswith("." + p) for p in patterns)

    def _assert_reverse_proxy_host_allowed(self, normalized_base: str) -> None:
        allowed = self._reverse_proxy_allowed_hosts_env()
        if not allowed:
            return
        host = (urlparse(normalized_base).hostname or "").lower()
        if not host:
            raise ValueError("reverse_proxy must include a host when ALLOWED_REVERSE_PROXY_HOSTS is set")
        if host not in allowed:
            raise ValueError(
                f"reverse_proxy host '{host}' is not permitted. "
                "Configure ALLOWED_REVERSE_PROXY_HOSTS (comma-separated hosts, e.g. as.mykhcdn.workers.dev)."
            )

    async def _reverse_proxy_route_handler(
        self,
        route,
        base: str,
        style: str,
        browser_index: int,
    ) -> None:
        """Rewrite all http(s) document/subresource requests through ``base`` (WebSocket/data/blob unchanged)."""
        req = route.request
        u = req.url
        if u.startswith(("data:", "blob:", "about:")) or u.startswith(("ws://", "wss://")):
            await route.continue_()
            return
        if not (u.startswith("http://") or u.startswith("https://")):
            await route.continue_()
            return
        host = (urlparse(u).hostname or "").lower()
        if self._host_matches(host, self._reverse_proxy_bypass_hosts_env()):
            if self.debug:
                logger.debug(
                    f"Browser {browser_index}: reverse_proxy bypass [{req.resource_type}] "
                    f"{u[:160]}{'…' if len(u) > 160 else ''}"
                )
            await route.continue_()
            return
        nu = self._build_reverse_proxied_url(u, base, style)
        if nu != u and self.debug:
            logger.debug(
                f"Browser {browser_index}: reverse_proxy [{req.resource_type}] "
                f"{u[:160]}{'…' if len(u) > 160 else ''} -> {nu[:160]}{'…' if len(nu) > 160 else ''}"
            )
        if nu == u:
            await route.continue_()
            return

        # Important: do not `continue_(url=nu)` for reverse-proxy mode.
        #
        # Continuing with the proxy URL makes Chromium treat the network response as coming from
        # the worker host. GoPlay then returns cookies such as `d` and `locl` on the proxied
        # document, but the browser rejects them because their Domain is `.goplay.ml` while the
        # response URL is `*.workers.dev`. Fetching the proxied URL ourselves and fulfilling the
        # original route keeps the browser-visible response scoped to the original URL, so the
        # target-domain cookies are accepted and can be captured by the solver.
        try:
            if req.resource_type == "document" and host.endswith("goplay.ml"):
                upstream = await self._fetch_reverse_proxy_document(nu, req)
                headers = dict(upstream.get("headers") or {})
                headers.pop("content-encoding", None)
                headers.pop("Content-Encoding", None)
                headers.pop("content-length", None)
                headers.pop("Content-Length", None)
                headers["content-type"] = headers.get("content-type") or headers.get("Content-Type") or "text/html; charset=UTF-8"
                headers["x-turnstile-upstream-status"] = str(upstream.get("status"))
                headers["x-turnstile-upstream-url"] = nu
                if self.debug:
                    logger.warning(
                        f"Browser {browser_index}: reverse_proxy document manual fetch "
                        f"{upstream.get('status')} -> 200 for {u[:120]}{'…' if len(u) > 120 else ''} "
                        f"body_excerpt={self._trim_text(upstream.get('text') or '', 180)}"
                    )
                await route.fulfill(
                    status=200,
                    headers=headers,
                    body=upstream.get("text") or "",
                )
                return
            response = await route.fetch(url=nu, timeout=60000)
            await route.fulfill(response=response)
            return
        except Exception as e:
            # The context may already be closing after the solve succeeds, leaving late image/XHR
            # routes disposed. Avoid turning that into a solve failure. For active requests, fall
            # back to URL rewrite when possible so reverse_proxy still has a best-effort path.
            if self.debug:
                msg = str(e).replace("\n", " ")[:240]
                logger.warning(
                    f"Browser {browser_index}: reverse_proxy fetch/fulfill failed "
                    f"[{req.resource_type}] {u[:120]}{'…' if len(u) > 120 else ''}: {msg}"
                )
            try:
                await route.continue_(url=nu)
            except Exception:
                pass

    @staticmethod
    def _decode_http_body(body: bytes, content_type: str) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([^\s;]+)", content_type or "", re.IGNORECASE)
        if match:
            charset = match.group(1).strip(' "\'')
        try:
            return body.decode(charset, errors="replace")
        except Exception:
            try:
                return body.decode("utf-8", errors="replace")
            except Exception:
                return ""

    async def _fetch_reverse_proxy_document(self, url: str, req) -> Dict[str, Any]:
        method = (getattr(req, "method", None) or "GET").upper()
        req_headers = {}
        try:
            source_headers = dict(getattr(req, "headers", {}) or {})
        except Exception:
            source_headers = {}
        for key, value in source_headers.items():
            lower = str(key).lower()
            if lower in {"host", "connection", "content-length", "accept-encoding"}:
                continue
            req_headers[str(key)] = str(value)
        req_headers["Accept-Encoding"] = "identity"
        data = None
        try:
            post_data = getattr(req, "post_data", None)
            if post_data:
                data = post_data.encode("utf-8") if isinstance(post_data, str) else post_data
        except Exception:
            data = None

        def _do_fetch() -> Dict[str, Any]:
            request_obj = urllib_request.Request(url, headers=req_headers, method=method, data=data)
            try:
                with urllib_request.urlopen(request_obj, timeout=60) as response:
                    raw = response.read()
                    headers = dict(response.headers.items())
                    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
                    return {
                        "status": getattr(response, "status", 200),
                        "headers": headers,
                        "text": self._decode_http_body(raw, content_type),
                    }
            except urllib_error.HTTPError as e:
                raw = e.read()
                headers = dict(e.headers.items()) if e.headers else {}
                content_type = headers.get("Content-Type") or headers.get("content-type") or ""
                return {
                    "status": getattr(e, "code", 599),
                    "headers": headers,
                    "text": self._decode_http_body(raw, content_type),
                }

        return await asyncio.to_thread(_do_fetch)

    _PROXY_SCHEMES = frozenset(("http", "https", "socks5", "socks4"))

    @staticmethod
    def _parse_proxy_spec(spec: str) -> Dict[str, str]:
        """Playwright ``proxy`` dict from a URL or compact proxy string.

        Supported formats:
        - scheme://host:port
        - scheme://user:pass@host:port
        - scheme:host:port
        - scheme:host:port:user:pass
        - scheme:user:pass:host:port
        """
        s = (spec or "").strip()
        if not s:
            raise ValueError("Proxy spec is empty")
        if "://" in s:
            p = urlparse(s)
            if not p.scheme or not p.hostname or not p.port:
                raise ValueError("Proxy URL must include scheme, host, and port (e.g. http://127.0.0.1:8080 or socks5://127.0.0.1:1080)")
            sch = p.scheme.lower()
            if sch not in TurnstileAPIServer._PROXY_SCHEMES:
                raise ValueError(f"Unsupported proxy scheme '{p.scheme}'; use http, https, socks5, or socks4")
            host = p.hostname
            port = int(p.port)
            cfg: Dict[str, str] = {"server": f"{sch}://{host}:{port}"}
            if p.username:
                cfg["username"] = unquote(p.username)
            if p.password:
                cfg["password"] = unquote(p.password)
            return cfg
        parts = s.split(":")
        if len(parts) == 3:
            scheme, host, port_s = parts
            scheme = scheme.lower()
            if scheme not in TurnstileAPIServer._PROXY_SCHEMES:
                raise ValueError(f"Unsupported proxy scheme '{scheme}'")
            try:
                int(port_s)
            except ValueError as e:
                raise ValueError("Proxy port must be numeric") from e
            return {"server": f"{scheme}://{host}:{port_s}"}
        if len(parts) == 5:
            scheme = parts[0].lower()
            if scheme not in TurnstileAPIServer._PROXY_SCHEMES:
                raise ValueError(f"Unsupported proxy scheme '{scheme}'")

            # Legacy compact form: scheme:host:port:user:pass
            if parts[2].isdigit():
                _, host, port_s, user, pwd = parts
            # Provider compact form: scheme:user:pass:host:port
            elif parts[4].isdigit():
                _, user, pwd, host, port_s = parts
            else:
                raise ValueError(
                    "Proxy port must be numeric. Supported compact auth formats: "
                    "scheme:host:port:user:pass or scheme:user:pass:host:port"
                )

            try:
                int(port_s)
            except ValueError as e:
                raise ValueError("Proxy port must be numeric") from e
            return {
                "server": f"{scheme}://{host}:{port_s}",
                "username": unquote(user),
                "password": unquote(pwd),
            }
        raise ValueError(
            "Invalid proxy format. Use: scheme://host:port, scheme://user:pass@host:port, "
            "scheme:host:port, scheme:host:port:user:pass, or scheme:user:pass:host:port "
            "(same rules as query parameter 'proxy')"
        )

    def _pick_proxy_for_solve(
        self,
        proxy_cfg_override: Optional[Dict[str, str]],
    ) -> tuple[Optional[Dict[str, str]], Optional[str]]:
        """Returns (playwright proxy dict, redacted label for logging). Query proxy wins over proxies.txt."""
        if proxy_cfg_override is not None:
            return proxy_cfg_override, proxy_cfg_override.get("server")
        if not self.proxy_support:
            return None, None
        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        with open(proxy_file_path) as proxy_file:
            proxies = [line.strip() for line in proxy_file if line.strip()]
        line = random.choice(proxies) if proxies else None
        if not line:
            return None, None
        cfg = self._parse_proxy_spec(line)
        return cfg, cfg.get("server")

    def _assert_proxy_supported_by_browser(self, proxy: Optional[Dict[str, str]]) -> None:
        """Chromium does not support SOCKS4/SOCKS5 proxy username/password (Patchright/Playwright)."""
        if not proxy:
            return
        server = (proxy.get("server") or "").strip().lower()
        has_user = bool((proxy.get("username") or "").strip())
        has_pass = bool((proxy.get("password") or "").strip())
        if not (has_user or has_pass):
            return
        if server.startswith("socks4://") or server.startswith("socks5://"):
            if self.browser_type in ("chromium", "chrome", "msedge"):
                raise ValueError(
                    "Chromium (chromium/chrome/msedge) does not support SOCKS proxy authentication. "
                    "Use socks5:host:port without credentials, an HTTP or HTTPS proxy with user:pass "
                    "(e.g. http:user:pass:host:port, http://user:pass@host:port, "
                    "https:user:pass:host:port, or https://user:pass@host:port), "
                    "or --browser_type camoufox (Firefox)."
                )

    @staticmethod
    def _supported_browser_types() -> List[str]:
        browser_types = ["chromium", "playwright", "chrome", "msedge"]
        if CAMOUFOX_AVAILABLE:
            browser_types.append("camoufox")
        return browser_types

    def _normalize_browser_type(self, browser_type: Optional[str]) -> str:
        value = (browser_type or self.browser_type or "chromium").strip().lower()
        if value == "playwrite":
            value = "playwright"
        if value not in self._supported_browser_types():
            raise ValueError(
                f"Unknown browser type '{value}'. Available browser types: {self._supported_browser_types()}"
            )
        if value == "playwright" and not PLAYWRIGHT_NATIVE_AVAILABLE:
            raise ValueError("Playwright is not available. Please install playwright or use a different browser type.")
        if value == "camoufox" and not CAMOUFOX_AVAILABLE:
            raise ValueError("Camoufox is not available. Please install camoufox or use a different browser type.")
        return value

    async def _launch_browser_instance(self, browser_type: str):
        normalized = self._normalize_browser_type(browser_type)
        if normalized in ['chromium', 'chrome', 'msedge']:
            runtime = await async_patchright().start()
            launch_options = {
                "headless": self.headless,
                "args": self.browser_args,
            }
            if normalized != 'chromium':
                launch_options["channel"] = normalized
            browser = await runtime.chromium.launch(**launch_options)
            return runtime, browser

        if normalized == "playwright":
            runtime = await async_playwright_native().start()
            browser = await runtime.chromium.launch(
                headless=self.headless,
                args=self.browser_args,
            )
            return runtime, browser

        camoufox = AsyncCamoufox(headless=self.headless)
        browser = await camoufox.start()
        return camoufox, browser

    async def _acquire_browser(self, browser_type_override: Optional[str] = None):
        requested_browser_type = self._normalize_browser_type(browser_type_override)
        if requested_browser_type == self.browser_type:
            index, browser = await self.browser_pool.get()

            async def _release():
                await self.browser_pool.put((index, browser))

            return index, browser, requested_browser_type, _release

        runtime, browser = await self._launch_browser_instance(requested_browser_type)
        released = False

        async def _release():
            nonlocal released
            if released:
                return
            released = True
            try:
                await browser.close()
            except Exception:
                pass
            stop = getattr(runtime, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:
                    pass

        if self.debug:
            logger.info(
                "Ephemeral browser launched | requested_browser=%s default_browser=%s",
                requested_browser_type,
                self.browser_type,
            )
        return 0, browser, requested_browser_type, _release

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
    def _trim_text(value: Any, limit: int = 240) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    @staticmethod
    def _cookie_name_list(cookies: List[Dict[str, Any]]) -> List[str]:
        names = sorted({str(cookie.get("name")) for cookie in (cookies or []) if cookie.get("name")})
        return names[:20]

    @staticmethod
    def _has_cookie_name(cookies: List[Dict[str, Any]], name: str) -> bool:
        return any(cookie.get("name") == name for cookie in (cookies or []))

    async def _collect_page_diagnostics(self, page) -> Dict[str, Any]:
        diagnostics: Dict[str, Any] = {}
        if page is None:
            return diagnostics

        try:
            diagnostics.update(await page.evaluate(
                """() => {
                    const bodyText = document.body
                        ? ((document.body.innerText || document.body.textContent || '').replace(/\\s+/g, ' ').trim())
                        : '';
                    const count = (selector) => document.querySelectorAll(selector).length;
                    return {
                        title: document.title || '',
                        body_excerpt: bodyText.slice(0, 600),
                        body_length: bodyText.length,
                        widget_count: count("div.cf-turnstile, .cf-turnstile, [data-sitekey], [name='cf-turnstile-response'], textarea[name='cf-turnstile-response'], input[name='cf-turnstile-response']"),
                        iframe_count: count("iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], iframe[title*='Cloudflare']"),
                        has_access_denied: /access denied|forbidden|not authorized/i.test(bodyText),
                        has_rate_limited: /rate limit|too many requests|too many times|try again later/i.test(bodyText),
                        has_cloudflare_interstitial: /just a moment|checking your browser|verify you are human|attention required|security check/i.test(bodyText),
                        has_turnstile_text: /turnstile|captcha|verify/i.test(bodyText),
                    };
                }"""
            ) or {})
        except Exception as e:
            diagnostics["diagnostic_error"] = self._trim_text(e, 160)

        try:
            diagnostics["url"] = page.url
        except Exception:
            pass

        if diagnostics.get("title"):
            diagnostics["title"] = self._trim_text(diagnostics["title"], 160)
        if diagnostics.get("body_excerpt"):
            diagnostics["body_excerpt"] = self._trim_text(diagnostics["body_excerpt"], 320)
        return diagnostics

    @staticmethod
    def _classify_solve_failure_reason(
        diagnostics: Dict[str, Any],
        cookies: List[Dict[str, Any]],
    ) -> str:
        if diagnostics.get("has_access_denied"):
            return "access_denied_page"
        if diagnostics.get("has_rate_limited"):
            return "rate_limited_page"
        if diagnostics.get("has_cloudflare_interstitial"):
            return "cloudflare_interstitial_unresolved"
        if cookies and (
            TurnstileAPIServer._has_cookie_name(cookies, "d")
            or TurnstileAPIServer._has_cookie_name(cookies, "locl")
            or TurnstileAPIServer._has_cookie_name(cookies, "cf_clearance")
        ):
            return "partial_session_cookies_only"
        if diagnostics.get("widget_count") or diagnostics.get("iframe_count"):
            return "turnstile_present_but_unsolved"
        if diagnostics.get("has_turnstile_text"):
            return "turnstile_page_without_widget"
        return "no_turnstile_token_or_session_cookies"

    @staticmethod
    def _failure_reason_message(reason: str) -> str:
        messages = {
            "embedded_no_token_after_attempts": "Embedded widget never returned a Turnstile token before attempts were exhausted.",
            "turnstile_present_but_unsolved": "Turnstile widget/iframe was present but no token or required session cookies were produced.",
            "turnstile_page_without_widget": "Turnstile-related text was detected, but no usable widget/token field became available.",
            "cloudflare_interstitial_unresolved": "Cloudflare interstitial/challenge page remained unresolved.",
            "access_denied_page": "The target page rendered an access denied / forbidden response.",
            "rate_limited_page": "The target page appears rate-limited or temporarily blocked.",
            "partial_session_cookies_only": "Only partial session cookies were captured; required token or cookie set was incomplete.",
            "no_turnstile_token_or_session_cookies": "No Turnstile token or usable session cookies were captured.",
            "embedded_solver_exception": "Embedded Turnstile solver raised an exception.",
            "solver_exception": "Turnstile solver raised an exception.",
            "solve_timeout": "Solve exceeded the configured time limit.",
        }
        return messages.get(reason, reason.replace("_", " "))

    async def _build_failure_payload(
        self,
        *,
        elapsed_time: float,
        reason: str,
        page=None,
        cookies: Optional[List[Dict[str, Any]]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "value": "CAPTCHA_FAIL",
            "elapsed_time": elapsed_time,
            "reason": reason,
            "message": self._failure_reason_message(reason),
        }
        if page is not None:
            diagnostics = await self._collect_page_diagnostics(page)
            if diagnostics:
                payload["page"] = diagnostics
                if diagnostics.get("url"):
                    payload["url_final"] = diagnostics["url"]
        if cookies is not None:
            payload["cookie_names"] = self._cookie_name_list(cookies)
            payload["d_cookie_present"] = self._has_cookie_name(cookies, "d")
            payload["locl_cookie_present"] = self._has_cookie_name(cookies, "locl")
            payload["cf_clearance_present"] = self._has_cookie_name(cookies, "cf_clearance")
        if extra:
            payload.update(extra)
        return payload

    def _log_failure_payload(self, browser_index: int, task_id: str, payload: Dict[str, Any]) -> None:
        page = payload.get("page") or {}
        ip_preflight = payload.get("ip_preflight") or {}
        logger.error(
            "Browser %s: Solve rejected | task_id=%s reason=%s message=%s final_url=%s title=%s "
            "nav_status=%s nav_status_text=%s nav_failure=%s widget_count=%s iframe_count=%s "
            "cookies=%s d_cookie=%s locl_cookie=%s cf_clearance=%s "
            "preflight_status=%s preflight_ip=%s preflight_error=%s body_excerpt=%s",
            browser_index,
            task_id,
            payload.get("reason"),
            payload.get("message"),
            page.get("url") or payload.get("url_final") or "",
            page.get("title") or "",
            payload.get("document_response_status"),
            payload.get("document_response_status_text"),
            payload.get("document_failure_text"),
            page.get("widget_count"),
            page.get("iframe_count"),
            payload.get("cookie_names") or [],
            payload.get("d_cookie_present"),
            payload.get("locl_cookie_present"),
            payload.get("cf_clearance_present"),
            ip_preflight.get("status"),
            ip_preflight.get("ip") or "",
            ip_preflight.get("error") or "",
            page.get("body_excerpt") or "",
        )

    @staticmethod
    def _request_failure_text(request_obj) -> str:
        try:
            failure = getattr(request_obj, "failure", None)
            if callable(failure):
                failure = failure()
            if isinstance(failure, dict):
                return str(failure.get("errorText") or failure.get("error") or "").strip()
            if failure:
                return str(failure).strip()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _extract_ip_value(text: Any) -> str:
        raw = " ".join(str(text or "").split())
        if not raw:
            return ""
        json_match = re.search(r'"ip"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
        if json_match:
            return json_match.group(1).strip()
        ipv4_match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", raw)
        if ipv4_match:
            return ipv4_match.group(0)
        ipv6_match = re.search(r"\b(?:[0-9a-f]{1,4}:){2,}[0-9a-f:]{1,4}\b", raw, re.IGNORECASE)
        if ipv6_match:
            return ipv6_match.group(0)
        return ""

    async def _run_ip_preflight(
        self,
        page,
        browser_index: int,
        reverse_proxy_base: Optional[str] = None,
        reverse_proxy_style: str = "host",
    ) -> Dict[str, Any]:
        preflight_url = self._normalize_page_url(
            os.environ.get("TURNSTILE_IP_PREFLIGHT_URL") or "https://api64.ipify.org?format=json"
        )
        started_at = time.time()
        result: Dict[str, Any] = {
            "enabled": True,
            "url_initial": preflight_url,
            "routing": "reverse_proxy" if reverse_proxy_base else "direct",
            "reverse_proxy": reverse_proxy_base or "",
            "reverse_proxy_style": reverse_proxy_style if reverse_proxy_style in ("full", "host") else "host",
        }
        try:
            response = await page.goto(preflight_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(0.25)
            body_text = await page.evaluate(
                "() => (document.body && document.body.innerText) || "
                "(document.documentElement && document.documentElement.innerText) || ''"
            )
            result["elapsed_time"] = round(time.time() - started_at, 3)
            result["status"] = response.status if response else None
            try:
                result["status_text"] = response.status_text if response else ""
            except Exception:
                result["status_text"] = ""
            try:
                result["url_final"] = page.url
            except Exception:
                result["url_final"] = preflight_url
            result["body_excerpt"] = self._trim_text(body_text, 160)
            result["ip"] = self._extract_ip_value(body_text)
            logger.info(
                "Browser %s: IP preflight | routing=%s reverse_proxy=%s reverse_proxy_style=%s "
                "status=%s status_text=%s ip=%s final_url=%s body_excerpt=%s",
                browser_index,
                result["routing"],
                result["reverse_proxy"] or "direct",
                result["reverse_proxy_style"],
                result.get("status"),
                result.get("status_text") or "",
                result.get("ip") or "",
                result.get("url_final") or "",
                result.get("body_excerpt") or "",
            )
        except Exception as e:
            result["elapsed_time"] = round(time.time() - started_at, 3)
            result["error"] = self._trim_text(e, 240)
            try:
                result["url_final"] = page.url
            except Exception:
                result["url_final"] = preflight_url
            logger.warning(
                "Browser %s: IP preflight failed | routing=%s reverse_proxy=%s reverse_proxy_style=%s "
                "final_url=%s error=%s",
                browser_index,
                result["routing"],
                result["reverse_proxy"] or "direct",
                result["reverse_proxy_style"],
                result.get("url_final") or "",
                result.get("error") or "",
            )
        return result

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
        for _ in range(self.thread_count):
            runtime, browser = await self._launch_browser_instance(self.browser_type)
            self.browser_runtimes.append(runtime)
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
        browser_type_override: Optional[str] = None,
        proxy_cfg_override: Optional[Dict[str, str]] = None,
        reverse_proxy_base: Optional[str] = None,
        reverse_proxy_style: str = "host",
    ) -> None:
        """Serve local HTML with an embedded Turnstile widget (legacy flow when ``sitekey`` is provided)."""
        index, browser, effective_browser_type, release_browser = await self._acquire_browser(browser_type_override)

        try:
            proxy_cfg, proxy_label = self._pick_proxy_for_solve(proxy_cfg_override)
            current_browser_type = self.browser_type
            self.browser_type = effective_browser_type
            self._assert_proxy_supported_by_browser(proxy_cfg)
            self.browser_type = current_browser_type
        except ValueError:
            await release_browser()
            raise

        rev_base = reverse_proxy_base.rstrip("/") if reverse_proxy_base else None
        rev_style = reverse_proxy_style if reverse_proxy_style in ("full", "host") else "host"

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

            async def _all_embedded_routes(route) -> None:
                req = route.request
                u = req.url
                doc_key = u.split("#")[0]
                slug_key = url_with_slash.split("#")[0]
                if doc_key.rstrip("/") == slug_key.rstrip("/") or doc_key.startswith(url_with_slash + "?"):
                    await route.fulfill(
                        body=page_data,
                        status=200,
                        headers={"content-type": "text/html; charset=utf-8"},
                    )
                    return
                if rev_base:
                    await self._reverse_proxy_route_handler(route, rev_base, rev_style, index)
                    return
                await route.continue_()

            await page.route("**/*", _all_embedded_routes)
            if self.debug:
                logger.debug(
                    f"Browser {index}: Embedded solve | url={url_with_slash!r} sitekey={sitekey!r} "
                    f"proxy={proxy_label!r} browser={effective_browser_type!r}"
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
                        self.results[task_id] = {
                            "value": turnstile_check,
                            "elapsed_time": elapsed_time,
                            "browser_type": effective_browser_type,
                            "browser_backend": effective_browser_type,
                        }
                        self._save_results()
                        return
                except Exception:
                    pass

            if self.results.get(task_id) == "CAPTCHA_NOT_READY":
                elapsed_time = round(time.time() - start_time, 3)
                payload = await self._build_failure_payload(
                    elapsed_time=elapsed_time,
                    reason="embedded_no_token_after_attempts",
                    page=page,
                    extra={
                        "attempts": 10,
                        "url_initial": url_with_slash,
                        "sitekey": sitekey,
                        "browser_type": effective_browser_type,
                        "browser_backend": effective_browser_type,
                    },
                )
                self.results[task_id] = payload
                self._save_results()
                self._log_failure_payload(index, task_id, payload)

        try:
            if solve_timeout is not None:
                await asyncio.wait_for(_run_embedded(), timeout=solve_timeout)
            else:
                await _run_embedded()
        except asyncio.TimeoutError:
            elapsed_time = round(time.time() - start_time, 3)
            payload = await self._build_failure_payload(
                elapsed_time=elapsed_time,
                reason="solve_timeout",
                extra={
                    "timeout_seconds": solve_timeout,
                    "message": f"Solve exceeded time limit of {solve_timeout} second(s).",
                    "sitekey": sitekey,
                    "url_initial": url,
                    "browser_type": effective_browser_type,
                    "browser_backend": effective_browser_type,
                },
            )
            self.results[task_id] = payload
            self._save_results()
            self._log_failure_payload(index, task_id, payload)
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            payload = await self._build_failure_payload(
                elapsed_time=elapsed_time,
                reason="embedded_solver_exception",
                extra={
                    "error": self._trim_text(e, 320),
                    "sitekey": sitekey,
                    "url_initial": url,
                    "browser_type": effective_browser_type,
                    "browser_backend": effective_browser_type,
                },
            )
            self.results[task_id] = payload
            self._save_results()
            logger.exception(f"Browser {index}: Embedded solve error: {str(e)}")
        finally:
            if context is not None:
                try:
                    await self._delay_before_close(index)
                    await context.close()
                except Exception:
                    pass
            await release_browser()

    async def _solve_turnstile(
        self,
        task_id: str,
        url: str,
        sitekey: Optional[str] = None,
        action: str = None,
        cdata: str = None,
        solve_timeout: Optional[float] = None,
        browser_type_override: Optional[str] = None,
        proxy_cfg_override: Optional[Dict[str, str]] = None,
        reverse_proxy_base: Optional[str] = None,
        reverse_proxy_style: str = "host",
    ):
        """Load the real page, pass Turnstile like a normal browser, then return token + cookies."""
        if sitekey:
            await self._solve_turnstile_embedded(
                task_id,
                url,
                sitekey,
                action,
                cdata,
                solve_timeout,
                browser_type_override,
                proxy_cfg_override,
                reverse_proxy_base,
                reverse_proxy_style,
            )
            return

        index, browser, effective_browser_type, release_browser = await self._acquire_browser(browser_type_override)

        try:
            proxy_cfg, proxy_label = self._pick_proxy_for_solve(proxy_cfg_override)
            current_browser_type = self.browser_type
            self.browser_type = effective_browser_type
            self._assert_proxy_supported_by_browser(proxy_cfg)
            self.browser_type = current_browser_type
        except ValueError:
            await release_browser()
            raise

        start_time = time.time()
        context = None

        rev_base = reverse_proxy_base.rstrip("/") if reverse_proxy_base else None
        rev_style = reverse_proxy_style if reverse_proxy_style in ("full", "host") else "host"

        async def _run_solve():
            nonlocal context
            context = await browser.new_context(**self._browser_context_options(proxy_cfg))
            page = await context.new_page()
            if rev_base:

                async def _rp_route(route) -> None:
                    await self._reverse_proxy_route_handler(route, rev_base, rev_style, index)

                await page.route("**/*", _rp_route)
            ip_preflight = await self._run_ip_preflight(page, index, rev_base, rev_style)
            set_cookie_headers: List[str] = []
            last_document_request_headers: Dict[str, str] = {}
            last_document_response_headers: Dict[str, str] = {}
            last_document_request_body: List[Optional[str]] = [None]
            last_document_status: List[Optional[int]] = [None]
            last_document_status_text: List[str] = [""]
            last_document_url: List[str] = [""]
            last_document_failure_text: List[str] = [""]

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
                        last_document_status[0] = response.status
                        try:
                            last_document_status_text[0] = response.status_text
                        except Exception:
                            last_document_status_text[0] = ""
                        try:
                            last_document_url[0] = response.url
                        except Exception:
                            last_document_url[0] = ""
                        try:
                            last_document_request_body[0] = req.post_data
                        except Exception:
                            last_document_request_body[0] = None
                except Exception:
                    pass

            def _on_request_failed(req):
                try:
                    if getattr(req, "resource_type", None) == "document":
                        last_document_failure_text[0] = self._request_failure_text(req)
                        try:
                            last_document_url[0] = req.url
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", _on_response)
            page.on("requestfailed", _on_request_failed)

            page_url = self._normalize_page_url(url)

            try:
                if self.debug:
                    logger.debug(
                        f"Browser {index}: Real page solve | url={page_url} sitekey={sitekey!r} "
                        f"proxy={proxy_label!r} browser={effective_browser_type!r}"
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
                        "browser_type": effective_browser_type,
                        "browser_backend": effective_browser_type,
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
                        sess["ip_preflight"] = ip_preflight
                        self._attach_http_capture(sess, dict(last_document_request_headers), last_document_request_body)
                    except Exception:
                        pass

                    if sess.get("cookie_header"):
                        failure_reason = "partial_session_cookies_only"
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
                        diagnostics = await self._collect_page_diagnostics(page)
                        reason = self._classify_solve_failure_reason(diagnostics, sess.get("cookies") or [])
                        failure_payload = await self._build_failure_payload(
                            elapsed_time=elapsed_time,
                            reason=reason,
                            page=page,
                            cookies=sess.get("cookies") or [],
                            extra={
                                "url_initial": page_url,
                                "document_response_status": last_document_status[0],
                                "document_response_status_text": last_document_status_text[0],
                                "document_response_url": last_document_url[0],
                                "document_failure_text": last_document_failure_text[0],
                                "request_headers": sess.get("request_headers"),
                                "response_headers": sess.get("response_headers"),
                                "set_cookie_headers": sess.get("set_cookie_headers"),
                                "ip_preflight": ip_preflight,
                            },
                        )
                        self.results[task_id] = failure_payload
                        self._save_results()
                        self._log_failure_payload(index, task_id, failure_payload)
                        return

                    self.results[task_id] = sess
                    self._save_results()
                else:
                    if session_via_dl and not turnstile_check:
                        await asyncio.sleep(0.1)
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
                        "browser_type": effective_browser_type,
                        "browser_backend": effective_browser_type,
                        "cookies": cookies,
                        "cookie_header": cookie_header,
                        "d_locl_cookie_header": self._d_locl_cookie_header(cookies),
                        "request_headers": req_hdrs,
                        "response_headers": dict(last_document_response_headers),
                        "set_cookie_headers": list(set_cookie_headers),
                        "ip_preflight": ip_preflight,
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
                payload = await self._build_failure_payload(
                    elapsed_time=elapsed_time,
                    reason="solver_exception",
                    page=page if 'page' in locals() else None,
                    extra={
                        "url_initial": page_url if 'page_url' in locals() else url,
                        "document_response_status": last_document_status[0] if 'last_document_status' in locals() else None,
                        "document_response_status_text": last_document_status_text[0] if 'last_document_status_text' in locals() else "",
                        "document_response_url": last_document_url[0] if 'last_document_url' in locals() else "",
                        "document_failure_text": last_document_failure_text[0] if 'last_document_failure_text' in locals() else "",
                        "error": self._trim_text(e, 320),
                        "ip_preflight": ip_preflight if 'ip_preflight' in locals() else None,
                        "browser_type": effective_browser_type,
                        "browser_backend": effective_browser_type,
                    },
                )
                self.results[task_id] = payload
                self._save_results()
                self._log_failure_payload(index, task_id, payload)
                logger.exception(f"Browser {index}: Error solving Turnstile: {str(e)}")

        try:
            if solve_timeout is not None:
                await asyncio.wait_for(_run_solve(), timeout=solve_timeout)
            else:
                await _run_solve()
        except asyncio.TimeoutError:
            elapsed_time = round(time.time() - start_time, 3)
            payload = await self._build_failure_payload(
                elapsed_time=elapsed_time,
                reason="solve_timeout",
                extra={
                    "timeout_seconds": solve_timeout,
                    "message": f"Solve exceeded time limit of {solve_timeout} second(s).",
                    "url_initial": url,
                    "browser_type": effective_browser_type,
                    "browser_backend": effective_browser_type,
                },
            )
            self.results[task_id] = payload
            self._save_results()
            self._log_failure_payload(index, task_id, payload)
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Clearing page state")
            if context is not None:
                try:
                    await self._delay_before_close(index)
                    await context.close()
                except Exception:
                    pass
            await release_browser()

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey_raw = request.args.get('sitekey')
        sitekey = (sitekey_raw or "").strip() or None
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        timeout_raw = request.args.get('timeout')
        browser_raw = request.args.get("browser") or request.args.get("browser_type")
        proxy_raw = request.args.get("proxy")
        try:
            requested_browser_type = self._normalize_browser_type(browser_raw)
        except ValueError as e:
            return jsonify({"status": "error", "error": str(e)}), 400
        proxy_cfg_override: Optional[Dict[str, str]] = None
        if proxy_raw is not None and str(proxy_raw).strip():
            try:
                proxy_cfg_override = self._parse_proxy_spec(proxy_raw)
                current_browser_type = self.browser_type
                self.browser_type = requested_browser_type
                self._assert_proxy_supported_by_browser(proxy_cfg_override)
                self.browser_type = current_browser_type
            except ValueError as e:
                return jsonify({"status": "error", "error": str(e)}), 400

        reverse_proxy_raw = request.args.get("reverse_proxy")
        reverse_proxy_style_raw = (request.args.get("reverse_proxy_style") or "host").strip().lower()
        if reverse_proxy_style_raw not in ("full", "host"):
            return jsonify({
                "status": "error",
                "error": "Invalid 'reverse_proxy_style': use 'full' or 'host'",
            }), 400
        reverse_proxy_base: Optional[str] = None
        reverse_proxy_style_effective = reverse_proxy_style_raw
        if reverse_proxy_raw is not None and str(reverse_proxy_raw).strip():
            try:
                reverse_proxy_base, forced_style = self._parse_reverse_proxy_param(
                    str(reverse_proxy_raw).strip()
                )
                if forced_style is not None:
                    reverse_proxy_style_effective = forced_style
                self._assert_reverse_proxy_host_allowed(reverse_proxy_base)
            except ValueError as e:
                return jsonify({"status": "error", "error": str(e)}), 400

        if not url:
            return jsonify({
                "status": "error",
                "error": "'url' is required"
            }), 400

        solve_timeout = self.DEFAULT_SOLVE_TIMEOUT_SECONDS
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
        logger.info(
            "Turnstile request accepted | task_id=%s url=%s sitekey=%s browser=%s headless=%s timeout=%s "
            "proxy_override=%s reverse_proxy=%s reverse_proxy_style=%s thread=%s",
            task_id,
            self._trim_text(url, 180),
            "provided" if sitekey else "none",
            requested_browser_type,
            self.headless,
            solve_timeout,
            proxy_cfg_override.get("server") if proxy_cfg_override else None,
            reverse_proxy_base or None,
            reverse_proxy_style_effective,
            self.thread_count,
        )

        try:
            self.app.add_background_task(
                self._solve_turnstile,
                task_id,
                url,
                sitekey,
                action,
                cdata,
                solve_timeout,
                requested_browser_type,
                proxy_cfg_override,
                reverse_proxy_base,
                reverse_proxy_style_effective,
            )

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({"task_id": task_id}), 202
        except Exception as e:
            logger.exception(f"Unexpected error processing request task_id={task_id}: {str(e)}")
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
            logger.warning(
                "Turnstile result failure | task_id=%s reason=%s message=%s elapsed=%s",
                task_id,
                result.get("reason"),
                result.get("message"),
                result.get("elapsed_time"),
            )

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
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the default browser type for the solver. Supported options: chromium, playwright, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=1, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Pick a random proxy from proxies.txt for each solve')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5000', help='Set the port for the API solver to listen on. (Default: 5000)')
    parser.add_argument('--close-delay', type=float, default=0, help='Keep the browser page open for this many seconds after success or failure before cleanup.')
    return parser.parse_args()


def create_app(
    headless: bool,
    useragent: str,
    debug: bool,
    browser_type: str,
    thread: int,
    proxy_support: bool,
    close_delay: float = 0,
) -> Quart:
    server = TurnstileAPIServer(
        headless=headless,
        useragent=useragent,
        debug=debug,
        browser_type=browser_type,
        thread=thread,
        proxy_support=proxy_support,
        close_delay=close_delay,
    )
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = TurnstileAPIServer._supported_browser_types()
    
    if args.browser_type not in browser_types:
        if args.browser_type == 'playwright' and not PLAYWRIGHT_NATIVE_AVAILABLE:
            logger.error(f"Playwright is not available. Please install playwright or use a different browser type. Available browser types: {browser_types}")
        elif args.browser_type == 'camoufox' and not CAMOUFOX_AVAILABLE:
            logger.error(f"Camoufox is not available. Please install camoufox or use a different browser type. Available browser types: {browser_types}")
        else:
            logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    elif args.headless is True and args.useragent is None and args.browser_type != 'camoufox':
        if CAMOUFOX_AVAILABLE:
            logger.error(f"You must specify a {COLORS.get('YELLOW')}User-Agent{COLORS.get('RESET')} for Turnstile Solver or use {COLORS.get('GREEN')}camoufox{COLORS.get('RESET')} without useragent")
        else:
            logger.error(f"You must specify a {COLORS.get('YELLOW')}User-Agent{COLORS.get('RESET')} for Turnstile Solver when using headless mode")
    else:
        app = create_app(
            headless=args.headless,
            debug=args.debug,
            useragent=args.useragent,
            browser_type=args.browser_type,
            thread=args.thread,
            proxy_support=args.proxy,
            close_delay=args.close_delay,
        )
        # Disable Quart's development file watcher in Docker. The watcher has
        # crashed this long-running solver with `PosixPath object is not
        # callable`, which resets API calls while the caller is waiting for the
        # first Turnstile session to be cached.
        app.run(host=args.host, port=int(args.port), use_reloader=False)
