"""What to Watch: movie, series and category picks from what you actually watched.

Signals, strongest first: titles you marked "watched it, liked it"; TMDB recommendations for what you watch most
(hours, decayed by recency); the genres you watch; and optional taste searches from config ("taste").
Everything is filtered to the services you pay for, in your country, and shaped by "not interested because...".
"""
from __future__ import annotations

import time
from collections import defaultdict

from . import filters as F
from .config import disc_cfg
from .history import nm_key
from .tmdb import IMG

SUG_HALF_LIFE = 60 * 86400
TV2MOVIE_GENRE = {10759: [28, 12], 10765: [878, 14], 10768: [10752], 10762: [10751]}
TV_SKIP = {10763, 10764, 10766, 10767, 10762}     # news, reality, soap, talk, kids: not primetime picks

# Neutral defaults. A household can describe its taste in config.json "taste" (see docs/CONFIG.md):
#   genres: {"Mystery": 1.0, ...} weights that boost a genre
#   story_genres: ["Adventure", ...] if set, Action picks must also have one of these
#   searches: [[reason, "Genre,Genre", ["keyword", ...]], ...] extra discover queries
DEFAULT_TASTE = {"genres": {}, "story_genres": [], "searches": [["a crowd favorite", "", []]]}
DEFAULT_TV_TASTE = [["a crowd favorite", "", []]]


def _movie_genres(ids):
    out = []
    for g in ids:
        out += TV2MOVIE_GENRE.get(g, [g])
    return out


def taste_seeds(d, limit=15):
    """Weighted watch history: hours watched, decayed by recency. Movies and series only."""
    now = time.time()
    seeds = {}
    for s in d["series"] + [x for x in d["daily"] if x.get("cat") == "game"]:
        if s["hours"] < 0.5:
            continue
        seeds[("tv", s["series"])] = s["hours"] * 0.5 ** ((now - s["last"]) / SUG_HALF_LIFE)
    for m in d["movies"]:
        seeds[("movie", m["title"])] = max(m["hours"], 1.0) * 0.5 ** ((now - m["last"]) / SUG_HALF_LIFE) * 1.5
    return sorted(((w, k) for k, w in seeds.items()), reverse=True)[:limit]


def resolve(core, typ, name):
    res = [x for x in core.tmdb.get("/search/multi", query=name, include_adult="false").get("results", [])
           if x.get("media_type") in ("tv", "movie")]
    pref = [x for x in res if x["media_type"] == typ]
    return (pref or res or [None])[0]


def provider_ids(core, mine, typ="movie"):
    from .apps import prov_map
    ids = []
    for x in core.tmdb.get("/watch/providers/" + typ, watch_region=core.country()).get("results", []):
        if prov_map().get(x["provider_name"]) in mine:
            ids.append(x["provider_id"])
    return ids


def _keyword_ids(core, kws):
    ids = []
    for k in kws:
        try:
            ids += [str(r["id"]) for r in core.tmdb.get("/search/keyword", query=k).get("results", [])[:1]]
        except Exception:
            pass
    return ids


def _all_seen(core):
    return core.seen_names()


