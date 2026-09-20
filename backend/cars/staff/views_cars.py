"""Inventory. Replaces `CarAdmin` and its `CarImageInline`.

The largest of the staff pages, because the admin was doing the most for free here: a
changelist with filters and a substring search, a change form with five field groups, an
inline formset for photos, and the direct-to-S3 upload that sidesteps Lambda's ~4.5 MB
request ceiling.

The upload JavaScript carries over almost unchanged. It derives the hidden field's name
from `input.name` with `.replace(/image$/, "image_key")`, and a plain `formset_factory`
names fields `form-0-image`, which that regex still matches.
"""

from django.contrib import messages
from django.core.files.storage import default_storage
from django.forms import formset_factory
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

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

ImageFormSet = formset_factory(CarImageForm, extra=3, can_delete=True)
#: One extra row, not three: a video is a rare addition next to a batch of photos, and
#: three empty file inputs for it made the panel read as though videos were expected.
VideoFormSet = formset_factory(CarVideoForm, extra=1, can_delete=True)
#: Specs appear on the *add* page as well as the edit page, unlike photos and videos.
#: They are attributes of the car item, so they land in the same conditional write as
#: the chassis guard -- there is no window in which one exists without the other. A
#: child row written before a failed guard would leave an orphan in a partition with
#: no META item.
SpecFormSet = formset_factory(CarSpecForm, extra=3, can_delete=True)


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
    "brand", "model_name", "grade", "model_code", "chassis_number",
    "manufacture_year", "fuel_type", "seat_capacity", "color", "price_jpy",
    "status", "description_en", "description_ja",
)

#: Blank means absent here, not empty.
#:
#: `CharField(required=False)` hands back `""`, and the store's `if car.chassis_number:`
#: guards read that correctly -- but it would leave an empty attribute sitting on the
#: item, and `store.cars.update` compares `car.chassis_number != old_chassis` to decide
#: whether the chassis moved. With `""` on one side and `None` on the other that is true
#: on *every* save of a chassis-less car, sending an update that changes nothing down
#: the transaction path instead of the plain `car.save()` beside it.
#:
#: Same rule `price_jpy` already follows, where absent and zero are different answers.
BLANK_IS_ABSENT = ("chassis_number",)


def _field_values(form):
    """The form's values, with blanks that mean "not given" turned back into None."""
    values = {name: form.cleaned_data.get(name) for name in EDITABLE}
    for name in BLANK_IS_ABSENT:
        if not (values.get(name) or "").strip():
            values[name] = None
    return values


def _blame_chassis(form, exc):
    """Attach a refused conditional write to the field that can actually fix it.

    `cars.create` raises `ConditionFailed` for a taken chassis number and for running
    out of slug candidates, and this used to hang both on `chassis_number`. Now that the
    field can legitimately be empty, pointing a failure at an empty box says nothing --
    so it only gets the blame when there is something in it to be wrong.
    """
    if form.cleaned_data.get("chassis_number"):
        form.add_error("chassis_number", str(exc))
    else:
        form.add_error(None, str(exc))


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
    })


@staff_required
@requires("car.add")
def car_add(request):
    if request.method == "POST":
        form = CarForm(request.POST, request.FILES)
        specset = SpecFormSet(request.POST, prefix="specs")
        if form.is_valid() and specset.is_valid():
            return _create(request, form, specset)
    else:
        form = CarForm()
        specset = SpecFormSet(prefix="specs")

    return render(request, "staff/cars/form.html", {
        "title": "Add a car",
        "form": form,
        "car": None,
        "images": [],
        "gallery": [],
        "image_formset": ImageFormSet(prefix="images"),
        "video_formset": VideoFormSet(prefix="videos"),
        "spec_formset": specset,
    })


def _create(request, form, specset):
    car = Car(car_id=keys.new_id())
    for name, value in _field_values(form).items():
        setattr(car, name, value)

    def refused():
        return render(request, "staff/cars/form.html", {
            "title": "Add a car", "form": form, "car": None, "images": [],
            "gallery": [],
            "image_formset": ImageFormSet(prefix="images"),
            "video_formset": VideoFormSet(prefix="videos"),
            "spec_formset": specset,
        })

    try:
        car.specs = spec_rules.clean(_spec_rows(specset))
    except spec_rules.TooManySpecs as exc:
        form.add_error(None, str(exc))
        return refused()

    try:
        car = car_store.create(car, now=timezone.now())
    except ConditionFailed as exc:
        _blame_chassis(form, exc)
        return refused()

    messages.success(request, f"Added {car.seo_title_plain}. Now add its photos and videos.")
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
        _blame_chassis(form, exc)
        return invalid()

    added = _apply_images(car, formset, now)
    clips = _apply_videos(car, videoset, now)
    _apply_existing_media(request, car)

    parts = ["Saved."]
    if added:
        parts.append(f"Added {added} photo(s).")
    if clips:
        parts.append(f"Added {clips} video(s).")
    messages.success(request, " ".join(parts))
    return redirect(reverse("staff:car-edit", args=[car.car_id]))


def _apply_images(car, formset, now):
    """Create whatever new photo rows the formset carries."""
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
            order=form.cleaned_data.get("order") or 0,
            now=now,
        )
        # Queue the resized copies. The ORM did this from CarImage.save(); the store
        # deliberately does not, because a storage layer that starts work is a storage
        # layer with opinions. Asking here keeps it visible at the call site.
        build_derivatives_task(f"{car.car_id}:{image.image_id}")
        added += 1
    return added


def _apply_videos(car, formset, now):
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
            order=form.cleaned_data.get("order") or 0,
            now=now,
        )
        added += 1
    return added


#: `image-<id>-*` and `video-<id>-*`, matching the kind names `media.py` uses.
_FIELD_PREFIX = {media_store.PHOTO: "image", media_store.VIDEO: "video"}


def _apply_existing_media(request, car):
    """Reorder and delete the photos and videos already attached, and set the card photo.

    Sent as `<kind>-<id>-order` / `<kind>-<id>-delete` / `primary` rather than through
    more formsets: these rows are edited in place next to their thumbnails, and a
    formset would put the fields somewhere else on the page. The photo field names are
    unchanged from when this handled photos alone.

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

    # Ordering goes through `media.set_order` rather than writing each row, because one
    # position may now move a photo past a video. The current sequence is the tiebreak,
    # so a row whose box was left blank keeps its place instead of jumping to the front
    # the way treating a missing number as zero would send it.
    current = media_store.gallery(car.car_id)
    keyed = []
    for index, (kind, item) in enumerate(current):
        item_id = item.video_id if kind == media_store.VIDEO else item.image_id
        raw = request.POST.get(f"{_FIELD_PREFIX[kind]}-{item_id}-order")
        posted = int(raw) if raw is not None and str(raw).strip().isdigit() else index
        keyed.append(((posted, index), (kind, item_id)))
    keyed.sort(key=lambda pair: pair[0])
    media_store.set_order(car.car_id, [entry for _, entry in keyed])

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
    messages.success(request, f"Deleted {label}.")
    return redirect(reverse("staff:car-list"))
