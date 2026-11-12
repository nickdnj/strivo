"""What to Watch filters: rating, number of ratings, years, genres to skip, original language, and an exception
for the truly great ones.

Each category (movies, series, docs...) has saved defaults in config.json "discover" that the background builds use
every day. The app can also ask for a one-off list with different filters ("just for now") without touching them.

Filters are used twice: when a list is built (so TMDB is asked for the right things) and when a list is shown (so a
change to the defaults takes effect at once on what is already there).
"""
from __future__ import annotations

import time

# The genres people filter on, by TMDB's movie names. TV names like "Sci-Fi & Fantasy" are split to match these.
GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family", "Fantasy",
          "History", "Horror", "Kids", "Music", "Mystery", "Reality", "Romance", "Science Fiction", "Thriller", "War",
          "Western"]
_SPLIT = {"Action & Adventure": ["Action", "Adventure"], "Sci-Fi & Fantasy": ["Science Fiction", "Fantasy"],
          "War & Politics": ["War"]}
SORTS = ("fit", "rating", "new")
LANGS = ("any", "mine")
KEYS = ("min_rating", "min_votes", "years", "skip_genres", "langs", "great", "sort")


def genre_names(names) -> set:
    out = set()
    for n in names or []:
        out.update(_SPLIT.get(n, [n]))
    return out


def clean(v: dict, base: dict) -> dict:
    """Valid filter values from untrusted input (the web API), on top of `base`."""
    f = {k: base.get(k) for k in KEYS}
    if not isinstance(v, dict):
        return f
    def num(k, conv, lo, hi):
        if k in v:
            try:
                f[k] = max(lo, min(hi, conv(v[k])))
            except (TypeError, ValueError):
                pass
    num("min_rating", float, 0.0, 9.5)
    num("min_votes", int, 0, 100000)
    num("years", int, 0, 100)
    num("great", lambda x: 0.0 if float(x or 0) <= 0 else max(5.0, float(x)), 0.0, 9.5)
    if isinstance(v.get("skip_genres"), list):
        f["skip_genres"] = sorted({str(g) for g in v["skip_genres"] if str(g) in GENRES})
    if v.get("langs") in LANGS:
        f["langs"] = v["langs"]
    if v.get("sort") in SORTS:
        f["sort"] = v["sort"]
    return f


def my_lang(c: dict) -> str:
    return (c.get("language") or "en-US").split("-")[0].lower()


def since(f: dict) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - f["years"] * 365.25 * 86400)) if f["years"] else ""


def is_great(item: dict, f: dict) -> bool:
    return bool(f.get("great")) and (item.get("rating") or 0) >= f["great"]


def passes(item: dict, f: dict, lang: str) -> bool:
    """Does a picked title (with rating, votes, year, genres, lang) fit the filters?"""
    r = item.get("rating") or 0
    if r < f["min_rating"] or (item.get("votes") or 0) < f["min_votes"]:
        return False
    s = since(f)
    if s and (item.get("year") or "9999") < s[:4]:
        return False
    if is_great(item, f):
        return True                                   # a great one gets past genre and language
    if genre_names(item.get("genres")) & set(f["skip_genres"]):
        return False
    if f["langs"] == "mine" and item.get("lang") and item["lang"] != lang:
        return False
    return True


def order(items: list, f: dict) -> list:
    """Sort a shown list. "fit" keeps the build's order (what suits you); Later ones always go last."""
    if f["sort"] == "rating":
        items = sorted(items, key=lambda i: -(i.get("rating") or 0))
    elif f["sort"] == "new":
        items = sorted(items, key=lambda i: i.get("year") or "", reverse=True)
    return sorted(items, key=lambda i: bool(i.get("later")))


def tmdb_params(core, typ: str, f: dict, lang: str, strict: bool = True) -> dict:
    """/discover query params for the filters. strict=False drops genre and language (for the great-ones query)."""
    p = {"vote_average.gte": f["min_rating"], "vote_count.gte": max(f["min_votes"], 10)}
    s = since(f)
    if s:
        p["first_air_date.gte" if typ == "tv" else "primary_release_date.gte"] = s
    if strict:
        if f["langs"] == "mine":
            p["with_original_language"] = lang
        ids = genre_ids(core, typ, f["skip_genres"])
        if ids:
            p["without_genres"] = ",".join(str(i) for i in ids)
    return p


def genre_ids(core, typ: str, names) -> list:
    if not names:
        return []
    want = set(names)
    try:
        gl = core.tmdb.get("/genre/%s/list" % typ).get("genres", [])
    except Exception:
        return []
    return [g["id"] for g in gl if genre_names([g["name"]]) & want]


def names_for(core, typ: str, ids) -> list:
    try:
        gl = {g["id"]: g["name"] for g in core.tmdb.get("/genre/%s/list" % typ).get("genres", [])}
    except Exception:
        return []
    return [gl[i] for i in ids or [] if i in gl]