def suggest_build(core, f=None, fname="suggestions.json"):
    c = core.config()
    mine = core.services()
    d = core.build()
    taste = dict(DEFAULT_TASTE)
    taste.update(c.get("taste") or {})
    f = f or disc_cfg(c)["movies"]
    lang = F.my_lang(c)
    min_rating, min_votes = f["min_rating"], f["min_votes"]
    since = F.since(f) or "1900-01-01"
    watched = {nm_key(m["title"]) for m in d["movies"]}
    skip = {(w["id"], w["type"]) for w in core.wishlist()}
    dm = core.dismissed()
    cands = {}
    gname = {g["id"]: g["name"] for g in core.tmdb.get("/genre/movie/list").get("genres", [])}
    gid = {n: i for i, n in gname.items()}
    story = {gid[n] for n in taste.get("story_genres", []) if n in gid}
    fbs = [fb for k, fb in core.feedback().items() if not k.startswith("tv:")]
    pen, hide_seq = defaultdict(int), False
    for fb in fbs:
        tg = fb.get("tags") or []
        if "genre" in tg:
            for n in fb.get("genres", []):
                pen[n] += 1
        if "kids" in tg:
            pen["Animation"] += 1
            pen["Family"] += 1
        if "sequel" in tg:
            hide_seq = True

    def mult(gids):
        m = 1 + sum(taste.get("genres", {}).get(gname.get(g, ""), 0) for g in gids)
        for g in gids:
            m *= max(0.5, 0.85 ** pen.get(gname.get(g, ""), 0))
        return m

    def add(x, weight, why, floor=None):
        floor = min_rating if floor is None else floor
        if x.get("media_type", "movie") != "movie" or not x.get("poster_path"):
            return
        if (x.get("vote_average") or 0) < floor or (x.get("vote_count") or 0) < min_votes:
            return
        if (x.get("release_date") or "") < since:
            return
        gids = x.get("genre_ids") or []
        if story and gid.get("Action") in gids and not (set(gids) & story):
            return
        pre = dict(rating=x.get("vote_average") or 0, votes=x.get("vote_count") or 0,
                   year=(x.get("release_date") or "")[:4], genres=[gname[g] for g in gids if g in gname],
                   lang=x.get("original_language") or "")
        if not F.passes(pre, f, lang):
            return
        r = cands.setdefault(x["id"], dict(id=x["id"], type="movie", title=x["title"], year=pre["year"],
                                           poster=IMG + "w185" + x["poster_path"], score=0, why=[],
                                           votes=pre["votes"], lang=pre["lang"], rating=round(pre["rating"], 1)))
        r["score"] += weight * mult(gids) * (1 + max(0, x["vote_average"] - min_rating) / 2)
        if why not in r["why"]:
            r["why"].append(why)

    pids = "|".join(str(i) for i in provider_ids(core, mine))
    if not pids:
        core.data.write(fname, dict(built=time.time(), items=[], reason="no services", filters=f))
        return []
    disc = dict({"watch_region": core.country(), "with_watch_providers": pids,
                 "with_watch_monetization_types": "flatrate", "sort_by": "popularity.desc"},
                **F.tmdb_params(core, "movie", f, lang))
    _best(core, "movie", disc, f, lang, lambda x, w, why: add(x, w, why))
    for reason, genres, kws, *opt in taste.get("searches", []):
        floor = max(min_rating, float(opt[0]) if opt and isinstance(opt[0], (int, float)) else min_rating)
        p = dict(disc, **{"vote_average.gte": floor})
        if genres:
            p["with_genres"] = ",".join(str(gid[g]) for g in genres.split(",") if g in gid)
        if kws:
            ids = _keyword_ids(core, kws)
            if not ids:
                continue
            p["with_keywords"] = "|".join(ids)
        for page in (1, 2):
            try:
                res = core.tmdb.get("/discover/movie", page=page, **p).get("results", [])
            except Exception:
                break
            for x in res:
                add(x, 5.0, reason, floor)
    for mid, fb in core.feedback().items():
        if fb.get("reason") == "liked" and not mid.startswith("tv:"):
            try:
                for x in core.tmdb.get("/movie/%s/recommendations" % mid).get("results", []):
                    add(x, 3.0, "you liked " + fb.get("title", "a movie"))
            except Exception:
                pass
    gscore = defaultdict(float)
    for w, (typ, name) in taste_seeds(d):
        try:
            hit = resolve(core, typ, name)
        except Exception:
            continue
        if not hit:
            continue
        if typ == "movie" and hit["media_type"] == "movie":
            for x in core.tmdb.get("/movie/%d/recommendations" % hit["id"]).get("results", []):
                add(x, w, "you watched " + name)
        try:
            info = core.tmdb.get("/%s/%d" % (hit["media_type"], hit["id"]))
        except Exception:
            continue
        for g in _movie_genres([g["id"] for g in info.get("genres", [])][:3]):
            gscore[g] += w
    for g, w in sorted(gscore.items(), key=lambda x: -x[1])[:3]:
        try:
            res = core.tmdb.get("/discover/movie", **dict(disc, with_genres=g)).get("results", [])
        except Exception:
            continue
        for x in res:
            add(x, w * 0.2, "you watch " + gname.get(g, "these").lower())
    out = []
    for r in sorted(cands.values(), key=lambda r: -r["score"])[:120]:
        if nm_key(r["title"]) in watched or (r["id"], "movie") in skip or ("suggest:%d" % r["id"]) in dm:
            continue
        try:
            wh = core.where(r["id"], "movie")
        except Exception:
            continue
        if hide_seq:
            try:
                col = (core.tmdb.get("/movie/%d" % r["id"]).get("belongs_to_collection") or {}).get("name", "")
            except Exception:
                col = ""
            base = nm_key(col.replace(" Collection", "")) if col else ""
            if base and not any(base in w for w in watched):
                continue
        on = sorted(n for n, k in wh.get("providers", {}).items() if k == "flatrate" and n in mine)
        if not on:
            continue
        item = dict(r, providers=on, overview=wh.get("overview", "")[:2000], genres=wh.get("genres", []),
                    rating=wh.get("rating") or r["rating"], runtime=wh.get("runtime"), score=round(r["score"], 2), why=r["why"][:3])
        if not F.passes(item, f, lang):
            continue
        out.append(item)
        if len(out) >= 40:
            break
    core.data.write(fname, dict(built=time.time(), items=out, filters=f))
    return out


