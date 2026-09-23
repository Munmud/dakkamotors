"""Inventory. Replaces `CarAdmin` and its `CarImageInline`.

The largest of the staff pages, because the admin was doing the most for free here: a
changelist with filters and a substring search, a change form with five field groups, an
inline formset for photos, and the direct-to-S3 upload that sidesteps Lambda's ~4.5 MB
request ceiling.

The upload JavaScript carries over almost unchanged. It derives the hidden field's name
from `input.name` with `.replace(/image$/, "image_key")`, and a plain `formset_factory`
names fields `form-0-image`, which that regex still matches.
"""

import itertools

from django.contrib import messages
from django.core.files.storage import default_storage
from django.forms import formset_factory
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .. import cdn
from ..choices import DERIVATIVE_WIDTHS
from ..forms import CarFilterForm, CarForm, CarImageForm, CarSpecForm, CarVideoForm
from .. import specs as spec_rules
from ..images import build_derivatives
from ..tasks import build_derivatives_task
from ..store import cars as car_store
from ..store import images as image_store
from ..store import media as media_store
from ..store import videos as video_store
from ..store import keys
from ..store import search
from ..store.errors import ConditionFailed, NotFound
from ..store.models import Car
from .auth import requires, staff_required

#: No blank rows on any of the three. A row appears when its "+ Add" button is pressed
#: and not before -- three empty file inputs on a page that had no photos yet were never
#: a feature, they were the allowance from before the button existed, and they made a
#: form for one car open with a screen of things to skip past.
#:
#: The trade-off is stated here because it is real: the add buttons ship `hidden` and
#: `formset-rows.js` reveals them, so a browser with no JavaScript now has no way to
#: attach a photo at all. That was the reason photos kept three rows for as long as
#: they did. In practice the no-JS case is the few-second window on each deploy where
#: collectstatic has not yet caught up with zappa update; the templates say so in a
#: <noscript> beside each button rather than leaving somebody staring at a page with
#: nowhere to put a file.
ImageFormSet = formset_factory(CarImageForm, extra=0, can_delete=True)
VideoFormSet = formset_factory(CarVideoForm, extra=0, can_delete=True)
#: `max_num` publishes the ceiling through the management form's MAX_NUM_FORMS field,
#: which `formset-rows.js` reads to know when to stop. `validate_max` stays False --
#: stated rather than left to the default, because it matters: `specs.clean` is the one
#: place that refuses row 41, and turning this on would add a second wording for the
#: same rule.
SpecFormSet = formset_factory(CarSpecForm, extra=0, max_num=spec_rules.MAX_PAIRS,
                              validate_max=False, can_delete=True)


def _spec_rows(formset):
    """Submitted rows in page order, deletions removed. Validation is `specs.clean`."""
    return [
        {name: form.cleaned_data.get(name, "") for name in spec_rules.FIELDS}
        for form in formset
        if form not in formset.deleted_forms and form.cleaned_data
    ]

#: Fields the form owns. Kept explicit so a new form field cannot silently start
#: writing something the store did not expect.
EDITABLE = (
    "brand", "model_name", "manufacture_year", "fuel_type", "seat_capacity",
    "color", "price_jpy", "status", "description_en", "description_ja",
    "brand_ja", "model_name_ja", "color_ja", "sold_at", "ad_set_id",
)

#: Blank means absent here, not empty.
#:
#: `CharField(required=False)` hands back `""`, and for a field the store reads with a
#: plain truth test that leaves an empty attribute on the item rather than an absent
#: one. `ad_set_id` is exactly that: "" would satisfy a `.exists()` condition while
#: meaning "not advertising", and clearing the box is how the owner readies a car for a
#: second campaign, so it has to actually clear.
#:
#: What must NOT go in here is a name that is not in `EDITABLE`. `values.get(name)`
#: returns None for one, which would put `chassis_number: None` back into the dict and
#: write it over every car on its next save -- and `store.cars.update` would read that
#: as the number having changed and delete the guard for one still on the car.
BLANK_IS_ABSENT = ("ad_set_id",)


def _field_values(form):
    """The form's values, with blanks that mean "not given" turned back into None."""
    values = {name: form.cleaned_data.get(name) for name in EDITABLE}
    for name in BLANK_IS_ABSENT:
        if not (values.get(name) or "").strip():
            values[name] = None
    # The store keeps the sale date as an ISO string -- it is a date, and a date sorts
    # as one -- while the form works in `datetime.date`, which is what a date input
    # deserves. The conversion belongs here, where the form stops and the store starts.
    sold_at = values.get("sold_at")
    values["sold_at"] = sold_at.isoformat() if sold_at else None
    return values


