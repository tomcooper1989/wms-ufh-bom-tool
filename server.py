"""
UFH BOM Generator - Server
WMS Underfloor Heating

To update PDF reading logic: edit this file.
To update BOM rules: edit index.html.
"""

import http.server
import socketserver
import json
import os
import re
import tempfile
import threading
import webbrowser

PORT = 5000
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


def decode_cid(text):
    cid_digit = {
        17: '.', 19: '0', 20: '1', 21: '2', 22: '3',
        23: '4', 24: '5', 25: '6', 26: '7', 27: '8', 28: '9'
    }
    def replace(m):
        return cid_digit.get(int(m.group(1)), '')
    return re.sub(r'\(cid:(\d+)\)', replace, text)


def get_floor_name(raw):
    """Extract floor name from a page's raw text."""
    raw_ns = raw.replace(' ', '').replace('\n', '')
    for marker in [
        'ProposedUnderfloorHeatingLayout',
        'As-BuiltUnderfloorHeatingLayout',
        'AsBuiltUnderfloorHeatingLayout',
        'AsInstalledUnderfloorHeatingLayout',
    ]:
        if marker in raw_ns:
            idx = raw_ns.index(marker)
            before = raw_ns[max(0, idx - 2000):idx]  # increased from 250
            patterns = [
                (r'LowerGroundFloor', 'Lower Ground Floor'),
                (r'GroundFloor', 'Ground Floor'),
                (r'BasementFloor', 'Basement Floor'),
                (r'Basement', 'Basement Floor'),
                (r'LowerGround', 'Lower Ground Floor'),
                (r'FirstFloor', 'First Floor'),
                (r'SecondFloor', 'Second Floor'),
                (r'SecoundFloor', 'Second Floor'),
                (r'ThirdFloor', 'Third Floor'),
                (r'FourthFloor', 'Fourth Floor'),
                (r'FifthFloor', 'Fifth Floor'),
                (r'SixthFloor', 'Sixth Floor'),
                (r'SeventhFloor', 'Seventh Floor'),
                (r'RoofFloor', 'Roof'),
                (r'RoofLevel', 'Roof'),
            ]
            all_matches = []
            for pattern, label in patterns:
                for m in re.finditer(pattern, before, re.IGNORECASE):
                    all_matches.append((m.start(), m.end(), label))
            if all_matches:
                all_matches.sort(key=lambda x: (x[1], x[1] - x[0]), reverse=True)
                return all_matches[0][2]

    # Fallback: look for floor name near top of page in raw text (with spaces)
    for line in raw.split('\n')[:20]:
        line_ns = line.replace(' ', '')
        for pattern, label in [
            ('FIRSTFLOOR', 'First Floor'), ('SECONDFLOOR', 'Second Floor'),
            ('THIRDFLOOR', 'Third Floor'), ('FOURTHFLOOR', 'Fourth Floor'),
            ('FIFTHFLOOR', 'Fifth Floor'), ('SIXTHFLOOR', 'Sixth Floor'),
            ('GROUNDFLOOR', 'Ground Floor'), ('BASEMENTFLOOR', 'Basement Floor'),
            ('LOWERGROUNDFLOOR', 'Lower Ground Floor'),
        ]:
            if pattern in line_ns.upper():
                return label

    return None


def extract_loop_groups_from_line(line):
    groups = []
    if re.search(r'Loop\s*Length', line, re.IGNORECASE):
        parts = re.split(r'Loop\s*Length', line, flags=re.IGNORECASE)
        for part in parts[1:]:
            found = re.findall(r'(\d+)\s*m\b', part)
            vals = [int(v) for v in found if int(v) > 0]
            if len(vals) >= 1:
                groups.append(('LL', vals))
    elif re.search(r'As Installed\s+\d+\s*m', line, re.IGNORECASE):
        parts = re.split(r'As Installed', line, flags=re.IGNORECASE)
        for part in parts[1:]:
            found = re.findall(r'(\d+)\s*m\b', part)
            vals = [int(v) for v in found if int(v) > 0]
            if len(vals) >= 2:
                groups.append(('AI', vals))
    return groups


def deduplicate_groups(all_groups):
    final_groups = []
    for source, g in all_groups:
        covered = False
        for i, existing in enumerate(final_groups):
            g_sorted = sorted(g)
            ex_copy = sorted(existing)
            is_subset = True
            for v in g_sorted:
                if v in ex_copy:
                    ex_copy.remove(v)
                else:
                    is_subset = False
                    break
            if is_subset:
                covered = True
                break
            # Check reverse: existing is subset of g (replace with longer)
            ex_sorted = sorted(existing)
            g_copy = sorted(g)
            ex_subset = True
            for v in ex_sorted:
                if v in g_copy:
                    g_copy.remove(v)
                else:
                    ex_subset = False
                    break
            if ex_subset and len(g) > len(existing):
                final_groups[i] = g
                covered = True
                break
        if not covered:
            final_groups.append(g)
    return final_groups


SYSTEM_MAP = [
    # AmbiLoFloor
    ("lofloor",                  "AmbiLoFloor"),
    ("lo floor",                 "AmbiLoFloor"),
    ("ambilofloor",              "AmbiLoFloor"),
    ("ambi-lofloor",             "AmbiLoFloor"),
    ("ambi lo floor",            "AmbiLoFloor"),
    # AmbiJoFloor
    ("jofloor",                  "AmbiJoFloor"),
    ("jo floor",                 "AmbiJoFloor"),
    ("ambijofloor",              "AmbiJoFloor"),
    ("ambi-jofloor",             "AmbiJoFloor"),
    ("ambi jo floor",            "AmbiJoFloor"),
    # AmbiPlate
    ("ambiplate 20",             "AmbiPlate"),
    ("ambi-plate 20",            "AmbiPlate"),
    ("ambi plate 20",            "AmbiPlate"),
    ("ambiplate20",              "AmbiPlate"),
    ("ambi-plate",               "AmbiPlate"),
    ("ambiplate",                "AmbiPlate"),
    ("overplate",                "AmbiPlate"),
    ("ambi plate",               "AmbiPlate"),
    ("over plate",               "AmbiPlate"),
    # OverDeck20
    ("overdeck 20",              "OverDeck20"),
    ("overdeck20",               "OverDeck20"),
    ("over deck 20",             "OverDeck20"),
    ("over-deck 20",             "OverDeck20"),
    # AmbiDeck20 — check before AmbiDeck18 / ambi-deck
    ("ambideck 20 pro",          "AmbiDeck20"),
    ("ambideck 20",              "AmbiDeck20"),
    ("ambi-deck 20",             "AmbiDeck20"),
    ("ambi deck 20",             "AmbiDeck20"),
    ("20mm ambi-deck",           "AmbiDeck20"),
    ("20mm ambideck",            "AmbiDeck20"),
    # AmbiDeck18
    ("ambideck 18",              "AmbiDeck18"),
    ("ambi-deck 18",             "AmbiDeck18"),
    ("ambi deck 18",             "AmbiDeck18"),
    ("18mm ambi-deck",           "AmbiDeck18"),
    ("18mm ambideck",            "AmbiDeck18"),
    ("ambi-deck",                "AmbiDeck18"),
    ("ambideck",                 "AmbiDeck18"),
    ("6mm cement",               "AmbiDeck18"),
    ("cement board",             "AmbiDeck18"),
    # AmbiCastellated
    ("castellated",              "AmbiCastellated"),
    ("ambicastellated",          "AmbiCastellated"),
    ("ambi-castellated",         "AmbiCastellated"),
    ("ambi castellated",         "AmbiCastellated"),
    # AmbiSolo
    ("ambisolo",                 "AmbiSolo"),
    ("ambi-solo",                "AmbiSolo"),
    ("ambi solo",                "AmbiSolo"),
    ("solo panel",               "AmbiSolo"),
    # AmbiDuoClip
    ("ambiduoclip",              "AmbiDuoClip"),
    ("ambi-duoclip",             "AmbiDuoClip"),
    ("ambi duoclip",             "AmbiDuoClip"),
    ("duo clip",                 "AmbiDuoClip"),
    ("duoclip",                  "AmbiDuoClip"),
    # AmbiClip / Cliprail
    ("cliprail",                 "AmbiClip"),
    ("clip rail",                "AmbiClip"),
    ("ambiclip",                 "AmbiClip"),
    ("ambi-clip",                "AmbiClip"),
    ("ambi clip",                "AmbiClip"),
    # AmbiTak — after DuoClip so "tacker" doesn't accidentally match duoclip drawings
    ("tacker type",              "AmbiTak"),
    ("typical tacker",           "AmbiTak"),
    ("ambitak",                  "AmbiTak"),
    ("ambi-tak",                 "AmbiTak"),
    ("ambi tak",                 "AmbiTak"),
    ("u clip",                   "AmbiTak"),
    ("\"u\" clip",               "AmbiTak"),
    ("separating layer",         "AmbiTak"),
    ("separating membrane",      "AmbiTak"),
    ("resilient layer",          "AmbiTak"),
    # AmbiFloat10
    ("ambi-float",               "AmbiFloat10"),
    ("ambifloat",                "AmbiFloat10"),
    ("ambi float",               "AmbiFloat10"),
    ("pre-formed foiled faced",  "AmbiFloat10"),
    ("preformed foiled faced",   "AmbiFloat10"),
]