def suggestions(core, fname="suggestions.json", f=None):
    c = core.data.read(fname, {"built": 0, "items": []}) or {"built": 0, "items": []}
    f, lang = f or disc_cfg(core.config())["movies"], F.my_lang(core.config())
    dm, lt, now = core.dismissed(), core.later(), time.time()
    skip = {(w["id"], w["type"]) for w in core.wishlist()}
    items = [dict(i, later=bool(lt.get("movie:%d" % i["id"], 0) > now - 30 * 86400)) for i in c["items"]
             if (i["id"], "movie") not in skip and ("suggest:%d" % i["id"]) not in dm and F.passes(i, f, lang)]
    gone = [dict(i, at=dm["suggest:%d" % i["id"]]) for i in c["items"] if ("suggest:%d" % i["id"]) in dm]
    return dict(built=c["built"], items=F.order(items, f), dismissed=gone, filters=f, pending=c.get("pending", False))


def shows_build(core, f=None, fname="show_suggestions.json", fresh=False):
    c = core.config()
    mine = core.services()
    d = core.build()
    f = f or disc_cfg(c)["series"]
    lang = F.my_lang(c)
    min_rating, min_votes = f["min_rating"], f["min_votes"]
    tvg = {}
    allseen = _all_seen(core) | {nm_key(s["series"]) for s in d["series"]} | {nm_key(s["series"]) for s in d["daily"]}
    skip = {w["id"] for w in core.wishlist() if w["type"] == "tv"} | {q["id"] for q in core.queue()}
    dm = core.dismissed()
    cands = {}

    def add(x, weight, why, floor=None):
        floor = min_rating if floor is None else floor
        if not x.get("poster_path") or not x.get("name"):
            return
        if (x.get("vote_average") or 0) < floor or (x.get("vote_count") or 0) < min_votes:
            return
        if set(x.get("genre_ids") or []) & TV_SKIP:
            return
        if not tvg:
            tvg.update({g["id"]: g["name"] for g in core.tmdb.get("/genre/tv/list").get("genres", [])} or {0: ""})
        pre = dict(rating=x.get("vote_average") or 0, votes=x.get("vote_count") or 0,
                   year=(x.get("first_air_date") or "")[:4],
                   genres=[tvg[g] for g in x.get("genre_ids") or [] if g in tvg], lang=x.get("original_language") or "")
        if not F.passes(pre, f, lang):
            return
        r = cands.setdefault(x["id"], dict(id=x["id"], type="tv", title=x["name"], year=pre["year"],
                                           poster=IMG + "w185" + x["poster_path"], score=0, why=[],
                                           votes=pre["votes"], lang=pre["lang"], rating=round(pre["rating"], 1)))
        r["score"] += weight * (1 + max(0, x["vote_average"] - min_rating) / 2)
        if why not in r["why"]:
            r["why"].append(why)

    pids = "|".join(str(i) for i in provider_ids(core, mine, "tv"))
    if not pids:
        core.data.write(fname, dict(built=time.time(), items=[], reason="no services", filters=f))
        return []
    disc = dict({"watch_region": core.country(), "with_watch_providers": pids,
                 "with_watch_monetization_types": "flatrate", "sort_by": "popularity.desc"},
                **F.tmdb_params(core, "tv", f, lang))
    _best(core, "tv", disc, f, lang, lambda x, w, why: add(x, w, why))
    for reason, genres, kws, *opt in (c.get("tv_taste") or DEFAULT_TV_TASTE):
        floor = max(min_rating, float(opt[0]) if opt and isinstance(opt[0], (int, float)) else min_rating)
        p = dict(disc, **{"vote_average.gte": floor})
        if genres:
            p["with_genres"] = genres
        if kws:
            ids = _keyword_ids(core, kws)
            if not ids:
                continue
            p["with_keywords"] = "|".join(ids)
        for page in (1, 2):
            try:
                res = core.tmdb.get("/discover/tv", page=page, **p).get("results", [])
            except Exception:
                break
            for x in res:
                add(x, 4.0, reason, floor)
    for w, (typ, name) in taste_seeds(d):
        if typ != "tv":
            continue
        try:
            hit = resolve(core, "tv", name)
        except Exception:
            continue
        if not hit or hit.get("media_type") != "tv":
            continue
        try:
            for x in core.tmdb.get("/tv/%d/recommendations" % hit["id"]).get("results", []):
                add(x, w * 2, "you watch " + name)
        except Exception:
            pass
    old = [] if fresh else ((core.data.read(fname, {}) or {}).get("items", []))
    have = {i["id"] for i in old}
    out = [i for i in old if ("showsug:%d" % i["id"]) not in dm and i["id"] not in skip and nm_key(i["title"]) not in allseen]
    for r in sorted(cands.values(), key=lambda r: -r["score"])[:100]:
        if len(out) >= 60:
            break
        if r["id"] in have or nm_key(r["title"]) in allseen or r["id"] in skip or ("showsug:%d" % r["id"]) in dm:
            continue
        try:
            wh = core.where(r["id"], "tv")
        except Exception:
            continue
        on = sorted(n for n, k in wh.get("providers", {}).items() if k == "flatrate" and n in mine)
        if not on:
            continue
        item = dict(r, providers=on, overview=wh.get("overview", "")[:2000], genres=wh.get("genres", []),
                    rating=wh.get("rating") or r["rating"], seasons=wh.get("seasons"), episodes=wh.get("episodes"),
                    status=wh.get("status", ""), score=round(r["score"], 2), why=r["why"][:3], added=time.time())
        if F.passes(item, f, lang):
            out.append(item)
    core.data.write(fname, dict(built=time.time(), items=out, filters=f))
    return out


