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

    This is the message that closes the gap where a booking existed only in the admin and
    nobody knew to look, so it leads with what has to be decided - who, when - and ends
    with the one button that decides it.
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

    html = theme.render(
        heading="New test drive request",
        preheader=f"{name} · {when} · {car}",
        body="".join([
            theme.lead(f"<strong>{_esc(name)}</strong> has asked for a test drive."),
            theme.details([
                ("When", f"<strong>{_esc(when)}</strong>"),
                ("Car", _esc(car)),
                ("Customer", _esc(name)),
                ("Phone", f'<a href="tel:{_esc(phone)}" style="color:{theme.INK};">{_esc(phone)}</a>'),
                ("Email", f'<a href="mailto:{_esc(customer.email)}" style="color:{theme.INK};">'
                          f"{_esc(customer.email)}</a>"),
            ]),
            theme.callout(
                "The slot is held for them, but <strong>they have not been told it is "
                "confirmed</strong>. Confirming it emails them automatically."
            ),
            theme.button("Confirm this booking", admin_url),
            theme.fallback_link(admin_url),
        ]),
    )

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
    greeting = customer.first_name or customer.username
    manage_url = f"{seo.SITE_URL}/account"
    address_html = "<br>".join(_esc(line) for line in _address_lines()[1:])

    html = theme.render(
        heading="Your test drive is confirmed",
        preheader=f"{when} at {b['name']}, Hamura",
        body="".join([
            theme.lead(f"Hello {_esc(greeting)} — we will have the car ready for you."),
            theme.details([
                ("When", f"<strong>{_esc(when)}</strong><br>"
                         f'<span style="font-size:12px;color:{theme.MUTED};">Japan time</span>'),
                ("Car", _esc(car)),
                ("Where", address_html),
                ("Phone", f'<a href="tel:{b["telephone"]}" style="color:{theme.INK};">'
                          f'{b["telephone_display"]}</a>'),
            ]),
            theme.callout("Please bring your driving licence — we cannot let you drive without it."),
            theme.button("Change or cancel this booking", manage_url),
            theme.note(
                f'Running late or cannot make it? Call us on '
                f'<a href="tel:{b["telephone"]}" style="color:{theme.MUTED};">'
                f'{b["telephone_display"]}</a> and we will hold the car.'
            ),
        ]),
    )

    text = (
        f"Hello {greeting},\n\n"
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
    greeting = customer.first_name or customer.username
    book_url = f"{seo.SITE_URL}/account"

    html = theme.render(
        heading="We had to cancel your test drive",
        preheader=f"Your booking for {when} is cancelled. We can find you another time.",
        body="".join([
            theme.lead(
                f"Hello {_esc(greeting)} — we are sorry. Your test drive on "
                f"<strong>{_esc(when)}</strong> is cancelled."
            ),
            theme.paragraph(
                "We would still like to get you behind the wheel. Pick another time that "
                "suits you, or call and we will sort it out between us."
            ),
            theme.button("Find another time", book_url),
            theme.note(
                f'Or call us on <a href="tel:{b["telephone"]}" style="color:{theme.MUTED};">'
                f'{b["telephone_display"]}</a>.'
            ),
        ]),
    )

    text = (
        f"Hello {greeting},\n\n"
        f"We are sorry, but we have had to cancel your test drive on {when}.\n\n"
        f"Please call us on {b['telephone_display']} and we will find another time, "
        f"or book one yourself at {book_url}\n\n{b['name']}\n"
    )

    return queue_email(to=customer.email, subject=f"Test drive cancelled: {when}", html=html, text=text)


# --------------------------------------------------------------------------------------
# Account emails
#
# Bilingual, because half the customers read Japanese. Each carries exactly one action -
# a verification mail that looks like marketing gets ignored, or filtered.
# --------------------------------------------------------------------------------------


def send_verification_email(pending, link):
    b = seo.BUSINESS
    if pending.language == "ja":
        subject = "メールアドレスのご確認 - ダッカモータース"
        html = theme.render(
            language="ja",
            heading="メールアドレスのご確認",
            preheader="下のボタンで登録が完了します。3日間有効です。",
            body="".join([
                theme.lead(f"{_esc(pending.name)} 様 — ご登録ありがとうございます。"),
                theme.paragraph(
                    "下のボタンを押すと、アカウントの作成が完了します。"
                    "<strong>押していただくまで、アカウントは作成されません。</strong>"
                ),
                theme.button("メールアドレスを確認する", link),
                theme.fallback_link(link, "ja"),
                theme.note(
                    "このリンクは3日間有効です。心当たりがない場合は、このメールを破棄してください。"
                    "アカウントは作成されません。"
                ),
            ]),
        )
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
        html = theme.render(
            heading="Confirm your email",
            preheader="One click finishes your account. The link works for three days.",
            body="".join([
                theme.lead(f"Hello {_esc(pending.name)} — thanks for signing up."),
                theme.paragraph(
                    "One click finishes your account and you can book a test drive. "
                    "<strong>Until you do, no account exists.</strong>"
                ),
                theme.button("Confirm my email address", link),
                theme.fallback_link(link),
                theme.note(
                    "The link works for three days. If you did not sign up, ignore this "
                    "email — nothing has been created and nothing will be."
                ),
            ]),
        )
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
        html = theme.render(
            language="ja",
            heading="パスワードの再設定",
            preheader="24時間有効、一度だけ使用できます。",
            body="".join([
                theme.lead(f"{_esc(name)} 様 — パスワード再設定のご依頼を承りました。"),
                theme.paragraph("下のボタンから、新しいパスワードを設定してください。"),
                theme.button("新しいパスワードを設定する", link),
                theme.fallback_link(link, "ja"),
                theme.note(
                    "このリンクは24時間有効で、一度だけ使用できます。"
                    "心当たりがない場合は破棄してください。パスワードは変更されません。"
                ),
            ]),
        )
        text = (
            f"{name} 様\n\nパスワード再設定のご依頼を承りました。\n"
            f"下のリンクから新しいパスワードを設定してください。\n\n{link}\n\n"
            "このリンクは24時間有効で、一度だけ使用できます。\n"
            "心当たりがない場合は破棄してください。パスワードは変更されません。\n\n"
            f"{b['name_ja']}\n"
        )
    else:
        subject = "Reset your password - Dakka Motors"
        html = theme.render(
            heading="Reset your password",
            preheader="Choose a new one. The link works once and expires in 24 hours.",
            body="".join([
                theme.lead(f"Hello {_esc(name)} — someone asked to reset this account's password."),
                theme.paragraph("If that was you, choose a new one here."),
                theme.button("Choose a new password", link),
                theme.fallback_link(link),
                theme.note(
                    "The link works once and expires in 24 hours. If this was not you, "
                    "ignore this email — your password stays exactly as it is."
                ),
            ]),
        )
        text = (
            f"Hello {name},\n\n"
            "Someone asked to reset the password for this account. Use the link below\n"
            f"to choose a new one.\n\n{link}\n\n"
            "It works once and expires in 24 hours. If this was not you, ignore this\n"
            "email - your password stays as it is.\n\n"
            f"{b['name']}\n"
        )

    return queue_email(to=user.email, subject=subject, html=html, text=text)


# --------------------------------------------------------------------------------------
# Questions about a car
# --------------------------------------------------------------------------------------


def notify_staff_of_question(question):
    """Someone has asked something about a car and is waiting on an answer."""
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    customer = question.customer
    name = (customer.get_full_name() or customer.username) if customer else "a visitor"
    email = customer.email if customer else ""
    admin_url = f"{seo.SITE_URL}/api/admin/cars/carquestion/{question.pk}/change/"

    html = theme.render(
        heading="A question about a car",
        preheader=f"{question.car} — {question.question[:80]}",
        body="".join([
            theme.lead(f"<strong>{_esc(name)}</strong> has asked about the "
                       f"{_esc(str(question.car))}."),
            theme.callout(_esc(question.question).replace("\n", "<br>")),
            theme.details([
                ("Car", _esc(str(question.car))),
                ("Asked in", "Japanese" if question.language == "ja" else "English"),
                ("Customer", _esc(name)),
                ("Email", f'<a href="mailto:{_esc(email)}" style="color:{theme.INK};">'
                          f"{_esc(email)}</a>" if email else "—"),
            ]),
            theme.paragraph(
                "Answering emails them straight away. Publishing is a separate step, so "
                "you can reply privately and decide about the public page afterwards."
            ),
            theme.button("Answer this question", admin_url),
            theme.fallback_link(admin_url),
        ]),
    )

    text = (
        f"{name} has asked about the {question.car}.\n\n"
        f"{question.question}\n\n"
        f"Asked in: {'Japanese' if question.language == 'ja' else 'English'}\n"
        f"Customer: {name}\n"
        f"Email:    {email or '-'}\n\n"
        "Answering emails them straight away. Publishing is a separate step.\n"
        f"Answer here: {admin_url}\n"
    )

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=f"Question about the {question.car}",
        html=html,
        text=text,
        reply_to=email or None,
    )


