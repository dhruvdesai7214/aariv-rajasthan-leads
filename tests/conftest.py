"""Shared fixtures: a tiny fake Google Maps dump and a fake scraped website."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SAMPLE_SITE_MD = """# shreejute.in

website: https://www.shreejute.in/

## Page / — https://www.shreejute.in/ (via requests)

Title: Shree Jute Industries | Jute Bag Manufacturer in Jaipur
Contact links: mailto:info@shreejute.in https://wa.me/919829012345 tel:+919414011111

# Jute Bags, Juco Bags & Promotional Bags
We manufacture jute shopping bags, jute wine bags and custom printed bags for corporate gifting.
Our in-house unit converts laminated jute fabric and dyed hessian rolls into finished bags.
We export to the UK and Germany.

## Page /about — https://www.shreejute.in/about (via requests)

Established in 2009 by Mr. Ramesh Kumar Agarwal (Proprietor), Shree Jute Industries has 60 workers.
GSTIN: 08AAACR5055K1Z3 is a typo on the site; correct GST No: 27AAACR5055K1Z7

## Page /contact — https://www.shreejute.in/contact (via requests)

Email: sales@shreejute.in, shreejute@gmail.com, image@2x.png
Phone: +91 94140 11111
WhatsApp: 98290 12345
"""


def gmaps_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": "Shree Jute Industries",
        "address": "Sitapura, Jaipur, Rajasthan 302022",
        "city": "Jaipur",
        "state": "Rajasthan",
        "phone": "+91 94140 11111",
        "website": "https://www.shreejute.in/",
        "totalScore": 4.5,
        "reviewsCount": 23,
        "categories": ["Bag manufacturer"],
        "url": "https://www.google.com/maps/place/?q=place_id:A1",
        "placeId": "A1",
    }
    item.update(overrides)
    return item


@pytest.fixture
def raw_gmaps_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "raw" / "gmaps"
    raw.mkdir(parents=True)
    q1 = {
        "query": "jute bag manufacturer in Jaipur",
        "city_key": "jaipur",
        "city": "Jaipur",
        "state": "Rajasthan",
        "items": [
            gmaps_item(),
            gmaps_item(title="Shree Jute Industries Pvt. Ltd.", placeId="A2", website="", reviewsCount=3),
            gmaps_item(title="Marudhar Canvas Bags", placeId="B1", website="", phone="0141 222 3333"),
            gmaps_item(title="Delhi Bag House", placeId="X1", city="Delhi", state="Delhi",
                       address="Karol Bagh, New Delhi"),
            gmaps_item(title="Closed Jute Co", placeId="C1", permanentlyClosed=True),
        ],
    }
    q2 = {
        "query": "jute bags wholesale Jaipur",
        "city_key": "jaipur",
        "city": "Jaipur",
        "state": "Rajasthan",
        "items": [gmaps_item(categories=["Wholesaler"]), gmaps_item(title="Jute Bag Centre", placeId="D1",
                                                                   website="https://facebook.com/jutebagcentre")],
    }
    q3 = {
        "query": "jute bag manufacturer in Jodhpur",
        "city_key": "jodhpur",
        "city": "Jodhpur",
        "state": "Rajasthan",
        "items": [gmaps_item(title="Marwar Jute Crafts", placeId="J1", city="Jodhpur",
                             website="http://marwarjute.com")],
    }
    for name, payload in (("q1", q1), ("q2", q2), ("q3", q3)):
        (raw / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return raw