def shows(core, fname="show_suggestions.json", f=None):
    c = core.data.read(fname, {"built": 0, "items": []}) or {"built": 0, "items": []}
    f, lang = f or disc_cfg(core.config())["series"], F.my_lang(core.config())
    dm, lt, now = core.dismissed(), core.later(), time.time()
    skip = {w["id"] for w in core.wishlist() if w["type"] == "tv"} | {q["id"] for q in core.queue()}
    items = [dict(i, later=bool(lt.get(str(i["id"]), 0) > now - 30 * 86400)) for i in c["items"]
             if i["id"] not in skip and ("showsug:%d" % i["id"]) not in dm and F.passes(i, f, lang)]
    items.sort(key=lambda i: -i.get("score", 0))
    gone = [dict(i, at=dm["showsug:%d" % i["id"]]) for i in c["items"] if ("showsug:%d" % i["id"]) in dm]
    return dict(built=c["built"], items=F.order(items, f), dismissed=gone, filters=f, pending=c.get("pending", False))


def disc_file(cat):
    return "disc_%s.json" % cat


def disc_build(core, cat, f=None, fname=None, fresh=False):
    c = core.config()
    dc = disc_cfg(c)[cat]
    f = f or dc
    fname = fname or disc_file(cat)
    lang = F.my_lang(c)
    mine = core.services()
    allseen = _all_seen(core)
    skip = {(w["id"], w["type"]) for w in core.wishlist()} | {(q["id"], "tv") for q in core.queue()}
    dm = core.dismissed()
    cands = {}
    for typ, extra in dc["spec"].items():
        pids = "|".join(str(i) for i in provider_ids(core, mine, typ))
        if not pids:
            continue
        p = dict({"watch_region": core.country(), "with_watch_providers": pids,
                  "with_watch_monetization_types": "flatrate"}, **F.tmdb_params(core, typ, f, lang))
        for k, v in extra.items():
            if k == "keyword":
                try:
                    p["with_keywords"] = str(core.tmdb.get("/search/keyword", query=v)["results"][0]["id"])
                except Exception:
                    continue
            else:
                p[k] = v
        if "with_genres" in p and "without_genres" in p:     # skipping the category's own genre would empty it
            keep = set(p["with_genres"].split(","))
            p["without_genres"] = ",".join(g for g in p["without_genres"].split(",") if g not in keep)
            if not p["without_genres"]:
                del p["without_genres"]
        for sort, page in [("popularity.desc", n) for n in (1, 2, 3)] + [("vote_average.desc", n) for n in (1, 2)]:
            try:
                res = core.tmdb.get("/discover/" + typ, page=page, sort_by=sort, **p).get("results", [])
            except Exception:
                break
            for i, x in enumerate(res):
                name = x.get("name") or x.get("title")
                if not x.get("poster_path") or not name:
                    continue
                cands.setdefault((typ, x["id"]), dict(
                    id=x["id"], type=typ, cat=cat, title=name,
                    year=(x.get("first_air_date") or x.get("release_date") or "")[:4],
                    poster=IMG + "w185" + x["poster_path"], votes=x.get("vote_count") or 0,
                    lang=x.get("original_language") or "", rating=round(x.get("vote_average") or 0, 1),
                    score=round((x.get("vote_average") or 0) * 10 - (page - 1) * 5 - i * 0.2, 2),
                    why=["popular " + dc["label"].lower()]))
    old = [] if fresh else (core.data.read(fname, {}) or {}).get("items", [])

    def ok(r):
        return ("disc:%s:%d" % (r["type"], r["id"])) not in dm and (r["id"], r["type"]) not in skip \
            and nm_key(r["title"]) not in allseen

    out = [r for r in old if ok(r)]
    have = {(r["type"], r["id"]) for r in out}
    for r in sorted(cands.values(), key=lambda r: -r["score"]):
        if len(out) >= 50:
            break
        if (r["type"], r["id"]) in have or not ok(r):
            continue
        try:
            wh = core.where(r["id"], r["type"])
        except Exception:
            continue
        on = sorted(n for n, k in wh.get("providers", {}).items() if k == "flatrate" and n in mine)
        if not on:
            continue
        item = dict(r, providers=on, overview=wh.get("overview", "")[:2000], genres=wh.get("genres", []),
                    rating=wh.get("rating") or r["rating"], runtime=wh.get("runtime"), seasons=wh.get("seasons"), added=time.time())
        if F.passes(item, f, lang):
            out.append(item)
    core.data.write(fname, dict(built=time.time(), items=out, filters=f))
    return out


