"""Self-authored reference datasets served as MCP resources and queried by tools.

Everything below was typed in by the author of this repository (facts such as symbols, atomic numbers and unit
conversion definitions are public knowledge; the selection, structure, rounding and every glossary sentence are original).
Numbers for countries are APPROXIMATE and ILLUSTRATIVE (rounded, circa 2023) - do not use them as an authoritative source.

Each dataset is a tuple of dicts built from compact row tuples so the file stays reviewable.
"""

import hashlib
import struct
import zlib

DATASET_NOTE = "Original, hand-authored, approximate/illustrative data for protocol testing; not authoritative."

# ------------------------------------------------------------------------------------------------ countries
# name, iso2, iso3, capital, region, population (millions, rounded), area (thousand km2, rounded), languages, currency
_COUNTRY_ROWS = (
    ("Argentina", "AR", "ARG", "Buenos Aires", "South America", 46.0, 2780, ("Spanish",), "ARS"),
    ("Australia", "AU", "AUS", "Canberra", "Oceania", 26.5, 7690, ("English",), "AUD"),
    ("Austria", "AT", "AUT", "Vienna", "Europe", 9.1, 84, ("German",), "EUR"),
    ("Belgium", "BE", "BEL", "Brussels", "Europe", 11.7, 31, ("Dutch", "French", "German"), "EUR"),
    ("Bolivia", "BO", "BOL", "Sucre", "South America", 12.4, 1099, ("Spanish", "Quechua", "Aymara"), "BOB"),
    ("Brazil", "BR", "BRA", "Brasilia", "South America", 216.0, 8516, ("Portuguese",), "BRL"),
    ("Canada", "CA", "CAN", "Ottawa", "North America", 40.0, 9985, ("English", "French"), "CAD"),
    ("Chile", "CL", "CHL", "Santiago", "South America", 19.6, 756, ("Spanish",), "CLP"),
    ("China", "CN", "CHN", "Beijing", "Asia", 1410.0, 9597, ("Mandarin",), "CNY"),
    ("Colombia", "CO", "COL", "Bogota", "South America", 52.0, 1142, ("Spanish",), "COP"),
    ("Cuba", "CU", "CUB", "Havana", "North America", 11.1, 110, ("Spanish",), "CUP"),
    ("Denmark", "DK", "DNK", "Copenhagen", "Europe", 5.9, 43, ("Danish",), "DKK"),
    ("Egypt", "EG", "EGY", "Cairo", "Africa", 112.0, 1002, ("Arabic",), "EGP"),
    ("Ethiopia", "ET", "ETH", "Addis Ababa", "Africa", 126.0, 1104, ("Amharic", "Oromo"), "ETB"),
    ("Finland", "FI", "FIN", "Helsinki", "Europe", 5.6, 338, ("Finnish", "Swedish"), "EUR"),
    ("France", "FR", "FRA", "Paris", "Europe", 68.0, 644, ("French",), "EUR"),
    ("Germany", "DE", "DEU", "Berlin", "Europe", 84.5, 358, ("German",), "EUR"),
    ("Ghana", "GH", "GHA", "Accra", "Africa", 34.0, 239, ("English",), "GHS"),
    ("Greece", "GR", "GRC", "Athens", "Europe", 10.4, 132, ("Greek",), "EUR"),
    ("India", "IN", "IND", "New Delhi", "Asia", 1428.0, 3287, ("Hindi", "English"), "INR"),
    ("Indonesia", "ID", "IDN", "Jakarta", "Asia", 277.0, 1905, ("Indonesian",), "IDR"),
    ("Ireland", "IE", "IRL", "Dublin", "Europe", 5.2, 70, ("English", "Irish"), "EUR"),
    ("Italy", "IT", "ITA", "Rome", "Europe", 59.0, 301, ("Italian",), "EUR"),
    ("Japan", "JP", "JPN", "Tokyo", "Asia", 124.0, 378, ("Japanese",), "JPY"),
    ("Kenya", "KE", "KEN", "Nairobi", "Africa", 55.0, 580, ("Swahili", "English"), "KES"),
    ("Mexico", "MX", "MEX", "Mexico City", "North America", 128.0, 1964, ("Spanish",), "MXN"),
    ("Morocco", "MA", "MAR", "Rabat", "Africa", 37.8, 447, ("Arabic", "Tamazight"), "MAD"),
    ("Netherlands", "NL", "NLD", "Amsterdam", "Europe", 17.9, 42, ("Dutch",), "EUR"),
    ("New Zealand", "NZ", "NZL", "Wellington", "Oceania", 5.2, 268, ("English", "Maori"), "NZD"),
    ("Nigeria", "NG", "NGA", "Abuja", "Africa", 224.0, 924, ("English",), "NGN"),
    ("Norway", "NO", "NOR", "Oslo", "Europe", 5.5, 385, ("Norwegian",), "NOK"),
    ("Peru", "PE", "PER", "Lima", "South America", 34.0, 1285, ("Spanish", "Quechua"), "PEN"),
    ("Poland", "PL", "POL", "Warsaw", "Europe", 37.7, 313, ("Polish",), "PLN"),
    ("Portugal", "PT", "PRT", "Lisbon", "Europe", 10.3, 92, ("Portuguese",), "EUR"),
    ("South Africa", "ZA", "ZAF", "Pretoria", "Africa", 60.4, 1221, ("Zulu", "Xhosa", "Afrikaans", "English"), "ZAR"),
    ("South Korea", "KR", "KOR", "Seoul", "Asia", 51.7, 100, ("Korean",), "KRW"),
    ("Spain", "ES", "ESP", "Madrid", "Europe", 48.0, 506, ("Spanish",), "EUR"),
    ("Sweden", "SE", "SWE", "Stockholm", "Europe", 10.5, 450, ("Swedish",), "SEK"),
    ("Switzerland", "CH", "CHE", "Bern", "Europe", 8.8, 41, ("German", "French", "Italian", "Romansh"), "CHF"),
    ("Thailand", "TH", "THA", "Bangkok", "Asia", 71.8, 513, ("Thai",), "THB"),
    ("Turkey", "TR", "TUR", "Ankara", "Asia", 85.8, 784, ("Turkish",), "TRY"),
    ("United Kingdom", "GB", "GBR", "London", "Europe", 67.7, 244, ("English",), "GBP"),
    ("United States", "US", "USA", "Washington, D.C.", "North America", 335.0, 9834, ("English",), "USD"),
    ("Uruguay", "UY", "URY", "Montevideo", "South America", 3.4, 176, ("Spanish",), "UYU"),
    ("Vietnam", "VN", "VNM", "Hanoi", "Asia", 98.9, 331, ("Vietnamese",), "VND"),
)
COUNTRIES = tuple(
    {"name": n, "iso2": a2, "iso3": a3, "capital": cap, "region": reg, "population_millions": pop, "area_thousand_km2": area,
     "languages": list(langs), "currency": cur}
    for (n, a2, a3, cap, reg, pop, area, langs, cur) in _COUNTRY_ROWS)