# Map from System row values to internal system codes
SYSTEM_ROW_MAP = {
    # AmbiTak
    'ambitak': 'AmbiTak', 'ambi-tak': 'AmbiTak', 'ambi tak': 'AmbiTak',
    'tacker': 'AmbiTak',
    # AmbiSolo
    'ambisolo': 'AmbiSolo', 'ambi-solo': 'AmbiSolo', 'ambi solo': 'AmbiSolo',
    # AmbiClip
    'ambiclip': 'AmbiClip', 'ambi-clip': 'AmbiClip', 'ambi clip': 'AmbiClip',
    'cliprail': 'AmbiClip', 'clip rail': 'AmbiClip',
    # AmbiDuoClip
    'ambiduoclip': 'AmbiDuoClip', 'ambi-duoclip': 'AmbiDuoClip',
    'duo clip': 'AmbiDuoClip', 'duoclip': 'AmbiDuoClip',
    # AmbiPlate
    'ambiplate': 'AmbiPlate', 'ambi-plate': 'AmbiPlate', 'ambi plate': 'AmbiPlate',
    'overplate': 'AmbiPlate', 'over plate': 'AmbiPlate',
    'ambiplate 20': 'AmbiPlate', 'ambi-plate 20': 'AmbiPlate', 'ambi plate 20': 'AmbiPlate',
    'ambiplate20': 'AmbiPlate',
    # AmbiLoFloor
    'amilofloor': 'AmbiLoFloor', 'lofloor': 'AmbiLoFloor',
    'ambilofloor': 'AmbiLoFloor', 'ambi-lofloor': 'AmbiLoFloor',
    'lo floor': 'AmbiLoFloor',
    # AmbiJoFloor
    'ambijofloor': 'AmbiJoFloor', 'jofloor': 'AmbiJoFloor',
    'ambi-jofloor': 'AmbiJoFloor', 'jo floor': 'AmbiJoFloor',
    # OverDeck20
    'overdeck20': 'OverDeck20', 'overdeck 20': 'OverDeck20',
    'over deck 20': 'OverDeck20', 'over-deck 20': 'OverDeck20',
    # AmbiDeck20
    'ambideck20': 'AmbiDeck20', 'ambideck 20': 'AmbiDeck20',
    'ambi-deck 20': 'AmbiDeck20', 'ambi deck 20': 'AmbiDeck20',
    # AmbiDeck18
    'ambideck18': 'AmbiDeck18', 'ambideck 18': 'AmbiDeck18',
    'ambi-deck 18': 'AmbiDeck18', 'ambi deck 18': 'AmbiDeck18',
    'ambi-deck': 'AmbiDeck18', 'ambideck': 'AmbiDeck18',
    # AmbiCastellated
    'castellated': 'AmbiCastellated', 'ambicastellated': 'AmbiCastellated',
    'ambi-castellated': 'AmbiCastellated', 'ambi castellated': 'AmbiCastellated',
    # AmbiFloat10
    'ambi-float': 'AmbiFloat10', 'ambifloat': 'AmbiFloat10',
    'ambi float': 'AmbiFloat10', 'ambi-float 10': 'AmbiFloat10',
}


def detect_system_from_row(raw_text):
    """Check for System row in manifold table - fastest method for new drawings."""
    for line in raw_text.split('\n'):
        if re.search(r'System', line, re.IGNORECASE):
            print(f"DEBUG system line: {repr(line)}")
    for line in raw_text.split('\n'):
        if re.match(r'\s*System\s+', line, re.IGNORECASE):
            line_lower = line.lower()
            for key, system in SYSTEM_ROW_MAP.items():
                if key in line_lower:
                    return system
    return None


def detect_system(raw_text, pdf_path, page_index):
    """Detect system type — checks System row first, then full text keyword scan."""
    system = detect_system_from_row(raw_text)
    if system:
        return system
    text_lower = raw_text.lower()
    for keyword, system in SYSTEM_MAP:
        if keyword in text_lower:
            print(f"DEBUG keyword match: {repr(keyword)} -> {system}")
            return system
    print(f"DEBUG detect_system: no match found on page {page_index}")
    return None




def _extract_values_from_chars(char_list):
    """Extract decimal values from a list of chars using x-position gap grouping."""
    import re
    if not char_list:
        return []
    char_list = sorted(char_list, key=lambda c: c['x0'])
    groups = []
    current = [char_list[0]]
    for c in char_list[1:]:
        if c['x0'] - current[-1]['x0'] > 8:
            groups.append(current)
            current = [c]
        else:
            current.append(c)
    groups.append(current)
    values = []
    for grp in groups:
        grp_text = decode_cid(''.join(c['text'] for c in grp))
        m = re.search(r'(\d+\.\d+)', grp_text)
        if m:
            v = float(m.group(1))
            if 0 < v < 300:
                values.append(v)
    return values

