"""
WMS UFH BOM Generator - Web Application
Deployed on Railway. Users access via browser, no local install needed.
"""

from flask import Flask, request, jsonify, send_from_directory, redirect, session
import os, tempfile, functools

# Import all extraction logic from server.py
from server import scan_pdf_pages, scan_and_extract, extract_page

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production')

# Password from environment variable — set in Railway dashboard
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '')

# ---------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if ACCESS_PASSWORD and not session.get('authenticated'):
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
# Main app routes
# ---------------------------------------------------------------

@app.route('/')
@login_required
def index():
    return send_from_directory('.', 'index.html')


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
                                  project_ref_override=project_ref_override)
    finally:
        try: os.unlink(tmp_path)
        except: pass

    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
