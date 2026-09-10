"""The look of every email the site sends.

Email is not the web. There is no external stylesheet, no flexbox and no grid; Outlook
renders through Word, Gmail strips anything it dislikes, and roughly a third of people
never load the images. So this is tables, inline styles, and one 600px column - the
boring techniques, because they are the ones that arrive intact.

Two decisions are worth knowing about:

**The logo does not depend on images loading.** The yellow plate is a table cell with a
background colour and a border, so it is drawn by the client itself and is always there.
Only the letter inside it is a PNG. When images are blocked the `alt` text renders in its
place, styled heavy and dark, and the mark still reads as the mark.

**Every message keeps a real plain-text part.** Beyond being what text-only clients show,
a message whose HTML has no text alternative is a well-known spam signal, and this domain
is young enough to need the help.
"""

import html as html_module

from . import seo

# ---------------------------------------------------------------------------- palette --
# The tokens from frontend/src/styles.css. The email and the site are the same brand.
INK = "#1b2430"
INK_SOFT = "#2b3747"
PAPER = "#f4f5f6"
SURFACE = "#ffffff"
PLATE = "#fbd200"
LINE = "#d8dbde"
MUTED = "#61697a"

# Zen Kaku Gothic New cannot be loaded in email, so this is the closest stack that also
# carries Japanese - the JP faces have to be in the list or kana fall back to something
# ugly on Windows.
FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Helvetica,Arial,"
    "'Hiragino Kaku Gothic ProN','Yu Gothic',Meiryo,sans-serif"
)

WIDTH = 600


def _t(language, en, ja):
    return ja if language == "ja" else en


# ------------------------------------------------------------------------- fragments --
#
# Each returns one block of the body column. They are written to be concatenated in the
# order they should appear, which keeps the message functions readable.


def paragraph(html, *, size=15, color=INK_SOFT, top=0):
    return (
        f'<p style="margin:{top}px 0 16px;font-family:{FONT};font-size:{size}px;'
        f'line-height:1.65;color:{color};">{html}</p>'
    )


def lead(html):
    """The one sentence that says why this email exists."""
    return paragraph(html, size=17, color=INK)


def button(label, url):
    """A padded link in a table cell.

    Not a bare <a>: Outlook ignores padding on inline elements, so the cell is the button
    and the link fills it.
    """
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 20px;">
  <tr>
    <td align="center" bgcolor="{INK}" style="border-radius:5px;">
      <a href="{url}" style="display:inline-block;padding:14px 30px;font-family:{FONT};
         font-size:15px;font-weight:bold;line-height:1;color:#ffffff;text-decoration:none;
         border-radius:5px;">{label}</a>
    </td>
  </tr>
</table>"""


def fallback_link(url, language="en"):
    """Because some clients mangle buttons, and some people copy links by hand."""
    label = _t(language, "Or paste this into your browser:", "ボタンが使えない場合はこちら:")
    return (
        f'<p style="margin:0 0 20px;font-family:{FONT};font-size:12px;line-height:1.6;'
        f'color:{MUTED};">{label}<br>'
        f'<a href="{url}" style="color:{MUTED};text-decoration:underline;word-break:break-all;">'
        f"{url}</a></p>"
    )


def details(rows):
    """Label/value pairs - when, which car, who.

    The label column is fixed so the values line up down the message, and each row keeps
    a hairline under it rather than sitting in its own box.
    """
    cells = ""
    for label, value in rows:
        if value in (None, ""):
            continue
        cells += f"""
  <tr>
    <td style="padding:9px 12px 9px 0;border-bottom:1px solid {LINE};font-family:{FONT};
        font-size:12px;line-height:1.5;color:{MUTED};white-space:nowrap;
        vertical-align:top;">{label}</td>
    <td style="padding:9px 0;border-bottom:1px solid {LINE};font-family:{FONT};
        font-size:15px;line-height:1.5;color:{INK};vertical-align:top;">{value}</td>
  </tr>"""
    if not cells:
        return ""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
        f' style="width:100%;border-collapse:collapse;margin:0 0 22px;">{cells}\n</table>'
    )


def callout(html):
    """A held-back note: the plate-yellow edge marks it without shouting."""
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="width:100%;margin:0 0 20px;">
  <tr>
    <td style="padding:14px 16px;background-color:{PAPER};border-left:4px solid {PLATE};
        font-family:{FONT};font-size:14px;line-height:1.6;color:{INK_SOFT};">{html}</td>
  </tr>
</table>"""


def note(html):
    """Small print: what to do if this email was not expected."""
    return (
        f'<p style="margin:0 0 8px;font-family:{FONT};font-size:12px;line-height:1.6;'
        f'color:{MUTED};">{html}</p>'
    )


# ---------------------------------------------------------------------------- shell ---


