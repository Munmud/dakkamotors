"""Outbound email.

Nothing is sent from the request path. A message is written to S3 and a second Lambda
picks it up and calls Brevo. See `infra/mailer.yaml`.

That split began as a necessity -- the function was in a private subnet with no NAT and
could not reach Brevo at all -- and survives as a choice now the VPC is gone, for the
reason below.

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
from django.utils.formats import date_format

from . import email_theme as theme
from . import identity
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


#: Monday first, matching `datetime.weekday()`. Written out rather than taken from
#: Django's locale data, because `LANGUAGE_CODE` is "en-us" and activating a translation
#: just to format seven characters would make the output depend on process state.
JA_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")


def _when(starts_at, ends_at, language="en"):
    """Format an appointment window.

    Takes the two instants rather than a slot row: a booking carries snapshots of its
    own window now, so there is no row to pass, and a booking must read correctly even
    after the slot it pointed at is gone.

    Django's `date_format`, not strftime. The Japanese line used to be
    `f"{local:%Y年%-m月%-d日…}"` and had never once run -- nothing passed a language until
    bookings started carrying one -- and it does not work: `%-m` is a glibc extension,
    and on Windows, where this suite runs, strftime goes through the locale codec and
    raises on 年 outright. `Y`, `n`, `j` and `H:i` are Django's own and are the same
    everywhere.
    """
    local = timezone.localtime(starts_at)
    ends = timezone.localtime(ends_at)
    if language == "ja":
        return (f"{date_format(local, 'Y年n月j日')}"
                f"（{JA_WEEKDAYS[local.weekday()]}）"
                f"{date_format(local, 'H:i')}〜{date_format(ends, 'H:i')}")
    return f"{local:%A %d %B %Y, %H:%M}–{ends:%H:%M}"


def _email_for(customer, booking):
    """The customer's address, preferring the live account over the snapshot.

    A booking carries a copy of who made it, so a message about it still reads
    correctly after the account behind it is gone.
    """
    return identity.email_of(customer) or (booking.customer_email or "")


def _greeting_for(customer, booking):
    """What to call them: a first name if there is one, otherwise whatever we have."""
    first = (getattr(customer, "first_name", "") or "").strip()
    return (first
            or identity.full_name_of(customer)
            or (booking.customer_name or "")
            or "there")


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


def _language_of(booking):
    """Which language to write this booking's messages in.

    Read off the booking rather than the account, because the three messages a booking
    sends are spread over days and a guest has no account to consult. Anything that is
    not Japanese is English, so an old booking with no attribute at all still works.
    """
    return "ja" if getattr(booking, "language", "") == "ja" else "en"


def _can_sign_in(booking):
    """Whether a link to /account means anything to the person who made this booking.

    A guest has no Cognito user and no password, so that page is a sign-in form they can
    never get past -- and `BookingCancelView` and `BookingRescheduleView` are
    `IsAuthenticated` anyway, so there is nothing behind it for them either. They are
    told to phone, which is what the shop would have done regardless.

    This was wrong for a day: guest booking shipped and three messages went on pointing
    at a wall, because until then everybody who could book could also sign in.
    """
    return not identity.is_guest(getattr(booking, "customer_sub", ""))


def notify_staff_of_booking(booking, customer=None):
    """Tell the shop someone has asked for a test drive.

    This is the message that closes the gap where a booking existed only in the admin and
    nobody knew to look, so it leads with what has to be decided - who, when - and ends
    with the one button that decides it.
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    name = identity.full_name_of(customer) or booking.customer_name or "a customer"
    phone = identity.phone_of(customer) or booking.customer_phone or "not given"
    email = _email_for(customer, booking)
    when = _when(booking.slot_starts_at, booking.slot_ends_at)
    car = booking.car_label or "no car specified"
    admin_url = f"{seo.SITE_URL}/api/staff/bookings/{booking.booking_id}/"

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
                ("Email", f'<a href="mailto:{_esc(email)}" style="color:{theme.INK};">'
                          f"{_esc(email)}</a>"),
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
        f"Email:    {email}\n\n"
        f"The place is held, but they have not been told it is confirmed.\n"
        f"Confirm here: {admin_url}\n"
    )

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=f"Test drive request: {when} — {car}",
        html=html,
        text=text,
        # So a reply goes to the customer rather than into a noreply void.
        reply_to=email or None,
    )


