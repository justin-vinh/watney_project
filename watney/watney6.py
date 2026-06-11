"""
watney_v6.py - WATNEY oncology annotation platform.

WATNEY (Workflow for Annotating Therapeutics and Noting Events in oncologY) is a
NiceGUI-based human-in-the-loop review tool for LLM-extracted oncology progression
events. Annotators review extraction cards, assign agents to progression dates, add
clinician-entered events, exclude patients, and export a structured CSV.

Entry point: main() at the bottom of this module.
Run with:    python watney_v6.py
"""

import re
import json
import html
import sqlite3
from pathlib import Path
from datetime import datetime

import pandas as pd
from nicegui import ui

# Support both installed-package imports (watney.project) and local dev imports.
try:
    from watney.project import (
        load_global_config, save_global_config,
        create_project, open_project, load_extraction_into_project,
        get_project_annotations_db_path, get_project_exports_dir,
        get_project_checkpoints_dir, do_checkpoint, open_annotations_db,
        migrate_legacy_folder, ProjectError,
        get_recent_projects, record_recent_project, open_patients_db,
    )
    from watney.drug_dict import DRUG_DICT_JS
except ImportError:
    from project import (
        load_global_config, save_global_config,
        create_project, open_project, load_extraction_into_project,
        get_project_annotations_db_path, get_project_exports_dir,
        get_project_checkpoints_dir, do_checkpoint, open_annotations_db,
        migrate_legacy_folder, ProjectError,
        get_recent_projects, record_recent_project, open_patients_db,
    )
    from drug_dict import DRUG_DICT_JS

# =============================================================================
# CONFIG
# =============================================================================

try:
    from importlib.metadata import version as _pkg_version
    WATNEY_VERSION = _pkg_version('watney')
except Exception:
    WATNEY_VERSION = '6'  # fallback for dev / non-package runs

def _major_version(v):
    """Return only the major version number, e.g. '3.0.1' -> '3'."""
    try:
        return str(v).split('.')[0]
    except Exception:
        return str(v)

# Expected column names in the input extraction CSV.
NOTES_COL = 'all_notes'
GENERATION_COL = 'generation'
PATIENT_ID_COL = 'DFCI_MRN'

# ── Project-based paths (set when a project is opened) ──────────────────────
CURRENT_PROJECT: dict = {}         # project.json contents
PROJECT_DIR: Path | None = None    # absolute path to project folder

# Legacy fallback (used only when no project is open — e.g. at module load)
ANNOTATION_OUTPUT_DIR = Path('./watney_annotations')
SQLITE_PATH = ANNOTATION_OUTPUT_DIR / 'watney_annotations_database.db'
CONFIG_PATH = ANNOTATION_OUTPUT_DIR / 'watney_config.json'

# Active extraction CSV path; updated when a project is opened or extraction is switched.
EXTRACTION_CSV_PATH = None
# Username set at login; tags every annotation row written to the database.
CURRENT_USER = None
# When True the login overlay is shown and annotation actions are blocked.
UI_LOCKED = True
# NiceGUI element references kept at module scope so logout can reset them.
user_label = None
nav_bar = None
# In-memory DataFrame of the active extraction CSV.
df = None
# Default note body font size in pixels; overridden by global config.
NOTE_FONT_SIZE = 11

# Mutable flags updated by the background version check on page load.
_UPDATE_AVAILABLE = [False]
_LATEST_VERSION   = [None]

# =============================================================================
# CONFIG FILE HELPERS
# Thin wrappers — now reads from global config (~/.watney/config.json).
# Per-project settings live in project.json (managed by project.py).
# NOTE_FONT_SIZE and disable_cmdf are stored in global config for convenience.
# =============================================================================

def load_config() -> dict:
    """Load global config (font size, cmdf setting, last_project)."""
    return load_global_config()

def save_config(cfg: dict):
    """Save global config."""
    save_global_config(cfg)

_cfg = load_config()
if _cfg.get('note_font_size'):
    NOTE_FONT_SIZE = int(_cfg['note_font_size'])

# =============================================================================
# LOAD DATA
# =============================================================================

def load_dataframe(path: str) -> pd.DataFrame:
    """Read an extraction CSV, preserving MRN column as string to avoid float coercion."""
    return pd.read_csv(path, dtype={PATIENT_ID_COL: str})

# =============================================================================
# SQLITE SETUP
# Project-based: conn/cursor are set when a project is opened (_open_project_db).
# A minimal legacy DB is created at module load so that the module-level helpers
# (load_annotations_df etc.) don't crash before login.
# =============================================================================

def _bootstrap_legacy_db():
    """Open (or create) the legacy annotations DB for pre-login safety.

    Creates the watney_annotations/ folder and a minimal SQLite database so
    module-level helpers do not crash before the user logs in and opens a
    project. Once a project is opened, _open_project_db() replaces this
    connection with the project's own annotations.db.
    """
    ANNOTATION_OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
    c = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("""CREATE TABLE IF NOT EXISTS annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        DFCI_MRN TEXT NOT NULL,
        progression_date TEXT, progression_source TEXT, agent TEXT,
        evidence TEXT, report_id TEXT, determined_by TEXT, user TEXT,
        modification_timestamp TEXT, agent_start TEXT, agent_start_source TEXT,
        agent_end TEXT, agent_end_source TEXT,
        exclusion_flag TEXT, exclusion_reason TEXT, extraction_version TEXT,
        deleted INTEGER DEFAULT 0, deletion_reason TEXT, deletion_timestamp TEXT,
        import_source TEXT, unexclusion_reason TEXT)""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_progression_event
        ON annotations (DFCI_MRN, progression_date, progression_source, report_id)""")
    for _col, _typ in [
        ('agent_start','TEXT'),('agent_start_source','TEXT'),
        ('agent_end','TEXT'),('agent_end_source','TEXT'),
        ('exclusion_flag','TEXT'),('exclusion_reason','TEXT'),
        ('extraction_version','TEXT'),
        ('deleted','INTEGER'),('deletion_reason','TEXT'),('deletion_timestamp','TEXT'),
        ('import_source','TEXT'),('unexclusion_reason','TEXT'),
    ]:
        try: c.execute(f'ALTER TABLE annotations ADD COLUMN {_col} {_typ}')
        except sqlite3.OperationalError: pass
    c.commit()
    return c, c.cursor()

conn, cursor = _bootstrap_legacy_db()

def _open_project_db(project_dir: Path):
    """Replace the module-level conn/cursor with the project's annotations.db.

    Called once when a project is opened so all subsequent DB helpers use the
    correct project database rather than the legacy bootstrap path.
    """
    global conn, cursor
    try: conn.close()
    except Exception: pass
    conn = open_annotations_db(project_dir)
    cursor = conn.cursor()

# =============================================================================
# SQLITE HELPERS
# =============================================================================

def load_annotations_df(include_deleted: bool = False) -> pd.DataFrame:
    """Return the annotations table as a DataFrame.

    Args:
        include_deleted: When False (default) rows with deleted=1 are excluded.
            Pass True only for admin or audit purposes.
    """
    if include_deleted:
        return pd.read_sql_query("SELECT * FROM annotations", conn)
    return pd.read_sql_query(
        "SELECT * FROM annotations WHERE (deleted IS NULL OR deleted = 0)", conn
    )

annotations_df = load_annotations_df()

# ─────────────────────────────────────────────────────────────────────────────
# PROJECT OPEN HELPER  (called from login flow inside build_page)
# ─────────────────────────────────────────────────────────────────────────────
def _apply_opened_project(meta: dict, project_df, project_conn, project_dir: Path):
    """Atomically update all module-level state when a project is opened.

    Sets CURRENT_PROJECT, PROJECT_DIR, df, conn, cursor, annotations_df, and
    EXTRACTION_CSV_PATH from the newly opened project, then records the project
    path in the global recent-projects list.
    """
    global CURRENT_PROJECT, PROJECT_DIR, df, conn, cursor, annotations_df
    global EXTRACTION_CSV_PATH
    CURRENT_PROJECT = meta
    PROJECT_DIR = project_dir
    df = project_df
    conn = project_conn
    cursor = conn.cursor()
    annotations_df = load_annotations_df()
    # EXTRACTION_CSV_PATH kept for legacy compatibility inside build_page closures
    EXTRACTION_CSV_PATH = str(project_dir / meta['active_extraction'])
    # Persist last_project + recents in global config
    record_recent_project(project_dir, meta.get('project_name', project_dir.name))

def require_user() -> bool:
    """Guard used by annotation write functions; notifies and returns False if no user is set."""
    if not CURRENT_USER:
        ui.notify('Enter username first', color='red')
        return False
    return True

def refresh_annotations_df():
    """Reload annotations_df from the database, discarding any cached state."""
    global annotations_df
    annotations_df = load_annotations_df()

def save_annotations():
    """Commit the current transaction and refresh the in-memory annotations cache."""
    conn.commit()
    refresh_annotations_df()

def safe_str(x) -> str:
    """Coerce any value to a stripped string, returning '' for NaN/None/bytes."""
    if pd.isna(x):
        return ''
    try:
        if isinstance(x, bytes):
            return x.decode('utf-8', errors='ignore').strip()
        return str(x).strip()
    except:
        return ''

def normalize_patient_id(x) -> str:
    """Normalise an MRN value to a clean string.

    Strips trailing '.0' artifacts introduced when pandas reads integer IDs
    from a CSV into a float column before string conversion.
    """
    if pd.isna(x):
        return ''
    s = str(x).strip()
    if s.endswith('.0'):
        s = s[:-2]
    return s

def upsert_annotation(new_row: dict):
    """Insert or update a single annotation row.

    Matches on (DFCI_MRN, report_id, progression_date, progression_source).
    On update, agent/evidence/interval fields are overwritten; extraction_version
    is set only if not already populated (COALESCE guard).
    On insert, inherits any existing exclusion_flag for the patient.
    """
    cursor.execute("""
    SELECT id FROM annotations
    WHERE DFCI_MRN=? AND report_id=? AND progression_date=? AND progression_source=?
    """, (new_row['DFCI_MRN'], new_row['report_id'],
          new_row['progression_date'], new_row['progression_source']))
    existing = cursor.fetchone()
    _ext_ver = new_row.get('extraction_version') or (
        Path(EXTRACTION_CSV_PATH).name if EXTRACTION_CSV_PATH and PROJECT_DIR else None
    )
    if existing:
        cursor.execute("""
        UPDATE annotations
        SET agent=?, evidence=?, determined_by=?, user=?, modification_timestamp=?,
            agent_start=?, agent_start_source=?, agent_end=?, agent_end_source=?,
            extraction_version=COALESCE(?,extraction_version)
        WHERE id=?
        """, (
            new_row['agent'], new_row['evidence'], new_row['determined_by'],
            new_row.get('user'), new_row['modification_timestamp'],
            new_row.get('agent_start'), new_row.get('agent_start_source'),
            new_row.get('agent_end'),   new_row.get('agent_end_source'),
            _ext_ver, existing['id']
        ))
    else:
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
            exclusion_flag, exclusion_reason, extraction_version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            new_row['DFCI_MRN'], new_row['progression_date'], new_row['progression_source'],
            new_row['agent'], new_row['evidence'], new_row['report_id'],
            new_row['determined_by'], new_row.get('user'), new_row['modification_timestamp'],
            new_row.get('agent_start'), new_row.get('agent_start_source'),
            new_row.get('agent_end'),   new_row.get('agent_end_source'),
            _eflag, _ereason, _ext_ver,
        ))
    save_annotations()

def save_agent_assignment(rid: str, agent_value: str, patient_id: str,
                          progression_date=None, evidence=None,
                          agent_start=None, agent_start_source=None,
                          agent_end=None, agent_end_source=None):
    """Persist an LLM-sourced agent assignment for a progression event."""
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

