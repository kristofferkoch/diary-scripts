-- Shopping list — a standing checklist the user works through in the store.
-- Backs the /shopping/ page on server: items grouped by a small set
-- of coarse categories (Netthandel always last), each with a checkbox.
-- Checks persist and are reversible on the web; the sjekk routine sweeps
-- checked-off (bought) items out of the list with `scripts/shopping.py sweep`.
--
-- Category is stored as free text, NOT a CHECK constraint: the canonical
-- ordered category list lives in `mail_reader/shopping.py` (CATEGORIES) so
-- it can be reordered/extended without a migration. The app validates
-- category on every write; only manual SQL can introduce an off-list value,
-- and the reader groups any such stray just before Netthandel.
--
-- Apply with:  psql -d mailvec -f migrations/012_shopping_list.sql

CREATE TABLE IF NOT EXISTS shopping_items (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT        NOT NULL,
    category    TEXT        NOT NULL DEFAULT 'Annet',
    checked     BOOLEAN     NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The page lists every item ordered within its category by insertion time
-- (checked items keep their place, greyed); this index serves that scan.
CREATE INDEX IF NOT EXISTS shopping_items_order_idx
    ON shopping_items (category, created_at);
