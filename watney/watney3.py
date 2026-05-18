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

WATNEY_VERSION = 3

NOTES_COL = 'all_notes'
GENERATION_COL = 'generation'
PATIENT_ID_COL = 'DFCI_MRN'

ANNOTATION_OUTPUT_DIR = Path('watney_annotations')
ANNOTATION_OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

SQLITE_PATH = (
    ANNOTATION_OUTPUT_DIR /
    'progression_annotations_database.db'
)

CONFIG_PATH = ANNOTATION_OUTPUT_DIR / 'watney_config.json'

EXTRACTION_CSV_PATH = None  # loaded from config or set at login

CURRENT_USER = None
UI_LOCKED = True
user_label = None
nav_bar = None
df = None  # loaded after CSV path is confirmed

# =============================================================================
# CONFIG FILE HELPERS
# =============================================================================

def load_config():
    """Load persisted config from JSON. Returns dict."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    """Persist config to JSON."""
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)

_cfg = load_config()
if _cfg.get('csv_path'):
    EXTRACTION_CSV_PATH = _cfg['csv_path']

# =============================================================================
# LOAD DATA (deferred until CSV path is known)
# =============================================================================

def load_dataframe(path: str):
    """Load the extraction CSV into df. Raises on failure."""
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

    modification_timestamp TEXT
)
""")

cursor.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_progression_event
ON annotations (
    DFCI_MRN,
    progression_date,
    progression_source,
    report_id
)
""")

conn.commit()

# =============================================================================
# SQLITE HELPERS
# =============================================================================

def load_annotations_df():

    return pd.read_sql_query(
        "SELECT * FROM annotations",
        conn
    )


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
            return x.decode(
                'utf-8',
                errors='ignore'
            ).strip()

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
    SELECT id
    FROM annotations
    WHERE DFCI_MRN = ?
      AND report_id = ?
      AND progression_date = ?
      AND progression_source = ?
    """, (
        new_row['DFCI_MRN'],
        new_row['report_id'],
        new_row['progression_date'],
        new_row['progression_source']
    ))

    existing = cursor.fetchone()

    if existing:

        cursor.execute("""
       UPDATE annotations
       SET agent                  = ?,
           evidence               = ?,
           determined_by          = ?,
           user                   = ?,
           modification_timestamp = ?
       WHERE id = ?
       """, (
           new_row['agent'],
           new_row['evidence'],
           new_row['determined_by'],
           new_row.get('user'),
           new_row['modification_timestamp'],
           existing['id']
       ))

    else:

        cursor.execute("""
       INSERT INTO annotations (
            DFCI_MRN,
            progression_date,
            progression_source,
            agent,
            evidence,
            report_id,
            determined_by,
            user,
            modification_timestamp)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
       """, (
           new_row['DFCI_MRN'],
           new_row['progression_date'],
           new_row['progression_source'],
           new_row['agent'],
           new_row['evidence'],
           new_row['report_id'],
           new_row['determined_by'],
           new_row.get('user'),
           new_row['modification_timestamp']
        ))

    save_annotations()


def save_agent_assignment(
    rid,
    agent_value,
    patient_id,
    progression_date=None,
    evidence=None,
    user=None
):
    user = CURRENT_USER

    if not require_user():
        return

    if not agent_value:

        ui.notify(
            'No agent selected',
            color='red'
        )

        return

    now = datetime.now().isoformat(
        timespec='seconds'
    )

    new_row = {
        'DFCI_MRN': str(patient_id).strip(),
        'progression_date': progression_date,
        'progression_source': 'LLM',
        'agent': agent_value,
        'evidence': evidence,
        'report_id': rid,
        'determined_by': None,
        'user': user,
        'modification_timestamp': now
    }

    upsert_annotation(new_row)

    ui.notify(
        f'Saved agent assignment: {agent_value}',
        color='green'
    )


def save_clinician_progression_event(
    patient_id,
    progression_date,
    agent,
    evidence,
    report_id,
    determined_by,
    user=None
):
    user = CURRENT_USER

    if not require_user():
        return

    now = datetime.now().isoformat(
        timespec='seconds'
    )

    new_row = {
        'DFCI_MRN': str(patient_id).strip(),
        'progression_date': progression_date,
        'progression_source': 'manual',
        'agent': agent,
        'evidence': evidence,
        'report_id': report_id,
        'determined_by': determined_by,
        'user': user,
        'modification_timestamp': now
    }

    upsert_annotation(new_row)

    ui.notify(
        'Clinician progression event saved',
        color='green'
    )


def get_saved_agent(
    patient_id,
    report_id,
    progression_date
):

    cursor.execute("""
    SELECT agent
    FROM annotations
    WHERE DFCI_MRN = ?
      AND report_id = ?
      AND progression_date = ?
    LIMIT 1
    """, (
        patient_id,
        report_id,
        progression_date
    ))

    row = cursor.fetchone()

    if not row:
        return None

    return row['agent']


