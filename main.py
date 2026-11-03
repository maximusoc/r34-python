import asyncio
import json
import logging
import os
import random
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydoll.browser.chromium.chrome import Chrome
from pydoll.browser.options import ChromiumOptions

from drive import GoogleDriveClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("uploader")

BASE = "https://rule34.xxx"
LOGIN_URL = f"{BASE}/index.php?page=account&s=login&code=00"
UPLOAD_URL = f"{BASE}/index.php?page=post&s=add"
SOURCE = "https://patreon.com/MaximusOC"

POSTS_FILE = Path("posts.json")
STATE_FILE = Path("storageState.json")
TMP_DIR = Path("tmp_downloads")
DEBUG_DIR = Path("debug")

RATING_MAP = {"safe": "s", "questionable": "q", "explicit": "e"}

# Selectors, adjust here if the site markup changes
SEL = {
    "login_user": 'input[name="user"]',
    "login_pass": 'input[name="pass"]',
    "login_submit": 'input[type="submit"]',
    "file": 'input[type="file"][name="upload"]',
    "source": 'input[name="source"]',
    "title": 'input[name="title"]',
    "rating": lambda r: f'input[name="rating"][value="{r}"]',
    "tags": 'textarea[name="tags"]',
    "submit": 'input[type="submit"][name="submit"]',
}


# ---------- queue ----------
def load_posts() -> list[dict]:
    return json.loads(POSTS_FILE.read_text(encoding="utf-8"))