def notify_staff_of_paused_ad(car, booking, *, paused=True):
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
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    label = car.seo_title_plain or car.slug or "a car"
    when = _when(booking.slot_starts_at, booking.slot_ends_at)
    car_url = f"{seo.SITE_URL}/api/staff/cars/{car.car_id}/"

    if paused:
        heading = "Advertisement stopped"
        lead = (f"The advertisement for the <strong>{_esc(label)}</strong> has been "
                "paused: it has its test drive.")
        callout = ("Nothing more will be spent on this ad set. To advertise this car "
                   "again, or to put the rest of the budget on another one, clear the "
                   "ad set id on the car's page first.")
        subject = f"Ad stopped: {label} has a test drive booked"
        opening = f"The advertisement for the {label} has been paused: it has its test drive."
        plain_note = "Nothing more will be spent on this ad set."
    else:
        heading = "Advertisement still running"
        lead = (f"The <strong>{_esc(label)}</strong> has a test drive booked, but Meta "
                "would not pause its advertisement.")
        callout = ("<strong>It is still spending.</strong> Open Ads Manager and pause "
                   "that ad set by hand.")
        subject = f"Pause this ad by hand: {label}"
        opening = (f"The {label} has a test drive booked, but Meta would not pause its "
                   "advertisement.")
        plain_note = ("IT IS STILL SPENDING. Open Ads Manager and pause that ad set "
                      "by hand.")

    html = theme.render(
        heading=heading,
        preheader=f"{label} · {when}",
        body="".join([
            theme.lead(lead),
            theme.details([
                ("Car", _esc(label)),
                ("Booked for", f"<strong>{_esc(when)}</strong>"),
                ("Ad set", _esc(car.ad_set_id)),
            ]),
            theme.callout(callout),
            theme.button("Open the car", car_url),
            theme.fallback_link(car_url),
        ]),
    )

    text = (
        f"{opening}\n\n"
        f"Car:        {label}\n"
        f"Booked for: {when}\n"
        f"Ad set:     {car.ad_set_id}\n\n"
        f"{plain_note}\n"
        f"Car page: {car_url}\n"
    )

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=subject,
        html=html,
        text=text,
    )


