"""Bakes the brand rasters: the monogram marks, and the web cuts of the master artwork.

Two things live here because they are two halves of one brand, used at two scales.

`docs/Logo.png` is the master artwork -- a car, a swoosh and a chrome DM, about
1167x600 once its margin is trimmed. It is an illustration, and it needs the room: at
32px it is a grey smudge and at 16px there is nothing left of it at all. So it is used
only where it is large enough to read, and the monogram alone covers the small sizes
(the favicon, and the 26px logo in an email masthead).

Nothing here redraws the artwork. The cuts are a trim, a resize and a composite onto
the artwork's own background colour -- what ships is the supplied file.

Run by hand, not by CI, and not often -- only when the monogram itself changes:

    python docs/brand/generate.py          # Pillow, already in backend/requirements.txt

Its outputs are committed rather than built, because they are not build inputs in any
sense CI would recognise: `docs/brand/*.png` is reference artwork nothing reads, and
the one raster that ships -- `frontend/public/assets/email-mark-dm.png` -- is served
from the frontend bucket, which the frontend workflow syncs straight out of `public/`
without a Python step anywhere near it. Wiring this into a build would mean giving the
Node deploy a Python toolchain to regenerate a file that changes once a year.

The paths below are the same ones in `frontend/public/plate.svg` and the inline
`BrandMark` in `frontend/src/components/Header.jsx`. Three copies is one more than
anybody wants, but the alternatives are worse: the favicon must be a standalone file,
the header must be inline for the gradient to paint with the masthead, and this script
has no SVG renderer to read either of them with. If the mark changes, change all three
-- `test_the_monogram_is_the_same_shape_everywhere` in `cars/tests.py` fails if you do
not.
"""
import pathlib

from PIL import Image, ImageChops, ImageDraw

BRAND = pathlib.Path(__file__).resolve().parent
REPO = BRAND.parents[1]

INK = (27, 39, 52)          # #1b2734
D_OUTER = "M0 0 H30 Q46 0 46 16 V28 Q46 44 30 44 H0 Z"
D_COUNTER = "M12 12 H27 Q34 12 34 19 V25 Q34 32 27 32 H12 Z"
M_OUTER = "M0 44 V0 H11 L23 21 L35 0 H46 V44 H35 V17 L23 38 L11 17 V44 Z"
LETTER_W, LETTER_H, GAP = 46.0, 44.0, 6.0
LOCKUP_W, LOCKUP_H = LETTER_W * 2 + GAP, LETTER_H

# The chrome ramp, matching the SVG gradient stop for stop.
CHROME = [(0.00, (232, 237, 242)), (0.18, (255, 255, 255)), (0.38, (176, 188, 200)),
          (0.52, (242, 246, 249)), (0.72, (150, 163, 176)), (1.00, (221, 228, 234))]

SS = 8                      # supersample factor; the edges are drawn, not hinted


def _tokens(d):
    out, num = [], ""
    for ch in d:
        if ch in "MHVQLZ":
            if num.strip():
                out.append(num.strip())
            num = ""
            out.append(ch)
        elif ch in " ,":
            if num.strip():
                out.append(num.strip())
            num = ""
        else:
            num += ch
    if num.strip():
        out.append(num.strip())
    return out


def flatten(d, steps=32):
    """The SVG subset these paths use, turned into a polygon. Quadratics only."""
    pts, cur, cmd, i, t = [], (0.0, 0.0), None, 0, _tokens(d)
    while i < len(t):
        if t[i] in "MHVQLZ":
            cmd = t[i]
            i += 1
            if cmd == "Z":
                break
            continue
        if cmd in ("M", "L"):
            cur = (float(t[i]), float(t[i + 1]))
            pts.append(cur)
            i += 2
        elif cmd == "H":
            cur = (float(t[i]), cur[1])
            pts.append(cur)
            i += 1
        elif cmd == "V":
            cur = (cur[0], float(t[i]))
            pts.append(cur)
            i += 1
        elif cmd == "Q":
            cx, cy, x, y = (float(v) for v in t[i:i + 4])
            i += 4
            x0, y0 = cur
            for s in range(1, steps + 1):
                u, m = s / steps, 1 - s / steps
                pts.append((m * m * x0 + 2 * m * u * cx + u * u * x,
                            m * m * y0 + 2 * m * u * cy + u * u * y))
            cur = (x, y)
    return pts


def lockup_mask(w, h, scale, dx, dy):
    """'DM' as one alpha mask. The counter is punched, not overdrawn, so the mark can
    sit on any ground."""
    m = Image.new("L", (w * SS, h * SS), 0)
    draw = ImageDraw.Draw(m)

    def put(path, offset, value):
        draw.polygon([((dx + (x + offset) * scale) * SS, (dy + y * scale) * SS)
                      for x, y in flatten(path)], fill=value)

    put(D_OUTER, 0, 255)
    put(D_COUNTER, 0, 0)
    put(M_OUTER, LETTER_W + GAP, 255)
    return m.resize((w, h), Image.LANCZOS)