def disc_list(core, cat, fname=None, f=None):
    c = core.data.read(fname or disc_file(cat), {"built": 0, "items": []}) or {"built": 0, "items": []}
    f, lang = f or disc_cfg(core.config())[cat], F.my_lang(core.config())
    dm, lt, now = core.dismissed(), core.later(), time.time()
    skip = {(w["id"], w["type"]) for w in core.wishlist()} | {(q["id"], "tv") for q in core.queue()}

    def key(i):
        return "%s:%d" % (i["type"], i["id"])

    items = [dict(i, later=bool(lt.get(key(i), 0) > now - 30 * 86400)) for i in c["items"]
             if (i["id"], i["type"]) not in skip and ("disc:" + key(i)) not in dm and F.passes(i, f, lang)]
    gone = [dict(i, at=dm["disc:" + key(i)]) for i in c["items"] if ("disc:" + key(i)) in dm]
    return dict(built=c["built"], items=F.order(items, f), dismissed=gone, filters=f, pending=c.get("pending", False))


def _best(core, typ, disc, f, lang, add):
    """The best rated on your services (not only the popular ones), and the great ones the filters let through."""
    for page in (1, 2, 3):
        try:
            res = core.tmdb.get("/discover/" + typ, page=page, **dict(disc, sort_by="vote_average.desc")).get("results", [])
        except Exception:
            break
        for x in res:
            add(x, 4.0, "one of the best rated")
    if f.get("great"):
        loose = {k: v for k, v in disc.items() if k not in ("with_original_language", "without_genres")}
        loose["vote_average.gte"] = f["great"]
        for page in (1, 2):
            try:
                res = core.tmdb.get("/discover/" + typ, page=page, **dict(loose, sort_by="vote_average.desc")).get("results", [])
            except Exception:
                break
            for x in res:
                add(x, 5.0, "a great one")