def delete_agent_assignment(
    rid,
    patient_id,
    progression_date
):

    cursor.execute("""
    DELETE FROM annotations
    WHERE DFCI_MRN = ?
      AND report_id = ?
      AND progression_date = ?
    """, (
        patient_id,
        rid,
        progression_date
    ))

    deleted = cursor.rowcount

    save_annotations()

    if deleted:

        ui.notify(
            'Agent assignment removed',
            color='orange'
        )

        return True

    ui.notify(
        'No assignment found to remove',
        color='red'
    )

    return False


def get_clinician_events(patient_id):

    patient_id = normalize_patient_id(patient_id)

    return pd.read_sql_query("""
    SELECT *
    FROM annotations
    WHERE DFCI_MRN = ?
      AND progression_source = 'manual'
    ORDER BY progression_date
    """, conn, params=(patient_id,))


def safe_json_loads(x):

    if pd.isna(x):
        return {}

    try:
        return json.loads(x)

    except:
        return {}


def compress_blank_lines(text):

    return re.sub(r'\n\s*\n\s*\n+', '\n\n', text)


def extract_field(pattern, text, default='unknown'):

    match = re.search(pattern, text, re.MULTILINE)

    return match.group(1).strip() if match else default


def normalize_any_date(x):

    if pd.isna(x):
        return None

    x = str(x).strip()

    if not x or x.lower() == 'nan':
        return None

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y%m%d",
        "%m/%d/%y",
        "%m/%d/%Y"
    ):
        try:
            return datetime.strptime(
                x,
                fmt
            ).date().isoformat()

        except:
            continue

    return x


def sort_date_key(x):

    d = normalize_any_date(x)

    if not d or pd.isna(d):
        return "9999-12-31"

    return str(d)


def normalize_date(date_str):

    if not date_str:
        return None

    date_str = date_str.strip()

    try:

        return datetime.strptime(
            date_str,
            "%Y-%m-%d"
        ).date().isoformat()

    except:
        pass

    for fmt in (
        "%Y%m%d",
        "%Y/%m/%d",
        "%Y %m %d"
    ):
        try:

            return datetime.strptime(
                date_str,
                fmt
            ).date().isoformat()

        except:
            continue

    return None

# =============================================================================
# NOTE PARSING
# =============================================================================

def parse_notes(notes_text):

    if not isinstance(notes_text, str):
        return []

    notes_text = compress_blank_lines(notes_text)

    raw_notes = re.split(r'={20,}', notes_text)

    notes = []

    for note in raw_notes:

        note = note.strip()

        if not note:
            continue

        notes.append({
            'note_number': extract_field(
                r'Note Number:\s*(.+)',
                note
            ),
            'report_id': extract_field(
                r'Note Report ID:\s*(.+)',
                note
            ),
            'note_date': extract_field(
                r'Note Date:\s*(.+)',
                note
            ),
            'dept': extract_field(
                r'Note Dept:\s*(.+)',
                note
            ),
            'author': extract_field(
                r'Note Author:\s*(.+)',
                note
            ),
            'raw_text': note,
        })

    return notes

# =============================================================================
# HIGHLIGHTING
# =============================================================================

def get_anchor_text(evidence_text):

    if not evidence_text:
        return None

    anchor = re.split(
        r'\.\.\.|…',
        evidence_text
    )[0].strip()

    return re.sub(r'\s+', ' ', anchor)


def highlight_evidence(note_text, evidence_text):

    anchor = get_anchor_text(evidence_text)

    if not anchor:
        return (html.escape(note_text), False)

    escaped = re.escape(anchor)
    escaped = re.sub(r'\\ ', r'\\s+', escaped)

    try:

        match = re.search(
            escaped,
            note_text,
            re.IGNORECASE | re.DOTALL
        )

        if not match:
            return (html.escape(note_text), False)

        start, end = match.span()

        return (
            html.escape(note_text[:start]) +
            f'<span class="evidence-highlight" id="evidence-highlight">'
            f'{html.escape(note_text[start:end])}</span>' +
            html.escape(note_text[end:]),
            True
        )

    except:
        return (html.escape(note_text), False)

# =============================================================================
# HTML RENDER
# =============================================================================

def build_notes_html(
    notes,
    highlighted_report=None,
    evidence_text=None
):

    html_parts = []

    for note in notes:

        report_id = note['report_id']
        raw_text = note['raw_text']

        if (
            highlighted_report and
            report_id == highlighted_report and
            evidence_text
        ):
            rendered_text, _ = highlight_evidence(
                raw_text,
                evidence_text
            )

        else:
            rendered_text = html.escape(raw_text)

        html_parts.append(f"""
        <div id="report_{report_id}" class="note-card">

            <div class="note-meta">
                <span><b>#{note['note_number']}</b></span>
                <span>{note['note_date']}</span>
                <span>{note['dept']}</span>
                <span>{note['author']}</span>
                <span>RID: {report_id}</span>
            </div>

            <pre>{rendered_text}</pre>

        </div>
        """)

    return '\n'.join(html_parts)

