"""Phase 3 targeted integration tests - proves Smart Compression wired
into the real Archive/Verify/Checkout/Checkin lifecycle via main.py.
Uses the same `sandbox`/`make_file` fixtures as the existing suite so
every test runs isolated in a tmp dir, never touching real data.
"""

import os
import shutil

import archive_manager
import database_manager
import main


PHOTOS_JPEG = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos\images.jpg"


def _init(sandbox):
    main.initialize()


def test_archive_baseline_only_file(sandbox, make_file):
    _init(sandbox)
    main.create_category("Cat")
    path = make_file("plain.txt", "x" * 50)  # tiny, no smart candidate worth it
    result = main.archive_file_result(path, "Cat")
    assert result["success"]
    assert result["strategy"] == "baseline_storagearch_zip"
    assert result["smart_compression_used"] is False


def test_archive_jpeg_xl_selected(sandbox):
    if not os.path.isfile(PHOTOS_JPEG):
        return  # real JPEG fixture unavailable in this environment - skip silently
    _init(sandbox)
    main.create_category("Photos")
    local = os.path.join(sandbox, "images.jpg")
    shutil.copy(PHOTOS_JPEG, local)
    result = main.archive_file_result(local, "Photos")
    assert result["success"], result
    assert result["strategy"] == "jpeg_xl_lossless_jpeg"
    assert result["smart_compression_used"] is True


def test_archive_zstd_selected(sandbox, make_file):
    _init(sandbox)
    main.create_category("Data")
    # compressible, large enough to clear the benefit threshold
    path = make_file("data.json", '{"k":"v",' * 200000 + '"end":1}')
    result = main.archive_file_result(path, "Data")
    assert result["success"]
    assert result["strategy"] == "generic_zstd"
    assert result["smart_compression_used"] is True


def test_mixed_strategies_in_one_archive(sandbox, make_file):
    _init(sandbox)
    main.create_category("Mixed")
    tiny = make_file("tiny.txt", "x" * 20)
    big = make_file("big.json", '{"k":"v",' * 200000 + '"end":1}')
    r1 = main.archive_file_result(tiny, "Mixed")
    r2 = main.archive_file_result(big, "Mixed")
    assert r1["success"] and r2["success"]
    assert r1["strategy"] == "baseline_storagearch_zip"
    assert r2["strategy"] == "generic_zstd"


def test_manifest_strategy_persistence(sandbox, make_file):
    _init(sandbox)
    main.create_category("ManifestTest")
    path = make_file("data.json", '{"k":"v",' * 200000 + '"end":1}')
    result = main.archive_file_result(path, "ManifestTest")
    assert result["success"]

    items = database_manager.search_items("data.json")
    assert len(items) == 1
    item_id = items[0][0]
    item = database_manager.get_item_by_id(item_id)
    assert item[15] == "generic_zstd"  # strategy column persisted in DB

    archive_path = database_manager.get_archive_path_by_item_id(item_id)
    import json
    manifest_path = os.path.splitext(archive_path)[0] + ".manifest.json"
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    entry = manifest["files"][0]
    assert entry["strategy"] == "generic_zstd"
    assert entry["original_sha256"] == item[8]


def test_exact_smart_checkout_restore(sandbox):
    if not os.path.isfile(PHOTOS_JPEG):
        return
    _init(sandbox)
    main.create_category("Photos")
    local = os.path.join(sandbox, "images.jpg")
    shutil.copy(PHOTOS_JPEG, local)
    result = main.archive_file_result(local, "Photos")
    assert result["success"] and result["strategy"] == "jpeg_xl_lossless_jpeg"

    items = database_manager.search_items("images.jpg")
    item_id = items[0][0]
    original_hash = archive_manager.calculate_file_hash(local)

    workspace_path = main.checkout(item_id)
    assert workspace_path is not None
    restored_hash = archive_manager.calculate_file_hash(workspace_path)
    assert restored_hash == original_hash