def save_clinician_progression_event(
        patient_id: str, progression_date: str, agent: str, evidence: str,
        # Annotator-entered progression event not derived from the LLM extraction.
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

def get_saved_annotation(patient_id: str, report_id: str, progression_date: str):
    """Fetch the saved agent assignment for a given progression event, or None."""
    cursor.execute("""
    SELECT agent, agent_start, agent_start_source, agent_end, agent_end_source
    FROM annotations WHERE DFCI_MRN=? AND report_id=? AND progression_date=? LIMIT 1
    """, (patient_id, report_id, progression_date))
    return cursor.fetchone()

def delete_agent_assignment(rid: str, patient_id: str, progression_date: str) -> bool:
    """Hard-delete an annotation row by its event key. Returns True if a row was removed.

    Note: prefer soft-delete (deleted=1) for audit trails. This function is used
    only for undoing an in-session agent assignment before any downstream use.
    """
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

def get_clinician_events(patient_id: str) -> pd.DataFrame:
    """Return all manually entered (clinician) annotation rows for a patient."""
    patient_id = normalize_patient_id(patient_id)
    return pd.read_sql_query("""
    SELECT * FROM annotations WHERE DFCI_MRN=? AND progression_source='manual'
    ORDER BY progression_date
    """, conn, params=(patient_id,))

def safe_json_loads(x) -> dict:
    """Parse a JSON string, returning an empty dict on any failure or NaN input."""
    if pd.isna(x): return {}
    try: return json.loads(x)
    except: return {}

def compress_blank_lines(text: str) -> str:
    """Collapse runs of three or more blank lines down to a single blank line."""
    return re.sub(r'\n\s*\n\s*\n+', '\n\n', text)

def extract_field(pattern: str, text: str, default: str = 'unknown') -> str:
    """Extract the first capture group of a regex from text, or return default."""
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else default

def normalize_any_date(x) -> str | None:
    """Convert a date value to ISO-8601 string, or return None/'NA' for missing values.

    Accepts multiple date formats common in clinical exports. Returns the literal
    string 'NA' when the source value is the string 'NA' (indicating the LLM
    determined no progression date was present but the event was still annotated).
    """
    if pd.isna(x): return None
    x = str(x).strip()
    if not x or x.lower() == 'nan': return None
    if x.upper() == 'NA': return 'NA'
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m/%d/%y", "%m/%d/%Y"):
        try: return datetime.strptime(x, fmt).date().isoformat()
        except: continue
    return x

def dates_within_days(d1: str, d2: str, n: int = 7) -> bool:
    """Return True if both dates parse and |d1 - d2| <= n days."""
    try:
        from datetime import date as _date, timedelta as _td
        _d1 = _date.fromisoformat(str(d1).strip()[:10])
        _d2 = _date.fromisoformat(str(d2).strip()[:10])
        return abs((_d1 - _d2).days) <= n
    except Exception:
        return False

def sort_date_key(x) -> str:
    """Return an ISO date string suitable for lexicographic sort; unknown dates sort last."""
    d = normalize_any_date(x)
    if not d or pd.isna(d): return "9999-12-31"
    return str(d)

def clean_date_input(raw):
    if not raw: return None
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

def parse_notes(notes_text: str) -> list[dict]:
    """Parse the concatenated notes string into a list of structured note dicts.

    Each dict has keys: note_number, note_date, dept, author, report_id, body.
    Notes are delimited by separator lines (40+ '=' or '-' characters).
    """
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

def highlight_evidence(note_text: str, evidence_text: str) -> tuple:
    """Wrap matching evidence text in an HTML highlight span.

    Returns (html_str, matched_bool). Searches note_text for evidence_text
    using a token-overlap heuristic and wraps the best match in a span with
    class 'evidence-highlight'. Falls back to the plain escaped text if no
    adequate match is found.
    """
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

def build_notes_html(notes: list[dict], highlighted_report: str | None = None,
                     evidence_text: str | None = None) -> str:
    """Render parsed note dicts to HTML for the right-panel pane.

    Args:
        notes: Output of parse_notes().
        highlighted_report: report_id whose body should have evidence_text highlighted.
        evidence_text: Passage to highlight inside the selected report.
    """
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
# MULTI-INSTANCE DETECTION
# Tracks connected browser clients so the UI can warn when multiple annotators
# are accessing the same instance simultaneously.
# =============================================================================

# Shared mutable counter; incremented on connect, decremented on disconnect.
_active_clients = {'count': 0, 'timestamps': []}


# =============================================================================
# PAGE
# =============================================================================

ui.add_head_html(f'''<script>
(function(){{
// ── Drug dictionary ────────────────────────────────────────────────────────
{DRUG_DICT_JS}

// Build reverse map: alias → canonical
var ALIAS_MAP = {{}};
Object.keys(DRUGS).forEach(function(canonical){{
  ALIAS_MAP[canonical] = canonical;
  (DRUGS[canonical] || []).forEach(function(alias){{
    ALIAS_MAP[alias.toLowerCase()] = canonical;
  }});
}});

function resolveQuery(q){{
  q = (q || '').trim().toLowerCase();
  if(!q) return {{aliases:[]}};
  var canonical = ALIAS_MAP[q] || null;
  if(canonical){{
    var allTerms = [canonical].concat(DRUGS[canonical] || []);
    var seen = {{}};
    allTerms = allTerms.filter(function(t){{ if(seen[t.toLowerCase()]) return false; seen[t.toLowerCase()]=true; return true; }});
    var aliases = allTerms.filter(function(t){{ return t.toLowerCase() !== q; }});
    return {{aliases: aliases}};
  }}
  return {{aliases:[]}};
}}

window._watneyDrugs = DRUGS;
window._watneyAliasMap = ALIAS_MAP;
window._watneyResolveQuery = resolveQuery;
}})();
</script>''', shared=True)

# ── Global Cmd/Ctrl-F handler — registered ONCE at page load, never duplicated ──
# Each render_patient call re-injects the search bar JS which would accumulate
# duplicate keydown listeners if Cmd-F were handled there. By putting it here
# (shared=True, runs once) we guarantee exactly one handler exists.
ui.add_head_html('''<script>
(function(){
  if(window._watneyGlobalKeyDone) return;  // guard: only register once per page load
  window._watneyGlobalKeyDone = true;
  document.addEventListener('keydown', function(e){
    if((e.ctrlKey || e.metaKey) && e.key === 'f'){
      if(window._watneyCmdfEnabled !== false){
        e.preventDefault();
        e.stopPropagation();
        if(window.WS) window.WS.open();
      }
    }
  }, true);  // capture phase — fires before NiceGUI and before any other handler
})();
</script>''', shared=True)


@ui.page('/')
def build_page():
    """NiceGUI page factory for the main WATNEY interface.

    Called once per browser connection. Builds the full page DOM including
    the login overlay, left/right panel containers, nav bar, and all nested
    closures that share state across patient navigation.
    """
    global current_patient_index, agent_output, NOTE_FONT_SIZE
    global CURRENT_USER, UI_LOCKED, user_label, nav_bar, df
    global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR, CONFIG_PATH
    global conn, cursor, annotations_df, progression_sort_order

    current_patient_index  = 0
    agent_output           = None
    progression_sort_order = 'Ascending'
    _refresh_summary_holder = [None]

    # Track open dialogs for toggle-close behavior
    _open_dialogs = {'patient_list': None, 'settings': None}

    _active_clients['count'] += 1
    import time as _time
    _active_clients['timestamps'].append(_time.time())

    from nicegui import app as _ngapp
    @_ngapp.on_disconnect
    def _on_disconnect():
        _active_clients['count'] = max(0, _active_clients['count'] - 1)

    # ── CSS ───────────────────────────────────────────────────────────────────
    ui.add_head_html(f"""<style>
body{{font-family:Arial;}}
.left-pane{{height:94vh;overflow-y:auto;padding-right:8px;padding-bottom:60px;box-sizing:border-box;}}
.right-pane{{height:94vh;overflow-y:auto;padding-left:8px;box-sizing:border-box;min-width:0;}}
#panel-divider{{width:4px;flex-shrink:0;cursor:col-resize;
    align-self:stretch;background:#e2e8f0;transition:background 0.15s;z-index:10;}}
#panel-divider:hover,#panel-divider.dragging{{background:#93c5fd;}}
.note-card{{border:1px solid #1e3a5f;border-radius:5px;padding:0;margin-bottom:8px;background:#fafafa;overflow:hidden;}}
.note-meta{{display:flex;gap:10px;font-size:10px;font-weight:600;color:#e0eaff;margin-bottom:4px;padding:3px 6px 4px;border-radius:3px 3px 0 0;background:#1e3a5f;letter-spacing:0.02em;}}
pre{{white-space:pre-wrap;font-size:{NOTE_FONT_SIZE}px;line-height:1.4;margin:0;padding:6px 10px;word-wrap:break-word;overflow-wrap:break-word;}}
.evidence-highlight{{background-color:#ffe066;padding:2px 3px;border-radius:2px;font-weight:bold;}}
.agent-box,.annotation-box{{border:1px solid #ddd;padding:8px;border-radius:5px;margin-bottom:10px;}}
.bottom-nav{{position:fixed;bottom:15px;left:15px;right:0;width:calc(37% - 15px);display:flex;
    align-items:center;justify-content:space-between;gap:6px;z-index:5000;
    background:rgba(255,255,255,0.95);padding:5px 12px;border-radius:6px;
    box-shadow:0 2px 8px rgba(0,0,0,0.12);}}
/* Push Quasar toast notifications above the nav bar */
.q-notifications__list--bottom{{padding-bottom:70px!important;}}
.patient-list-row:hover{{background:#f0f4ff;cursor:pointer;}}
.hl{{background:#fde68a;border-radius:2px;}}
.hl.cur{{background:#f97316;color:#fff;}}
/* Search strip */
#ws-strip{{position:fixed;bottom:65px;left:15px;width:calc(37% - 15px);
    z-index:10001;display:none;flex-direction:column;gap:3px;}}
#ws-alias{{background:linear-gradient(135deg,#eff6ff,#dbeafe);
    border:1px solid #bfdbfe;border-radius:8px;padding:5px 10px;
    display:none;align-items:center;gap:5px;flex-wrap:wrap;
    box-shadow:0 2px 8px rgba(59,130,246,0.10);}}
#ws-bar{{background:#fff;border:1.5px solid #cbd5e1;border-radius:8px;
    padding:5px 8px;display:flex;align-items:center;gap:7px;
    box-shadow:0 4px 16px rgba(0,0,0,0.10);}}
#ws-input{{flex:1;border:1.5px solid #e2e8f0;border-radius:6px;
    padding:5px 10px;font-size:13px;font-family:Arial,sans-serif;
    outline:none;background:#f8fafc;color:#1e293b;transition:border-color 0.15s;}}
#ws-input:focus{{border-color:#3b82f6;background:#fff;}}
#ws-count{{font-size:11px;color:#94a3b8;white-space:nowrap;min-width:42px;text-align:right;}}
.ws-btn{{border:1.5px solid #e2e8f0;border-radius:6px;background:#f8fafc;
    color:#64748b;font-size:12px;padding:4px 8px;cursor:pointer;line-height:1;font-family:Arial;}}
.ws-btn:hover{{background:#f1f5f9;border-color:#cbd5e1;}}
/* Sticky note header — fixed to right of left panel, tracks right pane */
#note-sticky-header{{
    position:fixed;top:0;z-index:200;
    background:#1e3a5f;
    border-bottom:2px solid #1e40af;border-radius:0 0 6px 6px;
    padding:4px 14px;font-size:10px;font-weight:600;color:#e0eaff;
    display:none;flex-wrap:wrap;gap:8px;align-items:center;
    box-shadow:0 3px 10px rgba(0,0,0,0.25);
    pointer-events:none;letter-spacing:0.02em;
}}
/* Search grayed-out state when source highlight is active */
#ws-strip.ws-grayed #ws-bar{{
    opacity:0.72;cursor:pointer;
    background:#f1f5f9;
    border-color:#94a3b8;
}}
#ws-strip.ws-grayed{{cursor:pointer;}}
#ws-strip.ws-grayed #ws-input{{pointer-events:none;color:#94a3b8;}}
#ws-strip.ws-grayed::before{{
    content:'click to resume search';
    display:block;font-size:10px;color:#475569;text-align:center;
    padding:2px 8px;background:#e2e8f0;border-radius:4px;
    margin-bottom:2px;letter-spacing:0.03em;
}}
#ws-bar{{position:relative;}}
/* Extraction viewer */
.ext-section{{border-radius:6px;margin-bottom:4px;overflow:visible;}}
.ext-key{{font-size:12px;font-weight:600;color:#374151;min-width:130px;flex-shrink:0;line-height:1.4;}}
.ext-val{{font-size:12px;color:#111827;word-break:break-word;line-height:1.4;}}
.ext-null{{font-size:12px;color:#9ca3af;font-style:italic;}}
.ext-rationale{{font-size:11px;color:#6b7280;font-style:italic;word-break:break-word;line-height:1.3;}}
.ext-list-item{{border:1px solid #e5e7eb;border-radius:4px;padding:4px 8px;margin-bottom:3px;}}
.ext-badge{{font-size:9px;color:#9ca3af;margin-bottom:2px;}}
/* Login animations */
@keyframes watney-float{{0%,100%{{transform:translateY(0px);}}50%{{transform:translateY(-8px);}}}}
@keyframes watney-pulse-ring{{0%{{transform:scale(0.85);opacity:0.7;}}70%{{transform:scale(1.1);opacity:0;}}100%{{transform:scale(0.85);opacity:0;}}}}
@keyframes watney-fade-in{{from{{opacity:0;transform:translateY(16px);}}to{{opacity:1;transform:translateY(0);}}}}
@keyframes watney-spin-slow{{from{{transform:rotate(0deg);}}to{{transform:rotate(360deg);}}}}
@keyframes watney-spin-rev{{from{{transform:rotate(0deg);}}to{{transform:rotate(-360deg);}}}}
@keyframes watney-update-pulse{{0%,100%{{opacity:1;}}50%{{opacity:0.5;}}}}
@keyframes watney-glow{{0%,100%{{box-shadow:0 8px 32px rgba(99,102,241,0.35);}}50%{{box-shadow:0 12px 48px rgba(99,102,241,0.65);}}}}
@keyframes watney-shimmer{{0%{{background-position:-200% center;}}100%{{background-position:200% center;}}}}
@keyframes watney-orbit{{from{{transform:rotate(0deg) translateX(52px) rotate(0deg);}}to{{transform:rotate(360deg) translateX(52px) rotate(-360deg);}}}}
@keyframes watney-orbit2{{from{{transform:rotate(180deg) translateX(68px) rotate(-180deg);}}to{{transform:rotate(540deg) translateX(68px) rotate(-540deg);}}}}
@keyframes watney-bg-shift{{0%,100%{{background-position:0% 50%;}}50%{{background-position:100% 50%;}}}}
.watney-update-badge{{animation:watney-update-pulse 2s infinite;}}
/* Mars / easter egg */
@keyframes mars-surface-drift{{0%,100%{{opacity:0.5;}}50%{{opacity:0.7;}}}}
@keyframes star-twinkle{{0%,100%{{opacity:0.2;}}50%{{opacity:1;}}}}
/* Tutorial interactive highlight */
@keyframes tut-highlight-pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(59,130,246,0.4);}}50%{{box-shadow:0 0 0 8px rgba(59,130,246,0);}}}}
.tut-highlight{{animation:tut-highlight-pulse 1.5s infinite;outline:2px solid #3b82f6;outline-offset:2px;border-radius:4px;}}
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
    dragging = true; startX = e.clientX;
    startW = leftPane.getBoundingClientRect().width;
    divider.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var rowW = leftPane.parentElement
               ? leftPane.parentElement.getBoundingClientRect().width
               : window.innerWidth;
    var newW = Math.min(Math.max(startW + (e.clientX - startX), 300), rowW - 200);
    leftPane.style.width = newW + 'px';
    leftPane.style.minWidth = newW + 'px';
    leftPane.style.flexShrink = '0';
    leftPane.style.flexGrow = '0';
    var nav = document.querySelector('.bottom-nav');
    if (nav) nav.style.width = (newW - 15) + 'px';
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove('dragging');
    document.body.style.cursor = '';
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

    # ── LOGIN OVERLAY ─────────────────────────────────────────────────────────
    lock_overlay = ui.column().classes(
        'fixed inset-0 flex items-center justify-center z-[9999]'
    ).style('background-color:white;pointer-events:all;overflow:hidden;')


    with lock_overlay:
        # ── Animated login background gradient ───────────────────────────────
        ui.html('''<div style="position:absolute;inset:0;z-index:0;pointer-events:none;
            background:linear-gradient(135deg,#f0f9ff,#eff6ff,#faf5ff,#eff6ff,#f0f9ff);
            background-size:400% 400%;animation:watney-bg-shift 12s ease infinite;"></div>
        <div style="position:absolute;inset:0;z-index:0;pointer-events:none;overflow:hidden;">
          <!-- Orbiting ring 1 -->
          <div style="position:absolute;top:50%;left:50%;width:420px;height:420px;
              margin:-210px 0 0 -210px;border-radius:50%;
              border:1px solid rgba(99,102,241,0.12);"></div>
          <div style="position:absolute;top:50%;left:50%;width:580px;height:580px;
              margin:-290px 0 0 -290px;border-radius:50%;
              border:1px solid rgba(59,130,246,0.08);"></div>
        </div>''')

        login_content = ui.column().classes('items-center gap-3 relative').style('z-index:1;animation:watney-fade-in 0.7s ease;')
        with login_content:
            # Logo with layered animation
            ui.html(f'''
<div style="text-align:center;margin-bottom:4px;">
  <div style="position:relative;display:inline-block;width:80px;height:80px;margin-bottom:10px;">
    <!-- Outer glow ring -->
    <div style="position:absolute;inset:-8px;border-radius:50%;
        background:radial-gradient(circle,rgba(99,102,241,0.18) 0%,transparent 70%);
        animation:watney-glow 3s ease-in-out infinite;"></div>
    <!-- Spinning outer ring -->
    <div style="position:absolute;inset:-4px;border-radius:50%;
        border:1.5px dashed rgba(99,102,241,0.3);
        animation:watney-spin-slow 14s linear infinite;"></div>
    <!-- Spinning inner ring (opposite direction) -->
    <div style="position:absolute;inset:4px;border-radius:50%;
        border:1px solid rgba(59,130,246,0.25);
        animation:watney-spin-rev 9s linear infinite;"></div>
    <!-- Main circle -->
    <div style="width:80px;height:80px;border-radius:50%;
        background:linear-gradient(135deg,#3b82f6 0%,#6366f1 60%,#8b5cf6 100%);
        display:flex;align-items:center;justify-content:center;
        animation:watney-float 3.5s ease-in-out infinite;
        box-shadow:0 8px 32px rgba(99,102,241,0.4);">
      <svg width="42" height="42" viewBox="0 0 42 42" fill="none">
        <!-- Document body -->
        <rect x="9" y="5" width="24" height="32" rx="3" stroke="white" stroke-width="2.5" fill="none"/>
        <!-- Folded corner -->
        <polyline points="27,5 33,11" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
        <polyline points="27,5 27,11 33,11" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        <!-- Lines of text -->
        <line x1="14" y1="18" x2="28" y2="18" stroke="white" stroke-width="2" stroke-linecap="round"/>
        <line x1="14" y1="23" x2="28" y2="23" stroke="white" stroke-width="2" stroke-linecap="round"/>
        <line x1="14" y1="28" x2="22" y2="28" stroke="white" stroke-width="2" stroke-linecap="round"/>
      </svg>
    </div>
  </div>
  <!-- Shimmer title -->
  <div style="font-size:2.8rem;font-weight:900;letter-spacing:0.14em;
      background:linear-gradient(90deg,#1d4ed8,#7c3aed,#2563eb,#7c3aed,#1d4ed8);
      background-size:200% auto;
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
      animation:watney-shimmer 4s linear infinite;">
    WATNEY {_major_version(WATNEY_VERSION)}
  </div>
  <div style="font-size:0.7rem;color:#94a3b8;font-weight:600;letter-spacing:0.18em;
      text-transform:uppercase;margin-top:2px;">
    Oncology Annotation Platform
  </div>
</div>
''')

            # Multi-instance warning — shows on ALL instances including the newest
            if _active_clients['count'] > 1:
                ui.html(f'''<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;
                    padding:7px 16px;font-size:11px;font-weight:600;color:#c2410c;text-align:center;
                    max-width:340px;animation:watney-fade-in 0.4s ease;
                    box-shadow:0 2px 8px rgba(194,65,12,0.12);">
                  ⚠ {_active_clients["count"]} browser tabs are open.<br>
                  <span style="font-weight:400;font-size:10px;">Multiple instances may cause unexpected behaviour.</span>
                </div>''')

            # Update badge
            _update_notice_holder = [None]
            with ui.element('div'):
                _update_notice_holder[0] = ui.label('').classes('text-xs text-orange-600 font-semibold text-center watney-update-badge')
                _update_notice_holder[0].set_visibility(False)

            ui.html('<div style="width:260px;height:1px;background:linear-gradient(90deg,transparent,#c7d2fe,transparent);margin:4px 0;"></div>')

            # ── Collect recent projects once at render time ───────────────────
            _recents = get_recent_projects()

            step1 = ui.column().classes('items-center gap-2 w-80')
            step2 = ui.column().classes('items-center gap-2 w-80')  # new project form
            step2.set_visibility(False)

            with step1:
                ui.label('Enter Name to Begin').classes('text-base font-semibold text-center text-gray-600')
                name_error = ui.label('').classes('text-xs text-red-500 text-center')
                user_select = ui.input(
                    placeholder='Your name'
                ).classes('w-full text-center').props('outlined dense')

                ui.html('<div style="width:100%;height:1px;background:#e2e8f0;margin:4px 0;"></div>')
                ui.label('Project').classes('text-xs font-semibold text-gray-500 self-start')

                proj_error = ui.label('').classes('text-xs text-red-500 text-center')

                # Recent projects — plain name list; path looked up by index
                # NiceGUI dict-options select is unreliable across versions;
                # plain list + index lookup is the most compatible approach.
                _recent_select = None
                _path_display  = None
                if _recents:
                    _recent_names = [r['name'] for r in _recents]
                    _name_display = ui.label(_recents[0]['name']).classes(
                        'text-sm font-semibold text-indigo-700 w-full'
                    )
                    _recent_select = ui.select(
                        options=_recent_names,
                        value=_recent_names[0],
                        label='Recent projects'
                    ).classes('w-full').props('outlined dense')
                    _path_display = ui.label(_recents[0]['path']).classes(
                        'text-xs text-gray-400 w-full break-all'
                    ).style('font-family:monospace;line-height:1.3;padding:2px 0 4px;')

                proj_path_input = ui.input(
                    label='Project folder path' if not _recents else 'Or enter a different path',
                    placeholder='/path/to/MyProject'
                ).classes('w-full').props('outlined dense')

                if _recent_select and _path_display:
                    def _on_recent_change(e):
                        val = _recent_select.value
                        if val and val in _recent_names:
                            idx = _recent_names.index(val)
                            path = _recents[idx]['path']
                            proj_path_input.set_value(path)
                            _path_display.set_text(path)
                            _name_display.set_text(val)
                    _recent_select.on('update:model-value', _on_recent_change)
                    proj_path_input.set_value(_recents[0]['path'])

                def _try_open_project(pdir: Path | None = None):
                    global CURRENT_PROJECT, PROJECT_DIR
                    raw = str(pdir) if pdir is not None else proj_path_input.value.strip()
                    if not raw:
                        proj_error.set_text('Enter a project folder path.'); return
                    proj_dir = Path(raw)
                    proj_error.set_text('')
                    try:
                        meta, proj_df, proj_conn = open_project(proj_dir)
                        _apply_opened_project(meta, proj_df, proj_conn, proj_dir)
                        _finish_login()
                    except ProjectError as e:
                        err_str = str(e)
                        if err_str.startswith('LEGACY_FOLDER:'):
                            proj_error.set_text(
                                'Legacy folder detected. Use "Migrate Legacy Data" in Settings, '
                                'or create a new project.'
                            )
                        else:
                            proj_error.set_text(str(e))
                    except Exception as ex:
                        proj_error.set_text(f'Error: {ex}')

                def after_username():
                    global CURRENT_USER
                    name = user_select.value.strip()
                    if not name:
                        name_error.set_text('Please enter your name to continue.')
                        user_select.run_method('focus')
                        return
                    if name.lower() == 'mark watney':
                        CURRENT_USER = name
                        ui.navigate.to('/easter-egg')
                        return
                    name_error.set_text('')
                    CURRENT_USER = name
                    if proj_path_input.value.strip():
                        _try_open_project()
                    else:
                        proj_error.set_text('Select a recent project or enter a folder path.')

                # Enter on name field or path field both trigger the full flow
                user_select.on('keydown.enter', after_username)
                proj_path_input.on('keydown.enter', after_username)

                ui.html('<div style="width:100%;height:1px;background:#e2e8f0;margin:4px 0;"></div>')

                # Open Project — primary action, full width
                ui.button('Open Project', on_click=after_username).classes('w-full').props('color=primary dense')
                # New Project — secondary, understated
                def _go_new_project():
                    if not user_select.value.strip():
                        name_error.set_text('Please enter your name first.')
                        user_select.run_method('focus')
                        return
                    proj_error.set_text('')
                    step1.set_visibility(False)
                    step2.set_visibility(True)
                ui.button('+ New Project', on_click=_go_new_project).props('flat dense').classes('w-full text-xs text-gray-400 mt-0')

            # ── Step 2: New project CSV path ──────────────────────────────────
            with step2:
                ui.label('New Project').classes('text-base font-semibold text-center')
                ui.label('Name your project, choose a folder, and load the extraction CSV.') \
                    .classes('text-xs text-gray-500 text-center')

                new_proj_error = ui.label('').classes('text-xs text-red-500 text-center')

                new_proj_name_input = ui.input(
                    label='Project name', placeholder='NSCLC Cohort 2024'
                ).classes('w-full').props('outlined dense')
                new_proj_dir_input = ui.input(
                    label='Project folder (will be created)',
                    placeholder='/path/to/MyNewProject'
                ).classes('w-full').props('outlined dense')
                csv_input = ui.input(
                    label='Extraction CSV path', placeholder='/path/to/extraction.csv'
                ).classes('w-full').props('outlined dense')
                csv_error = ui.label('').classes('text-xs text-red-500')

                def create_project_and_login():
                    global CURRENT_PROJECT, PROJECT_DIR, CURRENT_USER
                    # Username must be set (user may have jumped to step2 via back/forward)
                    name = user_select.value.strip()
                    if not name:
                        new_proj_error.set_text('Return to step 1 and enter your name.')
                        return
                    n = new_proj_name_input.value.strip()
                    d = new_proj_dir_input.value.strip()
                    c = csv_input.value.strip()
                    if not n: new_proj_error.set_text('Enter a project name.'); return
                    if not d: new_proj_error.set_text('Enter a folder path.'); return
                    if not c: new_proj_error.set_text('Enter the CSV path.'); return
                    new_proj_error.set_text('')
                    csv_error.set_text('')
                    CURRENT_USER = name
                    try:
                        proj_dir = Path(d)
                        csv_p = Path(c)
                        meta = create_project(proj_dir, n, csv_p)
                        proj_conn = open_annotations_db(proj_dir)
                        proj_df = load_dataframe(str(proj_dir / meta['active_extraction']))
                        _apply_opened_project(meta, proj_df, proj_conn, proj_dir)
                        _finish_login()
                    except ProjectError as e:
                        new_proj_error.set_text(str(e))
                    except Exception as ex:
                        new_proj_error.set_text(f'Error: {ex}')

                with ui.row().classes('w-full gap-2 mt-1'):
                    ui.button('← Back', on_click=lambda: (
                        step2.set_visibility(False), step1.set_visibility(True)
                    )).props('flat dense')
                    ui.button('Create & Enter', on_click=create_project_and_login) \
                        .classes('flex-grow').props('color=primary dense')

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
            # ── Start checkpoint timer (real projects only) ───────────────────
            if not demo and PROJECT_DIR is not None:
                _interval_min = CURRENT_PROJECT.get('checkpoint_interval_minutes', 30)
                _interval_sec = max(60, _interval_min * 60)
                def _do_checkpoint():
                    if _demo_mode[0] or PROJECT_DIR is None: return
                    try:
                        chk = do_checkpoint(PROJECT_DIR, conn)
                        ui.notify(f'Checkpoint saved: {chk.name}', color='grey', timeout=2000)
                    except Exception as _ce:
                        pass  # silent failure — don't interrupt annotation
                _checkpoint_timer_holder[0] = ui.timer(_interval_sec, _do_checkpoint)

        def require_user():
            if not CURRENT_USER: ui.notify('Set username first', color='red'); return False
            return True

        _checkpoint_timer_holder = [None]  # holds the checkpoint ui.timer instance

        # ── Easter egg auto-login (from ?ee_user= URL param) ─────────────────
        # JS reads the URL param and sets a global; Python polls it once.
        ui.add_body_html('''<script>
(function(){
  var p = new URLSearchParams(window.location.search);
  var u = p.get('ee_user');
  if(u) window._eeAutoUser = decodeURIComponent(u.replace(/\\+/g,' '));
})();
</script>''')

        async def _check_ee_auto_login():
            val = await ui.run_javascript('window._eeAutoUser || ""')
            if val and val.strip():
                global CURRENT_USER
                CURRENT_USER = val.strip()
                await ui.run_javascript('window._eeAutoUser = "";'
                                        'history.replaceState(null,"","/");')
                if EXTRACTION_CSV_PATH and Path(EXTRACTION_CSV_PATH).exists():
                    _finish_login()
                else:
                    user_select.set_value(CURRENT_USER)
                    step1.set_visibility(False)
                    step2.set_visibility(True)  # show new project form

        ui.timer(0.3, _check_ee_auto_login, once=True)



    # ── Check for updates on page load ────────────────────────────────────────
    async def _check_update_on_load():
        import asyncio, re as _re
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
                    _UPDATE_AVAILABLE[0] = True
                    _LATEST_VERSION[0] = latest
                    if _update_notice_holder[0]:
                        _update_notice_holder[0].set_text(f'🔔 Update available: v{latest}')
                        _update_notice_holder[0].set_visibility(True)
        except Exception:
            pass

    ui.timer(1.0, _check_update_on_load, once=True)

    # ── Tutorial ──────────────────────────────────────────────────────────────
    _tut_fn_holder = [None]

    def _do_launch_tutorial():
        steps = [
            ('Welcome to WATNEY 6',
             'WATNEY helps you review and annotate LLM-extracted oncology progression events from clinical notes. '
             'This tutorial is interactive — some steps will highlight the actual UI element being described. '
             'Use the buttons or dot-navigation below to move through steps.',
             '🏥', None),
            ('Log In & Open Project',
             'Enter your name, then open an existing project folder or create a new one. '
             'WATNEY remembers your last project and jumps straight in on the next login. '
             'Each project keeps its own database, extraction versions, and checkpoints.',
             '👤', None),
            ('The Layout',
             'The left panel is your annotation workspace. The right panel shows full clinical notes. '
             'Drag the thin divider bar between panels left or right to resize them.',
             '⟺', 'panel-divider'),
            ('Navigate Patients',
             'Use Prev / Next in the nav bar, or press ← → on your keyboard, to move between patients. '
             '"Patients" shows all patients and their annotation status — click Go to jump to any of them.',
             '⟵⟶', None),
            ('Search Notes (Cmd/Ctrl-F)',
             'Press Cmd-F (Mac) or Ctrl-F (Windows) anywhere on the page, or click the Search button in the nav bar. '
             'The search bar includes NLP drug-alias expansion — searching "temodar" also finds "tmz" and "temozolomide". '
             'Click the NLP button to toggle aliases. Click the Search button again to close the bar.',
             '🔍', None),
            ('Progression Summary',
             'The Progression Summary table at the top of the left panel shows every assigned event for the current patient: '
             'date, agent, start/end dates, source, annotator, and extraction version. '
             'It updates the moment you save or remove an assignment. '
             'Use the × button on any row to soft-delete that annotation — '
             'the record is preserved in the database for audit purposes but excluded from exports.',
             '📋', None),
            ('Agent Intervals Box',
             'Select an agent from the dropdown to see its treatment intervals extracted by the LLM. '
             'Each interval shows start and end date. Click "Start source" or "End source" to jump to the '
             'supporting note and highlight the evidence.',
             '📅', None),
            ('Progression Cards — Highlighted in Amber',
             'Each LLM-extracted progression event is a card. The amber-highlighted card matches the currently '
             'selected agent\'s treatment plan — it floats to the top. A notice above the cards names the matched agent.',
             '🟡', None),
            ('Assign an Agent',
             'Inside a progression card: select the agent, review auto-filled start/end dates, '
             'edit if needed (YYYYMMDD auto-formats on blur), then click Save Agent Assignment. '
             'Use "No End Date" to record NA. Click "Source" to jump to the supporting note.',
             '💾', None),
            ('Remove / Undo',
             '"Remove Agent" clears a specific card\'s assignment. '
             'The "Undo" button in the nav bar deletes the most recently modified annotation for the current patient. '
             'Both update the Progression Summary immediately.',
             '↩', None),
            ('Add a Clinician Progression Event',
             'Scroll to the bottom of the left panel to manually add a progression event. '
             'Agent is required. Enter a Progression Date OR press NO PROGRESSION for patients with no progression. '
             'Start and end dates auto-fill from LLM data when you pick an agent.',
             '🩺', None),
            ('Exclude Patient',
             'Click "Exclude Patient" near the patient header to flag a patient for exclusion. '
             'A dialog will ask for a reason. Excluded patients show a red banner and can be un-excluded.',
             '🚫', None),
            ('View Extraction Data',
             'The "View Extraction Data" button at the bottom of the left panel shows the full LLM extraction '
             'in a structured, interactive viewer. Agents are promoted to a top-level section. '
             'Click any section to expand or collapse it.',
             '🔬', None),
            ('Export & Settings',
             'Export downloads all annotations as a timestamped CSV (also auto-saved to the project exports/ folder). '
             'Settings lets you adjust note font size, configure the Cmd/Ctrl-F search shortcut, '
             'rename the project, manage checkpoint intervals, load a new extraction CSV, '
             'import annotations from a prior export, check for updates, and view annotation stats. '
             'Both buttons toggle closed if clicked again while open.',
             '⚙️', None),
        ]

        step_idx   = [0]
        _hl_active = [None]   # currently highlighted element id

        def _clear_highlight():
            if _hl_active[0]:
                ui.run_javascript(f"""
                (function(){{
                    var el = document.getElementById('{_hl_active[0]}');
                    if(el) el.classList.remove('tut-highlight');
                }})();
                """)
                _hl_active[0] = None

        def _apply_highlight(el_id):
            _clear_highlight()
            if not el_id: return
            ui.run_javascript(f"""
            (function(){{
                var el = document.getElementById('{el_id}');
                if(el){{
                    el.classList.add('tut-highlight');
                    el.scrollIntoView({{behavior:'smooth',block:'nearest'}});
                }}
            }})();
            """)
            _hl_active[0] = el_id

        with ui.dialog() as tut_dlg, ui.card().classes('w-[560px]').style('padding:20px;'):
            with ui.row().classes('w-full items-center justify-between mb-1'):
                ui.label('WATNEY Tutorial').classes('text-lg font-bold')
                progress_label = ui.label('').classes('text-xs text-gray-400')

            ui.separator()

            icon_label  = ui.label('').classes('text-5xl text-center w-full mt-4')
            title_label = ui.label('').classes('text-base font-bold text-blue-700 text-center w-full mt-2')
            body_label  = ui.label('').classes('text-sm text-gray-700 text-center w-full mt-3 leading-relaxed px-2')

            # Interactive callout
            callout = ui.label('').classes(
                'text-xs text-blue-600 font-semibold text-center w-full mt-2 '
                'bg-blue-50 rounded px-3 py-1'
            )
            callout.set_visibility(False)

            ui.element('div').style('height:16px;')

            with ui.row().classes('w-full justify-between items-center mt-2'):
                prev_btn = ui.button('← Back').props('flat dense')
                dot_row  = ui.row().classes('gap-1 items-center')
                next_btn = ui.button('Next →').props('dense color=primary')

            dots = []
            with dot_row:
                for i in range(len(steps)):
                    d = ui.element('div').style(
                        'width:8px;height:8px;border-radius:50%;'
                        'background:#cbd5e1;cursor:pointer;transition:background 0.2s;'
                    )
                    dots.append(d)

            def render_step(i):
                _, title, body, el_id = steps[i][0], steps[i][0], steps[i][1], steps[i][3]
                icon_label.set_text(steps[i][2])
                title_label.set_text(steps[i][0])
                body_label.set_text(body)
                progress_label.set_text(f'{i+1} / {len(steps)}')
                prev_btn.set_visibility(i > 0)
                next_btn.set_text('Finish' if i == len(steps)-1 else 'Next →')
                for j, d in enumerate(dots):
                    d.style(
                        'width:8px;height:8px;border-radius:50%;cursor:pointer;transition:background 0.2s;'
                        + ('background:#3b82f6;' if j == i else 'background:#cbd5e1;')
                    )
                if el_id:
                    callout.set_text(f'👆 The highlighted element is shown in the main UI')
                    callout.set_visibility(True)
                    _apply_highlight(el_id)
                else:
                    callout.set_visibility(False)
                    _clear_highlight()

            def go_prev():
                step_idx[0] = max(0, step_idx[0] - 1)
                render_step(step_idx[0])

            def go_next():
                if step_idx[0] == len(steps) - 1:
                    _clear_highlight()
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
            def _load_demo_df():
                """Load demo CSV; return df or None."""
                global df, EXTRACTION_CSV_PATH
                demo_path = Path(__file__).parent / 'watney_demo_data.csv'
                if not demo_path.exists():
                    ui.notify(f'Demo file not found: {demo_path}', color='red'); return None
                try:
                    demo_df = load_dataframe(str(demo_path))
                    df = demo_df
                    EXTRACTION_CSV_PATH = str(demo_path)
                    return demo_df
                except Exception as e:
                    ui.notify(f'Could not load demo data: {e}', color='red'); return None

            def launch_demo():
                global CURRENT_USER
                if _load_demo_df() is None: return
                CURRENT_USER = user_select.value.strip() or 'Demo User'
                _login_bottom.set_visibility(False)
                _finish_login(demo=True)

            def launch_demo_with_tutorial():
                global CURRENT_USER
                if _load_demo_df() is None: return
                CURRENT_USER = user_select.value.strip() or 'Demo User'
                _login_bottom.set_visibility(False)
                _finish_login(demo=True)
                ui.timer(0.8, lambda: _tut_fn_holder[0]() if _tut_fn_holder[0] else None, once=True)

            ui.button('Try Demo', on_click=launch_demo).props('outline').classes('text-gray-500 text-sm')
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
        with right_panel:
            ui.html('<div id="note-sticky-header"></div>')
            ui.html(notes_html).classes('w-full')
            _inject_sticky_header_js()
        # Gray out the search bar since source highlight is now active
        ui.run_javascript("""
            if(window._watneyGraySearch) window._watneyGraySearch();
        """)
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

            with ui.row().classes('w-full gap-2 mt-1 items-center'):
                start_input = ui.input(label='Start date', placeholder='YYYY-MM-DD').classes('flex-grow')
                end_input   = ui.input(label='End date',   placeholder='YYYY-MM-DD').classes('flex-grow')
                ui.button('No End Date',
                    on_click=lambda ei=end_input: ei.set_value('NA')
                ).props('dense outline').classes('text-gray-500')

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
        # Toggle close if already open
        if _open_dialogs['patient_list'] is not None:
            try:
                _open_dialogs['patient_list'].close()
            except Exception:
                pass
            _open_dialogs['patient_list'] = None
            return

        if _demo_mode[0]:
            _pa = pd.read_sql_query(
                "SELECT DISTINCT DFCI_MRN FROM annotations WHERE (deleted IS NULL OR deleted = 0) AND progression_source != 'exclusion_placeholder'",
                _db()
            )
            annotated_mrns = set(_pa['DFCI_MRN'].apply(safe_str).tolist())
        else:
            refresh_annotations_df()
            # annotations_df already excludes soft-deleted rows
            annotated_mrns = set(
                annotations_df[annotations_df['progression_source'] != 'exclusion_placeholder']['DFCI_MRN'].apply(safe_str).tolist()
            )

        # Read exclusion status — patients.db in project mode, annotations in demo/legacy
        try:
            if not _demo_mode[0] and PROJECT_DIR is not None:
                _pc = open_patients_db(PROJECT_DIR)
                _excl_df = pd.read_sql_query(
                    "SELECT DFCI_MRN, exclusion_flag FROM patients WHERE exclusion_flag IS NOT NULL",
                    _pc
                )
                _pc.close()
            else:
                _excl_df = pd.read_sql_query(
                    "SELECT DFCI_MRN, exclusion_flag FROM annotations WHERE exclusion_flag IS NOT NULL GROUP BY DFCI_MRN",
                    _db()
                )
            excluded_mrns = set(
                _excl_df.loc[_excl_df['exclusion_flag'] == 'True', 'DFCI_MRN'].apply(safe_str).tolist()
            )
        except Exception:
            excluded_mrns = set()

        _all_rows = []
        for i, row in df.iterrows():
            pid = normalize_patient_id(row[PATIENT_ID_COL])
            _all_rows.append({'idx': i, 'pid': pid,
                              'has': safe_str(pid) in annotated_mrns,
                              'excl': safe_str(pid) in excluded_mrns})

        with ui.dialog() as dlg, ui.card().classes('w-[640px] max-h-[88vh] flex flex-col gap-0'):
            _open_dialogs['patient_list'] = dlg
            ui.label('Patient List').classes('text-lg font-bold mb-1')
            ui.label(f'{len(df)} patients · {len(annotated_mrns)} annotated · {len(excluded_mrns)} excluded').classes('text-xs text-gray-500 mb-1')
            search_input = ui.input(placeholder='Search MRN…').classes('w-full mb-2').props('outlined dense clearable')

            with ui.row().classes('w-full text-xs font-bold border-b pb-1 mb-1'):
                ui.label('#').style('width:36px')
                ui.label('MRN').style('width:160px')
                ui.label('Annotated').style('width:80px')
                ui.label('Status').style('width:80px')

            list_container = ui.column().classes('w-full overflow-y-auto').style('max-height:60vh;')

            def _render_list(filter_str=''):
                list_container.clear()
                q = filter_str.strip().lower()
                with list_container:
                    shown = 0
                    for rd in _all_rows:
                        if q and q not in rd['pid'].lower():
                            continue
                        shown += 1
                        ann_color  = 'color:#16a34a;font-weight:600;' if rd['has']  else 'color:#dc2626;'
                        excl_color = 'color:#dc2626;font-weight:600;' if rd['excl'] else 'color:#16a34a;'
                        with ui.row().classes('w-full text-xs items-center patient-list-row rounded px-1'):
                            ui.label(str(rd['idx']+1)).style('width:36px;color:#999;')
                            ui.label(rd['pid']).style('width:160px;')
                            ui.label('Yes' if rd['has'] else 'No').style(f'width:80px;{ann_color}')
                            ui.label('Excluded' if rd['excl'] else 'Included').style(f'width:80px;{excl_color}')
                            def go(idx=rd['idx'], d=dlg):
                                global current_patient_index
                                _save_search_state()
                                current_patient_index = idx
                                _open_dialogs['patient_list'] = None
                                d.close()
                                render_patient(idx)
                            ui.button('Go', on_click=go).props('dense flat size=xs')
                    if shown == 0:
                        ui.label('No patients match.').classes('text-xs text-gray-400 px-2')

            _render_list()
            search_input.on('update:model-value', lambda _: _render_list(search_input.value or ''))
            ui.separator().classes('my-2')
            def _close_pl():
                _open_dialogs['patient_list'] = None
                dlg.close()
            ui.button('Close', on_click=_close_pl).props('dense')

        dlg.open()

    # ── Export ────────────────────────────────────────────────────────────────
    def export_csv():
        if _demo_mode[0]:
            df_out = pd.read_sql_query(
                """SELECT * FROM annotations
                   WHERE (deleted IS NULL OR deleted = 0)
                   AND progression_source != 'exclusion_placeholder'""",
                _db()
            )
        else:
            df_out = load_annotations_df()  # already filters soft-deleted rows
            if not df_out.empty and 'progression_source' in df_out.columns:
                df_out = df_out[df_out['progression_source'] != 'exclusion_placeholder'].copy()

        # ── Apply exclusion status from patients.db at export time ───────────
        if not _demo_mode[0] and PROJECT_DIR is not None:
            try:
                _pc = open_patients_db(PROJECT_DIR)
                _pat_df = pd.read_sql_query("SELECT * FROM patients", _pc)
                _pc.close()
                if not _pat_df.empty and not df_out.empty:
                    # Merge exclusion columns onto annotation rows
                    _excl_cols = [c for c in ['exclusion_flag','exclusion_reason',
                                               'excluded_by','excluded_at',
                                               'unexclusion_reason','unexcluded_by','unexcluded_at']
                                  if c in _pat_df.columns]
                    _merge_cols = ['DFCI_MRN'] + _excl_cols
                    _pat_excl = _pat_df[_merge_cols].copy()
                    # Drop existing exclusion columns from df_out to avoid duplicates
                    for _c in _excl_cols:
                        if _c in df_out.columns:
                            df_out = df_out.drop(columns=[_c])
                    df_out = df_out.merge(_pat_excl, on='DFCI_MRN', how='left')

                # Add stub rows for excluded patients with NO annotation rows
                _excl_pat = _pat_df[_pat_df['exclusion_flag'] == 'True'] if not _pat_df.empty else pd.DataFrame()
                if not _excl_pat.empty:
                    _ann_mrns = set(df_out['DFCI_MRN'].apply(safe_str)) if not df_out.empty else set()
                    _stub_mrns = _excl_pat[~_excl_pat['DFCI_MRN'].apply(safe_str).isin(_ann_mrns)]
                    if not _stub_mrns.empty:
                        _stubs = _stub_mrns.copy()
                        _stubs['progression_source'] = 'excluded'
                        df_out = pd.concat([df_out, _stubs], ignore_index=True)
            except Exception:
                pass

        if df_out.empty:
            ui.notify('No annotations to export', color='orange'); return
        # Sort by MRN order matching the input CSV
        if df is not None and not df_out.empty:
            try:
                mrn_order = {str(normalize_patient_id(m)): i for i, m in enumerate(df[PATIENT_ID_COL])}
                df_out = df_out.copy()
                df_out['_sort_key'] = df_out['DFCI_MRN'].apply(
                    lambda x: mrn_order.get(safe_str(x), 999999)
                )
                df_out = df_out.sort_values('_sort_key').drop(columns=['_sort_key'])
            except Exception:
                pass
        import base64
        prefix = 'watney_demo_' if _demo_mode[0] else 'watney_annotations_'
        fn = f"{prefix}{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        # ── Save to project exports/ folder (real projects only) ─────────────
        saved_path_msg = fn
        if not _demo_mode[0] and PROJECT_DIR is not None:
            try:
                exp_dir = get_project_exports_dir(PROJECT_DIR)
                exp_path = exp_dir / fn
                df_out.to_csv(str(exp_path), index=False)
                saved_path_msg = str(exp_path)
            except Exception:
                pass
        # Also trigger browser download
        b64 = base64.b64encode(df_out.to_csv(index=False).encode()).decode()
        ui.run_javascript(f"""
            const a=document.createElement('a');
            a.href='data:text/csv;base64,{b64}';a.download='{fn}';
            document.body.appendChild(a);a.click();document.body.removeChild(a);
        """)
        ui.notify(f'Exported {len(df_out)} rows → {saved_path_msg}', color='green')


    # ── Reconciliation dialog (Phase 2.5) ────────────────────────────────────
    def _open_reconciliation_dialog(old_df, new_df):
        """Compare old and new extractions. Open a per-patient diff dialog."""
        import json as _rjson

        def _get_prog_dates(gen_str):
            try:
                g = _rjson.loads(gen_str or '{}') if isinstance(gen_str, str) else {}
                evts = g.get('progression', {}).get('progression_events', [])
                return [str(e.get('progression_date', '')).strip() for e in evts if e.get('progression_date')]
            except Exception:
                return []

        def _get_agents(gen_str):
            try:
                g = _rjson.loads(gen_str or '{}') if isinstance(gen_str, str) else {}
                agents = g.get('systemic_therapy', {}).get('agents', [])
                return [str(a.get('drug_name', '')).strip().lower() for a in agents if a.get('drug_name')]
            except Exception:
                return []

        # Build changed-patient list (generation differs)
        changed = []
        if old_df is not None and new_df is not None:
            old_map = {str(r[PATIENT_ID_COL]).strip(): str(r.get(GENERATION_COL, ''))
                       for _, r in old_df.iterrows()}
            for _, r in new_df.iterrows():
                mrn = str(r[PATIENT_ID_COL]).strip()
                new_gen = str(r.get(GENERATION_COL, ''))
                old_gen = old_map.get(mrn)
                if old_gen is not None and old_gen != new_gen:
                    changed.append({'mrn': mrn, 'old_gen': old_gen, 'new_gen': new_gen})

        # Load reconciliation tolerance from project settings
        _tol_default = CURRENT_PROJECT.get('reconciliation_date_tolerance_days', 7)

        with ui.dialog().props('maximized=false persistent=false') as recon_dlg,              ui.card().classes('w-[820px]').style('max-height:90vh;display:flex;flex-direction:column;overflow:hidden;padding:0;'):

            with ui.row().classes('w-full items-center justify-between').style('padding:12px 16px 8px;flex-shrink:0;'):
                ui.label(f'Reconciliation — {len(changed)} patients have extraction changes').classes('text-base font-bold')

            ui.separator().style('flex-shrink:0;')

            if not changed:
                with ui.column().style('padding:20px 16px;'):
                    ui.label('No extraction changes detected.').classes('text-sm text-green-600')
                with ui.row().style('padding:8px 16px;flex-shrink:0;'):
                    ui.button('Close', on_click=recon_dlg.close).props('dense')
                recon_dlg.open()
                return

            # ── Tolerance slider ──────────────────────────────────────────────
            with ui.row().classes('items-center gap-4').style('padding:8px 16px;flex-shrink:0;background:#f8fafc;border-bottom:1px solid #e2e8f0;'):
                ui.label('Date shift tolerance:').classes('text-xs font-semibold text-gray-600')
                _tol_slider = ui.slider(min=0, max=30, step=1, value=_tol_default).classes('w-40')
                _tol_lbl    = ui.label(f'{_tol_default} days').classes('text-xs text-gray-600 w-14')
                ui.label('Adjust to change what counts as "shifted" vs "changed"').classes('text-xs text-gray-400')

            _tol_slider.on('update:model-value', lambda _: _tol_lbl.set_text(f'{int(_tol_slider.value)} days'))

            # ── Scrollable table ─────────────────────────────────────────────
            with ui.row().classes('w-full text-xs font-bold border-b').style('padding:4px 16px;flex-shrink:0;background:#f1f5f9;'):
                ui.label('MRN').style('width:120px;flex-shrink:0')
                ui.label('Annotation Status').style('width:100px;flex-shrink:0')
                ui.label('Change Summary').style('flex:1;')
                ui.label('Status').style('width:110px;flex-shrink:0')
                ui.label('').style('width:40px;flex-shrink:0')

            table_col = ui.column().classes('w-full overflow-y-auto').style('flex:1;padding:0 8px;')

            _recon_rows_cache = [None]  # will hold computed rows for export

            def _build_recon_rows(tol):
                rows = []
                for entry in changed:
                    mrn = entry['mrn']
                    old_dates = _get_prog_dates(entry['old_gen'])
                    new_dates = _get_prog_dates(entry['new_gen'])
                    old_agents = set(_get_agents(entry['old_gen']))
                    new_agents = set(_get_agents(entry['new_gen']))

                    # Build change summary
                    parts = []
                    matched_new = set()
                    for od in old_dates:
                        best = None
                        for nd in new_dates:
                            if nd in matched_new: continue
                            if od == nd:
                                best = (nd, 0); break
                            if dates_within_days(od, nd, tol):
                                try:
                                    from datetime import date as _d
                                    delta = abs((_d.fromisoformat(nd[:10]) - _d.fromisoformat(od[:10])).days)
                                except Exception:
                                    delta = tol
                                if best is None or delta < best[1]:
                                    best = (nd, delta)
                        if best:
                            matched_new.add(best[0])
                            if best[1] == 0:
                                pass  # exact match, no change
                            else:
                                parts.append(f'shifted {od}→{best[0]} (+{best[1]}d)')
                        else:
                            # Find the closest new date even outside tolerance
                            closest = None
                            closest_delta = None
                            for nd in new_dates:
                                if nd in matched_new: continue
                                try:
                                    from datetime import date as _d
                                    delta = abs((_d.fromisoformat(nd[:10]) - _d.fromisoformat(od[:10])).days)
                                    if closest_delta is None or delta < closest_delta:
                                        closest, closest_delta = nd, delta
                                except Exception:
                                    pass
                            if closest:
                                parts.append(f'date changed {od}→{closest}')
                            else:
                                parts.append(f'removed: {od}')
                    for nd in new_dates:
                        if nd not in matched_new:
                            parts.append(f'new date: {nd}')
                    added_agents   = new_agents - old_agents
                    removed_agents = old_agents - new_agents
                    if added_agents:   parts.append(f'+agent(s): {", ".join(sorted(added_agents))}')
                    if removed_agents: parts.append(f'-agent(s): {", ".join(sorted(removed_agents))}')
                    change_summary = '; '.join(parts) if parts else 'content change (non-progression field)'

                    # Check annotation status
                    _ann_q = pd.read_sql_query(
                        """SELECT progression_date, agent FROM annotations
                           WHERE DFCI_MRN=? AND (deleted IS NULL OR deleted=0)
                           AND progression_source != 'exclusion_placeholder'""",
                        _db(), params=(mrn,)
                    )
                    ann_status = 'Annotated' if not _ann_q.empty else 'Not annotated'

                    # Conflict detection
                    conflict = 'Not annotated'
                    if not _ann_q.empty:
                        conflict = 'No conflict'
                        for _, ar in _ann_q.iterrows():
                            ann_date = str(ar.get('progression_date', '') or '').strip()
                            if not ann_date: continue
                            # Check if this annotated date still appears (exact or within tol) in new_dates
                            found = any(ann_date == nd or dates_within_days(ann_date, nd, tol) for nd in new_dates)
                            if not found:
                                conflict = 'Review needed'; break
                            # Also flag if date shifted at all (within tol = still present but moved)
                            if any(ann_date != nd and dates_within_days(ann_date, nd, tol) for nd in new_dates):
                                conflict = 'Review needed'; break

                    rows.append({
                        'mrn': mrn, 'ann_status': ann_status,
                        'change_summary': change_summary, 'conflict': conflict,
                    })
                return rows

            def _render_table(tol):
                table_col.clear()
                rows = _build_recon_rows(tol)
                _recon_rows_cache[0] = rows
                with table_col:
                    for rd in rows:
                        conflict = rd['conflict']
                        badge_color = '#f59e0b' if conflict == 'Review needed' else ('#16a34a' if conflict == 'No conflict' else '#9ca3af')
                        badge_bg    = '#fffbeb' if conflict == 'Review needed' else ('#f0fdf4' if conflict == 'No conflict' else '#f9fafb')
                        mrn_idx = next((i for i,r in df.iterrows() if normalize_patient_id(r[PATIENT_ID_COL]) == rd['mrn']), None)
                        with ui.row().classes('w-full text-xs items-start border-b py-1').style('padding:4px 8px;'):
                            ui.label(rd['mrn']).style('width:120px;flex-shrink:0;font-weight:600;')
                            ui.label(rd['ann_status']).style('width:100px;flex-shrink:0;color:#6b7280;')
                            ui.label(rd['change_summary']).style('flex:1;word-break:break-word;color:#374151;')
                            ui.label(conflict).style(
                                f'width:110px;flex-shrink:0;font-weight:600;'
                                f'color:{badge_color};background:{badge_bg};'
                                f'border-radius:4px;padding:1px 5px;font-size:9px;text-align:center;'
                            )
                            def _go_patient(idx=mrn_idx, d=recon_dlg):
                                if idx is None: return
                                global current_patient_index
                                current_patient_index = idx
                                d.close()
                                render_patient(idx)
                            ui.button('Go', on_click=_go_patient).props('dense flat size=xs').style('width:40px;flex-shrink:0;')

            # Initial render + live update on slider change
            _render_table(_tol_default)
            def _on_tol_change(_):
                tol = int(_tol_slider.value)
                # Persist to project
                if CURRENT_PROJECT:
                    CURRENT_PROJECT['reconciliation_date_tolerance_days'] = tol
                    try:
                        import json as _pj
                        with open(PROJECT_DIR / 'project.json', 'w') as _pf:
                            _pj.dump(CURRENT_PROJECT, _pf, indent=2)
                    except Exception:
                        pass
                _render_table(tol)
            _tol_slider.on('update:model-value', _on_tol_change)

            ui.separator().style('flex-shrink:0;')
            with ui.row().classes('gap-2').style('padding:8px 16px;flex-shrink:0;'):
                ui.button('Close', on_click=recon_dlg.close).props('dense')

                def _export_recon():
                    rows = _recon_rows_cache[0] or []
                    if not rows: return
                    import base64 as _b64
                    tol = int(_tol_slider.value)
                    for r in rows: r['tolerance_days_used'] = tol
                    _recon_df = pd.DataFrame(rows, columns=['mrn','ann_status','change_summary','conflict','tolerance_days_used'])
                    fn = f"watney_reconciliation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                    b64 = _b64.b64encode(_recon_df.to_csv(index=False).encode()).decode()
                    ui.run_javascript(f"""
                        const a=document.createElement('a');
                        a.href='data:text/csv;base64,{b64}';a.download='{fn}';
                        document.body.appendChild(a);a.click();document.body.removeChild(a);
                    """)
                    ui.notify(f'Reconciliation report exported: {fn}', color='green')

                ui.button('Export reconciliation report', on_click=_export_recon).props('dense outline')

        recon_dlg.open()

    # ── Import annotations dialog (Phase 3.1) ────────────────────────────────
    def _open_import_dialog():
        """Cross-project annotation import with fuzzy date tolerance."""
        with ui.dialog() as imp_dlg, ui.card().classes('w-[580px]').style('max-height:90vh;overflow-y:auto;'):
            ui.label('Import Annotations').classes('text-base font-bold')
            ui.label(
                'Import from a watney_annotations_*.csv export or another project\'s annotations.db. '
                'Existing annotations are never overwritten.'
            ).classes('text-xs text-gray-500 mb-2')

            _src_input = ui.input(
                label='Source file path (.csv or annotations.db)',
                placeholder='/path/to/watney_annotations_20260101.csv'
            ).classes('w-full').props('outlined dense')

            ui.label('Match tolerance').classes('text-xs font-semibold text-gray-600 mt-2')
            ui.label(
                'Annotations within N days of the same date and same agent are treated as duplicates and skipped.'
            ).classes('text-xs text-gray-400')
            with ui.row().classes('items-center gap-3 mt-1'):
                _imp_tol_slider = ui.slider(min=0, max=30, step=1, value=7).classes('w-40')
                _imp_tol_lbl = ui.label('7 days').classes('text-xs text-gray-600 w-14')
            _imp_tol_slider.on('update:model-value', lambda _: _imp_tol_lbl.set_text(f'{int(_imp_tol_slider.value)} days'))
            ui.label('Re-run Preview after changing tolerance.').classes('text-xs text-amber-600')

            _imp_status = ui.label('').classes('text-xs mt-2')
            _imp_preview_data = [None]  # cache computed rows for confirm

            with ui.row().classes('gap-2 mt-2'):
                _imp_btn = ui.button('Import', on_click=lambda: None).props('dense color=primary')
                _imp_btn.set_enabled(False)

                def _do_preview():
                    src = Path(_src_input.value.strip())
                    if not src.exists():
                        _imp_status.set_text(f'File not found: {src}')
                        _imp_status.classes('text-red-600', remove='text-green-600 text-gray-600'); return
                    tol = int(_imp_tol_slider.value)
                    try:
                        # Load source
                        if src.suffix.lower() == '.db':
                            _src_conn = sqlite3.connect(str(src), check_same_thread=False)
                            _src_conn.row_factory = sqlite3.Row
                            src_df = pd.read_sql_query(
                                "SELECT * FROM annotations WHERE (deleted IS NULL OR deleted = 0)",
                                _src_conn
                            )
                            _src_conn.close()
                        else:
                            src_df = pd.read_csv(str(src), dtype={'DFCI_MRN': str})
                        src_df = src_df[src_df.get('progression_source', pd.Series()) != 'exclusion_placeholder'] if 'progression_source' in src_df.columns else src_df

                        # Get project MRNs
                        _pat_conn = sqlite3.connect(str(PROJECT_DIR / 'patients.db'), check_same_thread=False)
                        proj_mrns = set(r[0] for r in _pat_conn.execute("SELECT DFCI_MRN FROM patients WHERE active=1").fetchall())
                        _pat_conn.close()

                        # Get existing annotations
                        _existing = pd.read_sql_query(
                            "SELECT DFCI_MRN, agent, progression_date FROM annotations WHERE (deleted IS NULL OR deleted=0)",
                            _db()
                        )

                        to_import = []
                        n_dup = 0
                        n_no_mrn = 0
                        for _, row in src_df.iterrows():
                            mrn = str(row.get('DFCI_MRN', '') or '').strip()
                            if mrn not in proj_mrns:
                                n_no_mrn += 1; continue
                            inc_agent = str(row.get('agent', '') or '').strip().lower()
                            inc_date  = str(row.get('progression_date', '') or '').strip()
                            # Conflict check: same MRN + same agent + date within tol
                            conflict = False
                            mrn_existing = _existing[_existing['DFCI_MRN'].apply(safe_str) == mrn]
                            for _, ex in mrn_existing.iterrows():
                                ex_agent = str(ex.get('agent', '') or '').strip().lower()
                                ex_date  = str(ex.get('progression_date', '') or '').strip()
                                if ex_agent == inc_agent and dates_within_days(inc_date, ex_date, tol):
                                    conflict = True; break
                            if conflict:
                                n_dup += 1
                            else:
                                to_import.append(dict(row))

                        n_total = len(src_df)
                        n_mrn_ok = n_total - n_no_mrn
                        n_will_import = len(to_import)
                        _imp_preview_data[0] = {'rows': to_import, 'src': src, 'tol': tol}
                        _imp_status.set_text(
                            f"Source: {src.name} | Tolerance: {tol}d | "
                            f"Found {n_total} annotations ({n_mrn_ok} with MRN in project). "
                            f"Will import: {n_will_import} | Skipped (duplicate): {n_dup} | Skipped (MRN not in project): {n_no_mrn}"
                        )
                        _imp_status.classes('text-green-700' if n_will_import > 0 else 'text-gray-600', remove='text-red-600')
                        _imp_btn.set_enabled(n_will_import > 0)
                    except Exception as _pe:
                        _imp_status.set_text(f'Preview error: {_pe}')
                        _imp_status.classes('text-red-600', remove='text-green-600 text-gray-600')
                        _imp_btn.set_enabled(False)

                ui.button('Preview', on_click=_do_preview).props('dense outline')

            def _do_import():
                data = _imp_preview_data[0]
                if not data:
                    ui.notify('Run Preview first', color='red'); return
                rows = data['rows']
                src = data['src']
                tol = data['tol']
                imported = 0; errors = 0
                src_name = src.name
                for row in rows:
                    try:
                        _db().execute("""
                            INSERT OR IGNORE INTO annotations
                            (DFCI_MRN, progression_date, progression_source, agent, evidence,
                             report_id, determined_by, user, modification_timestamp,
                             agent_start, agent_start_source, agent_end, agent_end_source,
                             extraction_version, import_source, deleted)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                        """, (
                            str(row.get('DFCI_MRN','')).strip(),
                            str(row.get('progression_date','')).strip() or None,
                            str(row.get('progression_source','')).strip() or None,
                            str(row.get('agent','')).strip() or None,
                            str(row.get('evidence','')).strip() or None,
                            str(row.get('report_id','')).strip() or None,
                            str(row.get('determined_by','')).strip() or None,
                            str(row.get('user','')).strip() or None,
                            str(row.get('modification_timestamp','')).strip() or None,
                            str(row.get('agent_start','')).strip() or None,
                            str(row.get('agent_start_source','')).strip() or None,
                            str(row.get('agent_end','')).strip() or None,
                            str(row.get('agent_end_source','')).strip() or None,
                            'imported',
                            src_name,
                            ))
                        imported += 1
                    except Exception:
                        errors += 1
                _db().commit()
                if not _demo_mode[0]:
                    refresh_annotations_df()
                imp_dlg.close()
                # Defer render_patient so dialog is fully closed before panel rebuild
                ui.timer(0.05, lambda: render_patient(current_patient_index), once=True)
                ui.notify(
                    f'Import complete: {imported} imported, {errors} errors. Tolerance: {tol}d.',
                    color='green'
                )

            _imp_btn.on('click', _do_import)

            ui.separator().classes('my-2')
            ui.button('Close', on_click=imp_dlg.close).props('dense')

        imp_dlg.open()

    # ── Settings ──────────────────────────────────────────────────────────────
    def show_settings():
        # Toggle close if already open
        if _open_dialogs['settings'] is not None:
            try:
                _open_dialogs['settings'].close()
            except Exception:
                pass
            _open_dialogs['settings'] = None
            return

        global EXTRACTION_CSV_PATH, SQLITE_PATH, ANNOTATION_OUTPUT_DIR
        global conn, cursor, df, NOTE_FONT_SIZE, CONFIG_PATH

        with ui.dialog() as dlg, ui.card().classes('w-[600px] max-h-[92vh] overflow-y-auto'):
            _open_dialogs['settings'] = dlg
            ui.label('Settings').classes('text-lg font-bold mb-2')

            ui.label('Session Info').classes('text-sm font-semibold mt-1 text-gray-700')
            ui.label(f'WATNEY version: {WATNEY_VERSION}').classes('text-xs text-gray-600')
            if _UPDATE_AVAILABLE[0] and _LATEST_VERSION[0]:
                ui.label(f'⚠ Update available: v{_LATEST_VERSION[0]}').classes('text-xs text-orange-600 font-semibold')
            ui.label(f'Current user: {CURRENT_USER or "not set"}').classes('text-xs text-gray-600')
            ui.separator()

            ui.label('About').classes('text-sm font-semibold mt-1 text-gray-700')
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

            ui.label('Report Text Size').classes('text-sm font-semibold mt-1 text-gray-700')
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

            ui.label('Search Behavior').classes('text-sm font-semibold mt-1 text-gray-700')
            ui.label("By default Cmd-F / Ctrl-F opens the WATNEY search bar. Disable to use the browser's native search instead.").classes('text-xs text-gray-500 mb-1')
            _cfg_now2 = load_config()
            cmdf_default = not bool(_cfg_now2.get('disable_cmdf', False))
            cmdf_toggle = ui.switch('Intercept Cmd-F / Ctrl-F for WATNEY search', value=cmdf_default)
            def _save_cmdf(e):
                cfg2 = load_config()
                cfg2['disable_cmdf'] = not cmdf_toggle.value
                save_config(cfg2)
                ui.run_javascript(f'window._watneyCmdfEnabled = {str(cmdf_toggle.value).lower()};')
                ui.notify('Saved — takes effect immediately', color='green')
            cmdf_toggle.on('update:model-value', _save_cmdf)
            ui.separator()

            # ── Project section (replaces old File Paths / Change Paths) ───
            ui.label('Project').classes('text-sm font-semibold mt-1 text-gray-700')
            if PROJECT_DIR is not None:
                _proj_name_lbl = ui.label(
                    f'Name: {CURRENT_PROJECT.get("project_name","—")}'
                ).classes('text-xs text-gray-600')
                ui.label(f'Folder: {PROJECT_DIR}').classes('text-xs text-gray-500 break-all')
                ann_db = get_project_annotations_db_path(PROJECT_DIR)
                ui.label(f'DB: {ann_db.name}').classes('text-xs text-gray-500 ml-2')

                # Editable project name
                _new_name_input = ui.input(label='Rename project', placeholder='New name').classes('w-full mt-1')
                def _rename_project():
                    n = _new_name_input.value.strip()
                    if not n: ui.notify('Enter a name', color='red'); return
                    CURRENT_PROJECT['project_name'] = n
                    try:
                        with open(PROJECT_DIR / 'project.json', 'w') as _f:
                            import json as _j; _j.dump(CURRENT_PROJECT, _f, indent=2)
                        _proj_name_lbl.set_text(f'Name: {n}')
                        ui.notify(f'Renamed to "{n}"', color='green')
                    except Exception as _re: ui.notify(f'Error: {_re}', color='red')
                ui.button('Rename', on_click=_rename_project).props('dense outline').classes('mt-1')

                ui.separator().classes('my-2')

                # Checkpoint controls
                ui.label('Checkpoints').classes('text-xs font-semibold text-gray-700')
                _chk_interval = CURRENT_PROJECT.get('checkpoint_interval_minutes', 30)
                _lcp = CURRENT_PROJECT.get('last_checkpoint')
                ui.label(f'Last checkpoint: {_lcp or "none"}').classes('text-xs text-gray-500')
                with ui.row().classes('items-center gap-3 mt-1'):
                    _chk_slider = ui.slider(min=5, max=60, step=5, value=_chk_interval).classes('w-32')
                    _chk_lbl = ui.label(f'{_chk_interval} min').classes('text-xs text-gray-600')
                _chk_slider.on('update:model-value', lambda _: _chk_lbl.set_text(f'{int(_chk_slider.value)} min'))

                def _save_chk_interval():
                    CURRENT_PROJECT['checkpoint_interval_minutes'] = int(_chk_slider.value)
                    try:
                        with open(PROJECT_DIR / 'project.json', 'w') as _f:
                            import json as _j; _j.dump(CURRENT_PROJECT, _f, indent=2)
                        ui.notify(f'Checkpoint interval set to {int(_chk_slider.value)} min', color='green')
                    except Exception as _e: ui.notify(f'Error: {_e}', color='red')

                def _checkpoint_now():
                    if PROJECT_DIR is None: return
                    try:
                        chk = do_checkpoint(PROJECT_DIR, conn)
                        ui.notify(f'Checkpoint saved: {chk.name}', color='green')
                    except Exception as _e: ui.notify(f'Checkpoint failed: {_e}', color='red')

                with ui.row().classes('gap-2 mt-1'):
                    ui.button('Save interval', on_click=_save_chk_interval).props('dense outline')
                    ui.button('Checkpoint now', on_click=_checkpoint_now).props('dense outline')

                ui.separator().classes('my-2')

                # Extraction versions
                ui.label('Extractions').classes('text-xs font-semibold text-gray-700')
                for _ext in CURRENT_PROJECT.get('extractions', []):
                    _active = (_ext['filename'] == CURRENT_PROJECT.get('active_extraction'))
                    _col = 'text-indigo-600 font-semibold' if _active else 'text-gray-500'
                    ui.label(
                        f"{'▶ ' if _active else '  '}{_ext['label']}  "
                        f"({_ext['row_count']} patients, {_ext['loaded_date'][:10]})"
                    ).classes(f'text-xs {_col}')

                # Load new extraction
                ui.label('Load New Extraction').classes('text-xs font-semibold text-gray-700 mt-2')
                _new_ext_input = ui.input(label='New extraction CSV path', placeholder='/path/to/new_extraction.csv').classes('w-full')
                _new_ext_status = ui.label('').classes('text-xs mt-1')

                def _load_new_extraction():
                    global df, EXTRACTION_CSV_PATH
                    p = Path(_new_ext_input.value.strip())
                    if not p.name:
                        _new_ext_status.set_text('Enter a CSV path.')
                        _new_ext_status.classes('text-red-600', remove='text-green-600'); return
                    try:
                        # Capture old extraction for reconciliation diff
                        _old_ext_path = PROJECT_DIR / CURRENT_PROJECT.get('active_extraction', '')
                        _old_df = None
                        if _old_ext_path.exists():
                            try: _old_df = load_dataframe(str(_old_ext_path))
                            except Exception: pass

                        result = load_extraction_into_project(PROJECT_DIR, p)
                        df = result['df']
                        # Reload CURRENT_PROJECT from disk so the banner reflects the new extraction
                        try:
                            import json as _lj
                            with open(PROJECT_DIR / 'project.json', 'r') as _lf:
                                CURRENT_PROJECT.clear()
                                CURRENT_PROJECT.update(_lj.load(_lf))
                        except Exception:
                            pass
                        EXTRACTION_CSV_PATH = str(PROJECT_DIR / CURRENT_PROJECT['active_extraction'])
                        _new_ext_input.value = ''
                        _new_ext_status.set_text(f"Loaded {result['ext_filename']}")
                        _new_ext_status.classes('text-green-600', remove='text-red-600')
                        render_patient(0)
                        # Open reconciliation dialog
                        _open_reconciliation_dialog(_old_df, df)
                    except ProjectError as _pe:
                        _new_ext_status.set_text(str(_pe))
                        _new_ext_status.classes('text-red-600', remove='text-green-600')
                    except Exception as _ex:
                        _new_ext_status.set_text(f'Error: {_ex}')
                        _new_ext_status.classes('text-red-600', remove='text-green-600')

                ui.button('Load New Extraction', on_click=_load_new_extraction).props('dense outline').classes('mt-1')

                ui.separator().classes('my-2')

                # Import annotations (Phase 3)
                ui.label('Import Annotations').classes('text-xs font-semibold text-gray-700')
                ui.label('Import from a prior export CSV or another project\'s annotations.db.').classes('text-xs text-gray-500 mb-1')
                ui.button('Import Annotations…', on_click=_open_import_dialog).props('dense outline')

                ui.separator().classes('my-2')

                # Legacy migration
                ui.label('Legacy Migration').classes('text-xs font-semibold text-gray-700')
                ui.label('Import annotations from an old watney_annotations/ folder.').classes('text-xs text-gray-500 mb-1')
                _leg_folder_input = ui.input(label='Legacy folder path', placeholder='./watney_annotations').classes('w-full')
                _leg_csv_input    = ui.input(label='CSV path for that data', placeholder='/path/to/extraction.csv').classes('w-full')
                _leg_name_input   = ui.input(label='New project name', placeholder='Migrated Project').classes('w-full')
                _leg_dest_input   = ui.input(label='New project folder', placeholder='/path/to/MigratedProject').classes('w-full')
                _leg_status = ui.label('').classes('text-xs mt-1')

                def _do_migrate():
                    legacy_p  = Path(_leg_folder_input.value.strip())
                    csv_p     = Path(_leg_csv_input.value.strip())
                    name_p    = _leg_name_input.value.strip()
                    dest_p    = Path(_leg_dest_input.value.strip())
                    if not all([legacy_p.name, csv_p.name, name_p, dest_p.name]):
                        _leg_status.set_text('Fill in all fields.'); return
                    try:
                        meta = migrate_legacy_folder(legacy_p, dest_p, name_p, csv_p)
                        _leg_status.set_text(f'Migrated to {dest_p}. Open it as a project to use.')
                        _leg_status.classes('text-green-600', remove='text-red-600')
                        ui.notify('Migration complete. Open the new project to use it.', color='green')
                    except ProjectError as _pe:
                        _leg_status.set_text(str(_pe))
                        _leg_status.classes('text-red-600', remove='text-green-600')
                    except Exception as _ex:
                        _leg_status.set_text(f'Error: {_ex}')
                        _leg_status.classes('text-red-600', remove='text-green-600')

                ui.button('Migrate', on_click=_do_migrate).props('dense outline').classes('mt-1')
            else:
                ui.label('No project open — log out and open a project to manage settings here.').classes('text-xs text-gray-400')
            ui.separator()

            ui.label('Database Stats').classes('text-sm font-semibold mt-1 text-gray-700')
            stats_df = load_annotations_df()
            ui.label(f'Total annotations: {len(stats_df)}').classes('text-xs text-gray-600')
            ui.label(f'Unique patients: {stats_df["DFCI_MRN"].nunique() if not stats_df.empty else 0}').classes('text-xs text-gray-600')
            ui.label(f'LLM-sourced: {len(stats_df[stats_df["progression_source"]=="LLM"]) if not stats_df.empty else 0}').classes('text-xs text-gray-600')
            ui.label(f'Clinician-sourced: {len(stats_df[stats_df["progression_source"]=="manual"]) if not stats_df.empty else 0}').classes('text-xs text-gray-600')
            ui.separator().classes('my-2')

            ui.label('Updates').classes('text-sm font-semibold mt-1 text-gray-700')
            ui.label(f'Installed version: {WATNEY_VERSION}').classes('text-xs text-gray-600')
            version_status = ui.label('').classes('text-xs text-gray-500 mt-1')
            if _UPDATE_AVAILABLE[0] and _LATEST_VERSION[0]:
                version_status.set_text(f'Latest: {_LATEST_VERSION[0]} — update available!')
                version_status.classes('text-orange-600', remove='text-gray-500 text-green-600')
            update_btn = ui.button('Update WATNEY').props('dense outline').classes('mt-1')
            update_btn.set_visibility(_UPDATE_AVAILABLE[0])

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
                        _LATEST_VERSION[0] = latest
                        if latest != str(WATNEY_VERSION):
                            _UPDATE_AVAILABLE[0] = True
                            version_status.set_text(f'Latest: {latest} — update available!')
                            version_status.classes('text-orange-600', remove='text-gray-500 text-green-600')
                            update_btn.set_visibility(True)
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
                    _UPDATE_AVAILABLE[0] = False
                else:
                    version_status.set_text(f'Update failed: {stderr.decode()[:200]}')
                    version_status.classes('text-red-600', remove='text-green-600 text-orange-600')

            update_btn.on('click', do_update)
            ui.button('Check for updates', on_click=check_version).props('dense outline').classes('mt-1')

            ui.separator().classes('my-2')
            def _close_settings():
                _open_dialogs['settings'] = None
                dlg.close()
            ui.button('Close', on_click=_close_settings).props('dense')

        dlg.open()

    # ── Demo / real DB plumbing ───────────────────────────────────────────────
    _demo_mode = [False]
    _demo_conn = [None]

    def _ensure_demo_conn():
        if _demo_conn[0] is None:
            import sqlite3 as _sq
            dc = _sq.connect(':memory:', check_same_thread=False)
            dc.row_factory = _sq.Row
            # Schema must match _bootstrap_legacy_db (real DB) column-for-column so that
            # _get_exclusion / _set_exclusion and any future helpers work identically in
            # demo mode without branching.  Add columns here whenever the real schema grows.
            dc.execute("""CREATE TABLE IF NOT EXISTS annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                DFCI_MRN TEXT NOT NULL,
                progression_date TEXT, progression_source TEXT, agent TEXT,
                evidence TEXT, report_id TEXT, determined_by TEXT, user TEXT,
                modification_timestamp TEXT,
                agent_start TEXT, agent_start_source TEXT,
                agent_end TEXT, agent_end_source TEXT,
                exclusion_flag TEXT, exclusion_reason TEXT, extraction_version TEXT,
                deleted INTEGER DEFAULT 0, deletion_reason TEXT, deletion_timestamp TEXT,
                import_source TEXT, unexclusion_reason TEXT)""")
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
        # extraction_version: None in demo mode (no project context)
        _ext_ver = row.get('extraction_version', None)
        if existing:
            cr.execute("""UPDATE annotations
                SET agent=?,evidence=?,determined_by=?,user=?,modification_timestamp=?,
                    agent_start=?,agent_start_source=?,agent_end=?,agent_end_source=?,
                    extraction_version=COALESCE(?,extraction_version)
                WHERE id=?""",
                (row['agent'], row['evidence'], row['determined_by'], row.get('user'),
                 row['modification_timestamp'], row.get('agent_start'), row.get('agent_start_source'),
                 row.get('agent_end'), row.get('agent_end_source'), _ext_ver, existing['id']))
        else:
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
                 exclusion_flag,exclusion_reason,extraction_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row['DFCI_MRN'], row['progression_date'], row['progression_source'],
                 row['agent'], row['evidence'], row['report_id'],
                 row['determined_by'], row.get('user'), row['modification_timestamp'],
                 row.get('agent_start'), row.get('agent_start_source'),
                 row.get('agent_end'), row.get('agent_end_source'),
                 _eflag, _ereason, _ext_ver))
        dc.commit()

    def _patients_conn():
        """Return a connection to patients.db, or None in demo mode / no project."""
        if _demo_mode[0] or PROJECT_DIR is None:
            return None
        return open_patients_db(PROJECT_DIR)

    def _get_exclusion(patient_id):
        """Read exclusion status from patients.db (project mode) or annotations (demo/legacy)."""
        pid = str(patient_id).strip()
        pc = _patients_conn()
        if pc is not None:
            try:
                row = pc.execute(
                    "SELECT exclusion_flag, exclusion_reason, excluded_by, excluded_at, "
                    "unexclusion_reason, unexcluded_by, unexcluded_at FROM patients WHERE DFCI_MRN=?",
                    (pid,)
                ).fetchone()
                pc.close()
                return row
            except Exception:
                pc.close()
                return None
        # Demo / legacy fallback: read from annotations
        try:
            _cr = _db().cursor()
            _cr.execute(
                "SELECT exclusion_flag, exclusion_reason, unexclusion_reason FROM annotations "
                "WHERE DFCI_MRN=? AND exclusion_flag IS NOT NULL LIMIT 1",
                (pid,)
            )
            return _cr.fetchone()
        except Exception:
            return None

    def _set_exclusion(patient_id, flag, reason):
        """Write exclusion status to patients.db (project mode) or annotations (demo/legacy)."""
        pid = str(patient_id).strip()
        now = datetime.now().isoformat(timespec='seconds')
        pc = _patients_conn()
        if pc is not None:
            # ── Project mode: write to patients.db ───────────────────────────
            if flag:
                pc.execute(
                    """UPDATE patients SET exclusion_flag='True', exclusion_reason=?,
                       excluded_by=?, excluded_at=?,
                       unexclusion_reason=NULL, unexcluded_by=NULL, unexcluded_at=NULL
                       WHERE DFCI_MRN=?""",
                    (reason, CURRENT_USER, now, pid)
                )
            else:
                pc.execute(
                    """UPDATE patients SET exclusion_flag=NULL, exclusion_reason=NULL,
                       excluded_by=NULL, excluded_at=NULL,
                       unexclusion_reason=?, unexcluded_by=?, unexcluded_at=?
                       WHERE DFCI_MRN=?""",
                    (reason, CURRENT_USER, now, pid)
                )
            pc.commit()
            pc.close()
            return
        # ── Demo / legacy fallback: write to annotations ──────────────────────
        _cr = _db().cursor()
        if flag:
            flag_str = 'True'
            _cr.execute(
                "UPDATE annotations SET exclusion_flag=?, exclusion_reason=? WHERE DFCI_MRN=?",
                (flag_str, reason, pid)
            )
            if _cr.rowcount == 0:
                _cr.execute(
                    """INSERT INTO annotations
                       (DFCI_MRN, progression_source, exclusion_flag, exclusion_reason,
                        user, modification_timestamp)
                       VALUES (?, 'exclusion_placeholder', ?, ?, ?, ?)""",
                    (pid, flag_str, reason, CURRENT_USER, now)
                )
        else:
            _cr.execute(
                "DELETE FROM annotations WHERE DFCI_MRN=? AND progression_source='exclusion_placeholder'",
                (pid,)
            )
            _cr.execute(
                """UPDATE annotations SET exclusion_flag=NULL, exclusion_reason=NULL,
                   unexclusion_reason=?, user=COALESCE(user,?), modification_timestamp=?
                   WHERE DFCI_MRN=?""",
                (reason, CURRENT_USER, now, pid)
            )
        _db().commit()
        if not _demo_mode[0]:
            refresh_annotations_df()

    def _show_exclusion_dialog(patient_id, currently_excluded):
        action_label = 'Remove Exclusion' if currently_excluded else 'Exclude Patient'
        with ui.dialog() as excl_dlg, ui.card().classes('w-[440px]'):
            ui.label(action_label).classes('text-base font-bold')
            if currently_excluded:
                ui.label('This patient is currently excluded. Remove the exclusion?').classes('text-sm text-gray-600')
                _excl_row = _get_exclusion(patient_id)
                if _excl_row and _excl_row['exclusion_reason']:
                    ui.label(f'Original reason: {_excl_row["exclusion_reason"]}').classes('text-xs text-gray-400 italic mt-1')
            else:
                ui.label('Are you sure you want to exclude this patient?').classes('text-sm text-gray-600')
            _reason_label = 'Un-exclusion reason (required)' if currently_excluded else 'Exclusion reason (required)'
            reason_input = ui.textarea(label=_reason_label).classes('w-full mt-2')
            reason_input.props('outlined dense')
            err_label = ui.label('').classes('text-xs text-red-500')
            action_row = ui.row().classes('w-full justify-end gap-2 mt-3')
            # Capture client while dialog slot is still valid
            from nicegui import context as _ctx
            import asyncio as _asyncio
            _client = _ctx.slot.parent.client

            with action_row:
                def _do_confirm():
                    reason = reason_input.value.strip()
                    if not reason:
                        err_label.set_text('Please provide a reason.')
                        return
                    new_excluded = 0 if currently_excluded else 1
                    _set_exclusion(patient_id, new_excluded, reason)
                    msg = '✓ Patient excluded.' if new_excluded else '✓ Patient re-included.'
                    color = 'negative' if new_excluded else 'positive'
                    excl_dlg.close()
                    ui.notify(msg, color=color, timeout=2500)
                    async def _render():
                        with _client:
                            render_patient(current_patient_index)
                    _asyncio.get_running_loop().create_task(_render())

                ui.button('Cancel', on_click=excl_dlg.close).props('flat dense')
                ui.button(action_label, on_click=_do_confirm).props(
                    'dense color=negative' if not currently_excluded else 'dense'
                )
        excl_dlg.open()

    # ── Sticky header JS helper ───────────────────────────────────────────────
    def _inject_sticky_header_js():
        ui.run_javascript("""
(function(){
  var sticky = document.getElementById('note-sticky-header');
  var rp     = document.querySelector('.right-pane');
  if (!sticky || !rp) { setTimeout(arguments.callee, 200); return; }

  // Position sticky to match the note-card left/right bounds exactly.
  function positionSticky() {
    var rpRect = rp.getBoundingClientRect();
    // Left: account for right-pane padding-left
    var pl = parseFloat(window.getComputedStyle(rp).paddingLeft) || 8;
    var left = rpRect.left + pl;
    // Right: use the actual right edge of a note card if available,
    // otherwise fall back to right-pane right edge minus scrollbar width.
    var right = rpRect.right;
    var firstCard = rp.querySelector('.note-card');
    if(firstCard){
      right = firstCard.getBoundingClientRect().right;
    } else {
      // Estimate scrollbar width
      var sbw = rp.offsetWidth - rp.clientWidth;
      right = rpRect.right - sbw;
    }
    sticky.style.left  = left + 'px';
    sticky.style.width = (right - left) + 'px';
    sticky.style.top   = '0px';
  }

  function updateSticky() {
    positionSticky();
    var rpRect = rp.getBoundingClientRect();
    var rpTop  = rpRect.top;
    var cards  = Array.from(rp.querySelectorAll('.note-card'));
    var active = null;
    for (var i = 0; i < cards.length; i++) {
      var cardRect = cards[i].getBoundingClientRect();
      var meta     = cards[i].querySelector('.note-meta');
      if (!meta) continue;
      var metaRect = meta.getBoundingClientRect();
      if (cardRect.bottom > rpTop + 2 && metaRect.bottom < rpTop + 24) {
        active = meta;
      }
    }
    if (active) {
      sticky.innerHTML = '';
      sticky.style.display = 'flex';
      var spans = active.querySelectorAll('span');
      spans.forEach(function(s, i) {
        if (i > 0) {
          var sep = document.createElement('span');
          sep.textContent = ' \u00b7 ';
          sep.style.color = '#93c5fd';
          sticky.appendChild(sep);
        }
        var sp = document.createElement('span');
        sp.textContent = s.textContent;
        sticky.appendChild(sp);
      });
    } else {
      sticky.style.display = 'none';
    }
  }

  rp.addEventListener('scroll', updateSticky, {passive: true});
  window.addEventListener('resize', function(){ positionSticky(); updateSticky(); });
  setTimeout(updateSticky, 200);
  setTimeout(updateSticky, 800);
})();
""")


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

        _note_font = NOTE_FONT_SIZE
        _cmdf_off  = load_config().get('disable_cmdf', False)

        with left_panel:
            # Apply font size and cmdf preference — inside left_panel where slot is always valid
            ui.run_javascript(f"""
                let el=document.getElementById('wfont');
                if(!el){{el=document.createElement('style');el.id='wfont';document.head.appendChild(el);}}
                el.textContent='pre{{font-size:{_note_font}px!important;}}';
                window._watneyCmdfEnabled = {'false' if _cmdf_off else 'true'};
            """)
            with ui.row().classes('w-full items-start justify-between no-wrap'):
                with ui.column().classes('gap-0'):
                    # Update badge next to WATNEY header
                    with ui.row().classes('items-center gap-2'):
                        ui.label(f'WATNEY {_major_version(WATNEY_VERSION)}').classes('text-2xl font-bold')
                        if _UPDATE_AVAILABLE[0] and _LATEST_VERSION[0]:
                            ui.label(f'🔔 v{_LATEST_VERSION[0]}').classes(
                                'text-xs text-white font-bold bg-orange-500 rounded px-2 py-0.5 watney-update-badge'
                            )
                    ui.label('Developed by Justin Vinh @ DFCI').classes('text-[11px] text-gray-500 leading-tight')
                    user_label = ui.label(f'User: {CURRENT_USER or "not set"}').classes('text-xs text-gray-600')
                    if _demo_mode[0]:
                        ui.label('⚠ DEMO MODE').classes('text-xs text-amber-600 font-bold')
                    elif CURRENT_PROJECT:
                        ui.label(f'Project: {CURRENT_PROJECT.get("project_name","—")}').classes('text-xs text-indigo-600')
                # Logout — top-right of header
                ui.button('Logout', on_click=_do_logout).props('dense flat size=sm').classes('text-gray-400 self-start')

            # ── Extraction selector ───────────────────────────────────────────
            # Always reflects which CSV is loaded into df. Switching re-renders
            # the current patient with the new extraction immediately.
            if not _demo_mode[0] and CURRENT_PROJECT and PROJECT_DIR:
                _ext_list = CURRENT_PROJECT.get('extractions', [])
                if len(_ext_list) > 1:
                    # Build label→rel_path map; show filename without leading N_ prefix for display
                    def _ext_display_name(ext_entry):
                        return Path(ext_entry['filename']).name
                    _ext_display_names = [_ext_display_name(e) for e in _ext_list]
                    _active_rel = CURRENT_PROJECT.get('active_extraction', '')
                    _active_display = Path(_active_rel).name if _active_rel else _ext_display_names[0]
                    _cur_idx = next((i for i, e in enumerate(_ext_list) if e['filename'] == _active_rel), 0)

                    with ui.row().classes('w-full items-center gap-1 mt-1').style('flex-wrap:nowrap;'):
                        ui.label('Extraction:').classes('text-xs text-gray-500 flex-shrink-0')
                        _ext_select = ui.select(
                            options=_ext_display_names,
                            value=_ext_display_names[_cur_idx],
                            label=None,
                        ).classes('flex-1').props('outlined dense').style('min-width:0;')

                    def _on_extraction_change(_e,
                                              _ext_list=_ext_list,
                                              _display_names=_ext_display_names):
                        global df, EXTRACTION_CSV_PATH
                        chosen = _ext_select.value
                        if not chosen: return
                        try:
                            _idx = _display_names.index(chosen)
                        except ValueError:
                            return
                        entry = _ext_list[_idx]
                        new_rel = entry['filename']
                        if new_rel == CURRENT_PROJECT.get('active_extraction'):
                            return  # already selected
                        new_path = PROJECT_DIR / new_rel
                        if not new_path.exists():
                            ui.notify(f'File not found: {new_path}', color='red'); return
                        try:
                            new_df = load_dataframe(str(new_path))
                        except Exception as _e2:
                            ui.notify(f'Could not load extraction: {_e2}', color='red'); return
                        # Update module-level state
                        df = new_df
                        EXTRACTION_CSV_PATH = str(new_path)
                        CURRENT_PROJECT['active_extraction'] = new_rel
                        # Persist to project.json (survives across sessions)
                        import json as _pj
                        try:
                            with open(PROJECT_DIR / 'project.json', 'w') as _pf:
                                _pj.dump(CURRENT_PROJECT, _pf, indent=2)
                        except Exception:
                            pass
                        ui.notify(f'Switched to: {Path(new_rel).name}', color='green', timeout=1800)
                        from nicegui import context as _ctx
                        import asyncio as _asyncio
                        _client = _ctx.slot.parent.client
                        _idx = current_patient_index
                        async def _deferred_render():
                            with _client:
                                render_patient(_idx)
                        _asyncio.get_running_loop().create_task(_deferred_render())

                    _ext_select.on('update:model-value', _on_extraction_change)

                else:
                    # Only one extraction — just show the filename
                    _sole = Path(CURRENT_PROJECT.get('active_extraction', '')).name
                    if _sole:
                        ui.label(f'Extraction: {_sole}').classes('text-xs text-gray-500 mt-1')
            ui.separator().classes('mb-4')

            _excl = _get_exclusion(patient_id)
            _is_excluded = bool(_excl and _excl['exclusion_flag'] == 'True')

            # ── MRN header + exclusion button ────────────────────────────────
            with ui.row().classes('w-full items-center justify-between'):
                ui.label(f'Patient {patient_id} | Row {index+1}/{len(df)}').classes('text-xl font-bold')
                ui.button(
                    'Un-exclude Patient' if _is_excluded else 'Exclude Patient',
                    on_click=lambda pid=patient_id, excl=_is_excluded:
                        _show_exclusion_dialog(pid, excl)
                ).props('dense outline size=sm').classes(
                    'text-green-700' if _is_excluded else ''
                )

            # ── Exclusion banner (shown immediately under MRN when excluded) ─
            if _is_excluded:
                with ui.card().classes('w-full').style(
                    'background:#fef2f2;border:1px solid #fca5a5;padding:8px;margin-bottom:6px;'
                ):
                    ui.label('⚠ Patient Excluded').classes('text-sm font-bold text-red-600')
                    ui.label(
                        f'Reason: {_excl["exclusion_reason"] or "No reason given"}'
                    ).classes('text-xs text-red-500')

            ui.label('Progression Summary').classes('text-sm font-bold')

            summary_container = ui.column().classes('w-full')

            def _parse_ext_label(ext_ver):
                """Extract version label from filename: 'extraction_v2_20260521.csv' -> 'v2'."""
                if not ext_ver or str(ext_ver).strip() in ('', 'None', 'nan'):
                    return '—'
                import re as _re2
                m = _re2.search(r'v(\d+)', str(ext_ver))
                return f'v{m.group(1)}' if m else str(ext_ver)[:6]

            def _soft_delete_annotation(ann_id, ann_info):
                """Show a confirmation dialog then soft-delete the annotation."""
                with ui.dialog() as del_dlg, ui.card().classes('w-[420px]'):
                    ui.label('Delete Annotation?').classes('text-base font-bold text-red-700')
                    ui.label(
                        f"Date: {ann_info['date']}  ·  Agent: {ann_info['agent']}  "
                        f"·  Source: {ann_info['source']}"
                    ).classes('text-xs text-gray-600 mt-1')
                    ui.label('This is a soft delete — the record is preserved for audit purposes.').classes('text-xs text-gray-400 mt-1')
                    del_reason = ui.textarea(label='Reason (required)').classes('w-full mt-2').props('outlined dense rows=2')
                    del_err = ui.label('').classes('text-xs text-red-500')
                    with ui.row().classes('w-full justify-end gap-2 mt-3'):
                        ui.button('Cancel', on_click=del_dlg.close).props('flat dense')
                        def _confirm_delete(aid=ann_id, dlg=del_dlg):
                            reason = del_reason.value.strip()
                            if not reason:
                                del_err.set_text('Reason is required.'); return
                            ts = datetime.now().isoformat(timespec='seconds')
                            try:
                                _db().execute(
                                    """UPDATE annotations
                                       SET deleted=1, deletion_reason=?, deletion_timestamp=?
                                       WHERE id=?""",
                                    (reason, ts, aid)
                                )
                                _db().commit()
                            except Exception as _de:
                                ui.notify(f'Delete failed: {_de}', color='red')
                                dlg.close(); return
                            if not _demo_mode[0]:
                                refresh_annotations_df()
                            ui.notify('Annotation soft-deleted', color='orange')
                            dlg.close()
                            refresh_summary()
                        ui.button('Delete', on_click=_confirm_delete).props('dense color=negative')
                del_dlg.open()

            def refresh_summary():
                summary_container.clear()
                summary_rows = []
                if _demo_mode[0]:
                    _df_ann = pd.read_sql_query(
                        "SELECT * FROM annotations WHERE (deleted IS NULL OR deleted = 0)", _db()
                    )
                else:
                    refresh_annotations_df()
                    _df_ann = annotations_df  # already filtered
                patient_annotations = _df_ann[
                    (_df_ann['DFCI_MRN'].apply(safe_str) == safe_str(patient_id)) &
                    (_df_ann['progression_source'] != 'exclusion_placeholder')
                ]
                for _, ann in patient_annotations.iterrows():
                    prog_date = normalize_any_date(ann.get('progression_date'))
                    if not prog_date: continue
                    summary_rows.append({
                        'id':          int(ann.get('id', 0) or 0),
                        'date':        prog_date,
                        'sort_date':   sort_date_key(prog_date),
                        'agent':       ann.get('agent', '') or '',
                        'agent_start': ann.get('agent_start', '') or '',
                        'agent_end':   ann.get('agent_end', '') or '',
                        'source':      ann.get('progression_source', '') or '',
                        'user':        ann.get('user', '') or '',
                        'ext_ver':     _parse_ext_label(ann.get('extraction_version')),
                    })
                summary_rows.sort(key=lambda x: x.get('sort_date', '9999-12-31'))
                with summary_container:
                    if not summary_rows:
                        ui.label('NO PROGRESSION DATES ASSIGNED TO AGENTS').classes('text-xs text-red-500 font-bold')
                    else:
                        with ui.column().classes('w-full gap-1'):
                            with ui.row().classes('w-full text-xs font-bold border-b pb-1'):
                                ui.label('Date').style('width:82px;flex-shrink:0')
                                ui.label('Agent').style('flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap')
                                ui.label('Start').style('width:74px;flex-shrink:0')
                                ui.label('End').style('width:74px;flex-shrink:0')
                                ui.label('Src').style('width:44px;flex-shrink:0')
                                ui.label('User').style('width:62px;flex-shrink:0')
                                ui.label('Ext').style('width:30px;flex-shrink:0;color:#6366f1')
                                ui.label('Del').style('width:28px;flex-shrink:0')
                            for rd in summary_rows:
                                with ui.row().classes('w-full text-xs items-center').style('flex-wrap:nowrap'):
                                    _date_style  = 'width:82px;flex-shrink:0;' + ('color:#d97706;font-weight:600;' if rd['date'] == 'NA' else '')
                                    _start_style = 'width:74px;flex-shrink:0;' + ('color:#d97706;font-weight:600;' if rd['agent_start'] == 'NA' else '')
                                    _end_style   = 'width:74px;flex-shrink:0;' + ('color:#d97706;font-weight:600;' if rd['agent_end'] == 'NA' else '')
                                    ui.label(rd['date']).style(_date_style)
                                    ui.label(rd['agent']).style('flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap')
                                    ui.label(rd['agent_start']).style(_start_style)
                                    ui.label(rd['agent_end']).style(_end_style)
                                    ui.label(rd['source']).style('width:44px;flex-shrink:0')
                                    ui.label(rd['user']).style('width:62px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#374151;')
                                    ui.label(rd['ext_ver']).style('width:30px;flex-shrink:0;color:#6366f1;font-size:9px;')
                                    ui.button('×',
                                        on_click=lambda ann_id=rd['id'], ann_info=rd:
                                            _soft_delete_annotation(ann_id, ann_info)
                                    ).props('dense flat size=xs').classes('text-red-400').style('width:28px;flex-shrink:0;')

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
            # ── Active extraction filename banner ─────────────────────────────
            # df always reflects the active extraction (EXTRACTION_CSV_PATH).
            # Display the raw filename so users can confirm which version is loaded.
            def _get_extraction_filename():
                if _demo_mode[0]:
                    return 'Demo extraction'
                if EXTRACTION_CSV_PATH:
                    return Path(EXTRACTION_CSV_PATH).name
                active_rel = CURRENT_PROJECT.get('active_extraction', '')
                return Path(active_rel).name if active_rel else ''
            _ext_fname = _get_extraction_filename()
            if _ext_fname:
                ui.label(f'Extraction: {_ext_fname}').classes('text-xs text-indigo-500').style('font-style:italic;')
            highlight_notice = ui.label('').classes('text-xs text-amber-600 font-semibold')

            events_container = ui.column().classes('w-full')

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

            def _fmt_clin_date(field):
                v = field.value
                if not v or v.strip().upper() == 'NA': return
                d = re.sub(r'\D', '', v)
                if len(d) == 8:
                    field.set_value(f"{d[:4]}-{d[4:6]}-{d[6:8]}")

            clin_start_input.on('blur', lambda _: _fmt_clin_date(clin_start_input))
            clin_end_input.on('blur',   lambda _: _fmt_clin_date(clin_end_input))

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
            clinician_determined_by = ui.input(label='Determined by', placeholder='e.g. Dr. X, tumor board').classes('w-full')

            def save_clinician_event():
                row_d = df.iloc[current_patient_index]
                pid   = normalize_patient_id(row_d[PATIENT_ID_COL])
                if not clinician_agent.value:
                    ui.notify('Agent is required', color='red'); return

                if _no_progression[0]:
                    cleaned_date = 'NA'
                else:
                    if not clinician_date.value or not clinician_date.value.strip():
                        ui.notify('Enter a progression date or press NO PROGRESSION', color='red'); return
                    d = re.sub(r'\D', '', clinician_date.value or '')
                    if d and len(d) != 8: ui.notify('Date must be YYYYMMDD', color='red'); return
                    cleaned_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else None
                    if clinician_date.value and not cleaned_date: ui.notify('Invalid date.', color='red'); return

                rid = clinician_report_id.value.strip() if clinician_report_id.value.strip() else ('manual_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
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

            # ── View Extraction Data ──────────────────────────────────────────
            def show_extraction():
                SECTION_COLORS = [
                    ('#eff6ff','#1d4ed8','#dbeafe'),
                    ('#f5f3ff','#6d28d9','#ede9fe'),
                    ('#fefce8','#92400e','#fef9c3'),
                    ('#f0fdf4','#15803d','#dcfce7'),
                    ('#fff1f2','#be123c','#ffe4e6'),
                    ('#ecfeff','#0e7490','#cffafe'),
                    ('#fff7ed','#c2410c','#ffedd5'),
                    ('#f0f9ff','#0369a1','#e0f2fe'),
                ]
                RATIONALE_KEYS = {'text','note_date','author','report_id','encounter'}

                def _is_empty(v): return v is None or v == [] or v == {} or v == ''
                def _fmt(v):
                    if v is None: return '—'
                    return 'Yes' if v is True else ('No' if v is False else str(v))

                # Build the top-level keys with agents promoted
                # Flatten: pull systemic_therapy.agents to top level
                def _build_display_data(ext):
                    """Return ordered list of (section_key, display_label, data)."""
                    result = []
                    for k, v in ext.items():
                        if k == 'systemic_therapy':
                            # Add systemic_therapy section (minus agents)
                            st_data = dict(v) if isinstance(v, dict) else {}
                            agents = st_data.pop('agents', [])
                            result.append((k, 'SYSTEMIC THERAPY', st_data))
                            # Promote agents as its own top-level section
                            result.append(('__agents__', 'AGENTS', agents))
                        else:
                            result.append((k, k.upper().replace('_', ' '), v))
                    return result

                display_sections = _build_display_data(extraction)

                def _render_value(v, depth=0):
                    """Recursively render a value with appropriate indentation."""
                    pad = f'padding-left:{depth*14}px;'
                    if _is_empty(v):
                        ui.label('—').classes('ext-null').style(pad)
                    elif isinstance(v, dict):
                        is_rationale = set(v.keys()) <= RATIONALE_KEYS
                        if is_rationale:
                            non_null = {rk: rv for rk, rv in v.items() if not _is_empty(rv)}
                            if non_null:
                                parts = []
                                if non_null.get('text'): parts.append(f'"{non_null["text"]}"')
                                for rk in ('note_date', 'author', 'report_id', 'encounter'):
                                    if non_null.get(rk): parts.append(f'{rk}: {non_null[rk]}')
                                ui.label('  ·  '.join(parts)).classes('ext-rationale').style(pad + 'font-style:italic;')
                            else:
                                ui.label('—').classes('ext-null').style(pad)
                        else:
                            for dk, dv in v.items():
                                lbl = dk.replace('_', ' ')
                                with ui.row().classes('w-full items-baseline gap-2').style(f'margin:1px 0;{pad}'):
                                    ui.label(lbl + ':').classes('ext-key').style('min-width:140px;font-size:12px;')
                                    if _is_empty(dv):
                                        ui.label('—').classes('ext-null')
                                    elif isinstance(dv, (dict, list)):
                                        pass  # rendered below
                                    else:
                                        ui.label(_fmt(dv)).classes('ext-val')
                                if isinstance(dv, (dict, list)) and not _is_empty(dv):
                                    _render_value(dv, depth + 1)
                    elif isinstance(v, list):
                        if not v:
                            ui.label('(empty)').classes('ext-null').style(pad)
                        else:
                            for i, item in enumerate(v):
                                with ui.card().classes('w-full ext-list-item').style(
                                    f'{pad}margin-bottom:5px;padding:6px 10px;'
                                ):
                                    if len(v) > 1:
                                        ui.label(f'[{i+1} of {len(v)}]').classes('ext-badge')
                                    _render_value(item, 0)
                    else:
                        ui.label(_fmt(v)).classes('ext-val').style(pad)

                with ui.dialog() as ext_dlg, ui.card().classes('w-[700px]').style('max-height:90vh;padding:0;display:flex;flex-direction:column;overflow:hidden;'):
                    with ui.row().classes('w-full items-center justify-between').style('padding:14px 16px 10px;flex-shrink:0;'):
                        ui.label('Extraction Data').classes('text-base font-bold')
                        with ui.column().classes('items-end gap-0'):
                            ui.label(f'Patient {patient_id}').classes('text-xs text-gray-400')
                            _ext_fn = Path(EXTRACTION_CSV_PATH).name if EXTRACTION_CSV_PATH else (Path(CURRENT_PROJECT.get('active_extraction','')).name if CURRENT_PROJECT else '')
                            if _ext_fn and not _demo_mode[0]:
                                ui.label(_ext_fn).classes('text-xs text-indigo-400 font-mono')
                    ui.separator().style('flex-shrink:0;')

                    # Scrollable content area
                    with ui.column().classes('w-full').style('overflow-y:auto;flex:1;padding:12px 16px;gap:4px;'):
                        if not extraction:
                            ui.label('No extraction data available.').classes('text-sm text-gray-500')
                        else:
                            for si, (sec_key, sec_label, sec_data) in enumerate(display_sections):
                                bg, accent, light = SECTION_COLORS[si % len(SECTION_COLORS)]
                                with ui.expansion(sec_label).classes('w-full rounded mb-1').style(
                                    f'background:{bg};border:1.5px solid {light};overflow:visible;'
                                ).props(f'header-style="font-size:13px;font-weight:700;color:{accent};letter-spacing:0.04em;"'):
                                    with ui.column().classes('w-full gap-0').style('padding:6px 10px 8px 10px;overflow:visible;'):
                                        _render_value(sec_data, depth=0)

                    ui.separator().style('flex-shrink:0;')
                    with ui.row().style('padding:8px 16px;flex-shrink:0;gap:8px;'):
                        ui.button('Close', on_click=ext_dlg.close).props('dense')

                        # Raw JSON view
                        def _show_raw_json(ext=extraction, pid=patient_id):
                            with ui.dialog() as raw_dlg, ui.card().classes('w-[760px]').style(
                                'max-height:90vh;padding:0;display:flex;flex-direction:column;overflow:hidden;'
                            ):
                                with ui.row().classes('w-full items-center justify-between').style(
                                    'padding:12px 16px 8px;flex-shrink:0;'
                                ):
                                    ui.label('Raw JSON').classes('text-sm font-bold')
                                    ui.label(f'Patient {pid}').classes('text-xs text-gray-400')
                                ui.separator().style('flex-shrink:0;')
                                with ui.element('div').style('overflow-y:auto;flex:1;padding:12px 16px;'):
                                    def _render_json_node(obj, depth=0):
                                        indent = depth * 16
                                        pad = f'padding-left:{indent}px;'
                                        if obj is None:
                                            ui.label('null').style(f'{pad}font-family:monospace;font-size:11px;color:#9ca3af;')
                                        elif isinstance(obj, bool):
                                            ui.label(str(obj).lower()).style(f'{pad}font-family:monospace;font-size:11px;color:#7c3aed;')
                                        elif isinstance(obj, (int, float)):
                                            ui.label(str(obj)).style(f'{pad}font-family:monospace;font-size:11px;color:#0369a1;')
                                        elif isinstance(obj, str):
                                            ui.label(obj if obj else '""').style(
                                                f'{pad}font-family:monospace;font-size:11px;color:#166534;'
                                                'word-break:break-word;white-space:pre-wrap;'
                                            )
                                        elif isinstance(obj, list):
                                            if not obj:
                                                ui.label('[]').style(f'{pad}font-family:monospace;font-size:11px;color:#6b7280;')
                                            else:
                                                for i, item in enumerate(obj):
                                                    with ui.row().classes('w-full items-start gap-1').style(pad + 'margin:1px 0;'):
                                                        ui.label(f'[{i}]').style(
                                                            'font-family:monospace;font-size:10px;color:#9ca3af;'
                                                            'min-width:30px;flex-shrink:0;padding-top:1px;'
                                                        )
                                                        with ui.column().classes('flex-1 gap-0'):
                                                            _render_json_node(item, 0)
                                        elif isinstance(obj, dict):
                                            for k, v in obj.items():
                                                is_leaf = not isinstance(v, (dict, list))
                                                if is_leaf:
                                                    with ui.row().classes('w-full items-start gap-2').style(
                                                        pad + 'padding:2px 0;border-bottom:1px solid #f8fafc;'
                                                    ):
                                                        ui.label(k).style(
                                                            'font-family:monospace;font-size:10px;color:#6366f1;font-weight:600;'
                                                            'min-width:160px;max-width:220px;flex-shrink:0;word-break:break-word;'
                                                        )
                                                        _render_json_node(v, 0)
                                                else:
                                                    ui.label(k).style(
                                                        f'{pad}font-family:monospace;font-size:10px;color:#6366f1;'
                                                        'font-weight:700;padding:4px 0 2px;display:block;'
                                                    )
                                                    _render_json_node(v, depth + 1)
                                    _render_json_node(ext, 0)
                                ui.separator().style('flex-shrink:0;')
                                with ui.row().style('padding:8px 16px;flex-shrink:0;'):
                                    ui.button('Close', on_click=raw_dlg.close).props('dense')
                            raw_dlg.open()

                        ui.button('Raw JSON', on_click=_show_raw_json).props('dense outline').classes('text-gray-500')
                ext_dlg.open()

            ui.button('View Extraction Data', on_click=show_extraction).props('dense outline').classes('w-full text-gray-600')
            ui.element('div').style('height:800px;')  # breathing room — doubled again

        # ── Right panel ───────────────────────────────────────────────────────
        with right_panel:
            ui.label('All Relevant Notes').classes('text-lg font-bold mb-2')
            ui.html('<div id="note-sticky-header"></div>')
            ui.html(notes_html).classes('w-full')
            _inject_sticky_header_js()

        # ── Search bar JS (injected after render) ─────────────────────────────
        ui.timer(0.05, lambda: ui.run_javascript("""
(function(){
  var old = document.getElementById('ws-strip');
  if (old) old.remove();
  var _prev = window._WS_state || {open:false,query:'',nlp:true,disabled:{},aliasVisible:false};

  // Load cmd-f preference (default: enabled)
  if(typeof window._watneyCmdfEnabled === 'undefined'){
    window._watneyCmdfEnabled = true;
  }

  var ad = document.createElement('div'); ad.id='ws-alias';
  ad.innerHTML =
    '<span style="font-size:11px;font-weight:600;color:#1e40af;white-space:nowrap;margin-right:4px;">Also searching:</span>'+
    '<span id="ws-chips" style="display:flex;flex-wrap:wrap;gap:3px;align-items:center;flex:1;"></span>';

  var bd = document.createElement('div'); bd.id='ws-bar';
  bd.innerHTML =
    '<input id="ws-input" type="text" placeholder="Search in notes\u2026" autocomplete="off" spellcheck="false"/>'+
    '<span id="ws-count"></span>'+
    '<button id="ws-nlp" title="Toggle drug alias expansion" style="'+
      'border:1.5px solid #bfdbfe;border-radius:6px;background:#dbeafe;color:#1d4ed8;'+
      'font-size:11px;font-family:Arial;padding:4px 8px;cursor:pointer;white-space:nowrap;'+
      'font-weight:600;line-height:1;transition:all 0.15s;">NLP \u2713</button>'+
    '<button class="ws-btn" id="ws-prev" title="Previous (Up arrow)">\u25b2</button>'+
    '<button class="ws-btn" id="ws-next" title="Next (Down arrow)">\u25bc</button>'+
    '<button class="ws-btn" id="ws-x" style="border-color:#fecaca;color:#f87171;" title="Close">\u00d7</button>';

  var strip = document.createElement('div'); strip.id='ws-strip';
  strip.appendChild(ad); strip.appendChild(bd);
  document.body.appendChild(strip);

  // Called from Python scroll_to_note to gray out search — saves cur position
  window._watneyGraySearch = function(){
    if(strip.style.display==='flex') strip.classList.add('ws-grayed');
    var inp2 = document.getElementById('ws-input');
    if(inp2) inp2.disabled = true;
    // cur position is already saved in the outer `cur` variable
  };
  window._watneyUngraySearch = function(){
    strip.classList.remove('ws-grayed');
    var inp2 = document.getElementById('ws-input');
    if(inp2) inp2.disabled = false;
  };

  // Grayed state: when source-highlight is active, gray out search bar
  // Clicking ANYWHERE on the strip (bar or label above) un-grays and restores position
  function _ungray(){
    if(!strip.classList.contains('ws-grayed')) return;
    strip.classList.remove('ws-grayed');
    document.querySelectorAll('.evidence-highlight').forEach(function(el){ el.style.background=''; });
    var inp2 = document.getElementById('ws-input');
    if(inp2){ inp2.disabled=false; }
    // Re-run search to re-highlight (evidence-highlight clear wiped DOM), then jump to saved cur
    var savedCur = cur;
    run();
    // After run() resets cur to 0, jump back to where we were
    if(marks.length && savedCur > 0 && savedCur < marks.length){
      cur = savedCur;
      goTo(cur);
    }
  }
  // Listen on the strip itself so the "click to resume search" label area works too
  strip.addEventListener('click', _ungray);
  strip.addEventListener('mousedown', function(e){
    if(strip.classList.contains('ws-grayed')){ e.preventDefault(); _ungray(); }
  });

  var DRUGS = window._watneyDrugs || {};
  var RM    = window._watneyAliasMap || {};
  function resolve(q) {
    var qL = q.toLowerCase();
    // Use the pre-built global resolver if available
    if(window._watneyResolveQuery){
      var gr = window._watneyResolveQuery(q);
      // gr.aliases = all terms except q itself
      // We want terms = all including canonical
      var c = (window._watneyAliasMap||{})[qL] || null;
      var allTerms = c ? [c].concat((window._watneyDrugs||{})[c] || []) : [q];
      var seen={};
      allTerms = allTerms.filter(function(t){var k=t.toLowerCase();return seen[k]?false:(seen[k]=true);});
      return { terms: allTerms, aliases: allTerms.filter(function(t){return t.toLowerCase()!==qL;}) };
    }
    var c = RM[qL] || null;
    if (!c) return { terms:[q], aliases:[] };
    var all = [c].concat(DRUGS[c] || []);
    var seen = {};
    all = all.filter(function(t){ var k=t.toLowerCase(); return seen[k]?false:(seen[k]=true); });
    return { terms:all, aliases:all.filter(function(t){ return t.toLowerCase()!==qL; }) };
  }

  var marks=[], cur=-1, nlpOn=_prev.nlp, disabled=_prev.disabled;

  // Escape regex special chars (no space escaping — notes may have any whitespace)
  function esc(s){
    return s.replace(/[.*+?^${}()|[\]\\\\]/g, '\\\\$&');
  }

  function clearHL(){
    document.querySelectorAll('.hl').forEach(function(el){
      el.parentNode.replaceChild(document.createTextNode(el.textContent),el);
    });
    try{document.querySelector('.right-pane').normalize();}catch(e){}
    marks=[]; cur=-1;
    var ct=document.getElementById('ws-count'); if(ct) ct.textContent='';
    _clearScrollMarkers();
  }

  // Build regex: if isDrug, wrap term with word-boundary lookarounds so
  // "bev" won't match inside "beverly". Non-drug free-text matches anywhere.
  function buildPattern(term, isDrug){
    var e = esc(term);
    if(isDrug){
      // (?<![a-zA-Z0-9]) and (?![a-zA-Z0-9]) — supported in all modern browsers
      return '(?<![a-zA-Z0-9])' + e + '(?![a-zA-Z0-9])';
    }
    return e;
  }

  function doSearch(terms, isDrug){
    clearHL();
    if(!terms||!terms.length||!terms[0]) return;
    var patterns = terms.map(function(t){ return buildPattern(t, isDrug); });
    var re;
    try{ re = new RegExp('(' + patterns.join('|') + ')', 'gi'); }
    catch(e){ return; }
    var rp = document.querySelector('.right-pane'); if(!rp) return;
    var walker = document.createTreeWalker(rp, NodeFilter.SHOW_TEXT, null, false);
    var nodes = [], n;
    while((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function(tn){
      var t = tn.textContent;
      if(!re.test(t)){ re.lastIndex=0; return; }
      re.lastIndex = 0;
      var frag = document.createDocumentFragment(), last = 0, m;
      while((m = re.exec(t)) !== null){
        if(m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
        var sp = document.createElement('span');
        sp.className = 'hl'; sp.textContent = m[0];
        frag.appendChild(sp); marks.push(sp);
        last = m.index + m[0].length;
      }
      if(last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
      tn.parentNode.replaceChild(frag, tn);
    });
    var ct = document.getElementById('ws-count');
    if(ct) ct.textContent = marks.length ? ('1/' + marks.length) : '0 results';
    if(marks.length){ cur = 0; goTo(0); }
    _renderScrollMarkers();
  }

  // Render thin tick marks on the right-pane scrollbar track showing where highlights are
  function _renderScrollMarkers(){
    var rp = document.querySelector('.right-pane');
    var container = document.getElementById('ws-scroll-markers');
    if(!rp){ if(container) container.remove(); return; }
    // Create or reuse marker container — sits as a fixed overlay beside the scrollbar
    if(!container){
      container = document.createElement('div');
      container.id = 'ws-scroll-markers';
      container.style.cssText =
        'position:fixed;top:0;width:8px;background:transparent;pointer-events:none;z-index:199;';
      document.body.appendChild(container);
    }
    container.innerHTML = '';
    if(!marks.length){
      container.style.display='none'; return;
    }
    var rpRect = rp.getBoundingClientRect();
    var scrollH = rp.scrollHeight;
    var clientH = rp.clientHeight;
    container.style.display = 'block';
    container.style.left    = (rpRect.right - 6) + 'px';
    container.style.top     = rpRect.top + 'px';
    container.style.height  = rpRect.height + 'px';
    marks.forEach(function(mk){
      var mkRect = mk.getBoundingClientRect();
      // Position relative to scroll content
      var absTop = mkRect.top - rpRect.top + rp.scrollTop;
      var pct = absTop / scrollH;
      var tickTop = pct * rpRect.height;
      var tick = document.createElement('div');
      tick.style.cssText =
        'position:absolute;left:0;width:8px;height:5px;border-radius:2px;'+
        'background:#f97316;opacity:0.75;'+
        'top:' + tickTop + 'px;';
      container.appendChild(tick);
    });
  }

  function _clearScrollMarkers(){
    var c = document.getElementById('ws-scroll-markers');
    if(c){ c.innerHTML=''; c.style.display='none'; }
  }

  function goTo(i){
    marks.forEach(function(m){m.classList.remove('cur');});
    if(!marks.length) return;
    marks[i].classList.add('cur');
    marks[i].scrollIntoView({behavior:'smooth',block:'center'});
    var ct=document.getElementById('ws-count');
    if(ct) ct.textContent=(i+1)+'/'+marks.length;
    // Highlight the active tick in the scrollbar overlay
    var ticks = document.querySelectorAll('#ws-scroll-markers div');
    ticks.forEach(function(t,ti){
      t.style.background = (ti===i) ? '#ef4444' : '#f97316';
      t.style.opacity    = (ti===i) ? '1' : '0.65';
      t.style.height     = (ti===i) ? '8px' : '5px';
    });
  }
  function step(d){if(!marks.length) return; cur=(cur+d+marks.length)%marks.length; goTo(cur);}

  function buildChips(aliases){
    var c=document.getElementById('ws-chips'); if(!c) return; c.innerHTML='';
    aliases.forEach(function(a){
      var off=!!disabled[a];
      var b=document.createElement('button');
      b.textContent=a; b.title=(off?'Include ':'Exclude ')+a;
      b.style.cssText='border-radius:12px;padding:1px 8px;font-size:10px;cursor:pointer;'+
        'border:1px solid '+(off?'#e2e8f0':'#bfdbfe')+';'+
        'background:'+(off?'#f1f5f9':'#eff6ff')+';'+
        'color:'+(off?'#94a3b8':'#3b82f6')+';'+
        'text-decoration:'+(off?'line-through':'none')+';';
      b.onclick=function(){ disabled[a]=!disabled[a]; run(); };
      c.appendChild(b);
    });
  }

  var _lastQ = '';  // track query changes to reset disabled state
  function run(){
    var inp=document.getElementById('ws-input'); if(!inp||inp.disabled) return;
    var q=inp.value.trim();
    if(!q){clearHL(); ad.style.display='none'; _lastQ=''; saveState(); return;}
    // Reset disabled chips when the search term changes
    if(q.toLowerCase() !== _lastQ.toLowerCase()){ disabled={}; }
    _lastQ = q;
    var r = resolve(q);
    // isDrug: true when the query resolves to a known drug (has aliases)
    var isDrug = nlpOn && r.aliases && r.aliases.length > 0;
    var active = [q];
    if(isDrug){
      buildChips(r.aliases); ad.style.display='flex';
      r.aliases.forEach(function(a){ if(!disabled[a]) active.push(a); });
    } else {
      ad.style.display='none';
    }
    // Deduplicate case-insensitively
    var seen={};
    active=active.filter(function(t){var k=t.toLowerCase(); return seen[k]?false:(seen[k]=true);});
    doSearch(active, isDrug);
    saveState();  // always keep state current
  }

  var inp=document.getElementById('ws-input');
  if(inp) inp.addEventListener('input',run);
  var prevBtn=document.getElementById('ws-prev');
  var nextBtn=document.getElementById('ws-next');
  var xBtn=document.getElementById('ws-x');
  if(prevBtn) prevBtn.addEventListener('click',function(){step(-1);});
  if(nextBtn) nextBtn.addEventListener('click',function(){step(1);});
  if(xBtn)    xBtn.addEventListener('click',function(){window.WS&&window.WS.close();});

  var nlpBtn=document.getElementById('ws-nlp');
  if(nlpBtn){
    nlpBtn.textContent='NLP '+(nlpOn?'\u2713':'\u2715');
    nlpBtn.style.background=nlpOn?'#dbeafe':'#f1f5f9';
    nlpBtn.style.borderColor=nlpOn?'#bfdbfe':'#e2e8f0';
    nlpBtn.style.color=nlpOn?'#1d4ed8':'#64748b';
    nlpBtn.addEventListener('click',function(){
      nlpOn=!nlpOn; disabled={};
      this.textContent='NLP '+(nlpOn?'\u2713':'\u2715');
      this.style.background=nlpOn?'#dbeafe':'#f1f5f9';
      this.style.borderColor=nlpOn?'#bfdbfe':'#e2e8f0';
      this.style.color=nlpOn?'#1d4ed8':'#64748b';
      run();
    });
  }

  // ── Keyboard handler for arrows/Enter/Escape inside the search bar ────────
  // NOTE: Cmd/Ctrl-F is handled by a GLOBAL one-time listener (see add_head_html)
  // to avoid accumulating duplicate handlers across patient navigations.
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'&&strip.style.display==='flex'){
      window.WS&&window.WS.close(); return;
    }
    if(strip.style.display==='flex' && !strip.classList.contains('ws-grayed')){
      if(e.key==='ArrowDown'){e.preventDefault();step(1);}
      else if(e.key==='ArrowUp'){e.preventDefault();step(-1);}
      else if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();step(1);}
      else if(e.key==='Enter'&&e.shiftKey){e.preventDefault();step(-1);}
    }
  }, true);

  if(_prev.open){
    strip.style.display='flex';
    var _ri=document.getElementById('ws-input');
    if(_ri&&_prev.query){
      _ri.value=_prev.query;
      // Small delay so right-pane DOM is fully painted before searching
      setTimeout(function(){ run(); }, 80);
    }
  }

  function saveState(){
    var ii=document.getElementById('ws-input');
    window._WS_state={
      open:strip.style.display==='flex',
      query:ii?ii.value:'',
      nlp:nlpOn, disabled:disabled,
      aliasVisible:ad.style.display==='flex'
    };
  }
  if(inp) inp.addEventListener('input',saveState);
  // Also save whenever the strip visibility changes (navigation, close)
  window._watneySearchSaveState = saveState;

  window.WS={
    open:function(){
      if(strip.style.display==='flex' && !strip.classList.contains('ws-grayed')){
        saveState();window.WS.close();return;
      }
      // If grayed, ungrayed and focus
      strip.classList.remove('ws-grayed');
      var inp2=document.getElementById('ws-input');
      if(inp2) inp2.disabled=false;
      strip.style.display='flex';
      setTimeout(function(){var ii=document.getElementById('ws-input');if(ii){ii.focus();ii.select();}},50);
    },
    close:function(){
      saveState(); window._WS_state.open=false; clearHL();
      strip.style.display='none'; ad.style.display='none';
      strip.classList.remove('ws-grayed');
      var c=document.getElementById('ws-chips'); if(c) c.innerHTML='';
      var ii=document.getElementById('ws-input'); if(ii){ ii.value=''; ii.disabled=false; }
      disabled={};
    }
  };
})();
"""), once=True)

    # ── Navigation ────────────────────────────────────────────────────────────
    def _save_search_state():
        """Call JS saveState before render_patient tears down the DOM."""
        ui.run_javascript('if(window._watneySearchSaveState) window._watneySearchSaveState();')

    def next_patient():
        global current_patient_index
        if current_patient_index < len(df) - 1:
            _save_search_state()
            current_patient_index += 1; render_patient(current_patient_index)

    def prev_patient():
        global current_patient_index
        if current_patient_index > 0:
            _save_search_state()
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
        global CURRENT_PROJECT, PROJECT_DIR
        _reset_demo_conn()
        _demo_mode[0] = False
        CURRENT_USER   = None
        UI_LOCKED      = True
        CURRENT_PROJECT = {}
        PROJECT_DIR    = None
        EXTRACTION_CSV_PATH = None
        df = None
        # Cancel checkpoint timer if running
        if _checkpoint_timer_holder[0] is not None:
            try: _checkpoint_timer_holder[0].cancel()
            except Exception: pass
            _checkpoint_timer_holder[0] = None
        # Hide floating JS widgets BEFORE clearing panels (slot still valid)
        ui.run_javascript(
            "var ws=document.getElementById('ws-strip');"
            "if(ws){ws.style.display='none';window._WS_state={open:false,query:'',nlp:true,disabled:{}}; }"
            "var sh=document.getElementById('note-sticky-header');if(sh)sh.style.display='none';"
            ""
        )
        # Clear panels
        left_panel.clear()
        right_panel.clear()
        # Reset login form widgets (these are NiceGUI elements, slot still valid via lock_overlay)
        user_select.set_value('')
        name_error.set_text('')
        step1.set_visibility(True)
        step2.set_visibility(False)
        lock_overlay.set_visibility(True)
        _login_bottom.set_visibility(True)
        if nav_bar is not None: nav_bar.style('display:none')

    async def _search_open():
        await ui.run_javascript('if(window.WS)window.WS.open()')

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
            ui.button('Search',   on_click=_search_open).props('dense outline')
            ui.button('Patients', on_click=_show_patient_list).props('dense outline')
            ui.button('Export',   on_click=_export_csv).props('dense outline')
            ui.button('Settings', on_click=_show_settings).props('dense outline')

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
# EASTER EGG PAGE — self-contained, no Python callbacks needed
# =============================================================================

@ui.page('/easter-egg')
def easter_egg_page():
    """Standalone Easter egg page; navigated to when 'Mark Watney' is entered as username.

    Self-contained HTML/CSS/JS with no Python callbacks. Both navigation
    buttons perform window.location.replace('/') to return to the main page.
    """
    """Standalone Mars-themed easter egg. Pure HTML/CSS/JS — no polling timers."""
    ui.add_head_html('''<style>
*{margin:0;padding:0;box-sizing:border-box;}
html,body{width:100%;height:100%;overflow:hidden;background:#000;}
#mars-root{width:100vw;height:100vh;position:relative;overflow:hidden;
  background:radial-gradient(ellipse at center,#1c0800 0%,#0a0200 55%,#000 100%);
  display:flex;flex-direction:column;}
#ee-stars{position:absolute;inset:0;pointer-events:none;z-index:0;}
.planet{position:absolute;top:3%;right:8%;width:200px;height:200px;border-radius:50%;z-index:1;
  background:radial-gradient(circle at 32% 28%,#e8713a 0%,#c04a1a 35%,#8b2800 65%,#3d1000 100%);
  box-shadow:0 0 70px rgba(220,100,30,.45),inset -24px -16px 36px rgba(0,0,0,.6);
  animation:planet-float 8s ease-in-out infinite;}
@keyframes planet-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
@keyframes twinkle{0%,100%{opacity:.2}50%{opacity:1}}
.crater{position:absolute;border-radius:50%;}
.polar{position:absolute;top:3%;left:28%;right:28%;height:18px;border-radius:50%;
  background:rgba(255,255,255,.07);}
.top-bar{position:relative;z-index:10;display:flex;justify-content:space-between;
  align-items:center;padding:18px 22px 0;flex-shrink:0;}
.nav-btns{display:flex;gap:12px;}
.ee-btn{background:transparent;border-radius:5px;font-size:11px;padding:5px 14px;
  cursor:pointer;font-family:monospace;letter-spacing:.06em;transition:all .22s;white-space:nowrap;}
#btn-logout{border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.5);}
#btn-logout:hover{color:rgba(255,255,255,.85);border-color:rgba(255,255,255,.45);}
#btn-continue{border:1px solid rgba(200,80,20,.45);color:rgba(212,98,42,.8);font-size:10.5px;}
#btn-continue:hover{color:#f59e0b;border-color:rgba(212,98,42,.8);}
.sol-stamp{font-family:monospace;font-size:10px;color:rgba(180,70,20,.55);letter-spacing:.12em;}
.log-panel{position:relative;z-index:10;flex:1;overflow-y:auto;
  display:flex;flex-direction:column;justify-content:center;
  padding:24px 11% 32px;max-width:900px;margin:0 auto;width:100%;}
.log-label{font-family:monospace;font-size:10px;font-weight:700;
  color:rgba(200,70,20,.65);letter-spacing:.16em;margin-bottom:18px;}
.log-heading{font-family:monospace;font-size:15px;font-weight:800;
  color:#f59e0b;letter-spacing:.05em;margin-bottom:18px;}
.log-body{font-family:monospace;font-size:13px;line-height:1.9;color:#d4b896;}
.log-body p{margin:0 0 12px;}
.log-body p.italic{font-style:italic;color:#fde68a;font-size:13.5px;}
.log-body p.last{color:#fbbf24;font-style:italic;margin:0;}
.log-footer{margin-top:24px;font-family:monospace;font-size:9px;
  color:rgba(180,70,20,.35);letter-spacing:.18em;text-align:center;}
</style>''')

    # HTML only — no <script> tags allowed in ui.html()
    ui.html('''
<div id="mars-root">
  <div id="ee-stars"></div>
  <div class="planet">
    <div class="crater" style="top:22%;left:18%;width:48px;height:19px;background:rgba(80,20,0,.44);transform:rotate(-18deg);"></div>
    <div class="crater" style="top:55%;left:58%;width:30px;height:12px;background:rgba(60,15,0,.38);transform:rotate(8deg);"></div>
    <div class="crater" style="top:70%;left:24%;width:62px;height:22px;background:rgba(70,18,0,.36);transform:rotate(12deg);"></div>
    <div class="crater" style="top:38%;left:65%;width:20px;height:20px;background:rgba(50,10,0,.32);"></div>
    <div class="polar"></div>
  </div>
  <div class="top-bar">
    <div class="nav-btns">
      <button class="ee-btn" id="btn-logout">&#8592; logout</button>
      <button class="ee-btn" id="btn-continue">My name is actually Mark Watney</button>
    </div>
    <div class="sol-stamp">SOL 6 &middot; ARES III</div>
  </div>
  <div class="log-panel">
    <div class="log-label">&#9654;&nbsp; PERSONAL LOG &mdash; WATNEY, MARK &mdash; ARES III</div>
    <div class="log-heading">LOG ENTRY: SOL 6</div>
    <div class="log-body">
      <p class="italic">I&rsquo;m pretty much screwed.</p>
      <p>That&rsquo;s my considered opinion.<br>Screwed.</p>
      <p>Six days in to what should be the greatest two months of my life, and it&rsquo;s turned in to a nightmare.</p>
      <p>I don&rsquo;t even know who&rsquo;ll read this. I guess someone will find it eventually. Maybe a hundred years from now.</p>
      <p>For the record... I didn&rsquo;t die on Sol&nbsp;6. Certainly the rest of the crew thought I did, and I can&rsquo;t blame them. Maybe there&rsquo;ll be a day of national mourning for me, and my Wikipedia page will say &ldquo;Mark Watney is the only human being to have died on Mars.&rdquo;</p>
      <p>And it&rsquo;ll be right, probably. Cause I&rsquo;ll surely die here. Just not on Sol&nbsp;6 when everyone thinks I did.</p>
      <p class="last">Let&rsquo;s see... where do I begin?</p>
    </div>
    <div class="log-footer">WATNEY ONCOLOGY ANNOTATION PLATFORM &middot; v6 &middot; ARES III MISSION LOG</div>
  </div>
</div>
''')

    # Stars + button navigation — add_body_html required for scripts
    ui.add_body_html('''<script>
(function(){
  function goHome(){ window.location.replace('/'); }

  // Stars
  function initStars(){
    var sc = document.getElementById('ee-stars');
    if(!sc){ setTimeout(initStars, 50); return; }
    for(var i=0;i<220;i++){
      var s=document.createElement('div');
      var sz=Math.random()<0.04?3:Math.random()<0.18?2:1;
      s.style.cssText='position:absolute;border-radius:50%;background:#fff;'+
        'width:'+sz+'px;height:'+sz+'px;'+
        'left:'+Math.random()*100+'%;top:'+Math.random()*100+'%;'+
        'opacity:'+(0.15+Math.random()*0.75)+';'+
        'animation:twinkle '+(2+Math.random()*4)+'s '+(Math.random()*3)+'s ease-in-out infinite;';
      sc.appendChild(s);
    }
  }

  // Buttons — retry until DOM is ready
  function wireButtons(){
    var lo = document.getElementById('btn-logout');
    var co = document.getElementById('btn-continue');
    if(!lo || !co){ setTimeout(wireButtons, 50); return; }
    lo.addEventListener('click', function(){ window.location.replace('/'); });
    co.addEventListener('click', function(){ window.location.replace('/?ee_user=Mark+Watney'); });
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', function(){ initStars(); wireButtons(); });
  } else {
    initStars(); wireButtons();
  }
})();
</script>''')


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    """Launch the WATNEY NiceGUI server.

    Reads watney_icon.ico from the package directory if present. Runs with
    reload=False to avoid double-initialisation of module-level state.
    Call this function directly or via the 'watney' console script entry point.
    """
    _favicon = Path(__file__).parent / 'watney_icon.ico'
    _favicon_arg = str(_favicon) if _favicon.exists() else None
    try:
        ui.run(title='WATNEY', reload=False, favicon=_favicon_arg)
    except KeyboardInterrupt:
        pass

if __name__ in {'__main__', '__mp_main__', '<run_path>'}:
    main()