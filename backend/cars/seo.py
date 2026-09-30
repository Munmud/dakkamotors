"""Business facts and the structured data built from them.

Everything a search engine or an AI assistant needs to know about the dealership lives
here, in one place, so it can be corrected without hunting through templates.

Structured data is the highest-leverage part of this file. Google uses it for rich
results, and answer engines (ChatGPT, Perplexity, Google's AI overviews) lean on it
heavily because it states facts unambiguously instead of making them infer from layout.
"""

import re

SITE_URL = "https://dakkamotors.com"

BUSINESS = {
    "name": "Dakka Motors",
    "name_ja": "ダッカモータース",
    "legal_country": "JP",
    "telephone": "+818092823601",
    "telephone_display": "080-9282-3601",
    "email": "",
    "street_address": "Shinmeidai 1-16-2, Shinmeidai Tempo C-1F",
    "street_address_ja": "神明台1丁目16-2 神明台店舗C-1F",
    "locality": "Hamura",
    "locality_ja": "羽村市",
    "region": "Tokyo",
    "region_ja": "東京都",
    "postal_code": "205-0023",
    # Left blank on purpose. Google geocodes the postal address accurately; publishing
    # approximate coordinates would place the shop somewhere it is not. Fill these in
    # from the Google Business Profile listing once it exists.
    "latitude": "",
    "longitude": "",
    # **No opening hours, and this is settled rather than outstanding.** The owner
    # has said there are none to publish -- it is a small lot and the phone is
    # answered when it is answered.
    #
    # There were some once: 06:00 to 24:00, every day, published in four places
    # before anybody confirmed them. A claim in structured data is a claim Google
    # repeats as fact, and "open until midnight" is the kind of fact that turns
    # somebody driving out at 21:00 into somebody who never rings again.
    #
    # The consequence is that nothing on the site may imply a time, including "we
    # will call you back today". `lead.doneBody` says "as soon as we can" for that
    # reason, and should keep saying something of that shape.
}

#: Where customers realistically come from. Hamura sits in western Tokyo, so the
#: neighbouring cities matter as much as the city itself for local search.
SERVICE_AREAS = [
    "Hamura", "Fussa", "Akiruno", "Ome", "Mizuho", "Musashimurayama",
    "Tachikawa", "Akishima", "Tokyo",
]

LANGUAGES = ("en", "ja")


def _postal_address():
    return {
        "@type": "PostalAddress",
        "streetAddress": BUSINESS["street_address"],
        "addressLocality": BUSINESS["locality"],
        "addressRegion": BUSINESS["region"],
        "postalCode": BUSINESS["postal_code"],
        "addressCountry": BUSINESS["legal_country"],
    }


def dealer_schema():
    """AutoDealer: who the business is and where it is.

    Emitted on every page. `AutoDealer` is a LocalBusiness subtype, so it carries the
    local-search signals while also telling an engine what the business actually sells.
    """
    data = {
        "@type": "AutoDealer",
        "@id": f"{SITE_URL}/#dealer",
        "name": BUSINESS["name"],
        "alternateName": BUSINESS["name_ja"],
        "url": SITE_URL,
        "telephone": BUSINESS["telephone"],
        "address": _postal_address(),
        "areaServed": [
            {"@type": "City", "name": area} for area in SERVICE_AREAS
        ],
        "currenciesAccepted": "JPY",
        "priceRange": "¥¥",
    }
    if BUSINESS["latitude"] and BUSINESS["longitude"]:
        data["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": BUSINESS["latitude"],
            "longitude": BUSINESS["longitude"],
        }
    return data


#: Spec labels that mean "how far has it been driven", in both languages, lowercased.
#: Free-form rows are the only place mileage lives -- there is no field for it, because
#: not every car has a figure the shop is willing to state.
_MILEAGE_LABELS = ("mileage", "odometer", "kilometres", "kilometers", "km",
                   "走行距離", "走行", "距離")

_MILEAGE_VALUE = re.compile(
    r"^\s*(?:約|approx\.?\s*)?([\d,]+)\s*(\+|以上)?\s*(?:km|KM|Km|キロ|km走行|kms)?\s*$")


def mileage_from(specs):
    """The odometer reading as a Schema.org QuantitativeValue, or None.

    Read out of the free-form specs because that is where staff type it, and read
    strictly: `215,000km` and `215000 km` are a reading, `215000+km` is a *floor* and
    becomes `minValue` rather than `value`, and anything else -- a range, a guess, a
    sentence -- yields nothing at all. The rule the rest of this module follows is that
    a wrong `mileageFromOdometer` is worse than an absent one, and a number Google
    shows in a search result beside the car is exactly the kind of wrong that matters.
    """
    for row in specs or []:
        labels = (row.get("label_en") or "", row.get("label_ja") or "")
        if not any(want in label.strip().lower()
                   for label in labels for want in _MILEAGE_LABELS):
            continue
        for value in (row.get("value_en") or "", row.get("value_ja") or ""):
            match = _MILEAGE_VALUE.match(value)
            if not match:
                continue
            km = int(match.group(1).replace(",", ""))
            if km <= 0:
                continue
            key = "minValue" if match.group(2) else "value"
            return {"@type": "QuantitativeValue", key: km, "unitCode": "KMT"}
    return None


