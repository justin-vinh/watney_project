import re
import json
import html
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime

import pandas as pd
from nicegui import ui

# Tool name: WATNEY

# =============================================================================
# CONFIG
# =============================================================================

WATNEY_VERSION = 4

NOTES_COL       = 'all_notes'
GENERATION_COL  = 'generation'
PATIENT_ID_COL  = 'DFCI_MRN'

ANNOTATION_OUTPUT_DIR = Path('watney_annotations')
ANNOTATION_OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

SQLITE_PATH = ANNOTATION_OUTPUT_DIR / 'progression_annotations_database.db'
CONFIG_PATH = ANNOTATION_OUTPUT_DIR / 'watney_config.json'

EXTRACTION_CSV_PATH = None   # loaded from config or set at login
CURRENT_USER        = None
UI_LOCKED           = True
user_label          = None
nav_bar             = None
df                  = None   # loaded after CSV path is confirmed
REPORT_FONT_SIZE    = 11     # px; overridden by saved config

# =============================================================================
# CONFIG FILE HELPERS
# =============================================================================

def load_config() -> dict:
    """Load persisted config from JSON. Returns {} on any failure."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)

_cfg = load_config()
if _cfg.get('csv_path'):
    EXTRACTION_CSV_PATH = _cfg['csv_path']
if _cfg.get('report_font_size'):
    REPORT_FONT_SIZE = int(_cfg['report_font_size'])

# =============================================================================
# DATA LOADING
# =============================================================================

def load_dataframe(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype={PATIENT_ID_COL: str})

if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
    df = load_dataframe(EXTRACTION_CSV_PATH)

# =============================================================================
# SQLITE  -  SCHEMA
# =============================================================================
# Design notes
# ─────────────
# • All date columns store ISO-8601 TEXT (YYYY-MM-DD).  This sorts correctly
#   with plain string comparison and is unambiguous to any reader / tool.
# • *_source columns hold exactly 'LLM' or 'manual' — never NULL when the
#   corresponding value is present.
# • progression_source records HOW THE EVENT was created:
#       'LLM'    → came from the model extraction; clinician confirmed / assigned agent
#       'manual' → clinician entered the whole event from scratch in the UI
# • agent_start_source / agent_end_source record how the INTERVAL DATES were chosen:
#       'LLM'    → copied verbatim from the model's agent-interval list
#       'manual' → overridden by the clinician
# • The unique index (mrn, progression_date, progression_source, report_id)
#   means one row per distinct event: the same LLM event can be annotated once
#   while a clinician can also add independent manual events for the same date.
# • WAL journal mode gives better concurrency and crash recovery than the default.
# =============================================================================

conn              = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
conn.row_factory  = sqlite3.Row
cursor            = conn.cursor()

cursor.execute("PRAGMA journal_mode=WAL")

cursor.execute("""
CREATE TABLE IF NOT EXISTS annotations (
    -- surrogate key
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- patient
    mrn                     TEXT    NOT NULL,

    -- progression event identity  (drives the unique index below)
    progression_date        TEXT,
    progression_source      TEXT    NOT NULL
                            CHECK (progression_source IN ('LLM', 'manual')),
    report_id               TEXT,

    -- clinician decision
    agent                   TEXT,
    evidence                TEXT,
    determined_by           TEXT,

    -- agent treatment interval
    agent_start             TEXT,
    agent_start_source      TEXT
                            CHECK (agent_start_source IN ('LLM', 'manual', NULL)),
    agent_end               TEXT,
    agent_end_source        TEXT
                            CHECK (agent_end_source IN ('LLM', 'manual', NULL)),

    -- audit columns
    annotated_by            TEXT,
    created_at              TEXT    NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    modified_at             TEXT    NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)
""")

cursor.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS uq_annotation_event
ON annotations (mrn, progression_date, progression_source, report_id)
""")

# ── Backwards-compat migration from older schema ──────────────────────────────
_existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(annotations)")}

for _col, _type in [
    ('mrn',                'TEXT'),
    ('agent_end',          'TEXT'),
    ('agent_end_source',   'TEXT'),
    ('agent_start_source', 'TEXT'),
    ('annotated_by',       'TEXT'),
    ('created_at',         'TEXT'),
    ('modified_at',        'TEXT'),
]:
    if _col not in _existing_cols:
        try:
            cursor.execute(f"ALTER TABLE annotations ADD COLUMN {_col} {_type}")
        except sqlite3.OperationalError:
            pass

# Populate new columns from old names where needed
if 'DFCI_MRN' in _existing_cols:
    cursor.execute("UPDATE annotations SET mrn = DFCI_MRN WHERE mrn IS NULL OR mrn = ''")
if 'user' in _existing_cols:
    cursor.execute('UPDATE annotations SET annotated_by = "user" WHERE annotated_by IS NULL')
if 'modification_timestamp' in _existing_cols:
    cursor.execute("""
        UPDATE annotations
        SET modified_at = modification_timestamp
        WHERE (modified_at IS NULL OR modified_at = '')
          AND modification_timestamp IS NOT NULL
    """)

conn.commit()

# =============================================================================
# SQLITE HELPERS
# =============================================================================

def load_annotations_df() -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM annotations", conn)

annotations_df = load_annotations_df()


def require_user() -> bool:
    if not CURRENT_USER:
        ui.notify('Enter username first', color='red')
        return False
    return True


def refresh_annotations_df() -> None:
    global annotations_df
    annotations_df = load_annotations_df()


def save_annotations() -> None:
    conn.commit()
    refresh_annotations_df()


def safe_str(x) -> str:
    if pd.isna(x):
        return ''
    try:
        if isinstance(x, bytes):
            return x.decode('utf-8', errors='ignore').strip()
        return str(x).strip()
    except Exception:
        return ''


def normalize_patient_id(x) -> str:
    if pd.isna(x):
        return ''
    s = str(x).strip()
    if s.endswith('.0'):
        s = s[:-2]
    return s