def extract_areas_from_chars_data(chars):
    """Same as extract_areas_from_chars but takes chars list directly (avoids re-opening PDF)."""
    import re
    if not chars:
        return [], []
    from collections import defaultdict
    y_rows = defaultdict(list)
    for c in chars:
        y_key = round(c['top'] * 2) / 2
        y_rows[y_key].append(c)
    gross_areas = []
    net_areas = []
    sorted_items = sorted(y_rows.items())
    for idx_r, (y, row_chars) in enumerate(sorted_items):
        row_chars.sort(key=lambda c: c['x0'])
        raw_text = decode_cid(''.join(c['text'] for c in row_chars))
        if re.search(r'Gross\s*Floor\s*Area', raw_text, re.IGNORECASE):
            vals = _parse_area_row(row_chars, raw_text)
            if not vals and idx_r + 1 < len(sorted_items):
                # Values may be on the very next row (0.5pt below label)
                next_chars = sorted(sorted_items[idx_r+1][1], key=lambda c: c['x0'])
                next_text = decode_cid(''.join(c['text'] for c in next_chars))
                vals = re.findall(r'(\d+\.\d+)\s*m', next_text)
                vals = [float(v) for v in vals if 0 < float(v) < 300]
            if not vals and idx_r > 0:
                prev_chars = sorted(sorted_items[idx_r-1][1], key=lambda c: c['x0'])
                vals = _extract_values_from_chars(prev_chars)
            gross_areas = vals
        elif re.search(r'Net\s*Floor\s*Area', raw_text, re.IGNORECASE):
            vals = _parse_area_row(row_chars, raw_text)
            if not vals and idx_r + 1 < len(sorted_items):
                next_chars = sorted(sorted_items[idx_r+1][1], key=lambda c: c['x0'])
                next_text = decode_cid(''.join(c['text'] for c in next_chars))
                vals = re.findall(r'(\d+\.\d+)\s*m', next_text)
                vals = [float(v) for v in vals if 0 < float(v) < 300]
            if not vals and idx_r > 0:
                prev_chars = sorted(sorted_items[idx_r-1][1], key=lambda c: c['x0'])
                vals = _extract_values_from_chars(prev_chars)
            net_areas = vals

    return gross_areas, net_areas


def _parse_area_row(row_chars, full_text):
    import re
    label_match = re.search(r'(?:Net|Gross)\s*Floor\s*Area', full_text, re.IGNORECASE)
    if not label_match:
        return []
    # Always try regex on full text first — most reliable across all drawing formats
    vals_regex = re.findall(r'(\d+\.\d+)\s*m', full_text)
    if vals_regex:
        return [float(v) for v in vals_regex if 0 < float(v) < 300]
    # Fallback: char-level grouping after label
    label_len = label_match.end()
    running = ''
    data_chars = []
    for c in row_chars:
        running += decode_cid(c['text'])
        if len(running) > label_len:
            data_chars.append(c)
    if not data_chars:
        return []
    groups = []
    current = [data_chars[0]]
    for c in data_chars[1:]:
        if c['x0'] - current[-1]['x0'] > 8:
            groups.append(current)
            current = [c]
        else:
            current.append(c)
    groups.append(current)
    values = []
    for grp in groups:
        grp_text = decode_cid(''.join(c['text'] for c in grp))
        m = re.search(r'(\d+\.\d+)', grp_text)
        if m:
            v = float(m.group(1))
            if 0 < v < 300:
                values.append(v)
    return values


def extract_areas_from_chars(page):
    """
    Extract Net and Gross floor areas using char-level Y-position grouping.
    Handles both normal and CID-encoded area rows, grouping by x-position gaps.
    """
    import re
    chars = page.chars
    if not chars:
        return [], []

    from collections import defaultdict
    y_rows = defaultdict(list)
    for c in chars:
        y_key = round(c['top'] * 2) / 2
        y_rows[y_key].append(c)

    parse_area_row = _parse_area_row  # use shared function

    gross_areas = []
    net_areas = []

    for y, row_chars in sorted(y_rows.items()):
        row_chars.sort(key=lambda c: c['x0'])
        raw_text = decode_cid(''.join(c['text'] for c in row_chars))
        if re.search(r'Gross\s*Floor\s*Area', raw_text, re.IGNORECASE):
            gross_areas = parse_area_row(row_chars)
        elif re.search(r'Net\s*Floor\s*Area', raw_text, re.IGNORECASE):
            net_areas = parse_area_row(row_chars)

    return gross_areas, net_areas

def _run_extraction_from_captured(captured, pdf_path, page_index, unit_index, split_x, unit_label, floor_name_override, project_ref_override):
    """Run extraction using pre-captured page data (from scan_and_extract)."""
    return extract_page(pdf_path, page_index,
                        unit_index=unit_index, split_x=split_x,
                        unit_label=unit_label, floor_name_override=floor_name_override,
                        project_ref_override=project_ref_override,
                        _preloaded=captured)


