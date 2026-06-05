-- GPS location for note attachments — the "GPS as a signal" idea (IDEAS.md).
-- A phone photo's EXIF often carries where it was taken; note_images.process()
-- already preserves the EXIF on the stored web image, and now also parses the
-- GPS lat/lon out at upload time into these structured columns so the page can
-- show a "📍 sted" map link and a future query can ask "notater tatt i
-- nærheten av X" without re-parsing the JPEG.
--
-- Signed decimal degrees (lat in [-90,90], lon in [-180,180]); both NULL when
-- the photo had no usable fix (screenshots, location-off, share-sheet
-- stripping) — location is optional by nature.
--
-- Apply with:  psql -d mailvec -f migrations/014_note_attachment_gps.sql
ALTER TABLE note_attachments
    ADD COLUMN IF NOT EXISTS gps_lat DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS gps_lon DOUBLE PRECISION;