# =============================================================================
# STATE
# =============================================================================

current_patient_index = 0
agent_output = None
event_agent_map = {}

# =============================================================================
# CSS
# =============================================================================

ui.add_head_html("""
<style>
body { font-family: Arial; }

.left-pane {
    height: 94vh;
    overflow-y: auto;
    padding-right: 8px;
}

.right-pane {
    height: 94vh;
    overflow-y: auto;
    border-left: 1px solid #ddd;
    padding-left: 8px;
}

.note-card {
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 4px 6px;
    margin-bottom: 6px;
    background: #fafafa;
}

.note-meta {
    display: flex;
    gap: 10px;
    font-size: 10px;
    color: #666;
    margin-bottom: 3px;
    border-bottom: 1px solid #eee;
    padding-bottom: 2px;
}

pre {
    white-space: pre-wrap;
    font-size: 11px;
    line-height: 1.12;
    margin: 0;
}

.evidence-highlight {
    background-color: #ffe066;
    padding: 2px 3px;
    border-radius: 2px;
    font-weight: bold;
}


.agent-box, .annotation-box {
    border: 1px solid #ddd;
    padding: 8px;
    border-radius: 5px;
    margin-bottom: 10px;
}

/* Patient list dialog table */
.bottom-nav {
    position: fixed;
    bottom: 15px;
    left: 0;
    right: 0;
    width: 33.333%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    z-index: 9999;
    background: rgba(255,255,255,0.95);
    padding: 5px 10px;
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12);
}

.patient-list-row:hover {
    background: #f0f4ff;
    cursor: pointer;
}
</style>
""")

# =============================================================================
# LAYOUT
# =============================================================================

with ui.row().classes('w-full no-wrap') as main_container:
    left_panel = ui.column().classes('left-pane w-1/3')
    right_panel = ui.column().classes('right-pane w-2/3')

lock_overlay = ui.column().classes(
    'fixed inset-0 flex items-center justify-center z-[9999]'
).style('background-color: white; pointer-events: all;')

with lock_overlay:

    ui.label(f'WATNEY v{WATNEY_VERSION}').classes('text-4xl font-bold')
    ui.separator().classes('mb-4')

    # -- Step 1: username --
    step1 = ui.column().classes('items-center gap-2')
    # -- Step 2: CSV path (shown only if no saved CSV) --
    step2 = ui.column().classes('items-center gap-2')
    step2.set_visibility(False)

    with step1:
        ui.label('Enter Name to Begin').classes('text-xl font-bold')
        user_select = ui.input(
            label='Username',
            placeholder='e.g. John Doe'
        ).classes('w-64')

        csv_note = ui.label('').classes('text-xs text-gray-500')
        if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
            csv_note.set_text(f'CSV: {Path(EXTRACTION_CSV_PATH).name}')

        def after_username():
            global CURRENT_USER, df, EXTRACTION_CSV_PATH

            if not user_select.value.strip():
                ui.notify('Username required', color='red')
                return

            CURRENT_USER = user_select.value.strip()

            # If we already have a valid CSV, go straight in
            if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
                _finish_login()
            else:
                # Need to ask for CSV path
                step1.set_visibility(False)
                step2.set_visibility(True)

        ui.button('Continue', on_click=after_username).classes('w-64')

    with step2:
        ui.label('Set Input CSV Path').classes('text-xl font-bold')
        ui.label(
            'No CSV configured. Enter the full path to your extraction CSV.'
        ).classes('text-xs text-gray-500 text-center w-72')

        csv_input = ui.input(
            label='CSV path',
            placeholder='/path/to/extraction.csv'
        ).classes('w-96')

        csv_error = ui.label('').classes('text-xs text-red-500')

        def set_csv_and_login():
            global df, EXTRACTION_CSV_PATH

            p = csv_input.value.strip()
            if not p:
                csv_error.set_text('Please enter a path.')
                return

            path = Path(p)
            if not path.exists():
                csv_error.set_text(f'File not found: {path}')
                return

            try:
                df = load_dataframe(str(path))
            except Exception as e:
                csv_error.set_text(f'Could not load CSV: {e}')
                return

            EXTRACTION_CSV_PATH = str(path)
            cfg = load_config()
            cfg['csv_path'] = str(path)
            save_config(cfg)

            _finish_login()

        ui.button('Load & Enter', on_click=set_csv_and_login).classes('w-96')

    def _finish_login():
        global UI_LOCKED

        UI_LOCKED = False
        lock_overlay.set_visibility(False)

        if user_label is not None:
            user_label.set_text(f'User: {CURRENT_USER}')

        if nav_bar is not None:
            nav_bar.style('display:flex')

        render_patient(current_patient_index)
        ui.notify(f'Welcome {CURRENT_USER}', color='green')

def require_user():
    if not CURRENT_USER:
        ui.notify('Set username first', color='red')
        return False
    return True