def extract_page(pdf_path, page_index, unit_index=None, split_x=None, unit_label=None, floor_name_override=None, project_ref_override=None, _preloaded=None):
    """Extract all BOM data from a single page."""
    try:
        import pdfplumber

        if _preloaded:
            # Use pre-captured data from scan_and_extract — no PDF open needed
            raw_text       = _preloaded['raw_text']
            all_tables     = _preloaded['all_tables']
            _page_chars    = _preloaded['_page_chars']
            _page_words    = _preloaded['_page_words']
            _page_chars_unit = _preloaded['_page_chars_unit']
            w, h           = _preloaded['w'], _preloaded['h']
            _has_loop_length = _preloaded['_has_loop_length']
        else:
            with pdfplumber.open(pdf_path) as pdf:
                page = pdf.pages[page_index]
                w, h = page.width, page.height

                raw_text = decode_cid(page.extract_text(layout=False, x_tolerance=3, y_tolerance=3) or "")
                if not raw_text.strip():
                    return {"unreadable": True, "floor_name": "Page {}".format(page_index + 1)}

                all_tables = page.extract_tables()
                _page_words = page.extract_words(x_tolerance=5, y_tolerance=3)
                _all_chars = page.chars

                if split_x is not None and unit_index is not None:
                    if unit_index == 0:
                        _page_chars_unit = [c for c in _all_chars if c['x0'] < split_x]
                    else:
                        _page_chars_unit = [c for c in _all_chars if c['x0'] >= split_x]
                    from collections import defaultdict as _ddu
                    _urows = _ddu(list)
                    for c in _page_chars_unit:
                        _urows[round(c['top'] / 2) * 2].append(c)
                    _ulines = []
                    for _uy in sorted(_urows):
                        _urow = sorted(_urows[_uy], key=lambda c: c['x0'])
                        _ulines.append(''.join(c['text'] for c in _urow))
                    raw_text = decode_cid('\n'.join(_ulines))
                else:
                    _page_chars_unit = _all_chars

                _chars_text = decode_cid(''.join(c['text'] for c in _page_chars_unit))
                _has_loop_length = re.search(r'Loop\s*Length|LoopLength', raw_text + _chars_text, re.IGNORECASE)
                _page_chars = _page_chars_unit if _has_loop_length else []
                _page_w, _page_h = w, h
        project_ref = ""
        _raw_joined = raw_text.replace('\n', ' ')

        # 1. MCL near CONSTRUCTION/REVISION (title block pattern)
        _mcl_m = re.search(r'(MCL[\d]+[\-A-Z0-9]*(?:[\-][A-Z0-9]+)*)\s*(?:CONSTRUCTION|REVISION|PROVISIONAL|AS\s+INSTALLED)', _raw_joined, re.IGNORECASE)
        if _mcl_m:
            project_ref = _mcl_m.group(1)

        # 2. Near DRAWING NUMBER label — most reliable for all formats
        if not project_ref:
            _m = re.search(r'DRAWING\s+NUMBER\s*(.*?)(?:SCALE|DATE\s+DRAWN|CHECKED)', _raw_joined, re.IGNORECASE)
            if _m:
                # The ref may be split by words like CONSTRUCTION, REVISION, revision numbers
                # Collect all hyphenated ref fragments from this block
                block = _m.group(1)
                fragments = re.findall(r'[A-Z]{2,}[\d\-A-Z\/\.]*[\d][A-Z\d\-\/\.]*', block)
                # Filter out pure keywords
                _keywords = {'CONSTRUCTION','PROVISIONAL','REVISION','INSTALLED','SCALE','DATE','DRAWN','CHECKED','FLOOR','SUB','DPM'}
                ref_parts = [f for f in fragments if f.upper() not in _keywords and len(f) > 3]
                if ref_parts:
                    candidate = '-'.join(ref_parts).strip('-')
                    # Clean up any double-hyphens
                    candidate = re.sub(r'-{2,}', '-', candidate)
                    if len(candidate) > 4:
                        project_ref = candidate

        # 3. WSO — only near title block (avoid picking up referenced drawing numbers)
        if not project_ref:
            _m = re.search(r'DRAWING\s+NUMBER[^\n]{0,60}(WSO\d+(?:-[A-Z])?)', _raw_joined, re.IGNORECASE)
            if not _m:
                # Fallback: WSO near revision/date block
                _m = re.search(r'(WSO\d+(?:-[A-Z])?)\s*\d{2}', _raw_joined)
            if _m: project_ref = _m.group(1)

        # 4. OP number
        if not project_ref:
            _m = re.search(r'\b(OP\d{5,})\b', raw_text)
            if _m: project_ref = _m.group(1)

        # 5. MCL fallback (looser, but strip trailing keywords)
        if not project_ref:
            _m = re.search(r'\b(MCL[\d]+[\-A-Z0-9]+)', raw_text)
            if _m:
                candidate = _m.group(1)
                candidate = re.sub(r'(CONSTRUCTION|REVISION|PROVISIONAL|SCALE|DATE).*$', '', candidate, flags=re.IGNORECASE).strip().rstrip('-')
                if len(candidate) > 4:
                    project_ref = candidate

        # 6. 659- style
        if not project_ref:
            _m = re.search(r'659-[\w\-]+', raw_text)
            if _m: project_ref = _m.group(0)

        # 7. SO345-style generic (last resort)
        if not project_ref:
            _m = re.search(r'\b([A-Z]{2,}\d+[-][A-Z0-9][-A-Z0-9\-]{4,})', raw_text)
            if _m: project_ref = _m.group(1)

        # Final cleanup — strip any trailing keywords that got concatenated
        if project_ref:
            project_ref = re.sub(r'(CONSTRUCTION|REVISION|PROVISIONAL|AS\s*INSTALLED|SCALE|DATE\s*DRAWN).*$', '', project_ref, flags=re.IGNORECASE).strip().rstrip('-')

        # Use override if provided (e.g. from full-page scan for multi-unit drawings)
        if project_ref_override:
            project_ref = project_ref_override

        # Floor name
        floor_name = floor_name_override or get_floor_name(raw_text) or "Floor {}".format(page_index + 1)

        # Drawing title from title block (e.g. 'Ground Floor_House Type BV1_Plots 03,04,05 & 06')
        drawing_title = floor_name  # default
        raw_ns = raw_text.replace(' ', '').replace('\n', '')
        title_match = re.search(r'TITLE([A-Za-z][\w\s_,&\.]{5,100}?)(?:Proposed|AsBuilt|AsInstalled)', raw_ns, re.IGNORECASE)
        if title_match:
            t = title_match.group(1).strip()
            # Re-add spaces before capitals and after underscores
            t = re.sub(r'_', ' ', t)
            t = re.sub(r'([a-z])([A-Z])', r'\1 \2', t)
            t = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', t)
            t = re.sub(r'\s+', ' ', t).strip()
            if 5 < len(t) < 100:
                drawing_title = t

        # Manifold count — use the highest of two methods
        # Method 1: count Heat Required At Manifold occurrences
        mf_count_heat = len(re.findall(r'Heat.{0,5}Required.{0,5}At.{0,5}Manifold', raw_text, re.IGNORECASE))
        # Method 2: count unique manifold name references (handles garbled text)
        mf_names = re.findall(r'Manifold[\s]*(?:MH|G|B)[\d\.]+', raw_text, re.IGNORECASE)
        mf_count_names = len(set(mf_names))
        num_manifolds = max(1, mf_count_heat, mf_count_names)

        # System type
        system_type = detect_system(raw_text, pdf_path, page_index)

        # Pipe size
        pipe_size = "16mm"
        if '12mm' in raw_text and '16mm' not in raw_text:
            pipe_size = "12mm"
        if '17mm' in raw_text:
            pipe_size = "17mm"
        for t in all_tables:
            for row in t:
                if not row or not row[0]:
                    continue
                label = decode_cid(str(row[0]).strip())
                if re.search(r'Pipe.{0,2}Size', label, re.IGNORECASE):
                    all_cells = decode_cid(" ".join(str(c) for c in row if c))
                    if '12 mm' in all_cells or '12mm' in all_cells:
                        pipe_size = "12mm"

        # ---------------------------------------------------------------
        # LOOP LENGTH EXTRACTION
        # Run ALL methods, then pick the most complete result.
        # ---------------------------------------------------------------

        def _score(groups):
            """Score a set of loop groups: total loops found."""
            return sum(len(g) for g in groups)

        # --- Method A: "Loop Length 52m 69m 21m" header row (older WMS style) ---
        method_a_groups = []
        try:
            raw_groups = []
            for line in raw_text.split('\n'):
                raw_groups.extend(extract_loop_groups_from_line(line))
            method_a_groups = deduplicate_groups(raw_groups)
        except Exception:
            pass

        # --- Method B: char-level Y-grouping on "Loop Length" rows ---
        # Only runs when Loop Length header style detected (chars already loaded above)
        method_b_groups = []
        if _page_chars and _has_loop_length:
            try:
                from collections import defaultdict as _dd2
                _y2 = _dd2(list)
                for _c2 in _page_chars:
                    _y2[round(_c2['top'] * 2) / 2].append(_c2)
                _sorted_rows = sorted(_y2.items())
                _char_groups = []
                for _idx2, (_y_val, _rc2) in enumerate(_sorted_rows):
                    _rc2.sort(key=lambda c: c['x0'])
                    _txt2 = decode_cid(''.join(c['text'] for c in _rc2))
                    if re.search(r'Loop\s*Length', _txt2, re.IGNORECASE):
                        _ll_count = len(re.findall(r'Loop\s*Length', _txt2, re.IGNORECASE))
                        # Trim chars before first 'Loop' to avoid label contamination
                        # e.g. "Manifold bracket Loop Length 45m..." — 45m is not a loop
                        _first_loop_m = re.search(r'Loop\s*Length', _txt2, re.IGNORECASE)
                        _first_loop_x = None
                        if _first_loop_m:
                            # Find x position of the 'L' in first 'Loop'
                            _loop_chars = [c for c in _rc2 if decode_cid(c['text']) == 'L']
                            for _lc in _loop_chars:
                                _nearby = decode_cid(''.join(
                                    decode_cid(c['text']) for c in _rc2
                                    if abs(c['x0'] - _lc['x0']) < 60
                                )).replace(' ','').lower()
                                if 'looplength' in _nearby or 'loop' in _nearby:
                                    _first_loop_x = _lc['x0']
                                    break
                        _val_chars = [c for c in _rc2 if _first_loop_x is None or c['x0'] >= _first_loop_x]
                        _found2 = [int(v) for v in re.findall(r'(\d+)\s*m', _txt2) if int(v) > 0]
                        if not _found2 and _idx2 + 1 < len(_sorted_rows):
                            _val_chars = sorted(_sorted_rows[_idx2+1][1], key=lambda c: c['x0'])
                            _val_txt = decode_cid(''.join(c['text'] for c in _val_chars))
                            _found2 = [int(v) for v in re.findall(r'(\d+)\s*m', _val_txt) if int(v) > 0]
                        if _found2:
                            _metre_pos = []
                            _cn = ''; _cx = None
                            for _ch in _val_chars:
                                _d = decode_cid(_ch['text'])
                                if _d.isdigit():
                                    if _cx is None: _cx = _ch['x0']
                                    _cn += _d
                                elif _d == 'm' and _cn:
                                    _metre_pos.append((_cx, int(_cn)))
                                    _cn = ''; _cx = None
                                elif not _d.strip():
                                    pass
                                else:
                                    _cn = ''; _cx = None
                            if _metre_pos:
                                _xvals = [x for x, v in _metre_pos]
                                _gaps = [(b-a, i) for i, (a,b) in enumerate(zip(_xvals, _xvals[1:]))]
                                _gaps_sorted = sorted(_gaps, reverse=True)
                                if _ll_count >= 2:
                                    _split_pts = sorted([_xvals[i+1] for g, i in _gaps_sorted[:_ll_count-1]])
                                    _groups_split = [[] for _ in range(_ll_count)]
                                    for _mx, _mv in _metre_pos:
                                        _gi = sum(1 for sp in _split_pts if _mx >= sp)
                                        _groups_split[_gi].append(_mv)
                                    for _g in _groups_split:
                                        if _g: _char_groups.append(_g)
                                else:
                                    _clean = [v for x, v in _metre_pos]
                                    if _gaps_sorted and _gaps_sorted[0][0] > 50 and _gaps_sorted[0][1] >= 1:
                                        _cut = _gaps_sorted[0][1] + 1
                                        if any(kw in _txt2.lower() for kw in ['gross', 'net', 'floor area', 'design temp', 'heat output']):
                                            _clean = [v for x, v in _metre_pos[:_cut]]
                                    if _clean:
                                        _char_groups.append(_clean)
                            else:
                                _char_groups.append(_found2)
                if _char_groups:
                    seen = []
                    for g in _char_groups:
                        if g not in seen:
                            seen.append(g)
                    method_b_groups = seen
            except Exception:
                pass

        # --- Method C: per-row format "RoomName LoopNo PipeCentres NNm L/Min ..." ---
        # Used in newer Export-to-PDF drawings where each loop is on its own line
        method_c_groups = []
        try:
            # Pattern: optional room ref, loop number, pipe centres, then NN m, then L/Min float
            # e.g. "001 (KITCHEN / FAMILY) 2 200 mm 59 m 3.0 ..."
            # e.g. "004 (HALL) 8 200 mm 40 m 2.0 ..."
            per_row_pattern = re.compile(
                r'\d{3}\s*\([^)]+\)\s+\d+\s+\d+\s*mm\s+(\d+)\s*m\b',
                re.IGNORECASE
            )
            # Also handle compact format without room name: "Loop No. N ... NN m ..."
            compact_pattern = re.compile(
                r'Loop\s*No\.\s*(\d+)[^\n]*?(\d+)\s*m\b',
                re.IGNORECASE
            )
            # Collect all per-row lengths — group by manifold using "Distributor N" boundaries
            lines = raw_text.split('\n')
            current_mf = []
            mf_buckets = [current_mf]
            for line in lines:
                if re.search(r'Distributor\s+\d+', line, re.IGNORECASE) and not current_mf:
                    pass  # first distributor header before any loops
                elif re.search(r'Distributor\s+\d+\s+Mass\s+Flow', line, re.IGNORECASE):
                    # Start a new manifold bucket after this
                    current_mf = []
                    mf_buckets.append(current_mf)
                m = per_row_pattern.search(line)
                if m:
                    val = int(m.group(1))
                    if 5 < val < 400:
                        current_mf.append(val)
            # Filter empty buckets and deduplicate within each
            for bucket in mf_buckets:
                if len(bucket) >= 1:
                    method_c_groups.append(bucket)
            # Sanity check: if per-row gave us more total loops than num_manifolds suggests,
            # also try treating all loops as a single manifold
            if not method_c_groups:
                all_per_row = []
                for line in lines:
                    m = per_row_pattern.search(line)
                    if m:
                        val = int(m.group(1))
                        if 5 < val < 400:
                            all_per_row.append(val)
                if all_per_row:
                    method_c_groups = [all_per_row]
        except Exception:
            pass

        # --- Method D: whole-page table detection fallback ---
        # Uses already-extracted tables (no re-open needed)
        # Manifold table can be anywhere on a WMS drawing
        method_d_groups = []
        try:
            for t in all_tables:
                for row in t:
                    if not row or not row[0]:
                        continue
                    label = decode_cid(str(row[0]).strip())
                    all_cells = decode_cid(" ".join(str(c) for c in row if c))
                    if re.search(r'Loop.{0,2}Length', label, re.IGNORECASE):
                        found = re.findall(r"(\d+)\s*m\b", all_cells)
                        candidate = [int(v) for v in found if int(v) > 0]
                        if candidate:
                            method_d_groups = [candidate]
        except Exception:
            pass

        # --- Pick best result: most loops found wins ---
        all_candidates = [
            ('A', method_a_groups),
            ('B', method_b_groups),
            ('C', method_c_groups),
            ('D', method_d_groups),
        ]
        best_source, best_groups = max(all_candidates, key=lambda x: _score(x[1]))

        # If two methods agree on count, prefer the one that matches num_manifolds
        top_score = _score(best_groups)
        tied = [(s, g) for s, g in all_candidates if _score(g) == top_score and top_score > 0]
        if len(tied) > 1:
            # Prefer whichever has group count matching num_manifolds
            for s, g in tied:
                if len(g) == num_manifolds:
                    best_source, best_groups = s, g
                    break

        per_manifold_groups = best_groups
        loops = [l for g in per_manifold_groups for l in g]
        manifold_loops = [len(g) for g in per_manifold_groups]
        _loop_method = best_source if loops else 'none'

        # Reconcile manifold count
        if len(per_manifold_groups) > num_manifolds:
            num_manifolds = len(per_manifold_groups)
        if not manifold_loops:
            manifold_loops = [len(loops)]


        # ---------------------------------------------------------------
        # AREA EXTRACTION
        # Run all methods, pick the one with most values found.
        # ---------------------------------------------------------------

        # Method 2: raw text line scan (fast - works well on clean Export-to-PDF drawings)
        gross_a2, net_a2 = [], []
        for line in raw_text.split('\n'):
            if not gross_a2 and re.search(r'Gross.{0,5}Floor.{0,5}Area', line, re.IGNORECASE):
                vals = re.findall(r'(\d+\.?\d*)\s*m', line)
                candidate = [float(v) for v in vals if 0 < float(v) < 500]
                if candidate:
                    gross_a2 = candidate
            if not net_a2 and re.search(r'Net.{0,5}Floor.{0,5}Area', line, re.IGNORECASE):
                vals = re.findall(r'(\d+\.?\d*)\s*m', line)
                candidate = [float(v) for v in vals if 0 < float(v) < 500]
                if candidate:
                    net_a2 = candidate

        # Method 1: char-level Y-position grouping — only if text method didn't get areas
        gross_a1, net_a1 = [], []
        if not gross_a2 and _page_chars:
            gross_a1, net_a1 = extract_areas_from_chars_data(_page_chars)

        # Method 3: use already-extracted tables (no extra page access needed)
        gross_a3, net_a3 = [], []
        try:
            for t in all_tables:
                for row in t:
                    if not row or not row[0]:
                        continue
                    label = decode_cid(str(row[0]).strip())
                    cells = [decode_cid(str(c)) for c in row if c]
                    all_cells = " ".join(cells)
                    hg = re.search(r'Gross.{0,5}Floor.{0,5}Area', label, re.IGNORECASE)
                    hn = re.search(r'Net.{0,5}Floor.{0,5}Area', label, re.IGNORECASE)
                    if hg:
                        vals = re.findall(r'(\d+\.?\d*)', all_cells)
                        # Filter: exclude values that are likely design temperatures (18-30°C)
                        # and ensure values look like floor areas (>0.5, <500, not whole numbers in 18-30 range)
                        gross_a3 = [float(v) for v in vals
                                    if 0.5 < float(v) < 500
                                    and not (float(v) == int(float(v)) and 18 <= float(v) <= 30)]
                    if hn:
                        vals = re.findall(r'(\d+\.?\d*)', all_cells)
                        net_a3 = [float(v) for v in vals
                                  if 0.5 < float(v) < 500
                                  and not (float(v) == int(float(v)) and 18 <= float(v) <= 30)]
        except Exception:
            pass

        # Pick best: most gross values wins; use matching net from same method
        # BUT: if Method 3 has roughly double the values of Method 1, it's likely
        # double-counting (e.g. temp column interleaved) — prefer Method 1 in that case
        gross_candidates = [gross_a1, gross_a2, gross_a3]
        net_candidates   = [net_a1,   net_a2,   net_a3  ]
        best_idx = max(range(3), key=lambda i: len(gross_candidates[i]))
        # Sanity: if Method 3 has ~2x the count of Method 1 and both found values, prefer Method 1
        if (gross_a3 and gross_a1 and
                len(gross_a3) >= len(gross_a1) * 1.8 and
                sum(gross_a1) > 0):
            best_idx = 0
        gross_areas = gross_candidates[best_idx]
        net_areas   = net_candidates[best_idx]
        # If the winning method has no net, try others
        if not net_areas:
            for n in net_candidates:
                if n:
                    net_areas = n
                    break

        # Sanity check: if gross total is implausibly large (>500m²) for the number of rooms,
        # the text method likely picked up garbled interleaved chars — try char method instead
        _gross_sum = sum(gross_areas) if gross_areas else 0
        if _gross_sum > 500 and _page_chars:
            _g_alt, _n_alt = extract_areas_from_chars_data(_page_chars)
            if _g_alt and sum(_g_alt) < _gross_sum:
                gross_areas = _g_alt
                net_areas = _n_alt if _n_alt else net_areas

        gross_total = round(sum(gross_areas), 1) if gross_areas else None
        net_total = round(sum(net_areas), 1) if net_areas else gross_total

        # --- Method E: OCR fallback ---
        # Triggered when loops or areas are missing/incomplete.
        # Scans the FULL page at 300dpi — reliable regardless of table position.
        _room_count = len(set(re.findall(r'\b0\d{2}\b', raw_text)))
        _loops_seem_incomplete = (not loops) or (_room_count > 2 and len(loops) < _room_count)
        _areas_missing = (not gross_total) or (not net_total)
        _area_method = 'none' if _areas_missing else 'text'
        if _loops_seem_incomplete or _areas_missing:
            try:
                import pdf2image as _pdf2img
                import pytesseract as _tess
                from PIL import ImageEnhance as _IE
                import os as _os
                # Point to Tesseract on Windows if not on PATH
                for _tpath in [
                    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
                    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
                    r'C:\Users\{}\AppData\Local\Programs\Tesseract-OCR\tesseract.exe'.format(
                        _os.environ.get('USERNAME', '')),
                ]:
                    if _os.path.exists(_tpath):
                        _tess.pytesseract.tesseract_cmd = _tpath
                        break

                # Convert full page at 300dpi — fast enough on a server, reliable anywhere
                _pages = _pdf2img.convert_from_path(pdf_path, dpi=300,
                                                    first_page=page_index+1,
                                                    last_page=page_index+1)
                _img = _pages[0]
                _img_enhanced = _IE.Contrast(_img).enhance(2)

                # Single full-page OCR pass — covers all table positions
                _ocr_full = _tess.image_to_string(_img_enhanced, config='--psm 6')

                # Extract Loop Length row if loops incomplete
                if _loops_seem_incomplete:
                    _ll_m = re.search(r'Loop\s*Length\s+([\d\s m]+)', _ocr_full, re.IGNORECASE)
                    if _ll_m:
                        _ocr_loops = [int(v) for v in re.findall(r'(\d+)\s*m\b', _ll_m.group(1))
                                      if 5 < int(v) < 400]
                        if len(_ocr_loops) > len(loops):
                            loops = _ocr_loops
                            manifold_loops = [len(loops)]
                            per_manifold_groups = [loops]
                            _loop_method = 'E (OCR)'
                    # Also try per-row format: "001 (ROOM) N NNNmm NNm"
                    if len(loops) < _room_count:
                        _pr_matches = re.findall(
                            r'\d{3}\s*\([^)]+\)\s+\d+\s+\d+\s*mm\s+(\d+)\s*m\b', _ocr_full)
                        if len(_pr_matches) > len(loops):
                            _ocr_pr = [int(v) for v in _pr_matches if 5 < int(v) < 400]
                            if _ocr_pr:
                                loops = _ocr_pr
                                manifold_loops = [len(loops)]
                                per_manifold_groups = [loops]
                                _loop_method = 'E (OCR)'

                # Extract Gross Floor Area if missing
                _area_method = 'E (OCR)' if _areas_missing else 'text'
                if not gross_total:
                    _gm = re.search(r'Gross\s*Floor\s*Area[\s\S]{0,200}?(\d+\.\d+)', _ocr_full, re.IGNORECASE)
                    if _gm:
                        _g_line_start = _ocr_full.find(_gm.group(0)[:20])
                        _g_line_end = _ocr_full.find('\n', _g_line_start + 50)
                        _g_line = _ocr_full[_g_line_start:_g_line_end if _g_line_end > 0 else _g_line_start+200]
                        _gvals = [float(v) for v in re.findall(r'(\d+\.\d+)', _g_line)
                                  if 0.5 < float(v) < 500]
                        if _gvals:
                            gross_total = round(sum(_gvals), 1)

                # Extract Net Floor Area if missing
                if not net_total:
                    _nm = re.search(r'Net\s*Floor\s*Area[\s\S]{0,200}?(\d+\.\d+)', _ocr_full, re.IGNORECASE)
                    if _nm:
                        _n_line_start = _ocr_full.find(_nm.group(0)[:20])
                        _n_line_end = _ocr_full.find('\n', _n_line_start + 50)
                        _n_line = _ocr_full[_n_line_start:_n_line_end if _n_line_end > 0 else _n_line_start+200]
                        _nvals = [float(v) for v in re.findall(r'(\d+\.\d+)', _n_line)
                                  if 0.5 < float(v) < 500]
                        if _nvals:
                            net_total = round(sum(_nvals), 1)

            except Exception:
                pass

        # Build warnings list for anything that couldn't be read
        warnings = []
        if not loops:
            warnings.append("Loop lengths could not be read — enter manually")
        elif len(loops) != sum(manifold_loops):
            warnings.append(f"Loop count mismatch: found {len(loops)} lengths for {sum(manifold_loops)} loops — check manually")
        if not gross_total:
            warnings.append("Gross floor area could not be read — enter manually")
        elif gross_areas and all(not re.search(r'\d+\.\d', str(v)) for v in []):
            pass  # fine
        if not net_total:
            warnings.append("Net floor area could not be read — enter manually")
        if not system_type:
            warnings.append("System type not detected — select manually")
        if not project_ref:
            warnings.append("Project reference not found — enter manually")
        if num_manifolds > 1 and len(manifold_loops) != num_manifolds:
            warnings.append(f"Expected {num_manifolds} manifolds but only split {len(manifold_loops)} — check loop grouping")
        # Note: area approximation warning removed — char-level extraction is reliable
        # enough that flagging it causes false alarms on correctly-reading drawings

        return {
            "floor_name":     floor_name,
            "drawing_title":  drawing_title,
            "system_type":    system_type,
            "project_ref":    project_ref,
            "num_manifolds":  num_manifolds,
            "manifold_loops": manifold_loops,
            "loops":          loops,
            "pipe_size":      pipe_size,
            "gross_area":     gross_total,
            "net_area":       net_total,
            "unit_label":     unit_label,
            "warnings":       warnings,
            "read_methods": {
                "loops": _loop_method,
                "areas": _area_method,
            },
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "floor_name": "Page {}".format(page_index + 1)}