def custom_file(cat):
    return "custom_%s.json" % cat


def listing(core, cat, custom=False):
    """What to Watch for one category: the everyday list, or the last "just for now" list."""
    if custom:
        c = core.data.read(custom_file(cat), None) or {}
        f = c.get("filters")
        if not f:
            return dict(built=0, items=[], dismissed=[], filters=disc_cfg(core.config())[cat], custom=True)
        kw = dict(fname=custom_file(cat), f=f)
    else:
        kw = {}
    if cat == "movies":
        out = suggestions(core, **kw)
    elif cat == "series":
        out = shows(core, **kw)
    else:
        out = disc_list(core, cat, **kw)
    out["custom"] = custom
    return out


def build_one(core, cat, f=None, custom=False, fresh=False):
    """Build one category's list. custom=True builds a one-off list with filters `f` and leaves the defaults alone."""
    fname = custom_file(cat) if custom else None
    if custom:
        core.data.write(fname, dict(built=0, items=[], filters=f, pending=True))
    if cat == "movies":
        return suggest_build(core, f, fname or "suggestions.json")
    if cat == "series":
        return shows_build(core, f, fname or "show_suggestions.json", fresh=fresh or custom)
    return disc_build(core, cat, f, fname, fresh=fresh or custom)


def build_all(core, only=None, fresh=False):
    if not core.tmdb.configured:
        return
    for cat, d in disc_cfg(core.config()).items():
        if only and cat != only:
            continue
        if not d["on"] and not only:
            continue
        try:
            build_one(core, cat, fresh=fresh)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("building %s picks failed", cat)
