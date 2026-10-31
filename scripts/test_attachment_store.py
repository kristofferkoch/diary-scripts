"""Security tests for contained attachment reads and writes."""
from __future__ import annotations

import pytest

from scripts.attachment_store import (
    existing_attachment_path,
    find_attachment,
    read_attachment,
    read_attachment_path,
    validate_attachment_id,
    write_attachment,
)


@pytest.mark.parametrize("value", ["blob.csv", "a1", "att-2", "A_b-c.9"])
def test_attachment_ids_preserve_legitimate_values(value):
    assert validate_attachment_id(value) == value


@pytest.mark.parametrize(
    "value",
    [None, 1, True, "", ".", "..", "../outside", "/absolute", "dir/blob",
     "back\\slash", "white space", "unicode-æ"],
)
def test_attachment_ids_reject_ambiguous_ids_and_paths(value):
    with pytest.raises(ValueError):
        validate_attachment_id(value)


def test_legitimate_nested_write_read_and_find(tmp_path):
    root = tmp_path / "store"
    path = write_attachment(root, "message-1", "plan.pdf", b"new")
    assert path == root / "message-1" / "plan.pdf"
    assert path.read_bytes() == b"new"
    assert find_attachment(root, "message-1", "plan.pdf") == path
    assert read_attachment_path(root, path) == b"new"
    assert read_attachment_path(root, "message-1/plan.pdf") == b"new"
    assert existing_attachment_path(root, path) == path


def test_zero_length_existing_file_is_not_a_completed_download(tmp_path):
    root = tmp_path / "store"
    path = write_attachment(root, "message-1", "plan.pdf", b"")
    assert find_attachment(root, "message-1", "plan.pdf") is None
    write_attachment(root, "message-1", "plan.pdf", b"complete")
    assert path.read_bytes() == b"complete"


@pytest.mark.parametrize("container_id", ["../outside", "/absolute", ".", ".."])
def test_write_rejects_container_traversal_before_creating_anything(tmp_path, container_id):
    root = tmp_path / "store"
    with pytest.raises(ValueError):
        write_attachment(root, container_id, "plan.pdf", b"new")
    assert not root.exists()
    assert not (tmp_path / "outside").exists()


def test_write_rejects_symlinked_store_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "store"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        write_attachment(root, "message-1", "plan.pdf", b"new")
    assert not (outside / "message-1").exists()


def test_write_rejects_symlinked_container(tmp_path):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "message-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        write_attachment(root, "message-1", "plan.pdf", b"new")
    assert not (outside / "plan.pdf").exists()


def test_write_rejects_symlinked_target_without_truncating_it(tmp_path):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    (root / "message-1").mkdir(parents=True)
    outside.write_bytes(b"original")
    (root / "message-1" / "plan.pdf").symlink_to(outside)
    with pytest.raises(OSError):
        write_attachment(root, "message-1", "plan.pdf", b"new")
    assert outside.read_bytes() == b"original"


def test_no_overwrite_does_not_follow_a_symlink_collision(tmp_path):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.write_bytes(b"original")
    (root / "plan.pdf").symlink_to(outside)
    with pytest.raises(FileExistsError):
        write_attachment(root, None, "plan.pdf", b"new", overwrite=False)
    assert outside.read_bytes() == b"original"


def test_read_rejects_symlinked_root_and_target(tmp_path):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.write_bytes(b"secret")
    (root / "blob").symlink_to(outside)
    with pytest.raises(OSError):
        read_attachment(root, "blob")
    linked_root = tmp_path / "linked-store"
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        read_attachment(linked_root, "outside")


def test_recorded_path_rejects_escape_and_symlinks(tmp_path):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    (root / "message-1").mkdir(parents=True)
    outside.write_bytes(b"secret")
    (root / "message-1" / "linked").symlink_to(outside)
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    for path in (outside, "../outside", root / "message-1" / "linked",
                 root / "linked-dir" / "outside"):
        with pytest.raises((OSError, ValueError)):
            read_attachment_path(root, path)
        assert existing_attachment_path(root, path) is None


def test_read_size_cap(tmp_path):
    write_attachment(tmp_path, None, "blob", b"12345")
    assert read_attachment(tmp_path, "blob", 5) == b"12345"
    with pytest.raises(ValueError):
        read_attachment(tmp_path, "blob", 4)