def detect_units_on_page(raw_text, chars):
    """
    Detect whether a page has multiple side-by-side units (Type 5.01, Plot 3, Unit A etc).
    Returns a list of unit label strings, or empty list if single unit.
    Also returns the X split position between units (midpoint between the two label groups).
    """
    # Look for repeated unit labels near manifold data
    # Pattern: "Type 5.01", "Type 6.01", "Plot 03", "Unit A", "Unit 5", "Flat B" etc
    unit_pattern = re.compile(
        r'\b(Type\s+[\d\.]+|Plot\s+[\w\.]+|Unit\s+[\w\.]+|Flat\s+[\w\.]+)\b',
        re.IGNORECASE
    )
    # Also look for label appearing before each "Mass Flow Rate" on same line
    mf_line = ''
    for line in raw_text.split('\n'):
        if re.search(r'Mass\s+Flow\s+Rate', line, re.IGNORECASE) and re.search(r'Heat\s+Required', line, re.IGNORECASE):
            mf_line = line
            break

    if not mf_line:
        return [], None

    # Find unit labels on the manifold line
    labels_on_mf = unit_pattern.findall(mf_line)
    # Deduplicate preserving order
    seen = []
    for lb in labels_on_mf:
        clean = re.sub(r'\s+', ' ', lb.strip())
        if clean not in seen:
            seen.append(clean)

    if len(seen) < 2:
        return [], None

    # Find X split: look for where "Gross Floor Area" label appears twice on same char row
    from collections import defaultdict
    rows = defaultdict(list)
    for c in chars:
        rows[round(c['top'] / 2) * 2].append(c)

    split_x = None
    for y, row_chars in sorted(rows.items()):
        row_chars.sort(key=lambda c: c['x0'])
        row_text = ''.join(c['text'] for c in row_chars)
        # Find two occurrences of "GrossFloorArea" (or similar) label
        label_positions = [c['x0'] for c in row_chars
                           if c['text'] in ('G',) and
                           ''.join(rc['text'] for rc in row_chars[row_chars.index(c):row_chars.index(c)+5])
                           in ('Gross', 'GrossF')]
        # Simpler: find x positions of all 'G' chars that start 'Gross'
        gross_label_xs = []
        for idx, c in enumerate(row_chars):
            if c['text'] == 'G':
                word = ''.join(rc['text'] for rc in row_chars[idx:idx+5])
                if word.startswith('Gross'):
                    gross_label_xs.append(c['x0'])
        if len(gross_label_xs) >= 2:
            # Split x: find rightmost data char between the two label positions,
            # then take midpoint between that and the second label start.
            # This correctly places the split in the gap between the two tables.
            first_table_end_x = gross_label_xs[0]
            for c in row_chars:
                if gross_label_xs[0] < c['x0'] < gross_label_xs[1]:
                    if c['text'].strip():
                        first_table_end_x = max(first_table_end_x, c['x0'])
            split_x = (first_table_end_x + gross_label_xs[1]) / 2
            break

    if split_x is None:
        # Fallback: use x midpoint of page
        if chars:
            xs = [c['x0'] for c in chars]
            split_x = (min(xs) + max(xs)) / 2

    return seen, split_x


