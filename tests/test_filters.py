"""Discover filters: rating, ratings count, years, skipped genres, language, the great-ones exception, one-off lists."""
import time

from fakes import FakeTMDB
from strivo import filters as F
from strivo import picks
from strivo.core import Core

BASE = dict(min_rating=8.0, min_votes=500, years=0, skip_genres=[], langs="any", great=0.0, sort="fit")


def item(**kw):
    return dict(dict(rating=8.6, votes=5000, year="2020", genres=["Drama"], lang="en"), **kw)


def test_rating_votes_and_years():
    assert F.passes(item(), BASE, "en")
    assert not F.passes(item(rating=7.9), BASE, "en")
    assert not F.passes(item(votes=100), BASE, "en")
    old = dict(BASE, years=3)
    assert not F.passes(item(year="1999"), old, "en")
    assert F.passes(item(year=time.strftime("%Y")), old, "en")


def test_skipped_genres_language_and_the_great_ones():
    f = dict(BASE, skip_genres=["Animation", "Science Fiction"], langs="mine")
    assert not F.passes(item(genres=["Animation", "Family"]), f, "en")
    assert not F.passes(item(genres=["Sci-Fi & Fantasy"]), f, "en")      # TV's combined genre names count too
    assert not F.passes(item(lang="ko"), f, "en")
    assert F.passes(item(lang=""), f, "en")                              # unknown language isn't held against it
    g = dict(f, great=8.5)
    assert F.passes(item(genres=["Animation"], lang="ja", rating=8.6), g, "en")   # a great one gets through
    assert not F.passes(item(genres=["Animation"], rating=8.4), g, "en")


def test_clean_rejects_junk():
    f = F.clean(dict(min_rating="99", min_votes=-5, years="x", skip_genres=["Drama", "<script>"], langs="klingon",
                     great=3, sort="random"), BASE)
    assert f["min_rating"] == 9.5 and f["min_votes"] == 0 and f["years"] == 0
    assert f["skip_genres"] == ["Drama"] and f["langs"] == "any" and f["great"] == 5.0 and f["sort"] == "fit"


def test_order():
    xs = [item(rating=8.1, year="2024"), item(rating=9.0, year="2001", later=True), item(rating=8.7, year="2010")]
    assert [x["rating"] for x in F.order(xs, dict(BASE, sort="rating"))] == [8.7, 8.1, 9.0]   # Later stays last
    assert [x["year"] for x in F.order(xs, dict(BASE, sort="new"))] == ["2024", "2010", "2001"]


def _core(data, tmdb):
    data.set_secret("tmdb_key", "k")
    core = Core(data, transport=tmdb)
    core.settings_save({"my_services": ["Netflix"]})
    return core


def test_saved_filters_round_trip_and_reach_tmdb(data, tmdb):
    core = _core(data, tmdb)
    s = core.settings_save({"discover": {"movies": dict(min_rating=8.5, min_votes=1000, skip_genres=["Drama"],
                                                       langs="mine", great=8.8, sort="rating")}})
    m = s["discover"]["movies"]
    assert (m["min_rating"], m["min_votes"], m["skip_genres"], m["langs"], m["great"], m["sort"]) == \
        (8.5, 1000, ["Drama"], "mine", 8.8, "rating")
    assert "Animation" in s["genres"] and s["my_lang"] == "en"
    picks.suggest_build(core)
    q = [qq for p, qq, _ in tmdb.calls if p == "/discover/movie"]
    assert any(x.get("without_genres") == "18" and x.get("with_original_language") == "en" for x in q)
    assert any(x.get("sort_by") == "vote_average.desc" for x in q)                      # best rated, not only popular
    assert any(x.get("vote_average.gte") == "8.8" and "without_genres" not in x for x in q)   # the great ones
    assert picks.suggestions(core)["items"] == []          # the only movie is a Drama rated 8.1


def test_defaults_apply_at_once_and_rebuild_refills(data, tmdb):
    core = _core(data, tmdb)
    picks.suggest_build(core)
    assert [i["title"] for i in picks.suggestions(core)["items"]] == ["Picked Movie"]
    core.settings_save({"discover": {"movies": {"min_rating": 8.5}}})
    assert picks.suggestions(core)["items"] == []          # no rebuild needed to hide what no longer fits
    core.settings_save({"discover": {"movies": {"min_rating": 8.0, "skip_genres": ["Drama"], "great": 8.0}}})
    assert picks.suggestions(core)["items"]                # 8.1 counts as great: it gets past the skipped genre


class ForeignTMDB(FakeTMDB):
    def __call__(self, url, headers):
        r = super().__call__(url, headers)
        if "/discover/" in url:
            for x in r["results"]:
                x["original_language"] = "fr"
        return r


def test_one_off_list_leaves_the_defaults_alone(data):
    tmdb = ForeignTMDB()
    core = _core(data, tmdb)
    picks.build_all(core)
    assert picks.listing(core, "docs")["items"]
    assert picks.listing(core, "docs", custom=True) == dict(built=0, items=[], dismissed=[],
                                                            filters=core.settings_view()["discover"]["docs"],
                                                            custom=True)
    f = F.clean({"langs": "mine"}, core.settings_view()["discover"]["docs"])
    picks.build_one(core, "docs", f, custom=True)
    one = picks.listing(core, "docs", custom=True)
    assert one["custom"] and one["filters"]["langs"] == "mine" and one["items"] == []      # French only
    assert picks.listing(core, "docs")["items"]                                            # everyday list untouched
    assert core.settings_view()["discover"]["docs"]["langs"] == "any"
    picks.build_one(core, "series", F.clean({"min_rating": 9}, core.settings_view()["discover"]["series"]), custom=True)
    assert picks.listing(core, "series", custom=True)["items"] == []
    assert picks.listing(core, "series")["items"]


def test_fresh_rebuild_drops_what_no_longer_fits(data, tmdb):
    core = _core(data, tmdb)
    picks.shows_build(core)
    assert data.read("show_suggestions.json")["items"]
    core.settings_save({"discover": {"series": {"min_votes": 5000}}})
    picks.build_all(core, "series", fresh=True)
    assert data.read("show_suggestions.json")["items"] == []