# =============================================================================
# SCROLL
# =============================================================================

def scroll_to_note(report_id, evidence_text=''):

    global current_patient_index

    row = df.iloc[current_patient_index]

    notes = parse_notes(row[NOTES_COL])

    notes_html = build_notes_html(
        notes,
        highlighted_report=report_id,
        evidence_text=evidence_text
    )

    right_panel.clear()

    with right_panel:
        ui.html(notes_html).classes('w-full')

    ui.timer(
        0.05,
        lambda: ui.run_javascript(f"""
            const h = document.getElementById("evidence-highlight");
            if (h) {{
                h.scrollIntoView({{behavior:"auto", block:"center"}});
            }} else {{
                const f = document.getElementById("report_{report_id}");
                if (f) f.scrollIntoView({{behavior:"auto", block:"start"}});
            }}
        """),
        once=True
    )

# =============================================================================
# AGENT DISPLAY
# =============================================================================

def update_agent_display(agent_name, extraction):

    global agent_output

    agent_output.clear()

    systemic = extraction.get(
        'systemic_therapy',
        {}
    ) or {}

    agents = systemic.get(
        'agents',
        []
    )

    selected = next(
        (
            a for a in agents
            if a.get('drug_name') == agent_name
        ),
        None
    )

    if not selected:
        return

    intervals = sorted(
        selected.get('intervals', []),
        key=lambda x: sort_date_key(
            x.get('start_date')
        )
    )

    with agent_output:

        for interval in intervals:

            ui.label(
                f"{interval.get('start_date','unknown')} → "
                f"{interval.get('end_date','unknown')}"
            ).classes('left-sub-text')

# =============================================================================
# PROGRESSION CARD
# =============================================================================

def progression_card(event, patient_id, agent_names):

    progression_date = event.get(
        'progression_date',
        'unknown'
    )

    confidence = event.get(
        'confidence_level',
        'unknown'
    )

    rationale = event.get(
        'progression_date_rationale',
        {}
    ) or {}

    report_id = rationale.get(
        'report_id',
        'unknown'
    )

    note_date = rationale.get(
        'note_date',
        'unknown'
    )

    author = rationale.get(
        'author',
        'unknown'
    )

    evidence = rationale.get(
        'text',
        ''
    )

    with ui.card().classes('w-full compact-card'):

        with ui.row().classes(
            'w-full justify-between items-center'
        ):

            ui.label(
                progression_date
            ).classes('left-main-text font-bold')

            ui.label(
                f'Confidence: {confidence}'
            ).classes('left-sub-text')

        ui.label(
            f'Progression Date: {progression_date}'
        ).classes('left-sub-text')

        ui.label(
            f'Note Date: {note_date}'
        ).classes('left-sub-text')

        ui.label(
            f'Author: {author}'
        ).classes('left-sub-text')

        ui.label(
            f'Report ID: {report_id}'
        ).classes('left-sub-text')

        if evidence:

            ui.markdown(
                f'> {evidence}'
            ).classes('left-evidence')

        ui.button(
            'Source',
            on_click=lambda rid=report_id, ev=evidence:
                scroll_to_note(rid, ev)
        ).props('dense flat')

        current_value = get_saved_agent(
            patient_id,
            report_id,
            progression_date
        )

        selected_agent = ui.select(
            agent_names,
            value=(
                current_value
                if current_value in agent_names
                else None
            ),
            label='Assign agent'
        ).classes('w-full')

        ui.button(
            'Save Agent Assignment',
            on_click=lambda rid=report_id,
                            sa=selected_agent,
                            pid=patient_id,
                            llm=progression_date,
                            ev=evidence:
            save_agent_assignment(
                rid,
                sa.value,
                pid,
                progression_date=llm,
                evidence=ev
            )
        ).props('dense')

        ui.button(
            'Remove Agent',
            on_click=lambda rid=report_id,
                            pid=patient_id,
                            llm=progression_date,
                            sel=selected_agent:
            (
                delete_agent_assignment(
                    rid,
                    pid,
                    llm
                ),
                setattr(sel, 'value', None),
                render_patient(current_patient_index)
            )
        ).props('dense outline')

# =============================================================================
# PATIENT LIST DIALOG  (NEW)
# =============================================================================

