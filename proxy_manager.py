import asyncio
import logging
import random
import threading
import time
from pathlib import Path

import aiohttp
import requests

import config

logger = logging.getLogger(__name__)

try:
    from aiohttp_socks import ProxyConnector
    _HAS_AIOHTTP_SOCKS = True
except ImportError:
    _HAS_AIOHTTP_SOCKS = False


def _fetch_list(url: str, scheme: str, limit: int) -> list[str]:
    last_err = None
    for attempt in range(1, config.FETCH_RETRIES + 1):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            lines = [l.strip() for l in r.text.splitlines() if l.strip()]
            proxies = [f"{scheme}://{l}" for l in lines[:limit]]
            logger.info("Fetched %d %s proxies (attempt %d)", len(proxies), scheme, attempt)
            return proxies
        except Exception as e:
            last_err = e
            logger.warning("Proxy list fetch failed (attempt %d/%d): %s", attempt, config.FETCH_RETRIES, e)
            time.sleep(1)
    logger.error("Giving up fetching %s proxies: %s", scheme, last_err)
    return []


def fetch_all_proxies() -> list[str]:
    return (
        _fetch_list(config.SOCKS5_LIST_URL, "socks5", config.PROXY_LIMIT_PER_TYPE)
        + _fetch_list(config.HTTP_LIST_URL, "http", config.PROXY_LIMIT_PER_TYPE)
    )


async def _test_one(proxy: str, timeout: int) -> tuple[str, bool, float]:
    start = time.monotonic()
    try:
        if proxy.startswith("socks") and _HAS_AIOHTTP_SOCKS:
            connector = ProxyConnector.from_url(proxy)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    config.TEST_URL, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    ok = resp.status == 200
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    config.TEST_URL,
                    proxy=proxy if proxy.startswith("http") else None,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    ok = resp.status == 200
        return proxy, ok, time.monotonic() - start
    except Exception:
        return proxy, False, time.monotonic() - start


async def _test_all(proxies: list[str]) -> list[tuple[str, float]]:
    working: list[tuple[str, float]] = []
    for i in range(0, len(proxies), config.TEST_BATCH_SIZE):
        batch = proxies[i : i + config.TEST_BATCH_SIZE]
        results = await asyncio.gather(*[_test_one(p, config.TEST_TIMEOUT) for p in batch])
        working.extend((p, t) for p, ok, t in results if ok)
        if i + config.TEST_BATCH_SIZE < len(proxies):
            await asyncio.sleep(0.5)
    working.sort(key=lambda x: x[1])
    return working


def test_proxies(proxies: list[str]) -> list[str]:
    logger.info("Testing %d proxies...", len(proxies))
    try:
        working = asyncio.run(_test_all(proxies))
    except RuntimeError:
        # already inside an event loop (e.g. aiogram) — run in a fresh thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            working = ex.submit(asyncio.run, _test_all(proxies)).result()
    logger.info("%d/%d proxies working", len(working), len(proxies))
    return [p for p, _ in working]


def save_working_proxies(proxies: list[str]) -> None:
    Path(config.WORKING_PROXIES_FILE).write_text("\n".join(proxies) + "\n")
    logger.info("Saved %d working proxies to %s", len(proxies), config.WORKING_PROXIES_FILE)


def load_working_proxies() -> list[str]:
    p = Path(config.WORKING_PROXIES_FILE)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text().splitlines() if l.strip()]


def refresh_proxy_list() -> list[str]:
    proxies = fetch_all_proxies()
    working = test_proxies(proxies)
    save_working_proxies(working)
    return working


class ProxyManager:
    def __init__(self, proxies: list[str] | None = None, auto_refresh: bool = True):
        self._proxies: list[str] = list(proxies) if proxies else load_working_proxies()
        self._failed: set[str] = set()
        self._usage: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if auto_refresh:
            self.start_background_refresh()

    def get_proxy(self) -> str | None:
        with self._lock:
            available = [p for p in self._proxies if p not in self._failed]
            if not available:
                logger.warning("No proxies available")
                return None
            now = time.time()
            fresh = [p for p in available if now - self._last_used.get(p, 0) >= config.RECENT_USE_WINDOW]
            pool = fresh if fresh else available
            ranked = sorted(pool, key=self._proxies.index)
            top = ranked[: max(1, len(ranked) // 4)]
            proxy = random.choice(top)
            self._last_used[proxy] = now
            return proxy

    def mark_failed(self, proxy: str) -> None:
        with self._lock:
            self._failed.add(proxy)
            if proxy in self._proxies:
                self._proxies.remove(proxy)
            logger.warning("Proxy marked as failed: %s", proxy)

    def record_success(self, proxy: str) -> None:
        with self._lock:
            self._usage[proxy] = self._usage.get(proxy, 0) + 1
            if self._usage[proxy] >= config.MAX_SUCCESSES_PER_PROXY:
                self._proxies = [p for p in self._proxies if p != proxy]
                logger.info("Proxy retired after %d successful downloads: %s", self._usage[proxy], proxy)

    def resort(self) -> None:
        with self._lock:
            current = list(self._proxies)
        if not current:
            return
        try:
            logger.info("Re-testing %d proxies to re-sort by speed...", len(current))
            working = test_proxies(current)
            with self._lock:
                failed = self._failed & set(current)
                self._proxies = working + [p for p in current if p not in working and p not in failed]
            save_working_proxies(working)
            logger.info("Proxies re-sorted: %d still working", len(working))
        except Exception as e:
            logger.error("Proxy re-sort failed: %s", e)

    def refresh(self) -> None:
        try:
            logger.info("Refreshing proxy list...")
            working = refresh_proxy_list()
            with self._lock:
                existing = [p for p in self._proxies if p not in self._failed and p not in working]
                merged = working + [p for p in existing if p not in working]
                self._proxies = merged
                self._failed.clear()
            logger.info("Proxy list refreshed: %d proxies available", len(merged))
        except Exception as e:
            logger.error("Proxy refresh failed: %s", e)

    def start_background_refresh(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def _loop():
            last_refresh = time.time()
            last_resort = time.time()
            while not self._stop.wait(60):
                now = time.time()
                if now - last_resort >= config.RESORT_INTERVAL:
                    self.resort()
                    last_resort = now
                if now - last_refresh >= config.REFRESH_INTERVAL:
                    self.refresh()
                    last_refresh = now
                    last_resort = now

        self._thread = threading.Thread(target=_loop, daemon=True, name="proxy-refresh")
        self._thread.start()
        logger.info("Background proxy refresh started (every %ds)", config.REFRESH_INTERVAL)

    def stop(self) -> None:
        self._stop.set()

    def __len__(self) -> int:
        with self._lock:
            return len(self._proxies)