COUNTRY_BY_ISO2 = {c["iso2"]: c for c in COUNTRIES}
REGIONS = tuple(sorted({c["region"] for c in COUNTRIES}))

# ------------------------------------------------------------------------------------------------ periodic table (Z = 1..36)
# Z, symbol, name, standard atomic weight (rounded), group, period, category
_ELEMENT_ROWS = (
    (1, "H", "Hydrogen", 1.008, 1, 1, "nonmetal"), (2, "He", "Helium", 4.0026, 18, 1, "noble gas"),
    (3, "Li", "Lithium", 6.94, 1, 2, "alkali metal"), (4, "Be", "Beryllium", 9.0122, 2, 2, "alkaline earth metal"),
    (5, "B", "Boron", 10.81, 13, 2, "metalloid"), (6, "C", "Carbon", 12.011, 14, 2, "nonmetal"),
    (7, "N", "Nitrogen", 14.007, 15, 2, "nonmetal"), (8, "O", "Oxygen", 15.999, 16, 2, "nonmetal"),
    (9, "F", "Fluorine", 18.998, 17, 2, "halogen"), (10, "Ne", "Neon", 20.180, 18, 2, "noble gas"),
    (11, "Na", "Sodium", 22.990, 1, 3, "alkali metal"), (12, "Mg", "Magnesium", 24.305, 2, 3, "alkaline earth metal"),
    (13, "Al", "Aluminium", 26.982, 13, 3, "post-transition metal"), (14, "Si", "Silicon", 28.085, 14, 3, "metalloid"),
    (15, "P", "Phosphorus", 30.974, 15, 3, "nonmetal"), (16, "S", "Sulfur", 32.06, 16, 3, "nonmetal"),
    (17, "Cl", "Chlorine", 35.45, 17, 3, "halogen"), (18, "Ar", "Argon", 39.948, 18, 3, "noble gas"),
    (19, "K", "Potassium", 39.098, 1, 4, "alkali metal"), (20, "Ca", "Calcium", 40.078, 2, 4, "alkaline earth metal"),
    (21, "Sc", "Scandium", 44.956, 3, 4, "transition metal"), (22, "Ti", "Titanium", 47.867, 4, 4, "transition metal"),
    (23, "V", "Vanadium", 50.942, 5, 4, "transition metal"), (24, "Cr", "Chromium", 51.996, 6, 4, "transition metal"),
    (25, "Mn", "Manganese", 54.938, 7, 4, "transition metal"), (26, "Fe", "Iron", 55.845, 8, 4, "transition metal"),
    (27, "Co", "Cobalt", 58.933, 9, 4, "transition metal"), (28, "Ni", "Nickel", 58.693, 10, 4, "transition metal"),
    (29, "Cu", "Copper", 63.546, 11, 4, "transition metal"), (30, "Zn", "Zinc", 65.38, 12, 4, "transition metal"),
    (31, "Ga", "Gallium", 69.723, 13, 4, "post-transition metal"), (32, "Ge", "Germanium", 72.630, 14, 4, "metalloid"),
    (33, "As", "Arsenic", 74.922, 15, 4, "metalloid"), (34, "Se", "Selenium", 78.971, 16, 4, "nonmetal"),
    (35, "Br", "Bromine", 79.904, 17, 4, "halogen"), (36, "Kr", "Krypton", 83.798, 18, 4, "noble gas"),
)
ELEMENTS = tuple({"number": z, "symbol": s, "name": n, "atomic_mass": m, "group": g, "period": p, "category": cat}
                 for (z, s, n, m, g, p, cat) in _ELEMENT_ROWS)