def show_patient_list():
    """Show all patients with Yes/No assigned-progression indicator.
    Clicking a row navigates to that patient."""

    refresh_annotations_df()

    # Build a set of MRNs that have at least one annotation
    annotated_mrns = set(
        annotations_df['DFCI_MRN'].apply(safe_str).tolist()
    )

    with ui.dialog() as dlg, ui.card().classes(
        'w-[520px] max-h-[80vh] overflow-y-auto'
    ):
        ui.label('Patient List').classes('text-lg font-bold mb-1')
        ui.label(
            f'{len(df)} patients total · '
            f'{len(annotated_mrns)} with annotations'
        ).classes('text-xs text-gray-500 mb-2')

        # Header row
        with ui.row().classes(
            'w-full text-xs font-bold border-b pb-1 mb-1'
        ):
            ui.label('#').style('width: 40px')
            ui.label('MRN').style('width: 160px')
            ui.label('Assigned Progression').style('width: 160px')

        for i, row in df.iterrows():
            pid = normalize_patient_id(row[PATIENT_ID_COL])
            has = safe_str(pid) in annotated_mrns
            label_text = 'Yes' if has else 'No'
            label_color = 'color: #16a34a; font-weight:600;' if has else 'color: #dc2626;'

            with ui.row().classes(
                'w-full text-xs items-center patient-list-row rounded px-1'
            ):
                ui.label(str(i + 1)).style('width: 40px; color:#999;')
                ui.label(pid).style('width: 160px;')
                ui.label(label_text).style(f'width: 160px; {label_color}')

                # Navigate button — closes dialog and jumps to patient
                def go(idx=i, d=dlg):
                    global current_patient_index
                    current_patient_index = idx
                    d.close()
                    render_patient(current_patient_index)

                ui.button(
                    'Go',
                    on_click=go
                ).props('dense flat size=xs')

        ui.separator().classes('my-2')
        ui.button('Close', on_click=dlg.close).props('dense')

    dlg.open()


# =============================================================================
# EXPORT  (NEW)
# =============================================================================

