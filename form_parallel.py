import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, TimeoutError as PlaywrightTimeoutError

# ==============================
# SETTINGS
# ==============================
HEADLESS = True
# Two full passes: entire list once, then only first-pass failures once. No per-URL immediate retries.
NUM_PASSES = 2
# Default 3 for speed; use MAX_PARALLEL=2 on low-RAM machines or tight CI if you see OOM.
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "3"))

PAGE_GOTO_TIMEOUT_MS = 90_000
FIELD_WAIT_TIMEOUT_MS = 45_000
# After place_order, poll for thank-you / errors (keep reasonable for slow hosts).
RESULT_WAIT_MS = int(os.environ.get("RESULT_WAIT_MS", "90000"))
# Brief settle after click before polling (AJAX checkout).
POST_CLICK_SETTLE_MS = int(os.environ.get("POST_CLICK_SETTLE_MS", "600"))
# Cap wait after click for full-page navigations (AJAX often already at "load" → returns fast).
POST_CLICK_LOAD_WAIT_MS = int(os.environ.get("POST_CLICK_LOAD_WAIT_MS", "3000"))

BASE_DIR = Path(__file__).resolve().parent


def _origin_prefix(page_url: str) -> str:
    p = urlparse(page_url.strip())
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}".rstrip("/")
    return ""


def _origin_variants(page_url: str) -> list[str]:
    o = _origin_prefix(page_url)
    if not o:
        return []
    out = [o]
    p = urlparse(page_url.strip())
    if not p.scheme or not p.netloc:
        return out
    host = p.netloc.split("@")[-1].lower()
    if host.startswith("www."):
        base = host[4:]
        if base:
            out.append(f"{p.scheme}://{base}".rstrip("/"))
    else:
        out.append(f"{p.scheme}://www.{host}".rstrip("/"))
    return list(dict.fromkeys(out))


def _scheme_alternate(origin: str) -> str | None:
    o = origin.rstrip("/")
    if o.startswith("http://"):
        return ("https://" + o[7:]).rstrip("/")
    if o.startswith("https://"):
        return ("http://" + o[8:]).rstrip("/")
    return None


def _prefix_bundle_for_url(page_url: str) -> list[str]:
    acc: list[str] = []
    for v in _origin_variants(page_url):
        if not v:
            continue
        v = v.rstrip("/")
        acc.append(v)
        alt = _scheme_alternate(v)
        if alt:
            acc.append(alt.rstrip("/"))
    return acc


