import os
import sys
import time
import uuid
import json
import base64
import random
import logging
import asyncio
import argparse
import re
from typing import Any, Dict, List, Optional, Set
from http.cookies import SimpleCookie
from email.utils import parsedate_to_datetime
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

try:
    from seleniumbase import Driver as SeleniumBaseDriver
    SELENIUMBASE_AVAILABLE = True
except ImportError:
    SeleniumBaseDriver = None
    SELENIUMBASE_AVAILABLE = False


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
    DEFAULT_SOLVE_TIMEOUT_SECONDS = 120.0
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
        self.close_delay = max(0.0, float(close_delay or 6))
        self.useragent = useragent
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.browser_runtimes: List[Any] = []
        self.browser_args = ["--disable-blink-features=AutomationControlled"]
        self._seleniumbase_gui_click_blocked = False
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

    @staticmethod
    def _parse_cookie_header_pairs(cookie_header: Optional[str]) -> List[Dict[str, str]]:
        raw = str(cookie_header or "").strip()
        if not raw:
            return []
        pairs: List[Dict[str, str]] = []
        seen: Set[str] = set()
        for segment in raw.split(";"):
            part = segment.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookie_name = name.strip()
            if not cookie_name:
                continue
            lowered = cookie_name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            pairs.append({
                "name": cookie_name,
                "value": value.strip(),
            })
        return pairs

    @staticmethod
    def _cookie_bootstrap_url_for_target(target_url: str) -> str:
        normalized = TurnstileAPIServer._normalize_page_url(target_url)
        parsed = urlparse(normalized)
        if not parsed.netloc:
            return ""
        return urlunparse((parsed.scheme or "https", parsed.netloc, "/", "", "", ""))

    async def _apply_injected_cookies_to_context(
        self,
        context,
        target_url: str,
        cookie_header: Optional[str],
    ) -> int:
        cookies = self._parse_cookie_header_pairs(cookie_header)
        if not cookies:
            return 0
        bootstrap_url = self._cookie_bootstrap_url_for_target(target_url)
        if not bootstrap_url:
            return 0
        payload = [{"name": cookie["name"], "value": cookie["value"], "url": bootstrap_url} for cookie in cookies]
        await context.add_cookies(payload)
        return len(payload)

    def _apply_injected_cookies_to_webdriver(
        self,
        driver,
        target_url: str,
        cookie_header: Optional[str],
    ) -> int:
        cookies = self._parse_cookie_header_pairs(cookie_header)
        if not cookies:
            return 0
        bootstrap_url = self._cookie_bootstrap_url_for_target(target_url)
        if not bootstrap_url:
            return 0
        target_host = (urlparse(bootstrap_url).hostname or "").lower()
        if not target_host:
            return 0

        self._seleniumbase_ensure_connected(driver, context="inject_cookie_bootstrap")
        try:
            driver.get(bootstrap_url)
        except Exception:
            if not self._seleniumbase_ensure_connected(driver, context="inject_cookie_bootstrap_get"):
                return 0
            try:
                driver.get(bootstrap_url)
            except Exception:
                return 0

        added = 0
        for cookie in cookies:
            payload = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": target_host,
                "path": "/",
            }
            try:
                driver.add_cookie(payload)
                added += 1
            except Exception:
                if self._seleniumbase_ensure_connected(driver, context="inject_cookie_add"):
                    try:
                        driver.add_cookie(payload)
                        added += 1
                        continue
                    except Exception:
                        pass
                continue
        return added

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

    def _fetch_reverse_proxy_document_sync(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        read_body: bool = True,
    ) -> Dict[str, Any]:
        req_headers = {}
        for key, value in (headers or {}).items():
            lower = str(key).lower()
            if lower in {"host", "connection", "content-length", "accept-encoding"}:
                continue
            req_headers[str(key)] = str(value)
        req_headers["Accept-Encoding"] = "identity"

        request_obj = urllib_request.Request(url, headers=req_headers, method=(method or "GET").upper(), data=data)
        try:
            with urllib_request.urlopen(request_obj, timeout=60) as response:
                raw = response.read() if read_body else b""
                hdrs = response.headers
                header_items = dict(hdrs.items())
                content_type = header_items.get("Content-Type") or header_items.get("content-type") or ""
                return {
                    "status": getattr(response, "status", 200),
                    "headers": header_items,
                    "set_cookie_headers": list(hdrs.get_all("Set-Cookie") or []),
                    "text": self._decode_http_body(raw, content_type),
                }
        except urllib_error.HTTPError as e:
            raw = e.read() if read_body else b""
            hdrs = e.headers
            header_items = dict(hdrs.items()) if hdrs else {}
            content_type = header_items.get("Content-Type") or header_items.get("content-type") or ""
            return {
                "status": getattr(e, "code", 599),
                "headers": header_items,
                "set_cookie_headers": list(hdrs.get_all("Set-Cookie") or []) if hdrs else [],
                "text": self._decode_http_body(raw, content_type),
            }
        except Exception as e:
            return {
                "status": 599,
                "headers": {},
                "set_cookie_headers": [],
                "text": "",
                "error": self._trim_text(e, 240),
            }

    @staticmethod
    def _cookie_domains_for_target_host(target_host: str) -> Set[str]:
        host = (target_host or "").lower().strip(".")
        if not host:
            return set()
        domains = {host}
        parts = host.split(".")
        if len(parts) >= 2:
            domains.add(".".join(parts[-2:]))
        if len(parts) >= 3:
            domains.add(".".join(parts[-3:]))
        return domains

    def _parse_set_cookie_to_webdriver_cookie(
        self,
        set_cookie_value: str,
        target_host: str,
    ) -> Optional[Dict[str, Any]]:
        raw = (set_cookie_value or "").strip()
        if not raw:
            return None
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except Exception:
            parsed = SimpleCookie()
        if not parsed:
            parts = raw.split(";", 1)
            if not parts or "=" not in parts[0]:
                return None
            name, value = parts[0].split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                return None
            return {
                "name": name,
                "value": value,
                "path": "/",
                "domain": target_host,
            }

        allowed_domains = self._cookie_domains_for_target_host(target_host)
        candidates: List[Dict[str, Any]] = []
        for morsel in parsed.values():
            name = (morsel.key or "").strip()
            value = morsel.value or ""
            if not name:
                continue
            domain = (morsel["domain"] or target_host).strip().lstrip(".").lower()
            if allowed_domains and domain not in allowed_domains:
                if not any(domain == ad or domain.endswith("." + ad) for ad in allowed_domains):
                    continue
            cookie_out: Dict[str, Any] = {
                "name": name,
                "value": value,
                "path": (morsel["path"] or "/"),
                "domain": domain or target_host,
            }
            if morsel["secure"]:
                cookie_out["secure"] = True
            if morsel["httponly"]:
                cookie_out["httpOnly"] = True
            same_site = (morsel["samesite"] or "").strip()
            if same_site:
                normalized = same_site.capitalize()
                if normalized in {"Lax", "Strict", "None"}:
                    cookie_out["sameSite"] = normalized
            expires = (morsel["expires"] or "").strip()
            if expires:
                try:
                    dt = parsedate_to_datetime(expires)
                    if dt is not None:
                        cookie_out["expiry"] = int(dt.timestamp())
                except Exception:
                    pass
            candidates.append(cookie_out)

        if not candidates:
            return None

        by_name: Dict[str, Dict[str, Any]] = {}
        for cookie_out in candidates:
            lower = str(cookie_out.get("name") or "").strip().lower()
            if not lower:
                continue
            by_name[lower] = cookie_out

        for cookie_out in reversed(candidates):
            lower = str(cookie_out.get("name") or "").strip().lower()
            if not lower:
                continue
            if by_name.get(lower) is cookie_out and not TurnstileAPIServer._is_deleted_cookie_value(cookie_out.get("value")):
                return cookie_out

        return by_name.get(str(candidates[-1].get("name") or "").strip().lower()) or candidates[-1]

    @staticmethod
    def _cookie_identity(cookie: Dict[str, Any]) -> Optional[tuple]:
        name = str(cookie.get("name") or "").strip()
        if not name:
            return None
        domain = str(cookie.get("domain") or "").strip().lstrip(".").lower()
        path = str(cookie.get("path") or "/").strip() or "/"
        return (name, domain, path)

    @staticmethod
    def _is_deleted_cookie_value(value: Any) -> bool:
        return str(value or "").strip().lower() == "deleted"

    def _parse_set_cookie_headers_to_cookies(
        self,
        set_cookie_headers: List[str],
        target_hosts: Set[str],
    ) -> List[Dict[str, Any]]:
        if not set_cookie_headers:
            return []

        hosts = [h.strip().lstrip(".").lower() for h in (target_hosts or set()) if h]
        parsed: List[Dict[str, Any]] = []
        index: Dict[tuple, int] = {}

        for raw_cookie in set_cookie_headers:
            raw = (raw_cookie or "").strip()
            if not raw:
                continue

            parsed_cookie: Optional[Dict[str, Any]] = None
            if hosts:
                for host in hosts:
                    parsed_cookie = self._parse_set_cookie_to_webdriver_cookie(raw, host)
                    if parsed_cookie:
                        break
            else:
                parsed_cookie = self._parse_set_cookie_to_webdriver_cookie(raw, "")

            if not parsed_cookie:
                continue

            identity = self._cookie_identity(parsed_cookie)
            if identity and identity in index:
                parsed[index[identity]] = parsed_cookie
                continue
            if identity:
                index[identity] = len(parsed)
            parsed.append(parsed_cookie)

        return parsed

    def _merge_cookies_with_set_cookie_headers(
        self,
        cookies: List[Dict[str, Any]],
        set_cookie_headers: List[str],
        target_hosts: Set[str],
    ) -> List[Dict[str, Any]]:
        base_cookies = [dict(cookie) for cookie in (cookies or [])]
        if not set_cookie_headers:
            return base_cookies

        extra = self._parse_set_cookie_headers_to_cookies(set_cookie_headers, target_hosts)
        if not extra:
            return base_cookies

        # Drop seed cookies for any names that were actually refreshed by Set-Cookie.
        refreshed_names = {
            str(cookie.get("name") or "").strip().lower()
            for cookie in extra
            if str(cookie.get("name") or "").strip()
        }
        merged: List[Dict[str, Any]] = [
            dict(cookie)
            for cookie in base_cookies
            if str(cookie.get("name") or "").strip().lower() not in refreshed_names
        ]

        index: Dict[tuple, int] = {}
        for i, cookie in enumerate(merged):
            identity = self._cookie_identity(cookie)
            if identity:
                index[identity] = i

        for cookie in extra:
            identity = self._cookie_identity(cookie)
            if not identity:
                continue
            existing = index.get(identity)
            candidate_deleted = self._is_deleted_cookie_value(cookie.get("value"))
            if existing is None:
                if candidate_deleted:
                    continue
                index[identity] = len(merged)
                merged.append(cookie)
            else:
                current = merged[existing]
                if candidate_deleted and not self._is_deleted_cookie_value(current.get("value")):
                    continue
                merged[existing] = {**merged[existing], **cookie}

        return merged

    def _apply_reverse_proxy_cookies_to_target(
        self,
        driver,
        target_url: str,
        set_cookie_headers: List[str],
    ) -> int:
        if not set_cookie_headers:
            return 0
        self._seleniumbase_ensure_connected(driver, context="reverse_proxy_cookie_bootstrap")
        parsed_target = urlparse(target_url)
        target_host = (parsed_target.hostname or "").lower()
        if not target_host:
            return 0
        bootstrap = urlunparse((parsed_target.scheme or "https", parsed_target.netloc, "/", "", "", ""))
        try:
            driver.get(bootstrap)
        except Exception:
            if not self._seleniumbase_ensure_connected(driver, context="reverse_proxy_cookie_bootstrap_get"):
                return 0
            try:
                driver.get(bootstrap)
            except Exception:
                return 0

        added = 0
        for raw_cookie in set_cookie_headers:
            cookie = self._parse_set_cookie_to_webdriver_cookie(raw_cookie, target_host)
            if not cookie:
                continue
            try:
                driver.add_cookie(cookie)
                added += 1
            except Exception:
                if self._seleniumbase_ensure_connected(driver, context="reverse_proxy_cookie_add"):
                    try:
                        driver.add_cookie(cookie)
                        added += 1
                        continue
                    except Exception:
                        pass
                continue
        return added

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

    @staticmethod
    def _seleniumbase_proxy_string(proxy: Optional[Dict[str, str]]) -> Optional[str]:
        """Convert parsed proxy dict into SeleniumBase Driver proxy string."""
        if not proxy:
            return None
        server = (proxy.get("server") or "").strip()
        if not server:
            return None
        parsed = urlparse(server)
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return None
        scheme = (parsed.scheme or "").lower()
        username = (proxy.get("username") or "").strip()
        password = (proxy.get("password") or "").strip()
        auth = f"{username}:{password}@" if username or password else ""

        target = f"{host}:{port}"
        # SeleniumBase proxy auth parser expects:
        #   username:password@hostname:port          (HTTP)
        #   username:password@https://hostname:port  (HTTPS)
        # Keep scheme only when not plain HTTP so proxy protocol survives.
        if scheme and scheme != "http":
            target = f"{scheme}://{target}"

        return f"{auth}{target}"

    def _build_seleniumbase_driver_kwargs(
        self,
        proxy_cfg: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        screen_width = self._screen_width()
        screen_height = self._screen_height()
        kwargs: Dict[str, Any] = {
            "browser": "chrome",
            "headless": self.headless,
            "agent": self.useragent,
            "uc": self._seleniumbase_uc_mode(),
            "window_size": f"{screen_width},{screen_height}",
        }
        proxy_str = self._seleniumbase_proxy_string(proxy_cfg)
        if proxy_str:
            kwargs["proxy"] = proxy_str
        return {k: v for k, v in kwargs.items() if v is not None}

    @staticmethod
    def _proxy_has_auth(proxy_cfg: Optional[Dict[str, str]]) -> bool:
        if not proxy_cfg:
            return False
        return bool((proxy_cfg.get("username") or "").strip() or (proxy_cfg.get("password") or "").strip())

    def _seleniumbase_open_url(self, driver, url: str, proxy_cfg: Optional[Dict[str, str]] = None) -> None:
        """
        Open URL with SeleniumBase driver while accounting for UC + authenticated proxy behavior.

        SeleniumBase maintainer guidance for recent Chrome versions indicates that
        UC mode with authenticated proxy may require CDP mode activation before
        browsing for auth to take effect.
        """
        use_uc = self._seleniumbase_uc_mode()
        proxy_auth = self._proxy_has_auth(proxy_cfg)
        if use_uc and proxy_auth:
            cdp_activate = getattr(driver, "uc_activate_cdp_mode", None)
            if not callable(cdp_activate):
                cdp_activate = getattr(driver, "activate_cdp_mode", None)
            if callable(cdp_activate):
                try:
                    if self.debug:
                        logger.debug("Browser SB: UC + proxy auth detected; navigating via CDP mode")
                    cdp_activate(url)
                    # CDP activation can leave raw WebDriver socket disconnected.
                    # Reconnect now because downstream solver logic uses
                    # WebDriver cookie/script APIs.
                    self._seleniumbase_ensure_connected(driver, context="post_uc_cdp_activate")
                    return
                except Exception as cdp_error:
                    if self.debug:
                        logger.warning(
                            "Browser SB: CDP navigation failed for UC + proxy auth; falling back to Selenium get(). error=%s",
                            self._trim_text(cdp_error, 240),
                        )
        nav_fn = getattr(driver, "default_get", None) if use_uc else None
        if callable(nav_fn):
            nav_fn(url)
            return
        driver.get(url)

    def _seleniumbase_ensure_connected(self, driver, context: str = "") -> bool:
        """Best-effort reconnect for UC/CDP flows before raw WebDriver-only commands."""
        if driver is None:
            return False

        is_connected_fn = getattr(driver, "is_connected", None)
        connected = True
        if callable(is_connected_fn):
            try:
                connected = bool(is_connected_fn())
            except Exception:
                connected = True
        if connected:
            return True

        reconnect_error = None
        reconnect_fn = getattr(driver, "reconnect", None)
        if callable(reconnect_fn):
            try:
                reconnect_fn(timeout=0.1)
            except TypeError:
                try:
                    reconnect_fn(0.1)
                except Exception as e:
                    reconnect_error = e
            except Exception as e:
                reconnect_error = e
        else:
            connect_fn = getattr(driver, "connect", None)
            if callable(connect_fn):
                try:
                    connect_fn()
                except Exception as e:
                    reconnect_error = e

        if callable(is_connected_fn):
            try:
                if bool(is_connected_fn()):
                    if self.debug:
                        logger.debug(
                            "Browser SB: WebDriver reconnected%s",
                            f" ({context})" if context else "",
                        )
                    return True
            except Exception:
                pass

        if self.debug:
            logger.warning(
                "Browser SB: WebDriver reconnect attempt failed%s. error=%s",
                f" ({context})" if context else "",
                self._trim_text(reconnect_error, 220) if reconnect_error else "unknown",
            )
        return False

    def _seleniumbase_get_cookies_safe(
        self,
        driver,
        hosts: Set[str],
        *,
        context: str = "",
    ) -> List[Dict[str, Any]]:
        """Fetch cookies with a reconnect retry on transient UC/CDP disconnects."""
        try:
            return self._filter_cookies_for_hosts(driver.get_cookies(), hosts)
        except Exception as first_error:
            if self._seleniumbase_ensure_connected(driver, context=f"{context}:get_cookies_retry"):
                try:
                    return self._filter_cookies_for_hosts(driver.get_cookies(), hosts)
                except Exception as second_error:
                    raise RuntimeError(
                        "Unable to fetch SeleniumBase cookies after reconnect attempt: "
                        f"{self._trim_text(second_error, 220)}"
                    ) from second_error
            raise RuntimeError(
                "Unable to fetch SeleniumBase cookies; WebDriver appears disconnected: "
                f"{self._trim_text(first_error, 220)}"
            ) from first_error

    def _seleniumbase_uc_mode(self) -> bool:
        raw = (os.environ.get("SELENIUMBASE_UC") or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        # Default OFF for Docker/API stability. Enable explicitly when needed.
        return False

    @staticmethod
    def _seleniumbase_prewarm_enabled() -> bool:
        raw = (os.environ.get("SELENIUMBASE_PREWARM") or "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        # Default on: avoid first-request driver download races.
        return True

    def _create_seleniumbase_driver(self, proxy_cfg: Optional[Dict[str, str]] = None):
        if not SELENIUMBASE_AVAILABLE or SeleniumBaseDriver is None:
            raise RuntimeError("SeleniumBase is not available in this environment.")
        kwargs = self._build_seleniumbase_driver_kwargs(proxy_cfg)
        try:
            return SeleniumBaseDriver(**kwargs)
        except Exception as first_error:
            if kwargs.get("uc") is True:
                fallback_kwargs = dict(kwargs)
                fallback_kwargs["uc"] = False
                logger.warning(
                    "SeleniumBase UC launch failed; retrying with uc=False. error=%s",
                    self._trim_text(first_error, 240),
                )
                return SeleniumBaseDriver(**fallback_kwargs)
            raise

    def _prewarm_seleniumbase_driver_sync(self) -> bool:
        driver = None
        try:
            driver = self._create_seleniumbase_driver()
            driver.get("about:blank")
            return True
        except Exception as e:
            logger.warning(
                "SeleniumBase prewarm failed; continuing without prewarm. error=%s",
                self._trim_text(e, 240),
            )
            return False
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    @staticmethod
    def _safe_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(value, max_value))

    def _screen_width(self) -> int:
        # Prefer explicit solver viewport knobs, then Xvfb knobs.
        if (os.environ.get("SOLVER_VIEWPORT_WIDTH") or "").strip():
            return self._safe_int_env("SOLVER_VIEWPORT_WIDTH", 1920, 640, 7680)
        return self._safe_int_env("XVFB_SCREEN_WIDTH", 1920, 640, 7680)

    def _screen_height(self) -> int:
        if (os.environ.get("SOLVER_VIEWPORT_HEIGHT") or "").strip():
            return self._safe_int_env("SOLVER_VIEWPORT_HEIGHT", 1080, 480, 4320)
        return self._safe_int_env("XVFB_SCREEN_HEIGHT", 1080, 480, 4320)

    @staticmethod
    def _device_scale_factor() -> float:
        raw = (os.environ.get("SOLVER_DEVICE_SCALE_FACTOR") or "").strip()
        if not raw:
            return 1.0
        try:
            value = float(raw)
        except Exception:
            return 1.0
        if value < 0.5:
            return 0.5
        if value > 3.0:
            return 3.0
        return value

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
            if self.browser_type in ("chromium", "chrome", "msedge", "seleniumbase"):
                raise ValueError(
                    "Chromium (chromium/chrome/msedge/seleniumbase) does not support SOCKS proxy authentication. "
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
        if SELENIUMBASE_AVAILABLE:
            browser_types.append("seleniumbase")
        return browser_types

    def _normalize_browser_type(self, browser_type: Optional[str]) -> str:
        value = (browser_type or self.browser_type or "chromium").strip().lower()
        if value == "playwrite":
            value = "playwright"
        if value in ("selenium", "sb"):
            value = "seleniumbase"
        if value not in self._supported_browser_types():
            raise ValueError(
                f"Unknown browser type '{value}'. Available browser types: {self._supported_browser_types()}"
            )
        if value == "playwright" and not PLAYWRIGHT_NATIVE_AVAILABLE:
            raise ValueError("Playwright is not available. Please install playwright or use a different browser type.")
        if value == "camoufox" and not CAMOUFOX_AVAILABLE:
            raise ValueError("Camoufox is not available. Please install camoufox or use a different browser type.")
        if value == "seleniumbase" and not SELENIUMBASE_AVAILABLE:
            raise ValueError("SeleniumBase is not available. Please install seleniumbase or use a different browser type.")
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

        if normalized == "seleniumbase":
            driver = await asyncio.to_thread(self._create_seleniumbase_driver)
            return None, driver

        camoufox = AsyncCamoufox(headless=self.headless)
        browser = await camoufox.start()
        return camoufox, browser

    async def _acquire_browser(self, browser_type_override: Optional[str] = None):
        requested_browser_type = self._normalize_browser_type(browser_type_override)
        if requested_browser_type == "seleniumbase":
            runtime, browser = await self._launch_browser_instance(requested_browser_type)
            released = False

            async def _release():
                nonlocal released
                if released:
                    return
                released = True
                try:
                    await asyncio.to_thread(browser.quit)
                except Exception:
                    pass
                stop = getattr(runtime, "stop", None)
                if callable(stop):
                    try:
                        await stop()
                    except Exception:
                        pass

            return 0, browser, requested_browser_type, _release

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
        screen_width = self._screen_width()
        screen_height = self._screen_height()
        dpr = self._device_scale_factor()
        opts: Dict[str, Any] = {
            "viewport": {"width": screen_width, "height": screen_height},
            "screen": {"width": screen_width, "height": screen_height},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "light",
            "device_scale_factor": dpr,
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
        merged_by_name: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for cookie in (cookies or []):
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            lower = name.lower()
            value = str(cookie.get("value") or "")
            existing = merged_by_name.get(lower)
            if existing is None:
                merged_by_name[lower] = {"name": name, "value": value}
                order.append(lower)
                continue
            existing_deleted = TurnstileAPIServer._is_deleted_cookie_value(existing.get("value"))
            incoming_deleted = TurnstileAPIServer._is_deleted_cookie_value(value)
            if existing_deleted and not incoming_deleted:
                merged_by_name[lower] = {"name": name, "value": value}
                continue
            if not existing_deleted and incoming_deleted:
                continue
            merged_by_name[lower] = {"name": name, "value": value}
        return "; ".join(
            f"{merged_by_name[key]['name']}={merged_by_name[key]['value']}"
            for key in order
            if key in merged_by_name
        )

    @staticmethod
    def _format_cookie_header_excluding_injected_d(cookies: List[Dict[str, Any]], injected_cookie_header: Optional[str] = None) -> str:
        if not injected_cookie_header:
            return TurnstileAPIServer._format_cookie_header(cookies)
        injected_map: Dict[str, str] = {}
        for cookie in TurnstileAPIServer._parse_cookie_header_pairs(injected_cookie_header):
            injected_map[str(cookie.get("name") or "").strip().lower()] = str(cookie.get("value") or "").strip()
        filtered: List[Dict[str, Any]] = []
        for cookie in (cookies or []):
            name = str(cookie.get("name") or "").strip().lower()
            value = str(cookie.get("value") or "").strip()
            if name == "d" and injected_map.get("d") == value:
                continue
            filtered.append(cookie)
        return TurnstileAPIServer._format_cookie_header(filtered)

    @staticmethod
    def _select_last_response_cookie_value(set_cookie_headers: List[str], cookie_name: str) -> Optional[str]:
        target_name = str(cookie_name or "").strip().lower()
        if not target_name:
            return None
        selected = None
        for raw in (set_cookie_headers or []):
            parsed = SimpleCookie()
            try:
                parsed.load(str(raw or '').strip())
            except Exception:
                continue
            for morsel in parsed.values():
                name = str(morsel.key or '').strip().lower()
                if name != target_name:
                    continue
                value = str(morsel.value or '').strip()
                if not value or TurnstileAPIServer._is_deleted_cookie_value(value):
                    continue
                selected = value
        return selected

    @staticmethod
    def _select_last_response_d_value(set_cookie_headers: List[str]) -> Optional[str]:
        return TurnstileAPIServer._select_last_response_cookie_value(set_cookie_headers, 'd')

    @staticmethod
    def _normalize_cookie_store(cookies: List[Dict[str, Any]], injected_cookie_header: Optional[str] = None) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        by_name: Dict[str, int] = {}
        for cookie in (cookies or []):
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            lower = name.lower()
            value = str(cookie.get("value") or "")
            existing_index = by_name.get(lower)
            if existing_index is None:
                if TurnstileAPIServer._is_deleted_cookie_value(value):
                    continue
                by_name[lower] = len(normalized)
                normalized.append(dict(cookie))
                continue

            current = normalized[existing_index]
            current_deleted = TurnstileAPIServer._is_deleted_cookie_value(current.get("value"))
            incoming_deleted = TurnstileAPIServer._is_deleted_cookie_value(value)
            if incoming_deleted and not current_deleted:
                continue
            normalized[existing_index] = dict(cookie)

        return normalized

    @staticmethod
    def _replace_cookie_value_by_name(
        cookies: List[Dict[str, Any]],
        name: str,
        value: str,
    ) -> List[Dict[str, Any]]:
        target_name = str(name or "").strip().lower()
        if not target_name:
            return [dict(cookie) for cookie in (cookies or [])]
        updated: List[Dict[str, Any]] = []
        template: Optional[Dict[str, Any]] = None
        for cookie in (cookies or []):
            lower = str(cookie.get("name") or "").strip().lower()
            if lower != target_name:
                updated.append(dict(cookie))
                continue
            if template is None:
                template = dict(cookie)
        if template is None:
            template = {"name": name, "domain": "", "path": "/"}
        template = dict(template)
        template["name"] = name
        template["value"] = value
        updated.append(template)
        return updated

    @staticmethod
    def _merge_cookie_stores(
        base_cookies: List[Dict[str, Any]],
        overlay_cookies: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = [dict(cookie) for cookie in (base_cookies or [])]
        index: Dict[tuple, int] = {}
        for i, cookie in enumerate(merged):
            identity = TurnstileAPIServer._cookie_identity(cookie)
            if identity:
                index[identity] = i

        for cookie in (overlay_cookies or []):
            identity = TurnstileAPIServer._cookie_identity(cookie)
            if not identity:
                continue
            existing = index.get(identity)
            candidate_deleted = TurnstileAPIServer._is_deleted_cookie_value(cookie.get("value"))
            if existing is None:
                if candidate_deleted:
                    continue
                index[identity] = len(merged)
                merged.append(dict(cookie))
            else:
                current = merged[existing]
                if candidate_deleted and not TurnstileAPIServer._is_deleted_cookie_value(current.get("value")):
                    continue
                merged[existing] = {**current, **cookie}

        return merged

    @staticmethod
    def _overlay_injected_non_d_cookies(
        cookies: List[Dict[str, Any]],
        injected_cookie_header: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not injected_cookie_header:
            return [dict(cookie) for cookie in (cookies or [])]
        merged: List[Dict[str, Any]] = [dict(cookie) for cookie in (cookies or [])]
        for injected in TurnstileAPIServer._parse_cookie_header_pairs(injected_cookie_header):
            name = str(injected.get("name") or "").strip()
            value = str(injected.get("value") or "").strip()
            if not name:
                continue
            lower = name.lower()
            if lower == "d":
                continue
            replaced = False
            for cookie in merged:
                if str(cookie.get("name") or "").strip().lower() != lower:
                    continue
                cookie["name"] = name
                cookie["value"] = value
                replaced = True
            if not replaced:
                merged.append({"name": name, "value": value})
        return merged

    @staticmethod
    def _cookie_value_map(
        cookies: List[Dict[str, Any]],
        injected_cookie_header: Optional[str] = None,
    ) -> Dict[str, str]:
        cookie_map: Dict[str, str] = {}
        for cookie in TurnstileAPIServer._normalize_cookie_store(cookies or []):
            name = str(cookie.get("name") or "").strip().lower()
            if not name:
                continue
            value = str(cookie.get("value") or "").strip()
            cookie_map[name] = value
        return cookie_map

    @staticmethod
    def _has_d_and_locl(cookies: List[Dict[str, Any]], injected_cookie_header: Optional[str] = None) -> bool:
        cookie_map = TurnstileAPIServer._cookie_value_map(cookies, injected_cookie_header)
        d_value = cookie_map.get("d", "")
        locl_value = cookie_map.get("locl", "")
        if not d_value or not locl_value:
            return False
        if TurnstileAPIServer._is_deleted_cookie_value(d_value):
            return False
        if TurnstileAPIServer._is_deleted_cookie_value(locl_value):
            return False
        if injected_cookie_header:
            injected_map: Dict[str, str] = {}
            for cookie in TurnstileAPIServer._parse_cookie_header_pairs(injected_cookie_header):
                injected_map[str(cookie.get("name") or "").strip().lower()] = str(cookie.get("value") or "").strip()
            injected_d = injected_map.get("d", "")
            if injected_d and injected_d == d_value:
                return False
        return True

    @staticmethod
    def _has_new_d_after_injected(cookies: List[Dict[str, Any]], injected_cookie_header: Optional[str] = None) -> bool:
        if not injected_cookie_header:
            return TurnstileAPIServer._has_d_and_locl(cookies)
        injected_map: Dict[str, str] = {}
        for cookie in TurnstileAPIServer._parse_cookie_header_pairs(injected_cookie_header):
            injected_map[str(cookie.get("name") or "").strip().lower()] = str(cookie.get("value") or "").strip()
        cookie_map = TurnstileAPIServer._cookie_value_map(cookies, None)
        d_value = cookie_map.get("d", "")
        locl_value = cookie_map.get("locl", "")
        if not d_value or not locl_value:
            return False
        if TurnstileAPIServer._is_deleted_cookie_value(d_value):
            return False
        if TurnstileAPIServer._is_deleted_cookie_value(locl_value):
            return False
        injected_d = injected_map.get("d", "")
        if injected_d and d_value == injected_d:
            return False
        return True

    @staticmethod
    def _has_cf_clearance(cookies: List[Dict[str, Any]]) -> bool:
        names = {c.get("name") for c in cookies}
        return "cf_clearance" in names

    @staticmethod
    def _require_d_and_locl_only() -> bool:
        raw = (os.environ.get("TURNSTILE_REQUIRE_D_LOCL") or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        # Default strict mode: keep solving until d+locl are captured.
        return True

    @staticmethod
    def _cf_clearance_grace_seconds() -> float:
        """
        Wait window before accepting cf_clearance-only sessions.
        Allows downstream Set-Cookie (d/locl/etc.) to arrive after challenge flow.
        """
        raw = (os.environ.get("TURNSTILE_CF_CLEARANCE_GRACE_SECONDS") or "").strip()
        if not raw:
            return 10.0
        try:
            value = float(raw)
        except Exception:
            return 10.0
        if value < 0:
            return 0.0
        if value > 120:
            return 120.0
        return value

    @staticmethod
    def _turnstile_token_post_wait_seconds() -> float:
        raw = (os.environ.get("TURNSTILE_TOKEN_POST_WAIT_SECONDS") or "").strip()
        if not raw:
            return 15.0
        try:
            value = float(raw)
        except Exception:
            return 15.0
        if value < 0:
            return 0.0
        if value > 120:
            return 120.0
        return value

    def _has_usable_session_cookies(self, cookies: List[Dict[str, Any]]) -> bool:
        if self._has_d_and_locl(cookies):
            return True
        if self._require_d_and_locl_only():
            return False
        return self._has_cf_clearance(cookies)

    @staticmethod
    def _d_locl_cookie_header(cookies: List[Dict[str, Any]]) -> str:
        by: Dict[str, str] = {}
        for cookie in (cookies or []):
            name = str(cookie.get("name") or "").strip()
            if name not in ("d", "locl"):
                continue
            value = str(cookie.get("value") or "").strip()
            if not value or TurnstileAPIServer._is_deleted_cookie_value(value):
                continue
            by[name] = value
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
    async def _playwright_submit_turnstile_form(page, token: str) -> Dict[str, Any]:
        script = """(token) => {
            const t = token || "";
            let input = document.querySelector('[name="cf-turnstile-response"]');
            if (input) {
                try { input.value = t; } catch (e) {}
            } else if (t) {
                input = document.createElement("input");
                input.type = "hidden";
                input.name = "cf-turnstile-response";
                input.value = t;
            }
            let form = input ? input.closest("form") : null;
            if (!form) {
                form = document.querySelector("form");
                if (form && input && !input.closest("form")) {
                    try { form.appendChild(input); } catch (e) {}
                }
            }
            if (!form) {
                return { submitted: false, reason: "no_form" };
            }
            let prevented = false;
            try {
                const ev = new Event("submit", { bubbles: true, cancelable: true });
                form.dispatchEvent(ev);
                prevented = !!ev.defaultPrevented;
            } catch (e) {}
            try {
                if (!prevented) {
                    if (typeof form.requestSubmit === "function") form.requestSubmit();
                    else form.submit();
                }
                return {
                    submitted: true,
                    prevented,
                    action: form.action || location.href,
                    method: (form.method || "GET").toUpperCase(),
                };
            } catch (e) {
                return { submitted: false, prevented, reason: String(e || "") };
            }
        }"""
        try:
            result = await page.evaluate(script, token or "")
            if isinstance(result, dict):
                return result
            return {"submitted": bool(result)}
        except Exception as e:
            return {"submitted": False, "reason": TurnstileAPIServer._trim_text(e, 240)}

    def _seleniumbase_submit_turnstile_form(self, driver, token: str) -> Dict[str, Any]:
        script = """
            const t = arguments[0] || "";
            let input = document.querySelector('[name="cf-turnstile-response"]');
            if (input) {
                try { input.value = t; } catch (e) {}
            } else if (t) {
                input = document.createElement("input");
                input.type = "hidden";
                input.name = "cf-turnstile-response";
                input.value = t;
            }
            let form = input ? input.closest("form") : null;
            if (!form) {
                form = document.querySelector("form");
                if (form && input && !input.closest("form")) {
                    try { form.appendChild(input); } catch (e) {}
                }
            }
            if (!form) {
                return { submitted: false, reason: "no_form" };
            }
            let prevented = false;
            try {
                const ev = new Event("submit", { bubbles: true, cancelable: true });
                form.dispatchEvent(ev);
                prevented = !!ev.defaultPrevented;
            } catch (e) {}
            try {
                if (!prevented) {
                    if (typeof form.requestSubmit === "function") form.requestSubmit();
                    else form.submit();
                }
                return {
                    submitted: true,
                    prevented: prevented,
                    action: form.action || location.href,
                    method: (form.method || "GET").toUpperCase()
                };
            } catch (e) {
                return { submitted: false, prevented: prevented, reason: String(e || "") };
            }
        """
        try:
            result = driver.execute_script(script, token or "")
            return result if isinstance(result, dict) else {"submitted": bool(result)}
        except Exception as e:
            return {"submitted": False, "reason": self._trim_text(e, 240)}

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
        payload.setdefault("result_mode", "failure" if payload.get("value") == "CAPTCHA_FAIL" else "unknown")
        return payload

    def _log_failure_payload(self, browser_index: int, task_id: str, payload: Dict[str, Any]) -> None:
        page = payload.get("page") or {}
        ip_preflight = payload.get("ip_preflight") or {}
        logger.error(
            "Browser %s: Solve rejected | task_id=%s reason=%s message=%s final_url=%s title=%s "
            "nav_status=%s nav_status_text=%s nav_failure=%s widget_count=%s iframe_count=%s "
            "cookies=%s d_cookie=%s locl_cookie=%s cf_clearance=%s "
            "preflight_status=%s preflight_ip=%s preflight_error=%s solver_error=%s body_excerpt=%s",
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
            payload.get("error") or "",
            page.get("body_excerpt") or "",
        )

    @staticmethod
    def _seleniumbase_ip_preflight_placeholder(rev_base: Optional[str], rev_style: str) -> Dict[str, Any]:
        return {
            "enabled": False,
            "routing": "reverse_proxy" if rev_base else "direct",
            "reverse_proxy": rev_base or "",
            "reverse_proxy_style": rev_style,
            "note": "IP preflight capture is only available for Playwright backends.",
        }

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

    @staticmethod
    def _describe_result_payload(task_id: str, result: Any) -> Dict[str, Any]:
        if result == "CAPTCHA_NOT_READY":
            return {
                "task_id": task_id,
                "status": "pending",
                "result_mode": "pending",
            }

        if not isinstance(result, dict):
            text_value = str(result or "")
            return {
                "task_id": task_id,
                "status": "unknown",
                "result_mode": "unknown",
                "has_token": bool(text_value),
                "token_length": len(text_value),
            }

        value = result.get("value")
        cookies = result.get("cookies")
        cookie_names = result.get("cookie_names")
        if cookie_names is None and isinstance(cookies, list):
            cookie_names = [c.get("name") for c in cookies if isinstance(c, dict) and c.get("name")]
        cookie_names = cookie_names or []
        has_cookie_header = bool(result.get("cookie_header"))
        has_request_headers = bool(result.get("request_headers"))
        has_session_cookies = bool(cookie_names or has_cookie_header)
        has_token = isinstance(value, str) and len(value.strip()) > 0 and value != "CAPTCHA_FAIL"
        has_d_cookie = bool(result.get("d_cookie_present")) or ("d" in cookie_names)
        has_locl_cookie = bool(result.get("locl_cookie_present")) or ("locl" in cookie_names)
        has_cf_clearance = bool(result.get("cf_clearance_present")) or ("cf_clearance" in cookie_names)

        if result.get("value") == "CAPTCHA_FAIL":
            status = "failure"
            result_mode = "failure"
        elif has_session_cookies:
            status = "ok"
            result_mode = "session_captured"
        elif has_token:
            status = "ok"
            result_mode = "token_only"
        else:
            status = "unknown"
            result_mode = "unknown"

        return {
            "task_id": task_id,
            "status": status,
            "result_mode": result_mode,
            "browser_type": result.get("browser_type"),
            "browser_backend": result.get("browser_backend"),
            "elapsed_time": result.get("elapsed_time"),
            "reason": result.get("reason"),
            "message": result.get("message"),
            "has_token": has_token,
            "token_length": len(value) if isinstance(value, str) else 0,
            "has_cookie_header": has_cookie_header,
            "has_request_headers": has_request_headers,
            "has_session_cookies": has_session_cookies,
            "cookie_names": cookie_names,
            "d_cookie_present": has_d_cookie,
            "locl_cookie_present": has_locl_cookie,
            "cf_clearance_present": has_cf_clearance,
            "ip_preflight": result.get("ip_preflight"),
            "has_turnstile_headers": bool(result.get("turnstile_headers")),
            "turnstile_request_count": len((result.get("turnstile_headers") or {}).get("requests") or []),
            "turnstile_response_count": len((result.get("turnstile_headers") or {}).get("responses") or []),
            "turnstile_failed_count": len((result.get("turnstile_headers") or {}).get("request_failed") or []),
            "url_initial": result.get("url_initial"),
            "url_final": result.get("url_final"),
        }

    @staticmethod
    def _result_cookie_names(result: Dict[str, Any]) -> Set[str]:
        names: Set[str] = set()
        for cookie in (result.get("cookies") or []):
            name = str(cookie.get("name") or "").strip()
            if name:
                names.add(name)
        for name in (result.get("cookie_names") or []):
            cleaned = str(name or "").strip()
            if cleaned:
                names.add(cleaned)
        return names

    @classmethod
    def _result_has_d_locl(cls, result: Dict[str, Any]) -> bool:
        cookies = result.get("cookies") or []
        if isinstance(cookies, list):
            return cls._has_d_and_locl(cookies)
        names = cls._result_cookie_names(result)
        return "d" in names and "locl" in names

    @classmethod
    def _result_quality_score(cls, result: Dict[str, Any]) -> int:
        if not isinstance(result, dict):
            return -999
        score = 0
        if result.get("value") != "CAPTCHA_FAIL":
            score += 10
        names = cls._result_cookie_names(result)
        if "cf_clearance" in names:
            score += 20
        if "d" in names:
            score += 40
        if "locl" in names:
            score += 40
        score += min(len(result.get("set_cookie_headers") or []), 20)
        return score

    def _selenium_fallback_browser(self) -> Optional[str]:
        raw = (os.environ.get("TURNSTILE_SELENIUM_FALLBACK_BROWSER") or "").strip().lower()
        if not raw:
            raw = "chromium"
        if raw in {"0", "false", "off", "none", "no"}:
            return None
        try:
            normalized = self._normalize_browser_type(raw)
        except ValueError:
            return None
        if normalized == "seleniumbase":
            return None
        return normalized

    @classmethod
    def _should_retry_after_selenium(cls, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return True
        if cls._result_has_d_locl(result):
            return False
        if result.get("value") == "CAPTCHA_FAIL":
            return True
        mode = str(result.get("result_mode") or "").strip().lower()
        return mode in {"session_captured", "token_only", "failure", ""}

    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/describe', methods=['GET'])(self.describe_result)

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Starting browser initialization")
        logger.info(
            "Display profile | viewport=%sx%s dpr=%s xvfb=%sx%sx%s dpi=%s",
            self._screen_width(),
            self._screen_height(),
            self._device_scale_factor(),
            os.environ.get("XVFB_SCREEN_WIDTH", "1920"),
            os.environ.get("XVFB_SCREEN_HEIGHT", "1080"),
            os.environ.get("XVFB_SCREEN_DEPTH", "24"),
            os.environ.get("XVFB_DPI", "96"),
        )
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        if self.browser_type == "seleniumbase":
            if self._seleniumbase_prewarm_enabled():
                logger.info("SeleniumBase prewarm enabled; creating and closing one driver at startup.")
                warmed = await asyncio.to_thread(self._prewarm_seleniumbase_driver_sync)
                if warmed:
                    logger.info("SeleniumBase prewarm completed.")
            else:
                logger.info("SeleniumBase prewarm disabled via SELENIUMBASE_PREWARM.")
            logger.success("SeleniumBase selected: browsers will launch per request (no persistent async pool).")
            return

        for _ in range(self.thread_count):
            runtime, browser = await self._launch_browser_instance(self.browser_type)
            self.browser_runtimes.append(runtime)
            await self.browser_pool.put((_+1, browser))

            if self.debug:
                logger.success(f"Browser {_ + 1} initialized successfully")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

    @staticmethod
    def _filter_cookies_for_hosts(
        cookies: List[Dict[str, Any]],
        hosts: Set[str],
    ) -> List[Dict[str, Any]]:
        if not hosts:
            return list(cookies or [])

        def _cookie_matches(domain: str) -> bool:
            d = (domain or "").lstrip(".").lower()
            if not d:
                return False
            return any(h == d or h.endswith("." + d) for h in hosts)

        return [c for c in (cookies or []) if _cookie_matches(c.get("domain", ""))]

    def _seleniumbase_click_turnstile(self, driver) -> None:
        if not self.headless and self._seleniumbase_gui_click_enabled():
            for method_name in ("uc_gui_click_cf", "uc_gui_click_captcha"):
                method = getattr(driver, method_name, None)
                if callable(method):
                    try:
                        method()
                        return
                    except BaseException as e:
                        msg = self._trim_text(e, 240)
                        if isinstance(e, SystemExit):
                            self._seleniumbase_gui_click_blocked = True
                            logger.warning(
                                "SeleniumBase GUI click disabled for this process after SystemExit from %s: %s",
                                method_name,
                                msg,
                            )
                        elif self.debug:
                            logger.debug(
                                "SeleniumBase GUI click method failed (%s): %s",
                                method_name,
                                msg,
                            )
                        pass

        click_script = """
            const selectors = [
                "div.cf-turnstile",
                ".cf-turnstile",
                "[data-sitekey]",
                "iframe[src*='challenges.cloudflare.com']",
                "iframe[src*='turnstile']",
                "iframe[title*='Cloudflare']"
            ];
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (!el) continue;
                try { el.scrollIntoView({block: "center", inline: "center"}); } catch (e) {}
                try { el.click(); return selector; } catch (e) {}
            }
            return "";
        """
        try:
            driver.execute_script(click_script)
        except Exception:
            pass

    def _seleniumbase_gui_click_enabled(self) -> bool:
        if self._seleniumbase_gui_click_blocked:
            return False
        raw = (os.environ.get("SELENIUMBASE_GUI_CLICK") or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        # Default OFF to avoid MouseInfo/tkinter SystemExit crashes in minimal Docker images.
        return False

    @staticmethod
    def _seleniumbase_read_turnstile_token(driver) -> str:
        script = """
            const selectors = [
                '[name="cf-turnstile-response"]',
                "textarea[name='cf-turnstile-response']",
                "input[name='cf-turnstile-response']"
            ];
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.value) return el.value;
            }
            return "";
        """
        try:
            value = driver.execute_script(script)
        except Exception:
            return ""
        return str(value or "").strip()

    @staticmethod
    def _seleniumbase_collect_page_diagnostics(driver) -> Dict[str, Any]:
        diagnostics: Dict[str, Any] = {}
        script = """
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
        """
        try:
            diagnostics.update(driver.execute_script(script) or {})
        except Exception:
            pass
        try:
            diagnostics["url"] = driver.current_url
        except Exception:
            pass
        if diagnostics.get("title"):
            diagnostics["title"] = TurnstileAPIServer._trim_text(diagnostics["title"], 160)
        if diagnostics.get("body_excerpt"):
            diagnostics["body_excerpt"] = TurnstileAPIServer._trim_text(diagnostics["body_excerpt"], 320)
        return diagnostics

    def _failure_payload_for_seleniumbase(
        self,
        *,
        elapsed_time: float,
        reason: str,
        browser_type: str,
        cookies: Optional[List[Dict[str, Any]]] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "value": "CAPTCHA_FAIL",
            "elapsed_time": elapsed_time,
            "reason": reason,
            "message": self._failure_reason_message(reason),
            "browser_type": browser_type,
            "browser_backend": browser_type,
            "result_mode": "failure",
        }
        if cookies is not None:
            payload["cookie_names"] = self._cookie_name_list(cookies)
            payload["d_cookie_present"] = self._has_cookie_name(cookies, "d")
            payload["locl_cookie_present"] = self._has_cookie_name(cookies, "locl")
            payload["cf_clearance_present"] = self._has_cookie_name(cookies, "cf_clearance")
        if diagnostics:
            payload["page"] = diagnostics
            if diagnostics.get("url"):
                payload["url_final"] = diagnostics["url"]
        if extra:
            payload.update(extra)
        return payload

    def _solve_turnstile_embedded_seleniumbase_sync(
        self,
        url: str,
        sitekey: str,
        action: Optional[str],
        cdata: Optional[str],
        proxy_cfg: Optional[Dict[str, str]],
        effective_browser_type: str,
        solve_timeout: Optional[float] = None,
        reverse_proxy_base: Optional[str] = None,
        reverse_proxy_style: str = "host",
        injected_cookie_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_time = time.time()
        driver = None
        url_with_slash = ""
        try:
            driver = self._create_seleniumbase_driver(proxy_cfg)
            url_with_slash = self._normalize_page_url(url)
            if not url_with_slash.endswith("/"):
                url_with_slash += "/"
            if injected_cookie_header:
                self._apply_injected_cookies_to_webdriver(driver, url_with_slash, injected_cookie_header)

            self._seleniumbase_open_url(driver, url_with_slash, proxy_cfg)
            turnstile_div = (
                '<div class="cf-turnstile" style="background: white;" data-sitekey="' + sitekey + '"'
                + (f' data-action="{action}"' if action else "")
                + (f' data-cdata="{cdata}"' if cdata else "")
                + "></div>"
            )
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)
            encoded_html = base64.b64encode(page_data.encode("utf-8")).decode("ascii")
            driver.execute_script(
                "document.open();document.write(atob(arguments[0]));document.close();",
                encoded_html,
            )

            if self.debug:
                logger.debug(
                    "Browser SB: Embedded solve | url=%r sitekey=%r browser=%r",
                    url_with_slash,
                    sitekey,
                    effective_browser_type,
                )

            token_value = ""
            for attempt in range(16):
                if solve_timeout is not None and (time.time() - start_time) > solve_timeout:
                    return self._failure_payload_for_seleniumbase(
                        elapsed_time=round(time.time() - start_time, 3),
                        reason="solve_timeout",
                        browser_type=effective_browser_type,
                        extra={
                            "timeout_seconds": solve_timeout,
                            "message": f"Solve exceeded time limit of {solve_timeout} second(s).",
                            "url_initial": url_with_slash,
                            "sitekey": sitekey,
                            "reverse_proxy": reverse_proxy_base or "",
                            "reverse_proxy_style": reverse_proxy_style,
                        },
                    )
                token_value = self._seleniumbase_read_turnstile_token(driver)
                if token_value:
                    break
                if attempt % 2 == 0:
                    self._seleniumbase_click_turnstile(driver)
                time.sleep(0.5)

            elapsed_time = round(time.time() - start_time, 3)
            if token_value:
                payload: Dict[str, Any] = {
                    "value": token_value,
                    "elapsed_time": elapsed_time,
                    "browser_type": effective_browser_type,
                    "browser_backend": effective_browser_type,
                    "result_mode": "token_only",
                }
                if reverse_proxy_base:
                    payload["note"] = (
                        "reverse_proxy is accepted for seleniumbase embedded mode, "
                        "but embedded HTML flow does not route page requests through worker."
                    )
                return payload

            diagnostics = self._seleniumbase_collect_page_diagnostics(driver)
            return self._failure_payload_for_seleniumbase(
                elapsed_time=elapsed_time,
                reason="embedded_no_token_after_attempts",
                browser_type=effective_browser_type,
                diagnostics=diagnostics,
                extra={
                    "attempts": 16,
                    "url_initial": url_with_slash,
                    "sitekey": sitekey,
                    "reverse_proxy": reverse_proxy_base or "",
                    "reverse_proxy_style": reverse_proxy_style,
                },
            )
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            diagnostics = self._seleniumbase_collect_page_diagnostics(driver) if driver else None
            return self._failure_payload_for_seleniumbase(
                elapsed_time=elapsed_time,
                reason="embedded_solver_exception",
                browser_type=effective_browser_type,
                diagnostics=diagnostics,
                extra={
                    "error": self._trim_text(e, 320),
                    "url_initial": url_with_slash or url,
                    "sitekey": sitekey,
                    "reverse_proxy": reverse_proxy_base or "",
                    "reverse_proxy_style": reverse_proxy_style,
                },
            )
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _solve_turnstile_seleniumbase_sync(
        self,
        url: str,
        proxy_cfg: Optional[Dict[str, str]],
        effective_browser_type: str,
        solve_timeout: Optional[float] = None,
        reverse_proxy_base: Optional[str] = None,
        reverse_proxy_style: str = "host",
        injected_cookie_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_time = time.time()
        driver = None
        page_url = ""
        nav_url = ""
        rev_base = reverse_proxy_base.rstrip("/") if reverse_proxy_base else None
        rev_style = reverse_proxy_style if reverse_proxy_style in ("full", "host") else "host"
        proxied_doc_set_cookie_headers: List[str] = []
        proxied_doc_status: Optional[int] = None
        proxied_doc_error: str = ""
        injected_cookie_names = [cookie["name"] for cookie in self._parse_cookie_header_pairs(injected_cookie_header)]
        injected_cookie_add_count = 0
        try:
            driver = self._create_seleniumbase_driver(proxy_cfg)
            page_url = self._normalize_page_url(url)
            if injected_cookie_names:
                injected_cookie_add_count = self._apply_injected_cookies_to_webdriver(
                    driver,
                    page_url,
                    injected_cookie_header,
                )
                if self.debug:
                    logger.info(
                        "Browser SB: injected %s/%s request cookies for %s",
                        injected_cookie_add_count,
                        len(injected_cookie_names),
                        page_url,
                    )
            nav_url = page_url
            if rev_base:
                nav_url = self._build_reverse_proxied_url(page_url, rev_base, rev_style)
                proxied_doc = self._fetch_reverse_proxy_document_sync(nav_url, read_body=False)
                proxied_doc_set_cookie_headers = list(proxied_doc.get("set_cookie_headers") or [])
                proxied_doc_status = proxied_doc.get("status")
                proxied_doc_error = proxied_doc.get("error") or ""
                if self.debug:
                    logger.info(
                        "Browser SB: reverse_proxy bootstrap | style=%s target=%s proxied=%s status=%s set_cookie_count=%s error=%s",
                        rev_style,
                        page_url,
                        nav_url,
                        proxied_doc_status,
                        len(proxied_doc_set_cookie_headers),
                        proxied_doc_error,
                    )

            self._seleniumbase_open_url(driver, nav_url, proxy_cfg)
            time.sleep(1.5)
            self._seleniumbase_click_turnstile(driver)

            init_host = (urlparse(page_url).hostname or "").lower()
            token_value = ""
            session_via_dl = False
            proxy_cookie_added = 0
            cf_clearance_seen_at: Optional[float] = None
            cf_clearance_grace = self._cf_clearance_grace_seconds()
            require_d_locl_only = self._require_d_and_locl_only()
            token_seen_at: Optional[float] = None
            token_post_wait = self._turnstile_token_post_wait_seconds()
            token_form_submit_attempted = False
            token_form_submit_result: Dict[str, Any] = {}
            max_poll_attempts = 200
            if solve_timeout is not None:
                max_poll_attempts = max(200, int((solve_timeout / 0.35) + 40))

            for attempt in range(max_poll_attempts):
                if solve_timeout is not None and (time.time() - start_time) > solve_timeout:
                    return self._failure_payload_for_seleniumbase(
                        elapsed_time=round(time.time() - start_time, 3),
                        reason="solve_timeout",
                        browser_type=effective_browser_type,
                        extra={
                            "timeout_seconds": solve_timeout,
                            "message": f"Solve exceeded time limit of {solve_timeout} second(s).",
                            "url_initial": page_url,
                            "url_final": nav_url or page_url,
                            "reverse_proxy": rev_base or "",
                            "reverse_proxy_style": rev_style,
                            "reverse_proxy_url": nav_url if rev_base else "",
                            "reverse_proxy_document_status": proxied_doc_status,
                            "reverse_proxy_set_cookie_count": len(proxied_doc_set_cookie_headers),
                            "reverse_proxy_bootstrap_error": proxied_doc_error,
                            "set_cookie_headers": proxied_doc_set_cookie_headers[:20],
                            "ip_preflight": self._seleniumbase_ip_preflight_placeholder(rev_base, rev_style),
                            "cf_clearance_grace_seconds": cf_clearance_grace,
                            "require_d_locl_only": require_d_locl_only,
                            "token_post_wait_seconds": token_post_wait,
                            "token_form_submit_result": token_form_submit_result,
                            "injected_cookie_names": injected_cookie_names,
                            "injected_cookie_add_count": injected_cookie_add_count,
                        },
                    )
                try:
                    current_host = (urlparse(driver.current_url).hostname or "").lower()
                except Exception:
                    current_host = init_host
                hosts = {h for h in (init_host, current_host) if h}
                cookies = self._seleniumbase_get_cookies_safe(
                    driver,
                    hosts,
                    context=f"poll_loop_attempt_{attempt}",
                )

                has_d_locl = self._has_new_d_after_injected(cookies, injected_cookie_header)
                has_cf_clearance = self._has_cf_clearance(cookies)
                if has_d_locl:
                    session_via_dl = True
                    break
                if has_cf_clearance and not require_d_locl_only:
                    now = time.time()
                    if cf_clearance_seen_at is None:
                        cf_clearance_seen_at = now
                        if self.debug:
                            logger.debug(
                                "Browser SB: cf_clearance detected (attempt %s), waiting %.1fs for d/locl",
                                attempt,
                                cf_clearance_grace,
                            )
                    elif (now - cf_clearance_seen_at) >= cf_clearance_grace:
                        session_via_dl = True
                        break

                token_value = self._seleniumbase_read_turnstile_token(driver)
                if token_value:
                    now = time.time()
                    if token_seen_at is None:
                        token_seen_at = now
                    if not token_form_submit_attempted:
                        token_form_submit_attempted = True
                        token_form_submit_result = self._seleniumbase_submit_turnstile_form(driver, token_value)
                        if self.debug:
                            logger.debug(
                                "Browser SB: token detected (attempt %s); form submit probe=%s",
                                attempt,
                                token_form_submit_result,
                            )
                        time.sleep(0.8)
                        continue
                    if self._has_new_d_after_injected(cookies, injected_cookie_header):
                        session_via_dl = True
                        break
                    if (now - token_seen_at) >= token_post_wait:
                        break

                if attempt % 4 == 0:
                    self._seleniumbase_click_turnstile(driver)
                time.sleep(0.35)

            try:
                final_host = (urlparse(driver.current_url).hostname or "").lower()
            except Exception:
                final_host = init_host
            hosts = {h for h in (init_host, final_host) if h}
            cookies = self._seleniumbase_get_cookies_safe(
                driver,
                hosts,
                context="post_loop_cookie_read",
            )

            if rev_base and not self._has_usable_session_cookies(cookies) and proxied_doc_set_cookie_headers:
                proxy_cookie_added = self._apply_reverse_proxy_cookies_to_target(
                    driver,
                    page_url,
                    proxied_doc_set_cookie_headers,
                )
                if proxy_cookie_added:
                    target_host = (urlparse(page_url).hostname or "").lower()
                    target_hosts = {target_host} if target_host else set()
                    cookies = self._seleniumbase_get_cookies_safe(
                        driver,
                        target_hosts,
                        context="post_reverse_proxy_cookie_apply",
                    )
                    if self._has_usable_session_cookies(cookies):
                        session_via_dl = True

            cookie_header = self._format_cookie_header_excluding_injected_d(cookies, injected_cookie_header)
            elapsed_time = round(time.time() - start_time, 3)

            if not token_value and not session_via_dl:
                diagnostics = self._seleniumbase_collect_page_diagnostics(driver)
                reason = self._classify_solve_failure_reason(diagnostics, cookies)
                return self._failure_payload_for_seleniumbase(
                    elapsed_time=elapsed_time,
                    reason=reason,
                    browser_type=effective_browser_type,
                    cookies=cookies,
                    diagnostics=diagnostics,
                    extra={
                        "url_initial": page_url,
                        "url_final": driver.current_url if driver else nav_url,
                        "reverse_proxy": rev_base or "",
                        "reverse_proxy_style": rev_style,
                        "reverse_proxy_url": nav_url if rev_base else "",
                        "reverse_proxy_document_status": proxied_doc_status,
                        "reverse_proxy_set_cookie_count": len(proxied_doc_set_cookie_headers),
                        "reverse_proxy_cookie_add_count": proxy_cookie_added,
                        "reverse_proxy_bootstrap_error": proxied_doc_error,
                        "set_cookie_headers": proxied_doc_set_cookie_headers[:20],
                        "ip_preflight": self._seleniumbase_ip_preflight_placeholder(rev_base, rev_style),
                        "cf_clearance_grace_seconds": cf_clearance_grace,
                        "require_d_locl_only": require_d_locl_only,
                        "token_post_wait_seconds": token_post_wait,
                        "token_form_submit_result": token_form_submit_result,
                        "injected_cookie_names": injected_cookie_names,
                        "injected_cookie_add_count": injected_cookie_add_count,
                    },
                )

            request_headers: Dict[str, str] = {}
            try:
                user_agent = driver.execute_script("return navigator.userAgent || ''")
                if user_agent:
                    request_headers["user-agent"] = str(user_agent)
            except Exception:
                pass
            if cookie_header:
                request_headers["cookie"] = cookie_header

            payload: Dict[str, Any] = {
                "value": token_value or "",
                "elapsed_time": elapsed_time,
                "url_initial": page_url,
                "url_final": driver.current_url,
                "browser_type": effective_browser_type,
                "browser_backend": effective_browser_type,
                "result_mode": "token_only" if token_value else "session_captured",
                "cookies": cookies,
                "cookie_header": cookie_header,
                "d_locl_cookie_header": self._d_locl_cookie_header(cookies),
                "request_headers": request_headers,
                "response_headers": {},
                "set_cookie_headers": proxied_doc_set_cookie_headers[:20],
                "turnstile_headers": {
                    "requests": [],
                    "responses": [],
                    "request_failed": [],
                },
                "reverse_proxy": rev_base or "",
                "reverse_proxy_style": rev_style,
                "reverse_proxy_url": nav_url if rev_base else "",
                "reverse_proxy_document_status": proxied_doc_status,
                "reverse_proxy_set_cookie_count": len(proxied_doc_set_cookie_headers),
                "reverse_proxy_cookie_add_count": proxy_cookie_added,
                "reverse_proxy_bootstrap_error": proxied_doc_error,
                "ip_preflight": self._seleniumbase_ip_preflight_placeholder(rev_base, rev_style),
                "cf_clearance_grace_seconds": cf_clearance_grace,
                "require_d_locl_only": require_d_locl_only,
                "token_post_wait_seconds": token_post_wait,
                "token_form_submit_result": token_form_submit_result,
                "injected_cookie_names": injected_cookie_names,
                "injected_cookie_add_count": injected_cookie_add_count,
            }
            if session_via_dl and not token_value:
                payload["turnstile_token"] = None
                if rev_base:
                    payload["note"] = (
                        "Usable session cookies were captured in seleniumbase reverse_proxy mode "
                        "(worker Set-Cookie applied onto target domain when needed)."
                    )
                else:
                    if self._has_new_d_after_injected(cookies, injected_cookie_header):
                        payload["note"] = (
                            "Session cookies `d` and `locl` were detected in the jar; headers captured immediately."
                        )
                    else:
                        payload["note"] = (
                            "cf_clearance was captured and no d/locl arrived before the current stop condition."
                        )
            return payload
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            diagnostics = self._seleniumbase_collect_page_diagnostics(driver) if driver else None
            return self._failure_payload_for_seleniumbase(
                elapsed_time=elapsed_time,
                reason="solver_exception",
                browser_type=effective_browser_type,
                diagnostics=diagnostics,
                extra={
                    "error": self._trim_text(e, 320),
                    "url_initial": page_url or url,
                    "url_final": nav_url or page_url or url,
                    "reverse_proxy": rev_base or "",
                    "reverse_proxy_style": rev_style,
                    "reverse_proxy_url": nav_url if rev_base else "",
                    "reverse_proxy_document_status": proxied_doc_status,
                    "reverse_proxy_set_cookie_count": len(proxied_doc_set_cookie_headers),
                    "reverse_proxy_bootstrap_error": proxied_doc_error,
                    "set_cookie_headers": proxied_doc_set_cookie_headers[:20],
                    "ip_preflight": self._seleniumbase_ip_preflight_placeholder(rev_base, rev_style),
                    "cf_clearance_grace_seconds": cf_clearance_grace,
                    "require_d_locl_only": require_d_locl_only,
                    "token_post_wait_seconds": token_post_wait,
                    "token_form_submit_result": token_form_submit_result,
                    "injected_cookie_names": injected_cookie_names,
                    "injected_cookie_add_count": injected_cookie_add_count,
                },
            )
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

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
        injected_cookie_header: Optional[str] = None,
    ) -> None:
        """Serve local HTML with an embedded Turnstile widget (legacy flow when ``sitekey`` is provided)."""
        effective_browser_type = self._normalize_browser_type(browser_type_override)
        if effective_browser_type == "seleniumbase":
            try:
                proxy_cfg, _ = self._pick_proxy_for_solve(proxy_cfg_override)
                current_browser_type = self.browser_type
                self.browser_type = effective_browser_type
                self._assert_proxy_supported_by_browser(proxy_cfg)
                self.browser_type = current_browser_type
            except ValueError as e:
                payload = self._failure_payload_for_seleniumbase(
                    elapsed_time=0.0,
                    reason="solver_exception",
                    browser_type=effective_browser_type,
                    extra={
                        "error": str(e),
                        "url_initial": url,
                        "sitekey": sitekey,
                    },
                )
                self.results[task_id] = payload
                self._save_results()
                return

            try:
                payload = await asyncio.to_thread(
                    self._solve_turnstile_embedded_seleniumbase_sync,
                    url,
                    sitekey,
                    action,
                    cdata,
                    proxy_cfg,
                    effective_browser_type,
                    solve_timeout,
                    reverse_proxy_base,
                    reverse_proxy_style,
                    injected_cookie_header,
                )
                if not isinstance(payload, dict):
                    raise RuntimeError("SeleniumBase embedded solve returned an invalid payload.")

                self.results[task_id] = payload
                self._save_results()
                if payload.get("value") == "CAPTCHA_FAIL":
                    self._log_failure_payload(0, task_id, payload)
                return
            except BaseException as e:
                elapsed_time = 0.0
                payload = self._failure_payload_for_seleniumbase(
                    elapsed_time=elapsed_time,
                    reason="embedded_solver_exception",
                    browser_type=effective_browser_type,
                    extra={
                        "error": self._trim_text(e, 320),
                        "url_initial": url,
                        "sitekey": sitekey,
                        "reverse_proxy": reverse_proxy_base or "",
                        "reverse_proxy_style": reverse_proxy_style,
                    },
                )
                self.results[task_id] = payload
                self._save_results()
                self._log_failure_payload(0, task_id, payload)
                return

        start_time = time.time()
        try:
            index, browser, effective_browser_type, release_browser = await self._acquire_browser(browser_type_override)
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            payload = await self._build_failure_payload(
                elapsed_time=elapsed_time,
                reason="embedded_solver_exception",
                extra={
                    "error": self._trim_text(e, 320),
                    "url_initial": url,
                    "sitekey": sitekey,
                    "browser_type": effective_browser_type,
                    "browser_backend": effective_browser_type,
                },
            )
            self.results[task_id] = payload
            self._save_results()
            self._log_failure_payload(0, task_id, payload)
            return

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
            base = self._normalize_page_url(url)
            url_with_slash = base + "/" if not base.endswith("/") else base
            if injected_cookie_header:
                await self._apply_injected_cookies_to_context(context, url_with_slash, injected_cookie_header)
            page = await context.new_page()
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
                            "result_mode": "token_only",
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
        injected_cookie_header: Optional[str] = None,
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
                injected_cookie_header,
            )
            return

        effective_browser_type = self._normalize_browser_type(browser_type_override)
        if effective_browser_type == "seleniumbase":
            try:
                proxy_cfg, _ = self._pick_proxy_for_solve(proxy_cfg_override)
                current_browser_type = self.browser_type
                self.browser_type = effective_browser_type
                self._assert_proxy_supported_by_browser(proxy_cfg)
                self.browser_type = current_browser_type
            except ValueError as e:
                payload = self._failure_payload_for_seleniumbase(
                    elapsed_time=0.0,
                    reason="solver_exception",
                    browser_type=effective_browser_type,
                    extra={
                        "error": str(e),
                        "url_initial": url,
                    },
                )
                self.results[task_id] = payload
                self._save_results()
                self._log_failure_payload(0, task_id, payload)
                return

            try:
                fallback_browser = self._selenium_fallback_browser()
                selenium_timeout = solve_timeout
                if fallback_browser and solve_timeout is not None:
                    selenium_timeout = min(solve_timeout, max(30.0, solve_timeout * 0.4))
                payload = await asyncio.to_thread(
                    self._solve_turnstile_seleniumbase_sync,
                    url,
                    proxy_cfg,
                    effective_browser_type,
                    selenium_timeout,
                    reverse_proxy_base,
                    reverse_proxy_style,
                    injected_cookie_header,
                )
                if not isinstance(payload, dict):
                    raise RuntimeError("SeleniumBase solve returned an invalid payload.")

                if fallback_browser and self._should_retry_after_selenium(payload):
                    if self.debug:
                        logger.debug(
                            "SeleniumBase result incomplete (score=%s, reason=%s); retrying with fallback browser=%s",
                            self._result_quality_score(payload),
                            payload.get("reason"),
                            fallback_browser,
                        )
                    original_payload = payload
                    await self._solve_turnstile(
                        task_id,
                        url,
                        sitekey,
                        action,
                        cdata,
                        solve_timeout,
                        fallback_browser,
                        proxy_cfg_override,
                        reverse_proxy_base,
                        reverse_proxy_style,
                        injected_cookie_header,
                    )
                    fallback_payload = self.results.get(task_id)
                    if (
                        isinstance(fallback_payload, dict)
                        and self._result_quality_score(fallback_payload) >= self._result_quality_score(original_payload)
                    ):
                        if self.debug:
                            logger.debug(
                                "Fallback browser result kept (score=%s >= %s)",
                                self._result_quality_score(fallback_payload),
                                self._result_quality_score(original_payload),
                            )
                        return
                    self.results[task_id] = original_payload
                    self._save_results()
                    if original_payload.get("value") == "CAPTCHA_FAIL":
                        self._log_failure_payload(0, task_id, original_payload)
                    return

                self.results[task_id] = payload
                self._save_results()
                if payload.get("value") == "CAPTCHA_FAIL":
                    self._log_failure_payload(0, task_id, payload)
                return
            except BaseException as e:
                payload = self._failure_payload_for_seleniumbase(
                    elapsed_time=0.0,
                    reason="solver_exception",
                    browser_type=effective_browser_type,
                    extra={
                        "error": self._trim_text(e, 320),
                        "url_initial": url,
                        "reverse_proxy": reverse_proxy_base or "",
                        "reverse_proxy_style": reverse_proxy_style,
                    },
                )
                self.results[task_id] = payload
                self._save_results()
                self._log_failure_payload(0, task_id, payload)
                return

        start_time = time.time()
        try:
            index, browser, effective_browser_type, release_browser = await self._acquire_browser(browser_type_override)
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            payload = await self._build_failure_payload(
                elapsed_time=elapsed_time,
                reason="solver_exception",
                extra={
                    "error": self._trim_text(e, 320),
                    "url_initial": url,
                    "browser_type": effective_browser_type,
                    "browser_backend": effective_browser_type,
                    "reverse_proxy": reverse_proxy_base or "",
                    "reverse_proxy_style": reverse_proxy_style,
                },
            )
            self.results[task_id] = payload
            self._save_results()
            self._log_failure_payload(0, task_id, payload)
            return

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
            page_url = self._normalize_page_url(url)
            injected_cookie_names = [cookie["name"] for cookie in self._parse_cookie_header_pairs(injected_cookie_header)]
            injected_cookie_add_count = 0
            if injected_cookie_names:
                injected_cookie_add_count = await self._apply_injected_cookies_to_context(
                    context,
                    page_url,
                    injected_cookie_header,
                )
                if self.debug:
                    logger.info(
                        "Browser %s: injected %s/%s request cookies for %s",
                        index,
                        injected_cookie_add_count,
                        len(injected_cookie_names),
                        page_url,
                    )
            page = await context.new_page()
            if rev_base:

                async def _rp_route(route) -> None:
                    await self._reverse_proxy_route_handler(route, rev_base, rev_style, index)

                await page.route("**/*", _rp_route)
            ip_preflight = await self._run_ip_preflight(page, index, rev_base, rev_style)
            response_capture_tasks: Set[asyncio.Task] = set()
            last_document_request_headers: Dict[str, str] = {}
            last_document_response_headers: Dict[str, str] = {}
            last_document_request_body: List[Optional[str]] = [None]
            last_document_status: List[Optional[int]] = [None]
            last_document_status_text: List[str] = [""]
            last_document_url: List[str] = [""]
            last_document_failure_text: List[str] = [""]
            last_document_set_cookie_headers: List[str] = []
            last_document_response_cookies: List[Dict[str, Any]] = []
            last_document_response_d_value: List[Optional[str]] = [None]
            turnstile_capture: Dict[str, Any] = {
                "requests": [],
                "responses": [],
                "request_failed": [],
            }
            turnstile_seen = {
                "request_urls": set(),
                "response_urls": set(),
                "failed_urls": set(),
            }

            def _is_turnstile_request(req) -> bool:
                try:
                    req_url = str(getattr(req, "url", "") or "")
                    req_type = str(getattr(req, "resource_type", "") or "")
                    return (
                        "challenges.cloudflare.com" in req_url
                        or "turnstile" in req_url.lower()
                        or req_type in {"fetch", "xhr", "iframe"}
                        and "cloudflare" in req_url.lower()
                    )
                except Exception:
                    return False

            def _push_limited(target_list: List[Dict[str, Any]], item: Dict[str, Any], limit: int = 12) -> None:
                if len(target_list) < limit:
                    target_list.append(item)

            async def _capture_response_set_cookies(response, headers: Dict[str, str]) -> None:
                try:
                    set_cookie_headers: List[str] = []
                    try:
                        header_values = await response.header_values("set-cookie")
                        if header_values:
                            set_cookie_headers.extend([str(v or "").strip() for v in header_values if str(v or "").strip()])
                    except Exception:
                        pass
                    if not set_cookie_headers:
                        try:
                            for pair in (await response.headers_array()) or []:
                                if isinstance(pair, dict):
                                    name = pair.get("name")
                                    value = pair.get("value")
                                else:
                                    name = getattr(pair, "name", None)
                                    value = getattr(pair, "value", None)
                                if str(name or "").lower() != "set-cookie":
                                    continue
                                raw = str(value or "").strip()
                                if raw:
                                    set_cookie_headers.append(raw)
                        except Exception:
                            pass
                    if not set_cookie_headers:
                        return

                    resp_url = str(getattr(response, "url", "") or "")
                    target_hosts = {
                        (urlparse(resp_url).hostname or "").lower(),
                        (urlparse(page_url).hostname or "").lower(),
                    }
                    target_hosts = {h for h in target_hosts if h}
                    response_cookies = self._parse_set_cookie_headers_to_cookies(set_cookie_headers, target_hosts)
                    if not response_cookies:
                        return

                    if self.debug:
                        logger.debug(
                            "Browser %s: response cookies captured url=%s names=%s",
                            getattr(response.request, "resource_type", "unknown"),
                            resp_url,
                            [str(c.get("name") or "").strip() for c in response_cookies],
                        )

                    last_document_set_cookie_headers.clear()
                    last_document_set_cookie_headers.extend(set_cookie_headers)
                    last_document_response_cookies[:] = response_cookies
                    parsed_response_d = self._select_last_response_d_value(set_cookie_headers)
                    if parsed_response_d:
                        last_document_response_d_value[0] = parsed_response_d

                    response_cookie_map: Dict[str, Dict[str, Any]] = {}
                    for cookie in response_cookies:
                        cname = str(cookie.get("name") or "").strip().lower()
                        if cname:
                            response_cookie_map[cname] = cookie

                    current_cookies = await context.cookies()
                    merged_current: List[Dict[str, Any]] = []
                    current_names: set = set()
                    for cookie in current_cookies:
                        cname = str(cookie.get("name") or "").strip().lower()
                        if not cname:
                            continue
                        current_names.add(cname)
                        merged_current.append(response_cookie_map.get(cname, cookie))

                    for cookie in response_cookies:
                        cname = str(cookie.get("name") or "").strip().lower()
                        if cname and cname not in current_names:
                            merged_current.append(cookie)

                    if merged_current:
                        await context.add_cookies([
                            {k: v for k, v in cookie.items() if k in {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}}
                            for cookie in merged_current
                            if str(cookie.get("name") or "").strip()
                        ])
                        if self.debug:
                            names = [str(c.get("name") or "").strip() for c in merged_current]
                            logger.debug(
                                "Browser %s: merged response cookies -> names=%s d=%s locl=%s",
                                getattr(response.request, "resource_type", "unknown"),
                                names,
                                self._has_cookie_name(merged_current, "d"),
                                self._has_cookie_name(merged_current, "locl"),
                            )
                except Exception:
                    pass

            def _capture_turnstile_request(req) -> None:
                if not _is_turnstile_request(req):
                    return
                try:
                    req_url = str(req.url)
                except Exception:
                    req_url = ""
                if req_url in turnstile_seen["request_urls"]:
                    return
                turnstile_seen["request_urls"].add(req_url)
                try:
                    headers = dict(getattr(req, "headers", {}) or {})
                except Exception:
                    headers = {}
                post_data_excerpt = ""
                has_turnstile_response_field = False
                try:
                    post_data = str(getattr(req, "post_data", "") or "")
                    if post_data:
                        has_turnstile_response_field = "cf-turnstile-response=" in post_data
                        post_data_excerpt = self._trim_text(post_data, 320)
                except Exception:
                    pass
                entry = {
                    "url": req_url,
                    "method": getattr(req, "method", None),
                    "resource_type": getattr(req, "resource_type", None),
                    "headers": headers,
                    "has_turnstile_response_field": has_turnstile_response_field,
                    "post_data_excerpt": post_data_excerpt,
                }
                _push_limited(turnstile_capture["requests"], entry)

            async def _on_response_async(response):
                try:
                    try:
                        h = dict(await response.all_headers())
                    except Exception:
                        h = dict(response.headers or {})
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
                    await _capture_response_set_cookies(response, h)
                except Exception:
                    pass

            def _on_response(response):
                try:
                    task = asyncio.create_task(_on_response_async(response))
                    response_capture_tasks.add(task)
                    task.add_done_callback(lambda t: response_capture_tasks.discard(t))
                except Exception:
                    pass

            async def _flush_response_capture_tasks() -> None:
                if not response_capture_tasks:
                    return
                pending = [task for task in list(response_capture_tasks) if not task.done()]
                if not pending:
                    return
                await asyncio.gather(*pending, return_exceptions=True)

            def _on_request_failed(req):
                try:
                    if getattr(req, "resource_type", None) == "document":
                        last_document_failure_text[0] = self._request_failure_text(req)
                        try:
                            last_document_url[0] = req.url
                        except Exception:
                            pass
                    if _is_turnstile_request(req):
                        try:
                            failed_url = str(req.url)
                        except Exception:
                            failed_url = ""
                        if failed_url not in turnstile_seen["failed_urls"]:
                            turnstile_seen["failed_urls"].add(failed_url)
                            _push_limited(turnstile_capture["request_failed"], {
                                "url": failed_url,
                                "resource_type": getattr(req, "resource_type", None),
                                "headers": dict(getattr(req, "headers", {}) or {}),
                                "failure_text": self._request_failure_text(req),
                            })
                except Exception:
                    pass

            def _on_request(req):
                try:
                    _capture_turnstile_request(req)
                except Exception:
                    pass

            page.on("request", _on_request)
            page.on("response", _on_response)
            page.on("requestfailed", _on_request_failed)

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
                await asyncio.sleep(1.0)

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
                        return self._merge_cookie_stores(list(raw), last_document_response_cookies)
                    filtered = [c for c in raw if _cookie_matches(hs, c.get("domain", ""))]
                    return self._merge_cookie_stores(filtered, last_document_response_cookies)

                turnstile_check = ""
                session_via_dl = False
                cf_clearance_seen_at: Optional[float] = None
                cf_clearance_grace = self._cf_clearance_grace_seconds()
                require_d_locl_only = self._require_d_and_locl_only()
                token_seen_at: Optional[float] = None
                token_post_wait = self._turnstile_token_post_wait_seconds()
                token_form_submit_attempted = False
                token_form_submit_result: Dict[str, Any] = {}
                max_poll_attempts = 200
                if solve_timeout is not None:
                    max_poll_attempts = max(200, int((solve_timeout / 0.35) + 40))
                for attempt in range(max_poll_attempts):
                    jar = await _filtered_jar()
                    jar = self._overlay_injected_non_d_cookies(jar, injected_cookie_header)

                    has_d_locl = self._has_new_d_after_injected(jar, injected_cookie_header)
                    has_cf_clearance = self._has_cf_clearance(jar)
                    if has_d_locl:
                        session_via_dl = True
                        if self.debug:
                            logger.debug(
                                f"Browser {index}: session cookies detected (attempt {attempt}), capturing now"
                            )
                        break
                    if has_cf_clearance and not require_d_locl_only:
                        now = time.time()
                        if cf_clearance_seen_at is None:
                            cf_clearance_seen_at = now
                            if self.debug:
                                logger.debug(
                                    "Browser %s: cf_clearance detected (attempt %s), waiting %.1fs for d/locl",
                                    index,
                                    attempt,
                                    cf_clearance_grace,
                                )
                        elif (now - cf_clearance_seen_at) >= cf_clearance_grace:
                            session_via_dl = True
                            if self.debug:
                                logger.debug(
                                    "Browser %s: cf_clearance grace elapsed (%.1fs), accepting clearance-only session",
                                    index,
                                    cf_clearance_grace,
                                )
                            break

                    turnstile_check = await self._read_turnstile_token(page)
                    if turnstile_check:
                        now = time.time()
                        if token_seen_at is None:
                            token_seen_at = now
                        if not token_form_submit_attempted:
                            token_form_submit_attempted = True
                            token_form_submit_result = await self._playwright_submit_turnstile_form(page, turnstile_check)
                            if self.debug:
                                logger.debug(
                                    "Browser %s: token detected (attempt %s); form submit probe=%s",
                                    index,
                                    attempt,
                                    token_form_submit_result,
                                )
                            await asyncio.sleep(0.8)
                            continue
                        if self._has_new_d_after_injected(jar, injected_cookie_header):
                            session_via_dl = True
                            break
                        if require_d_locl_only:
                            if self.debug and attempt % 25 == 0:
                                logger.debug(
                                    "Browser %s: token captured but d/locl not ready yet (attempt %s), continuing strict wait",
                                    index,
                                    attempt,
                                )
                            await asyncio.sleep(0.35)
                            continue
                        if (now - token_seen_at) >= token_post_wait:
                            break

                    if self.debug and attempt % 25 == 0:
                        logger.debug(f"Browser {index}: Waiting for d/locl or Turnstile token (attempt {attempt})")

                    if attempt % 4 == 0:
                        await self._try_click_turnstile(page)
                    await asyncio.sleep(0.35)

                await _flush_response_capture_tasks()

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
                        "result_mode": "failure",
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
                        if last_document_set_cookie_headers:
                            cookies = self._merge_cookies_with_set_cookie_headers(
                                cookies,
                                last_document_set_cookie_headers,
                                hosts,
                            )
                        if self._has_usable_session_cookies(cookies):
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=90000)
                            except Exception:
                                pass
                            await asyncio.sleep(0.35)
                            await _flush_response_capture_tasks()
                            await asyncio.sleep(3.0)
                            cookies = await context.cookies()
                            if hosts:
                                cookies = [c for c in cookies if _cookie_matches_inner(hosts, c.get("domain", ""))]
                            if last_document_set_cookie_headers:
                                cookies = self._merge_cookies_with_set_cookie_headers(
                                    cookies,
                                    last_document_set_cookie_headers,
                                    hosts,
                                )
                            cookies = self._merge_cookie_stores(cookies, last_document_response_cookies)
                            cookies = self._normalize_cookie_store(cookies, injected_cookie_header)
                        cookies = self._normalize_cookie_store(cookies, injected_cookie_header)
                        response_d = self._select_last_response_cookie_value(last_document_set_cookie_headers, "d")
                        response_locl = self._select_last_response_cookie_value(last_document_set_cookie_headers, "locl")
                        response_cf_clearance = self._select_last_response_cookie_value(last_document_set_cookie_headers, "cf_clearance")
                        cookie_map = self._cookie_value_map(cookies, injected_cookie_header)
                        current_d = str(cookie_map.get("d") or "").strip()
                        current_locl = str(cookie_map.get("locl") or "").strip()
                        current_cf_clearance = str(cookie_map.get("cf_clearance") or "").strip()
                        if response_d and (not current_d or self._is_deleted_cookie_value(current_d)):
                            cookies = self._replace_cookie_value_by_name(cookies, "d", response_d)
                        if response_locl and (not current_locl or self._is_deleted_cookie_value(current_locl)):
                            cookies = self._replace_cookie_value_by_name(cookies, "locl", response_locl)
                        if response_cf_clearance and (not current_cf_clearance or self._is_deleted_cookie_value(current_cf_clearance)):
                            cookies = self._replace_cookie_value_by_name(cookies, "cf_clearance", response_cf_clearance)
                        ch = self._format_cookie_header_excluding_injected_d(cookies, injected_cookie_header)
                        req_snap = dict(last_document_request_headers)
                        if ch and "cookie" not in {k.lower() for k in req_snap}:
                            req_snap["cookie"] = ch
                        sess["cookies"] = cookies
                        sess["cookie_header"] = ch
                        sess["d_locl_cookie_header"] = self._d_locl_cookie_header(cookies)
                        sess["request_headers"] = req_snap
                        sess["response_headers"] = dict(last_document_response_headers)
                        sess["ip_preflight"] = ip_preflight
                        sess["turnstile_headers"] = turnstile_capture
                        sess["cf_clearance_grace_seconds"] = cf_clearance_grace
                        sess["require_d_locl_only"] = require_d_locl_only
                        sess["token_post_wait_seconds"] = token_post_wait
                        sess["token_form_submit_result"] = token_form_submit_result
                        self._attach_http_capture(sess, dict(last_document_request_headers), last_document_request_body)
                    except Exception:
                        pass

                    if sess.get("cookie_header"):
                        sess["value"] = ""
                        sess["result_mode"] = "session_captured"
                        sess["turnstile_token"] = None
                        sess["note"] = (
                            "No cf-turnstile-response field was found; usable session cookies and headers were captured."
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
                                "set_cookie_headers": [],
                                "ip_preflight": ip_preflight,
                                "turnstile_headers": turnstile_capture,
                                "cf_clearance_grace_seconds": cf_clearance_grace,
                                "require_d_locl_only": require_d_locl_only,
                                "token_post_wait_seconds": token_post_wait,
                                "token_form_submit_result": token_form_submit_result,
                                "injected_cookie_names": injected_cookie_names,
                                "injected_cookie_add_count": injected_cookie_add_count,
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
                    await _flush_response_capture_tasks()

                    cookies = await _filtered_jar()
                    cookies = self._overlay_injected_non_d_cookies(cookies, injected_cookie_header)
                    final_host = (urlparse(page.url).hostname or "").lower()
                    init_host_cookie = (urlparse(page_url).hostname or "").lower()
                    hosts = {h for h in (final_host, init_host_cookie) if h}
                    if require_d_locl_only and not self._has_new_d_after_injected(cookies, injected_cookie_header):
                        elapsed_time = round(time.time() - start_time, 3)
                        diagnostics = await self._collect_page_diagnostics(page)
                        reason = self._classify_solve_failure_reason(diagnostics, cookies or [])
                        payload = await self._build_failure_payload(
                            elapsed_time=elapsed_time,
                            reason=reason,
                            page=page,
                            cookies=cookies,
                            extra={
                                "url_initial": page_url,
                                "url_final": page.url,
                                "document_response_status": last_document_status[0],
                                "document_response_status_text": last_document_status_text[0],
                                "document_response_url": last_document_url[0],
                                "document_failure_text": last_document_failure_text[0],
                                "set_cookie_headers": [],
                                "ip_preflight": ip_preflight,
                                "turnstile_headers": turnstile_capture,
                                "cf_clearance_grace_seconds": cf_clearance_grace,
                                "require_d_locl_only": require_d_locl_only,
                                "token_post_wait_seconds": token_post_wait,
                                "token_form_submit_result": token_form_submit_result,
                                "injected_cookie_names": injected_cookie_names,
                                "injected_cookie_add_count": injected_cookie_add_count,
                                "message": "Strict d+locl mode enabled: token/cf_clearance observed but d/locl are not ready yet.",
                            },
                        )
                        self.results[task_id] = payload
                        self._save_results()
                        self._log_failure_payload(index, task_id, payload)
                        return
                    response_d = self._select_last_response_cookie_value(last_document_set_cookie_headers, "d")
                    response_locl = self._select_last_response_cookie_value(last_document_set_cookie_headers, "locl")
                    response_cf_clearance = self._select_last_response_cookie_value(last_document_set_cookie_headers, "cf_clearance")
                    cookie_map = self._cookie_value_map(cookies, injected_cookie_header)
                    current_d = str(cookie_map.get("d") or "").strip()
                    current_locl = str(cookie_map.get("locl") or "").strip()
                    current_cf_clearance = str(cookie_map.get("cf_clearance") or "").strip()
                    if response_d and (not current_d or self._is_deleted_cookie_value(current_d)):
                        cookies = self._replace_cookie_value_by_name(cookies, "d", response_d)
                    if response_locl and (not current_locl or self._is_deleted_cookie_value(current_locl)):
                        cookies = self._replace_cookie_value_by_name(cookies, "locl", response_locl)
                    if response_cf_clearance and (not current_cf_clearance or self._is_deleted_cookie_value(current_cf_clearance)):
                        cookies = self._replace_cookie_value_by_name(cookies, "cf_clearance", response_cf_clearance)
                    cookie_header = self._format_cookie_header_excluding_injected_d(cookies, injected_cookie_header)
                    elapsed_time = round(time.time() - start_time, 3)

                    if turnstile_check:
                        logger.success(
                            f"Browser {index}: Solved — token {COLORS.get('MAGENTA')}{turnstile_check[:12]}…{COLORS.get('RESET')} in "
                            f"{COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s | final URL {page.url}"
                        )
                    else:
                        if self._has_new_d_after_injected(cookies, injected_cookie_header):
                            logger.success(
                                f"Browser {index}: d + locl captured in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s — {page.url}"
                            )
                        else:
                            logger.success(
                                f"Browser {index}: session cookies captured in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')}s — {page.url}"
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
                        "result_mode": "token_only" if turnstile_check else "session_captured",
                        "cookies": cookies,
                        "cookie_header": cookie_header,
                        "d_locl_cookie_header": self._d_locl_cookie_header(cookies),
                        "request_headers": req_hdrs,
                        "response_headers": dict(last_document_response_headers),
                        "set_cookie_headers": [],
                        "ip_preflight": ip_preflight,
                        "turnstile_headers": turnstile_capture,
                        "cf_clearance_grace_seconds": cf_clearance_grace,
                        "require_d_locl_only": require_d_locl_only,
                        "token_post_wait_seconds": token_post_wait,
                        "token_form_submit_result": token_form_submit_result,
                        "injected_cookie_names": injected_cookie_names,
                        "injected_cookie_add_count": injected_cookie_add_count,
                    }
                    if session_via_dl and not turnstile_check:
                        payload["turnstile_token"] = None
                        if self._has_new_d_after_injected(cookies, injected_cookie_header):
                            payload["note"] = (
                                "Session cookies `d` and `locl` were detected in the jar; headers captured immediately."
                            )
                        else:
                            payload["note"] = (
                                "cf_clearance was captured and no d/locl arrived before the current stop condition."
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
                        "turnstile_headers": turnstile_capture if 'turnstile_capture' in locals() else None,
                        "cf_clearance_grace_seconds": cf_clearance_grace if 'cf_clearance_grace' in locals() else None,
                        "require_d_locl_only": require_d_locl_only if 'require_d_locl_only' in locals() else None,
                        "token_post_wait_seconds": token_post_wait if 'token_post_wait' in locals() else None,
                        "token_form_submit_result": token_form_submit_result if 'token_form_submit_result' in locals() else None,
                        "injected_cookie_names": injected_cookie_names if 'injected_cookie_names' in locals() else [],
                        "injected_cookie_add_count": injected_cookie_add_count if 'injected_cookie_add_count' in locals() else 0,
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
                    "cf_clearance_grace_seconds": self._cf_clearance_grace_seconds(),
                    "require_d_locl_only": self._require_d_and_locl_only(),
                    "token_post_wait_seconds": self._turnstile_token_post_wait_seconds(),
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
        injected_cookie_raw = (
            request.args.get("cookies")
            or request.args.get("cookie")
            or request.args.get("cookie_header")
        )
        injected_cookie_header = str(injected_cookie_raw or "").strip() or None
        injected_cookie_names = [cookie["name"] for cookie in self._parse_cookie_header_pairs(injected_cookie_header)]
        if injected_cookie_header and not injected_cookie_names:
            return jsonify({
                "status": "error",
                "error": "Invalid 'cookies': expected a Cookie header value like 'name=value; other=value'",
            }), 400
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
            "proxy_override=%s proxy_auth=%s reverse_proxy=%s reverse_proxy_style=%s injected_cookies=%s thread=%s",
            task_id,
            self._trim_text(url, 180),
            "provided" if sitekey else "none",
            requested_browser_type,
            self.headless,
            solve_timeout,
            proxy_cfg_override.get("server") if proxy_cfg_override else None,
            bool(self._proxy_has_auth(proxy_cfg_override)),
            reverse_proxy_base or None,
            reverse_proxy_style_effective,
            injected_cookie_names,
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
                injected_cookie_header,
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

    async def describe_result(self):
        """Return a read-only classification/summary for a solve result."""
        task_id = request.args.get('id')

        if not task_id or task_id not in self.results:
            return jsonify({"status": "error", "error": "Invalid task ID/Request parameter"}), 400

        result = self.results[task_id]
        summary = self._describe_result_payload(task_id, result)
        status_code = 200
        if summary.get("result_mode") == "failure":
            status_code = 422
        return jsonify(summary), status_code


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
        help='Enable debug mode for both solver logs and Quart server diagnostics',
    )
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the default browser type for the solver. Supported options: chromium, playwright, chrome, msedge, camoufox, seleniumbase (default: chromium)')
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
    server.app.config["DEBUG"] = bool(debug)
    server.app.debug = bool(debug)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    if args.browser_type == "playwrite":
        args.browser_type = "playwright"
    if args.browser_type in ("selenium", "sb"):
        args.browser_type = "seleniumbase"
    browser_types = TurnstileAPIServer._supported_browser_types()
    
    if args.browser_type not in browser_types:
        if args.browser_type == 'playwright' and not PLAYWRIGHT_NATIVE_AVAILABLE:
            logger.error(f"Playwright is not available. Please install playwright or use a different browser type. Available browser types: {browser_types}")
        elif args.browser_type == 'camoufox' and not CAMOUFOX_AVAILABLE:
            logger.error(f"Camoufox is not available. Please install camoufox or use a different browser type. Available browser types: {browser_types}")
        elif args.browser_type in ('seleniumbase', 'selenium', 'sb') and not SELENIUMBASE_AVAILABLE:
            logger.error(f"SeleniumBase is not available. Please install seleniumbase or use a different browser type. Available browser types: {browser_types}")
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
        app.run(host=args.host, port=int(args.port), debug=args.debug, use_reloader=False)