def acknowledge_booking(booking, customer=None):
    """Tell the customer we have their request -- immediately, before staff see it.

    The gap this closes: a booking used to send two messages, both of them after staff
    had acted. Between clicking Confirm and somebody in the shop opening the queue --
    which can be a whole evening -- the customer had nothing in writing at all. A
    signed-in one could at least open /account; a guest, who is most of what an
    advertisement buys, had only the sentence on the screen they were about to close.

    The hard part is what it must NOT say. This is a request, not an appointment, and a
    message that reads like a confirmation would have somebody driving to Hamura on a
    time nobody agreed to. So the callout says so before the details do.
    """
    email = _email_for(customer, booking)
    if not email:
        return False

    b = seo.BUSINESS
    language = _language_of(booking)
    when = _when(booking.slot_starts_at, booking.slot_ends_at, language)
    car = booking.car_label or ""
    greeting = _greeting_for(customer, booking)
    address_html = "<br>".join(_esc(line) for line in _address_lines(language)[1:])

    if language == "ja":
        heading = "試乗のご希望を承りました"
        preheader = f"{when}｜確定し次第改めてご連絡いたします"
        lead = (f"{_esc(greeting)} 様 — "
                + (f"<strong>{_esc(car)}</strong> の" if car else "")
                + "試乗をご希望いただき、"
                  "ありがとうございます。")
        callout = ("<strong>この段階ではまだ確定ではありません。</strong>"
                   "ご希望のお時間を仮押さえして確認しております。"
                   "確定いたしましたら、改めてメールでお知らせいたします。")
        rows = [
            ("ご希望日時", f"<strong>{_esc(when)}</strong>"),
            ("車両", _esc(car)),
            ("場所", address_html),
            ("お電話", f'<a href="tel:{b["telephone"]}" style="color:{theme.INK};">'
                       f'{b["telephone_display"]}</a>'),
        ]
        note = (f'お急ぎの場合やご変更の際は、'
                f'<a href="tel:{b["telephone"]}" style="color:{theme.MUTED};">'
                f'{b["telephone_display"]}</a> までお電話ください。')
        subject = f"試乗のご希望を承りました：{when}"
        text = (
            f"{greeting} 様\n\n"
            f"試乗のご希望を承りました。"
            f"この段階ではまだ確定ではありません。\n"
            f"確定いたしましたら、改めてメールでお知らせいたします。\n\n"
            f"ご希望日時：{when}\n"
            + (f"車両：{car}\n" if car else "")
            + "\n場所：\n  "
            + "\n  ".join(_address_lines("ja"))
            + f"\n\nお電話：{b['telephone_display']}\n\n"
            f"{b['name_ja']}\n"
        )
    else:
        heading = "We have your test drive request"
        preheader = f"{when} — we will confirm it shortly"
        lead = (f"Hello {_esc(greeting)} — thanks for asking to test drive "
                + (f"the <strong>{_esc(car)}</strong>." if car else "with us."))
        callout = ("<strong>This is not a confirmation yet.</strong> We are holding the "
                   "time while we check the diary, and we will email you again as soon "
                   "as it is settled.")
        rows = [
            ("Requested", f"<strong>{_esc(when)}</strong><br>"
                          f'<span style="font-size:12px;color:{theme.MUTED};">Japan time</span>'),
            ("Car", _esc(car)),
            ("Where", address_html),
            ("Phone", f'<a href="tel:{b["telephone"]}" style="color:{theme.INK};">'
                      f'{b["telephone_display"]}</a>'),
        ]
        note = (f'Need it sooner, or need to change it? Call us on '
                f'<a href="tel:{b["telephone"]}" style="color:{theme.MUTED};">'
                f'{b["telephone_display"]}</a>.')
        subject = f"Test drive requested: {when}"
        text = (
            f"Hello {greeting},\n\n"
            f"We have your test drive request. This is not a confirmation yet.\n"
            f"We will email you again as soon as the time is settled.\n\n"
            f"Requested: {when} (Japan time)\n"
            + (f"Car:       {car}\n" if car else "")
            + "\nWhere:\n  "
            + "\n  ".join(_address_lines())
            + f"\n\nPhone: {b['telephone_display']}\n\n"
            f"{b['name']}\n"
        )

    html = theme.render(
        language=language,
        heading=heading,
        preheader=preheader,
        body="".join([
            theme.lead(lead),
            # Before the details, not after. Somebody who skims the time and stops
            # reading has to have met the word "not" first.
            theme.callout(callout),
            theme.details([(label, value) for label, value in rows if value]),
            theme.note(note),
        ]),
    )

    return queue_email(to=email, subject=subject, html=html, text=text)


