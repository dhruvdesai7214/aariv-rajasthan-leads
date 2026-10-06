"""Step 4: extract sales fields from each scraped website.

Reads data/raw/websites/<site_key>.md and writes data/enriched/<site_key>.json.

Two extractors produce the same JSON shape (see EXTRACTION_SCHEMA):
  * rules (default, free): regex + keyword heuristics, with GSTIN checksum validation.
  * llm (--use-llm): Claude via the Anthropic API, using a strict tool schema.
    Needs ANTHROPIC_API_KEY and costs money per site.

Resumable: sites that already have an enriched JSON file are skipped unless --force.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

from src.common import (
    ENRICHED_DIR,
    RAW_WEBSITES_DIR,
    api_retry,
    host_of,
    require_env,
    setup_logging,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

LLM_MODEL = "claude-sonnet-5-5"
LLM_MAX_INPUT_CHARS = 120_000
CURRENT_YEAR = datetime.now().year
RAJASTHAN_GST_STATE_CODE = "08"

# Strict JSON schema shared by both extractors (and sent to Claude as the tool schema).
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "owner_name": {
            "type": ["string", "null"],
            "description": "Name of the owner / proprietor / founder / director, without honorifics. null if not stated.",
        },
        "emails": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Business email addresses found on the site, most relevant first.",
        },
        "whatsapp_number": {
            "type": ["string", "null"],
            "description": "WhatsApp number in +91XXXXXXXXXX form, only if explicitly marked as WhatsApp.",
        },
        "gstin": {
            "type": ["string", "null"],
            "description": "15-character GSTIN if printed on the site.",
        },
        "year_established": {
            "type": ["integer", "null"],
            "description": "Year the business was established, if stated.",
        },
        "product_mix": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short product category labels, e.g. 'jute bags', 'canvas bags', 'promotional bags'.",
        },
        "mentions_jute_roll_input": {
            "type": "boolean",
            "description": "True if the site mentions jute fabric / hessian / jute rolls / laminated or dyed jute as material they use.",
        },
        "size_signal": {
            "type": "string",
            "enum": ["small", "mid", "large"],
            "description": "Rough company size from employees, capacity, exports, legal form.",
        },
        "notes_for_sales": {
            "type": "string",
            "description": "1-3 sentences a salesperson selling dyed/laminated jute rolls can use in a cold call.",
        },
    },
    "required": [
        "owner_name",
        "emails",
        "whatsapp_number",
        "gstin",
        "year_established",
        "product_mix",
        "mentions_jute_roll_input",
        "size_signal",
        "notes_for_sales",
    ],
    "additionalProperties": False,
}

# --------------------------------------------------------------------------- #
# Rule-based extractor
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?![\w-])")
_EMAIL_JUNK = re.compile(
    r"(\.(png|jpe?g|gif|webp|svg|css|js)$)|example\.|sentry|wixpress|domain\.com|yourdomain|"
    r"youremail|email\.com$|@sentry|godaddy|w3\.org|schema\.org|\bname@|user@",
    re.I,
)
_FREE_MAIL = ("gmail.com", "yahoo.", "rediffmail.com", "hotmail.com", "outlook.com")

MOBILE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")
WA_LINK_RE = re.compile(r"(?:wa\.me/|whatsapp\.com/send/?\?phone=)\+?(\d{10,13})", re.I)
WA_TEXT_RE = re.compile(r"whats\s*app[^\n\d+]{0,40}((?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5})", re.I)

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

YEAR_RE = re.compile(
    r"(?:since|established|estd\.?|est\.|founded|incorporated|inception|started)\D{0,30}?((?:19[4-9]|20[0-3])\d)\b",
    re.I,
)

_HONORIFIC = r"(?:mr\.?|mrs\.?|ms\.?|shri|smt\.?|sri|dr\.?)"
_ROLE = r"(?:proprietor|founder|co-founder|owner|managing director|director|ceo|partner|chairman|md)"
_NAME = r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})"
OWNER_PATTERNS: list[re.Pattern[str]] = [
    # "Proprietor: Mr. Ramesh Kumar Sharma" / "Founder - Ramesh Sharma"
    re.compile(rf"(?i:{_ROLE})\s*[:\-–—,]?\s*(?i:{_HONORIFIC}\s+)?{_NAME}"),
    # "Mr. Ramesh Sharma (Founder)" / "Ramesh Sharma, Proprietor"
    re.compile(rf"(?:(?i:{_HONORIFIC})\s+)?{_NAME}\s*[,(\-–—|]\s*(?i:{_ROLE})\b"),
    # "CEO Name Ramesh Sharma" (IndiaMART-style fact tables)
    re.compile(rf"(?i:(?:ceo|owner|proprietor)\s+name)\s*[:|]?\s*(?i:{_HONORIFIC}\s+)?{_NAME}"),
]
_NOT_NAME_WORDS = {
    "jute", "bag", "bags", "pvt", "ltd", "private", "limited", "the", "our", "team", "company",
    "india", "products", "product", "contact", "about", "message", "read", "more", "us", "home",
    "cotton", "canvas", "promotional", "manufacturer", "manufacturers", "exporter", "exporters",
    "industries", "enterprises", "fabrics", "and", "of", "for", "with", "rajasthan", "jaipur",
    "jodhpur", "kishangarh", "udaipur", "ajmer", "bikaner", "nature", "business", "director",
    "managing", "quality", "since", "group", "years", "experience", "view", "profile", "details",
}

# Label -> keywords (matched case-insensitively on word boundaries).
PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "jute bags": ["jute bag", "jute shopping bag", "jute tote", "jute wine bag", "jute lunch bag",
                  "jute pouch", "jute shopper", "jute carry bag"],
    "cotton bags": ["cotton bag", "calico bag", "cotton tote", "cotton shopping bag"],
    "canvas bags": ["canvas bag", "canvas tote", "canvas shopping bag"],
    "juco bags": ["juco"],
    "promotional bags": ["promotional bag", "corporate gift", "custom printed bag", "conference bag",
                         "logo printed", "branded bag", "promotional jute"],
    "non-woven bags": ["non woven", "non-woven"],
    "paper bags": ["paper bag"],
    "jute fabric / rolls": ["jute fabric", "jute roll", "hessian", "burlap", "jute cloth"],
    "laminated jute": ["laminated jute", "jute lamination", "pp laminated", "laminated bag"],
    "jute home decor": ["jute rug", "jute basket", "jute decor", "jute carpet", "jute mat"],
    "jute sacks": ["gunny", "jute sack", "sacking"],
}

JUTE_ROLL_INPUT_KEYWORDS: list[str] = [
    "jute fabric", "jute roll", "jute cloth", "hessian", "burlap", "laminated jute",
    "dyed jute", "jute lamination", "raw jute", "jute yarn", "juco fabric",
]

EMPLOYEES_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+)\s*\+?\s*(?:employees|workers|staff|artisans|craftsmen|people|team members|karigars)",
    re.I,
)
# IndiaMART-style "Total Number of Employees 11 to 25 People"
EMPLOYEE_RANGE_RE = re.compile(r"number of employees\D{0,20}(\d+)\s*(?:to|-)\s*(\d+)", re.I)
TURNOVER_RE = re.compile(r"(?:annual\s+)?turnover\D{0,30}(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*-?\s*(\d+)?\s*(lakh|lac|crore|cr)", re.I)


def _kw_present(text_lower: str, keyword: str) -> bool:
    return re.search(rf"\b{re.escape(keyword)}", text_lower) is not None


def gstin_is_valid(gstin: str) -> bool:
    """Validate a GSTIN's format and its mod-36 check character."""
    gstin = gstin.strip().upper()
    if not GSTIN_RE.fullmatch(gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        product = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    check = (36 - total % 36) % 36
    return _GSTIN_CHARS[check] == gstin[14]


def normalize_indian_mobile(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def extract_emails(text: str, site_domain: str = "") -> list[str]:
    found: list[str] = []
    for match in EMAIL_RE.findall(text.replace("mailto:", " ")):
        email = match.strip(".").lower()
        if _EMAIL_JUNK.search(email) or email in found:
            continue
        found.append(email)
    domain = site_domain.lower().removeprefix("www.")

    def rank(email: str) -> tuple[int, int]:
        """Own-domain first, then other business domains, then free webmail; role inboxes first."""
        local, host = email.split("@", 1)
        if domain and (host == domain or host.endswith("." + domain)):
            origin = 0
        elif host.startswith(_FREE_MAIL):
            origin = 2
        else:
            origin = 1
        role = 0 if local in ("info", "sales", "contact", "enquiry", "inquiry") else 1
        return origin, role

    return sorted(found, key=rank)


def extract_whatsapp(text: str) -> str | None:
    for pattern in (WA_LINK_RE, WA_TEXT_RE):
        for match in pattern.finditer(text):
            number = normalize_indian_mobile(match.group(1))
            if number:
                return number
    return None


def extract_mobiles(text: str) -> list[str]:
    out: list[str] = []
    for a, b in MOBILE_RE.findall(text):
        number = "+91" + a + b
        if number not in out:
            out.append(number)
    return out


def extract_gstin(text: str) -> str | None:
    candidates = GSTIN_RE.findall(text.upper())
    valid = [g for g in dict.fromkeys(candidates) if gstin_is_valid(g)]
    # Prefer a Rajasthan registration if the site lists several.
    valid.sort(key=lambda g: g[:2] != RAJASTHAN_GST_STATE_CODE)
    return valid[0] if valid else None


def extract_year(text: str) -> int | None:
    years = [int(y) for y in YEAR_RE.findall(text) if 1940 <= int(y) <= CURRENT_YEAR]
    return min(years) if years else None


def extract_owner(text: str) -> str | None:
    for pattern in OWNER_PATTERNS:
        for match in pattern.finditer(text):
            name = re.sub(r"\s+", " ", match.group(1)).strip()
            words = name.lower().split()
            if any(w in _NOT_NAME_WORDS for w in words):
                continue
            return name
    return None


def extract_products(text_lower: str) -> list[str]:
    return [
        label
        for label, keywords in PRODUCT_KEYWORDS.items()
        if any(_kw_present(text_lower, kw) for kw in keywords)
    ]


def mentions_jute_roll(text_lower: str) -> bool:
    return any(_kw_present(text_lower, kw) for kw in JUTE_ROLL_INPUT_KEYWORDS)


def estimate_size(text: str) -> tuple[str, list[str]]:
    """Heuristic small/mid/large with the evidence used."""
    lower = text.lower()
    evidence: list[str] = []
    employees: int | None = None
    range_match = EMPLOYEE_RANGE_RE.search(text)
    if range_match:
        employees = int(range_match.group(2))
        evidence.append(f"{range_match.group(1)}-{range_match.group(2)} employees")
    else:
        counts = [int(m.replace(",", "")) for m in EMPLOYEES_RE.findall(text)]
        counts = [c for c in counts if 2 <= c <= 50_000]
        if counts:
            employees = max(counts)
            evidence.append(f"{employees}+ workers/employees")

    score = 0
    if re.search(r"\bexport(s|ers?|ing)?\b", lower):
        score += 1
        evidence.append("exports")
    if re.search(r"private limited|pvt\.? ?ltd|\blimited\b", lower):
        score += 1
        evidence.append("company (Pvt Ltd/Ltd)")
    if re.search(r"\biso\s?\d{4,5}|sedex|bsci|oeko|gots\b", lower):
        score += 1
        evidence.append("certifications")
    if re.search(r"(lakh|lac|million|crore)\s+(?:pcs|pieces|bags|units)", lower):
        score += 1
        evidence.append("large capacity")
    turnover = TURNOVER_RE.search(text)
    if turnover:
        high = float(turnover.group(2) or turnover.group(1))
        unit = turnover.group(3).lower()
        crores = high if unit in ("crore", "cr") else high / 100
        evidence.append(f"turnover up to ~{crores:g} Cr")
        score += 2 if crores >= 25 else (1 if crores >= 5 else 0)

    if (employees is not None and employees >= 250) or score >= 4:
        return "large", evidence
    if (employees is not None and employees >= 50) or score >= 2:
        return "mid", evidence
    return "small", evidence


def build_sales_notes(
    products: list[str],
    jute_roll: bool,
    gstin: str | None,
    year: int | None,
    size: str,
    size_evidence: list[str],
    mobiles: list[str],
    whatsapp: str | None,
) -> str:
    notes: list[str] = []
    if products:
        notes.append("Makes " + ", ".join(products) + ".")
    if jute_roll:
        notes.append("Site mentions jute fabric/hessian/rolls as material, so a likely buyer of dyed/laminated jute rolls.")
    elif any(p in products for p in ("jute bags", "juco bags", "promotional bags", "canvas bags")):
        notes.append("Makes bags but doesn't name its fabric supplier; ask about current jute roll sourcing.")
    if gstin:
        state = "Rajasthan" if gstin.startswith(RAJASTHAN_GST_STATE_CODE) else f"state code {gstin[:2]}"
        notes.append(f"GST registered ({state}).")
    if year:
        notes.append(f"Established {year}.")
    notes.append(f"Size: {size}" + (f" ({', '.join(size_evidence)})." if size_evidence else "."))
    extra_mobiles = [m for m in mobiles if m != whatsapp][:3]
    if extra_mobiles:
        notes.append("Mobiles on site: " + ", ".join(extra_mobiles) + ".")
    return " ".join(notes)


def extract_rules(markdown: str, site_domain: str = "") -> dict[str, Any]:
    """Free, deterministic extraction from scraped markdown."""
    lower = markdown.lower()
    products = extract_products(lower)
    jute_roll = mentions_jute_roll(lower)
    gstin = extract_gstin(markdown)
    year = extract_year(markdown)
    whatsapp = extract_whatsapp(markdown)
    mobiles = extract_mobiles(markdown)
    size, size_evidence = estimate_size(markdown)
    return {
        "owner_name": extract_owner(markdown),
        "emails": extract_emails(markdown, site_domain),
        "whatsapp_number": whatsapp,
        "gstin": gstin,
        "year_established": year,
        "product_mix": products,
        "mentions_jute_roll_input": jute_roll,
        "size_signal": size,
        "notes_for_sales": build_sales_notes(
            products, jute_roll, gstin, year, size, size_evidence, mobiles, whatsapp
        ),
    }


# --------------------------------------------------------------------------- #
# LLM extractor (optional, paid)
# --------------------------------------------------------------------------- #

LLM_SYSTEM_PROMPT = """You extract B2B sales intelligence for Aariv Fabrics, an Ahmedabad supplier of dyed and \
laminated jute rolls (fabric). Their customers are small and mid-sized bag manufacturers in Rajasthan that \
buy jute fabric as raw material for jute, juco, canvas and promotional bags.

You will be given the scraped text of one company's website. Call the record_company tool exactly once with \
what the website states. Only use facts present in the text; use null or empty lists when something isn't \
stated. Do not guess phone numbers or emails. Normalize WhatsApp numbers to +91XXXXXXXXXX and only fill it \
when the number is explicitly marked as WhatsApp (text or wa.me link). notes_for_sales should be 1-3 concrete \
sentences a salesperson can use in a cold call (what they make, signs they buy jute fabric, size, anything \
notable such as exports or certifications)."""

LLM_TOOL: dict[str, Any] = {
    "name": "record_company",
    "description": "Record the structured sales fields extracted from the company's website.",
    "input_schema": EXTRACTION_SCHEMA,
    "strict": True,
}


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    import anthropic

    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in (408, 409, 429, 529)


class LLMExtractor:
    """Claude-based extractor using a strict tool schema."""

    def __init__(self, model: str = LLM_MODEL) -> None:
        import anthropic

        # Retries are handled by tenacity below, so turn off the SDK's own retries.
        self._client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"), max_retries=0)
        self.model = model

    def __call__(self, markdown: str, site_domain: str = "") -> dict[str, Any]:
        if len(markdown) > LLM_MAX_INPUT_CHARS:
            logger.warning(
                "%s: %d chars of website text; sending the first %d to the model",
                site_domain,
                len(markdown),
                LLM_MAX_INPUT_CHARS,
            )
            markdown = markdown[:LLM_MAX_INPUT_CHARS]

        @api_retry(_is_transient_anthropic_error, logger=logger)
        def _call() -> Any:
            # tool_choice must stay "auto": forcing a tool is rejected by this model, so the
            # prompt asks for the call and `strict` guarantees schema-valid arguments.
            # `fallbacks: "default"` lets the API retry a safety refusal on another model.
            return self._client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                system=LLM_SYSTEM_PROMPT,
                tools=[LLM_TOOL],
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[
                    {
                        "role": "user",
                        "content": f"Website: {site_domain}\n\n<website_text>\n{markdown}\n</website_text>\n\n"
                        "Call record_company with the extracted fields.",
                    }
                ],
            )

        response = _call()
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Model declined to process {site_domain}")
        for block in response.content:
            if block.type == "tool_use" and block.name == LLM_TOOL["name"]:
                data = block.input if isinstance(block.input, dict) else json.loads(block.input)
                return validate_record(data)
        raise RuntimeError(f"Model did not call {LLM_TOOL['name']} for {site_domain} (stop={response.stop_reason})")


def validate_record(data: dict[str, Any]) -> dict[str, Any]:
    """Check a record against EXTRACTION_SCHEMA's required keys and basic types; normalize a few fields."""
    missing = [k for k in EXTRACTION_SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"Extraction missing fields: {missing}")
    extra = set(data) - set(EXTRACTION_SCHEMA["properties"])
    if extra:
        raise ValueError(f"Extraction has unexpected fields: {sorted(extra)}")
    if not isinstance(data["emails"], list) or not isinstance(data["product_mix"], list):
        raise ValueError("emails and product_mix must be lists")
    if data["size_signal"] not in ("small", "mid", "large"):
        raise ValueError(f"Bad size_signal {data['size_signal']!r}")
    if data["whatsapp_number"]:
        data["whatsapp_number"] = normalize_indian_mobile(str(data["whatsapp_number"]))
    if data["gstin"] and not gstin_is_valid(str(data["gstin"])):
        logger.info("Dropping GSTIN %s that fails checksum", data["gstin"])
        data["gstin"] = None
    data["mentions_jute_roll_input"] = bool(data["mentions_jute_roll_input"])
    return data


# --------------------------------------------------------------------------- #
# Step runner
# --------------------------------------------------------------------------- #


def _site_domain_from_markdown(markdown: str, fallback: str) -> str:
    match = re.search(r"^website:\s*(\S+)", markdown, flags=re.M)
    if not match:
        return fallback
    return host_of(match.group(1)) or fallback


def run(
    in_dir: Path = RAW_WEBSITES_DIR,
    out_dir: Path = ENRICHED_DIR,
    use_llm: bool = False,
    force: bool = False,
    limit: int | None = None,
) -> list[Path]:
    """Extract every pending site. Returns the enriched JSON files written this run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(p for p in in_dir.glob("*.md") if not p.name.startswith("_"))
    pending = [p for p in sources if force or not (out_dir / f"{p.stem}.json").exists()]
    if limit is not None:
        pending = pending[:limit]
    method = "llm" if use_llm else "rules"
    logger.info(
        "Extract (%s): %d site files, %d already enriched, %d to process",
        method,
        len(sources),
        len(sources) - len([p for p in sources if not (out_dir / f"{p.stem}.json").exists()]),
        len(pending),
    )
    if not pending:
        return []

    extractor: Callable[[str, str], dict[str, Any]] = LLMExtractor() if use_llm else extract_rules
    written: list[Path] = []
    for path in tqdm(pending, desc=f"extract[{method}]", unit="site"):
        markdown = path.read_text(encoding="utf-8")
        domain = _site_domain_from_markdown(markdown, path.stem)
        try:
            record = extractor(markdown, domain)
        except Exception:
            logger.exception("Extraction failed for %s; will retry next run", path.stem)
            continue
        record = {
            **record,
            "site_key": path.stem,
            "domain": domain,
            "extraction_method": method,
            "model": LLM_MODEL if use_llm else None,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        out_path = out_dir / f"{path.stem}.json"
        write_json_atomic(out_path, record)
        written.append(out_path)
    logger.info("Wrote %d enriched files to %s", len(written), out_dir)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--use-llm", action="store_true", help=f"Use Claude ({LLM_MODEL}); paid, needs ANTHROPIC_API_KEY")
    parser.add_argument("--force", action="store_true", help="Re-extract sites that already have output")
    parser.add_argument("--limit", type=int, help="Only process the first N pending sites")
    args = parser.parse_args(argv)
    setup_logging()
    run(use_llm=args.use_llm, force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
