"""Outbound email.

Django cannot send it directly. The function runs in a private subnet with no NAT and
only an S3 gateway endpoint, so it has no route to Brevo - or anywhere else on the
internet - and an attempt would hang until the request timed out. What it *can* reach is
S3, so a message is written there and a Lambda outside the VPC picks it up and sends it.
See `infra/mailer.yaml`.

Queuing is fire-and-forget on purpose. A customer's booking must never fail because an
email could not be written; a lost notification is a smaller problem than a lost sale.
"""

import html as html_module
import json
import logging
import uuid

import boto3
from django.conf import settings
from django.utils import timezone

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


# --------------------------------------------------------------------------------------
# Account emails
#
# Bilingual, because half the customers read Japanese. Each is one clear link and
# nothing else - a verification mail that looks like marketing gets ignored or filtered.
# --------------------------------------------------------------------------------------


def send_verification_email(pending, link):
    b = seo.BUSINESS
    if pending.language == "ja":
        subject = "メールアドレスのご確認 - ダッカモータース"
        html = f"""<p>{_esc(pending.name)} 様</p>
<p>ダッカモータースへのご登録ありがとうございます。
下のリンクをクリックすると、アカウントの作成が完了します。</p>
<p><a href="{link}">{link}</a></p>
<p>このリンクは3日間有効です。心当たりがない場合は、このメールは破棄してください。
アカウントは作成されません。</p>
<p>{b['name_ja']}<br>{b['telephone_display']}</p>"""
        text = (
            f"{pending.name} 様\n\n"
            "ダッカモータースへのご登録ありがとうございます。\n"
            "下のリンクを開くと、アカウントの作成が完了します。\n\n"
            f"{link}\n\n"
            "このリンクは3日間有効です。心当たりがない場合は破棄してください。"
            "アカウントは作成されません。\n\n"
            f"{b['name_ja']}\n{b['telephone_display']}\n"
        )
    else:
        subject = "Confirm your email - Dakka Motors"
        html = f"""<p>Hello {_esc(pending.name)},</p>
<p>Thanks for signing up with {b['name']}. Click the link below to finish creating your
account &mdash; until you do, no account exists.</p>
<p><a href="{link}">{link}</a></p>
<p>The link works for three days. If you did not request this, ignore this email and
nothing will be created.</p>
<p>{b['name']}<br>{b['telephone_display']}</p>"""
        text = (
            f"Hello {pending.name},\n\n"
            f"Thanks for signing up with {b['name']}. Open the link below to finish\n"
            "creating your account - until you do, no account exists.\n\n"
            f"{link}\n\n"
            "The link works for three days. If you did not request this, ignore this\n"
            "email and nothing will be created.\n\n"
            f"{b['name']}\n{b['telephone_display']}\n"
        )

    return queue_email(to=pending.email, subject=subject, html=html, text=text)


def send_password_reset_email(user, link, language="en"):
    b = seo.BUSINESS
    name = user.first_name or user.username
    if language == "ja":
        subject = "パスワードの再設定 - ダッカモータース"
        html = f"""<p>{_esc(name)} 様</p>
<p>パスワード再設定のご依頼を承りました。下のリンクから新しいパスワードを設定してください。</p>
<p><a href="{link}">{link}</a></p>
<p>このリンクは24時間有効で、一度だけ使用できます。
心当たりがない場合は破棄してください。パスワードは変更されません。</p>
<p>{b['name_ja']}</p>"""
        text = (
            f"{name} 様\n\nパスワード再設定のご依頼を承りました。\n"
            f"下のリンクから新しいパスワードを設定してください。\n\n{link}\n\n"
            "このリンクは24時間有効で、一度だけ使用できます。\n"
            "心当たりがない場合は破棄してください。パスワードは変更されません。\n\n"
            f"{b['name_ja']}\n"
        )
    else:
        subject = "Reset your password - Dakka Motors"
        html = f"""<p>Hello {_esc(name)},</p>
<p>Someone asked to reset the password for this account. Use the link below to choose a
new one.</p>
<p><a href="{link}">{link}</a></p>
<p>It works once and expires in 24 hours. If this was not you, ignore this email &mdash;
your password stays as it is.</p>
<p>{b['name']}</p>"""
        text = (
            f"Hello {name},\n\n"
            "Someone asked to reset the password for this account. Use the link below\n"
            f"to choose a new one.\n\n{link}\n\n"
            "It works once and expires in 24 hours. If this was not you, ignore this\n"
            "email - your password stays as it is.\n\n"
            f"{b['name']}\n"
        )

    return queue_email(to=user.email, subject=subject, html=html, text=text)