def scan_pdf_pages(pdf_path):
    """Scan all pages - lightweight pass using pypdf (low memory).
    pdfplumber only used per-floor during extract, not during scan."""
    try:
        import gc

        # Use pypdf for the scan — much lower memory than pdfplumber
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader

        reader = PdfReader(pdf_path)
        num_pages = len(reader.pages)
        page_texts = []
        for page in reader.pages:
            try:
                raw = decode_cid(page.extract_text() or "")
            except Exception:
                raw = ""
            page_texts.append(raw)
        del reader
        gc.collect()

        pages = []
        for i, raw in enumerate(page_texts):
            floor_name = get_floor_name(raw) or "Page {}".format(i + 1)
            mf_count = len(re.findall(r'Heat.{0,5}Required.{0,5}At.{0,5}Manifold', raw, re.IGNORECASE))
            is_unreadable = not raw.strip()
            # Unit detection only if text hints at multi-unit page
            _may_have_units = bool(re.search(
                r'\bType\s+\d|\bPlot\s+\d|\bUnit\s+[A-Z0-9]|\bPhase\s+\d', raw, re.IGNORECASE))
            units = []
            split_x = None
            if _may_have_units and not is_unreadable:
                try:
                    import pdfplumber
                    with pdfplumber.open(pdf_path) as pdf:
                        page = pdf.pages[i]
                        units, split_x = detect_units_on_page(raw, page.chars)
                    gc.collect()
                except Exception:
                    pass
            page_ref = ""
            if units:
                try:
                    full_result = extract_page(pdf_path, i)
                    page_ref = full_result.get('project_ref', '')
                    gc.collect()
                except Exception:
                    pass
            pages.append({
                "page_index":    i,
                "floor_name":    floor_name,
                "num_manifolds": max(1, mf_count),
                "unreadable":    is_unreadable,
                "units":         units,
                "split_x":       split_x,
                "project_ref":   page_ref,
            })

        return {"pages": pages, "total": len(pages)}
    except Exception as e:
        return {"error": str(e)}