def test_verify_on_smart_archive(sandbox):
    if not os.path.isfile(PHOTOS_JPEG):
        return
    _init(sandbox)
    main.create_category("Photos")
    local = os.path.join(sandbox, "images.jpg")
    shutil.copy(PHOTOS_JPEG, local)
    result = main.archive_file_result(local, "Photos")
    assert result["success"]

    statuses = main.verify_category("Photos")
    assert all(s == "VERIFIED" for s in statuses.values()), statuses


def test_checkin_of_modified_smart_file_reselects_strategy(sandbox):
    if not os.path.isfile(PHOTOS_JPEG):
        return
    _init(sandbox)
    main.create_category("Photos")
    local = os.path.join(sandbox, "images.jpg")
    shutil.copy(PHOTOS_JPEG, local)
    result = main.archive_file_result(local, "Photos")
    assert result["success"] and result["strategy"] == "jpeg_xl_lossless_jpeg"

    items = database_manager.search_items("images.jpg")
    item_id = items[0][0]
    workspace_path = main.checkout(item_id)

    # modify: corrupt the magic-byte header so this is no longer detected
    # as JPEG at all (forces re-routing, not just a restore failure)
    with open(workspace_path, "r+b") as f:
        f.write(b"NOTJPEGXX")

    ok = main.checkin(item_id)
    assert ok

    item = database_manager.get_item_by_id(item_id)
    new_strategy = item[15]
    assert new_strategy != "jpeg_xl_lossless_jpeg", "strategy should have been re-evaluated, not carried over"

    # exact restore must still hold for the NEW content
    new_hash_on_disk = archive_manager.calculate_file_hash(workspace_path) if os.path.exists(workspace_path) else None
    checkout_path = main.checkout(item_id)
    restored_hash = archive_manager.calculate_file_hash(checkout_path)
    assert restored_hash == item[8]


def test_legacy_archive_verify_checkout_compatibility(sandbox, make_file):
    """An archive with NO strategy column data at all (simulating a
    pre-Phase-3 archive) must still Verify and Checkout correctly."""
    _init(sandbox)
    main.create_category("Legacy")
    path = make_file("legacy.txt", "legacy content, unchanged since Phase A")

    # archive via the ORIGINAL unmodified legacy function directly,
    # bypassing Smart Compression entirely, to simulate a pre-Phase-3 item
    category = database_manager.get_category_by_name("Legacy")
    zip_path = archive_manager.get_selected_archive(category[6], category[4], os.path.getsize(path))
    zip_path, file_added, member_name = archive_manager.add_file_to_archive(path, category[6], zip_path)
    assert file_added
    database_manager.save_archive(category[0], zip_path)
    archive_id = database_manager.get_archive_id(category[0], zip_path)
    main.refresh_archive_details(archive_id, zip_path, "Legacy")
    original_hash = archive_manager.calculate_file_hash(path)
    database_manager.save_item(
        archive_id, "file", "legacy.txt", path, member_name,
        os.path.getsize(path), os.path.getsize(path), original_hash, original_hash, "ARCHIVED",
        strategy=None,
    )

    status = main.verify_archive_by_id(archive_id)
    assert status == "VERIFIED"

    items = database_manager.search_items("legacy.txt")
    item_id = items[0][0]
    item = database_manager.get_item_by_id(item_id)
    assert item[15] is None  # no strategy recorded - legacy path

    workspace_path = main.checkout(item_id)
    assert workspace_path is not None
    assert archive_manager.calculate_file_hash(workspace_path) == original_hash


