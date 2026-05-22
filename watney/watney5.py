import re
import json
import html
import sqlite3
from pathlib import Path
from datetime import datetime

import pandas as pd
from nicegui import ui

# Tool name: WATNEY

# =============================================================================
# CONFIG
# =============================================================================

try:
    from importlib.metadata import version as _pkg_version
    WATNEY_VERSION = _pkg_version('watney')
except Exception:
    WATNEY_VERSION = '5'  # fallback for dev / non-package runs

def _major_version(v):
    """Return only the major version number, e.g. '3.0.1' -> '3'."""
    try:
        return str(v).split('.')[0]
    except Exception:
        return str(v)

NOTES_COL = 'all_notes'
GENERATION_COL = 'generation'
PATIENT_ID_COL = 'DFCI_MRN'

ANNOTATION_OUTPUT_DIR = Path('./watney_annotations')
ANNOTATION_OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

SQLITE_PATH = ANNOTATION_OUTPUT_DIR / 'watney_annotations_database.db'
CONFIG_PATH = ANNOTATION_OUTPUT_DIR / 'watney_config.json'

EXTRACTION_CSV_PATH = None
CURRENT_USER = None
UI_LOCKED = True
user_label = None
nav_bar = None
df = None
NOTE_FONT_SIZE = 11

# =============================================================================
# CONFIG FILE HELPERS
# =============================================================================

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)

_cfg = load_config()
if _cfg.get('csv_path'):
    EXTRACTION_CSV_PATH = _cfg['csv_path']
if _cfg.get('note_font_size'):
    NOTE_FONT_SIZE = int(_cfg['note_font_size'])

# =============================================================================
# LOAD DATA
# =============================================================================

def load_dataframe(path: str):
    return pd.read_csv(path, dtype={PATIENT_ID_COL: str})

if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
    df = load_dataframe(EXTRACTION_CSV_PATH)

# =============================================================================
# SQLITE SETUP
# =============================================================================

conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    DFCI_MRN TEXT NOT NULL,
    progression_date TEXT,
    progression_source TEXT,
    agent TEXT,
    evidence TEXT,
    report_id TEXT,
    determined_by TEXT,
    user TEXT,
    modification_timestamp TEXT,
    agent_start TEXT,
    agent_start_source TEXT,
    agent_end TEXT,
    agent_end_source TEXT
)
""")

cursor.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_progression_event
ON annotations (DFCI_MRN, progression_date, progression_source, report_id)
""")

for _col, _type in [
    ('agent_start',      'TEXT'), ('agent_start_source', 'TEXT'),
    ('agent_end',        'TEXT'), ('agent_end_source',   'TEXT'),
    ('exclusion_flag',   'TEXT'), ('exclusion_reason',   'TEXT'),
]:
    try:
        cursor.execute(f'ALTER TABLE annotations ADD COLUMN {_col} {_type}')
    except sqlite3.OperationalError:
        pass

conn.commit()

# =============================================================================
# SQLITE HELPERS
# =============================================================================

def load_annotations_df():
    return pd.read_sql_query("SELECT * FROM annotations", conn)

annotations_df = load_annotations_df()

def require_user():
    if not CURRENT_USER:
        ui.notify('Enter username first', color='red')
        return False
    return True

def refresh_annotations_df():
    global annotations_df
    annotations_df = load_annotations_df()

def save_annotations():
    conn.commit()
    refresh_annotations_df()

def safe_str(x):
    if pd.isna(x):
        return ''
    try:
        if isinstance(x, bytes):
            return x.decode('utf-8', errors='ignore').strip()
        return str(x).strip()
    except:
        return ''

def normalize_patient_id(x):
    if pd.isna(x):
        return ''
    s = str(x).strip()
    if s.endswith('.0'):
        s = s[:-2]
    return s

def upsert_annotation(new_row):
    cursor.execute("""
    SELECT id FROM annotations
    WHERE DFCI_MRN=? AND report_id=? AND progression_date=? AND progression_source=?
    """, (new_row['DFCI_MRN'], new_row['report_id'],
          new_row['progression_date'], new_row['progression_source']))
    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
        UPDATE annotations
        SET agent=?, evidence=?, determined_by=?, user=?, modification_timestamp=?,
            agent_start=?, agent_start_source=?, agent_end=?, agent_end_source=?
        WHERE id=?
        """, (
            new_row['agent'], new_row['evidence'], new_row['determined_by'],
            new_row.get('user'), new_row['modification_timestamp'],
            new_row.get('agent_start'), new_row.get('agent_start_source'),
            new_row.get('agent_end'),   new_row.get('agent_end_source'),
            existing['id']
        ))
    else:
        # Inherit any existing exclusion flag for this patient
        cursor.execute("""
            SELECT exclusion_flag, exclusion_reason FROM annotations
            WHERE DFCI_MRN=? AND exclusion_flag IS NOT NULL LIMIT 1
        """, (new_row['DFCI_MRN'],))
        _excl_row = cursor.fetchone()
        _eflag  = _excl_row['exclusion_flag']   if _excl_row else None
        _ereason = _excl_row['exclusion_reason'] if _excl_row else None
        cursor.execute("""
        INSERT INTO annotations (
            DFCI_MRN, progression_date, progression_source, agent, evidence,
            report_id, determined_by, user, modification_timestamp,
            agent_start, agent_start_source, agent_end, agent_end_source,
            exclusion_flag, exclusion_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_row['DFCI_MRN'], new_row['progression_date'], new_row['progression_source'],
            new_row['agent'], new_row['evidence'], new_row['report_id'],
            new_row['determined_by'], new_row.get('user'), new_row['modification_timestamp'],
            new_row.get('agent_start'), new_row.get('agent_start_source'),
            new_row.get('agent_end'),   new_row.get('agent_end_source'),
            _eflag, _ereason,
        ))
    save_annotations()

def save_agent_assignment(rid, agent_value, patient_id,
                          progression_date=None, evidence=None,
                          agent_start=None, agent_start_source=None,
                          agent_end=None, agent_end_source=None):
    if not require_user(): return
    if not agent_value:
        ui.notify('No agent selected', color='red')
        return
    upsert_annotation({
        'DFCI_MRN': str(patient_id).strip(),
        'progression_date': progression_date,
        'progression_source': 'LLM',
        'agent': agent_value, 'evidence': evidence, 'report_id': rid,
        'determined_by': None, 'user': CURRENT_USER,
        'modification_timestamp': datetime.now().isoformat(timespec='seconds'),
        'agent_start': agent_start, 'agent_start_source': agent_start_source,
        'agent_end': agent_end, 'agent_end_source': agent_end_source,
    })
    ui.notify(f'Saved: {agent_value}', color='green')

def save_clinician_progression_event(patient_id, progression_date, agent, evidence,
                                     report_id, determined_by,
                                     agent_start=None, agent_start_source=None,
                                     agent_end=None, agent_end_source=None):
    if not require_user(): return
    upsert_annotation({
        'DFCI_MRN': str(patient_id).strip(),
        'progression_date': progression_date,
        'progression_source': 'manual',
        'agent': agent, 'evidence': evidence, 'report_id': report_id,
        'determined_by': determined_by, 'user': CURRENT_USER,
        'modification_timestamp': datetime.now().isoformat(timespec='seconds'),
        'agent_start': agent_start, 'agent_start_source': agent_start_source,
        'agent_end': agent_end, 'agent_end_source': agent_end_source,
    })
    ui.notify('Clinician event saved', color='green')

def get_saved_annotation(patient_id, report_id, progression_date):
    cursor.execute("""
    SELECT agent, agent_start, agent_start_source, agent_end, agent_end_source
    FROM annotations WHERE DFCI_MRN=? AND report_id=? AND progression_date=? LIMIT 1
    """, (patient_id, report_id, progression_date))
    return cursor.fetchone()

def delete_agent_assignment(rid, patient_id, progression_date):
    cursor.execute("""
    DELETE FROM annotations WHERE DFCI_MRN=? AND report_id=? AND progression_date=?
    """, (patient_id, rid, progression_date))
    deleted = cursor.rowcount
    save_annotations()
    if deleted:
        ui.notify('Agent assignment removed', color='orange')
        return True
    ui.notify('No assignment found to remove', color='red')
    return False

def get_clinician_events(patient_id):
    patient_id = normalize_patient_id(patient_id)
    return pd.read_sql_query("""
    SELECT * FROM annotations WHERE DFCI_MRN=? AND progression_source='manual'
    ORDER BY progression_date
    """, conn, params=(patient_id,))

# exclusion_flag and exclusion_reason are stamped across all rows for a patient.

def safe_json_loads(x):
    if pd.isna(x): return {}
    try: return json.loads(x)
    except: return {}

def compress_blank_lines(text):
    return re.sub(r'\n\s*\n\s*\n+', '\n\n', text)

def extract_field(pattern, text, default='unknown'):
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else default

def normalize_any_date(x):
    if pd.isna(x): return None
    x = str(x).strip()
    if not x or x.lower() == 'nan': return None
    if x.upper() == 'NA': return 'NA'
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m/%d/%y", "%m/%d/%Y"):
        try: return datetime.strptime(x, fmt).date().isoformat()
        except: continue
    return x

def sort_date_key(x):
    d = normalize_any_date(x)
    if not d or pd.isna(d): return "9999-12-31"
    return str(d)

def clean_date_input(raw):
    if not raw: return None
    # --- CHANGE 3: preserve NA ---
    if raw.strip().upper() == 'NA': return 'NA'
    digits = re.sub(r'\D', '', raw.strip())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return raw.strip() or None

def get_agent_first_start(extraction, agent_name):
    systemic = extraction.get('systemic_therapy', {}) or {}
    selected = next((a for a in systemic.get('agents', []) if a.get('drug_name') == agent_name), None)
    if not selected: return None
    dates = [i.get('start_date') for i in selected.get('intervals', []) if i.get('start_date')]
    return sorted(dates, key=sort_date_key)[0] if dates else None

def get_agent_last_end(extraction, agent_name):
    systemic = extraction.get('systemic_therapy', {}) or {}
    selected = next((a for a in systemic.get('agents', []) if a.get('drug_name') == agent_name), None)
    if not selected: return None
    dates = [i.get('end_date') for i in selected.get('intervals', []) if i.get('end_date')]
    return sorted(dates, key=sort_date_key)[-1] if dates else None

# =============================================================================
# NOTE PARSING
# =============================================================================