def upsert_annotation(row: dict) -> None:
    """Insert or update a single annotation row."""
    cursor.execute("""
        SELECT id FROM annotations
        WHERE mrn = ? AND report_id = ? AND progression_date = ? AND progression_source = ?
    """, (row['mrn'], row['report_id'], row['progression_date'], row['progression_source']))

    existing = cursor.fetchone()
    now = datetime.now().isoformat(timespec='seconds')

    if existing:
        cursor.execute("""
            UPDATE annotations
            SET agent              = ?,
                evidence           = ?,
                determined_by      = ?,
                annotated_by       = ?,
                modified_at        = ?,
                agent_start        = ?,
                agent_start_source = ?,
                agent_end          = ?,
                agent_end_source   = ?
            WHERE id = ?
        """, (
            row['agent'], row['evidence'], row['determined_by'],
            row.get('annotated_by'), now,
            row.get('agent_start'), row.get('agent_start_source'),
            row.get('agent_end'),   row.get('agent_end_source'),
            existing['id'],
        ))
    else:
        cursor.execute("""
            INSERT INTO annotations (
                mrn, progression_date, progression_source,
                agent, evidence, report_id, determined_by,
                annotated_by, created_at, modified_at,
                agent_start, agent_start_source,
                agent_end,   agent_end_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row['mrn'], row['progression_date'], row['progression_source'],
            row['agent'], row['evidence'], row['report_id'], row['determined_by'],
            row.get('annotated_by'), now, now,
            row.get('agent_start'), row.get('agent_start_source'),
            row.get('agent_end'),   row.get('agent_end_source'),
        ))

    save_annotations()


def _build_row(
    mrn, progression_date, progression_source,
    agent, evidence, report_id, determined_by,
    agent_start=None, agent_start_source=None,
    agent_end=None,   agent_end_source=None,
) -> dict:
    return {
        'mrn':                str(mrn).strip(),
        'progression_date':   progression_date,
        'progression_source': progression_source,
        'agent':              agent,
        'evidence':           evidence,
        'report_id':          report_id,
        'determined_by':      determined_by,
        'annotated_by':       CURRENT_USER,
        'agent_start':        agent_start,
        'agent_start_source': agent_start_source,
        'agent_end':          agent_end,
        'agent_end_source':   agent_end_source,
    }


def save_agent_assignment(
    rid, agent_value, patient_id,
    progression_date=None, evidence=None,
    agent_start=None, agent_start_source=None,
    agent_end=None,   agent_end_source=None,
) -> None:
    if not require_user():
        return
    if not agent_value:
        ui.notify('No agent selected', color='red')
        return
    upsert_annotation(_build_row(
        mrn=patient_id, progression_date=progression_date,
        progression_source='LLM', agent=agent_value,
        evidence=evidence, report_id=rid, determined_by=None,
        agent_start=agent_start, agent_start_source=agent_start_source,
        agent_end=agent_end,     agent_end_source=agent_end_source,
    ))
    ui.notify(f'Saved: {agent_value}', color='green')


def save_clinician_progression_event(
    patient_id, progression_date, agent, evidence,
    report_id, determined_by,
    agent_start=None, agent_start_source=None,
    agent_end=None,   agent_end_source=None,
) -> None:
    if not require_user():
        return
    upsert_annotation(_build_row(
        mrn=patient_id, progression_date=progression_date,
        progression_source='manual', agent=agent,
        evidence=evidence, report_id=report_id, determined_by=determined_by,
        agent_start=agent_start, agent_start_source=agent_start_source,
        agent_end=agent_end,     agent_end_source=agent_end_source,
    ))
    ui.notify('Clinician progression event saved', color='green')


def get_saved_annotation(patient_id, report_id, progression_date) -> dict | None:
    cursor.execute("""
        SELECT agent, agent_start, agent_start_source, agent_end, agent_end_source
        FROM annotations
        WHERE mrn = ? AND report_id = ? AND progression_date = ?
        LIMIT 1
    """, (patient_id, report_id, progression_date))
    row = cursor.fetchone()
    return dict(row) if row else None


def delete_agent_assignment(rid, patient_id, progression_date) -> bool:
    cursor.execute("""
        DELETE FROM annotations WHERE mrn = ? AND report_id = ? AND progression_date = ?
    """, (patient_id, rid, progression_date))
    deleted = cursor.rowcount
    save_annotations()
    if deleted:
        ui.notify('Agent assignment removed', color='orange')
        return True
    ui.notify('No assignment found to remove', color='red')
    return False


def get_clinician_events(patient_id) -> pd.DataFrame:
    pid = normalize_patient_id(patient_id)
    return pd.read_sql_query("""
        SELECT * FROM annotations
        WHERE mrn = ? AND progression_source = 'manual'
        ORDER BY progression_date
    """, conn, params=(pid,))


def _reopen_db(db_path: str) -> None:
    global conn, cursor
    nc             = sqlite3.connect(db_path, check_same_thread=False)
    nc.row_factory = sqlite3.Row
    ncur           = nc.cursor()
    ncur.execute("PRAGMA journal_mode=WAL")
    ncur.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mrn TEXT NOT NULL,
            progression_date TEXT,
            progression_source TEXT NOT NULL CHECK (progression_source IN ('LLM','manual')),
            report_id TEXT,
            agent TEXT, evidence TEXT, determined_by TEXT,
            agent_start TEXT, agent_start_source TEXT,
            agent_end   TEXT, agent_end_source   TEXT,
            annotated_by TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            modified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    ncur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_annotation_event
        ON annotations (mrn, progression_date, progression_source, report_id)
    """)
    nc.commit()
    conn   = nc
    cursor = ncur

# =============================================================================
# UTILITY / DATE HELPERS
# =============================================================================

def safe_json_loads(x) -> dict:
    if pd.isna(x):
        return {}
    try:
        return json.loads(x)
    except Exception:
        return {}


def compress_blank_lines(text: str) -> str:
    return re.sub(r'\n\s*\n\s*\n+', '\n\n', text)


def extract_field(pattern, text, default='unknown') -> str:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else default


def normalize_any_date(x) -> str | None:
    if pd.isna(x):
        return None
    x = str(x).strip()
    if not x or x.lower() == 'nan':
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(x, fmt).date().isoformat()
        except Exception:
            continue
    return x


def sort_date_key(x) -> str:
    d = normalize_any_date(x)
    return str(d) if d and not pd.isna(d) else "9999-12-31"


def normalize_date(s: str) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y %m %d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    return None


def get_agent_llm_start(agent_name: str, extraction: dict) -> str | None:
    """Earliest start_date across all intervals for this agent."""
    agents = (extraction.get('systemic_therapy') or {}).get('agents', [])
    sel    = next((a for a in agents if a.get('drug_name') == agent_name), None)
    if not sel:
        return None
    dates = [normalize_any_date(iv.get('start_date')) for iv in sel.get('intervals', [])]
    valid = [d for d in dates if d]
    return min(valid) if valid else None


def get_agent_llm_end(agent_name: str, extraction: dict) -> str | None:
    """Latest end_date across all intervals for this agent."""
    agents = (extraction.get('systemic_therapy') or {}).get('agents', [])
    sel    = next((a for a in agents if a.get('drug_name') == agent_name), None)
    if not sel:
        return None
    dates = [normalize_any_date(iv.get('end_date')) for iv in sel.get('intervals', [])]
    valid = [d for d in dates if d]
    return max(valid) if valid else None

# =============================================================================
# NOTE PARSING
# =============================================================================

