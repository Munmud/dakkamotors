"""Screenshot every public page at phone and desktop width, in both languages.

    python tools/screenshot.py <output-dir>

Needs the site running: Django on :8000 against a seeded table (`tools/seed_lot.py`)
and Vite on :5173, which proxies /api to it.

Written for the restructure that made the site one job -- get a name and a phone
number -- so the set deliberately includes the two states a visitor only reaches by
acting, the filled form and the confirmation that replaces it. Those are the funnel,
and a page nobody has interacted with does not show them.

The submit at the end posts a real lead to whatever backend is running. Point this at
a local server, never at production.
"""

import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
OUT = pathlib.Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

PHONE = {"width": 390, "height": 844}
DESK = {"width": 1440, "height": 900}

PAGES = [
    ("home", "/"),
    ("car", "/cars/2012-toyota-prius"),
    ("car-reserved", "/cars/2016-honda-n-box"),
    ("car-no-price", "/cars/2011-suzuki-wagon-r"),
    ("request", "/request-a-car"),
    ("privacy", "/privacy"),
]


def settle(page):
    """Load, then scroll the whole page so lazy images actually fetch.

    `full_page=True` stitches a tall screenshot without ever scrolling the viewport,
    so every `loading="lazy"` image below the fold stays unfetched and photographs as
    an empty box. That is an artefact of the harness, not of the site, and it makes
    the saved shots lie about the thing they exist to show.
    """
    page.wait_for_load_state("networkidle")
    height = page.evaluate("document.body.scrollHeight")
    for y in range(0, height, 400):
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(60)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(700)


with sync_playwright() as p:
    browser = p.chromium.launch()
    for size_name, size in (("phone", PHONE), ("desktop", DESK)):
        for locale, lang in (("ja", "ja-JP"), ("en", "en-US")):
            context = browser.new_context(viewport=size, locale=lang,
                                          device_scale_factor=2,
                                          color_scheme="dark")
            for label, path in PAGES:
                suffix = "" if locale == "ja" else "-en"
                url = path + ("?lang=en" if locale == "en" else "")
                page = context.new_page()
                page.goto(BASE + url)
                settle(page)
                page.screenshot(
                    path=str(OUT / f"{size_name}-{label}{suffix}.png"),
                    full_page=True)
                page.close()
            context.close()

    # The two states a visitor only reaches by acting: a filled form, and the thank
    # you that replaces it. These are the whole funnel, so they are worth a shot each.
    for size_name, size in (("phone", PHONE), ("desktop", DESK)):
        context = browser.new_context(viewport=size, locale="ja-JP",
                                      device_scale_factor=2, color_scheme="dark")
        page = context.new_page()
        page.goto(BASE + "/cars/2012-toyota-prius")
        settle(page)
        form = page.locator("form.lead").first
        # By autocomplete, not by name: the inputs are controlled React and carry no
        # `name` -- nothing ever native-submits this form.
        form.locator('input[autocomplete="name"]').fill("佐藤 花子")
        form.locator('input[type="tel"]').fill("080-1234-5678")
        page.wait_for_timeout(200)
        form.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / f"{size_name}-car-form-filled.png"))

        form.locator('button[type="submit"]').click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / f"{size_name}-car-form-sent.png"))
        page.close()
        context.close()

    browser.close()

print(f"wrote {len(list(OUT.glob('*.png')))} screenshots to {OUT}")