ELEMENT_BY_SYMBOL = {e["symbol"]: e for e in ELEMENTS}
ELEMENT_CATEGORIES = tuple(sorted({e["category"] for e in ELEMENTS}))

# ------------------------------------------------------------------------------------------------ units
# category -> (base unit, {unit: factor-to-base}); temperature is handled with explicit affine formulas.
UNITS = {
    "length": ("m", {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344, "nmi": 1852.0}),
    "mass": ("kg", {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "oz": 0.028349523125, "lb": 0.45359237, "st": 6.35029318}),
    "volume": ("L", {"mL": 0.001, "L": 1.0, "m3": 1000.0, "tsp": 0.00492892159375, "tbsp": 0.01478676478125, "floz": 0.0295735295625,
                     "cup": 0.2365882365, "pt": 0.473176473, "gal": 3.785411784}),
    "time": ("s", {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0, "wk": 604800.0}),
    "energy": ("J", {"J": 1.0, "kJ": 1000.0, "cal": 4.184, "kcal": 4184.0, "Wh": 3600.0, "kWh": 3.6e6, "BTU": 1055.05585262, "eV": 1.602176634e-19}),
    "speed": ("m/s", {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 1852.0 / 3600.0}),
    "data": ("B", {"B": 1.0, "KB": 1e3, "KiB": 1024.0, "MB": 1e6, "MiB": 1048576.0, "GB": 1e9, "GiB": 1073741824.0}),
    "pressure": ("Pa", {"Pa": 1.0, "kPa": 1000.0, "bar": 1e5, "atm": 101325.0, "psi": 6894.757293168, "mmHg": 133.322387415}),
    "temperature": ("K", {"C": None, "F": None, "K": None}),
}
UNIT_CATEGORIES = tuple(sorted(UNITS))
_TEMP_TO_K = {"C": lambda v: v + 273.15, "F": lambda v: (v - 32) * 5 / 9 + 273.15, "K": lambda v: v}
_TEMP_FROM_K = {"C": lambda k: k - 273.15, "F": lambda k: (k - 273.15) * 9 / 5 + 32, "K": lambda k: k}


class UnitError(ValueError):
    pass


def unit_category_of(unit):
    hits = [c for c, (_, table) in UNITS.items() if unit in table]
    if len(hits) != 1:
        raise UnitError("unknown or ambiguous unit %r" % unit)
    return hits[0]


def convert(value, src, dst):
    """Convert ``value`` from unit ``src`` to ``dst`` (same category). Returns (result, category)."""
    cat = unit_category_of(src)
    if unit_category_of(dst) != cat:
        raise UnitError("cannot convert %s (%s) to %s (%s)" % (src, cat, dst, unit_category_of(dst)))
    if cat == "temperature":
        kelvin = _TEMP_TO_K[src](float(value))
        if kelvin < 0 and abs(kelvin) > 1e-9:
            raise UnitError("below absolute zero")
        return _TEMP_FROM_K[dst](kelvin), cat
    table = UNITS[cat][1]
    return float(value) * table[src] / table[dst], cat


def unit_table(category):
    base, table = UNITS[category]
    return {"category": category, "base_unit": base,
            "units": ({u: {"to_base": f} for u, f in table.items()} if category != "temperature"
                      else {"C": {"to_K": "C + 273.15"}, "F": {"to_K": "(F - 32) * 5/9 + 273.15"}, "K": {"to_K": "K"}})}


