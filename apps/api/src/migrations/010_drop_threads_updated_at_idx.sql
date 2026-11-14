-- 009 first shipped a threads (updated_at DESC, id DESC) index before paging moved to created_at.
-- The sort key is mutable, so that index could not serve the keyset and every write paid for it.
-- Fresh databases never create it; this drops it for databases that ran the first 009.
DROP INDEX IF EXISTS threads_updated_at_idx;