@staff_required
@requires("car.view")
def car_list(request):
    form = CarFilterForm(request.GET or None)
    form.is_valid()
    data = form.cleaned_data if form.is_bound else {}

    cars = search.find_cars(
        term=data.get("q") or None,
        status=data.get("status") or None,
        fuel_type=data.get("fuel_type") or None,
        brand=data.get("brand") or None,
    )
    return render(request, "staff/cars/list.html", {
        "title": "Cars",
        "form": form,
        "cars": cars,
        # So an empty lot can say "no cars yet" instead of blaming filters nobody set.
        "filtered": any(data.get(name) for name in ("q", "status", "fuel_type", "brand")),
    })


@staff_required
@requires("car.add")
def car_add(request):
    if request.method == "POST":
        form = CarForm(request.POST, request.FILES)
        imageset = ImageFormSet(request.POST, request.FILES, prefix="images")
        videoset = VideoFormSet(request.POST, request.FILES, prefix="videos")
        specset = SpecFormSet(request.POST, prefix="specs")
        if (form.is_valid() and imageset.is_valid() and videoset.is_valid()
                and specset.is_valid()):
            return _create(request, form, imageset, videoset, specset)
    else:
        form = CarForm()
        imageset = ImageFormSet(prefix="images")
        videoset = VideoFormSet(prefix="videos")
        specset = SpecFormSet(prefix="specs")

    return render(request, "staff/cars/form.html", {
        "title": "Add a car",
        "form": form,
        "car": None,
        "images": [],
        "gallery": [],
        "image_formset": imageset,
        "video_formset": videoset,
        "spec_formset": specset,
    })


def _create(request, form, imageset, videoset, specset):
    """Write the car, then hang its media off it. Strictly in that order.

    `images.create` writes an `IMG#` row into the car's partition and then calls
    `refresh_primary`, which GETs the META item -- so a photo written before the car
    exists is a `NotFound`, and a photo written before the guards are checked is an
    orphan child in a partition that never gets a car.

    The upload itself does not care when it happens. `uploads.py` keys objects by a
    fresh uuid rather than by car id, so the bytes are already in the bucket before this
    view is entered, which is why photos can be chosen on the add page at all.

    Known and accepted: if `_apply_images` throws after `cars.create` returned, the car
    exists with no photos and the page 500s. That is already true of `_save` on the edit
    page; this spreads it rather than inventing it.
    """
    car = Car(car_id=keys.new_id())
    for name, value in _field_values(form).items():
        setattr(car, name, value)

    def refused():
        # The BOUND formsets, for the reason `_save.invalid()` gives one screen down --
        # and here it is not only about retyping. The hidden `image_key` fields point at
        # objects already sitting in S3, so throwing them away makes somebody re-upload
        # a walkaround video because a chassis number was taken, and leaves the first
        # copy in the bucket with nothing referring to it.
        return render(request, "staff/cars/form.html", {
            "title": "Add a car", "form": form, "car": None, "images": [],
            "gallery": [],
            "image_formset": imageset,
            "video_formset": videoset,
            "spec_formset": specset,
        })

    try:
        car.specs = spec_rules.clean(_spec_rows(specset))
    except spec_rules.TooManySpecs as exc:
        form.add_error(None, str(exc))
        return refused()

    now = timezone.now()
    try:
        car = car_store.create(car, now=now)
    except ConditionFailed as exc:
        # No field to hang it on any more. With the chassis off the form the only thing
        # `create` can refuse is the slug, which nothing on the page caused and nothing
        # on the page fixes.
        form.add_error(None, str(exc))
        return refused()

    # Not `_apply_existing_media`: there is nothing existing to reorder, the card-photo
    # radio is not on this page, and `images.create` refreshes the primary itself.
    # Starts at 0 rather than asking `next_order`: `cars.create` returned three lines
    # up, so the partition holds a car and nothing else.
    position = itertools.count()
    added = _apply_images(car, imageset, now, position)
    clips = _apply_videos(car, videoset, now, position)

    parts = [f"Added {car.seo_title_plain}."]
    if added:
        parts.append(f"Added {added} photo(s).")
    if clips:
        parts.append(f"Added {clips} video(s).")
    if not (added or clips):
        parts.append("Now add its photos and videos.")
    cdn.invalidate(cdn.paths_for_car(car))
    messages.success(request, " ".join(parts))
    return redirect(reverse("staff:car-edit", args=[car.car_id]))