def save_posts(posts: list[dict]) -> None:
    POSTS_FILE.write_text(json.dumps(posts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pending(posts: list[dict]) -> list[dict]:
    """Only items that were never posted AND never failed. Failed items are never retried."""
    return [p for p in posts if not p.get("posted") and not p.get("failed")]


def mark_posted(posts: list[dict], post: dict) -> None:
    post["posted"] = True
    post["posted_at"] = datetime.now(timezone.utc).isoformat()
    for k in ("failed", "failed_at", "error"):
        post.pop(k, None)
    save_posts(posts)


def mark_failed(posts: list[dict], post: dict, reason: str) -> None:
    if post.get("posted"):
        return  # never downgrade a successful post
    post["failed"] = True
    post["failed_at"] = datetime.now(timezone.utc).isoformat()
    post["error"] = reason[:500]
    save_posts(posts)


# ---------- helpers ----------
async def human_pause(lo=0.6, hi=1.8):
    await asyncio.sleep(random.uniform(lo, hi))


async def type_into(el, text: str, fast: bool = False):
    await el.click()
    await human_pause(0.2, 0.6)
    if fast:
        await el.insert_text(text)
        return
    try:
        await el.type_text(text, humanize=True)
    except TypeError:
        await el.type_text(text, interval=random.uniform(0.04, 0.12))


async def snap(tab, name: str):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        await tab.take_screenshot(path=str(DEBUG_DIR / f"{name}.png"))
    except Exception as e:  # noqa
        log.warning("Screenshot failed: %s", e)


# ---------- session persistence ----------
COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}


async def save_state(browser, tab):
    cookies = await browser.get_cookies()
    clean = [{k: v for k, v in c.items() if k in COOKIE_KEYS} for c in cookies]
    try:
        raw = await tab.execute_script(
            "return JSON.stringify(Object.fromEntries(Object.entries(localStorage)))"
        )
        local = json.loads(raw["result"]["result"]["value"])
    except Exception:
        local = {}
    STATE_FILE.write_text(
        json.dumps({"cookies": clean, "localStorage": local, "origin": BASE}, indent=2),
        encoding="utf-8",
    )
    log.info("Session state saved (%d cookies)", len(clean))


async def load_state(browser, tab) -> bool:
    if not STATE_FILE.exists():
        return False
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("cookies"):
            await browser.set_cookies(state["cookies"])
        if state.get("localStorage"):
            await tab.go_to(BASE)
            payload = json.dumps(state["localStorage"])
            await tab.execute_script(
                f"Object.entries({payload}).forEach(([k,v]) => localStorage.setItem(k,v));"
            )
        log.info("Session state loaded")
        return True
    except Exception as e:
        log.warning("Could not load state: %s", e)
        return False


# ---------- auth ----------
async def is_logged_in(tab) -> bool:
    try:
        # Attempt pydoll's built-in Cloudflare Turnstile clicker if available
        async with tab.expect_cloudflare_turnstile(time_to_wait_captcha=15):
            await tab.go_to(UPLOAD_URL)
    except AttributeError:
        await tab.go_to(UPLOAD_URL)
        
    await human_pause(1.5, 3)
    el = await tab.query(SEL["file"], timeout=5, raise_exc=False)
    
    return el is not None

async def login(tab):
    log.info("Logging in...")
    try:
        # Attempt pydoll's built-in Cloudflare Turnstile clicker if available
        async with tab.expect_cloudflare_turnstile(time_to_wait_captcha=15):
            await tab.go_to(LOGIN_URL)
    except AttributeError:
        await tab.go_to(LOGIN_URL)

    await human_pause(3, 5)
    user = await tab.query(SEL["login_user"], timeout=15, raise_exc=False)
    if not user:
        await snap(tab, "login_blocked")
        log.error("Could not find login input. Page title/URL: %s", await tab.current_url())
        raise RuntimeError("Blocked at login screen (likely Cloudflare challenge). Check debug/login_blocked.png")
    
    pw = await tab.query(SEL["login_pass"], timeout=15)
    await type_into(user, os.environ["R34_USERNAME"])
    await human_pause()
    await type_into(pw, os.environ["R34_PASSWORD"])
    await human_pause()
    btn = await tab.query(SEL["login_submit"], timeout=10)
    await btn.click()
    await human_pause(2.5, 4.5)
    if not await is_logged_in(tab):
        await snap(tab, "login_failed")
        raise RuntimeError("Login failed (bad credentials, captcha, or markup change)")
    log.info("Login OK")

# ---------- tag security ----------
# Tags stripped from every post. Write them with spaces or underscores; both work.
_BLACKLISTED_TAGS_RAW = [
    "cowboy shot",
    "from side",
    "librarian",
    "clitoris",
    "pussy juice",
    "puckered anus",
    "huge areola",
    "erected nipples"
]
BLACKLISTED_TAGS = {t.strip().lower().replace(" ", "_") for t in _BLACKLISTED_TAGS_RAW}

# Tags that must be on every post
REQUIRED_TAGS = ("maximusoc", "ai_generated")

# Tags rewritten to a different tag (lowercase source -> replacement)
TAG_REPLACEMENTS = {
    "1girl": "1girls",
}


def sanitize_tags(raw_tags: str) -> str:
    """Enforce tag rules before upload.

    Rules:
    - Tags in BLACKLISTED_TAGS are removed first (before counting).
    - Tags in TAG_REPLACEMENTS are rewritten (e.g. 1girl -> 1girls).
    - Tags string must contain > 12 space-separated keywords; otherwise raise.
    - 'maximusoc' and 'ai_generated' (REQUIRED_TAGS) are always added if missing,
      and the result is verified to contain them.
    """
    keywords = raw_tags.split()

    removed = [k for k in keywords if k.lower() in BLACKLISTED_TAGS]
    if removed:
        log.info("Removed blacklisted tags: %s", " ".join(removed))
        keywords = [k for k in keywords if k.lower() not in BLACKLISTED_TAGS]

    keywords = [TAG_REPLACEMENTS.get(k.lower(), k) for k in keywords]
    # Dedupe in case a replacement collides with an existing tag
    keywords = list(dict.fromkeys(keywords))

    count = len(keywords)

    if count <= 12:
        raise ValueError(
            f"Tag count ({count}) must be greater than 12. "
            f"Please add more descriptive tags."
        )

    # Inject required tags if absent (case-insensitive check)
    present = {k.lower() for k in keywords}
    for tag in REQUIRED_TAGS:
        if tag not in present:
            keywords.append(tag)
            log.info("Required tag '%s' was missing — added automatically.", tag)

    # Hard guarantee: never return a tag string without the required tags
    final = {k.lower() for k in keywords}
    missing = [t for t in REQUIRED_TAGS if t not in final]
    if missing:
        raise ValueError(f"Required tags missing after sanitizing: {missing}")

    return " ".join(keywords)


# ---------- captcha provider check ----------
MAX_CAPTCHA_REFRESHES = 15

# Detects any captcha that is NOT Cloudflare Turnstile (reCAPTCHA, hCaptcha, etc.).
# Looks at iframes/scripts by URL and at the usual widget containers.
_FOREIGN_CAPTCHA_JS = """
return JSON.stringify((() => {
  const cf = /challenges\\.cloudflare\\.com|cdn-cgi\\/challenge-platform/i;
  const found = [];
  document.querySelectorAll('iframe[src], script[src]').forEach(el => {
    const s = el.src || '';
    if (cf.test(s)) return;
    if (/recaptcha|hcaptcha|geetest|funcaptcha|arkoselabs|mtcaptcha|friendlycaptcha|captcha\\.com|keycaptcha/i.test(s)) found.push(s);
  });
  document.querySelectorAll('.g-recaptcha, .h-captcha, .grecaptcha-badge, [data-hcaptcha-widget-id]')
    .forEach(el => found.push('element:' + (el.className || el.tagName)));
  return found;
})());
"""


async def foreign_captchas(tab) -> list[str]:
    """Return markers of non-Cloudflare captchas on the current page (empty if none)."""
    try:
        raw = await tab.execute_script(_FOREIGN_CAPTCHA_JS)
        return json.loads(raw["result"]["result"]["value"])
    except Exception as e:  # noqa
        log.warning("Captcha provider check failed: %s", e)
        return []


async def load_upload_page(tab):
    """Open the upload page, refreshing until only Cloudflare's captcha (or none) is present."""
    for attempt in range(1, MAX_CAPTCHA_REFRESHES + 1):
        try:
            # Attempt pydoll's built-in Cloudflare Turnstile clicker if available
            async with tab.expect_cloudflare_turnstile(time_to_wait_captcha=15):
                await tab.go_to(UPLOAD_URL)
        except AttributeError:
            await tab.go_to(UPLOAD_URL)
        await human_pause(1.5, 3)

        others = await foreign_captchas(tab)
        if not others:
            if attempt > 1:
                log.info("Upload page clean after %d attempts", attempt)
            return
        log.warning(
            "Non-Cloudflare captcha present (%s); refreshing (%d/%d)",
            ", ".join(others[:3]), attempt, MAX_CAPTCHA_REFRESHES,
        )
        await human_pause(2, 5)

    await snap(tab, "foreign_captcha")
    raise RuntimeError(
        f"Non-Cloudflare captcha still present after {MAX_CAPTCHA_REFRESHES} refreshes"
    )


# ---------- upload ----------
async def upload_post(tab, post: dict, image_path: Path) -> bool:
    # is_sfw: true -> "questionable", false -> "explicit"
    rating = RATING_MAP["questionable" if post["is_sfw"] else "explicit"]
    title_text = f"{post['character']} - {post['source']}"

    await load_upload_page(tab)

    file_input = await tab.query(SEL["file"], timeout=15)
    await file_input.set_input_files(str(image_path.resolve()))
    await human_pause()

    radio = await tab.query(SEL["rating"](rating), timeout=10)
    await radio.click()
    await human_pause(0.4, 1.0)

    src = await tab.query(SEL["source"], timeout=10)
    await src.insert_text("")
    
    await type_into(src, SOURCE, fast=True)
    await human_pause(0.4, 1.0)

    tags = await tab.query(SEL["tags"], timeout=10)

    clean_tags = sanitize_tags(post["booru_tags"])
    log.info("Typing tags: %r", clean_tags)
    await type_into(tags, clean_tags, fast=True)
    await human_pause(1, 2)

    # Title goes after tags: focusing it closes the tag autocomplete dropdown,
    # which otherwise covers the upload button.
    title = await tab.query(SEL["title"], timeout=10, raise_exc=False)
    if title:
        await type_into(title, title_text)
        await human_pause(0.4, 1.0)

    # Final gate: refuse to submit unless the required tags are in the string we typed
    typed = {k.lower() for k in clean_tags.split()}
    if not all(t in typed for t in REQUIRED_TAGS):
        raise ValueError(f"Refusing to submit: required tags {REQUIRED_TAGS} missing")

    submit = await tab.query(SEL["submit"], timeout=10)
    await submit.click()
    await human_pause(4, 7)

    url = await tab.current_url()
    html = (await tab.page_source()).lower()
    if "s=view" in url or "image added" in html:
        log.info("Upload accepted. URL=%s", url)
        return True
    if "already exists" in html or "error" in html:
        log.error("Site reported an error/duplicate. URL=%s", url)
    await snap(tab, f"upload_failed_{image_path.stem}")
    return False


# ---------- main ----------
def build_options() -> ChromiumOptions:
    o = ChromiumOptions()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--window-size=1366,850")
    o.add_argument("--lang=en-US")
    o.add_argument("--disable-blink-features=AutomationControlled")
    # Mask "HeadlessChrome" in the User-Agent so Cloudflare doesn't block immediately
    o.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    if os.getenv("CHROME_PATH"):
        o.binary_location = os.environ["CHROME_PATH"]
    return o

async def main():
    posts = load_posts()
    queue = pending(posts)
    if not queue:
        log.info("Nothing to post.")
        return

    batch = queue[:1]
    log.info("%d pending, processing %d this run", len(queue), len(batch))

    drive = GoogleDriveClient(os.environ["GDRIVE_FOLDER_ID"])
    TMP_DIR.mkdir(exist_ok=True)
    downloaded_files = []
    attempted = 0

    async with Chrome() as browser:
        tab = await browser.start()
        await load_state(browser, tab)
        if not await is_logged_in(tab):
            await login(tab)  # login failure aborts the run; no post is touched or marked
        await save_state(browser, tab)

        for i, post in enumerate(batch):
            log.info("[%d/%d] %s", i + 1, len(batch), post["filename"])
            local = None

            # Clear temp downloads before each task to avoid stale file interference
            shutil.rmtree(TMP_DIR, ignore_errors=True)
            TMP_DIR.mkdir(exist_ok=True)

            # Mark as failed BEFORE trying. If the process is killed mid-upload
            # (timeout, crash, cancel) the item will never be retried, which
            # prevents duplicate posts. Overwritten by mark_posted on success.
            mark_failed(posts, post, "interrupted before completion")
            attempted += 1

            try:
                local = drive.download(post["filename"], str(TMP_DIR))
                downloaded_files.append(local)
                ok = await upload_post(tab, post, local)
                if ok:
                    mark_posted(posts, post)  # persist immediately
                    log.info("Posted: %s", post["filename"])
                    try:
                        await save_state(browser, tab)
                    except Exception as e:
                        log.warning("Could not save session state: %s", e)
                else:
                    mark_failed(posts, post, "Site rejected the upload (error/duplicate/unknown)")
                    log.error("Failed (will not retry): %s", post["filename"])
            except Exception as e:
                log.exception("Error on %s: %s", post["filename"], e)
                mark_failed(posts, post, f"{type(e).__name__}: {e}")

    for local in downloaded_files:
        if local.exists():
            local.unlink(missing_ok=True)
    shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