def parse_notes(notes_text: str) -> list[dict]:
    if not isinstance(notes_text, str):
        return []
    notes_text = compress_blank_lines(notes_text)
    notes = []
    for note in re.split(r'={20,}', notes_text):
        note = note.strip()
        if not note:
            continue
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
# HIGHLIGHTING  —  multi-segment aware
# =============================================================================
# Evidence strings often look like:
#   "Segment one text... skipped portion …final segment."
# We split on ellipsis tokens (... or …), build a fuzzy match for each
# fragment, and wrap EVERY matched run with a highlight span.
# The first matched span carries id="evidence-highlight" for auto-scroll.
#
# Whitespace inside each segment is made flexible (\\s+) so minor
# formatting differences between the LLM quote and the actual note don't
# prevent a match.
# =============================================================================

_ELLIPSIS_RE = re.compile(r'\s*(?:\.\.\.+|…)\s*')


def _segments_from_evidence(evidence_text: str) -> list[str]:
    """Return non-empty, whitespace-normalised fragments split on any ellipsis."""
    if not evidence_text:
        return []
    parts = _ELLIPSIS_RE.split(evidence_text.strip())
    return [re.sub(r'\s+', ' ', p).strip() for p in parts if p.strip()]


def highlight_evidence(note_text: str, evidence_text: str) -> tuple[str, bool]:
    """
    Return (highlighted_html, found_bool).

    Each non-ellipsis segment that can be located in the note receives its
    own yellow <span>.  Overlapping spans are merged.  The first span gets
    id="evidence-highlight" so the page can scroll to it.
    """
    segments = _segments_from_evidence(evidence_text)
    if not segments:
        return html.escape(note_text), False

    def seg_pattern(seg: str) -> str:
        # Allow any whitespace (including newlines) between words
        return re.sub(r'\\ ', r'\\s+', re.escape(seg))

    # Collect match spans for every segment
    spans: list[tuple[int, int]] = []
    for seg in segments:
        pat = seg_pattern(seg)
        try:
            m = re.search(pat, note_text, re.IGNORECASE | re.DOTALL)
            if m:
                spans.append(m.span())
        except re.error:
            continue

    if not spans:
        return html.escape(note_text), False

    # Sort then merge overlapping / adjacent spans
    spans.sort(key=lambda s: s[0])
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Build final HTML string
    parts: list[str] = []
    pos   = 0
    first = True
    for start, end in merged:
        parts.append(html.escape(note_text[pos:start]))
        aid = ' id="evidence-highlight"' if first else ''
        parts.append(
            f'<span class="evidence-highlight"{aid}>'
            f'{html.escape(note_text[start:end])}</span>'
        )
        first = False
        pos   = end
    parts.append(html.escape(note_text[pos:]))
    return ''.join(parts), True

# =============================================================================
# HTML RENDER
# =============================================================================

def build_notes_html(notes, highlighted_report=None, evidence_text=None) -> str:
    parts = []
    for note in notes:
        rid      = note['report_id']
        raw_text = note['raw_text']
        if highlighted_report and rid == highlighted_report and evidence_text:
            rendered, _ = highlight_evidence(raw_text, evidence_text)
        else:
            rendered = html.escape(raw_text)
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
# STATE
# =============================================================================

current_patient_index  = 0
agent_output           = None
progression_sort_order = 'Ascending'

# =============================================================================
# CSS
# =============================================================================

ui.add_head_html("""
<style>
/* ── Global ──────────────────────────────────────────────────────────────── */
body { font-family: Arial, sans-serif; margin: 0; }

/* ── Two-column layout ───────────────────────────────────────────────────── */
.left-pane {
    height: 100vh;
    overflow-y: auto;
    overflow-x: hidden;
    /* bottom padding keeps last form element above the fixed nav bar */
    padding: 10px 10px 68px 10px;
    box-sizing: border-box;
}

.right-pane {
    height: 100vh;
    overflow-y: auto;
    border-left: 1px solid #ddd;
    padding: 10px 12px;
    box-sizing: border-box;
}

/* Tighten NiceGUI column/row default gaps inside the left panel */
.left-pane .q-gutter-y-md > *,
.left-pane .nicegui-column > * { margin-bottom: 3px !important; }
.left-pane hr                  { margin: 3px 0 !important; }
.left-pane .q-card             { padding: 5px 7px !important; }

/* ── Note cards ──────────────────────────────────────────────────────────── */
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
    padding: 1px 2px;
    border-radius: 2px;
    font-weight: bold;
}

/* ── Left-panel section boxes ─────────────────────────────────────────────── */
.agent-box {
    border: 1px solid #e2e8f0;
    padding: 5px 7px;
    border-radius: 5px;
    background: #f8fafc;
}

/* ── Progression summary table ──────────────────────────────────────────── */
.summary-row {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    width: 100%;
    font-size: 10px;
    line-height: 1.35;
    gap: 0;
}
.summary-row > * {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-shrink: 0;
}

/* ── Progression cards (dense, modern) ──────────────────────────────────── */
.prog-card {
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 5px 7px;
    margin-bottom: 4px;
    background: #ffffff;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.prog-card-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 2px;
}
.prog-meta-line {
    font-size: 10px;
    color: #64748b;
    line-height: 1.4;
    margin: 0;
}
.prog-evidence {
    font-size: 10px;
    color: #374151;
    background: #f1f5f9;
    border-left: 3px solid #94a3b8;
    padding: 2px 5px;
    margin: 3px 0 2px 0;
    border-radius: 0 3px 3px 0;
    font-style: italic;
    white-space: pre-wrap;
    word-break: break-word;
}

/* ── Nav bar — locked to bottom of left 1/3 ─────────────────────────────── */
.bottom-nav {
    position: fixed;
    bottom: 0;
    left: 0;
    width: 33.333%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: nowrap;
    gap: 4px;
    z-index: 9999;
    background: rgba(255, 255, 255, 0.97);
    padding: 5px 10px;
    border-top: 1px solid #e5e7eb;
    box-sizing: border-box;
}

/* ── Patient list dialog ─────────────────────────────────────────────────── */
.patient-list-row:hover { background: #f0f4ff; cursor: pointer; }
</style>
""")


def update_report_font_size(size_px: int) -> None:
    ui.run_javascript(f"""
        (function() {{
            let el = document.getElementById('watney-font-style');
            if (!el) {{
                el = document.createElement('style');
                el.id = 'watney-font-style';
                document.head.appendChild(el);
            }}
            el.textContent = 'pre {{ font-size: {size_px}px !important; }}';
        }})();
    """)

# =============================================================================
# LAYOUT SKELETON
# =============================================================================

with ui.row().classes('w-full no-wrap'):
    left_panel  = ui.column().classes('left-pane w-1/3')
    right_panel = ui.column().classes('right-pane w-2/3')

# =============================================================================
# LOGIN OVERLAY
# =============================================================================

lock_overlay = ui.column().classes(
    'fixed inset-0 flex items-center justify-center z-[9999]'
).style('background-color: white; pointer-events: all;')