def vehicle_schema(car, image_urls, canonical_url):
    """Car + Offer for one listing.

    `Car` is the specific Schema.org type for a road vehicle, and the one Google's
    vehicle-listing rich results read. Fields that are unknown are omitted rather than
    guessed - a wrong `mileageFromOdometer` is worse than an absent one.
    """
    offer = {
        "@type": "Offer",
        "url": canonical_url,
        "priceCurrency": "JPY",
        "availability": {
            "available": "https://schema.org/InStock",
            "reserved": "https://schema.org/PreOrder",
            "sold": "https://schema.org/SoldOut",
        }.get(car.status, "https://schema.org/InStock"),
        "seller": {"@id": f"{SITE_URL}/#dealer"},
        "itemCondition": "https://schema.org/UsedCondition",
    }
    if car.price_jpy is not None:
        offer["price"] = str(car.price_jpy)
        # The price is 支払総額 -- what the customer pays, with registration and
        # preparation in it -- not 車両本体価格 with the fees to follow. Saying so
        # here is not decoration: Google reads `priceSpecification` for the vehicle
        # rich result, and a shop whose listed price is the total is making a claim
        # its competitors mostly cannot. `valueAddedTaxIncluded` is the only field
        # schema.org has for it.
        offer["priceSpecification"] = {
            "@type": "PriceSpecification",
            "price": str(car.price_jpy),
            "priceCurrency": "JPY",
            "valueAddedTaxIncluded": True,
        }
    else:
        # Schema requires a price or a way to get one; point at the phone instead of
        # inventing a number.
        offer["priceSpecification"] = {
            "@type": "PriceSpecification",
            "priceCurrency": "JPY",
            "valueAddedTaxIncluded": True,
        }

    data = {
        "@type": "Car",
        "@id": f"{canonical_url}#vehicle",
        "name": car.seo_title_plain,
        "url": canonical_url,
        "brand": {"@type": "Brand", "name": car.brand},
        "model": car.model_name,
        "vehicleModelDate": str(car.manufacture_year),
        "productionDate": str(car.manufacture_year),
        "fuelType": car.get_fuel_type_display(),
        "seatingCapacity": car.seat_capacity,
        "itemCondition": "https://schema.org/UsedCondition",
        "offers": offer,
    }
    # Down here with the other optional facts rather than in the literal above. It was
    # unconditional while the staff form demanded one; now that a car can have no
    # chassis number, emitting it regardless would put a literal `null` into public
    # JSON-LD, which validators report as an error on the page rather than ignoring.
    if car.chassis_number:
        data["vehicleIdentificationNumber"] = car.chassis_number
    if car.color:
        data["color"] = car.color
    if car.grade:
        data["vehicleConfiguration"] = car.grade
    if car.model_code:
        data["vehicleEngine"] = {"@type": "EngineSpecification", "name": car.model_code}
    mileage = mileage_from(car.specs)
    if mileage:
        data["mileageFromOdometer"] = mileage
    if image_urls:
        data["image"] = image_urls
    description = (car.description_en or car.description_ja or "").strip()
    if description:
        data["description"] = description
    # Two omissions, recorded so they are not re-litigated:
    #
    # `car.specs` does NOT become `additionalProperty` -- `mileageFromOdometer` above
    # is read out of it, but that is one named field Google's vehicle result displays. It is valid Schema.org and
    # Google's vehicle rich result does not read it, so it would push staff-typed text
    # into the JSON-LD for no gain in any result anyone sees.
    #
    # A car's videos do NOT become `VideoObject`. That type needs `thumbnailUrl`,
    # `uploadDate` and `description` to be eligible for a video result, and nothing in
    # this system produces the first or the last -- an ineligible node is markup that
    # only costs bytes.
    return data


def breadcrumb_schema(trail):
    """trail: [(name, url), ...] ending at the current page."""
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": url}
            for i, (name, url) in enumerate(trail, start=1)
        ],
    }


def website_schema():
    return {
        "@type": "WebSite",
        "@id": f"{SITE_URL}/#website",
        "url": SITE_URL,
        "name": BUSINESS["name"],
        "inLanguage": ["en", "ja"],
        "publisher": {"@id": f"{SITE_URL}/#dealer"},
    }


def graph(*nodes):
    """Wrap nodes in a single @graph, which is how multiple entities share one block."""
    return {"@context": "https://schema.org", "@graph": [n for n in nodes if n]}