def scan_and_extract(pdf_path):
    """
    For single-page, single-unit PDFs: scan and extract in one pdfplumber open() call.
    page.chars is expensive (13-20s on complex PDFs) — this ensures it's only called once.
    For multi-page or multi-unit PDFs, returns scan only; browser calls /extract separately.
    """
    try:
        import pdfplumber as _plumber
        with _plumber.open(pdf_path) as pdf:
            pages_info = []
            for i, page in enumerate(pdf.pages):
                raw = decode_cid(page.extract_text(layout=False, x_tolerance=3, y_tolerance=3) or "")
                floor_name = get_floor_name(raw) or "Page {}".format(i + 1)
                mf_count = len(re.findall(r'Heat.{0,5}Required.{0,5}At.{0,5}Manifold', raw, re.IGNORECASE))
                # Accessing page.chars here — expensive but cached for subsequent access
                is_unreadable = len(page.chars) == 0
                units, split_x = ([], None) if is_unreadable else detect_units_on_page(raw, page.chars)
                pages_info.append({
                    "page_index": i,
                    "floor_name": floor_name,
                    "num_manifolds": max(1, mf_count),
                    "unreadable": is_unreadable,
                    "units": units,
                    "split_x": split_x,
                    "project_ref": "",
                })

            scan_result = {"pages": pages_info, "total": len(pages_info)}

            # Combined extract only for single-page, single-unit, readable drawings
            if (len(pages_info) == 1 and
                    not pages_info[0].get('units') and
                    not pages_info[0].get('unreadable')):
                page = pdf.pages[0]
                # page.chars is now cached from the scan above — free to call again
                w, h = page.width, page.height
                raw_text = decode_cid(page.extract_text(layout=False, x_tolerance=3, y_tolerance=3) or "")
                all_tables = page.extract_tables()
                _page_words = page.extract_words(x_tolerance=5, y_tolerance=3)
                _all_chars = page.chars  # instant — cached
                _chars_text = decode_cid(''.join(c['text'] for c in _all_chars))
                _has_loop_length = re.search(r'Loop\s*Length|LoopLength', raw_text + _chars_text, re.IGNORECASE)
                _page_chars = _all_chars if _has_loop_length else []

                # Store captured data for extraction (runs after with block closes)
                _captured = {
                    'raw_text': raw_text, 'all_tables': all_tables,
                    '_page_chars': _page_chars, '_page_words': _page_words,
                    '_page_chars_unit': _all_chars, 'w': w, 'h': h,
                    '_has_loop_length': _has_loop_length,
                }
            else:
                _captured = None

        # Now run extraction outside the with block (doesn't need PDF open)
        if _captured:
            scan_result['extract'] = _run_extraction_from_captured(
                _captured, pdf_path, 0, None, None, None, None, None
            )

        return scan_result
    except Exception as e:
        import traceback; traceback.print_exc()
        # Fallback to regular scan
        return scan_pdf_pages(pdf_path)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=TOOL_DIR, **kwargs)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip()
                break

        pdf_data = None
        page_index = 0
        unit_index = None
        split_x = None
        unit_label = None
        floor_name_override = None
        project_ref_override = None

        if boundary:
            boundary_bytes = ("--" + boundary).encode()
            parts = body.split(boundary_bytes)
            for part in parts:
                if b"filename=" in part and b".pdf" in part.lower():
                    split = part.split(b"\r\n\r\n", 1)
                    if len(split) == 2:
                        pdf_data = split[1].rstrip(b"\r\n--")
                if b'name="page_index"' in part:
                    val_split = part.split(b"\r\n\r\n", 1)
                    if len(val_split) == 2:
                        try:
                            page_index = int(val_split[1].rstrip(b"\r\n--"))
                        except Exception:
                            page_index = 0
                if b'name="unit_index"' in part:
                    val_split = part.split(b"\r\n\r\n", 1)
                    if len(val_split) == 2:
                        try:
                            unit_index = int(val_split[1].rstrip(b"\r\n--"))
                        except Exception:
                            unit_index = None
                if b'name="split_x"' in part:
                    val_split = part.split(b"\r\n\r\n", 1)
                    if len(val_split) == 2:
                        try:
                            split_x = float(val_split[1].rstrip(b"\r\n--"))
                        except Exception:
                            split_x = None
                if b'name="unit_label"' in part:
                    val_split = part.split(b"\r\n\r\n", 1)
                    if len(val_split) == 2:
                        try:
                            unit_label = val_split[1].rstrip(b"\r\n--").decode('utf-8', errors='replace')
                        except Exception:
                            unit_label = None
                if b'name="floor_name_override"' in part:
                    val_split = part.split(b"\r\n\r\n", 1)
                    if len(val_split) == 2:
                        try:
                            floor_name_override = val_split[1].rstrip(b"\r\n--").decode('utf-8', errors='replace')
                        except Exception:
                            floor_name_override = None
                if b'name="project_ref_override"' in part:
                    val_split = part.split(b"\r\n\r\n", 1)
                    if len(val_split) == 2:
                        try:
                            project_ref_override = val_split[1].rstrip(b"\r\n--").decode('utf-8', errors='replace')
                        except Exception:
                            project_ref_override = None

        if pdf_data:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_data)
                tmp_path = tmp.name
            try:
                if self.path == "/scan":
                    result = scan_pdf_pages(tmp_path)
                elif self.path == "/scan_and_extract":
                    result = scan_and_extract(tmp_path)
                elif self.path == "/extract":
                    result = extract_page(tmp_path, page_index, unit_index=unit_index, split_x=split_x, unit_label=unit_label, floor_name_override=floor_name_override, project_ref_override=project_ref_override)
                else:
                    result = {"error": "Unknown endpoint"}
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            response = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(response))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"No PDF found"}')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    print("=" * 50)
    print("  WMS UFH BOM Generator")
    print("  Starting...")
    print("=" * 50)
    os.chdir(TOOL_DIR)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        url = "http://localhost:{}".format(PORT)
        print("\n  Tool is running at: {}".format(url))
        print("  Opening in browser...")
        print("\n  To stop: close this window\n")
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")


if __name__ == "__main__":
    main()