def notify_customer_of_answer(question):
    """Their question has been answered. Sent once, on the first answer only."""
    customer = question.customer
    if not customer or not customer.email:
        return False

    b = seo.BUSINESS
    car_url = f"{seo.SITE_URL}/cars/{question.car.slug}"
    name = customer.first_name or customer.username

    if question.language == "ja":
        subject = f"ご質問への回答 - {question.car}"
        html = theme.render(
            language="ja",
            heading="ご質問への回答",
            preheader=f"{question.car}についてのご質問にお答えしました。",
            body="".join([
                theme.lead(f"{_esc(name)} 様 — お問い合わせありがとうございました。"),
                theme.paragraph("いただいたご質問:"),
                theme.callout(_esc(question.question).replace("\n", "<br>")),
                theme.paragraph(_esc(question.answer).replace("\n", "<br>")),
                theme.button("この車を見る", car_url),
                theme.note(
                    "他にもご不明な点がございましたら、車両ページからお気軽にご質問ください。"
                    f"お急ぎの場合は {b['telephone_display']} までお電話ください。"
                ),
            ]),
        )
        text = (
            f"{name} 様\n\nお問い合わせありがとうございました。\n\n"
            f"ご質問:\n{question.question}\n\n"
            f"回答:\n{question.answer}\n\n"
            f"車両ページ: {car_url}\n"
            f"お電話: {b['telephone_display']}\n\n{b['name_ja']}\n"
        )
    else:
        subject = f"Your question about the {question.car}"
        html = theme.render(
            heading="We have answered your question",
            preheader=f"About the {question.car}.",
            body="".join([
                theme.lead(f"Hello {_esc(name)} — thanks for asking."),
                theme.paragraph("You asked:"),
                theme.callout(_esc(question.question).replace("\n", "<br>")),
                theme.paragraph(_esc(question.answer).replace("\n", "<br>")),
                theme.button("See the car", car_url),
                theme.note(
                    "Anything else you want to know, ask from the car's page — or call "
                    f"us on {b['telephone_display']} if it is easier."
                ),
            ]),
        )
        text = (
            f"Hello {name},\n\nThanks for asking about the {question.car}.\n\n"
            f"You asked:\n{question.question}\n\n"
            f"Our answer:\n{question.answer}\n\n"
            f"See the car: {car_url}\n"
            f"Or call us on {b['telephone_display']}.\n\n{b['name']}\n"
        )

    return queue_email(to=customer.email, subject=subject, html=html, text=text)
