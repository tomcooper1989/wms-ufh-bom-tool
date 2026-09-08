"""
WMS UFH BOM Generator - Web Application
Deployed on Railway. Users access via browser, no local install needed.
"""

from flask import Flask, request, jsonify, send_from_directory, redirect, session
import os, tempfile, functools, json, datetime, re, contextlib, uuid, hmac, hashlib, base64, time

# Import all extraction logic from server.py
from server import scan_pdf_pages, scan_and_extract, extract_page

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production')

# Lets the frontend tell "still the same deploy" from "a new one has taken over", so it can
# prompt for a reload instead of silently running stale JS against a new backend. Gunicorn
# runs 2 workers (see Procfile) that each import this module independently, so a per-process
# uuid would differ between workers even with NO new deploy -- a request round-robining to
# the other worker would look like a false "new version" every time. RAILWAY_GIT_COMMIT_SHA
# is set identically for every worker/replica of one deploy and only changes on a real
# redeploy; uuid4 is just the local-dev fallback (a single process there, so no mismatch risk).
BOOT_ID = os.environ.get('RAILWAY_GIT_COMMIT_SHA') or uuid.uuid4().hex[:12]

# Password from environment variable — set in Railway dashboard
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '')

# Dashboard password — set DASHBOARD_PASSWORD in Railway env vars
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'wms-admin')

# Log file paths. /data is a mounted Railway volume, so these persist across deploys.
#
# LEGACY_LOG_FILE is the original single-JSON-array format. The log is now one JSON
# object per line (.jsonl) so that appends never rewrite the whole file; the legacy
# file is migrated in on first boot and then left alone as a backup. Rewriting the
# whole array on every append is what destroyed the pre-2026-07-10 history — two
# workers raced, corrupted the JSON, and the next load silently started from []. See
# LOGGING.md.
LEGACY_LOG_FILE = os.environ.get('LOG_FILE', '/data/bom_usage.json')
LOG_FILE = os.path.splitext(LEGACY_LOG_FILE)[0] + '.jsonl'
FAILURES_DIR = os.environ.get('FAILURES_DIR', '/data/bom_failures')
os.makedirs(FAILURES_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)

try:
    import fcntl   # POSIX (Railway)
except ImportError:
    fcntl = None
try:
    import msvcrt  # Windows (local dev)
except ImportError:
    msvcrt = None


# ---------------------------------------------------------------
# Usage logging helpers
#
# gunicorn runs 2 workers, so every one of these can be entered by two processes
# at once. Every write — append or full rewrite — serialises through log_lock().
# Do not lean on O_APPEND being atomic instead: that holds on Linux but not on
# Windows, where concurrent appends interleave and lose entries outright.
# ---------------------------------------------------------------

def _lock_file(lf, exclusive=True):
    if fcntl is not None:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)
    elif msvcrt is not None:
        lf.seek(0)
        mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_UNLCK
        while True:
            try:
                msvcrt.locking(lf.fileno(), mode, 1)
                return
            except OSError:
                if not exclusive:
                    return  # already unlocked
                continue    # LK_LOCK gave up after its retries; keep waiting


@contextlib.contextmanager
def log_lock():
    """Exclusive cross-process lock. Every write to the log must hold this.

    Not re-entrant — never call a helper that takes it from inside a with-block.
    """
    with open(LOG_FILE + '.lock', 'a+') as lf:
        _lock_file(lf, exclusive=True)
        try:
            yield
        finally:
            _lock_file(lf, exclusive=False)


def load_log():
    """Read all entries. A corrupt line is skipped and reported, never fatal."""
    entries = []
    if not os.path.exists(LOG_FILE):
        return entries
    with open(LOG_FILE, 'r') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                # One bad line costs one entry, not the whole history.
                app.logger.warning('skipping corrupt log line %d in %s', lineno, LOG_FILE)
    return entries


def append_log(entry):
    """Append one entry under the lock. Raises on failure — callers must handle.

    There is no read-modify-write cycle here, so concurrent workers cannot clobber
    each other's entries the way the old load/mutate/save did.
    """
    line = (json.dumps(entry, separators=(',', ':')) + '\n').encode('utf-8')
    with log_lock():
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