def confirm_booking_with_customer(booking, customer=None):
    """Tell the customer the appointment is on, and everything they need to turn up."""
    email = _email_for(customer, booking)
    if not email:
        return False

    b = seo.BUSINESS
    language = _language_of(booking)
    when = _when(booking.slot_starts_at, booking.slot_ends_at, language)
    car = booking.car_label or ""
    greeting = _greeting_for(customer, booking)
    manage_url = f"{seo.SITE_URL}/account"
    address_html = "<br>".join(_esc(line) for line in _address_lines(language)[1:])
    # A guest cannot sign in, so the manage page is a wall. They get the phone instead,
    # which is the only thing that would actually have worked for them anyway.
    can_manage = _can_sign_in(booking)

    if language == "ja":
        manage_block = (
            theme.button("予約を変更・キャンセルする", manage_url) if can_manage
            else theme.paragraph(
                f'ご変更・キャンセルは '
                f'<a href="tel:{b["telephone"]}" style="color:{theme.INK};">'
                f'{b["telephone_display"]}</a> までお電話ください。')
        )
        html = theme.render(
            language="ja",
            heading="試乗のご予約が確定しました",
            preheader=f"{when}｜{b['name_ja']}（羽村市）",
            body="".join([
                theme.lead(f"{_esc(greeting)} 様 — お車をご用意してお待ちしております。"),
                theme.details([
                    ("日時", f"<strong>{_esc(when)}</strong><br>"
                             f'<span style="font-size:12px;color:{theme.MUTED};">日本時間</span>'),
                    ("車両", _esc(car)),
                    ("場所", address_html),
                    ("お電話", f'<a href="tel:{b["telephone"]}" style="color:{theme.INK};">'
                               f'{b["telephone_display"]}</a>'),
                ]),
                theme.callout("運転免許証を必ずお持ちください。ご提示のない場合は運転していただけません。"),
                manage_block,
                theme.note(
                    f'遅れそうな場合やご都合が悪くなった場合は、'
                    f'<a href="tel:{b["telephone"]}" style="color:{theme.MUTED};">'
                    f'{b["telephone_display"]}</a> までご連絡ください。お車はお取り置きします。'
                ),
            ]),
        )
        text = (
            f"{greeting} 様\n\n"
            f"{b['name_ja']}での試乗のご予約が確定しました。\n\n"
            f"日時：{when}（日本時間）\n"
            + (f"車両：{car}\n" if car else "")
            + "\n場所：\n  "
            + "\n  ".join(_address_lines("ja"))
            + f"\n\nお電話：{b['telephone_display']}／遅れそうな場合はご連絡ください。\n\n"
            f"運転免許証を必ずお持ちください。お車をご用意してお待ちしております。\n"
            + (f"変更・キャンセル：{manage_url}\n\n" if can_manage
               else f"ご変更・キャンセルは {b['telephone_display']} までお電話ください。\n\n")
            + f"{b['name_ja']}\n"
        )
        subject = f"試乗のご予約が確定しました：{when}"
    else:
        manage_block = (
            theme.button("Change or cancel this booking", manage_url) if can_manage
            else theme.paragraph(
                f'To change or cancel, call us on '
                f'<a href="tel:{b["telephone"]}" style="color:{theme.INK};">'
                f'{b["telephone_display"]}</a>.')
        )
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
                manage_block,
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
            + (f"Change or cancel: {manage_url}\n\n" if can_manage
               else f"To change or cancel, call us on {b['telephone_display']}\n\n")
            + f"See you soon.\n{b['name']}\n"
        )
        subject = f"Test drive confirmed: {when}"

    return queue_email(to=email, subject=subject, html=html, text=text)


def notify_customer_of_cancellation(booking, customer=None):
    """Only sent when the shop cancels - a customer cancelling knows already."""
    email = _email_for(customer, booking)
    if not email:
        return False

    b = seo.BUSINESS
    language = _language_of(booking)
    when = _when(booking.slot_starts_at, booking.slot_ends_at, language)
    greeting = _greeting_for(customer, booking)
    # The car's own page, not /account, when there is one: a guest cannot sign in, and
    # "find another time" for the car they actually wanted beats a generic list anyway.
    can_manage = _can_sign_in(booking)
    book_url = (f"{seo.SITE_URL}/account" if can_manage
                else f"{seo.SITE_URL}/cars/{booking.car_slug}/test-drive"
                if booking.car_slug else seo.SITE_URL)

    if language == "ja":
        html = theme.render(
            language="ja",
            heading="試乗のご予約をキャンセルさせていただきました",
            preheader=f"{when} のご予約はキャンセルとなりました。別のお時間をご案内できます。",
            body="".join([
                theme.lead(
                    f"{_esc(greeting)} 様 — 申し訳ございません。"
                    f"<strong>{_esc(when)}</strong> の試乗はキャンセルとなりました。"
                ),
                theme.paragraph(
                    "ぜひ改めてお乗りいただきたく存じます。"
                    "ご都合のよいお時間をお選びいただくか、お電話いただければ調整いたします。"
                ),
                theme.button("別の日時を選ぶ", book_url),
                theme.note(
                    f'お電話は <a href="tel:{b["telephone"]}" style="color:{theme.MUTED};">'
                    f'{b["telephone_display"]}</a> まで。'
                ),
            ]),
        )
        text = (
            f"{greeting} 様\n\n"
            f"申し訳ございません。{when} の試乗のご予約をキャンセルさせていただきました。\n\n"
            f"{b['telephone_display']} までお電話いただければ別のお時間をご案内いたします。"
            f"ご自身でお選びいただく場合は {book_url}\n\n{b['name_ja']}\n"
        )
        subject = f"試乗のご予約をキャンセルさせていただきました：{when}"
    else:
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
        subject = f"Test drive cancelled: {when}"

    return queue_email(to=email, subject=subject, html=html, text=text)


