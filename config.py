#!/usr/bin/env python3
"""Central configuration loader for the job-search tracker.

There used to be no config module: every entry point (app.py, ingest.py,
rescore_viability.py, compare_scoring.py, import_linkedin.py) independently did
``tomllib.load(config.toml)`` and read a flat dict. This module is the single loader
they now share, and it introduces multi-search support *without* changing the
single-search behavior.

One canonical config (the single path passed as today), in one of two shapes:

  **Path A (single search, status quo):** no ``[[searches]]`` table. Every stanza
  lives inline. Wrapped as one implicit :class:`Search` with id ``__default__``.

  **Path B (multi-search):** the canonical config holds the globals common to every
  search (``[basics]``, ``[company_aliases]``, and optionally ``[ai]``/``[descriptions]``…)
  plus a ``[[searches]]`` manifest. Each entry names a per-search file that holds only
  the per-search stanzas (``[viability]``, ``[[tasks]]``, ``[labels]``). A search's
  *effective* config is the union of the canonical globals and that file; a search file
  may not redeclare a global stanza (hard error), so globals exist exactly once — which
  is why there is no cross-config consistency problem (one db_path, one alias namespace,
  one API key).

``Search.config`` is a flat dict shape-identical to today's single config (``[basics]``
flattened up to the top level), so ``ai_config.*`` and ``viability.scoring_hash_for_config``
consume it unchanged.
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Search id used for (a) the implicit single search in Path A and (b) the backfill of
# pre-multi-search DB rows. A named search can later adopt those rows via adopts_legacy.
DEFAULT_SEARCH_ID = "__default__"
DEFAULT_SEARCH_NAME = "default"

# Top-level scalars that historically sat bare at the top of config.toml and now belong
# under [basics]. Used only by the --fixbasics migrator to know what to move; the loader
# itself treats every non-table top-level key as a shared global regardless of this list.
_BARE_GLOBAL_KEYS = (
    "api_token", "username", "db_path", "uploads_dir",
    "reset_on_change", "auto_ghost", "auto_ghost_days",
    "fuzzy_dedup", "fuzzy_desc_threshold", "fuzzy_title_threshold",
    "fuzzy_title_word_threshold", "fuzzy_title_id_gate", "inherit_canonical_status",
)


class ConfigError(Exception):
    """A malformed or contradictory configuration: a bad ``[[searches]]`` entry, a search
    file redeclaring a global stanza, conflicting label display names, etc. Entry points
    print the message and exit."""


@dataclass
class Search:
    """One job search: a stable id (stored in every per-lens DB row), a display name,
    its fully-merged effective config (canonical globals + the search's own stanzas), and
    its ingest tasks. ``adopts_legacy`` marks the one search that absorbs pre-split
    ``__default__`` rows on first migration."""
    id: str
    name: str
    config: dict
    tasks: list[dict] = field(default_factory=list)
    adopts_legacy: bool = False


@dataclass
class AppConfig:
    """The loaded configuration for the whole app: every :class:`Search`, the shared
    globals, and a few derived conveniences (label-name union, the file list to watch for
    mtime-based reloads, the file that owns ``[company_aliases]``)."""
    searches: list[Search]
    shared: dict                 # flattened canonical globals (no [[searches]] / [basics] table)
    source_files: list[Path]     # canonical + every per-search file, for mtime watching
    aliases_path: Path           # always the canonical config (where [company_aliases] lives)
    label_names: dict[str, str]

    @property
    def db_path(self) -> str:
        return self.shared.get("db_path", "jobs.db")

    @property
    def uploads_dir(self) -> str:
        return self.shared.get("uploads_dir", "uploads")

    @property
    def api_token(self) -> str | None:
        return self.shared.get("api_token")

    @property
    def username(self) -> str | None:
        return self.shared.get("username")

    def get_search(self, search_id: str) -> Search | None:
        return next((s for s in self.searches if s.id == search_id), None)

    def default_search(self) -> Search:
        """The single (Path A) / first (Path B) search — the one single-search callers
        operate on until they are taught to loop ``self.searches``."""
        return self.searches[0]

    @property
    def adopter(self) -> Search | None:
        """The search (if any) marked ``adopts_legacy = true`` — the one that absorbs
        pre-split ``__default__`` rows on migration."""
        return next((s for s in self.searches if s.adopts_legacy), None)

    @property
    def is_multi_search(self) -> bool:
        """True in Path B (a real ``[[searches]]`` manifest). False for the implicit single
        ``__default__`` search (Path A) — where legacy adoption is meaningless (its rows already
        live under the only search) and must not warn."""
        return not (len(self.searches) == 1 and self.searches[0].id == DEFAULT_SEARCH_ID)


def _flatten_basics(canonical: dict, source: Path) -> dict:
    """Return the shared globals as a flat dict: the ``[basics]`` table merged up to the top
    level alongside the global tables (``[ai]``, ``[company_aliases]``, …). Falls back to
    today's bare top-level scalars — with a deprecation warning — when ``[basics]`` is absent,
    so an existing ``config.toml`` keeps working. Errors if a key is declared both under
    ``[basics]`` and bare at the top level (ambiguous)."""
    rest = {k: v for k, v in canonical.items() if k not in ("basics", "searches")}
    basics = canonical.get("basics")
    if basics is None:
        # Legacy shape: bare top-level scalars. Still supported; nudge toward [basics] so the
        # warning is visible in CLI logs and app startup (kept a plain stderr line rather than
        # warnings.warn so `pytest -W error` setups don't turn the nudge into a hard failure).
        print(f"DEPRECATION: {source}: put shared settings (db_path, api_token, …) under a "
              "[basics] table; bare top-level keys are deprecated. Run `ingest.py --fixbasics "
              f"--config {source}` to migrate.", file=sys.stderr)
        return rest
    if not isinstance(basics, dict):
        raise ConfigError(f"{source}: [basics] must be a table.")
    dupes = set(basics) & set(rest)
    if dupes:
        raise ConfigError(
            f"{source}: {sorted(dupes)} declared both under [basics] and bare at the top "
            "level — put each in exactly one place.")
    return {**basics, **rest}


def _build_label_names(searches: list[Search]) -> dict[str, str]:
    """Union each search's ``[labels]`` map for the UI, with the legacy per-task ``display``
    fallback. A label key mapping to two different display names across searches is a hard
    error (the UI can't show a key two ways)."""
    names: dict[str, str] = {}
    for s in searches:
        for k, v in (s.config.get("labels", {}) or {}).items():
            if k in names and names[k] != v:
                raise ConfigError(
                    f"label {k!r} maps to both {names[k]!r} and {v!r} across searches — "
                    "give it one display name.")
            names[k] = v
        for t in s.tasks:
            lbl = t.get("label")
            if lbl and lbl not in names and "display" in t:
                names[lbl] = t["display"]
    return names


def load_config(path) -> AppConfig:
    """Load the canonical config at ``path`` into an :class:`AppConfig`.

    Path A (no ``[[searches]]``) yields one implicit ``__default__`` search whose config is
    the whole (flattened) canonical dict — byte-identical behavior to the pre-multi-search
    app. Path B yields one search per manifest entry, each config being the canonical globals
    merged with that search's file. Raises :class:`ConfigError` on any malformed input."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "rb") as f:
        canonical = tomllib.load(f)

    shared = _flatten_basics(canonical, path)
    manifest = canonical.get("searches")
    source_files = [path]

    if not manifest:
        # Path A: the whole config is one implicit search.
        searches = [Search(id=DEFAULT_SEARCH_ID, name=DEFAULT_SEARCH_NAME,
                           config=shared, tasks=list(shared.get("tasks", []) or []))]
    else:
        if not isinstance(manifest, list):
            raise ConfigError(f"{path}: [[searches]] must be an array of tables.")
        searches = []
        seen_ids: set[str] = set()
        adopters: list[str] = []
        for i, entry in enumerate(manifest):
            sid = str(entry.get("search_id", "")).strip()
            if not sid:
                raise ConfigError(
                    f"{path}: [[searches]] entry #{i + 1} is missing a non-empty search_id.")
            if sid == DEFAULT_SEARCH_ID:
                raise ConfigError(f"{path}: search_id {DEFAULT_SEARCH_ID!r} is reserved.")
            if sid in seen_ids:
                raise ConfigError(f"{path}: duplicate search_id {sid!r}.")
            seen_ids.add(sid)
            name = str(entry.get("search_name") or sid)
            rel = entry.get("search_config_file")
            if not rel:
                raise ConfigError(f"{path}: search {sid!r} is missing search_config_file.")
            sfile = path.parent / rel
            if not sfile.exists():
                raise ConfigError(f"{path}: search {sid!r} config file not found: {sfile}")
            with open(sfile, "rb") as sf:
                sdict = tomllib.load(sf)
            if "searches" in sdict or "basics" in sdict:
                raise ConfigError(
                    f"{sfile}: a per-search file must not contain [[searches]] or [basics] "
                    "(those live only in the canonical config).")
            # A search file may not redeclare a global stanza — globals live once, in canonical.
            clash = set(sdict) & set(shared)
            if clash:
                raise ConfigError(
                    f"{sfile}: search {sid!r} redeclares global stanza(s) {sorted(clash)} — "
                    f"those belong only in {path.name}.")
            effective = {**shared, **sdict}
            adopts = bool(entry.get("adopts_legacy", False))
            if adopts:
                adopters.append(sid)
            searches.append(Search(id=sid, name=name, config=effective,
                                   tasks=list(effective.get("tasks", []) or []),
                                   adopts_legacy=adopts))
            source_files.append(sfile)
        if len(adopters) > 1:
            raise ConfigError(
                f"{path}: at most one search may set adopts_legacy=true (got {adopters}).")

    return AppConfig(searches=searches, shared=shared, source_files=source_files,
                     aliases_path=path, label_names=_build_label_names(searches))


# ── --fixbasics migrator ─────────────────────────────────────────────────────────────────

def migrate_config_to_basics(path) -> tuple[bool, str]:
    """Rewrite ``path`` in place, moving bare top-level scalars under a ``[basics]`` table
    while preserving comments and the rest of the file. Returns ``(changed, message)``.

    No-op ``(False, …)`` when ``[basics]`` already exists or there are no bare top-level
    settings. Before writing it re-parses the result and verifies the effective globals are
    unchanged (a safety guard mirroring the alias writer), then writes atomically."""
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    if "basics" in parsed:
        return False, f"{path}: [basics] already present; nothing to migrate."

    lines = original.splitlines(keepends=True)
    nl = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    # Bare top-level assignments can only appear before the first table header in TOML.
    first_header = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")),
                        len(lines))
    assign_re = re.compile(r"^\s*[^#\s]")  # a non-comment, non-blank line = an assignment here
    first_assign = next((i for i in range(first_header) if assign_re.match(lines[i])), None)
    if first_assign is None:
        return False, f"{path}: no bare top-level settings to migrate."

    # Insert [basics] just above the first bare assignment: leading file-level comments stay
    # above it; every bare key (and any interleaved comments) below it fall under [basics].
    new_text = "".join(lines[:first_assign] + [f"[basics]{nl}"] + lines[first_assign:])

    # Guard: the rewrite must parse and yield the same effective globals as before.
    new_parsed = tomllib.loads(new_text)
    before = {k: v for k, v in parsed.items() if k != "searches"}
    after = {**new_parsed.get("basics", {}),
             **{k: v for k, v in new_parsed.items() if k not in ("basics", "searches")}}
    if after != before:
        return False, f"{path}: refused — rewrite would change the effective config."

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return True, f"{path}: moved bare top-level settings under [basics]."
