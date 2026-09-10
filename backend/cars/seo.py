"""Business facts and the structured data built from them.

Everything a search engine or an AI assistant needs to know about the dealership lives
here, in one place, so it can be corrected without hunting through templates.

Structured data is the highest-leverage part of this file. Google uses it for rich
results, and answer engines (ChatGPT, Perplexity, Google's AI overviews) lean on it
heavily because it states facts unambiguously instead of making them infer from layout.
"""

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
    "opens": "06:00",
    "closes": "24:00",
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
    """AutoDealer: who the business is, where it is, and when it is open.

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
        "openingHoursSpecification": [
            {
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": [
                    "Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday",
                ],
                "opens": BUSINESS["opens"],
                "closes": BUSINESS["closes"],
            }
        ],
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
        "vehicleIdentificationNumber": car.chassis_number,
        "fuelType": car.get_fuel_type_display(),
        "seatingCapacity": car.seat_capacity,
        "itemCondition": "https://schema.org/UsedCondition",
        "offers": offer,
    }
    if car.color:
        data["color"] = car.color
    if car.grade:
        data["vehicleConfiguration"] = car.grade
    if car.model_code:
        data["vehicleEngine"] = {"@type": "EngineSpecification", "name": car.model_code}
    if image_urls:
        data["image"] = image_urls
    description = (car.description_en or car.description_ja or "").strip()
    if description:
        data["description"] = description
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
