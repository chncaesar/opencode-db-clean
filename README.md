# ocdbc — OpenCode Database Cleaner

[![PyPI](https://img.shields.io/pypi/v/ocdbc.svg)](https://pypi.org/project/ocdbc/)

Your OpenCode database may be much larger than the data it still contains.

OpenCode keeps session history in a local SQLite database at `~/.local/share/opencode/opencode.db`. After heavy use, compaction, deletion, or archival can leave behind **freelist pages**: empty pages inside the database file that SQLite can reuse, but the filesystem still counts as occupied disk space.

That means a 1.3 GB database might only contain 525 MB of live data.

`ocdbc` helps OpenCode users answer two practical questions:

- How much of my OpenCode database is live data?
- If there is significant freelist bloat, how can I safely reclaim the space?

`ocdbc` provides two commands:

- **`analyze`** — read-only health report (no risk)
- **`vacuum`** — safe VACUUM sequence with backup, integrity checks, and `auto_vacuum = INCREMENTAL` enablement

Part of the [OpenCode Reliability Toolkit](https://jczhu.com/opencode-tools/): small tools for making AI coding agents more reliable in real engineering workflows.

## Installation

`ocdbc` is available on [PyPI](https://pypi.org/project/ocdbc/):

```bash
# from PyPI (recommended)
pip install ocdbc

# from GitHub
pip install git+https://github.com/chncaesar/opencode-db-clean.git

# from source
git clone https://github.com/chncaesar/opencode-db-clean.git
cd opencode-db-clean
pip install .
```

Requires Python 3.8+ (stdlib only, zero pip dependencies).

## Usage

### Analyze (always safe, read-only)

```bash
ocdbc analyze
```

Shows: DB size, live data vs freelist, table sizes, session age distribution, and the 5 largest messages.

Run this first. If the freelist percentage is small, there may be nothing worth cleaning.

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

## When to use this

Use `ocdbc` when:

- `opencode.db` has grown large and you want to know whether the space is live data or freelist bloat.
- You use OpenCode heavily and want a safe local runtime maintenance check.
- You want a read-only report before deciding whether to run SQLite `VACUUM`.
- You want backups and integrity checks around database cleanup instead of running manual SQLite commands.

## When not to use this

Do not use `ocdbc vacuum` while OpenCode is running against the same database. The default command refuses to run if it detects an open database handle. Close OpenCode first, run `ocdbc analyze`, then vacuum only when the report shows meaningful reclaimable space.

`ocdbc` does not delete sessions or decide which history is valuable. It only analyzes SQLite storage layout and, when requested, rebuilds the database file to return unused pages to the filesystem.

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

  ✓ WAL checkpointed
  ✓ Integrity check passed
  ✓ Backup created (1.3 GB)
  ✓ Backup integrity verified
  ✓ auto_vacuum set to INCREMENTAL
  ✓ VACUUM complete
  ✓ journal_mode = wal
  ✓ Integrity check passed

Results
──────────────────────────────────────────────────────────
  Before:   1.3 GB
  After:    578 MB
  Reclaimed: 731 MB

  Backup:   /home/zjc/.local/share/opencode/opencode.db.backup.2026-07-22T...
  (Keep it until you verify OpenCode works correctly)
```

## Related

- [OpenCode issue #16777](https://github.com/anomalyco/opencode/issues/16777) — database bloat bug report
- [OpenCode PR #16730](https://github.com/anomalyco/opencode/pull/16730) — upstream fix (auto-vacuum, WAL checkpoint)
- [ocgc](https://github.com/whtsky/ocgc) — alternative garbage collector with session purging

## OpenCode Reliability Toolkit

| Tool | Description |
|------|-------------|
| [opencode-waitfor](https://github.com/chncaesar/opencode-waitfor) | `wait_for` for HTTP/TCP/command readiness checks |
| [opencode-db-clean](https://github.com/chncaesar/opencode-db-clean) | Reclaim disk space from bloated SQLite databases |
| [opencode-session-reflection](https://github.com/chncaesar/opencode-session-reflection) | Qualitative review of past coding sessions |
| [opencode-fleet](https://github.com/chncaesar/opencode-fleet) | Multi-node remote OpenCode orchestration |

## License

MIT