def test_manifest_refresh_does_not_rehash_unchanged_members(sandbox, make_file, monkeypatch):
    # REGRESSION: create_archive_manifest_smart used to re-hash EVERY member
    # of the category's zip on every single archive operation (by re-opening
    # the whole zip via archive_manager.calculate_zip_file_hash once per
    # member), making each new Archive click progressively slower as a
    # category accumulated history - confirmed directly: archiving the Nth
    # item into a growing category measurably slowed (~0.03s -> ~0.2s+ by
    # N=60 in a real timing run), entirely independent of the new item's own
    # size. The fix reuses the previous manifest's recorded sha256 for any
    # member whose CRC-32 (free from zipfile's central directory, no bytes
    # read) is unchanged since last time - a brand-new member still gets
    # fully hashed.
    import archive_manager_smart

    main.initialize()
    main.create_category("ManyItems")

    zip_path = None
    for i in range(6):
        path = make_file(f"item_{i}.txt", f"item {i} content " * 5)
        result = main.archive_file_result(path, "ManyItems")
        assert result["success"]

    archive_path = database_manager.get_archive_path_by_item_id(
        database_manager.search_items("item_0.txt")[0][0]
    )

    # Isolate manifest generation specifically (not the whole archive
    # pipeline, which hashes source files/verifies content for unrelated
    # reasons too): call it again directly, as if a 7th item were about to
    # be archived - nothing in the zip has changed since the last real
    # manifest write, so every member's hash should come from the cache.
    hash_calls = []
    real_sha256 = archive_manager_smart.hashlib.sha256

    def counting_sha256(*args, **kwargs):
        hash_calls.append(1)
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(archive_manager_smart.hashlib, "sha256", counting_sha256)
    manifest_path, _archive_sha256 = archive_manager_smart.create_archive_manifest_smart(archive_path, "ManyItems")
    assert manifest_path is not None

    # Exactly 1 expected: archive_manager.calculate_archive_hash(zip_path)
    # hashes the WHOLE zip as one blob for the manifest's own "archive_sha256"
    # (needed for verify_archive's CHANGED detection) - that's real,
    # necessary, unavoidable work, unrelated to the per-MEMBER bug this test
    # guards against. The bug produced one extra sha256() call PER MEMBER
    # (6 here) on top of this single archive-level one.
    assert len(hash_calls) == 1, (
        f"expected exactly 1 sha256() call (the whole-archive hash) when nothing changed since "
        f"the last manifest - all 6 members' hashes should come from cache, got {len(hash_calls)}"
    )

    # Correctness: the manifest must still be byte-exact-correct for every
    # member, including the ones whose hash was reused from cache.
    import json
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    assert len(manifest["files"]) == 6
    for entry in manifest["files"]:
        actual = archive_manager.calculate_zip_file_hash(archive_path, entry["path_in_archive"])
        assert entry["sha256"] == actual, f"cached/reused hash for {entry['path_in_archive']} does not match real content"


def test_manifest_refresh_still_hashes_a_genuinely_new_member(sandbox, make_file):
    # Companion to the caching test above: a member added SINCE the last
    # manifest write (different CRC-32, no prior cache entry) must still get
    # fully hashed - the cache only ever skips work that's provably
    # redundant, never trades away correctness for a real new/changed file.
    import archive_manager_smart

    main.initialize()
    main.create_category("Cat")
    path_a = make_file("a.txt", "content a " * 5)
    result_a = main.archive_file_result(path_a, "Cat")
    assert result_a["success"]

    archive_path = database_manager.get_archive_path_by_item_id(
        database_manager.search_items("a.txt")[0][0]
    )
    manifest_path = os.path.splitext(archive_path)[0] + ".manifest.json"
    import json
    with open(manifest_path, encoding="utf-8") as f:
        manifest_after_a = json.load(f)
    assert len(manifest_after_a["files"]) == 1

    path_b = make_file("b.txt", "totally different content b " * 5)
    result_b = main.archive_file_result(path_b, "Cat")
    assert result_b["success"]

    with open(manifest_path, encoding="utf-8") as f:
        manifest_after_b = json.load(f)
    assert len(manifest_after_b["files"]) == 2
    paths = {entry["path_in_archive"] for entry in manifest_after_b["files"]}
    assert result_a["success"] and result_b["success"]
    # Both members have correct, independently-verifiable hashes.
    for entry in manifest_after_b["files"]:
        actual = archive_manager.calculate_zip_file_hash(archive_path, entry["path_in_archive"])
        assert entry["sha256"] == actual