def rewrite_log(entries):
    """Replace the whole log atomically. Caller must hold log_lock(). Raises on failure."""
    # Write a temp file alongside, then swap: a crash or full disk mid-write leaves
    # the previous log intact rather than truncated to nothing.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(LOG_FILE) or '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            for entry in entries:
                f.write(json.dumps(entry, separators=(',', ':')) + '\n')
        os.replace(tmp_path, LOG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def migrate_legacy_log():
    """One-time import of the old single-JSON-array log into the .jsonl log."""
    if os.path.exists(LOG_FILE) or not os.path.exists(LEGACY_LOG_FILE):
        return
    with log_lock():
        if os.path.exists(LOG_FILE):  # another worker won the race
            return
        try:
            with open(LEGACY_LOG_FILE, 'r') as f:
                entries = json.load(f)
        except Exception:
            app.logger.exception('could not read legacy log %s — leaving it in place', LEGACY_LOG_FILE)
            return
        if not isinstance(entries, list):
            app.logger.error('legacy log %s is not a list — leaving it in place', LEGACY_LOG_FILE)
            return
        rewrite_log(entries)
        app.logger.info('migrated %d entries from %s to %s', len(entries), LEGACY_LOG_FILE, LOG_FILE)


migrate_legacy_log()


# ---------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------

# Hub SSO -- a short-lived identity token minted by The Hub (app/auth.py's make_sso_token, keyed
# on HUB_SSO_SECRET) and passed as ?hub_sso=<token> on this tool's iframe URL when opened from
# there, so someone already signed into the Hub isn't asked for this tool's own password too.
# Deliberately a SEPARATE secret from the Hub's own session-signing key -- copy the SAME value
# into both this service's Railway env and the Hub's, the same way MSGRAPH_CLIENT_SECRET already
# is shared across several of these deployments. Unset (empty string) = feature off, current
# behaviour unchanged -- this tool's own ACCESS_PASSWORD login stays fully in place either way for
# anyone who opens it directly rather than through the Hub.
HUB_SSO_SECRET = os.environ.get('HUB_SSO_SECRET', '')


def _verify_hub_sso(token):
    if not HUB_SSO_SECRET or not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user, exp, sig = raw.rsplit('|', 2)
        good = hmac.new(HUB_SSO_SECRET.encode(), f'{user}|{exp}'.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        return user
    except Exception:
        return None


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Identify via Hub SSO independently of ACCESS_PASSWORD: Tom's own instruction is this
        # tool should NEVER require a password, only skip the name prompt -- so session['hub_user']
        # (read by /hub_user, below) must get set even while ACCESS_PASSWORD stays unset/off,
        # not just when someone's about to be asked for a password.
        if 'hub_user' not in session:
            hub_user = _verify_hub_sso(request.args.get('hub_sso'))
            if hub_user:
                session['hub_user'] = hub_user
        if ACCESS_PASSWORD and not session.get('authenticated'):
            if session.get('hub_user'):
                session['authenticated'] = True
                return f(*args, **kwargs)
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        if request.form.get('password') == ACCESS_PASSWORD:
            session['authenticated'] = True
            return redirect('/')
        error = 'Incorrect password'
    return '''<!DOCTYPE html>
<html><head><title>WMS UFH BOM Tool</title>
<style>
  body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f5f5f5}
  .box{background:#fff;padding:2.5rem 3rem;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,0.1);text-align:center;width:320px}
  h2{margin:0 0 0.5rem;font-size:20px;color:#1a1a2e}
  p{color:#666;font-size:14px;margin:0 0 1.5rem}
  input{width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:8px;font-size:14px;box-sizing:border-box;margin-bottom:1rem}
  button{width:100%;padding:10px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#1d4ed8}
  .error{color:#dc2626;font-size:13px;margin-bottom:1rem}
</style></head>
<body><div class="box">
  <h2>WMS UFH BOM Tool</h2>
  <p>Enter your access password to continue</p>
  ''' + (f'<p class="error">{error}</p>' if error else '') + '''
  <form method="post">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Sign in</button>
  </form>
</div></body></html>'''


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ---------------------------------------------------------------
# Usage logging endpoints
# ---------------------------------------------------------------

@app.route('/log_bom', methods=['POST'])
@login_required
def log_bom():
    """Called by the browser each time a BOM is generated."""
    try:
        data = request.get_json(force=True) or {}
        _warnings = data.get('warnings', [])
        if not isinstance(_warnings, list):
            _warnings = []
        entry = {
            'ts': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'user': str(data.get('user', '')).strip()[:80],
            'project_ref': str(data.get('project_ref', '')).strip()[:80],
            'floor': str(data.get('floor', '')).strip()[:80],
            'system': str(data.get('system', '')).strip()[:40],
            'status': str(data.get('status', 'complete')).strip()[:20] or 'complete',
            'warnings': [str(w).strip()[:200] for w in _warnings][:20],
        }
        append_log(entry)
        return jsonify({'ok': True})
    except Exception as e:
        app.logger.exception('failed to record BOM usage entry')
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/log_failure', methods=['POST'])
@login_required
def log_failure():
    """Called when a PDF completely fails to read — saves the file and logs the event."""
    try:
        user = request.form.get('user', '').strip()[:80]
        filename = request.form.get('filename', 'unknown.pdf').strip()[:120]
        reason = request.form.get('reason', '').strip()[:200]
        failure_type = request.form.get('failure_type', 'complete').strip()[:20]
        # Ignore empty junk (e.g. bots/scanners POSTing to this open endpoint): a
        # genuine failure always carries a reason and/or the PDF that failed to read.
        if not reason and 'pdf' not in request.files:
            return jsonify({'ok': True, 'skipped': 'empty'})
        ts = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\.\-]', '_', filename)
        save_name = f'{ts}_{safe_name}'
        saved_path = None
        if 'pdf' in request.files:
            pdf_file = request.files['pdf']
            saved_path = os.path.join(FAILURES_DIR, save_name)
            pdf_file.save(saved_path)
        entry = {
            'ts': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'user': user,
            'filename': filename,
            'reason': reason,
            'failure_type': failure_type,
            'saved_as': save_name if saved_path else None,
            'type': 'failure',
        }
        append_log(entry)
        return jsonify({'ok': True})
    except Exception as e:
        app.logger.exception('failed to record PDF failure entry')
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/download_failure/<filename>', methods=['POST'])
def download_failure(filename):
    """Download a saved failure PDF — protected by dashboard password."""
    try:
        data = request.get_json(force=True) or {}
        if data.get('password') != DASHBOARD_PASSWORD:
            return jsonify({'error': 'wrong password'}), 403
        safe = re.sub(r'[^\w\.\-]', '_', filename)
        return send_from_directory(FAILURES_DIR, safe, as_attachment=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/download_log', methods=['POST'])
def download_log():
    """Download the raw usage log for backup — protected by dashboard password.
    Returns the current .jsonl, falling back to the legacy JSON-array file."""
    try:
        data = request.get_json(force=True) or {}
        if data.get('password') != DASHBOARD_PASSWORD:
            return jsonify({'error': 'wrong password'}), 403
        path = LOG_FILE if os.path.exists(LOG_FILE) else LEGACY_LOG_FILE
        if not os.path.exists(path):
            return jsonify({'error': 'no log file yet'}), 404
        return send_from_directory(os.path.dirname(path) or '.',
                                   os.path.basename(path), as_attachment=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/delete_entries', methods=['POST'])
def delete_entries():
    """Delete selected log entries by timestamp+user key — protected by dashboard password."""
    try:
        data = request.get_json(force=True) or {}
        if data.get('password') != DASHBOARD_PASSWORD:
            return jsonify({'error': 'wrong password'}), 403
        keys_to_delete = set(data.get('keys', []))  # each key = "ts||user"
        # Read and rewrite under the lock, or a concurrent append lands in the
        # window between them and is silently dropped by the rewrite.
        with log_lock():
            entries = [e for e in load_log()
                       if '{}||{}'.format(e.get('ts', ''), e.get('user', '')) not in keys_to_delete]
            rewrite_log(entries)
        return jsonify({'ok': True, 'remaining': len(entries)})
    except Exception as e:
        app.logger.exception('failed to delete log entries')
        return jsonify({'error': str(e)}), 500


@app.route('/import_entries', methods=['POST'])
def import_entries():
    """Restore entries from a backup — protected by dashboard password.

    Skips entries already present, so re-running it is safe: use it to merge a
    dashboard backup back in after a redeploy has wiped the log.
    """
    try:
        data = request.get_json(force=True) or {}
        if data.get('password') != DASHBOARD_PASSWORD:
            return jsonify({'error': 'wrong password'}), 403
        incoming = data.get('entries')
        if not isinstance(incoming, list):
            return jsonify({'error': 'entries must be a list'}), 400
        with log_lock():
            existing = load_log()
            seen = {json.dumps(e, sort_keys=True) for e in existing}
            added = [e for e in incoming
                     if isinstance(e, dict) and json.dumps(e, sort_keys=True) not in seen]
            if added:
                rewrite_log(existing + added)
        return jsonify({'ok': True, 'added': len(added), 'total': len(existing) + len(added)})
    except Exception as e:
        app.logger.exception('failed to import log entries')
        return jsonify({'error': str(e)}), 500


@app.route('/dashboard_data', methods=['POST'])
def dashboard_data():
    """Returns usage data — protected by dashboard password in request body."""
    try:
        data = request.get_json(force=True) or {}
        if data.get('password') != DASHBOARD_PASSWORD:
            return jsonify({'error': 'wrong password'}), 403
        entries = load_log()
        return jsonify({'entries': entries})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------
# Main app routes
# ---------------------------------------------------------------

@app.route('/health')
def health():
    return jsonify({'ok': True, 'boot_id': BOOT_ID})


@app.route('/')
@login_required
def index():
    return send_from_directory('.', 'index.html')


@app.route('/hub_user')
def hub_user():
    """The name-capture modal (index.html's initName()) checks this before showing itself, so
    someone arriving via a Hub SSO login never has to type their name -- it's already on the Hub
    session set by login_required(). Empty when there's no Hub-authenticated session (a direct
    visitor, or one who typed the ACCESS_PASSWORD by hand), which is exactly when the modal should
    still ask, same as today."""
    return jsonify({'name': session.get('hub_user', '')})


@app.route('/scan', methods=['POST'])
@login_required
def scan():
    return _handle_pdf_request('scan')


@app.route('/scan_and_extract', methods=['POST'])
@login_required
def scan_and_extract_route():
    return _handle_pdf_request('scan_and_extract')


@app.route('/extract', methods=['POST'])
@login_required
def extract():
    return _handle_pdf_request('extract')


def _handle_pdf_request(endpoint):
    if 'pdf' not in request.files:
        return jsonify({'error': 'No PDF uploaded'}), 400

    pdf_file = request.files['pdf']
    page_index = int(request.form.get('page_index', 0))
    unit_index = request.form.get('unit_index')
    split_x = request.form.get('split_x')
    unit_label = request.form.get('unit_label')
    floor_name_override = request.form.get('floor_name_override')
    project_ref_override = request.form.get('project_ref_override')
    system_type_hint = request.form.get('system_type_hint') or None

    if unit_index is not None:
        try: unit_index = int(unit_index)
        except: unit_index = None
    if split_x is not None:
        try: split_x = float(split_x)
        except: split_x = None

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        pdf_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        if endpoint == 'scan':
            result = scan_pdf_pages(tmp_path)
        elif endpoint == 'scan_and_extract':
            result = scan_and_extract(tmp_path)
        else:
            result = extract_page(tmp_path, page_index,
                                  unit_index=unit_index, split_x=split_x,
                                  unit_label=unit_label,
                                  floor_name_override=floor_name_override,
                                  project_ref_override=project_ref_override,
                                  system_type_hint=system_type_hint)
    except Exception as e:
        import traceback
        traceback.print_exc()
        result = {'error': str(e)}
    finally:
        try: os.unlink(tmp_path)
        except: pass

    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
