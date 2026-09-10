"""Outbound email.

Django cannot send it directly. The function runs in a private subnet with no NAT and
only an S3 gateway endpoint, so it has no route to Brevo - or anywhere else on the
internet - and an attempt would hang until the request timed out. What it *can* reach is
S3, so a message is written there and a Lambda outside the VPC picks it up and sends it.
See `infra/mailer.yaml`.

Queuing is fire-and-forget on purpose. A customer's booking must never fail because an
email could not be written; a lost notification is a smaller problem than a lost sale.
"""

import json
import logging
import uuid

import boto3
from django.conf import settings
from django.utils import timezone

from . import seo

logger = logging.getLogger(__name__)


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
# Both are plain, deliberately. A used-car dealer sending a heavily designed template
# from a new domain is more likely to be filtered than one sending a short, useful note.
# --------------------------------------------------------------------------------------


def _when(slot, language="en"):
    local = timezone.localtime(slot.starts_at)
    if language == "ja":
        return f"{local:%Y年%-m月%-d日（%a）%H:%M}〜{timezone.localtime(slot.ends_at):%H:%M}"
    return f"{local:%A %d %B %Y, %H:%M}–{timezone.localtime(slot.ends_at):%H:%M}"


def _address_lines():
    b = seo.BUSINESS
    return [
        b["name"],
        b["street_address"],
        f"{b['locality']}, {b['region']} {b['postal_code']}, Japan",
    ]


def notify_staff_of_booking(booking):
    """Tell the shop someone has asked for a test drive.

    This is the message that closes the gap where a booking existed only in the admin
    and nobody knew to look, so it leads with what has to be decided: who, when, and a
    link to confirm it.
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    customer = booking.customer
    profile = getattr(customer, "customer_profile", None)
    name = customer.get_full_name() or customer.username
    phone = profile.phone if profile else "not given"
    when = _when(booking.slot)
    car = booking.car_label or "no car specified"
    admin_url = f"{seo.SITE_URL}/api/admin/cars/testdrivebooking/{booking.pk}/change/"

    html = f"""<p><strong>New test drive request &mdash; awaiting your confirmation.</strong></p>
<table cellpadding="4">
  <tr><td>When</td><td><strong>{when}</strong></td></tr>
  <tr><td>Car</td><td>{car}</td></tr>
  <tr><td>Customer</td><td>{name}</td></tr>
  <tr><td>Phone</td><td><a href="tel:{phone}">{phone}</a></td></tr>
  <tr><td>Email</td><td><a href="mailto:{customer.email}">{customer.email}</a></td></tr>
</table>
<p>The place is held for them, but <strong>they have not been told it is confirmed</strong>.
Confirm it here and they will be emailed automatically:</p>
<p><a href="{admin_url}">{admin_url}</a></p>"""

    text = (
        f"New test drive request - awaiting your confirmation.\n\n"
        f"When:     {when}\n"
        f"Car:      {car}\n"
        f"Customer: {name}\n"
        f"Phone:    {phone}\n"
        f"Email:    {customer.email}\n\n"
        f"The place is held, but they have not been told it is confirmed.\n"
        f"Confirm here: {admin_url}\n"
    )

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=f"Test drive request: {when} — {car}",
        html=html,
        text=text,
        # So a reply goes to the customer rather than into a noreply void.
        reply_to=customer.email or None,
    )


def confirm_booking_with_customer(booking):
    """Tell the customer the appointment is on, and everything they need to turn up."""
    customer = booking.customer
    if not customer.email:
        return False

    b = seo.BUSINESS
    when = _when(booking.slot)
    car = booking.car_label or ""
    address = "<br>".join(_address_lines())
    manage_url = f"{seo.SITE_URL}/account"

    html = f"""<p>Hello {customer.first_name or customer.username},</p>
<p>Your test drive at {b['name']} is <strong>confirmed</strong>.</p>
<table cellpadding="4">
  <tr><td>When</td><td><strong>{when}</strong> (Japan time)</td></tr>
  {f'<tr><td>Car</td><td>{car}</td></tr>' if car else ''}
</table>
<p><strong>Where</strong><br>{address}</p>
<p><strong>Phone</strong><br>
  <a href="tel:{b['telephone']}">{b['telephone_display']}</a>
  &mdash; call us if you are running late or cannot make it.</p>
<p>Please bring your driving licence. We will have the car ready for you.</p>
<p>You can change or cancel this yourself at <a href="{manage_url}">{manage_url}</a>.</p>
<p>See you soon.<br>{b['name']}</p>"""

    text = (
        f"Hello {customer.first_name or customer.username},\n\n"
        f"Your test drive at {b['name']} is confirmed.\n\n"
        f"When:  {when} (Japan time)\n"
        + (f"Car:   {car}\n" if car else "")
        + "\nWhere:\n  "
        + "\n  ".join(_address_lines())
        + f"\n\nPhone: {b['telephone_display']} - call us if you are running late "
        f"or cannot make it.\n\n"
        f"Please bring your driving licence. We will have the car ready.\n"
        f"Change or cancel: {manage_url}\n\n"
        f"See you soon.\n{b['name']}\n"
    )

    return queue_email(
        to=customer.email,
        subject=f"Test drive confirmed: {when}",
        html=html,
        text=text,
    )


def notify_customer_of_cancellation(booking):
    """Only sent when the shop cancels - a customer cancelling knows already."""
    customer = booking.customer
    if not customer.email:
        return False

    b = seo.BUSINESS
    when = _when(booking.slot)
    html = f"""<p>Hello {customer.first_name or customer.username},</p>
<p>We are sorry, but we have had to cancel your test drive on <strong>{when}</strong>.</p>
<p>Please call us on <a href="tel:{b['telephone']}">{b['telephone_display']}</a> and we
will find another time, or book one yourself at
<a href="{seo.SITE_URL}/account">{seo.SITE_URL}/account</a>.</p>
<p>{b['name']}</p>"""
    text = (
        f"Hello {customer.first_name or customer.username},\n\n"
        f"We are sorry, but we have had to cancel your test drive on {when}.\n\n"
        f"Please call us on {b['telephone_display']} and we will find another time, "
        f"or book one yourself at {seo.SITE_URL}/account\n\n{b['name']}\n"
    )

    return queue_email(to=customer.email, subject=f"Test drive cancelled: {when}", html=html, text=text)
