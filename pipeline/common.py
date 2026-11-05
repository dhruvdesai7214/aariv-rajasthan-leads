"""Shared helpers: polite HTTP session, robots.txt checks, CSV IO, exclude list."""
import csv, re, time, urllib.parse, urllib.robotparser, unicodedata
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (compatible; brand-research-bot/0.1)"
TIMEOUT = 15

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})

# Never touch these, whatever robots.txt says (brief rule).
BLOCKED_HOSTS = ("instagram.com", "linkedin.com", "facebook.com", "faire.com", "tiktok.com")

_robots = {}

def robots_ok(url):
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if any(h in host for h in BLOCKED_HOSTS):
        return False
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=TIMEOUT)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp.allow_all = True  # unreachable robots -> fetch of page will decide
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)

LAST_ERROR = {}

def get(url, **kw):
    """GET honouring robots.txt. Returns Response or None."""
    if not robots_ok(url):
        return None
    try:
        return session.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)
    except requests.exceptions.SSLError:
        LAST_ERROR[url] = "tls"
        return None
    except requests.RequestException:
        LAST_ERROR[url] = "net"
        return None

def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(skin ?care|skincare|beauty|ltd|limited|co|company|the|london|uk|organics?)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)

def load_exclude():
    p = DATA / "exclude.txt"
    names = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {norm_name(n) for n in names if norm_name(n)}

def domain_of(url):
    host = urllib.parse.urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host

def read_csv(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def write_csv(name, rows, fields):
    with open(DATA / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> data/{name}")