def chrome_field(w, h):
    """The gradient, swept corner to corner the way the SVG sweeps it."""
    g = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(g)
    for i in range(w + h):
        u = i / max(1, w + h - 1)
        colour = CHROME[-1][1]
        for (a, ca), (b, cb) in zip(CHROME, CHROME[1:]):
            if a <= u <= b:
                f = (u - a) / (b - a)
                colour = tuple(int(ca[k] + (cb[k] - ca[k]) * f) for k in range(3))
                break
        draw.line([(i, 0), (0, i)], fill=colour)
    return g


def tile(px, bleed=0.74, radius=0.1875):
    """The app icon: chrome monogram on a rounded ink tile."""
    big = Image.new("RGBA", (px * SS, px * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, px * SS - 1, px * SS - 1], radius=int(px * SS * radius), fill=INK + (255,))
    scale = px * bleed / LOCKUP_W
    mask = lockup_mask(px, px, scale, (px - LOCKUP_W * scale) / 2,
                       (px - LOCKUP_H * scale) / 2).resize((px * SS, px * SS), Image.LANCZOS)
    big.paste(chrome_field(px * SS, px * SS), (0, 0), mask)
    return big.resize((px, px), Image.LANCZOS)


def email_mark(w=102, h=45):
    """Ink letters on transparent, for the chrome cell in `cars/email_theme.py`.

    Three times the 34x15 the email declares, because a masthead logo is the one image
    in a message people look straight at, and half of them are on a retina phone.
    """
    scale = min(w / LOCKUP_W, h / LOCKUP_H)
    mask = lockup_mask(w, h, scale, (w - LOCKUP_W * scale) / 2, (h - LOCKUP_H * scale) / 2)
    out = Image.new("RGBA", (w, h), INK + (0,))
    out.putalpha(mask)
    return Image.composite(Image.new("RGBA", (w, h), INK + (255,)), out, mask)


# ----------------------------------------------------------------- master artwork ---

MASTER = REPO / "docs" / "Logo.png"


def artwork():
    """`docs/Logo.png` with its flat margin trimmed off.

    The margin is generous and uneven, and it is dead weight in every placement -- as
    padding in the hero, and as wasted area inside a share card's fixed 1200x630. The
    threshold is loose because the ground is dithered rather than one flat value, so an
    exact-match trim would stop at the first stray pixel and take nothing off.
    """
    art = Image.open(MASTER).convert("RGB")
    ground = Image.new("RGB", art.size, art.getpixel((5, 5)))
    mask = ImageChops.difference(art, ground).convert("L").point(lambda v: 255 if v > 18 else 0)
    return art.crop(mask.getbbox())


def hero(width=1100):
    """The home masthead. Twice its ~550px display width, for retina.

    No alpha: the artwork's ground is #1b2734, which is `--hero-ground`, which is what
    the masthead is painted -- so a rectangle of it is invisible against the bar.
    Knocking the background out instead would have meant matting a dithered ground
    against soft drop shadows, which fringes, to solve a seam that does not exist.

    The consequence is that this function cannot move the ground. Changing the band's
    colour means supplying a `docs/Logo.png` painted on the new one.
    """
    art = artwork()
    return art.resize((width, round(art.height * width / art.width)), Image.LANCZOS)


def share_card(w=1200, h=630, margin=0.94):
    """The Open Graph / Twitter card.

    1200x630 is what every scraper crops to, and the trimmed artwork is within a few
    percent of that aspect already, so it drops in with almost nothing wasted. The
    ground is extended in the artwork's own colour rather than letterboxed in black.
    """
    art = artwork()
    scale = min(w * margin / art.width, h * margin / art.height)
    art = art.resize((round(art.width * scale), round(art.height * scale)), Image.LANCZOS)
    card = Image.new("RGB", (w, h), INK)
    card.paste(art, ((w - art.width) // 2, (h - art.height) // 2))
    return card


if __name__ == "__main__":
    assets = REPO / "frontend" / "public" / "assets"

    for size in (180, 192, 512):
        tile(size).save(BRAND / f"mark-{size}.png")
        print(f"docs/brand/mark-{size}.png")

    email_mark().save(assets / "email-mark-dm.png")
    print("frontend/public/assets/email-mark-dm.png")

    # Formats are chosen by audience, not by preference.
    #
    # The hero is read by browsers, so it is WebP: 50KB against 105KB for the same
    # picture as JPEG, and nothing that can render this site cannot render WebP.
    #
    # The share card is read by scrapers -- Facebook, X, LinkedIn, Slack, WhatsApp --
    # and their WebP support is patchy in a way that fails silently, showing no image
    # rather than a worse one. So it stays JPEG, at a quality high enough that the
    # chrome gradients do not band (q85 banded across the D; q92 does not).
    hero().save(assets / "logo-hero.webp", quality=88, method=6)
    print("frontend/public/assets/logo-hero.webp")
    share_card().save(assets / "share-card.jpg", quality=92, optimize=True,
                      progressive=True)
    print("frontend/public/assets/share-card.jpg")