@staff_required
@requires("car.change")
def car_edit(request, car_id):
    try:
        car = car_store.detail(car_id)
    except NotFound:
        raise Http404("No such car")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            return _delete(request, car)
        if action == "rebuild-derivatives":
            return _rebuild(request, car)
        return _save(request, car)

    return render(request, "staff/cars/form.html", {
        "title": car.seo_title_plain,
        "form": CarForm(initial=_initial(car)),
        "car": car,
        "images": car.images,
        "gallery": car.media,
        "image_formset": ImageFormSet(prefix="images"),
        "video_formset": VideoFormSet(prefix="videos"),
        "spec_formset": SpecFormSet(initial=car.specs or [], prefix="specs"),
    })


def _initial(car):
    initial = {name: getattr(car, name) for name in EDITABLE}
    return initial


def _save(request, car):
    form = CarForm(request.POST, request.FILES)
    formset = ImageFormSet(request.POST, request.FILES, prefix="images")
    videoset = VideoFormSet(request.POST, request.FILES, prefix="videos")
    specset = SpecFormSet(request.POST, prefix="specs")

    def invalid():
        # The bound formsets go back into the template, so a rejected save re-renders
        # what was typed. Rebuilding them from `car` would throw it all away -- which
        # is what a chassis collision would do, and that is the one case where somebody
        # has just filled the whole page in.
        return render(request, "staff/cars/form.html", {
            "title": car.seo_title_plain, "form": form, "car": car,
            "images": car.images, "gallery": car.media,
            "image_formset": formset, "video_formset": videoset,
            "spec_formset": specset,
        })

    if not (form.is_valid() and formset.is_valid() and videoset.is_valid()
            and specset.is_valid()):
        return invalid()

    now = timezone.now()
    fields = _field_values(form)

    try:
        fields["specs"] = spec_rules.clean(_spec_rows(specset))
    except spec_rules.TooManySpecs as exc:
        form.add_error(None, str(exc))
        return invalid()

    try:
        car_store.update(car, now=now, **fields)
    except ConditionFailed as exc:
        form.add_error(None, str(exc))
        return invalid()

    # Read before the writes, or it counts the rows it is about to add. New media then
    # sorts past everything already there, which is what keeps it at the end when
    # `_apply_existing_media` renumbers the whole gallery a moment later.
    position = itertools.count(media_store.next_order(car.car_id))
    added = _apply_images(car, formset, now, position)
    clips = _apply_videos(car, videoset, now, position)
    _apply_existing_media(request, car)

    parts = ["Saved."]
    if added:
        parts.append(f"Added {added} photo(s).")
    if clips:
        parts.append(f"Added {clips} video(s).")
    # After every write above, so the edge refetches the car as it is now. This is
    # what makes a status or price change show on the public page in seconds rather
    # than in the cache policy's fifteen minutes.
    cdn.invalidate(cdn.paths_for_car(car))
    messages.success(request, " ".join(parts))
    return redirect(reverse("staff:car-edit", args=[car.car_id]))


def _apply_images(car, formset, now, position):
    """Create whatever new photo rows the formset carries.

    `position` is a shared counter, not a per-type one. Photos and videos live in one
    sequence over one number space, so giving each type its own count would have them
    collide and let the `IMG#` before `VID#` tiebreak in `store/media.py` pick the
    leader instead of the person who uploaded them.
    """
    added = 0
    for form in formset:
        if form in formset.deleted_forms:
            continue
        name = form.uploaded_name("image") or _store_upload(
            form.cleaned_data.get("image"), "cars/")
        if not name:
            continue
        image = image_store.create(
            car_id=car.car_id,
            image_name=name,
            is_primary=bool(form.cleaned_data.get("is_primary")),
            order=next(position),
            now=now,
        )
        # Queue the resized copies. The ORM did this from CarImage.save(); the store
        # deliberately does not, because a storage layer that starts work is a storage
        # layer with opinions. Asking here keeps it visible at the call site.
        build_derivatives_task(f"{car.car_id}:{image.image_id}")
        added += 1
    return added


def _apply_videos(car, formset, now, position):
    """Create whatever new video rows the formset carries.

    No `build_derivatives_task` twin, because nothing resizes or transcodes a video --
    which is why this is shorter than `_apply_images` rather than a copy of it.
    """
    added = 0
    for form in formset:
        if form in formset.deleted_forms:
            continue
        name = form.uploaded_name("video") or _store_upload(
            form.cleaned_data.get("video"), "cars/video/")
        if not name:
            continue
        video_store.create(
            car_id=car.car_id,
            video_name=name,
            order=next(position),
            now=now,
        )
        added += 1
    return added


