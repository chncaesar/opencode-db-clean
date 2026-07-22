# ocdbc — OpenCode Database Cleaner

Safely analyze and shrink [OpenCode](https://github.com/anomalyco/opencode)'s SQLite database.

OpenCode stores all session history in a single SQLite database (`~/.local/share/opencode/opencode.db`). Over time, compaction and session archival leave behind **freelist pages** — free space inside the file that is never returned to the filesystem unless `auto_vacuum` is enabled and `VACUUM` is run. Databases routinely bloat to 1–2 GB, with >50% being dead air.

`ocdbc` provides two commands:

- **`analyze`** — read-only health report (no risk)
- **`vacuum`** — safe VACUUM sequence with backup, integrity checks, and `auto_vacuum = INCREMENTAL` enablement

## Installation

```bash
git clone https://github.com/chncaesar/opencode-db-clean.git
cd opencode-db-clean
chmod +x ocdbc
# optional: symlink into PATH
ln -s "$(pwd)/ocdbc" ~/.local/bin/ocdbc
```

Requires Python 3.8+ (stdlib only, zero pip dependencies).

## Usage

### Analyze (always safe, read-only)

```bash
ocdbc analyze
```

Shows: DB size, live data vs freelist, table sizes, session age distribution, and the 5 largest messages.

### Vacuum (safe sequence)

```bash
ocdbc vacuum
```

The vacuum command performs these steps in order:

1. **fuser check** — refuse if OpenCode still has the DB open (unless `--skip-fuser`)
2. **WAL checkpoint** — flush write-ahead log into the main DB
3. **Integrity check** — abort if database is corrupt
4. **Backup** — timestamped copy, then verify backup integrity
5. **Enable `auto_vacuum = INCREMENTAL`** — prevent future freelist bloat
6. **VACUUM** — rebuild the database with progress indication
7. **Re-enable WAL** — `VACUUM` resets journal mode; restore it
8. **Final integrity check** — verify the result

```bash
# Skip the confirmation prompt
ocdbc vacuum --force

# Skip backup (dangerous — backup is recommended)
ocdbc vacuum --no-backup --force

# Proceed without fuser installed (use with caution)
ocdbc vacuum --skip-fuser --force

# Custom database path
ocdbc vacuum --path /custom/path/opencode.db
```

### Safety

- **Refuses to run if OpenCode has the database open** (detected via `fuser`)
- **Refuses to run if fuser is not installed** — pass `--skip-fuser` to override
- **Checks integrity before and after** — abort if anything looks wrong
- **Creates a verified timestamped backup before VACUUM** (unless `--no-backup`)
- **VACUUM uses atomic rename** — original DB untouched if the operation fails

## Example

```bash
$ ocdbc analyze

OpenCode Database Health Report
──────────────────────────────────────────────────────────
  Path:          /home/zjc/.local/share/opencode/opencode.db
  File size:     1.3 GB
  Page size:     4.0 KB
  Journal mode:  wal
  Auto-vacuum:   OFF (NONE)

Storage
──────────────────────────────────────────────────────────
  Live data:     525 MB
  Freelist:      766 MB  (56% of file)
  ↳ VACUUM would reclaim ~766 MB
  ↳ Estimated result:  ~525 MB
```

```bash
$ ocdbc vacuum

  ✓ No process has the database open

  Current size:   1.3 GB
  Live data:      525 MB
  Freelist:       766 MB (56%)

  Proceed with VACUUM? [y/N] y

  ✓ Integrity check passed
  ✓ Backup created (1.3 GB)
  ✓ WAL checkpointed
  ✓ auto_vacuum set to INCREMENTAL
  ✓ VACUUM complete
  ✓ journal_mode = wal
  ✓ Integrity check passed

Results
──────────────────────────────────────────────────────────
  Before:   1.3 GB
  After:    534 MB
  Reclaimed: 766 MB

  Backup:   /home/zjc/.local/share/opencode/opencode.db.backup.2026-07-22T...
  (Keep it until you verify OpenCode works correctly)
```

## Related

- [OpenCode issue #16777](https://github.com/anomalyco/opencode/issues/16777) — database bloat bug report
- [OpenCode PR #16730](https://github.com/anomalyco/opencode/pull/16730) — upstream fix (auto-vacuum, WAL checkpoint)
- [ocgc](https://github.com/whtsky/ocgc) — alternative garbage collector with session purging

## License

MIT
