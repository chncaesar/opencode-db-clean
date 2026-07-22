"""ocdbc — OpenCode Database Cleaner.

Safely analyze and shrink OpenCode's SQLite database.
Reclaims space wasted by freelist bloat and enables incremental auto-vacuum.

Usage:
  ocdbc analyze          Show database health report (read-only, always safe)
  ocdbc vacuum           VACUUM the database, kill freelist, enable auto_vacuum
  ocdbc --help           Show full help
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import sqlite3
import textwrap
import threading
import time

DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")

# ── ANSI helpers ─────────────────────────────────────────────────────────────

_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}

_use_color = True


def _c(name: str, text: str) -> str:
    if not _use_color:
        return text
    return f"{_COLORS.get(name, '')}{text}{_COLORS['reset']}"


def _hr() -> str:
    return _c("dim", "─" * 58)


def _fail(msg: str) -> None:
    print(f"\n{_c('red', '✗')} {msg}", file=sys.stderr)
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"  {_c('green', '✓')} {msg}")


def _info(msg: str) -> None:
    print(f"  {msg}")


# ── Formatting ───────────────────────────────────────────────────────────────


def fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    if n is None:
        return "?"
    n = int(n)
    if n >= 1024 * 1024 * 1024:
        return f"{n / (1024 ** 3):.1f} GB"
    elif n >= 1024 * 1024:
        return f"{n / (1024 ** 2):.0f} MB"
    elif n >= 1024:
        return f"{n / 1024:.0f} KB"
    else:
        return f"{n} B"


def auto_vacuum_label(mode: int) -> str:
    labels = {0: "OFF (NONE)", 1: "FULL", 2: "INCREMENTAL"}
    return labels.get(mode, f"UNKNOWN ({mode})")


# ── Safety checks ────────────────────────────────────────────────────────────


def check_db_locked(db_path: str) -> list[str]:
    """Return list of PIDs holding *db_path* open, or [] if nobody has it.

    Uses ``fuser``.  Returns ``None`` when ``fuser`` is not available.
    """
    try:
        result = subprocess.run(
            ["fuser", db_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # fuser prints PIDs to stdout (with the file path on stderr)
        stdout = result.stdout.strip()
        if stdout:
            return stdout.split()
        return []
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


# ── Database queries ─────────────────────────────────────────────────────────


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def collect_stats(db_path: str) -> dict:
    """Gather read-only statistics from the database."""
    conn = _connect(db_path)
    try:
        stats: dict = {"db_path": db_path, "file_size": os.path.getsize(db_path)}

        # PRAGMAs
        for pragma in ("page_size", "page_count", "freelist_count",
                       "auto_vacuum", "journal_mode", "foreign_keys"):
            val = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
            stats[pragma] = val

        ps = stats["page_size"]
        pc = stats["page_count"]
        fc = stats["freelist_count"]
        stats["freelist_bytes"] = fc * ps
        stats["total_bytes"] = pc * ps
        stats["live_bytes"] = (pc - fc) * ps
        stats["freelist_pct"] = (fc / pc * 100) if pc > 0 else 0.0

        # Row counts
        for table in ("message", "part", "event", "session", "project",
                      "todo", "event_sequence", "workspace"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                stats[f"{table}_rows"] = n
            except Exception:
                stats[f"{table}_rows"] = 0
                print(
                    f"  {_c('yellow', 'warning')}: could not count rows in table '{table}'",
                    file=sys.stderr,
                )

        # Data sizes for the big three
        for table in ("message", "part", "event"):
            try:
                row = conn.execute(
                    f"""SELECT COUNT(*)          AS cnt,
                               AVG(LENGTH(data)) AS avg_len,
                               MAX(LENGTH(data)) AS max_len,
                               SUM(LENGTH(data)) AS total_len
                        FROM {table}"""
                ).fetchone()
                stats[f"{table}_data_rows"] = row["cnt"]
                stats[f"{table}_data_avg"] = row["avg_len"] or 0
                stats[f"{table}_data_max"] = row["max_len"] or 0
                stats[f"{table}_data_total"] = row["total_len"] or 0
            except Exception:
                print(
                    f"  {_c('yellow', 'warning')}: could not read data stats for table '{table}'",
                    file=sys.stderr,
                )

        # Session age
        try:
            buckets = conn.execute(
                """SELECT CASE
                     WHEN julianday('now') - julianday(datetime(time_created/1000, 'unixepoch')) < 7  THEN '0–7 days'
                     WHEN julianday('now') - julianday(datetime(time_created/1000, 'unixepoch')) < 30 THEN '7–30 days'
                     WHEN julianday('now') - julianday(datetime(time_created/1000, 'unixepoch')) < 60 THEN '30–60 days'
                     WHEN julianday('now') - julianday(datetime(time_created/1000, 'unixepoch')) < 90 THEN '60–90 days'
                     ELSE '90+ days'
                   END AS bucket,
                   COUNT(*) AS n
                FROM session
                WHERE time_created IS NOT NULL
                GROUP BY bucket
                ORDER BY MIN(julianday('now') - julianday(datetime(time_created/1000, 'unixepoch')))"""
            ).fetchall()
            stats["session_age"] = [(r["bucket"], r["n"]) for r in buckets]
        except Exception:
            stats["session_age"] = []

        # Date range
        try:
            row = conn.execute(
                """SELECT datetime(MIN(time_created)/1000, 'unixepoch') AS oldest,
                          datetime(MAX(time_created)/1000, 'unixepoch') AS newest
                   FROM session WHERE time_created IS NOT NULL"""
            ).fetchone()
            stats["session_oldest"] = row["oldest"] or "?"
            stats["session_newest"] = row["newest"] or "?"
        except Exception:
            stats["session_oldest"] = "?"
            stats["session_newest"] = "?"

        # Top 5 largest messages
        try:
            rows = conn.execute(
                """SELECT id, session_id, LENGTH(data) AS len,
                          datetime((json_extract(data, '$.time.created')/1000), 'unixepoch') AS created
                   FROM message
                   ORDER BY LENGTH(data) DESC
                   LIMIT 5"""
            ).fetchall()
            stats["largest_messages"] = [
                (r["id"], r["session_id"], r["len"], r["created"]) for r in rows
            ]
        except Exception:
            stats["largest_messages"] = []

        return stats
    finally:
        conn.close()


# ── VACUUM with progress ─────────────────────────────────────────────────────


def _vacuum_with_progress(conn: sqlite3.Connection, db_path: str) -> None:
    """Execute VACUUM while a background thread polls the temp-file size."""
    temp_path = db_path + "-vacuum"

    # Clean up stale temp file from a previous interrupted VACUUM
    if os.path.exists(temp_path):
        os.remove(temp_path)

    done = threading.Event()

    def _poll() -> None:
        seen_sizes: set[int] = set()
        last_len = 0
        while not done.is_set():
            if os.path.exists(temp_path):
                size = os.path.getsize(temp_path)
                if size not in seen_sizes:
                    seen_sizes.add(size)
                    line = f"\r    writing {fmt_bytes(size)} ..."
                    last_len = max(last_len, len(line) - 1)  # -1 for \r
                    print(line, end="", flush=True)
            time.sleep(0.3)
        # Erase the progress line
        print("\r" + " " * last_len + "\r", end="", flush=True)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    try:
        conn.execute("VACUUM")
    finally:
        done.set()
        poller.join(timeout=1)
        # If VACUUM didn't complete, the temp file may remain — clean it up.
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ── Commands ─────────────────────────────────────────────────────────────────


def cmd_analyze(args: argparse.Namespace) -> None:
    """Read-only database health report."""
    db_path = args.path or DEFAULT_DB
    if not os.path.exists(db_path):
        _fail(f"Database not found: {db_path}")

    s = collect_stats(db_path)

    print()
    print(_c("bold", "OpenCode Database Health Report"))
    print(_hr())
    print(f"  Path:          {s['db_path']}")
    print(f"  File size:     {fmt_bytes(s['file_size'])}")
    print(f"  Page size:     {fmt_bytes(s['page_size'])}")
    print(f"  Page count:    {s['page_count']:,}")
    print(f"  Journal mode:  {s['journal_mode']}")
    print(f"  Auto-vacuum:   {auto_vacuum_label(s['auto_vacuum'])}")
    print(f"  Foreign keys:  {'ON' if s['foreign_keys'] else 'OFF'}")
    print()

    # Storage breakdown
    print(_c("bold", "Storage"))
    print(_hr())
    print(f"  Live data:     {fmt_bytes(s['live_bytes'])}")
    print(f"  Freelist:      {fmt_bytes(s['freelist_bytes'])}  ({s['freelist_pct']:.0f}% of file)")
    if s["freelist_pct"] > 20:
        print(
            f"  {_c('yellow', '↳ VACUUM would reclaim ~' + fmt_bytes(s['freelist_bytes']))}"
        )
        print(
            f"  {_c('yellow', '↳ Estimated result:  ~' + fmt_bytes(s['live_bytes']))}"
        )
    elif s["freelist_pct"] > 5:
        print(f"  {_c('dim', '↳ VACUUM would reclaim ~' + fmt_bytes(s['freelist_bytes']))}")
    else:
        print(f"  {_c('dim', '↳ Freelist is already small. VACUUM not urgently needed.')}")
    print()

    # Table summary
    print(_c("bold", "Tables"))
    print(_hr())
    header = f"  {'Table':<18} {'Rows':>8}  {'Data size':>10}  {'Avg row':>10}  {'Max row':>10}"
    print(header)
    print(f"  {'─' * 18} {'─' * 8}  {'─' * 10}  {'─' * 10}  {'─' * 10}")
    for table in ("message", "part", "event"):
        rows = s.get(f"{table}_data_rows", 0)
        total = s.get(f"{table}_data_total", 0)
        avg = s.get(f"{table}_data_avg", 0)
        mx = s.get(f"{table}_data_max", 0)
        print(
            f"  {table:<18} {rows:>8,}  {fmt_bytes(total):>10}  "
            f"{fmt_bytes(avg):>10}  {fmt_bytes(mx):>10}"
        )
    for table in ("session", "event_sequence", "project"):
        rows = s.get(f"{table}_rows", 0)
        print(f"  {table:<18} {rows:>8,}")
    print()

    # Sessions
    print(_c("bold", "Sessions"))
    print(_hr())
    print(f"  Total:         {s.get('session_rows', 0)}")
    print(f"  Oldest:        {s['session_oldest']}")
    print(f"  Newest:        {s['session_newest']}")
    if s["session_age"]:
        print(f"  Age distribution:")
        for bucket, count in s["session_age"]:
            bar = "█" * min(count, 60)
            print(f"    {bucket:<12} {count:>4}  {_c('dim', bar)}")
    print()

    # Largest objects
    if s["largest_messages"]:
        print(_c("bold", "Largest Messages"))
        print(_hr())
        for msg_id, sess_id, length, created in s["largest_messages"]:
            print(
                f"  {fmt_bytes(length):>10}  {msg_id[:24]}  "
                f"{sess_id[:24]}  {created or '?'}"
            )
        print()


def cmd_vacuum(args: argparse.Namespace) -> None:
    """Safe VACUUM sequence."""
    db_path = args.path or DEFAULT_DB
    if not os.path.exists(db_path):
        _fail(f"Database not found: {db_path}")

    # ── 1. Check OpenCode is not running ──────────────────────────────────
    pids = check_db_locked(db_path)
    if pids is None:
        if args.skip_fuser:
            _info(
                _c("yellow",
                   "Could not verify DB is unused (fuser not available). "
                   "Proceeding because --skip-fuser was given."
                )
            )
        else:
            _fail(
                "Could not verify whether the database is in use.\n"
                "  fuser is not installed. Install it, or re-run with:\n"
                "    ocdbc vacuum --skip-fuser\n"
                "  (only if you are certain OpenCode is not running)"
            )
    elif pids:
        _fail(
            f"Database is in use by PID(s): {', '.join(pids)}.\n"
            f"  Quit OpenCode completely, then re-run."
        )
    else:
        _ok("No process has the database open")
        _info(
            _c("dim",
               "Keep OpenCode closed until VACUUM completes — "
               "re-opening it before this finishes will cause data loss."
            )
        )

    # ── 2. Collect before stats ───────────────────────────────────────────
    before = collect_stats(db_path)

    print()
    print(f"  Current size:   {fmt_bytes(before['file_size'])}")
    print(f"  Live data:      {fmt_bytes(before['live_bytes'])}")
    print(
        f"  Freelist:       {fmt_bytes(before['freelist_bytes'])} "
        f"({before['freelist_pct']:.0f}%)"
    )

    if before["freelist_pct"] < 5:
        print()
        _info(
            "Freelist is already small. VACUUM won't reclaim much, "
            "but will enable auto_vacuum."
        )

    # ── 3. Confirm ────────────────────────────────────────────────────────
    if not args.force:
        print()
        try:
            resp = input(
                f"  {_c('yellow', 'Proceed with VACUUM?')} [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            _info("Cancelled.")
            return
        if resp not in ("y", "yes"):
            _info("Cancelled.")
            return

    # ── 4. Checkpoint WAL + integrity check ───────────────────────────────
    # Checkpoint BEFORE backup so the .db file contains all committed data.
    print()
    _info("Checkpointing WAL …")
    conn = _connect(db_path)
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            _fail(f"WAL checkpoint failed: {exc}")
        _ok("WAL checkpointed")

        _info("Running integrity check …")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            conn.close()
            _fail(f"Integrity check failed: {result}\n  Aborting.")
        _ok("Integrity check passed")
    finally:
        conn.close()

    # ── 5. Create backup ──────────────────────────────────────────────────
    # WAL is already flushed — copying just the .db is a complete backup.
    backup_path = None  # track for error messages even when --no-backup
    if not args.no_backup:
        ts = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
        backup_path = f"{db_path}.backup.{ts}"
        _info(f"Creating backup …")
        _info(f"  {backup_path}")
        try:
            shutil.copy2(db_path, backup_path)
        except OSError as exc:
            _fail(f"Failed to create backup: {exc}")
        _ok(f"Backup created ({fmt_bytes(os.path.getsize(backup_path))})")

        # Verify the backup is sound
        _info("Verifying backup integrity …")
        try:
            bc = _connect(backup_path)
            try:
                bresult = bc.execute("PRAGMA integrity_check").fetchone()[0]
                if bresult != "ok":
                    _fail(
                        f"Backup integrity check failed: {bresult}\n"
                        f"  Backup at {backup_path} may be corrupt."
                    )
            finally:
                bc.close()
            _ok("Backup integrity verified")
        except sqlite3.Error as exc:
            _fail(f"Failed to verify backup: {exc}")
    else:
        _info(_c("yellow", "Skipping backup (--no-backup)"))

    # ── 6. Execute VACUUM sequence ────────────────────────────────────────
    conn = _connect(db_path)
    try:
        # 6a. Enable incremental auto-vacuum
        _info("Enabling auto_vacuum = INCREMENTAL …")
        try:
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        except sqlite3.Error as exc:
            _fail(f"Failed to set auto_vacuum: {exc}")
        mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        _ok(f"auto_vacuum set to {auto_vacuum_label(mode)}")

        # 6b. VACUUM
        _info("Running VACUUM (may take 30–90 seconds) …")
        _vacuum_with_progress(conn, db_path)
        _ok("VACUUM complete")

        # 6c. Re-enable WAL (VACUUM resets journal_mode to delete)
        _info("Re-enabling WAL journal mode …")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:
            _fail(f"Failed to re-enable WAL journal mode: {exc}")
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        _ok(f"journal_mode = {journal}")

        # 6d. Final integrity check
        _info("Running final integrity check …")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            restore_hint = (
                f"\n  Restore from backup: cp {backup_path} {db_path}"
                if backup_path
                else "\n  No backup was created (--no-backup was used)."
            )
            _fail(
                f"Post-VACUUM integrity check failed: {result}"
                + restore_hint
            )
        _ok("Integrity check passed")
    finally:
        conn.close()

    # ── 7. Report results ─────────────────────────────────────────────────
    after_size = os.path.getsize(db_path)
    reclaimed = before["file_size"] - after_size

    print()
    print(_c("bold", "Results"))
    print(_hr())
    print(f"  Before:   {fmt_bytes(before['file_size'])}")
    print(f"  After:    {fmt_bytes(after_size)}")
    if reclaimed >= 0:
        print(f"  Reclaimed: {_c('green', fmt_bytes(reclaimed))}")
    else:
        print(f"  Growth:   {_c('yellow', fmt_bytes(-reclaimed))} (unexpected — check backup)")

    if backup_path:
        print()
        print(
            f"  Backup:   {backup_path}\n"
            f"  {_c('dim', '(Keep it until you verify OpenCode works correctly)')}"
        )
    print()


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    global _use_color

    parser = argparse.ArgumentParser(
        prog="ocdbc",
        description="Safely analyze and shrink OpenCode's SQLite database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
        examples:
          ocdbc analyze                        # read-only health report
          ocdbc vacuum                         # full safe VACUUM sequence
          ocdbc vacuum --force                 # skip confirmation
          ocdbc vacuum --skip-fuser --force    # fuser not installed
          ocdbc vacuum --no-backup --force     # skip backup & confirmation
          ocdbc analyze --path /tmp/my.db      # custom database path

        default database: {DEFAULT_DB}
        """),
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable colored output"
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ---- analyze ----
    p_analyze = sub.add_parser(
        "analyze", help="Show database health report (read-only)"
    )
    p_analyze.add_argument("--path", help="Path to opencode.db")

    # ---- vacuum ----
    p_vacuum = sub.add_parser(
        "vacuum",
        help="VACUUM database, reclaim freelist, enable incremental auto-vacuum",
    )
    p_vacuum.add_argument("--path", help="Path to opencode.db")
    p_vacuum.add_argument(
        "--no-backup", action="store_true", help="Skip backup before VACUUM"
    )
    p_vacuum.add_argument(
        "--force", "-f", action="store_true", help="Skip confirmation prompt"
    )
    p_vacuum.add_argument(
        "--skip-fuser", action="store_true",
        help="Proceed even if fuser is not installed (use with caution)",
    )

    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        _use_color = False

    if args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "vacuum":
        cmd_vacuum(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
