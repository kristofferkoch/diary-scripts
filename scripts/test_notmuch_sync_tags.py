"""Tests for the discovery helpers in scripts/notmuch_sync_tags.py.

The original bug (2026-05-27): mail filtered by Proton to a user-created
folder `Folders/Botmail` kept showing up with `tag:inbox` because the
post-new hook only knew about INBOX/Spam/Archive/Sent/Drafts/Trash and
`Labels/*`. These tests pin the contract that `Folders/<X>` is also
discovered and strips `inbox` (canonical semantics, not additive like
labels).
"""
from __future__ import annotations

import pathlib

import pytest

from scripts import notmuch_sync_tags as sut


def _make_maildir(root: pathlib.Path, *names: str) -> None:
    for name in names:
        (root / name / "cur").mkdir(parents=True, exist_ok=True)
        (root / name / "new").mkdir(parents=True, exist_ok=True)
        (root / name / "tmp").mkdir(parents=True, exist_ok=True)


def test_discover_folder_rules_strips_inbox(tmp_path, monkeypatch):
    _make_maildir(tmp_path, "Folders/Botmail", "Folders/Faktura")
    monkeypatch.setattr(sut, "MAILDIR_ROOT", tmp_path)

    rules = sut.discover_folder_rules()

    assert rules == [
        ("Folders/Botmail", "botmail", ("inbox",)),
        ("Folders/Faktura", "faktura", ("inbox",)),
    ]


def test_discover_folder_rules_missing_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sut, "MAILDIR_ROOT", tmp_path)
    assert sut.discover_folder_rules() == []


def test_discover_folder_rules_skips_entries_without_cur(tmp_path, monkeypatch):
    (tmp_path / "Folders" / "Botmail" / "cur").mkdir(parents=True)
    (tmp_path / "Folders" / "Halfbaked").mkdir(parents=True)  # no cur/
    monkeypatch.setattr(sut, "MAILDIR_ROOT", tmp_path)

    rules = sut.discover_folder_rules()

    assert rules == [("Folders/Botmail", "botmail", ("inbox",))]


def test_discover_label_rules_does_not_strip_inbox(tmp_path, monkeypatch):
    """Labels are additive in Proton — keep inbox semantics untouched."""
    _make_maildir(tmp_path, "Labels/Familie", "Labels/Important")
    monkeypatch.setattr(sut, "MAILDIR_ROOT", tmp_path)

    rules = sut.discover_label_rules()

    assert rules == [
        ("Labels/Familie", "Familie", ()),
        ("Labels/Important", "Important", ()),
    ]


def test_discover_label_rules_skips_imap_namespace(tmp_path, monkeypatch):
    _make_maildir(tmp_path, "Labels/Familie", "Labels/[Imap]/Sent")
    monkeypatch.setattr(sut, "MAILDIR_ROOT", tmp_path)

    rules = sut.discover_label_rules()

    assert ("Labels/Familie", "Familie", ()) in rules
    assert not any(name.startswith("Labels/[Imap]") for name, _, _ in rules)


def test_folder_tag_normalization(tmp_path, monkeypatch):
    _make_maildir(tmp_path, "Folders/Bot Mail!", "Folders/Two-Words")
    monkeypatch.setattr(sut, "MAILDIR_ROOT", tmp_path)

    rules = sut.discover_folder_rules()
    tags = [tag for _, tag, _ in rules]

    assert "bot_mail" in tags
    assert "two_words" in tags
