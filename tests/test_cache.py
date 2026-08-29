"""The cache's job is to be an optimisation that can never become a failure.
These tests pin the two ways it previously could: a corrupt file raising on
every subsequent read, and a schema change turning yesterday's entries into
500s."""
import json
import time

from app.cache import SCHEMA_VERSION, DiskCache


def test_round_trip_and_age(tmp_path):
    cache = DiskCache(str(tmp_path))
    cache.set("someone", {"fetched_at": "now", "profile": {}})
    entry = cache.get_entry("someone")
    assert entry is not None
    assert entry.value["fetched_at"] == "now"
    assert entry.age_seconds == 0


def test_missing_key_is_a_miss(tmp_path):
    assert DiskCache(str(tmp_path)).get("nobody") is None


def test_corrupt_entry_is_a_miss_not_an_exception(tmp_path):
    """The production failure this prevents: a file truncated by a crash
    mid-write used to raise from json.loads on *every* later request, so one
    profile stayed permanently 500 until someone deleted the file by hand."""
    cache = DiskCache(str(tmp_path))
    cache.set("someone", {"profile": {}})
    (tmp_path / "someone.json").write_text('{"v": 2, "cached_at": 17880')
    assert cache.get("someone") is None


def test_entry_from_an_older_schema_is_a_miss(tmp_path):
    cache = DiskCache(str(tmp_path))
    (tmp_path / "someone.json").write_text(
        json.dumps({"cached_at": time.time(), "value": {"old": "shape"}})
    )
    assert cache.get("someone") is None


def test_expired_entry_is_a_miss_but_readable_when_asked_for(tmp_path):
    cache = DiskCache(str(tmp_path), ttl_seconds=1)
    cache.set("someone", {"profile": {}})
    (tmp_path / "someone.json").write_text(
        json.dumps({"v": SCHEMA_VERSION, "cached_at": time.time() - 3600, "value": {"profile": {}}})
    )
    assert cache.get("someone") is None
    stale = cache.get_entry("someone", include_expired=True)
    assert stale is not None and stale.age_seconds >= 3600


def test_write_leaves_no_temp_files_behind(tmp_path):
    cache = DiskCache(str(tmp_path))
    cache.set("someone", {"profile": {}})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["someone.json"]
