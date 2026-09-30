"""Outbound email.

Nothing is sent from the request path. A message is written to S3 and a second Lambda
picks it up and calls Brevo. See `infra/mailer.yaml`.

That split began as a necessity -- the function was in a private subnet with no NAT and
could not reach Brevo at all -- and survives as a choice now the VPC is gone, for the
reason below.

Queuing is fire-and-forget on purpose. A lead must never fail because an email could
not be written; a lost alert is a smaller problem than a lost sale.
"""

import html as html_module
import json
import logging
import uuid

import boto3
from django.conf import settings
from django.utils import timezone
from django.utils.formats import date_format

from . import email_theme as theme
from . import seo

logger = logging.getLogger(__name__)


def _esc(value):
    return html_module.escape(str(value or ""), quote=True)


def _config(name, default=""):
    return getattr(settings, name, default)


def queue_email(*, to, subject, html, text="", reply_to=None):
    """Put one message in the outbox. Returns True if it was queued."""
    bucket = _config("OUTBOX_BUCKET")
    sender = _config("MAIL_FROM")
    recipients = [address for address in (to if isinstance(to, (list, tuple)) else [to]) if address]

    if not bucket or not sender:
        # Local development, and any deploy where mail is not configured yet.
        logger.info("Email not configured; skipping %r to %s", subject, recipients)
        return False
    if not recipients:
        return False

    message = {
        "from": sender,
        "fromName": _config("MAIL_FROM_NAME", seo.BUSINESS["name"]),
        "replyTo": reply_to or _config("MAIL_REPLY_TO") or None,
        "to": recipients,
        "subject": subject,
        "html": html,
        "text": text,
        "queued_at": timezone.now().isoformat(),
    }

    try:
        boto3.client("s3", region_name=_config("AWS_S3_REGION_NAME", "ap-northeast-1")).put_object(
            Bucket=bucket,
            Key=f"outbox/{uuid.uuid4()}.json",
            Body=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
    except Exception:
        logger.exception("Could not queue email %r to %s", subject, recipients)
        return False

    return True

# --------------------------------------------------------------------------------------
# Messages
#
# Each one is built from the fragments in `email_theme`, so they share a masthead, a type
# scale and a footer. The plain-text part is written by hand rather than stripped from the
# HTML: it is what a text-only client shows, and a message with no text alternative scores
# badly with spam filters.
# --------------------------------------------------------------------------------------


def _address_lines(language="en"):
    """Where the shop is, as lines to stack. Same two forms the email footer uses."""
    b = seo.BUSINESS
    if language == "ja":
        return [
            b["name_ja"],
            f"〒{b['postal_code']}",
            f"{b['region_ja']}{b['locality_ja']}{b['street_address_ja']}",
        ]
    return [
        b["name"],
        b["street_address"],
        f"{b['locality']}, {b['region']} {b['postal_code']}, Japan",
    ]


def notify_staff_of_paused_ad(car, lead, *, paused=True):
    """Tell the owner the car's advertisement has been stopped, and why.

    This is the one message about money rather than about a customer, and it is sent
    because the stop happened without anybody asking for it: an advertisement that
    turns itself off silently is indistinguishable from one that broke. It says which
    car, which ad set, and what stopped it, so the decision to start it again -- or to
    put the rest of the budget on a different car -- can be made from the message.

    `paused=False` when Meta refused the call. Then this is the opposite message, and
    a far more urgent one: the ad set is still spending and only a person can stop it.
    It names the ad set for exactly that reason. Saying "stopped" in both cases would
    be the single most expensive sentence on the site.

    The trigger used to be a test drive being booked. It is a lead now -- somebody
    leaving their number about this car -- which is the same moment by a different
    name: the advertisement has produced the one thing it was bought to produce.
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    label = car.seo_title_plain or car.slug or "a car"
    who = lead.name or "somebody"
    car_url = f"{seo.SITE_URL}/api/staff/cars/{car.car_id}/"

    if paused:
        heading = "Advertisement stopped"
        opener = (f"The advertisement for the <strong>{_esc(label)}</strong> has been "
                  f"paused: <strong>{_esc(who)}</strong> asked about it.")
        callout = ("Nothing more will be spent on this ad set. To advertise this car "
                   "again, or to put the rest of the budget on another one, clear the "
                   "ad set id on the car's page first.")
        subject = f"Ad stopped: somebody asked about the {label}"
        opening = (f"The advertisement for the {label} has been paused: {who} asked "
                   "about it.")
        plain_note = "Nothing more will be spent on this ad set."
    else:
        heading = "Advertisement still running"
        opener = (f"Somebody asked about the <strong>{_esc(label)}</strong>, but Meta "
                  "would not pause its advertisement.")
        callout = ("<strong>It is still spending.</strong> Open Ads Manager and pause "
                   "that ad set by hand.")
        subject = f"Pause this ad by hand: {label}"
        opening = (f"Somebody asked about the {label}, but Meta would not pause its "
                   "advertisement.")
        plain_note = ("IT IS STILL SPENDING. Open Ads Manager and pause that ad set "
                      "by hand.")

    html = theme.render(
        heading=heading,
        preheader=f"{label} · {who}",
        body="".join([
            theme.lead(opener),
            theme.details([
                ("Car", _esc(label)),
                ("Asked by", f"<strong>{_esc(who)}</strong>"),
                ("Ad set", _esc(car.ad_set_id)),
            ]),
            theme.callout(callout),
            theme.button("Open the car", car_url),
            theme.fallback_link(car_url),
        ]),
    )

    text = (
        f"{opening}\n\n"
        f"Car:      {label}\n"
        f"Asked by: {who}\n"
        f"Ad set:   {car.ad_set_id}\n\n"
        f"{plain_note}\n"
        f"Car page: {car_url}\n"
    )

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=subject,
        html=html,
        text=text,
    )


def notify_staff_of_request(request):
    """Somebody left their number. This is the message the whole site exists to send.

    It leads with the phone number, because that is the only thing anybody has to do
    with it, and because the difference between ringing back in five minutes and
    ringing back tomorrow is most of the difference between a sale and no sale.

    Two shapes, one message: a lead about a car names the car, a lead about no car
    carries only the wish. Everything else is the same, which is why they are one
    feature.
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    admin_url = f"{seo.SITE_URL}/api/staff/requests/{request.request_id}/"
    phone_link = (f'<a href="tel:{_esc(request.phone)}" style="color:{theme.INK};">'
                  f"{_esc(request.phone)}</a>")
    email_link = (f'<a href="mailto:{_esc(request.email)}" style="color:{theme.INK};">'
                  f"{_esc(request.email)}</a>" if request.email else "not given")
    written_in = "Japanese" if request.language == "ja" else "English"
    about = request.car_label or ""

    if about:
        heading = "Someone wants this car"
        opener = (f"<strong>{_esc(request.name)}</strong> asked about the "
                  f"<strong>{_esc(about)}</strong>.")
        subject = f"{request.name} wants the {about} - {request.phone}"
        text_opener = f"{request.name} asked about the {about}."
    else:
        heading = "Someone is looking for a car"
        opener = (f"<strong>{_esc(request.name)}</strong> would like the shop "
                  "to find them a car.")
        subject = f"Car request from {request.name} - {request.phone}"
        text_opener = f"{request.name} would like the shop to find them a car."

    rows = [("Name", _esc(request.name)), ("Phone", phone_link)]
    if about:
        rows.append(("Car", _esc(about)))
    rows += [("Email", email_link), ("Written in", written_in)]

    html = theme.render(
        heading=heading,
        preheader=f"{request.name} · {request.phone}",
        body="".join([
            theme.lead(opener),
            theme.details(rows),
        ] + (
            [theme.callout(_esc(request.details).replace("\n", "<br>"))]
            if request.details else []
        ) + [
            theme.paragraph(
                "Nothing has been sent to them and they are not expecting an email -- "
                "they asked for a call. Ring the number above, then mark this resolved "
                "so it leaves the list."
            ),
            theme.button("Open the lead", admin_url),
            theme.fallback_link(admin_url),
        ]),
    )

    text = "\n".join([
        text_opener,
        "",
        f"Phone: {request.phone}",
        f"Email: {request.email or 'not given'}",
    ] + ([f"Car:   {about}"] if about else []) + [
        f"Written in: {written_in}",
        "",
    ] + ([request.details, ""] if request.details else []) + [
        "They asked for a call, not an email.",
        f"Open the lead: {admin_url}",
        "",
    ])

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=subject,
        html=html,
        text=text,
        reply_to=request.email or None,
    )
