# Retired: notes queue & shopping list

> Index: [scripts/](../README.md)

Removed 2026-10: the `/notes/` and `/shopping/` web pages, their routes
in `mail_reader/server.py`, `mail_reader/notes.py` / `note_images.py` /
`shopping.py`, and the `notes` / `shopping` CLIs. Capture moved to Signal
Note-to-Self (see [signal.md](signal.md)). The Postgres **tables stay**
— `notes_queue`, `note_attachments`, `shopping_items` (migrations
011–014) remain in `mailvec` and the historical data stays queryable.
