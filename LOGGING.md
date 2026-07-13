# Usage log: what went wrong and how it works now

## What actually happened (2026-07-13 investigation)

Usage history before Friday 2026-07-10 14:42 UTC was destroyed. **The Railway volume was
not the cause.** Evidence:

- A volume (`web-volume`, 5 GB) is mounted at `/data` and has been attached to **every**
  deployment since 2026-07-07 21:30 UTC (`railway deployment list --json`, `meta.volumeMounts`).
- The deployed code has written to `/data/bom_usage.json` since 2026-07-07.
- There were **no deploys at all** between Wed 2026-07-08 20:40 UTC and Fri 2026-07-10 13:28 UTC.

So Wednesday and Thursday entries were written to persistent storage, nothing wiped that
storage, and they still vanished. The application destroyed its own log.

### Root cause

The old code did a read-modify-write of a single JSON array on every logged BOM:

```python
entries = load_log()      # open, json.load
entries.append(entry)
save_log(entries)         # open(LOG_FILE, 'w')  <-- truncates immediately
```

gunicorn runs `--workers 2`. Two concurrent requests both truncate and both write, so
their output interleaves and the file becomes invalid JSON. On the next request:

```python
def load_log():
    try:
        ...json.load(f)
    except Exception:
        pass          # <-- swallows the corruption
    return []         # <-- "the log is empty"
```

`load_log` returns `[]`, the caller appends one entry, and `save_log` writes a fresh
one-element array over the entire history. Silently, with no error anywhere. The first
surviving entry is timestamped ~74 minutes after Friday's deploy, when usage picked up
again after the new feature shipped.

## How it works now

- **Append-only.** One JSON object per line in `/data/bom_usage.jsonl`. Recording a BOM
  appends a line; it never rewrites the file, so there is no read-modify-write cycle to
  lose a race.
- **Locked.** Every write takes an exclusive cross-process lock (`log_lock()`), so the
  two gunicorn workers cannot interleave. Do not rely on `O_APPEND` atomicity instead:
  it holds on Linux but not on Windows, where concurrent appends silently lose entries
  (verified: 60 of 200 lost).
- **Atomic rewrites.** Delete and import rewrite the whole file via a temp file and
  `os.replace`, so a crash or full disk leaves the previous log intact.
- **Corruption is survivable.** `load_log` skips a bad line and logs a warning. One bad
  line now costs one entry, not the whole history.
- **Failures are loud.** Write errors raise and are logged, instead of `except: pass`.
- **Legacy migration.** The old `/data/bom_usage.json` array is imported into the
  `.jsonl` on first boot and left in place as a backup.

## Backing up

```powershell
.\backup_log.ps1 -AppUrl https://web-production-c8487.up.railway.app
```

Prompts for the dashboard password, saves `backups\bom_usage_backup_<timestamp>.json`,
and refuses to write an empty backup. Worth running on a schedule: the volume protects
against redeploys, not against an accidental delete from the dashboard.

## Restoring

`/import_entries` skips entries already present, so running it twice is safe. **But the
POST body must be sent as explicit UTF-8 bytes**, or the restore will create duplicates:

```powershell
$backup = Get-Content .\backups\bom_usage_backup_<timestamp>.json -Raw | ConvertFrom-Json
$json  = @{ password = '<DASHBOARD_PASSWORD>'; entries = $backup.entries } | ConvertTo-Json -Depth 10
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)          # <-- required
Invoke-RestMethod -Uri 'https://web-production-c8487.up.railway.app/import_entries' `
  -Method Post -ContentType 'application/json; charset=utf-8' -Body $bytes
```

Windows PowerShell 5.1 does not encode a *string* body as UTF-8 for `application/json`.
Failure entries contain em dashes (`could not be read — enter manually`), which get
mangled to `-` in transit. The import dedupes on exact content, so a mangled entry does
not match the original, and you get a near-identical duplicate instead of a no-op. This
happened for real on 2026-07-13 and had to be unpicked by hand.

Sanity check after any restore - all three must hold:

```powershell
$now = @((Invoke-RestMethod -Uri "$base/dashboard_data" -Method Post `
  -ContentType 'application/json' -Body (@{password=$pw}|ConvertTo-Json)).entries)
$now.Count                                                              # expected total
@($now | Where-Object { $_.reason -and $_.reason.Contains('read - enter') }).Count  # must be 0
@($now | Group-Object { $_ | ConvertTo-Json -Compress } | Where-Object Count -gt 1).Count  # must be 0
```

Note `/delete_entries` keys on `ts||user`, which is **not unique** - two BOMs by the same
user in the same second share a key, as do a BOM and its failure entry. Deleting a key
removes every entry that matches it. To remove one of a set, delete the key and re-import
the ones you want to keep.

## Unrelated issue found during the investigation

The Railway service has an env var named **`access_password`** (lowercase), but the code
reads `ACCESS_PASSWORD`. Linux environment variables are case-sensitive, so
`ACCESS_PASSWORD` is unset, `login_required` is a no-op, and **the app currently serves to
anyone with the URL without a login** (verified: `GET /` returns 200 with the app page).

Fix by renaming the variable in Railway to `ACCESS_PASSWORD`. The dashboard is unaffected
— `DASHBOARD_PASSWORD` is spelled correctly and is enforced.