with lock_overlay:
    ui.label(f'WATNEY v{WATNEY_VERSION}').classes('text-4xl font-bold')
    ui.separator().classes('mb-4')

    step1 = ui.column().classes('items-center gap-2')
    step2 = ui.column().classes('items-center gap-2')
    step2.set_visibility(False)

    with step1:
        ui.label('Enter Name to Begin').classes('text-xl font-bold')
        user_select = ui.input(label='Username', placeholder='e.g. John Doe').classes('w-64')

        csv_note = ui.label('').classes('text-xs text-gray-500')
        if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
            csv_note.set_text(f'CSV: {Path(EXTRACTION_CSV_PATH).name}')

        def after_username():
            global CURRENT_USER
            if not user_select.value.strip():
                ui.notify('Username required', color='red')
                return
            CURRENT_USER = user_select.value.strip()
            if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
                _finish_login()
            else:
                step1.set_visibility(False)
                step2.set_visibility(True)

        ui.button('Continue', on_click=after_username).classes('w-64')

    with step2:
        ui.label('Set Input CSV Path').classes('text-xl font-bold')
        ui.label(
            'No CSV configured. Enter the full path to your extraction CSV.'
        ).classes('text-xs text-gray-500 text-center w-72')
        csv_input = ui.input(label='CSV path', placeholder='/path/to/extraction.csv').classes('w-96')
        csv_error = ui.label('').classes('text-xs text-red-500')

        def set_csv_and_login():
            global df, EXTRACTION_CSV_PATH
            p = csv_input.value.strip()
            if not p:
                csv_error.set_text('Please enter a path.'); return
            path = Path(p)
            if not path.exists():
                csv_error.set_text(f'File not found: {path}'); return
            try:
                df = load_dataframe(str(path))
            except Exception as e:
                csv_error.set_text(f'Could not load CSV: {e}'); return
            EXTRACTION_CSV_PATH = str(path)
            cfg = load_config(); cfg['csv_path'] = str(path); save_config(cfg)
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
    ui.timer(0.1, lambda: update_report_font_size(REPORT_FONT_SIZE), once=True)
    ui.notify(f'Welcome {CURRENT_USER}', color='green')


def require_user() -> bool:
    if not CURRENT_USER:
        ui.notify('Set username first', color='red')
        return False
    return True

# =============================================================================
# KEYBOARD NAVIGATION
# =============================================================================

ui.add_head_html("""
<script>
document.addEventListener('keydown', function(e) {
    const tag = document.activeElement ? document.activeElement.tagName : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (e.key === 'ArrowRight') { window._watneyNav && window._watneyNav('next'); }
    else if (e.key === 'ArrowLeft') { window._watneyNav && window._watneyNav('prev'); }
});
</script>
""")


def _register_keyboard_nav():
    ui.run_javascript("""
        window._watneyNav = function(dir) { emitEvent('watney_nav', {dir: dir}); };
    """)


async def _handle_nav_event(e):
    direction = e.args.get('dir') if hasattr(e, 'args') else None
    if direction == 'next':   _next_patient()
    elif direction == 'prev': _prev_patient()


ui.on('watney_nav', _handle_nav_event)

# =============================================================================
# SCROLL / RIGHT-PANEL REFRESH
# =============================================================================

def scroll_to_note(report_id: str, evidence_text: str = '') -> None:
    row        = df.iloc[current_patient_index]
    notes      = parse_notes(row[NOTES_COL])
    notes_html = build_notes_html(notes, highlighted_report=report_id, evidence_text=evidence_text)
    right_panel.clear()
    with right_panel:
        ui.html(notes_html).classes('w-full')
    ui.timer(0.05, lambda: ui.run_javascript(f"""
        const h = document.getElementById("evidence-highlight");
        if (h) {{ h.scrollIntoView({{behavior:"auto", block:"center"}}); }}
        else {{
            const f = document.getElementById("report_{report_id}");
            if (f) f.scrollIntoView({{behavior:"auto", block:"start"}});
        }}
    """), once=True)
    ui.timer(0.15, lambda: update_report_font_size(REPORT_FONT_SIZE), once=True)

# =============================================================================
# AGENT INTERVALS DISPLAY
# =============================================================================

def update_agent_display(agent_name: str, extraction: dict) -> None:
    global agent_output
    agent_output.clear()
    agents   = (extraction.get('systemic_therapy') or {}).get('agents', [])
    selected = next((a for a in agents if a.get('drug_name') == agent_name), None)
    if not selected:
        return
    intervals = sorted(selected.get('intervals', []), key=lambda x: sort_date_key(x.get('start_date')))
    with agent_output:
        for iv in intervals:
            ui.label(
                f"{iv.get('start_date','?')} → {iv.get('end_date','?')}"
            ).classes('text-xs text-gray-600')

# =============================================================================
# HELPER: side-by-side start / end date inputs
# =============================================================================

def _date_pair_row(
    start_label: str, start_ph: str, start_val: str,
    end_label:   str, end_ph:   str, end_val:   str,
):
    """Returns (start_input, end_input) placed side by side."""
    with ui.row().classes('w-full gap-1 items-start no-wrap'):
        si = ui.input(label=start_label, placeholder=start_ph).classes('flex-1').props('dense')
        ei = ui.input(label=end_label,   placeholder=end_ph  ).classes('flex-1').props('dense')
    if start_val: si.value = start_val
    if end_val:   ei.value = end_val
    return si, ei

# =============================================================================
# PROGRESSION CARD  (LLM events in left panel)
# =============================================================================