def export_csv():
    """Download the full annotations table as a timestamped CSV."""

    df_out = load_annotations_df()

    if df_out.empty:
        ui.notify('No annotations to export', color='orange')
        return

    import base64

    csv_bytes = df_out.to_csv(index=False).encode('utf-8')
    b64 = base64.b64encode(csv_bytes).decode()
    filename = (
        f"watney_annotations_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )

    ui.run_javascript(f"""
        const a = document.createElement('a');
        a.href = 'data:text/csv;base64,{b64}';
        a.download = '{filename}';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    """)

    ui.notify(
        f'Exported {len(df_out)} rows → {filename}',
        color='green'
    )


# =============================================================================
# SETTINGS DIALOG  (NEW)
# =============================================================================

def show_settings():
    """Settings / info dialog with path editing."""

    global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR, conn, cursor, df

    with ui.dialog() as dlg, ui.card().classes('w-[560px] max-h-[90vh] overflow-y-auto'):

        ui.label('Settings').classes('text-lg font-bold mb-2')

        # --- Session Info ---
        ui.label('Session Info').classes('text-sm font-semibold mt-1')
        ui.label(f'WATNEY version: {WATNEY_VERSION}').classes('text-xs text-gray-600')
        ui.label(f'Current user: {CURRENT_USER or "not set"}').classes('text-xs text-gray-600')

        ui.separator()

        # --- File Paths ---
        ui.label('File Paths').classes('text-sm font-semibold mt-1')

        csv_path = Path(EXTRACTION_CSV_PATH).resolve()
        db_path  = Path(SQLITE_PATH).resolve()

        ui.label('Input CSV').classes('text-xs font-medium text-gray-700 mt-1')
        ui.label(f'Name: {csv_path.name}').classes('text-xs text-gray-600 ml-2')
        ui.label(f'Path: {csv_path}').classes('text-xs text-gray-500 ml-2 break-all')

        ui.label('Database').classes('text-xs font-medium text-gray-700 mt-2')
        ui.label(f'Name: {db_path.name}').classes('text-xs text-gray-600 ml-2')
        ui.label(f'Path: {db_path}').classes('text-xs text-gray-500 ml-2 break-all')

        ui.separator()

        # --- Change Paths ---
        ui.label('Change Paths').classes('text-sm font-semibold mt-1')
        ui.label(
            'Changes take effect immediately and reload data. '
            'Unsaved annotations are written to the database before switching.'
        ).classes('text-xs text-orange-600 mb-2')

        new_csv_input = ui.input(
            label='New Input CSV path',
            placeholder=str(csv_path)
        ).classes('w-full')

        new_db_input = ui.input(
            label='New Database path',
            placeholder=str(db_path)
        ).classes('w-full')

        path_status = ui.label('').classes('text-xs mt-1')

        def apply_paths():
            global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR
            global conn, cursor, df, annotations_df

            new_csv = new_csv_input.value.strip()
            new_db  = new_db_input.value.strip()

            changed = False

            if new_csv:
                p = Path(new_csv)
                if not p.exists():
                    path_status.set_text(f'CSV not found: {p}')
                    path_status.classes('text-red-600', remove='text-green-600')
                    return
                try:
                    df = load_dataframe(str(p))
                    EXTRACTION_CSV_PATH = str(p)
                    cfg = load_config()
                    cfg['csv_path'] = str(p)
                    save_config(cfg)
                    changed = True
                except Exception as e:
                    path_status.set_text(f'CSV load error: {e}')
                    path_status.classes('text-red-600', remove='text-green-600')
                    return

            if new_db:
                p = Path(new_db)
                try:
                    conn.commit()  # flush current
                    conn.close()
                    new_conn = sqlite3.connect(str(p), check_same_thread=False)
                    new_conn.row_factory = sqlite3.Row
                    new_cursor = new_conn.cursor()
                    new_cursor.execute("""
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
                        modification_timestamp TEXT
                    )
                    """)
                    new_cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_progression_event
                    ON annotations (DFCI_MRN, progression_date, progression_source, report_id)
                    """)
                    new_conn.commit()
                    conn   = new_conn
                    cursor = new_cursor
                    SQLITE_PATH = str(p)
                    ANNOTATION_OUTPUT_DIR = p.parent
                    changed = True
                except Exception as e:
                    path_status.set_text(f'DB error: {e}')
                    path_status.classes('text-red-600', remove='text-green-600')
                    return

            if changed:
                refresh_annotations_df()
                render_patient(current_patient_index)
                path_status.set_text('Paths updated and data reloaded.')
                path_status.classes('text-green-600', remove='text-red-600')
                new_csv_input.value = ''
                new_db_input.value  = ''
            else:
                path_status.set_text('No new paths entered.')
                path_status.classes('text-gray-500', remove='text-red-600 text-green-600')

        ui.button('Apply', on_click=apply_paths).props('dense')

        ui.separator()

        # --- Database Stats ---
        ui.label('Database Stats').classes('text-sm font-semibold mt-1')
        stats_df = load_annotations_df()
        ui.label(f'Total annotations: {len(stats_df)}').classes('text-xs text-gray-600')
        ui.label(
            f'Unique patients annotated: '
            f'{stats_df["DFCI_MRN"].nunique() if not stats_df.empty else 0}'
        ).classes('text-xs text-gray-600')
        ui.label(
            f'LLM-sourced: '
            f'{len(stats_df[stats_df["progression_source"] == "LLM"]) if not stats_df.empty else 0}'
        ).classes('text-xs text-gray-600')
        ui.label(
            f'Clinician-sourced: '
            f'{len(stats_df[stats_df["progression_source"] == "manual"]) if not stats_df.empty else 0}'
        ).classes('text-xs text-gray-600')

        ui.separator().classes('my-2')

        ui.button('Close', on_click=dlg.close).props('dense')

    dlg.open()


# =============================================================================
# RENDER
# =============================================================================

def render_patient(index):

    global agent_output
    global progression_sort_order
    global user_label

    refresh_annotations_df()

    left_panel.clear()
    right_panel.clear()

    row = df.iloc[index]

    patient_id = normalize_patient_id(
        row[PATIENT_ID_COL]
    )

    extraction = safe_json_loads(
        row[GENERATION_COL]
    )

    notes = parse_notes(
        row[NOTES_COL]
    )

    notes_html = build_notes_html(notes)

    systemic = extraction.get(
        'systemic_therapy',
        {}
    ) or {}

    agents = systemic.get(
        'agents',
        []
    )

    drug_names = sorted([
        a.get('drug_name')
        for a in agents
        if a.get('drug_name')
    ])

    events = extraction.get(
        'progression',
        {}
    ).get(
        'progression_events',
        []
    )

    with (left_panel):

        with ui.column().classes('gap-0'):
            ui.label(f'WATNEY v{WATNEY_VERSION}').classes('text-2xl font-bold')

            ui.label(
                'Developed by Justin Vinh @ DFCI'
            ).classes('text-[11px] text-gray-500 leading-tight')

            user_label = ui.label(
                f'User: {CURRENT_USER or "not set"}'
            ).classes('text-xs text-gray-600')

        ui.separator().classes('mb-4')

        ui.label(
            f'Patient {patient_id} | '
            f'Row {index + 1}/{len(df)}'
        ).classes(
            'text-xl font-bold'
        )

        ui.label(
            'Progression Summary'
        ).classes('text-sm font-bold')

        summary_rows = []

        patient_annotations = annotations_df[
            annotations_df['DFCI_MRN'].apply(safe_str)
            == safe_str(patient_id)
        ]

        for _, ann in patient_annotations.iterrows():

            progression_date = normalize_any_date(
                ann.get('progression_date')
            )

            if not progression_date:
                continue

            summary_rows.append({
                'date': progression_date,
                'sort_date': sort_date_key(progression_date),
                'agent': ann.get('agent', ''),
                'source': ann.get('progression_source', ''),
                'user': ann.get('user', '')
            })

        summary_rows = sorted(
            summary_rows,
            key=lambda x: x.get(
                'sort_date',
                '9999-12-31'
            )
        )

        if not summary_rows:

            ui.label(
                'NO PROGRESSION DATES ASSIGNED TO AGENTS'
            ).classes(
                'text-xs text-red-500 font-bold'
            )

        else:

            with ui.column().classes('w-full gap-1'):

                with ui.row().classes(
                    'w-full text-xs font-bold border-b pb-1'
                ):
                    ui.label(
                        'Progression Date'
                    ).style('width: 110px')

                    ui.label(
                        'Assigned Agent'
                    ).style('width: 140px')

                    ui.label(
                        'Source'
                    ).style('width: 90px')

                    ui.label(
                        'User'
                    ).style('width: 120px')

                for row_data in summary_rows:
                    with ui.row().classes('w-full text-xs'):
                        ui.label(str(row_data['date'])).style('width: 110px')
                        ui.label(str(row_data['agent'])).style('width: 140px')
                        ui.label(str(row_data['source'])).style('width: 90px')
                        ui.label(str(row_data['user'])).style('width: 120px')

        with ui.column().classes('agent-box w-full'):

            ui.label(
                'Agent Intervals'
            ).classes('text-sm font-bold')

            agent_output = ui.column()

            if drug_names:

                dropdown = ui.select(
                    drug_names,
                    value=drug_names[0]
                ).classes('w-full')

                dropdown.on(
                    'update:model-value',
                    lambda _:
                    update_agent_display(
                        dropdown.value,
                        extraction
                    )
                )

                update_agent_display(
                    drug_names[0],
                    extraction
                )

        ui.separator()

        sort_select = ui.select(
            ['Ascending', 'Descending'],
            value=progression_sort_order,
            label='Progression order'
        ).classes('w-full')

        ui.separator()

        ui.label(
            'LLM Progression Events'
        ).classes('text-sm font-bold')

        ordered = sorted(
            events,
            key=lambda x: sort_date_key(
                x.get('progression_date')
            ),
            reverse=(
                progression_sort_order == 'Descending'
            )
        )

        if not ordered:

            ui.label(
                'No LLM progression events'
            ).classes(
                'text-xs text-gray-500'
            )

        else:

            for event in ordered:

                progression_card(
                    event,
                    patient_id,
                    drug_names
                )

        ui.separator()

        ui.label(
            'Clinician Added Progression Events'
        ).classes('text-sm font-bold')

        clin_events = get_clinician_events(
            patient_id
        )

        if clin_events.empty:

            ui.label(
                'No Clinician added progression events'
            ).classes(
                'text-xs text-gray-500'
            )

        else:

            for _, row in clin_events.iterrows():

                with ui.card().classes('w-full'):

                    date = row.get(
                        'progression_date',
                        ''
                    )

                    ui.label(
                        f"Date: {date}"
                    ).classes('text-xs')

                    ui.label(
                        f"Agent: {row.get('agent', '')}"
                    ).classes('text-xs')

                    ui.label(
                        f"Evidence: {row.get('evidence', '')}"
                    ).classes('text-xs')

                    ui.label(
                        f"Determined by: "
                        f"{row.get('determined_by', '')}"
                    ).classes('text-xs')

                    def delete_clin_event(
                        rid=row['report_id']
                    ):

                        cursor.execute("""
                        DELETE FROM annotations
                        WHERE report_id = ?
                        """, (rid,))

                        save_annotations()

                        ui.notify(
                            'Clinician event removed',
                            color='orange'
                        )

                        render_patient(
                            current_patient_index
                        )

                    ui.button(
                        'Remove',
                        on_click=delete_clin_event
                    ).props('dense outline')

                    ui.label(
                        "CLINICIAN ENTRY"
                    ).classes(
                        'text-xs text-red-500 font-bold'
                    )

        def on_sort_change(e):

            global progression_sort_order

            progression_sort_order = sort_select.value

            render_patient(
                current_patient_index
            )

        sort_select.on(
            'update:model-value',
            on_sort_change
        )

        ui.separator()

        ui.label(
            'Add Clinician Progression Event'
        ).classes('text-sm font-bold')

        # Custom agents stored per-session (persisted in config)
        _cfg_now = load_config()
        custom_agents = _cfg_now.get('custom_agents', [])
        all_agent_names = drug_names + [
            a for a in custom_agents if a not in drug_names
        ]

        clinician_agent = ui.select(
            all_agent_names if all_agent_names else [],
            label='Agent'
        ).classes('w-full')

        with ui.row().classes('w-full items-center gap-1'):
            custom_agent_input = ui.input(
                label='Add custom agent',
                placeholder='e.g. Pembrolizumab'
            ).classes('flex-grow')

            def add_custom_agent():
                name = custom_agent_input.value.strip()
                if not name:
                    ui.notify('Enter an agent name', color='red')
                    return
                cfg = load_config()
                existing = cfg.get('custom_agents', [])
                if name in existing or name in drug_names:
                    ui.notify(f'{name} already in list', color='orange')
                else:
                    existing.append(name)
                    cfg['custom_agents'] = existing
                    save_config(cfg)
                    ui.notify(f'Added {name}', color='green')
                # Update dropdown options and select the new agent
                updated = drug_names + [
                    a for a in cfg.get('custom_agents', [])
                    if a not in drug_names
                ]
                clinician_agent.options = updated
                clinician_agent.value = name
                clinician_agent.update()
                custom_agent_input.value = ''

            ui.button('Add', on_click=add_custom_agent).props('dense outline')

        clinician_date = ui.input(
            label='Progression Date (YYYY-MM-DD)',
            placeholder='YYYY-MM-DD'
        ).classes('w-full')

        def validate_and_format_clinician_date():

            value = clinician_date.value

            if not value:
                return

            digits = re.sub(r'\D', '', value)

            # Must be exactly 8 digits for a valid date
            if len(digits) != 8:
                ui.notify(
                    'Invalid date: enter YYYYMMDD (8 digits)',
                    color='red'
                )
                return

            try:
                formatted = (
                    f"{digits[:4]}-"
                    f"{digits[4:6]}-"
                    f"{digits[6:8]}"
                )

                clinician_date.set_value(formatted)

            except Exception:
                ui.notify(
                    'Invalid date format',
                    color='red'
                )

        clinician_date.on(
            'blur',
            lambda e: validate_and_format_clinician_date()
        )

        clinician_evidence = ui.textarea(
            label='Evidence (optional)'
        ).classes('w-full')

        clinician_report_id = ui.input(
            label='Report ID of Evidence (optional)'
        ).classes('w-full')

        clinician_determined_by = ui.input(
            label='Determined by',
            placeholder='e.g., Dr. X, tumor board'
        ).classes('w-full')

        def save_clinician_event():

            row = df.iloc[current_patient_index]

            patient_id = normalize_patient_id(
                row[PATIENT_ID_COL]
            )

            raw_date = clinician_date.value or ''
            digits = re.sub(r'\D', '', raw_date)

            if digits and len(digits) != 8:
                ui.notify(
                    'Date must be YYYYMMDD (8 digits)',
                    color='red'
                )
                return

            cleaned_date = (
                f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
                if digits else None
            )

            if (
                clinician_date.value and
                not cleaned_date
            ):

                ui.notify(
                    'Invalid date format. '
                    'Try YYYYMMDD or YYYY-MM-DD',
                    color='red'
                )

                return

            report_id = (
                clinician_report_id.value.strip()
                if clinician_report_id.value
                else None
            )

            if not report_id:

                report_id = (
                    f"clinician::{patient_id}::"
                    f"{datetime.now().timestamp()}"
                )

            save_clinician_progression_event(
                patient_id=patient_id,
                progression_date=cleaned_date,
                agent=clinician_agent.value,
                evidence=(
                    clinician_evidence.value or ''
                ),
                report_id=report_id,
                determined_by=(
                    clinician_determined_by.value
                ),
            )

            clinician_date.value = ''
            clinician_evidence.value = ''
            clinician_determined_by.value = ''
            clinician_agent.value = None
            clinician_report_id.value = ''

            render_patient(current_patient_index)

        ui.button(
            'Save Clinician Progression Event',
            on_click=save_clinician_event
        ).props('dense')

        ui.separator().classes('my-4')

    with right_panel:

        ui.label(
            'All Relevant Notes'
        ).classes(
            'text-lg font-bold mb-2'
        )

        ui.html(notes_html).classes('w-full')

# =============================================================================
# NAVIGATION
# =============================================================================

progression_sort_order = 'Ascending'


def next_patient():

    global current_patient_index

    if current_patient_index < len(df) - 1:

        current_patient_index += 1

        render_patient(current_patient_index)


def prev_patient():

    global current_patient_index

    if current_patient_index > 0:

        current_patient_index -= 1

        render_patient(current_patient_index)

# =============================================================================
# LEFT NAV BUTTONS (Prev / Next)
# =============================================================================

def _prev_patient():
    if not require_user():
        return
    prev_patient()

def _next_patient():
    if not require_user():
        return
    next_patient()

def _show_patient_list():
    if not require_user():
        return
    show_patient_list()

def _export_csv():
    if not require_user():
        return
    export_csv()

def _show_settings():
    if not require_user():
        return
    show_settings()

# All nav buttons in one fixed bar spanning bottom of left panel
nav_bar = ui.element('div').classes('bottom-nav').style('display:none')
with nav_bar:
    with ui.element('div').style('display:flex; gap:6px;'):
        ui.button('Prev', on_click=_prev_patient).props('dense')
        ui.button('Next', on_click=_next_patient).props('dense')
    with ui.element('div').style('display:flex; gap:6px;'):
        ui.button('Patient List', on_click=_show_patient_list).props('dense outline')
        ui.button('Export', on_click=_export_csv).props('dense outline')
        ui.button('Settings', on_click=_show_settings).props('dense outline')

# =============================================================================
# START
# =============================================================================

# render_patient is called by _finish_login after login completes

# ui.run(
#     title='LLM Oncology Reviewer',
#     reload=False
# )
#


def main():
    ui.run(
        title='WATNEY — LLM Oncology Reviewer',
        reload=False,
    )

if __name__ == "__main__":
    main()