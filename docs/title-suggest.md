# Title suggestion

ntasker can derive a short task title from the description text, so quick brain-dump descriptions don't need
hand-written titles.

## How it works

`POST /api/title-suggest` with `{"text": "..."}` returns `{"title": "...", "language": "de"}`.

Pipeline (`src/ntasker/titlegen.py`):

1. **Language detection** -- `langdetect` (seeded, deterministic) picks the ISO-639-1 code; fallback `en`.
2. **YAKE** extracts candidate phrases (up to trigrams) with a score (lower = better) using the detected
   language's stopwords.
3. **TextRank** (`summa`) ranks central single words. Each YAKE phrase's score is divided by
   `1 + |phrase words TextRank also ranked|`, so both extractors vote together.
4. **Cut-phrase repair** -- a candidate whose occurrence in the text is immediately followed by a word
   character was truncated by the n-gram window (e.g. `effectiveTime-Präzedenz bei seltener` [Re-Statement
   ...]); its dangling tail is dropped at the phrase's last stopword. Phrases ending at a clause boundary
   stay untouched.
5. The best non-overlapping phrases are joined with an en dash until 60 characters are reached, ordered by
   their position in the text (headline reading order, not rank order). The first letter is only
   capitalized for plain lowercase words -- camelCase identifiers (`effectiveTime`) are left as-is.
   Degenerate input (too short for extraction) falls back to the trimmed first line.

The heavy imports (numpy/scipy via yake/summa) are loaded lazily on the first request, not at server start.

## UI

- **Create form:** while the title field is empty (or was auto-filled), typing in the description
  (debounced, 700 ms) keeps the title in sync. Typing into the title field turns the automation off;
  clearing the title turns it back on. A ✨ button next to the title regenerates on demand.
- **Edit modal:** ✨ button only -- existing titles are never overwritten automatically.
