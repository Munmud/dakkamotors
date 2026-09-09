"""Presigned direct-to-S3 uploads for the admin.

Uploading through Lambda is capped at roughly 4.5 MB: Lambda's synchronous request
payload limit is 6 MB and API Gateway base64-encodes binary on the way in, inflating it
by about a third. Phone photos routinely exceed that and video always does, so the
browser is handed a presigned POST and sends the bytes straight to S3.

A presigned POST is used rather than a presigned PUT because only the POST policy can
carry a `content-length-range` condition. S3 itself then enforces the size cap, so a
modified client cannot simply claim a smaller size and upload whatever it likes.
"""

import uuid

from django.conf import settings
from django.core.files.storage import default_storage

# Formats worth accepting, mapped to the extension the stored object will use.
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# MP4/H.264 is the one combination every browser can play. Since nothing transcodes the
# file after upload, accepting anything else means storing a video that silently fails
# to play for most visitors.
VIDEO_TYPES = {
    "video/mp4": ".mp4",
}

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024

SIGNATURE_EXPIRY_SECONDS = 900


class UploadRejected(Exception):
    """The requested upload is not something we are willing to sign."""


def _validate(kind, content_type, size):
    if kind == "image":
        allowed, limit, label = IMAGE_TYPES, MAX_IMAGE_BYTES, "photo"
    elif kind == "video":
        allowed, limit, label = VIDEO_TYPES, MAX_VIDEO_BYTES, "video"
    else:
        raise UploadRejected("Unknown upload type.")

    if content_type not in allowed:
        if kind == "video":
            raise UploadRejected(
                "Only MP4 video can be used. iPhones set to 'High Efficiency' record "
                "HEVC in a .mov file, which Chrome and Firefox cannot play. Switch "
                "Settings > Camera > Formats to 'Most Compatible', or convert the clip "
                "to MP4 first."
            )
        raise UploadRejected(
            f"{content_type or 'That file type'} cannot be used. Upload a JPEG, PNG or WebP."
        )

    if size is None or size <= 0:
        raise UploadRejected("Could not determine the file size.")
    if size > limit:
        raise UploadRejected(
            f"That {label} is {size / 1024 / 1024:.1f} MB, over the "
            f"{limit // 1024 // 1024} MB limit."
        )

    return allowed[content_type], limit


def build_presigned_upload(kind, content_type, size):
    """Return the S3 POST target plus the storage name to save on the model.

    `name` is relative to the storage's own location prefix, so it can be assigned
    straight to a FileField, while `fields`/`url` are what the browser posts to.
    """
    extension, limit = _validate(kind, content_type, size)

    folder = "cars/video" if kind == "video" else f"cars/{uuid.uuid4()}"
    filename = "video" if kind == "video" else "original"
    if kind == "video":
        filename = str(uuid.uuid4())
    name = f"{folder}/{filename}{extension}"

    bucket = settings.AWS_STORAGE_BUCKET_NAME
    if not bucket:
        raise UploadRejected(
            "Direct upload needs S3. Locally, files are stored on disk - use the "
            "ordinary file picker instead."
        )

    storage = default_storage
    # The storage prefixes everything with its location ("media"), and S3 needs the
    # full key, so reassemble it here rather than hardcoding the prefix.
    key = f"{storage.location}/{name}" if storage.location else name

    presigned = storage.connection.meta.client.generate_presigned_post(
        Bucket=bucket,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, limit],
        ],
        ExpiresIn=SIGNATURE_EXPIRY_SECONDS,
    )

    return {"url": presigned["url"], "fields": presigned["fields"], "name": name}