def _dedupe_prefixes(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        k = x.rstrip("/").lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(x.rstrip("/"))
    return out


def _extra_allow_prefixes_from_env() -> list[str]:
    raw = os.environ.get("EXTRA_ALLOW_ORIGINS", "").strip()
    if not raw:
        return []
    return [x.strip().rstrip("/") for x in raw.split(",") if x.strip()]


def _allowed_prefixes_for_checkout(checkout_url: str) -> list[str]:
    acc: list[str] = []
    acc.extend(_prefix_bundle_for_url(checkout_url))
    acc.extend(_extra_allow_prefixes_from_env())
    bundled = _dedupe_prefixes(acc)
    if not bundled:
        raise ValueError(f"Invalid checkout URL (no origin): {checkout_url!r}")
    return bundled


def refresh_allowlist_after_goto(prefix_state: dict, final_page_url: str) -> None:
    more = _prefix_bundle_for_url(final_page_url)
    prefix_state["prefixes"] = _dedupe_prefixes([*prefix_state["prefixes"], *more])


def _url_matches_allowlist(url: str, prefixes: list[str]) -> bool:
    if not url:
        return False
    lu = url.lower()
    if lu.startswith("blob:") or lu.startswith("data:"):
        return True
    if lu.startswith("about:blank") or lu.startswith("about:srcdoc"):
        return True
    for p in prefixes:
        if not p:
            continue
        pl = p.lower().rstrip("/")
        if lu.startswith(pl):
            return True
        if pl.startswith("https://"):
            if lu.startswith("wss://" + pl[8:]):
                return True
        elif pl.startswith("http://"):
            if lu.startswith("ws://" + pl[7:]):
                return True
    return False


async def install_checkout_allowlist(context, checkout_url: str, prefix_state: dict) -> None:
    """
    Allowlist: only URLs under the checkout origin (www/http/https variants) + EXTRA_ALLOW_ORIGINS.
    Main-frame document navigations always continue (redirects / thank-you).
    After goto(), prefixes grow from final page URL so http→https or host changes still load assets.
    """
    prefix_state["prefixes"] = _allowed_prefixes_for_checkout(checkout_url)

    async def handle_route(route) -> None:
        req = route.request
        rt = req.resource_type
        url = req.url or ""
        frame = req.frame
        prefixes = prefix_state["prefixes"]
        try:
            is_main = frame is not None and frame.parent_frame is None
        except Exception:
            is_main = True

        if rt == "document" and is_main:
            await route.continue_()
            return

        if _url_matches_allowlist(url, prefixes):
            await route.continue_()
            return

        await route.abort()

    await context.route("**/*", handle_route)


def load_key_value_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def load_urls(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    urls: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u and not u.startswith("#"):
                urls.append(u)
    return urls


def log_append(rel_name: str, message: str) -> None:
    path = BASE_DIR / rel_name
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


async def detect_checkout_outcome(page) -> tuple[str, str]:
    """
    Returns ('success', detail) | ('error', message) | ('pending', reason).
    Fast checks only — safe to call in a tight poll loop (no multi-second waits here).
    """
    url = page.url.lower()

    if "order-received" in url:
        return "success", f"redirect url={page.url}"

    try:
        title = await page.title()
        if title and "order-received" in _norm(title):
            return "success", f"redirect title={title!r}"
    except Exception:
        pass

    thank_selectors = (
        ".woocommerce-thankyou-order-received",
        ".woocommerce-order",
        "p.woocommerce-notice--success",
        ".wc-block-order-confirmation-status",
    )
    for sel in thank_selectors:
        loc = page.locator(sel).first
        try:
            if await loc.is_visible():
                return "success", f"matched {sel}"
        except Exception:
            pass

    h_ok = page.locator(
        "h1:has-text('Order received'), h1:has-text('অর্ডার সম্পন্ন')"
    )
    try:
        if await h_ok.first.is_visible():
            return "success", "thank-you heading"
    except Exception:
        pass

    err = page.locator(
        ".woocommerce-error, ul.woocommerce-error, "
        ".woocommerce .woocommerce-error, "
        ".wc-block-components-notice-banner.is-error, "
        ".woocommerce-invalid-required-field .woocommerce-error"
    )
    try:
        n = await err.count()
        for i in range(min(n, 5)):
            el = err.nth(i)
            try:
                if await el.is_visible():
                    text = (await el.inner_text()).strip()
                    if text:
                        return "error", text[:500]
            except Exception:
                continue
    except Exception:
        pass

    pay_err = page.locator(".woocommerce-checkout-payment .woocommerce-error, #payment .woocommerce-error")
    try:
        pe = pay_err.first
        if await pe.is_visible():
            text = (await pe.inner_text()).strip()
            if text:
                return "error", text[:500]
    except Exception:
        pass

    return "pending", "no clear success or error yet"


async def wait_for_checkout_result(page, total_ms: int) -> tuple[str, str]:
    deadline = time.monotonic() + total_ms / 1000
    last_pending = "waiting"
    while time.monotonic() < deadline:
        status, detail = await detect_checkout_outcome(page)
        if status != "pending":
            return status, detail
        last_pending = detail
        await asyncio.sleep(0.25)
    return "timeout", last_pending


async def submit_once(browser: Browser, url: str, name: str, phone: str, address: str) -> tuple[bool, str]:
    context = await browser.new_context(
        locale="en-BD",
        viewport={"width": 1280, "height": 900},
    )
    prefix_state: dict = {"prefixes": []}
    await install_checkout_allowlist(context, url, prefix_state)
    page = await context.new_page()
    page.set_default_timeout(FIELD_WAIT_TIMEOUT_MS)

    try:
        await page.goto(url, timeout=PAGE_GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
        refresh_allowlist_after_goto(prefix_state, page.url)

        await asyncio.gather(
            page.wait_for_selector(
                "#billing_first_name", state="visible", timeout=FIELD_WAIT_TIMEOUT_MS
            ),
            page.wait_for_selector(
                "#billing_phone", state="visible", timeout=FIELD_WAIT_TIMEOUT_MS
            ),
            page.wait_for_selector(
                "#billing_address_1", state="visible", timeout=FIELD_WAIT_TIMEOUT_MS
            ),
        )

        await page.locator("#billing_first_name").scroll_into_view_if_needed()
        await page.locator("#billing_phone").scroll_into_view_if_needed()
        await page.locator("#billing_address_1").scroll_into_view_if_needed()

        await page.fill("#billing_first_name", name)
        await page.fill("#billing_phone", phone)
        await page.fill("#billing_address_1", address)

        order_btn = page.locator("#place_order")
        await order_btn.wait_for(state="visible", timeout=FIELD_WAIT_TIMEOUT_MS)

        await order_btn.click()

        await asyncio.sleep(POST_CLICK_SETTLE_MS / 1000.0)
        try:
            await page.wait_for_load_state("load", timeout=POST_CLICK_LOAD_WAIT_MS)
        except PlaywrightTimeoutError:
            pass

        status, detail = await wait_for_checkout_result(page, RESULT_WAIT_MS)
        if status == "success":
            return True, detail
        if status == "error":
            return False, f"checkout_error: {detail}"
        if status == "timeout":
            return False, f"timeout_waiting_result: {detail}"
        return False, detail

    except PlaywrightTimeoutError as e:
        return False, f"timeout: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        await context.close()


def _write_url_list(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


async def run_pass(
    browser: Browser,
    indexed_urls: list[tuple[int, str]],
    name: str,
    phone: str,
    address: str,
    semaphore: asyncio.Semaphore,
    pass_no: int,
) -> list[tuple[int, str, bool, str]]:
    """Run each (line_no, url) once. Returns list of (line_no, url, ok, reason)."""
    n_items = len(indexed_urls)
    print(
        f"[pass {pass_no}] {n_items} URL(s), up to {MAX_PARALLEL} parallel — starting…"
    )

    async def one(line_no: int, url: str) -> tuple[int, str, bool, str]:
        async with semaphore:
            try:
                ok, reason = await submit_once(browser, url, name, phone, address)
                return line_no, url, ok, reason
            except Exception as e:
                return line_no, url, False, f"{type(e).__name__}: {e}"

    results = await asyncio.gather(*(one(ln, u) for ln, u in indexed_urls))
    for line_no, url, ok, reason in results:
        if ok:
            log_append(
                "success.log",
                f"pass={pass_no} | line={line_no} | {url} | {reason}",
            )
            print(f"[pass {pass_no}] OK line={line_no}: {url}")
        else:
            print(f"[pass {pass_no}] FAIL line={line_no}: {url} — {reason}")
    return results


async def main() -> None:
    form_path = BASE_DIR / "form_data.txt"
    urls_path = BASE_DIR / "urls.txt"

    form_data = load_key_value_file(form_path)
    name = form_data.get("name", "")
    phone = form_data.get("phone", "")
    address = form_data.get("address", "")

    if not (name and phone and address):
        raise SystemExit(
            "form_data.txt must define non-empty name, phone, and address."
        )

    urls = load_urls(urls_path)
    if not urls:
        raise SystemExit("urls.txt has no URLs (non-comment, non-empty lines).")

    for i, u in enumerate(urls):
        s = u.strip()
        if not s.startswith(("http://", "https://")):
            raise SystemExit(
                f"urls.txt line {i + 1}: URL must start with http:// or https:// — got {u!r}"
            )

    n = len(urls)
    line_ok = [False] * n
    line_last_fail: list[str | None] = [None] * n

    for log_name in (
        "success.log",
        "failed.log",
        "retry.log",
        "final_pass.txt",
        "final_fail.txt",
    ):
        (BASE_DIR / log_name).write_text("", encoding="utf-8")

    semaphore = asyncio.Semaphore(MAX_PARALLEL)

    launch_args: list[str] = [
        "--disable-extensions",
        "--disable-background-networking",
    ]
    if os.environ.get("CI"):
        launch_args.extend(["--disable-dev-shm-usage", "--no-sandbox"])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=launch_args)

        try:
            pass1 = await run_pass(
                browser,
                list(enumerate(urls)),
                name,
                phone,
                address,
                semaphore,
                pass_no=1,
            )
            for line_no, url, ok, reason in pass1:
                if ok:
                    line_ok[line_no] = True
                else:
                    line_last_fail[line_no] = reason

            if NUM_PASSES >= 2 and not all(line_ok):
                retry_items = [(i, urls[i]) for i in range(n) if not line_ok[i]]
                log_append(
                    "retry.log",
                    f"--- pass 2: {len(retry_items)} line(s) from pass 1 failures ---",
                )
                for i, u in retry_items:
                    r = line_last_fail[i]
                    log_append("retry.log", f"line={i} | {u} | pass1_reason={r}")
                pass2 = await run_pass(
                    browser,
                    retry_items,
                    name,
                    phone,
                    address,
                    semaphore,
                    pass_no=2,
                )
                for line_no, url, ok, reason in pass2:
                    if ok:
                        line_ok[line_no] = True
                    else:
                        line_last_fail[line_no] = reason
            elif NUM_PASSES >= 2 and all(line_ok):
                log_append("retry.log", "--- pass 2 skipped (no failures in pass 1) ---")

            final_pass_lines = [urls[i] for i in range(n) if line_ok[i]]
            _write_url_list(BASE_DIR / "final_pass.txt", final_pass_lines)

            final_fail_tuples = [
                (urls[i], line_last_fail[i])
                for i in range(n)
                if not line_ok[i] and line_last_fail[i] is not None
            ]
            final_fail_lines = [f"{u} | {r}" for u, r in final_fail_tuples]
            _write_url_list(BASE_DIR / "final_fail.txt", final_fail_lines)

            for u, r in final_fail_tuples:
                log_append("failed.log", f"final | {u} | {r}")

            print(
                f"\nDone. final_pass={len(final_pass_lines)} final_fail={len(final_fail_tuples)} "
                f"(see final_pass.txt, final_fail.txt)"
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