# ------------------------------------------------------------------------------------------------ glossary
_GLOSSARY_ROWS = (
    ("audience", "The party a token is meant for; a verifier must reject tokens whose audience is not itself."),
    ("backpressure", "A signal or mechanism that slows a producer down when a consumer cannot keep up."),
    ("base64", "A text encoding that represents arbitrary bytes with 64 printable characters, growing the data by about one third."),
    ("blob", "An opaque sequence of bytes, delivered in JSON as a base64 string next to a MIME type."),
    ("capability", "A feature a peer announces during initialization; behaviour that is not announced must not be relied on."),
    ("checksum", "A short value computed from data so that accidental changes can be detected."),
    ("completion", "A suggestion of possible values for an argument while the user is still typing it."),
    ("cursor", "An opaque token that marks where a paginated listing should resume."),
    ("deadlock", "A state in which parties each wait for something only another waiting party can provide."),
    ("debounce", "Collapsing a burst of events into a single event after things go quiet."),
    ("digest", "The fixed-size output of a hash function applied to some input."),
    ("endpoint", "A network address at which a service accepts requests."),
    ("envelope", "The outer structure around a message that carries routing metadata but not the payload meaning."),
    ("fsync", "A system call that forces buffered file data to stable storage before returning."),
    ("handshake", "The opening exchange in which two peers agree on the rules of the conversation."),
    ("idempotent", "Describes an operation that has the same effect whether it runs once or many times."),
    ("invariant", "A condition that must hold at every observable moment of a program's execution."),
    ("jitter", "Random variation added to a delay so that many clients do not retry in lockstep."),
    ("keepalive", "A periodic message that proves a connection is still alive."),
    ("leeway", "A small tolerance applied to time comparisons to absorb clock differences between machines."),
    ("lockout", "Temporarily refusing further attempts after too many failures."),
    ("manifest", "A list of files together with their hashes, used to confirm a bundle is intact."),
    ("mime type", "A label such as text/plain or image/png that tells a receiver how to interpret bytes."),
    ("nonce", "A value meant to be used only once, so that replaying a message can be detected."),
    ("notification", "A message that expects no reply and therefore carries no request identifier."),
    ("pagination", "Splitting a long list into pages that are fetched one at a time."),
    ("prompt", "A reusable, parameterised starting point for a conversation with a language model."),
    ("quorum", "The minimum number of participants that must agree before a decision counts."),
    ("resource", "Read-only content identified by a URI that a server offers to its client."),
    ("rotation", "Replacing a key or token with a fresh one on a schedule or after use."),
    ("sandbox", "An environment that restricts what the code running inside it may touch."),
    ("session", "State that links a series of requests from the same client."),
    ("subscription", "A standing request to be told whenever a particular resource changes."),
    ("template", "A pattern with named holes, such as a URI with {id}, that expands into concrete values."),
    ("throttle", "To limit the rate at which requests are accepted."),
    ("timeout", "The longest a caller will wait before giving up."),
    ("tombstone", "A marker left behind to record that something was deleted."),
    ("transport", "The channel, such as standard input/output or HTTP, that carries protocol messages."),
    ("uri", "A string that names a resource; the part before the first colon is its scheme."),
    ("watermark", "A marker of how far a consumer has processed an ordered stream."),
)
GLOSSARY = tuple({"term": t, "definition": d} for t, d in _GLOSSARY_ROWS)
GLOSSARY_BY_TERM = {g["term"]: g for g in GLOSSARY}

# ------------------------------------------------------------------------------------------------ binary blobs (deterministic)


def _png_chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def make_png(width=32, height=32):
    """A small RGB test pattern. Uses stored (level 0) deflate blocks so the bytes do not depend on the zlib build."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type 0
        for x in range(width):
            checker = 255 if ((x // 4) + (y // 4)) % 2 == 0 else 0
            rows += bytes(((x * 255) // (width - 1), (y * 255) // (height - 1), checker))
    comp = zlib.compressobj(0)
    idat = comp.compress(bytes(rows)) + comp.flush()
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b""))


def make_random_bin(n=512):
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(b"mcp-resource-heavy:random.bin:%d" % counter).digest()
        counter += 1
    return bytes(out[:n])


BLOBS = {
    "sample.png": {"mime": "image/png", "title": "Generated 32x32 RGB test pattern", "data": make_png()},
    "random.bin": {"mime": "application/octet-stream", "title": "512 deterministic pseudo-random bytes", "data": make_random_bin()},
}

ABOUT_TEXT = """# mcp-resource-heavy

A stdlib-only MCP server whose point is *resources*: static documents, URI templates, dynamic values that change,
binary blobs, subscriptions with update notifications, cursor pagination, completions and multi-message prompts.

Datasets (original, approximate): %d countries, %d elements (Z=1..36), %d unit categories, %d glossary terms.
""" % (len(COUNTRIES), len(ELEMENTS), len(UNITS), len(GLOSSARY))
