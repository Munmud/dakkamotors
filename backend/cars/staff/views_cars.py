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
from ..forms import CarFilterForm, CarForm, CarImageForm
from ..images import build_derivatives
from ..tasks import build_derivatives_task
from ..store import cars as car_store
from ..store import images as image_store
from ..store import keys
from ..store import search
from ..store.errors import ConditionFailed, NotFound
from ..store.models import Car
from .auth import requires, staff_required

ImageFormSet = formset_factory(CarImageForm, extra=3, can_delete=True)

#: Fields the form owns. Kept explicit so a new form field cannot silently start
#: writing something the store did not expect.
EDITABLE = (
    "brand", "model_name", "grade", "model_code", "chassis_number",
    "manufacture_year", "fuel_type", "seat_capacity", "color", "price_jpy",
    "status", "description_en", "description_ja",
)


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
        if form.is_valid():
            return _create(request, form)
    else:
        form = CarForm()

    return render(request, "staff/cars/form.html", {
        "title": "Add a car",
        "form": form,
        "car": None,
        "images": [],
        "image_formset": ImageFormSet(prefix="images"),
    })


def _create(request, form):
    car = Car(car_id=keys.new_id())
    for name in EDITABLE:
        setattr(car, name, form.cleaned_data.get(name))
    video = form.uploaded_name("video") or _store_upload(form.cleaned_data.get("video"),
                                                         "cars/video/")
    if video:
        car.video_name = video
        car.video_uploaded_at = timezone.now()

    try:
        car = car_store.create(car, now=timezone.now())
    except ConditionFailed as exc:
        form.add_error("chassis_number", str(exc))
        return render(request, "staff/cars/form.html", {
            "title": "Add a car", "form": form, "car": None, "images": [],
            "image_formset": ImageFormSet(prefix="images"),
        })

    messages.success(request, f"Added {car.seo_title_plain}. Now add its photos.")
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
        "image_formset": ImageFormSet(prefix="images"),
    })


def _initial(car):
    initial = {name: getattr(car, name) for name in EDITABLE}
    return initial


def _save(request, car):
    form = CarForm(request.POST, request.FILES)
    formset = ImageFormSet(request.POST, request.FILES, prefix="images")

    if not (form.is_valid() and formset.is_valid()):
        return render(request, "staff/cars/form.html", {
            "title": car.seo_title_plain, "form": form, "car": car,
            "images": car.images, "image_formset": formset,
        })

    now = timezone.now()
    fields = {name: form.cleaned_data.get(name) for name in EDITABLE}

    video = form.uploaded_name("video") or _store_upload(form.cleaned_data.get("video"),
                                                         "cars/video/")
    if video:
        fields["video_name"] = video
        # Stamped only when the file actually changes, matching the old save_model.
        fields["video_uploaded_at"] = now

    try:
        car_store.update(car, now=now, **fields)
    except ConditionFailed as exc:
        form.add_error("chassis_number", str(exc))
        return render(request, "staff/cars/form.html", {
            "title": car.seo_title_plain, "form": form, "car": car,
            "images": car.images, "image_formset": formset,
        })

    added = _apply_images(car, formset, now)
    _apply_existing_images(request, car)

    messages.success(request, "Saved." + (f" Added {added} photo(s)." if added else ""))
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


def _apply_existing_images(request, car):
    """Reorder, re-flag and delete the photos already attached.

    Sent as `image-<id>-order` / `image-<id>-delete` / `primary` rather than through a
    second formset: these rows are edited in place next to their thumbnails, and a
    formset would put the fields somewhere else on the page.
    """
    primary = request.POST.get("primary") or ""
    for image in image_store.for_car(car.car_id):
        if request.POST.get(f"image-{image.image_id}-delete"):
            image_store.delete(car.car_id, image.image_id)
            continue
        raw = request.POST.get(f"image-{image.image_id}-order")
        if raw is not None and str(raw).strip().isdigit():
            new_order = int(raw)
            if new_order != int(image.order or 0):
                image.update(actions=[type(image).order.set(new_order)])

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
    if car.video_name:
        try:
            default_storage.delete(car.video_name)
        except Exception:  # noqa: BLE001 - cleanup must not break the delete
            pass

    car_store.delete(car)
    messages.success(request, f"Deleted {label}.")
    return redirect(reverse("staff:car-list"))