# --------------------------------------------------------------------------------------
# Account emails
#
# Bilingual, because half the customers read Japanese. Each carries exactly one action -
# a verification mail that looks like marketing gets ignored, or filtered.
# --------------------------------------------------------------------------------------


def send_verification_email(pending, code):
    """The six-digit code that finishes a sign-up.

    A code, not a link. A link opened in the phone's mail app lost the page the
    customer had open on the laptop; a code is typed into the page they are already
    on, and that page then takes them where they were going.
    """
    b = seo.BUSINESS
    if pending.language == "ja":
        subject = f"確認コード {code} - ダッカモータース"
        html = theme.render(
            language="ja",
            heading="メールアドレスのご確認",
            preheader=f"確認コードは {code} です。3日間有効です。",
            body="".join([
                theme.lead(f"{_esc(pending.name)} 様 — ご登録ありがとうございます。"),
                theme.paragraph(
                    "サイトの画面にこのコードを入力すると、アカウントの作成が完了します。"
                    "<strong>入力いただくまで、アカウントは作成されません。</strong>"
                ),
                theme.code_block(code),
                theme.note(
                    "このコードは3日間、5回まで有効です。心当たりがない場合は、このメールを"
                    "破棄してください。アカウントは作成されません。"
                ),
            ]),
        )
        text = "\n".join([
            f"{pending.name} 様",
            "",
            "ダッカモータースへのご登録ありがとうございます。",
            "サイトの画面に下のコードを入力すると、アカウントの作成が完了します。",
            "",
            f"確認コード: {code}",
            "",
            "このコードは3日間、5回まで有効です。心当たりがない場合は破棄してください。"
            "アカウントは作成されません。",
            "",
            f"{b['name_ja']}",
            f"{b['telephone_display']}",
            "",
        ])
    else:
        subject = f"Your code is {code} - Dakka Motors"
        html = theme.render(
            heading="Confirm your email",
            preheader=f"Your code is {code}. It works for three days.",
            body="".join([
                theme.lead(f"Hello {_esc(pending.name)} — thanks for signing up."),
                theme.paragraph(
                    "Type this code into the page you have open and your account is "
                    "ready. <strong>Until you do, no account exists.</strong>"
                ),
                theme.code_block(code),
                theme.note(
                    "The code works for three days and for five tries. If you did not "
                    "sign up, ignore this email — nothing has been created and nothing "
                    "will be."
                ),
            ]),
        )
        text = "\n".join([
            f"Hello {pending.name},",
            "",
            f"Thanks for signing up with {b['name']}. Type the code below into the page",
            "you have open to finish creating your account - until you do, no account",
            "exists.",
            "",
            f"Your code: {code}",
            "",
            "The code works for three days and for five tries. If you did not request",
            "this, ignore this email and nothing will be created.",
            "",
            f"{b['name']}",
            f"{b['telephone_display']}",
            "",
        ])

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