def progression_card(event: dict, patient_id: str, agent_names: list, extraction: dict) -> None:
    progression_date = event.get('progression_date', 'unknown')
    confidence       = event.get('confidence_level',  'unknown')
    rationale        = event.get('progression_date_rationale') or {}
    report_id        = rationale.get('report_id', 'unknown')
    note_date        = rationale.get('note_date',  'unknown')
    author           = rationale.get('author',     'unknown')
    evidence         = rationale.get('text', '')

    saved = get_saved_annotation(patient_id, report_id, progression_date) or {}

    with ui.element('div').classes('prog-card w-full'):

        # ── header ─────────────────────────────────────────────────────────
        with ui.element('div').classes('prog-card-header'):
            ui.label(progression_date).classes('text-xs font-bold text-slate-700')
            ui.label(f'Conf: {confidence}').classes('text-[10px] text-slate-400')

        # meta
        ui.html(
            f'<p class="prog-meta-line">Note: {html.escape(str(note_date))}'
            f'  ·  {html.escape(str(author))}'
            f'  ·  RID: {html.escape(str(report_id))}</p>'
        )

        # evidence snippet
        if evidence:
            with ui.element('div').classes('prog-evidence'):
                ui.label(evidence)

        with ui.row().classes('items-center gap-1 mt-1'):
            ui.button(
                'View Source',
                on_click=lambda rid=report_id, ev=evidence: scroll_to_note(rid, ev),
            ).props('dense flat size=xs')

        ui.separator().style('margin:3px 0;')

        # ── agent selector ─────────────────────────────────────────────────
        selected_agent = ui.select(
            agent_names,
            value=saved.get('agent') if saved.get('agent') in agent_names else None,
            label='Assign agent',
        ).classes('w-full').props('dense')

        # LLM interval hint
        llm_hint = ui.label('').classes('text-[10px] text-slate-400 leading-tight')

        def _refresh_hint(name=None):
            name = name or selected_agent.value
            if not name:
                llm_hint.set_text(''); return
            ls = get_agent_llm_start(name, extraction) or 'N/A'
            le = get_agent_llm_end(name,   extraction) or 'N/A'
            llm_hint.set_text(f'LLM: {ls} → {le}')

        _refresh_hint(saved.get('agent'))
        selected_agent.on('update:model-value', lambda _: _refresh_hint())

        # start / end overrides
        s_prefill = saved.get('agent_start', '') if saved.get('agent_start_source') == 'manual' else ''
        e_prefill = saved.get('agent_end',   '') if saved.get('agent_end_source')   == 'manual' else ''

        start_inp, end_inp = _date_pair_row(
            'Override start', 'YYYY-MM-DD', s_prefill,
            'Override end',   'YYYY-MM-DD', e_prefill,
        )

        # ── action buttons ─────────────────────────────────────────────────
        with ui.row().classes('gap-1 mt-1'):

            def _save(
                rid=report_id, sa=selected_agent, pid=patient_id,
                pd_=progression_date, ev=evidence,
                si=start_inp, ei=end_inp, extr=extraction,
            ):
                name = sa.value
                if not name:
                    ui.notify('No agent selected', color='red'); return

                ovs = (si.value or '').strip()
                if ovs:
                    cs = normalize_date(ovs)
                    if not cs: ui.notify('Invalid start date', color='red'); return
                    ag_start, ag_start_src = cs, 'manual'
                else:
                    ag_start = get_agent_llm_start(name, extr)
                    ag_start_src = 'LLM' if ag_start else None

                ove = (ei.value or '').strip()
                if ove:
                    ce = normalize_date(ove)
                    if not ce: ui.notify('Invalid end date', color='red'); return
                    ag_end, ag_end_src = ce, 'manual'
                else:
                    ag_end = get_agent_llm_end(name, extr)
                    ag_end_src = 'LLM' if ag_end else None

                save_agent_assignment(
                    rid, name, pid,
                    progression_date=pd_, evidence=ev,
                    agent_start=ag_start, agent_start_source=ag_start_src,
                    agent_end=ag_end,     agent_end_source=ag_end_src,
                )

            ui.button('Save', on_click=_save).props('dense size=sm')
            ui.button(
                'Remove',
                on_click=lambda rid=report_id, pid=patient_id,
                                pd_=progression_date, sa=selected_agent: (
                    delete_agent_assignment(rid, pid, pd_),
                    setattr(sa, 'value', None),
                    render_patient(current_patient_index),
                ),
            ).props('dense outline size=sm')

# =============================================================================
# PATIENT LIST DIALOG
# =============================================================================

def show_patient_list() -> None:
    refresh_annotations_df()
    annotated_mrns = set(annotations_df['mrn'].apply(safe_str).tolist())

    with ui.dialog() as dlg, ui.card().classes('w-[520px] max-h-[80vh] overflow-y-auto'):
        ui.label('Patient List').classes('text-lg font-bold mb-1')
        ui.label(
            f'{len(df)} patients  ·  {len(annotated_mrns)} with annotations'
        ).classes('text-xs text-gray-500 mb-2')

        with ui.row().classes('w-full text-xs font-bold border-b pb-1 mb-1'):
            ui.label('#').style('width:40px')
            ui.label('MRN').style('width:160px')
            ui.label('Annotated').style('width:90px')

        for i, row in df.iterrows():
            pid = normalize_patient_id(row[PATIENT_ID_COL])
            has = safe_str(pid) in annotated_mrns
            col = 'color:#16a34a;font-weight:600' if has else 'color:#dc2626'
            with ui.row().classes('w-full text-xs items-center patient-list-row rounded px-1'):
                ui.label(str(i + 1)).style('width:40px;color:#999')
                ui.label(pid).style('width:160px')
                ui.label('Yes' if has else 'No').style(f'width:90px;{col}')
                def go(idx=i, d=dlg):
                    global current_patient_index
                    current_patient_index = idx
                    d.close()
                    render_patient(current_patient_index)
                ui.button('Go', on_click=go).props('dense flat size=xs')

        ui.separator().classes('my-2')
        ui.button('Close', on_click=dlg.close).props('dense')
    dlg.open()

# =============================================================================
# EXPORT
# =============================================================================