def _logo():
    """The masthead plate.

    The cell is the plate. Yellow ground, rounded corner, and the letter sits inside it -
    so with images off you still get a yellow plate with a heavy D on it, which is the
    logo. Outlook squares the corners off; it survives that.
    """
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td width="52" height="52" align="center" valign="middle" bgcolor="{PLATE}"
        style="width:52px;height:52px;background-color:{PLATE};border-radius:9px;
        text-align:center;vertical-align:middle;line-height:52px;">
      <img src="{seo.SITE_URL}/assets/email-mark-d.png" width="26" height="25" alt="D"
           style="border:0;outline:none;text-decoration:none;vertical-align:middle;
           font-family:'Arial Black','Arial Bold',Arial,sans-serif;font-size:21px;
           line-height:25px;font-weight:900;color:{INK};">
    </td>
    <td style="padding-left:14px;font-family:{FONT};font-size:18px;font-weight:bold;
        letter-spacing:0.06em;color:#ffffff;white-space:nowrap;">DAKKA MOTORS</td>
  </tr>
</table>"""


def _footer(language):
    b = seo.BUSINESS
    if language == "ja":
        name = b["name_ja"]
        address = f"〒{b['postal_code']} {b['region_ja']}{b['locality_ja']}{b['street_address_ja']}"
        hours = f"営業時間 {b['opens']}〜{b['closes']}"
    else:
        name = b["name"]
        address = (
            f"{b['street_address']}, {b['locality']}, "
            f"{b['region']} {b['postal_code']}, Japan"
        )
        hours = f"Open {b['opens']}–{b['closes']} daily"

    return f"""
<tr>
  <td style="padding:22px 32px 30px;background-color:{PAPER};border-top:1px solid {LINE};">
    <p style="margin:0 0 6px;font-family:{FONT};font-size:13px;font-weight:bold;
       color:{INK};">{name}</p>
    <p style="margin:0 0 10px;font-family:{FONT};font-size:12px;line-height:1.7;
       color:{MUTED};">{address}<br>{hours}</p>
    <p style="margin:0;font-family:{FONT};font-size:12px;line-height:1.7;color:{MUTED};">
      <a href="tel:{b['telephone']}" style="color:{INK_SOFT};text-decoration:none;">
        {b['telephone_display']}</a>
      &nbsp;&middot;&nbsp;
      <a href="{seo.SITE_URL}" style="color:{INK_SOFT};text-decoration:none;">dakkamotors.com</a>
    </p>
  </td>
</tr>"""


def render(*, heading, body, preheader="", language="en"):
    """Wrap a message body in the shell. Returns a complete HTML document.

    `preheader` is the grey line the inbox shows after the subject. Left empty, clients
    fill it with whatever text comes first - usually "View this email", or the logo's alt
    text - so it is worth setting deliberately.
    """
    hidden_preheader = ""
    if preheader:
        # Escaped here rather than at every call site: a preheader is always plain text,
        # and it is often built from a customer's own name, which they chose.
        hidden_preheader = (
            f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
            f'font-size:1px;line-height:1px;color:{PAPER};opacity:0;">'
            f"{html_module.escape(preheader, quote=True)}"
            # Pushes the client's own snippet-scraping past the preheader text.
            f'{"&#8199;&#65279;&#847; " * 60}</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="{language}" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{heading}</title>
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
<![endif]-->
<style>
  /* Kept short on purpose: Gmail strips most of a <style> block, so nothing here is
     load-bearing - it only tidies the edges where it does survive. */
  body {{ margin:0 !important; padding:0 !important; width:100% !important; }}
  a {{ color:{INK}; }}
  @media only screen and (max-width:620px) {{
    .shell {{ width:100% !important; }}
    .pad {{ padding-left:22px !important; padding-right:22px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background-color:{PAPER};">
{hidden_preheader}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background-color:{PAPER};">
  <tr>
    <td align="center" style="padding:28px 12px;">

      <table role="presentation" class="shell" cellpadding="0" cellspacing="0" border="0"
             width="{WIDTH}" style="width:{WIDTH}px;max-width:{WIDTH}px;
             background-color:{SURFACE};border-radius:6px;overflow:hidden;">

        <!-- One yellow edge along the top, the way the plate is the one bold thing on
             the site. Everything below it stays quiet. -->
        <tr><td height="4" bgcolor="{PLATE}"
                style="height:4px;line-height:4px;font-size:4px;background-color:{PLATE};">&nbsp;</td></tr>

        <tr>
          <td class="pad" style="padding:24px 32px;background-color:{INK};">{_logo()}</td>
        </tr>

        <tr>
          <td class="pad" style="padding:32px 32px 8px;">
            <h1 style="margin:0 0 18px;font-family:{FONT};font-size:22px;line-height:1.35;
                font-weight:bold;color:{INK};">{heading}</h1>
            {body}
          </td>
        </tr>
        {_footer(language)}
      </table>

    </td>
  </tr>
</table>
</body>
</html>"""