def notify_staff_of_request(request):
    """Somebody wants a car the shop does not have listed.

    The person's details are on the request itself, whether they were typed in as a
    guest or copied from Cognito at the time -- so nothing here needs the user.
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    admin_url = f"{seo.SITE_URL}/api/staff/requests/{request.request_id}/"
    phone_link = (f'<a href="tel:{_esc(request.phone)}" style="color:{theme.INK};">'
                  f"{_esc(request.phone)}</a>")
    email_link = (f'<a href="mailto:{_esc(request.email)}" style="color:{theme.INK};">'
                  f"{_esc(request.email)}</a>")
    written_in = "Japanese" if request.language == "ja" else "English"

    html = theme.render(
        heading="Someone is looking for a car",
        preheader=f"{request.name} — {request.details[:80]}",
        body="".join([
            theme.lead(f"<strong>{_esc(request.name)}</strong> would like the shop "
                       "to find them a car."),
            theme.callout(_esc(request.details).replace("\n", "<br>")),
            theme.details([
                ("Name", _esc(request.name)),
                ("Phone", phone_link),
                ("Email", email_link),
                ("Written in", written_in),
            ]),
            theme.paragraph(
                "Nothing has been sent to them. Call or email when there is something "
                "to say, then mark the request resolved so it leaves the list."
            ),
            theme.button("Open the request", admin_url),
            theme.fallback_link(admin_url),
        ]),
    )

    text = "\n".join([
        f"{request.name} would like the shop to find them a car.",
        "",
        request.details,
        "",
        f"Phone: {request.phone}",
        f"Email: {request.email}",
        f"Written in: {written_in}",
        "",
        f"Open the request: {admin_url}",
        "",
    ])

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=f"Car request from {request.name}",
        html=html,
        text=text,
        reply_to=request.email or None,
    )


def notify_staff_of_question(question, customer=None):
    """Someone has asked something about a car and is waiting on an answer.

    The customer is passed in rather than read off the question: a question carries only
    a subject identifier, and the person's name and address come from Cognito.
    """
    recipients = _config("STAFF_ALERT_EMAIL")
    if not recipients:
        return False

    name = identity.full_name_of(customer) or "a visitor"
    email = identity.email_of(customer)
    admin_url = f"{seo.SITE_URL}/api/staff/questions/{question.question_id}/"

    html = theme.render(
        heading="A question about a car",
        preheader=f"{question.car_label} — {question.question[:80]}",
        body="".join([
            theme.lead(f"<strong>{_esc(name)}</strong> has asked about the "
                       f"{_esc(question.car_label)}."),
            theme.callout(_esc(question.question).replace("\n", "<br>")),
            theme.details([
                ("Car", _esc(question.car_label)),
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
        f"{name} has asked about the {question.car_label}.\n\n"
        f"{question.question}\n\n"
        f"Asked in: {'Japanese' if question.language == 'ja' else 'English'}\n"
        f"Customer: {name}\n"
        f"Email:    {email or '-'}\n\n"
        "Answering emails them straight away. Publishing is a separate step.\n"
        f"Answer here: {admin_url}\n"
    )

    return queue_email(
        to=[address.strip() for address in recipients.split(",")],
        subject=f"Question about the {question.car_label}",
        html=html,
        text=text,
        reply_to=email or None,
    )


def notify_customer_of_answer(question, customer=None):
    """Their question has been answered. Sent once, on the first answer only."""
    if not customer or not identity.email_of(customer):
        return False

    b = seo.BUSINESS
    car_url = f"{seo.SITE_URL}/cars/{question.car_slug}"
    name = (getattr(customer, "first_name", "")
            or identity.full_name_of(customer))

    if question.language == "ja":
        subject = f"ご質問への回答 - {question.car_label}"
        html = theme.render(
            language="ja",
            heading="ご質問への回答",
            preheader=f"{question.car_label}についてのご質問にお答えしました。",
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
        subject = f"Your question about the {question.car_label}"
        html = theme.render(
            heading="We have answered your question",
            preheader=f"About the {question.car_label}.",
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
            f"Hello {name},\n\nThanks for asking about the {question.car_label}.\n\n"
            f"You asked:\n{question.question}\n\n"
            f"Our answer:\n{question.answer}\n\n"
            f"See the car: {car_url}\n"
            f"Or call us on {b['telephone_display']}.\n\n{b['name']}\n"
        )

    return queue_email(to=customer.email, subject=subject, html=html, text=text)