def export_csv() -> None:
    import base64
    df_out = load_annotations_df()
    if df_out.empty:
        ui.notify('No annotations to export', color='orange'); return
    b64      = base64.b64encode(df_out.to_csv(index=False).encode('utf-8')).decode()
    filename = f"watney_annotations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    ui.run_javascript(f"""
        const a = document.createElement('a');
        a.href = 'data:text/csv;base64,{b64}';
        a.download = '{filename}';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
    """)
    ui.notify(f'Exported {len(df_out)} rows → {filename}', color='green')

# =============================================================================
# SETTINGS DIALOG
# =============================================================================

def show_settings() -> None:
    global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR
    global conn, cursor, df, REPORT_FONT_SIZE

    with ui.dialog() as dlg, ui.card().classes('w-[620px] max-h-[92vh] overflow-y-auto'):

        ui.label('Settings').classes('text-lg font-bold mb-1')

        # ── Session ──────────────────────────────────────────────────────────
        ui.label('Session').classes('text-sm font-semibold')
        ui.label(f'WATNEY v{WATNEY_VERSION}  ·  User: {CURRENT_USER or "not set"}').classes(
            'text-xs text-gray-500'
        )
        ui.separator()

        # ── Font size ─────────────────────────────────────────────────────────
        ui.label('Report Text Size').classes('text-sm font-semibold')
        font_slider  = ui.slider(min=7, max=20, step=1, value=REPORT_FONT_SIZE).classes('w-full')
        font_display = ui.label(f'{REPORT_FONT_SIZE} px').classes('text-xs text-gray-500')

        def _on_font(e=None):
            global REPORT_FONT_SIZE
            REPORT_FONT_SIZE = int(font_slider.value)
            font_display.set_text(f'{REPORT_FONT_SIZE} px')
            update_report_font_size(REPORT_FONT_SIZE)
            cfg = load_config(); cfg['report_font_size'] = REPORT_FONT_SIZE; save_config(cfg)

        font_slider.on('update:model-value', _on_font)
        with ui.row().classes('gap-2'):
            ui.button('−', on_click=lambda: (
                setattr(font_slider, 'value', max(7, font_slider.value - 1)), _on_font()
            )).props('dense outline')
            ui.button('+', on_click=lambda: (
                setattr(font_slider, 'value', min(20, font_slider.value + 1)), _on_font()
            )).props('dense outline')
            ui.button('Reset', on_click=lambda: (
                setattr(font_slider, 'value', 11), _on_font()
            )).props('dense outline')

        ui.separator()

        # ── File paths ────────────────────────────────────────────────────────
        ui.label('File Paths').classes('text-sm font-semibold')
        csv_p = Path(EXTRACTION_CSV_PATH).resolve()
        db_p  = Path(SQLITE_PATH).resolve()
        ui.label(f'CSV: {csv_p}').classes('text-xs text-gray-500 break-all')
        ui.label(f'DB:  {db_p}' ).classes('text-xs text-gray-500 break-all')
        ui.separator()

        ui.label('Change Paths').classes('text-sm font-semibold')
        ui.label('Changes take effect immediately.').classes('text-xs text-orange-500 mb-1')
        new_csv = ui.input(label='New CSV path', placeholder=str(csv_p)).classes('w-full')
        new_db  = ui.input(label='New DB path',  placeholder=str(db_p) ).classes('w-full')
        path_st = ui.label('').classes('text-xs')

        def apply_paths():
            global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR
            global conn, cursor, df, annotations_df
            changed = False
            ncsv = new_csv.value.strip()
            ndb  = new_db.value.strip()
            if ncsv:
                p = Path(ncsv)
                if not p.exists(): path_st.set_text(f'CSV not found: {p}'); return
                try:
                    df = load_dataframe(str(p))
                    EXTRACTION_CSV_PATH = str(p)
                    cfg = load_config(); cfg['csv_path'] = str(p); save_config(cfg)
                    changed = True
                except Exception as e:
                    path_st.set_text(f'CSV error: {e}'); return
            if ndb:
                p = Path(ndb)
                try:
                    conn.commit(); conn.close(); _reopen_db(str(p))
                    SQLITE_PATH = str(p); ANNOTATION_OUTPUT_DIR = p.parent; changed = True
                except Exception as e:
                    path_st.set_text(f'DB error: {e}'); return
            if changed:
                refresh_annotations_df(); render_patient(current_patient_index)
                path_st.set_text('Updated.'); new_csv.value = ''; new_db.value = ''
            else:
                path_st.set_text('No new paths entered.')

        ui.button('Apply', on_click=apply_paths).props('dense')
        ui.separator()

        # ── Annotation folder ─────────────────────────────────────────────────
        ui.label('Annotation Folder').classes('text-sm font-semibold')
        cur_dir = Path(ANNOTATION_OUTPUT_DIR).resolve()
        ui.label(f'Current: {cur_dir}').classes('text-xs text-gray-500 break-all mb-1')
        ui.label(
            'Copies DB + config to new location, updates all paths, removes old files.'
        ).classes('text-xs text-orange-500 mb-1')
        new_dir_inp = ui.input(
            label='New folder', placeholder='/path/to/new/watney_annotations'
        ).classes('w-full')
        move_st = ui.label('').classes('text-xs')

        def move_folder():
            global ANNOTATION_OUTPUT_DIR, SQLITE_PATH, CONFIG_PATH
            global conn, cursor, annotations_df
            nds = new_dir_inp.value.strip()
            if not nds: move_st.set_text('Enter a path.'); return
            nd = Path(nds).resolve()
            if nd == cur_dir: move_st.set_text('Same as current.'); return
            try: nd.mkdir(parents=True, exist_ok=True)
            except Exception as e: move_st.set_text(f'Cannot create: {e}'); return
            old_db  = Path(SQLITE_PATH).resolve()
            old_cfg = Path(CONFIG_PATH).resolve()
            new_db_ = nd / old_db.name
            new_cfg = nd / old_cfg.name
            try: conn.commit(); conn.close()
            except Exception: pass
            try: shutil.copy2(str(old_db), str(new_db_))
            except Exception as e: move_st.set_text(f'DB copy failed: {e}'); _reopen_db(str(old_db)); return
            try:
                if old_cfg.exists(): shutil.copy2(str(old_cfg), str(new_cfg))
            except Exception: pass
            _reopen_db(str(new_db_))
            ANNOTATION_OUTPUT_DIR = nd; SQLITE_PATH = str(new_db_); CONFIG_PATH = new_cfg
            cfg = {}
            try:
                with open(new_cfg) as f: cfg = json.load(f)
            except Exception: pass
            cfg['csv_path'] = EXTRACTION_CSV_PATH
            with open(new_cfg, 'w') as f: json.dump(cfg, f, indent=2)
            try: old_db.unlink(missing_ok=True)
            except Exception: pass
            try:
                if old_cfg.exists() and old_cfg != new_cfg: old_cfg.unlink(missing_ok=True)
            except Exception: pass
            try:
                od = old_db.parent
                if od != nd and not any(od.iterdir()): od.rmdir()
            except Exception: pass
            refresh_annotations_df()
            move_st.set_text(f'Moved → {nd}'); new_dir_inp.value = ''

        ui.button('Move Folder', on_click=move_folder).props('dense')
        ui.separator()

        # ── DB stats ──────────────────────────────────────────────────────────
        ui.label('Database Stats').classes('text-sm font-semibold')
        stats = load_annotations_df()
        ui.label(
            f'Rows: {len(stats)}  ·  '
            f'Patients: {stats["mrn"].nunique() if not stats.empty else 0}  ·  '
            f'LLM: {len(stats[stats["progression_source"]=="LLM"]) if not stats.empty else 0}  ·  '
            f'Manual: {len(stats[stats["progression_source"]=="manual"]) if not stats.empty else 0}'
        ).classes('text-xs text-gray-500')
        ui.separator()

        # ── Resources / GitHub ────────────────────────────────────────────────
        ui.label('Resources').classes('text-sm font-semibold')
        GITHUB_URL = 'https://github.com/justin-vinh/watney_project'
        if GITHUB_URL:
            ui.link('WATNEY on GitHub', GITHUB_URL, new_tab=True).classes('text-xs text-blue-600')
        else:
            with ui.row().classes('items-center gap-1'):
                ui.label('GitHub:').classes('text-xs text-gray-400')
                ui.label('(set GITHUB_URL in source to enable)').classes(
                    'text-xs text-gray-300 italic'
                )
        ui.separator()

        ui.button('Close', on_click=dlg.close).props('dense')
    dlg.open()

# =============================================================================
# RENDER PATIENT
# =============================================================================

def render_patient(index: int) -> None:
    global agent_output, progression_sort_order, user_label

    refresh_annotations_df()
    left_panel.clear()
    right_panel.clear()

    row        = df.iloc[index]
    patient_id = normalize_patient_id(row[PATIENT_ID_COL])
    extraction = safe_json_loads(row[GENERATION_COL])
    notes      = parse_notes(row[NOTES_COL])
    notes_html = build_notes_html(notes)

    drug_names = sorted([
        a.get('drug_name')
        for a in (extraction.get('systemic_therapy') or {}).get('agents', [])
        if a.get('drug_name')
    ])

    events = (extraction.get('progression') or {}).get('progression_events', [])

    # ═══════════════════════════════════════════════════════════════════════
    # LEFT PANEL
    # ═══════════════════════════════════════════════════════════════════════
    with left_panel:

        # ── header ──────────────────────────────────────────────────────────
        with ui.column().classes('gap-0 mb-1'):
            ui.label(f'WATNEY v{WATNEY_VERSION}').classes('text-xl font-bold leading-tight')
            ui.label('Developed by Justin Vinh @ DFCI').classes('text-[10px] text-gray-400')
            user_label = ui.label(
                f'User: {CURRENT_USER or "not set"}'
            ).classes('text-[10px] text-gray-500')

        ui.separator().classes('my-1')
        ui.label(f'Patient {patient_id}  ·  {index + 1} / {len(df)}').classes(
            'text-sm font-bold leading-tight'
        )

        # ── Progression Summary ──────────────────────────────────────────────
        ui.label('Progression Summary').classes('text-[11px] font-bold mt-1')

        patient_ann = annotations_df[
            annotations_df['mrn'].apply(safe_str) == safe_str(patient_id)
        ]
        summary_rows = sorted([
            {
                'date':        normalize_any_date(a.get('progression_date')) or '',
                'sort_date':   sort_date_key(a.get('progression_date')),
                'agent':       a.get('agent', '') or '',
                'ag_start':    a.get('agent_start', '') or '',
                'prog_src':    a.get('progression_source', '') or '',
                'agent_src':   a.get('agent_start_source', '') or '',
                'ag_start_src':a.get('agent_start_source', '') or '',
                'user':        a.get('annotated_by', '') or '',
            }
            for _, a in patient_ann.iterrows()
            if normalize_any_date(a.get('progression_date'))
        ], key=lambda x: x['sort_date'])

        if not summary_rows:
            ui.label('NO PROGRESSION DATES ASSIGNED').classes('text-[10px] text-red-500 font-bold')
        else:
            # Column widths in px
            # Prog.Date | Agent | Ag.Start | Prog.Src | Agent Src | Ag.Start Src | User
            W = dict(date=68, agent=72, ag_start=66, psrc=38, asrc=34, assrc=40)
            with ui.column().classes('w-full gap-0'):
                with ui.element('div').classes('summary-row font-bold border-b').style(
                    'padding-bottom:2px; margin-bottom:2px; font-size:10px;'
                ):
                    ui.label('Prog.Date' ).style(f'width:{W["date"]}px')
                    ui.label('Agent'     ).style(f'width:{W["agent"]}px')
                    ui.label('Ag.Start'  ).style(f'width:{W["ag_start"]}px')
                    ui.label('Prog.Src'  ).style(f'width:{W["psrc"]}px')
                    ui.label('Ag.Src'    ).style(f'width:{W["asrc"]}px')
                    ui.label('Start.Src' ).style(f'width:{W["assrc"]}px')
                    ui.label('User'      ).style(
                        'flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis'
                    )
                for sr in summary_rows:
                    with ui.element('div').classes('summary-row'):
                        ui.label(sr['date']        ).style(f'width:{W["date"]}px')
                        ui.label(sr['agent']       ).style(
                            f'width:{W["agent"]}px; overflow:hidden; text-overflow:ellipsis'
                        )
                        ui.label(sr['ag_start']    ).style(f'width:{W["ag_start"]}px')
                        ui.label(sr['prog_src']    ).style(f'width:{W["psrc"]}px')
                        ui.label(sr['agent_src']   ).style(f'width:{W["asrc"]}px')
                        ui.label(sr['ag_start_src']).style(f'width:{W["assrc"]}px')
                        ui.label(sr['user']        ).style(
                            'flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis'
                        )

        # ── Agent Intervals ──────────────────────────────────────────────────
        ui.separator().classes('my-1')
        with ui.column().classes('agent-box w-full gap-1'):
            ui.label('Agent Intervals').classes('text-[11px] font-bold')
            agent_output = ui.column()
            if drug_names:
                dd = ui.select(drug_names, value=drug_names[0]).classes('w-full').props('dense')
                dd.on('update:model-value', lambda _: update_agent_display(dd.value, extraction))
                update_agent_display(drug_names[0], extraction)

        # ── Sort ─────────────────────────────────────────────────────────────
        ui.separator().classes('my-1')
        sort_select = ui.select(
            ['Ascending', 'Descending'],
            value=progression_sort_order,
            label='Progression order',
        ).classes('w-full').props('dense')

        # ── LLM Progression Events ───────────────────────────────────────────
        ui.separator().classes('my-1')
        ui.label('LLM Progression Events').classes('text-[11px] font-bold')

        ordered = sorted(
            events,
            key=lambda x: sort_date_key(x.get('progression_date')),
            reverse=(progression_sort_order == 'Descending'),
        )

        if not ordered:
            ui.label('No LLM progression events').classes('text-[10px] text-gray-400')
        else:
            for ev in ordered:
                progression_card(ev, patient_id, drug_names, extraction)

        # ── Clinician Added Events ───────────────────────────────────────────
        ui.separator().classes('my-1')
        ui.label('Clinician Added Progression Events').classes('text-[11px] font-bold')

        clin_events = get_clinician_events(patient_id)
        if clin_events.empty:
            ui.label('None').classes('text-[10px] text-gray-400')
        else:
            for _, cev in clin_events.iterrows():
                with ui.element('div').classes('prog-card w-full'):
                    with ui.element('div').classes('prog-card-header'):
                        ui.label(str(cev.get('progression_date', '—'))).classes(
                            'text-xs font-bold text-slate-700'
                        )
                        ui.label('CLINICIAN').classes('text-[9px] text-red-500 font-bold')
                    ui.html(
                        f'<p class="prog-meta-line">'
                        f'Agent: {html.escape(str(cev.get("agent","")))}  ·  '
                        f'Start: {html.escape(str(cev.get("agent_start","") or "—"))}  ·  '
                        f'End: {html.escape(str(cev.get("agent_end","") or "—"))}'
                        f'</p>'
                    )
                    if cev.get('evidence'):
                        with ui.element('div').classes('prog-evidence'):
                            ui.label(cev['evidence'])
                    ui.html(
                        f'<p class="prog-meta-line">By: {html.escape(str(cev.get("determined_by","—")))}</p>'
                    )

                    def _del(rid=cev['report_id']):
                        cursor.execute(
                            "DELETE FROM annotations WHERE report_id = ?", (rid,)
                        )
                        save_annotations()
                        ui.notify('Clinician event removed', color='orange')
                        render_patient(current_patient_index)

                    ui.button('Remove', on_click=_del).props('dense outline size=xs')

        def on_sort_change(_):
            global progression_sort_order
            progression_sort_order = sort_select.value
            render_patient(current_patient_index)

        sort_select.on('update:model-value', on_sort_change)

        # ── Add Clinician Progression Event ──────────────────────────────────
        ui.separator().classes('my-1')
        ui.label('Add Clinician Progression Event').classes('text-[11px] font-bold')

        _cfg_now     = load_config()
        custom_agents= _cfg_now.get('custom_agents', [])
        all_agents   = drug_names + [a for a in custom_agents if a not in drug_names]

        clin_agent = ui.select(all_agents or [], label='Agent').classes('w-full').props('dense')

        with ui.row().classes('w-full items-center gap-1'):
            custom_inp = ui.input(
                label='Add custom agent', placeholder='e.g. Pembrolizumab'
            ).classes('flex-grow').props('dense')

            def add_custom():
                name = custom_inp.value.strip()
                if not name: ui.notify('Enter a name', color='red'); return
                cfg = load_config()
                ex  = cfg.get('custom_agents', [])
                if name not in ex and name not in drug_names:
                    ex.append(name); cfg['custom_agents'] = ex; save_config(cfg)
                    ui.notify(f'Added {name}', color='green')
                upd = drug_names + [a for a in cfg.get('custom_agents', []) if a not in drug_names]
                clin_agent.options = upd; clin_agent.value = name; clin_agent.update()
                custom_inp.value = ''

            ui.button('Add', on_click=add_custom).props('dense outline')

        # LLM interval hint for clinician form
        clin_hint = ui.label('').classes('text-[10px] text-slate-400')

        def _clin_hint(_=None):
            n = clin_agent.value
            if not n: clin_hint.set_text(''); return
            ls = get_agent_llm_start(n, extraction) or 'N/A'
            le = get_agent_llm_end(n,   extraction) or 'N/A'
            clin_hint.set_text(f'LLM: {ls} → {le}')

        clin_agent.on('update:model-value', _clin_hint)

        clin_date = ui.input(
            label='Progression Date (YYYY-MM-DD)', placeholder='YYYY-MM-DD'
        ).classes('w-full').props('dense')

        def _fmt_date():
            v = clin_date.value
            if not v: return
            digits = re.sub(r'\D', '', v)
            if len(digits) != 8:
                ui.notify('Enter 8 digits YYYYMMDD', color='red'); return
            clin_date.set_value(f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}')

        clin_date.on('blur', lambda _: _fmt_date())

        clin_evidence = ui.textarea(
            label='Evidence (optional)'
        ).classes('w-full').props('rows=1 autogrow dense')

        clin_report_id  = ui.input(label='Report ID (optional)').classes('w-full').props('dense')
        clin_determined = ui.input(
            label='Determined by', placeholder='e.g. Dr. X'
        ).classes('w-full').props('dense')

        clin_start, clin_end = _date_pair_row(
            'Agent start (opt)', 'YYYY-MM-DD', '',
            'Agent end (opt)',   'YYYY-MM-DD', '',
        )

        def save_clin_event():
            _r   = df.iloc[current_patient_index]
            pid  = normalize_patient_id(_r[PATIENT_ID_COL])

            digits = re.sub(r'\D', '', clin_date.value or '')
            if digits and len(digits) != 8:
                ui.notify('Date must be 8 digits YYYYMMDD', color='red'); return
            cleaned = f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}' if digits else None

            rid = (clin_report_id.value or '').strip() or (
                f'clinician::{pid}::{datetime.now().timestamp()}'
            )

            ovs = (clin_start.value or '').strip()
            if ovs:
                cs = normalize_date(ovs)
                if not cs: ui.notify('Invalid start date', color='red'); return
                ag_start, ag_start_src = cs, 'manual'
            else:
                ag_start = get_agent_llm_start(clin_agent.value, extraction)
                ag_start_src = 'LLM' if ag_start else None

            ove = (clin_end.value or '').strip()
            if ove:
                ce = normalize_date(ove)
                if not ce: ui.notify('Invalid end date', color='red'); return
                ag_end, ag_end_src = ce, 'manual'
            else:
                ag_end = get_agent_llm_end(clin_agent.value, extraction)
                ag_end_src = 'LLM' if ag_end else None

            save_clinician_progression_event(
                patient_id=pid, progression_date=cleaned,
                agent=clin_agent.value,
                evidence=clin_evidence.value or '',
                report_id=rid, determined_by=clin_determined.value,
                agent_start=ag_start, agent_start_source=ag_start_src,
                agent_end=ag_end,     agent_end_source=ag_end_src,
            )

            for w in (clin_date, clin_evidence, clin_report_id, clin_determined, clin_start, clin_end):
                w.value = ''
            clin_agent.value = None
            render_patient(current_patient_index)

        ui.button('Save Clinician Progression Event', on_click=save_clin_event).props('dense')
        ui.separator().classes('my-2')

    # ═══════════════════════════════════════════════════════════════════════
    # RIGHT PANEL
    # ═══════════════════════════════════════════════════════════════════════
    with right_panel:
        ui.label('All Relevant Notes').classes('text-base font-bold mb-2')
        ui.html(notes_html).classes('w-full')

    ui.timer(0.1, lambda: update_report_font_size(REPORT_FONT_SIZE), once=True)

# =============================================================================
# NAVIGATION
# =============================================================================

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

def _prev_patient():
    if require_user(): prev_patient()

def _next_patient():
    if require_user(): next_patient()

def _show_patient_list():
    if require_user(): show_patient_list()

def _export_csv():
    if require_user(): export_csv()

def _show_settings():
    if require_user(): show_settings()

# =============================================================================
# NAV BAR — fixed to bottom of left 1/3
# =============================================================================

nav_bar = ui.element('div').classes('bottom-nav').style('display:none')
with nav_bar:
    with ui.element('div').style('display:flex; gap:5px; align-items:center;'):
        ui.button('← Prev', on_click=_prev_patient).props('dense')
        ui.button('Next →', on_click=_next_patient).props('dense')
    with ui.element('div').style('display:flex; gap:4px; align-items:center;'):
        ui.button('List',     on_click=_show_patient_list).props('dense outline')
        ui.button('Export',   on_click=_export_csv       ).props('dense outline')
        ui.button('Settings', on_click=_show_settings    ).props('dense outline')

# =============================================================================
# START
# =============================================================================

ui.timer(0.3, _register_keyboard_nav, once=True)

ui.run(title='WATNEY — LLM Oncology Reviewer', reload=False)