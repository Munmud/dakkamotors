"""WCAG contrast over every rendered text node on every page, both themes' one theme.

Measured rather than eyeballed: a dark theme fails quietly. Dim grey on near-black
looks deliberate at any ratio, so the only way to know whether `--text-dim` clears
4.5:1 is to read the computed colours off the live page and do the arithmetic.
"""

import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
PAGES = ["/", "/cars/2012-toyota-prius", "/cars/2011-suzuki-wagon-r",
         "/request-a-car", "/privacy"]

# Walks up for the first non-transparent background, because text almost never sits on
# an element that paints one.
SCRIPT = """
() => {
  const lum = ([r, g, b]) => {
    const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92
                                : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const parse = (s) => (s.match(/[\\d.]+/g) || []).slice(0, 4).map(Number);
  const opaque = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c.length >= 3 && (c[3] === undefined || c[3] > 0.95)) return c.slice(0, 3);
      n = n.parentElement;
    }
    return [11, 17, 20];
  };
  const out = [];
  for (const el of document.querySelectorAll("*")) {
    const text = [...el.childNodes]
      .filter((n) => n.nodeType === 3).map((n) => n.textContent.trim())
      .join(" ").trim();
    if (!text) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") continue;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    // Text drawn over a photograph has no computed background to read, and its
    // contrast is a property of the photo. Skipped rather than guessed at.
    if (el.closest(".card__frame, .gallery")) continue;
    const fg = parse(cs.color).slice(0, 3);
    const bg = opaque(el);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    const px = parseFloat(cs.fontSize);
    const large = px >= 24 || (px >= 18.66 && parseInt(cs.fontWeight, 10) >= 700);
    out.push({ text: text.slice(0, 44), ratio: Math.round(ratio * 100) / 100,
               need: large ? 3 : 4.5, px, sel: el.tagName.toLowerCase() +
               (el.className && typeof el.className === "string"
                 ? "." + el.className.trim().split(/\\s+/).join(".") : "") });
  }
  return out;
}
"""

fails = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width in (390, 1440):
        ctx = browser.new_context(viewport={"width": width, "height": 900},
                                  locale="ja-JP", color_scheme="dark")
        for path in PAGES:
            page = ctx.new_page()
            page.goto(BASE + path)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            for row in page.evaluate(SCRIPT):
                if row["ratio"] < row["need"]:
                    fails.append((width, path, row))
            page.close()
        ctx.close()
    browser.close()

if not fails:
    print("PASS: every text node clears its WCAG AA threshold.")
else:
    seen = set()
    print(f"{len(fails)} failing text nodes:\n")
    for width, path, r in sorted(fails, key=lambda f: f[2]["ratio"]):
        key = (r["sel"], r["text"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  {r['ratio']:5.2f} (needs {r['need']}) {r['px']:.0f}px  {r['sel']}")
        print(f"        {r['text']!r}  [{width}px {path}]")
sys.exit(0)