#: `image-<id>-*` and `video-<id>-*`, matching the kind names `media.py` uses.
_FIELD_PREFIX = {media_store.PHOTO: "image", media_store.VIDEO: "video"}


def _moved(entries, move):
    """One neighbour swap, from a `move` button: "up:image:<id>" or "down:video:<id>".

    The swap is over the merged gallery rather than within a type, so Up on the first
    photo below a video swaps it with the video -- photos and videos are one sequence,
    which is the whole reason `store/media.py` exists.

    Every way of being wrong is a no-op rather than an error: a row deleted earlier in
    this same POST is no longer in the list, and a move off either end has nowhere to
    go. Neither is worth an error message to somebody who just pressed an arrow.
    """
    direction, _, rest = move.partition(":")
    kind, _, item_id = rest.partition(":")
    if direction not in ("up", "down") or not item_id:
        return entries
    kind = media_store.VIDEO if kind == "video" else media_store.PHOTO
    try:
        here = entries.index((kind, item_id))
    except ValueError:
        return entries
    there = here - 1 if direction == "up" else here + 1
    if not 0 <= there < len(entries):
        return entries
    entries[here], entries[there] = entries[there], entries[here]
    return entries


def _apply_existing_media(request, car):
    """Reorder and delete the photos and videos already attached, and set the card photo.

    Sent as `<kind>-<id>-delete` / `primary` / a `move` button rather than through more
    formsets: these rows are edited in place next to their thumbnails, and a formset
    would put the fields somewhere else on the page. The photo field names are unchanged
    from when this handled photos alone.

    Deletions run first and ordering second, over what survives. The other way round
    renumbers rows that are about to disappear, which leaves gaps in the sequence --
    harmless to sorting, confusing in the boxes staff see on the next page load.
    """
    for image in image_store.for_car(car.car_id):
        if request.POST.get(f"image-{image.image_id}-delete"):
            image_store.delete(car.car_id, image.image_id)
    for video in video_store.for_car(car.car_id):
        if request.POST.get(f"video-{video.video_id}-delete"):
            video_store.delete(car.car_id, video.video_id)

    # Ordering goes through `media.set_order` rather than writing each row, because a
    # move may take a photo past a video and the two share one number space. It runs on
    # every save whether or not anything moved, which is also what closes the gaps a
    # deletion leaves.
    current = media_store.gallery(car.car_id)
    entries = [(kind, item.video_id if kind == media_store.VIDEO else item.image_id)
               for kind, item in current]
    media_store.set_order(car.car_id, _moved(entries, request.POST.get("move") or ""))

    primary = request.POST.get("primary") or ""
    if primary:
        try:
            image_store.set_primary(car.car_id, primary)
        except NotFound:
            pass
    else:
        image_store.refresh_primary(car.car_id)


def _store_upload(uploaded, folder):
    """Fallback path: the browser posted the bytes through Django after all.

    Only reached when the direct-upload JavaScript did not run. Subject to Lambda's
    request ceiling, which is exactly what the direct upload exists to avoid.
    """
    if not uploaded:
        return ""
    return default_storage.save(f"{folder}{uploaded.name}", uploaded)


@requires("car.change")
def _rebuild(request, car):
    """Manual repair for photos that never finished processing."""
    rebuilt = failed = 0
    for image in image_store.for_car(car.car_id):
        if not image.image_name:
            continue
        try:
            build_derivatives(image, widths=DERIVATIVE_WIDTHS)
            rebuilt += 1
        except Exception:  # noqa: BLE001 - reported, never raised at the user
            failed += 1
    if rebuilt:
        messages.success(request, f"Rebuilt {rebuilt} photo(s).")
    if failed:
        messages.error(request, f"{failed} photo(s) failed.")
    return redirect(reverse("staff:car-edit", args=[car.car_id]))


@requires("car.delete")
def _delete(request, car):
    """Remove the car, its guards, its photos, and the files behind them.

    Django left files on disk by default and `models.py` hung a `post_delete` receiver
    off CarImage to clean them up. There are no signals here, so the S3 cleanup is
    explicit - and doing it here rather than in the store keeps the store free of
    opinions about whether a caller wanted the bytes gone.
    """
    label = car.seo_title_plain
    for image in image_store.for_car(car.car_id):
        image_store.delete(car.car_id, image.image_id)
    for video in video_store.for_car(car.car_id):
        video_store.delete(car.car_id, video.video_id)

    car_store.delete(car)
    cdn.invalidate(cdn.paths_for_car(car))
    messages.success(request, f"Deleted {label}.")
    return redirect(reverse("staff:car-list"))