def parse_notes(notes_text):
    if not isinstance(notes_text, str): return []
    notes_text = compress_blank_lines(notes_text)
    notes = []
    for note in re.split(r'={20,}', notes_text):
        note = note.strip()
        if not note: continue
        notes.append({
            'note_number': extract_field(r'Note Number:\s*(.+)', note),
            'report_id':   extract_field(r'Note Report ID:\s*(.+)', note),
            'note_date':   extract_field(r'Note Date:\s*(.+)', note),
            'dept':        extract_field(r'Note Dept:\s*(.+)', note),
            'author':      extract_field(r'Note Author:\s*(.+)', note),
            'raw_text':    note,
        })
    return notes

# =============================================================================
# HIGHLIGHTING
# =============================================================================

def highlight_evidence(note_text, evidence_text):
    if not evidence_text:
        return (html.escape(note_text), False)

    segments = [
        re.sub(r'\s+', ' ', s.strip())
        for s in re.split(r'\.\.\.|…', evidence_text)
        if s.strip()
    ]
    if not segments:
        return (html.escape(note_text), False)

    all_spans = []
    for segment in segments:
        escaped = re.sub(r'\\ ', r'\\s+', re.escape(segment))
        try:
            for m in re.finditer(escaped, note_text, re.IGNORECASE | re.DOTALL):
                all_spans.append(m.span())
        except:
            pass

    if not all_spans:
        return (html.escape(note_text), False)

    all_spans.sort()
    merged = [all_spans[0]]
    for s, e in all_spans[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    result = []
    prev = 0
    first = True
    for s, e in merged:
        result.append(html.escape(note_text[prev:s]))
        anchor_id = ' id="evidence-highlight"' if first else ''
        first = False
        result.append(
            f'<span class="evidence-highlight"{anchor_id}>'
            f'{html.escape(note_text[s:e])}</span>'
        )
        prev = e
    result.append(html.escape(note_text[prev:]))
    return (''.join(result), True)

# =============================================================================
# HTML RENDER
# =============================================================================

def build_notes_html(notes, highlighted_report=None, evidence_text=None):
    parts = []
    for note in notes:
        rid = note['report_id']
        raw = note['raw_text']
        if highlighted_report and rid == highlighted_report and evidence_text:
            rendered, _ = highlight_evidence(raw, evidence_text)
        else:
            rendered = html.escape(raw)
        parts.append(f"""
        <div id="report_{rid}" class="note-card">
            <div class="note-meta">
                <span><b>#{note['note_number']}</b></span>
                <span>{note['note_date']}</span>
                <span>{note['dept']}</span>
                <span>{note['author']}</span>
                <span>RID: {rid}</span>
            </div>
            <pre>{rendered}</pre>
        </div>""")
    return '\n'.join(parts)

# =============================================================================
# PAGE
# =============================================================================

_active_clients = {'count': 0}


@ui.page('/')
def build_page():
    global current_patient_index, agent_output, NOTE_FONT_SIZE
    global CURRENT_USER, UI_LOCKED, user_label, nav_bar, df
    global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR, CONFIG_PATH
    global conn, cursor, annotations_df, progression_sort_order

    current_patient_index  = 0
    agent_output           = None
    progression_sort_order = 'Ascending'
    _refresh_summary_holder = [None]

    _active_clients['count'] += 1
    from nicegui import app as _ngapp
    @_ngapp.on_disconnect
    def _on_disconnect():
        _active_clients['count'] = max(0, _active_clients['count'] - 1)

    # ── CSS ───────────────────────────────────────────────────────────────────
    ui.add_head_html(f"""<style>
body{{font-family:Arial;}}
.left-pane{{height:94vh;overflow-y:auto;padding-right:8px;box-sizing:border-box;}}
.right-pane{{height:94vh;overflow-y:auto;padding-left:8px;box-sizing:border-box;min-width:0;}}
#panel-divider{{width:4px;flex-shrink:0;cursor:col-resize;
    align-self:stretch;background:#e2e8f0;transition:background 0.15s;z-index:10;}}
#panel-divider:hover,#panel-divider.dragging{{background:#93c5fd;}}
.note-card{{border:1px solid #ddd;border-radius:4px;padding:4px 6px;margin-bottom:6px;background:#fafafa;}}
.note-meta{{display:flex;gap:10px;font-size:10px;color:#666;margin-bottom:3px;border-bottom:1px solid #eee;padding-bottom:2px;}}
pre{{white-space:pre-wrap;font-size:{NOTE_FONT_SIZE}px;line-height:1.4;margin:0;}}
.evidence-highlight{{background-color:#ffe066;padding:2px 3px;border-radius:2px;font-weight:bold;}}
.agent-box,.annotation-box{{border:1px solid #ddd;padding:8px;border-radius:5px;margin-bottom:10px;}}
.bottom-nav{{position:fixed;bottom:15px;left:15px;right:0;width:calc(37% - 15px);display:flex;
    align-items:center;justify-content:space-between;gap:6px;z-index:9999;
    background:rgba(255,255,255,0.95);padding:5px 12px;border-radius:6px;
    box-shadow:0 2px 8px rgba(0,0,0,0.12);}}
.patient-list-row:hover{{background:#f0f4ff;cursor:pointer;}}
</style>""")

    # ── Panel resize JS ──────────────────────────────────────────────────────
    ui.add_head_html('''
<script>
(function initResize() {
  var divider  = document.getElementById('panel-divider');
  var leftPane = document.querySelector('.left-pane');
  if (!divider || !leftPane) { setTimeout(initResize, 100); return; }

  var dragging = false, startX = 0, startW = 0;

  divider.addEventListener('mousedown', function(e) {
    dragging = true;
    startX   = e.clientX;
    startW   = leftPane.getBoundingClientRect().width;
    divider.classList.add('dragging');
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var rowW = leftPane.parentElement
               ? leftPane.parentElement.getBoundingClientRect().width
               : window.innerWidth;
    var newW = Math.min(Math.max(startW + (e.clientX - startX), 300), rowW - 200);
    leftPane.style.width      = newW + 'px';
    leftPane.style.minWidth   = newW + 'px';
    leftPane.style.flexShrink = '0';
    leftPane.style.flexGrow   = '0';
    var nav = document.querySelector('.bottom-nav');
    if (nav) nav.style.width = (newW - 15) + 'px';
  });

  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove('dragging');
    document.body.style.cursor     = '';
    document.body.style.userSelect = '';
  });
})();
</script>
''')

    # ── Layout ────────────────────────────────────────────────────────────────
    with ui.row().classes('w-full no-wrap').style('overflow:hidden;height:94vh;align-items:stretch;'):
        left_panel  = ui.column().classes('left-pane').style('min-width:300px;width:37%;flex-shrink:0;flex-grow:0;')
        ui.element('div').props('id=panel-divider')
        right_panel = ui.column().classes('right-pane').style('flex:1 1 0;min-width:180px;')

    lock_overlay = ui.column().classes(
        'fixed inset-0 flex items-center justify-center z-[9999]'
    ).style('background-color:white;pointer-events:all;')

    with lock_overlay:
        ui.label(f'WATNEY {_major_version(WATNEY_VERSION)}').classes('text-4xl font-bold')
        ui.separator().classes('mb-4')
        if _active_clients['count'] > 1:
            ui.label(
                f'⚠ {_active_clients["count"]} browser tabs are open. '
                'Multiple instances can cause unexpected behaviour — '
                'please close the other tabs before continuing.'
            ).classes('text-xs text-red-600 font-semibold text-center w-80 mb-2')
        step1 = ui.column().classes('items-center gap-2')
        step2 = ui.column().classes('items-center gap-2')
        step2.set_visibility(False)

        with step1:
            ui.label('Enter Name to Begin').classes('text-xl font-bold text-center')
            name_error = ui.label('').classes('text-xs text-red-500 text-center')
            user_select = ui.input(
                placeholder='Your name'
            ).classes('w-64 text-center').props('outlined dense')
            csv_note = ui.label('').classes('text-xs text-gray-500 text-center')
            if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
                csv_note.set_text(f'CSV: {Path(EXTRACTION_CSV_PATH).name}')

            def after_username():
                global CURRENT_USER
                if not user_select.value.strip():
                    name_error.set_text('Please enter your name to continue.')
                    user_select.run_method('focus')
                    return
                name_error.set_text('')
                CURRENT_USER = user_select.value.strip()
                if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
                    _finish_login()
                else:
                    step1.set_visibility(False)
                    step2.set_visibility(True)

            ui.button('Continue', on_click=after_username).classes('w-64')

        with step2:
            ui.label('Set Input CSV Path').classes('text-xl font-bold')
            ui.label('No CSV configured. Enter the full path to your extraction CSV.').classes(
                'text-xs text-gray-500 text-center w-72')
            csv_input = ui.input(label='CSV path', placeholder='/path/to/extraction.csv').classes('w-96')
            csv_error = ui.label('').classes('text-xs text-red-500')

            def set_csv_and_login():
                global df, EXTRACTION_CSV_PATH
                p = csv_input.value.strip()
                if not p: csv_error.set_text('Please enter a path.'); return
                path = Path(p)
                if not path.exists(): csv_error.set_text(f'File not found: {path}'); return
                try: df = load_dataframe(str(path))
                except Exception as e: csv_error.set_text(f'Could not load CSV: {e}'); return
                EXTRACTION_CSV_PATH = str(path)
                cfg = load_config(); cfg['csv_path'] = str(path); save_config(cfg)
                _finish_login()

            ui.button('Load & Enter', on_click=set_csv_and_login).classes('w-96')

        def _finish_login(demo=False):
            global UI_LOCKED
            UI_LOCKED = False
            lock_overlay.set_visibility(False)
            _hide_login_bottom()
            if user_label is not None: user_label.set_text(f'User: {CURRENT_USER}')
            if nav_bar is not None: nav_bar.style('display:flex')
            render_patient(current_patient_index, demo=demo)
            msg = 'Demo mode active — synthetic data only' if demo else f'Welcome {CURRENT_USER}'
            color = 'orange' if demo else 'green'
            ui.notify(msg, color=color)

    def require_user():
        if not CURRENT_USER: ui.notify('Set username first', color='red'); return False
        return True

    # ── Tutorial ──────────────────────────────────────────────────────────────
    _tut_fn_holder = [None]

    def _do_launch_tutorial():
        steps = [
            ('Welcome to WATNEY',
             'WATNEY helps you review and annotate LLM-extracted oncology progression events from clinical notes. '
             'Use the Tutorial + Demo button to explore with synthetic patients, or log in with your own data. '
             'Navigate these steps with the buttons below or by clicking the dots.',
             '🏥'),
            ('Log In',
             'Enter your name — this tags every annotation you make. '
             'If a CSV path is already saved, you go straight in. '
             'Otherwise you will be prompted to enter the path to your extraction CSV. '
             'WATNEY remembers it for next time.',
             '👤'),
            ('The Layout',
             'The left panel is your annotation workspace. The right panel shows the full clinical notes. '
             'Drag the thin bar between the panels left or right to resize them to your liking.',
             '⟺'),
            ('Navigate Patients',
             'Use Prev / Next in the nav bar, or press ← → on your keyboard, to move between patients. '
             '"Patient List" shows all patients and whether each has been annotated — click Go to jump directly to any of them.',
             '⟵⟶'),
            ('Progression Summary',
             'At the top of the left panel, the Progression Summary table shows every assigned progression event for the current patient: '
             'date, agent, start/end dates, source, and annotator. It updates the moment you save or remove an assignment.',
             '📋'),
            ('Agent Intervals Box',
             'Select an agent from the dropdown to see its treatment intervals extracted by the LLM. '
             'Each interval shows its start and end date. '
             'Click Start source or End source to jump to the note that supports that date and highlight the evidence.',
             '📅'),
            ('Progression Cards — Highlighted in Amber',
             'Each LLM-extracted progression event appears as a card. '
             'The card highlighted in amber is the most likely progression event for the currently selected agent — '
             'its "treatment plan at time of progression" field matches the agent name. '
             'A notice above the cards tells you which agent is matched. The matched card always appears at the top.',
             '🟡'),
            ('Assign an Agent to a Progression',
             'Inside a progression card: select the responsible agent, review the auto-filled start and end dates (they come from the LLM), '
             'edit if needed (blur the field to auto-format to YYYY-MM-DD), then click Save Agent Assignment. '
             'Use "No End Date" to record that no end date is known. '
             'Click Source to jump to the note and highlight the supporting evidence.',
             '💾'),
            ('Remove and Undo',
             'Remove Agent clears the assignment on a specific card without leaving the page. '
             'The Undo button in the nav bar deletes the most recently modified annotation for the current patient. '
             'Both actions update the Progression Summary immediately.',
             '↩'),
            ('Add a Clinician Progression Event',
             'Scroll to the bottom of the left panel to manually add a progression event. '
             'Agent is required. Enter a Progression Date OR press NO PROGRESSION for patients who did not progress on an agent. '
             'Start and end dates auto-fill when you pick an agent — use "No End Date" if applicable.',
             '🩺'),
            ('Exclude Patient',
             'Use the Exclude Patient button near the patient header to flag a patient for exclusion. '
             'A confirmation dialog will ask for a reason. Excluded patients show a banner and can be un-excluded.',
             '🚫'),
            ('Export',
             'Click Export in the nav bar to download all annotations as a timestamped CSV. '
             'In demo mode, only demo annotations are exported — your real database is never touched.',
             '📥'),
            ('Settings',
             'Open Settings from the nav bar to: adjust the note text font size, view or change the CSV and database paths, '
             'move the annotations folder, and check for WATNEY updates from PyPI. '
             'Use Logout (top-right of the left panel) to return to the login screen at any time.',
             '⚙️'),
        ]

        step_idx = [0]

        with ui.dialog() as tut_dlg, ui.card().classes('w-[540px]'):
            with ui.row().classes('w-full items-center justify-between mb-2'):
                ui.label('WATNEY Tutorial').classes('text-lg font-bold')
                progress_label = ui.label('').classes('text-xs text-gray-400')

            ui.separator()

            icon_label  = ui.label('').classes('text-4xl text-center w-full mt-4')
            title_label = ui.label('').classes('text-base font-bold text-blue-700 text-center w-full mt-2')
            body_label  = ui.label('').classes('text-sm text-gray-700 text-center w-full mt-2 leading-relaxed px-4')

            ui.element('div').style('height:24px;')

            with ui.row().classes('w-full justify-between items-center mt-4'):
                prev_btn = ui.button('← Back').props('flat dense')
                dot_row  = ui.row().classes('gap-1 items-center')
                next_btn = ui.button('Next →').props('dense')

            dots = []
            with dot_row:
                for i in range(len(steps)):
                    d = ui.element('div').style(
                        'width:8px;height:8px;border-radius:50%;'
                        'background:#cbd5e1;cursor:pointer;'
                    )
                    dots.append(d)

            def render_step(i):
                icon, title, body = steps[i][2], steps[i][0], steps[i][1]
                icon_label.set_text(icon)
                title_label.set_text(title)
                body_label.set_text(body)
                progress_label.set_text(f'{i+1} / {len(steps)}')
                prev_btn.set_visibility(i > 0)
                next_btn.set_text('Finish' if i == len(steps)-1 else 'Next →')
                for j, d in enumerate(dots):
                    d.style(
                        'width:8px;height:8px;border-radius:50%;cursor:pointer;'
                        + ('background:#3b82f6;' if j == i else 'background:#cbd5e1;')
                    )

            def go_prev():
                step_idx[0] = max(0, step_idx[0] - 1)
                render_step(step_idx[0])

            def go_next():
                if step_idx[0] == len(steps) - 1:
                    tut_dlg.close()
                else:
                    step_idx[0] = min(len(steps)-1, step_idx[0] + 1)
                    render_step(step_idx[0])

            prev_btn.on('click', go_prev)
            next_btn.on('click', go_next)

            for i, d in enumerate(dots):
                def jump(_, idx=i):
                    step_idx[0] = idx
                    render_step(idx)
                d.on('click', jump)

            render_step(0)

        tut_dlg.open()

    _tut_fn_holder[0] = _do_launch_tutorial

    # ── Demo + Tutorial bottom bar ────────────────────────────────────────────
    _login_bottom = ui.element('div').style(
        'position:fixed;bottom:20px;left:0;right:0;'
        'display:flex;flex-direction:column;align-items:center;gap:6px;z-index:10000;'
    )
    with _login_bottom:
        with ui.row().classes('items-center gap-3'):

            def launch_demo():
                global CURRENT_USER, df, EXTRACTION_CSV_PATH
                demo_path = Path(__file__).parent / 'watney_demo_data.csv'
                if not demo_path.exists():
                    ui.notify(f'Demo file not found: {demo_path}', color='red'); return
                try:
                    df = load_dataframe(str(demo_path))
                except Exception as e:
                    ui.notify(f'Could not load demo data: {e}', color='red'); return
                CURRENT_USER = user_select.value.strip() or 'Demo User'
                EXTRACTION_CSV_PATH = str(demo_path)
                _login_bottom.set_visibility(False)
                _finish_login(demo=True)

            def launch_tutorial():
                _tut_fn_holder[0]()

            ui.button('Try Demo', on_click=launch_demo).props('outline').classes('text-gray-500 text-sm')

            def launch_demo_with_tutorial():
                global CURRENT_USER, df, EXTRACTION_CSV_PATH
                demo_path = Path(__file__).parent / 'watney_demo_data.csv'
                if not demo_path.exists():
                    ui.notify(f'Demo file not found: {demo_path}', color='red'); return
                try:
                    df = load_dataframe(str(demo_path))
                except Exception as e:
                    ui.notify(f'Could not load demo data: {e}', color='red'); return
                CURRENT_USER = user_select.value.strip() or 'Demo User'
                EXTRACTION_CSV_PATH = str(demo_path)
                _login_bottom.set_visibility(False)
                _finish_login(demo=True)
                ui.timer(0.8, lambda: _tut_fn_holder[0]() if _tut_fn_holder[0] else None, once=True)

            ui.button('Tutorial + Demo', on_click=launch_demo_with_tutorial).props('outline').classes('text-blue-500 text-sm')

        ui.label('Demo uses synthetic data · nothing saved to your database').classes('text-xs text-gray-400')

    def _hide_login_bottom():
        _login_bottom.set_visibility(False)

    # ── Scroll ────────────────────────────────────────────────────────────────
    def scroll_to_note(report_id, evidence_text=''):
        row = df.iloc[current_patient_index]
        notes_html = build_notes_html(parse_notes(row[NOTES_COL]),
                                      highlighted_report=report_id, evidence_text=evidence_text)
        right_panel.clear()
        with right_panel: ui.html(notes_html).classes('w-full')
        ui.timer(0.05, lambda: ui.run_javascript(f"""
            const h=document.getElementById("evidence-highlight");
            if(h){{h.scrollIntoView({{behavior:"auto",block:"center"}});}}
            else{{const f=document.getElementById("report_{report_id}");
                  if(f)f.scrollIntoView({{behavior:"auto",block:"start"}});}}
        """), once=True)

    # ── Agent display ─────────────────────────────────────────────────────────
    def update_agent_display(agent_name, extraction):
        global agent_output
        agent_output.clear()
        systemic = extraction.get('systemic_therapy', {}) or {}
        selected = next((a for a in systemic.get('agents', []) if a.get('drug_name') == agent_name), None)
        if not selected: return
        intervals = sorted(selected.get('intervals', []), key=lambda x: sort_date_key(x.get('start_date')))
        with agent_output:
            for iv in intervals:
                start_date = iv.get('start_date', 'unknown')
                end_date   = iv.get('end_date', 'unknown')
                start_rat  = (iv.get('start_date_rationale') or {})
                end_rat    = (iv.get('end_date_rationale') or {})
                start_text = start_rat.get('text') or ''
                end_text   = end_rat.get('text') or ''
                start_rid  = start_rat.get('report_id') or ''
                end_rid    = end_rat.get('report_id') or ''

                with ui.row().classes('w-full items-center gap-1 flex-wrap'):
                    ui.label(f"{start_date} → {end_date}").classes('text-xs')
                    if start_rid or start_text:
                        ui.button(
                            'Start source',
                            on_click=lambda rid=start_rid, ev=start_text: scroll_to_note(rid, ev)
                        ).props('dense flat size=xs')
                    if end_rid or end_text:
                        ui.button(
                            'End source',
                            on_click=lambda rid=end_rid, ev=end_text: scroll_to_note(rid, ev)
                        ).props('dense flat size=xs')

    # ── Progression card ──────────────────────────────────────────────────────
    def _agent_matches_plan(agent_name, plan_text):
        if not agent_name or not plan_text:
            return False
        pattern = r'(?i)\b' + re.escape(agent_name.strip()) + r'\b'
        return bool(re.search(pattern, plan_text))

    def progression_card(event, patient_id, agent_names, extraction, active_agent=None):
        progression_date = event.get('progression_date', 'unknown')
        confidence       = event.get('confidence_level', 'unknown')
        rationale        = event.get('progression_date_rationale', {}) or {}
        report_id        = rationale.get('report_id', 'unknown')
        note_date        = rationale.get('note_date', 'unknown')
        author           = rationale.get('author', 'unknown')
        evidence         = rationale.get('text', '')
        treatment_plan   = event.get('treatment_plan_at_time') or ''

        matches = active_agent and _agent_matches_plan(active_agent, treatment_plan)
        card_style = ('border-left: 4px solid #f59e0b; background: #fffbeb;'
                      if matches else '')

        with ui.card().classes('w-full compact-card').style(card_style):
            with ui.row().classes('w-full justify-between items-center'):
                ui.label(progression_date).classes('left-main-text font-bold')
                ui.label(f'Confidence: {confidence}').classes('left-sub-text')

            ui.label(f'Progression Date: {progression_date}').classes('left-sub-text')
            ui.label(f'Note Date: {note_date}').classes('left-sub-text')
            ui.label(f'Author: {author}').classes('left-sub-text')
            ui.label(f'Report ID: {report_id}').classes('left-sub-text')
            if treatment_plan:
                match_style = 'font-weight:600; color:#b45309;' if matches else ''
                ui.label(f'Treatment at time: {treatment_plan}').classes('left-sub-text').style(match_style)

            if evidence:
                ui.markdown(f'> {evidence}').classes('left-evidence text-xs')

            ui.button('Source',
                on_click=lambda rid=report_id, ev=evidence: scroll_to_note(rid, ev)
            ).props('dense flat')

            def _get_saved_ann(pid, rid, pdate):
                _cr = _db().cursor()
                _cr.execute("""SELECT agent,agent_start,agent_start_source,agent_end,agent_end_source
                    FROM annotations WHERE DFCI_MRN=? AND report_id=? AND progression_date=? LIMIT 1""",
                    (pid, rid, pdate))
                return _cr.fetchone()

            saved              = _get_saved_ann(patient_id, report_id, progression_date)
            saved_agent        = saved['agent']              if saved else None
            saved_agent_start  = saved['agent_start']        if saved else None
            saved_start_source = saved['agent_start_source'] if saved else None
            saved_agent_end    = saved['agent_end']          if saved else None
            saved_end_source   = saved['agent_end_source']   if saved else None

            selected_agent = ui.select(
                agent_names,
                value=(saved_agent if saved_agent in agent_names else None),
                label='Assign agent'
            ).classes('w-full')

            # --- CHANGE 3: No End Date button on same row as start/end ---
            with ui.row().classes('w-full gap-2 mt-1 items-center'):
                start_input = ui.input(label='Start date', placeholder='YYYY-MM-DD').classes('flex-grow')
                end_input   = ui.input(label='End date',   placeholder='YYYY-MM-DD').classes('flex-grow')
                ui.button('No End Date',
                    on_click=lambda ei=end_input: ei.set_value('NA')
                ).props('dense outline').classes('text-gray-500')

            # --- CHANGE 3: guard NA in blur formatter ---
            def _fmt_date_field(field):
                v = field.value
                if not v or v.strip().upper() == 'NA': return
                d = re.sub(r'\D', '', v)
                if len(d) == 8:
                    field.set_value(f"{d[:4]}-{d[4:6]}-{d[6:8]}")

            start_input.on('blur', lambda _: _fmt_date_field(start_input))
            end_input.on('blur',   lambda _: _fmt_date_field(end_input))

            llm_hint = ui.label('').classes('text-xs text-gray-400')

            def refresh_dates(agent_name, _si=start_input, _ei=end_input, _lbl=llm_hint):
                ls = get_agent_first_start(extraction, agent_name)
                le = get_agent_last_end(extraction, agent_name)
                parts = []
                if ls: parts.append(f'LLM start: {ls}')
                if le: parts.append(f'LLM end: {le}')
                _lbl.set_text('  ·  '.join(parts))
                _si.value = ls or ''
                _ei.value = le or ''

            selected_agent.on('update:model-value', lambda _: refresh_dates(selected_agent.value))

            if saved_agent and saved_agent in agent_names:
                ls = get_agent_first_start(extraction, saved_agent)
                le = get_agent_last_end(extraction, saved_agent)
                parts = []
                if ls: parts.append(f'LLM start: {ls}')
                if le: parts.append(f'LLM end: {le}')
                llm_hint.set_text('  ·  '.join(parts))
                start_input.value = (saved_agent_start if saved_start_source == 'manual' and saved_agent_start else ls or '')
                end_input.value   = (saved_agent_end   if saved_end_source   == 'manual' and saved_agent_end   else le or '')

            def do_save(rid=report_id, sa=selected_agent, pid=patient_id,
                        prog_dt=progression_date, ev=evidence,
                        si=start_input, ei=end_input, _rs=None):
                av = sa.value
                if not av: ui.notify('No agent selected', color='red'); return
                ls = get_agent_first_start(extraction, av)
                le = get_agent_last_end(extraction, av)
                rs = (si.value or '').strip()
                re_ = (ei.value or '').strip()
                a_start     = clean_date_input(rs)  if rs  else ls
                a_start_src = 'manual' if rs  and rs  != ls else ('LLM' if ls else None)
                a_end       = clean_date_input(re_) if re_ else le
                a_end_src   = 'manual' if re_ and re_ != le else ('LLM' if le else None)
                _demo_upsert({
                    'DFCI_MRN': str(pid).strip(), 'progression_date': prog_dt,
                    'progression_source': 'LLM', 'agent': av, 'evidence': ev,
                    'report_id': rid, 'determined_by': None, 'user': CURRENT_USER,
                    'modification_timestamp': datetime.now().isoformat(timespec='seconds'),
                    'agent_start': a_start, 'agent_start_source': a_start_src,
                    'agent_end': a_end, 'agent_end_source': a_end_src,
                })
                ui.notify(f'Saved: {av}', color='green')
                if _refresh_summary_holder[0]:
                    _refresh_summary_holder[0]()

            ui.button('Save Agent Assignment',
                on_click=lambda rid=report_id, sa=selected_agent, pid=patient_id,
                                prog_dt=progression_date, ev=evidence,
                                si=start_input, ei=end_input:
                    do_save(rid, sa, pid, prog_dt, ev, si, ei)
            ).props('dense')

            def do_remove(rid=report_id, pid=patient_id, prog_dt=progression_date,
                          sel=selected_agent, si=start_input, ei=end_input, lbl=llm_hint):
                if _demo_mode[0]:
                    _cr = _db().cursor()
                    _cr.execute("""DELETE FROM annotations
                        WHERE DFCI_MRN=? AND report_id=? AND progression_date=?""",
                        (pid, rid, prog_dt))
                    _db().commit()
                    ui.notify('Agent assignment removed', color='orange')
                else:
                    delete_agent_assignment(rid, pid, prog_dt)
                sel.value = None
                si.value  = ''
                ei.value  = ''
                lbl.set_text('')
                if _refresh_summary_holder[0]:
                    _refresh_summary_holder[0]()

            ui.button('Remove Agent', on_click=do_remove).props('dense outline')

    # ── Patient list ──────────────────────────────────────────────────────────
    def show_patient_list():
        if _demo_mode[0]:
            _pa = pd.read_sql_query("SELECT DISTINCT DFCI_MRN FROM annotations", _db())
            annotated_mrns = set(_pa['DFCI_MRN'].apply(safe_str).tolist())
        else:
            refresh_annotations_df()
            annotated_mrns = set(annotations_df['DFCI_MRN'].apply(safe_str).tolist())

        # Load exclusion state: one row per patient (take any row with a flag)
        try:
            _excl_df = pd.read_sql_query(
                "SELECT DFCI_MRN, exclusion_flag FROM annotations WHERE exclusion_flag IS NOT NULL GROUP BY DFCI_MRN",
                _db()
            )
            excluded_mrns = set(
                _excl_df.loc[_excl_df['exclusion_flag'] == 'True', 'DFCI_MRN'].apply(safe_str).tolist()
            )
        except Exception:
            excluded_mrns = set()

        with ui.dialog() as dlg, ui.card().classes('w-[620px] max-h-[80vh] overflow-y-auto'):
            ui.label('Patient List').classes('text-lg font-bold mb-1')
            ui.label(f'{len(df)} patients total · {len(annotated_mrns)} with annotations · {len(excluded_mrns)} excluded').classes('text-xs text-gray-500 mb-2')
            with ui.row().classes('w-full text-xs font-bold border-b pb-1 mb-1'):
                ui.label('#').style('width:36px')
                ui.label('MRN').style('width:140px')
                ui.label('Annotated').style('width:80px')
                ui.label('Status').style('width:80px')
            for i, row in df.iterrows():
                pid = normalize_patient_id(row[PATIENT_ID_COL])
                has = safe_str(pid) in annotated_mrns
                is_excl = safe_str(pid) in excluded_mrns
                ann_color = 'color:#16a34a;font-weight:600;' if has else 'color:#dc2626;'
                excl_color = 'color:#dc2626;font-weight:600;' if is_excl else 'color:#16a34a;'
                with ui.row().classes('w-full text-xs items-center patient-list-row rounded px-1'):
                    ui.label(str(i+1)).style('width:36px;color:#999;')
                    ui.label(pid).style('width:140px;')
                    ui.label('Yes' if has else 'No').style(f'width:80px;{ann_color}')
                    ui.label('Excluded' if is_excl else 'Included').style(f'width:80px;{excl_color}')
                    def go(idx=i, d=dlg):
                        global current_patient_index
                        current_patient_index = idx; d.close(); render_patient(idx)
                    ui.button('Go', on_click=go).props('dense flat size=xs')
            ui.separator().classes('my-2')
            ui.button('Close', on_click=dlg.close).props('dense')
        dlg.open()

    # ── Export ────────────────────────────────────────────────────────────────
    def export_csv():
        if _demo_mode[0]:
            df_out = pd.read_sql_query("SELECT * FROM annotations", _db())
        else:
            df_out = load_annotations_df()
        if df_out.empty: ui.notify('No annotations to export', color='orange'); return
        import base64
        prefix = 'watney_demo_' if _demo_mode[0] else 'watney_annotations_'
        fn = f"{prefix}{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        b64 = base64.b64encode(df_out.to_csv(index=False).encode()).decode()
        ui.run_javascript(f"""
            const a=document.createElement('a');
            a.href='data:text/csv;base64,{b64}';a.download='{fn}';
            document.body.appendChild(a);a.click();document.body.removeChild(a);
        """)
        ui.notify(f'Exported {len(df_out)} rows → {fn}', color='green')

    # ── Settings ──────────────────────────────────────────────────────────────
    def show_settings():
        global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR
        global conn, cursor, df, NOTE_FONT_SIZE, CONFIG_PATH

        with ui.dialog() as dlg, ui.card().classes('w-[580px] max-h-[90vh] overflow-y-auto'):
            ui.label('Settings').classes('text-lg font-bold mb-2')

            ui.label('Session Info').classes('text-sm font-semibold mt-1')
            ui.label(f'WATNEY version: {WATNEY_VERSION}').classes('text-xs text-gray-600')
            ui.label(f'Current user: {CURRENT_USER or "not set"}').classes('text-xs text-gray-600')
            ui.separator()

            ui.label('Report Text Size').classes('text-sm font-semibold mt-1')
            with ui.row().classes('items-center gap-3 mt-1'):
                font_slider = ui.slider(min=8, max=20, step=1, value=NOTE_FONT_SIZE).classes('w-48')
                font_label  = ui.label(f'{NOTE_FONT_SIZE} px').classes('text-xs text-gray-600 w-12')

            def apply_font_size():
                global NOTE_FONT_SIZE
                NOTE_FONT_SIZE = int(font_slider.value)
                font_label.set_text(f'{NOTE_FONT_SIZE} px')
                ui.run_javascript(f"""
                    let el=document.getElementById('wfont');
                    if(!el){{el=document.createElement('style');el.id='wfont';document.head.appendChild(el);}}
                    el.textContent='pre{{font-size:{NOTE_FONT_SIZE}px!important;}}';
                """)
                cfg = load_config(); cfg['note_font_size'] = NOTE_FONT_SIZE; save_config(cfg)
                ui.notify(f'Font size set to {NOTE_FONT_SIZE}px', color='green')

            font_slider.on('update:model-value', lambda _: font_label.set_text(f'{int(font_slider.value)} px'))
            ui.button('Apply', on_click=apply_font_size).props('dense')
            ui.separator()

            ui.label('About').classes('text-sm font-semibold mt-1')
            ui.label(
                'WATNEY is an interactive oncology annotation platform for reviewing and '
                'validating LLM-extracted progression events and systemic therapy timelines '
                'from longitudinal clinical notes.'
            ).classes('text-xs text-gray-600')
            with ui.row().classes('items-center gap-1 mt-1'):
                ui.label('GitHub:').classes('text-xs text-gray-500')
                ui.link(
                    'justin-vinh/watney_project',
                    'https://github.com/justin-vinh/watney_project/tree/main',
                    new_tab=True
                ).classes('text-xs')
            ui.label('Developed by Justin Vinh @ Dana-Farber Cancer Institute').classes('text-xs text-gray-500 mt-1')
            ui.separator()

            ui.label('File Paths').classes('text-sm font-semibold mt-1')
            csv_path = Path(EXTRACTION_CSV_PATH).resolve() if EXTRACTION_CSV_PATH else None
            db_path  = Path(SQLITE_PATH).resolve()
            ui.label('Input CSV').classes('text-xs font-medium text-gray-700 mt-1')
            if csv_path:
                ui.label(f'Name: {csv_path.name}').classes('text-xs text-gray-600 ml-2')
                ui.label(f'Path: {csv_path}').classes('text-xs text-gray-500 ml-2 break-all')
            else:
                ui.label('Not set').classes('text-xs text-red-500 ml-2')
            ui.label('Database').classes('text-xs font-medium text-gray-700 mt-2')
            ui.label(f'Name: {db_path.name}').classes('text-xs text-gray-600 ml-2')
            ui.label(f'Path: {db_path}').classes('text-xs text-gray-500 ml-2 break-all')
            ui.label('Annotations Folder').classes('text-xs font-medium text-gray-700 mt-2')
            ui.label(f'Path: {Path(ANNOTATION_OUTPUT_DIR).resolve()}').classes('text-xs text-gray-500 ml-2 break-all')
            ui.separator()

            ui.label('Change Paths').classes('text-sm font-semibold mt-1')
            ui.label('Changing the annotations folder copies the DB and config to the new location and removes the old files.').classes('text-xs text-orange-600 mb-2')
            new_csv_input       = ui.input(label='New Input CSV path', placeholder='/path/to/extraction.csv').classes('w-full')
            new_annot_dir_input = ui.input(label='New annotations folder path', placeholder='/path/to/annotations_folder').classes('w-full')
            new_db_input        = ui.input(label='New database filename (optional)', placeholder='annotations.db').classes('w-full')
            path_status = ui.label('').classes('text-xs mt-1')

            def apply_paths():
                global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR
                global conn, cursor, df, annotations_df, CONFIG_PATH
                new_csv = new_csv_input.value.strip()
                new_annot = new_annot_dir_input.value.strip()
                new_db_name = new_db_input.value.strip()
                changed = False

                if new_csv:
                    p = Path(new_csv)
                    if not p.exists():
                        path_status.set_text(f'CSV not found: {p}')
                        path_status.classes('text-red-600', remove='text-green-600'); return
                    try:
                        df = load_dataframe(str(p)); EXTRACTION_CSV_PATH = str(p)
                        cfg = load_config(); cfg['csv_path'] = str(p); save_config(cfg)
                        changed = True
                    except Exception as e:
                        path_status.set_text(f'CSV load error: {e}')
                        path_status.classes('text-red-600', remove='text-green-600'); return

                if new_annot:
                    import shutil
                    old_dir = Path(ANNOTATION_OUTPUT_DIR).resolve()
                    new_dir = Path(new_annot).resolve()
                    if new_dir == old_dir: path_status.set_text('New folder is same as current.'); return
                    try:
                        new_dir.mkdir(exist_ok=True, parents=True)
                        old_db = Path(SQLITE_PATH).resolve()
                        old_cfg = Path(CONFIG_PATH).resolve()
                        db_name = new_db_name if new_db_name else old_db.name
                        new_db_p = new_dir / db_name
                        new_cfg_p = new_dir / old_cfg.name
                        conn.commit(); conn.close()
                        shutil.copy2(str(old_db), str(new_db_p))
                        if old_cfg.exists(): shutil.copy2(str(old_cfg), str(new_cfg_p))
                        new_conn = sqlite3.connect(str(new_db_p), check_same_thread=False)
                        new_conn.row_factory = sqlite3.Row
                        new_cursor = new_conn.cursor()
                        new_cursor.execute("""CREATE TABLE IF NOT EXISTS annotations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, DFCI_MRN TEXT NOT NULL,
                            progression_date TEXT, progression_source TEXT, agent TEXT,
                            evidence TEXT, report_id TEXT, determined_by TEXT, user TEXT,
                            modification_timestamp TEXT, agent_start TEXT, agent_start_source TEXT,
                            agent_end TEXT, agent_end_source TEXT,
                            exclusion_flag TEXT, exclusion_reason TEXT)""")
                        for _c, _t in [('agent_start','TEXT'),('agent_start_source','TEXT'),
                                       ('agent_end','TEXT'),('agent_end_source','TEXT'),
                                       ('exclusion_flag','TEXT'),('exclusion_reason','TEXT')]:
                            try: new_cursor.execute(f'ALTER TABLE annotations ADD COLUMN {_c} {_t}')
                            except sqlite3.OperationalError: pass
                        new_conn.commit()
                        conn = new_conn; cursor = new_cursor
                        ANNOTATION_OUTPUT_DIR = str(new_dir); SQLITE_PATH = str(new_db_p); CONFIG_PATH = new_cfg_p
                        cfg = load_config(); cfg['csv_path'] = EXTRACTION_CSV_PATH or cfg.get('csv_path',''); save_config(cfg)
                        try: old_db.unlink()
                        except: pass
                        try: old_cfg.unlink()
                        except: pass
                        try: old_dir.rmdir()
                        except: pass
                        changed = True
                    except Exception as e:
                        path_status.set_text(f'Folder move error: {e}')
                        path_status.classes('text-red-600', remove='text-green-600'); return
                elif new_db_name:
                    old_db = Path(SQLITE_PATH).resolve()
                    new_db_p = old_db.parent / new_db_name
                    try:
                        conn.commit(); conn.close(); old_db.rename(new_db_p)
                        new_conn = sqlite3.connect(str(new_db_p), check_same_thread=False)
                        new_conn.row_factory = sqlite3.Row
                        conn = new_conn; cursor = new_conn.cursor(); SQLITE_PATH = str(new_db_p)
                        changed = True
                    except Exception as e:
                        path_status.set_text(f'DB rename error: {e}')
                        path_status.classes('text-red-600', remove='text-green-600'); return

                if changed:
                    refresh_annotations_df(); render_patient(current_patient_index)
                    path_status.set_text('Paths updated successfully.')
                    path_status.classes('text-green-600', remove='text-red-600')
                    new_csv_input.value = ''; new_annot_dir_input.value = ''; new_db_input.value = ''
                else:
                    path_status.set_text('No changes entered.')
                    path_status.classes('text-gray-500', remove='text-red-600 text-green-600')

            ui.button('Apply', on_click=apply_paths).props('dense')
            ui.separator()

            ui.label('Database Stats').classes('text-sm font-semibold mt-1')
            stats_df = load_annotations_df()
            ui.label(f'Total annotations: {len(stats_df)}').classes('text-xs text-gray-600')
            ui.label(f'Unique patients: {stats_df["DFCI_MRN"].nunique() if not stats_df.empty else 0}').classes('text-xs text-gray-600')
            ui.label(f'LLM-sourced: {len(stats_df[stats_df["progression_source"]=="LLM"]) if not stats_df.empty else 0}').classes('text-xs text-gray-600')
            ui.label(f'Clinician-sourced: {len(stats_df[stats_df["progression_source"]=="manual"]) if not stats_df.empty else 0}').classes('text-xs text-gray-600')
            ui.separator().classes('my-2')

            ui.label('Updates').classes('text-sm font-semibold mt-1')
            ui.label(f'Installed version: {WATNEY_VERSION}').classes('text-xs text-gray-600')
            version_status = ui.label('').classes('text-xs text-gray-500 mt-1')
            update_btn = ui.button('Update WATNEY').props('dense outline').classes('mt-1')
            update_btn.set_visibility(False)

            async def check_version():
                import asyncio, re as _re
                version_status.set_text('Checking PyPI…')
                try:
                    proc = await asyncio.create_subprocess_exec(
                        'pip', 'index', 'versions', 'watney',
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, _ = await proc.communicate()
                    text = stdout.decode()
                    m = (_re.search(r'Available versions:\s*([\d.]+)', text)
                         or _re.search(r'\(([\d.]+)\)', text))
                    if m:
                        latest = m.group(1).strip()
                        if latest != str(WATNEY_VERSION):
                            version_status.set_text(f'Latest: {latest} — update available!')
                            version_status.classes('text-orange-600', remove='text-gray-500 text-green-600')
                            update_btn.set_visibility(True)
                            update_btn._props['label'] = f'Update to {latest}'
                            update_btn.update()
                        else:
                            version_status.set_text(f'Up to date (v{latest})')
                            version_status.classes('text-green-600', remove='text-gray-500 text-orange-600')
                            update_btn.set_visibility(False)
                    else:
                        version_status.set_text('Could not parse version info.')
                except Exception as ex:
                    version_status.set_text(f'Check failed: {ex}')

            async def do_update():
                import asyncio
                update_btn.set_visibility(False)
                version_status.set_text('Updating…')
                proc1 = await asyncio.create_subprocess_exec(
                    'pip', 'uninstall', 'watney', '-y',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc1.communicate()
                proc2 = await asyncio.create_subprocess_exec(
                    'pip', 'install', 'watney',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr = await proc2.communicate()
                if proc2.returncode == 0:
                    version_status.set_text('Update complete — please restart WATNEY.')
                    version_status.classes('text-green-600', remove='text-orange-600 text-gray-500')
                else:
                    version_status.set_text(f'Update failed: {stderr.decode()[:200]}')
                    version_status.classes('text-red-600', remove='text-green-600 text-orange-600')

            update_btn.on('click', do_update)
            ui.button('Check for updates', on_click=check_version).props('dense outline').classes('mt-1')

            ui.separator().classes('my-2')
            ui.button('Close', on_click=dlg.close).props('dense')
        dlg.open()

    # ── Demo / real DB plumbing ───────────────────────────────────────────────
    _demo_mode = [False]
    _demo_conn = [None]

    DEMO_SCHEMA = """
    CREATE TABLE IF NOT EXISTS annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        DFCI_MRN TEXT NOT NULL, progression_date TEXT,
        progression_source TEXT, agent TEXT, evidence TEXT,
        report_id TEXT, determined_by TEXT, user TEXT,
        modification_timestamp TEXT,
        agent_start TEXT, agent_start_source TEXT,
        agent_end TEXT, agent_end_source TEXT,
        exclusion_flag TEXT, exclusion_reason TEXT)"""

    def _ensure_demo_conn():
        if _demo_conn[0] is None:
            import sqlite3 as _sq
            dc = _sq.connect(':memory:', check_same_thread=False)
            dc.row_factory = _sq.Row
            dc.execute(DEMO_SCHEMA)
            dc.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_demo
                ON annotations(DFCI_MRN,progression_date,progression_source,report_id)""")
            dc.commit()
            _demo_conn[0] = dc
        return _demo_conn[0]

    def _db():
        return _ensure_demo_conn() if _demo_mode[0] else conn

    def _reset_demo_conn():
        if _demo_conn[0] is not None:
            try: _demo_conn[0].close()
            except: pass
            _demo_conn[0] = None

    def _demo_upsert(row):
        if not _demo_mode[0]:
            upsert_annotation(row)
            return
        dc = _db()
        cr = dc.cursor()
        cr.execute("""SELECT id FROM annotations
            WHERE DFCI_MRN=? AND report_id=? AND progression_date=? AND progression_source=?""",
            (row['DFCI_MRN'], row['report_id'], row['progression_date'], row['progression_source']))
        existing = cr.fetchone()
        if existing:
            cr.execute("""UPDATE annotations
                SET agent=?,evidence=?,determined_by=?,user=?,modification_timestamp=?,
                    agent_start=?,agent_start_source=?,agent_end=?,agent_end_source=?
                WHERE id=?""",
                (row['agent'], row['evidence'], row['determined_by'], row.get('user'),
                 row['modification_timestamp'], row.get('agent_start'), row.get('agent_start_source'),
                 row.get('agent_end'), row.get('agent_end_source'), existing['id']))
        else:
            # Inherit any existing exclusion flag for this patient
            _ecr = dc.cursor()
            _ecr.execute("""
                SELECT exclusion_flag, exclusion_reason FROM annotations
                WHERE DFCI_MRN=? AND exclusion_flag IS NOT NULL LIMIT 1
            """, (row['DFCI_MRN'],))
            _excl_row = _ecr.fetchone()
            _eflag   = _excl_row['exclusion_flag']   if _excl_row else None
            _ereason = _excl_row['exclusion_reason'] if _excl_row else None
            cr.execute("""INSERT INTO annotations
                (DFCI_MRN,progression_date,progression_source,agent,evidence,
                 report_id,determined_by,user,modification_timestamp,
                 agent_start,agent_start_source,agent_end,agent_end_source,
                 exclusion_flag,exclusion_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row['DFCI_MRN'], row['progression_date'], row['progression_source'],
                 row['agent'], row['evidence'], row['report_id'],
                 row['determined_by'], row.get('user'), row['modification_timestamp'],
                 row.get('agent_start'), row.get('agent_start_source'),
                 row.get('agent_end'), row.get('agent_end_source'),
                 _eflag, _ereason))
        dc.commit()

    def _get_exclusion(patient_id):
        """Return (exclusion_flag TEXT, exclusion_reason TEXT) from any row for this patient, or None."""
        try:
            _cr = _db().cursor()
            _cr.execute("""
                SELECT exclusion_flag, exclusion_reason FROM annotations
                WHERE DFCI_MRN=? AND exclusion_flag IS NOT NULL LIMIT 1
            """, (str(patient_id).strip(),))
            return _cr.fetchone()
        except Exception:
            return None

    def _set_exclusion(patient_id, flag, reason):
        """Stamp exclusion_flag and exclusion_reason onto ALL existing rows for this patient.
        Future rows inherit the flag via upsert_annotation / _demo_upsert."""
        _cr = _db().cursor()
        flag_str = 'True' if flag else 'False'
        _cr.execute("""
            UPDATE annotations
            SET exclusion_flag=?, exclusion_reason=?
            WHERE DFCI_MRN=?
        """, (flag_str, reason, str(patient_id).strip()))
        _db().commit()
        if not _demo_mode[0]:
            refresh_annotations_df()

    def _show_exclusion_dialog(patient_id, currently_excluded):
        from nicegui import context as _ctx
        _page_client = _ctx.slot.parent.client  # capture page client before dialog opens

        action_label = 'Remove Exclusion' if currently_excluded else 'Exclude Patient'
        with ui.dialog() as excl_dlg, ui.card().classes('w-[440px]'):
            ui.label(action_label).classes('text-base font-bold')
            if currently_excluded:
                ui.label('This patient is currently excluded. Remove the exclusion?').classes('text-sm text-gray-600')
            else:
                ui.label('Are you sure you want to exclude this patient?').classes('text-sm text-gray-600')
            reason_input = ui.textarea(label='Reason (required)').classes('w-full mt-2')
            reason_input.props('outlined dense')
            err_label = ui.label('').classes('text-xs text-red-500')
            status_label = ui.label('').classes('text-sm font-semibold mt-1')

            post_confirm_row = ui.row().classes('w-full justify-end gap-2 mt-2')
            post_confirm_row.set_visibility(False)
            action_row = ui.row().classes('w-full justify-end gap-2 mt-3')

            with post_confirm_row:
                def _go_next(d=excl_dlg):
                    d.close()
                    async def _deferred_next():
                        if current_patient_index < len(df) - 1:
                            next_patient()
                        else:
                            _page_client.run_javascript("console.log('last patient')")
                    import asyncio
                    asyncio.ensure_future(_deferred_next())
                ui.button('Stay on Patient', on_click=excl_dlg.close).props('flat dense')
                ui.button('Next Patient →', on_click=_go_next).props('dense')

            with action_row:
                def _do_confirm():
                    reason = reason_input.value.strip()
                    if not reason:
                        err_label.set_text('Please provide a reason.')
                        return
                    new_excluded = 0 if currently_excluded else 1
                    _set_exclusion(patient_id, new_excluded, reason)
                    if new_excluded:
                        status_label.set_text('✓ Patient has been excluded.')
                        status_label.classes('text-red-600', remove='text-green-600')
                    else:
                        status_label.set_text('✓ Patient has been re-included.')
                        status_label.classes('text-green-600', remove='text-red-600')
                    action_row.set_visibility(False)
                    reason_input.set_enabled(False)
                    post_confirm_row.set_visibility(True)
                    # Render outside dialog slot context using the captured page client
                    async def _deferred_render():
                        with _page_client:
                            render_patient(current_patient_index)
                    import asyncio
                    asyncio.ensure_future(_deferred_render())

                ui.button('Cancel', on_click=excl_dlg.close).props('flat dense')
                ui.button(action_label, on_click=_do_confirm).props(
                    'dense color=negative' if not currently_excluded else 'dense'
                )
        excl_dlg.open()

    # ── Render patient ────────────────────────────────────────────────────────
    def render_patient(index, demo=None):
        global agent_output, progression_sort_order, user_label
        if demo is not None:
            if demo and not _demo_mode[0]:
                _reset_demo_conn()
            _demo_mode[0] = demo
        if not _demo_mode[0]:
            refresh_annotations_df()
        left_panel.clear()
        right_panel.clear()

        row        = df.iloc[index]
        patient_id = normalize_patient_id(row[PATIENT_ID_COL])
        extraction = safe_json_loads(row[GENERATION_COL])
        notes_html = build_notes_html(parse_notes(row[NOTES_COL]))
        systemic   = extraction.get('systemic_therapy', {}) or {}
        drug_names = sorted([a.get('drug_name') for a in systemic.get('agents', []) if a.get('drug_name')])
        events     = extraction.get('progression', {}).get('progression_events', [])

        ui.run_javascript(f"""
            let el=document.getElementById('wfont');
            if(!el){{el=document.createElement('style');el.id='wfont';document.head.appendChild(el);}}
            el.textContent='pre{{font-size:{NOTE_FONT_SIZE}px!important;}}';
        """)

        with left_panel:
            with ui.row().classes('w-full items-start justify-between no-wrap'):
                with ui.column().classes('gap-0'):
                    ui.label(f'WATNEY {_major_version(WATNEY_VERSION)}').classes('text-2xl font-bold')
                    ui.label('Developed by Justin Vinh @ DFCI').classes('text-[11px] text-gray-500 leading-tight')
                    user_label = ui.label(f'User: {CURRENT_USER or "not set"}').classes('text-xs text-gray-600')
                    if _demo_mode[0]:
                        ui.label('⚠ DEMO MODE').classes('text-xs text-amber-600 font-bold')
                ui.button('Logout', on_click=_do_logout).props('dense flat size=sm').classes('text-gray-400')
            ui.separator().classes('mb-4')

            # --- CHANGE 2: patient header row with Exclude Patient button ---
            _excl = _get_exclusion(patient_id)
            _is_excluded = bool(_excl and _excl['exclusion_flag'] == 'True')

            with ui.row().classes('w-full items-center justify-between'):
                ui.label(f'Patient {patient_id} | Row {index+1}/{len(df)}').classes('text-xl font-bold')
                if not _is_excluded:
                    ui.button(
                        'Exclude Patient',
                        on_click=lambda pid=patient_id: _show_exclusion_dialog(pid, False)
                    ).props('dense outline size=sm')

            if _is_excluded:
                with ui.card().classes('w-full').style(
                    'background:#fef2f2;border:1px solid #fca5a5;padding:8px;margin-bottom:6px;'
                ):
                    ui.label('⚠ Patient Excluded').classes('text-sm font-bold text-red-600')
                    ui.label(f'Reason: {_excl["exclusion_reason"] or "No reason given"}').classes(
                        'text-xs text-red-500'
                    )
                    ui.button(
                        'Un-exclude Patient',
                        on_click=lambda pid=patient_id: _show_exclusion_dialog(pid, True)
                    ).props('dense outline size=sm').classes('text-green-700 mt-1')

            ui.label('Progression Summary').classes('text-sm font-bold')

            summary_container = ui.column().classes('w-full')

            def refresh_summary():
                summary_container.clear()
                summary_rows = []
                if _demo_mode[0]:
                    _df_ann = pd.read_sql_query("SELECT * FROM annotations", _db())
                else:
                    refresh_annotations_df()
                    _df_ann = annotations_df
                patient_annotations = _df_ann[
                    _df_ann['DFCI_MRN'].apply(safe_str) == safe_str(patient_id)
                ]
                for _, ann in patient_annotations.iterrows():
                    prog_date = normalize_any_date(ann.get('progression_date'))
                    if not prog_date: continue
                    summary_rows.append({
                        'date':        prog_date,
                        'sort_date':   sort_date_key(prog_date),
                        'agent':       ann.get('agent', '') or '',
                        'agent_start': ann.get('agent_start', '') or '',
                        'agent_end':   ann.get('agent_end', '') or '',
                        'source':      ann.get('progression_source', '') or '',
                        'user':        ann.get('user', '') or '',
                    })
                summary_rows.sort(key=lambda x: x.get('sort_date', '9999-12-31'))
                with summary_container:
                    if not summary_rows:
                        ui.label('NO PROGRESSION DATES ASSIGNED TO AGENTS').classes('text-xs text-red-500 font-bold')
                    else:
                        with ui.column().classes('w-full gap-1'):
                            with ui.row().classes('w-full text-xs font-bold border-b pb-1'):
                                ui.label('Prog. Date').style('width:88px;flex-shrink:0')
                                ui.label('Agent').style('flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap')
                                ui.label('Start').style('width:80px;flex-shrink:0')
                                ui.label('End').style('width:80px;flex-shrink:0')
                                ui.label('Source').style('width:52px;flex-shrink:0')
                                ui.label('User').style('width:62px;flex-shrink:0')
                            for rd in summary_rows:
                                with ui.row().classes('w-full text-xs').style('flex-wrap:nowrap'):
                                    _date_style = 'width:88px;flex-shrink:0;' + ('color:#d97706;font-weight:600;' if rd['date'] == 'NA' else '')
                                    _start_style = 'width:80px;flex-shrink:0;' + ('color:#d97706;font-weight:600;' if rd['agent_start'] == 'NA' else '')
                                    _end_style   = 'width:80px;flex-shrink:0;' + ('color:#d97706;font-weight:600;' if rd['agent_end'] == 'NA' else '')
                                    ui.label(rd['date']).style(_date_style)
                                    ui.label(rd['agent']).style('flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap')
                                    ui.label(rd['agent_start']).style(_start_style)
                                    ui.label(rd['agent_end']).style(_end_style)
                                    ui.label(rd['source']).style('width:52px;flex-shrink:0')
                                    ui.label(rd['user']).style('width:62px;flex-shrink:0')

            _refresh_summary_holder[0] = refresh_summary
            refresh_summary()

            with ui.column().classes('agent-box w-full'):
                ui.label('Agent Intervals').classes('text-sm font-bold')
                agent_output = ui.column()
                if drug_names:
                    dropdown = ui.select(drug_names, value=drug_names[0]).classes('w-full')
                    def on_agent_dropdown_change(_):
                        update_agent_display(dropdown.value, extraction)
                        render_events(active_agent=dropdown.value)
                    dropdown.on('update:model-value', on_agent_dropdown_change)
                    update_agent_display(drug_names[0], extraction)

            ui.separator()
            sort_select = ui.select(['Ascending','Descending'], value=progression_sort_order, label='Progression order').classes('w-full')
            ui.separator()

            ui.label('LLM Progression Events').classes('text-sm font-bold')
            highlight_notice = ui.label('').classes('text-xs text-amber-600 font-semibold')

            events_container = ui.column().classes('w-full')

            # --- CHANGE 4: matched card always sorts to top ---
            def render_events(active_agent=None):
                events_container.clear()
                matched   = [e for e in events if active_agent and _agent_matches_plan(active_agent, e.get('treatment_plan_at_time') or '')]
                unmatched = [e for e in events if not (active_agent and _agent_matches_plan(active_agent, e.get('treatment_plan_at_time') or ''))]
                matched   = sorted(matched,   key=lambda x: sort_date_key(x.get('progression_date')), reverse=(progression_sort_order == 'Descending'))
                unmatched = sorted(unmatched, key=lambda x: sort_date_key(x.get('progression_date')), reverse=(progression_sort_order == 'Descending'))
                ordered   = matched + unmatched

                any_match = bool(matched)
                if any_match:
                    highlight_notice.set_text(f'▶ Highlighted card: likely progression event for {active_agent}')
                else:
                    highlight_notice.set_text('')
                with events_container:
                    if not ordered:
                        ui.label('No LLM progression events').classes('text-xs text-gray-500')
                    else:
                        for event in ordered:
                            try:
                                progression_card(event, patient_id, drug_names, extraction,
                                                 active_agent=active_agent)
                            except Exception as _card_err:
                                ui.label(f'[Card error: {_card_err}]').classes('text-xs text-red-400')

            render_events(active_agent=drug_names[0] if drug_names else None)

            ui.separator()
            ui.label('Clinician Added Progression Events').classes('text-sm font-bold')
            if _demo_mode[0]:
                clin_events = pd.read_sql_query(
                    """SELECT * FROM annotations WHERE DFCI_MRN=? AND progression_source='manual'
                    ORDER BY progression_date""", _db(), params=(normalize_patient_id(patient_id),))
            else:
                clin_events = get_clinician_events(patient_id)
            if clin_events.empty:
                ui.label('No Clinician added progression events').classes('text-xs text-gray-500')
            else:
                for _, crow in clin_events.iterrows():
                    clin_card = ui.card().classes('w-full')
                    with clin_card:
                        ui.label(f"Date: {crow.get('progression_date','')}").classes('text-xs')
                        ui.label(f"Agent: {crow.get('agent','')}").classes('text-xs')
                        ui.label(f"Start: {crow.get('agent_start','')}  End: {crow.get('agent_end','')}").classes('text-xs')
                        ui.label(f"Evidence: {crow.get('evidence','')}").classes('text-xs')
                        ui.label(f"Determined by: {crow.get('determined_by','')}").classes('text-xs')
                        def delete_clin_event(rid=crow['report_id'], card=clin_card):
                            if _demo_mode[0]:
                                _cr = _db().cursor()
                                _cr.execute("DELETE FROM annotations WHERE report_id=?", (rid,))
                                _db().commit()
                            else:
                                cursor.execute("DELETE FROM annotations WHERE report_id=?", (rid,))
                                save_annotations()
                            ui.notify('Clinician event removed', color='orange')
                            card.delete()
                            refresh_summary()
                        ui.button('Remove', on_click=delete_clin_event).props('dense outline')
                        ui.label("CLINICIAN ENTRY").classes('text-xs text-red-500 font-bold')

            def on_sort_change(e):
                global progression_sort_order
                progression_sort_order = sort_select.value
                render_patient(current_patient_index)
            sort_select.on('update:model-value', on_sort_change)

            ui.separator()
            ui.label('Add Clinician Progression Event').classes('text-sm font-bold')

            _cfg_now = load_config()
            custom_agents = _cfg_now.get('custom_agents', [])
            all_agent_names = drug_names + [a for a in custom_agents if a not in drug_names]
            clinician_agent = ui.select(all_agent_names or [], label='Agent').classes('w-full')
            clin_llm_hint = ui.label('').classes('text-xs text-gray-400')

            with ui.row().classes('w-full items-center gap-1'):
                custom_agent_input = ui.input(label='Add custom agent', placeholder='e.g. Pembrolizumab').classes('flex-grow')
                def add_custom_agent():
                    name = custom_agent_input.value.strip()
                    if not name: ui.notify('Enter an agent name', color='red'); return
                    if name in clinician_agent.options:
                        ui.notify(f'{name} already in list', color='orange')
                    else:
                        clinician_agent.options = clinician_agent.options + [name]
                        ui.notify(f'Added {name}', color='green')
                    clinician_agent.value = name
                    clinician_agent.update(); custom_agent_input.value = ''
                ui.button('Add', on_click=add_custom_agent).props('dense outline')

            # --- CHANGE 3: No End Date button in clinician section ---
            with ui.row().classes('w-full gap-2 items-center'):
                clin_start_input = ui.input(label='Agent start date', placeholder='YYYY-MM-DD').classes('flex-grow')
                clin_end_input   = ui.input(label='Agent end date',   placeholder='YYYY-MM-DD').classes('flex-grow')
                ui.button('No End Date',
                    on_click=lambda: clin_end_input.set_value('NA')
                ).props('dense outline').classes('text-gray-500')

            def on_clin_agent_change(_):
                ls = get_agent_first_start(extraction, clinician_agent.value)
                le = get_agent_last_end(extraction, clinician_agent.value)
                parts = []
                if ls: parts.append(f'LLM start: {ls}')
                if le: parts.append(f'LLM end: {le}')
                clin_llm_hint.set_text('  ·  '.join(parts))
                clin_start_input.value = ls or ''
                clin_end_input.value   = le or ''

            clinician_agent.on('update:model-value', on_clin_agent_change)

            # --- CHANGE 3: guard NA in clinician blur formatter ---
            def _fmt_clin_date(field):
                v = field.value
                if not v or v.strip().upper() == 'NA': return
                d = re.sub(r'\D', '', v)
                if len(d) == 8:
                    field.set_value(f"{d[:4]}-{d[4:6]}-{d[6:8]}")

            clin_start_input.on('blur', lambda _: _fmt_clin_date(clin_start_input))
            clin_end_input.on('blur',   lambda _: _fmt_clin_date(clin_end_input))

            # --- CHANGE 1: NO PROGRESSION toggle ---
            _no_progression = [False]

            with ui.row().classes('w-full items-center gap-2'):
                clinician_date = ui.input(
                    label='Progression Date (YYYY-MM-DD)', placeholder='YYYY-MM-DD'
                ).classes('flex-grow')
                no_prog_btn = ui.button(
                    'NO PROGRESSION',
                    on_click=lambda: _toggle_no_progression()
                ).props('dense outline').classes('text-red-600')

            def _toggle_no_progression():
                _no_progression[0] = not _no_progression[0]
                if _no_progression[0]:
                    clinician_date.set_value('NA')
                    clinician_date.set_enabled(False)
                    no_prog_btn.props('dense color=red')
                    no_prog_btn.classes('text-white', remove='text-red-600')
                else:
                    clinician_date.set_value('')
                    clinician_date.set_enabled(True)
                    no_prog_btn.props('dense outline')
                    no_prog_btn.classes('text-red-600', remove='text-white')

            def validate_clin_date():
                v = clinician_date.value
                if not v: return
                d = re.sub(r'\D', '', v)
                if len(d) != 8: ui.notify('Invalid date: enter YYYYMMDD', color='red'); return
                clinician_date.set_value(f"{d[:4]}-{d[4:6]}-{d[6:8]}")
            clinician_date.on('blur', lambda e: validate_clin_date())

            clinician_evidence = ui.textarea(label='Evidence (optional)').classes('w-full')
            clinician_evidence.props('dense outlined')
            clinician_evidence.style('font-family: Arial, sans-serif; font-size: 14px;')

            clinician_report_id     = ui.input(label='Report ID of Evidence (optional)').classes('w-full')
            clinician_determined_by = ui.input(label='Determined by', placeholder='e.g., Dr. X, tumor board').classes('w-full')

            def save_clinician_event():
                row_d = df.iloc[current_patient_index]
                pid   = normalize_patient_id(row_d[PATIENT_ID_COL])
                if not clinician_agent.value:
                    ui.notify('Agent is required', color='red'); return

                # accept NA (no progression) in place of a date
                if _no_progression[0]:
                    cleaned_date = 'NA'
                else:
                    if not clinician_date.value or not clinician_date.value.strip():
                        ui.notify('Enter a progression date or press NO PROGRESSION to record no progression', color='red'); return
                    d = re.sub(r'\D', '', clinician_date.value or '')
                    if d and len(d) != 8: ui.notify('Date must be YYYYMMDD', color='red'); return
                    cleaned_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else None
                    if clinician_date.value and not cleaned_date: ui.notify('Invalid date.', color='red'); return

                rid = clinician_report_id.value.strip() if clinician_report_id.value.strip() else 'NA'
                ls = get_agent_first_start(extraction, clinician_agent.value)
                le = get_agent_last_end(extraction, clinician_agent.value)
                rs  = (clin_start_input.value or '').strip()
                re_ = (clin_end_input.value or '').strip()
                a_start     = clean_date_input(rs)  if rs  else ls
                a_start_src = 'manual' if rs  and rs  != ls else ('LLM' if ls else None)
                a_end       = clean_date_input(re_) if re_ else le
                a_end_src   = 'manual' if re_ and re_ != le else ('LLM' if le else None)
                _demo_upsert({
                    'DFCI_MRN': str(pid).strip(), 'progression_date': cleaned_date,
                    'progression_source': 'manual',
                    'agent': clinician_agent.value,
                    'evidence': (clinician_evidence.value or ''),
                    'report_id': rid,
                    'determined_by': clinician_determined_by.value,
                    'user': CURRENT_USER,
                    'modification_timestamp': datetime.now().isoformat(timespec='seconds'),
                    'agent_start': a_start, 'agent_start_source': a_start_src,
                    'agent_end': a_end, 'agent_end_source': a_end_src,
                })
                if not _demo_mode[0]: pass
                else: ui.notify('Clinician event saved', color='green')

                # --- CHANGE 1: reset NO PROGRESSION toggle on save ---
                _no_progression[0] = False
                clinician_date.set_enabled(True)
                no_prog_btn.props('dense outline')
                no_prog_btn.classes('text-red-600', remove='text-white')

                clinician_date.value = ''; clinician_evidence.value = ''
                clinician_determined_by.value = ''; clinician_agent.value = None
                clinician_report_id.value = ''; clin_start_input.value = ''
                clin_end_input.value = ''; clin_llm_hint.set_text('')
                render_patient(current_patient_index)

            ui.button('Save Clinician Progression Event', on_click=save_clinician_event).props('dense')
            ui.separator().classes('my-4')

        with right_panel:
            ui.label('All Relevant Notes').classes('text-lg font-bold mb-2')
            ui.html(notes_html).classes('w-full')

    # ── Navigation ────────────────────────────────────────────────────────────
    def next_patient():
        global current_patient_index
        if current_patient_index < len(df) - 1:
            current_patient_index += 1; render_patient(current_patient_index)

    def prev_patient():
        global current_patient_index
        if current_patient_index > 0:
            current_patient_index -= 1; render_patient(current_patient_index)

    def _prev_patient():
        if not require_user(): return
        prev_patient()

    def _next_patient():
        if not require_user(): return
        next_patient()

    def _show_patient_list():
        if not require_user(): return
        show_patient_list()

    def _export_csv():
        if not require_user(): return
        export_csv()

    def _show_settings():
        if not require_user(): return
        show_settings()

    # ── Undo ─────────────────────────────────────────────────────────────────
    def _undo_last():
        if not require_user(): return
        if df is None: return
        row = df.iloc[current_patient_index]
        patient_id = normalize_patient_id(row[PATIENT_ID_COL])
        _cr = _db().cursor()
        _cr.execute("""
            SELECT id FROM annotations
            WHERE DFCI_MRN = ?
            ORDER BY modification_timestamp DESC
            LIMIT 1
        """, (patient_id,))
        row_result = _cr.fetchone()
        if not row_result:
            ui.notify('Nothing to undo', color='orange')
            return
        _cr.execute("DELETE FROM annotations WHERE id = ?", (row_result['id'],))
        _db().commit()
        if not _demo_mode[0]:
            refresh_annotations_df()
        ui.notify('Last annotation undone', color='orange')
        render_patient(current_patient_index)

    # ── Logout ───────────────────────────────────────────────────────────────
    def _do_logout():
        global CURRENT_USER, df, EXTRACTION_CSV_PATH, UI_LOCKED
        _reset_demo_conn()
        _demo_mode[0] = False
        CURRENT_USER  = None
        UI_LOCKED     = True
        _cfg_r = load_config()
        if _cfg_r.get('csv_path') and Path(_cfg_r['csv_path']).exists():
            EXTRACTION_CSV_PATH = _cfg_r['csv_path']
            df = load_dataframe(EXTRACTION_CSV_PATH)
        else:
            EXTRACTION_CSV_PATH = None
            df = None
        left_panel.clear()
        right_panel.clear()
        lock_overlay.set_visibility(True)
        _login_bottom.set_visibility(True)
        if nav_bar is not None: nav_bar.style('display:none')

    # ── Nav bar ───────────────────────────────────────────────────────────────
    nav_bar = ui.element('div').classes('bottom-nav').style('display:none')
    with nav_bar:
        with ui.element('div').style('display:flex;gap:6px;'):
            ui.button('Prev', on_click=_prev_patient).props('dense')
            ui.button('Next', on_click=_next_patient).props('dense')
        ui.element('div').style('width:1px;background:#ccc;height:24px;margin:0 4px;flex-shrink:0;')
        with ui.element('div').style('display:flex;gap:6px;'):
            ui.button('Undo', on_click=_undo_last).props('dense outline color=orange')
        ui.element('div').style('width:1px;background:#ccc;height:24px;margin:0 4px;flex-shrink:0;')
        with ui.element('div').style('display:flex;gap:6px;'):
            ui.button('Patient List', on_click=_show_patient_list).props('dense outline')
            ui.button('Export',       on_click=_export_csv).props('dense outline')
            ui.button('Settings',     on_click=_show_settings).props('dense outline')

    # ── Keyboard navigation ───────────────────────────────────────────────────
    def _on_key(e):
        if not e.action.keydown or e.action.repeat:
            return
        if e.key == 'ArrowRight':
            _next_patient()
        elif e.key == 'ArrowLeft':
            _prev_patient()

    ui.keyboard(on_key=_on_key)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    _favicon = Path(__file__).parent / 'watney_icon.ico'
    _favicon_arg = str(_favicon) if _favicon.exists() else None
    try:
        ui.run(title='WATNEY', reload=False, favicon=_favicon_arg)
    except KeyboardInterrupt:
        pass

if __name__ in {'__main__', '__mp_main__', '<run_path>'}:
    main()