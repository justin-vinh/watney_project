"""
project.py — WATNEY Phase 1 project management.

Handles project folder structure, project.json, patients.db,
extraction versioning, and checkpoint scheduling.

Used by watney_v6.py:
    from project import (
        load_global_config, save_global_config,
        create_project, open_project, load_extraction_into_project,
        get_project_annotations_db_path, get_project_exports_dir,
        get_project_checkpoints_dir, do_checkpoint,
        ProjectError,
    )
"""

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# =============================================================================
# CONSTANTS
# =============================================================================

REQUIRED_CSV_COLUMNS = {'DFCI_MRN', 'generation', 'all_notes'}
PATIENT_ID_COL = 'DFCI_MRN'
MAX_CHECKPOINTS = 20

# =============================================================================
# GLOBAL CONFIG  (~/.watney/config.json  or  ./watney_global_config.json)
# =============================================================================

def _global_config_path() -> Path:
    """Return path to global config, preferring ~/.watney/."""
    try:
        home_cfg = Path.home() / '.watney' / 'config.json'
        home_cfg.parent.mkdir(parents=True, exist_ok=True)
        return home_cfg
    except Exception:
        return Path('./watney_global_config.json')


def load_global_config() -> dict:
    p = _global_config_path()
    if p.exists():
        try:
            with open(p, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_global_config(cfg: dict):
    p = _global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        json.dump(cfg, f, indent=2)


def get_recent_projects(max_items: int = 8) -> list:
    """
    Return a list of recent project dicts, most-recent first.
    Each entry: {'path': str, 'name': str}
    Silently drops entries where project.json no longer exists.
    """
    cfg = load_global_config()
    raw = cfg.get('recent_projects', [])
    valid = []
    for entry in raw:
        p = Path(entry.get('path', ''))
        if (p / 'project.json').exists():
            try:
                with open(p / 'project.json', 'r') as f:
                    meta = json.load(f)
                valid.append({'path': str(p), 'name': meta.get('project_name', p.name)})
            except Exception:
                pass
    return valid[:max_items]


def record_recent_project(project_dir: Path, project_name: str):
    """Add or promote a project to the top of the recent_projects list."""
    cfg = load_global_config()
    path_str = str(project_dir.resolve())
    existing = [e for e in cfg.get('recent_projects', []) if e.get('path') != path_str]
    cfg['recent_projects'] = [{'path': path_str, 'name': project_name}] + existing[:15]
    cfg['last_project'] = path_str
    save_global_config(cfg)


# =============================================================================
# EXCEPTIONS
# =============================================================================

class ProjectError(Exception):
    """Raised for user-facing project management errors."""
    pass


# =============================================================================
# PROJECT STRUCTURE HELPERS
# =============================================================================

def _project_json_path(project_dir: Path) -> Path:
    return project_dir / 'project.json'


def _patients_db_path(project_dir: Path) -> Path:
    return project_dir / 'patients.db'


def get_project_annotations_db_path(project_dir: Path) -> Path:
    return project_dir / 'annotations.db'


def get_project_exports_dir(project_dir: Path) -> Path:
    d = project_dir / 'exports'
    d.mkdir(exist_ok=True)
    return d


def get_project_checkpoints_dir(project_dir: Path) -> Path:
    d = project_dir / 'checkpoints'
    d.mkdir(exist_ok=True)
    return d


def _extractions_dir(project_dir: Path) -> Path:
    d = project_dir / 'extractions'
    d.mkdir(exist_ok=True)
    return d


def _read_project_json(project_dir: Path) -> dict:
    p = _project_json_path(project_dir)
    if not p.exists():
        raise ProjectError(f'No project.json found in {project_dir}')
    with open(p, 'r') as f:
        return json.load(f)


def _write_project_json(project_dir: Path, data: dict):
    with open(_project_json_path(project_dir), 'w') as f:
        json.dump(data, f, indent=2)


# =============================================================================
# PATIENTS.DB
# =============================================================================

def _open_patients_db(project_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_patients_db_path(project_dir)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            DFCI_MRN    TEXT PRIMARY KEY,
            date_added  TEXT NOT NULL,
            active      INTEGER DEFAULT 1,
            cohort_notes TEXT
        )
    """)
    conn.commit()
    return conn


def _populate_patients_db(project_dir: Path, df: 'pd.DataFrame'):
    """Insert MRNs from df into patients.db; skip duplicates (never delete)."""
    conn = _open_patients_db(project_dir)
    now = datetime.now().isoformat(timespec='seconds')
    added = 0
    for mrn in df[PATIENT_ID_COL].astype(str).str.strip():
        if not mrn or mrn.lower() == 'nan':
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO patients (DFCI_MRN, date_added) VALUES (?, ?)",
                (mrn, now)
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                added += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return added


# =============================================================================
# ANNOTATIONS.DB  (same schema as legacy watney_annotations_database.db)
# =============================================================================

_ANNOTATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    DFCI_MRN             TEXT NOT NULL,
    progression_date     TEXT,
    progression_source   TEXT,
    agent                TEXT,
    evidence             TEXT,
    report_id            TEXT,
    determined_by        TEXT,
    user                 TEXT,
    modification_timestamp TEXT,
    agent_start          TEXT,
    agent_start_source   TEXT,
    agent_end            TEXT,
    agent_end_source     TEXT,
    exclusion_flag       TEXT,
    exclusion_reason     TEXT,
    extraction_version   TEXT,
    deleted              INTEGER DEFAULT 0,
    deletion_reason      TEXT,
    deletion_timestamp   TEXT,
    import_source        TEXT
)
"""

_ANNOTATIONS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_progression_event
ON annotations (DFCI_MRN, progression_date, progression_source, report_id)
"""

_ANNOTATIONS_OPTIONAL_COLS = [
    ('agent_start',        'TEXT'),
    ('agent_start_source', 'TEXT'),
    ('agent_end',          'TEXT'),
    ('agent_end_source',   'TEXT'),
    ('exclusion_flag',     'TEXT'),
    ('exclusion_reason',   'TEXT'),
    ('extraction_version', 'TEXT'),
    # Phase 2/3 additions
    ('deleted',            'INTEGER'),
    ('deletion_reason',    'TEXT'),
    ('deletion_timestamp', 'TEXT'),
    ('import_source',      'TEXT'),
]


def open_annotations_db(project_dir: Path) -> sqlite3.Connection:
    """Open (or create) annotations.db inside project_dir with WAL mode."""
    db_path = get_project_annotations_db_path(project_dir)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(_ANNOTATIONS_SCHEMA)
    conn.execute(_ANNOTATIONS_INDEX)
    for col, typ in _ANNOTATIONS_OPTIONAL_COLS:
        try:
            conn.execute(f'ALTER TABLE annotations ADD COLUMN {col} {typ}')
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


# =============================================================================
# CREATE PROJECT
# =============================================================================

def create_project(project_dir: Path, project_name: str,
                   csv_path: Path) -> dict:
    """
    Create a new project folder from scratch.

    Returns the project metadata dict (same as project.json contents).
    Raises ProjectError on any validation failure.
    """
    # Validate CSV
    if not csv_path.exists():
        raise ProjectError(f'CSV not found: {csv_path}')
    try:
        df = pd.read_csv(str(csv_path), dtype={PATIENT_ID_COL: str})
    except Exception as e:
        raise ProjectError(f'Could not read CSV: {e}')
    missing = REQUIRED_CSV_COLUMNS - set(df.columns)
    if missing:
        raise ProjectError(f'CSV missing required columns: {missing}')

    # Create folder structure
    project_dir.mkdir(parents=True, exist_ok=True)
    _extractions_dir(project_dir)
    get_project_checkpoints_dir(project_dir)
    get_project_exports_dir(project_dir)

    # Copy CSV — prefix with version number: 1_originalfilename.csv
    orig_name = csv_path.name
    ext_filename = f'1_{orig_name}'
    ext_dest = _extractions_dir(project_dir) / ext_filename
    shutil.copy2(str(csv_path), str(ext_dest))

    # Build project.json
    now_iso = datetime.now().isoformat(timespec='seconds')
    ext_rel = f'extractions/{ext_filename}'
    meta = {
        'project_name':               project_name,
        'created':                    now_iso,
        'last_opened':                now_iso,
        'active_extraction':          ext_rel,
        'extractions': [
            {
                'filename':    ext_rel,
                'loaded_date': now_iso,
                'row_count':   len(df),
                'label':       'v1 — initial run',
            }
        ],
        'checkpoint_interval_minutes': 30,
        'last_checkpoint':            None,
        'watney_version':             '6',
    }
    _write_project_json(project_dir, meta)

    # Populate patients.db
    _populate_patients_db(project_dir, df)

    # Create annotations.db
    open_annotations_db(project_dir).close()

    return meta


# =============================================================================
# OPEN PROJECT
# =============================================================================

def open_project(project_dir: Path) -> tuple:
    """
    Open an existing project.

    Returns (meta: dict, df: pd.DataFrame, annotations_conn: sqlite3.Connection).
    Raises ProjectError on failure.
    """
    if not project_dir.is_dir():
        raise ProjectError(f'Not a directory: {project_dir}')

    # ── Legacy migration check ────────────────────────────────────────────────
    # If this looks like a legacy watney_annotations/ folder, reject with helpful message.
    if (project_dir / 'watney_annotations_database.db').exists() and \
       not _project_json_path(project_dir).exists():
        raise ProjectError(
            f'LEGACY_FOLDER:{project_dir}'
        )

    meta = _read_project_json(project_dir)

    # Load active extraction
    ext_rel = meta.get('active_extraction')
    if not ext_rel:
        raise ProjectError('project.json has no active_extraction field')
    ext_path = project_dir / ext_rel
    if not ext_path.exists():
        raise ProjectError(f'Active extraction CSV not found: {ext_path}')
    try:
        df = pd.read_csv(str(ext_path), dtype={PATIENT_ID_COL: str})
    except Exception as e:
        raise ProjectError(f'Could not load extraction CSV: {e}')

    # Update last_opened
    meta['last_opened'] = datetime.now().isoformat(timespec='seconds')
    _write_project_json(project_dir, meta)

    # Open annotations.db
    conn = open_annotations_db(project_dir)

    return meta, df, conn


# =============================================================================
# LOAD NEW EXTRACTION INTO EXISTING PROJECT
# =============================================================================

def load_extraction_into_project(project_dir: Path,
                                  new_csv_path: Path) -> dict:
    """
    Add a new extraction version to an existing project.

    - Copies CSV to extractions/extraction_vN_YYYYMMDD.csv
    - Updates project.json active_extraction + extractions list
    - Adds new MRNs to patients.db (never removes existing)
    - Does NOT touch annotations.db

    Returns a summary dict:
        { 'new_patients': int, 'changed_patients': int,
          'ext_filename': str, 'df': pd.DataFrame }
    Raises ProjectError on validation failure.
    """
    if not new_csv_path.exists():
        raise ProjectError(f'CSV not found: {new_csv_path}')
    try:
        new_df = pd.read_csv(str(new_csv_path), dtype={PATIENT_ID_COL: str})
    except Exception as e:
        raise ProjectError(f'Could not read CSV: {e}')
    missing = REQUIRED_CSV_COLUMNS - set(new_df.columns)
    if missing:
        raise ProjectError(f'CSV missing required columns: {missing}')

    meta = _read_project_json(project_dir)

    # Determine next version number
    existing = meta.get('extractions', [])
    next_v = len(existing) + 1

    # Prefix with version number: N_originalfilename.csv
    orig_name = new_csv_path.name
    ext_filename = f'{next_v}_{orig_name}'
    ext_dest = _extractions_dir(project_dir) / ext_filename
    # Avoid collision if file already exists (same source loaded twice)
    while ext_dest.exists():
        next_v += 1
        ext_filename = f'{next_v}_{orig_name}'
        ext_dest = _extractions_dir(project_dir) / ext_filename

    shutil.copy2(str(new_csv_path), str(ext_dest))

    # Compare with previous extraction to count changed patients
    changed = 0
    prev_ext_rel = meta.get('active_extraction')
    if prev_ext_rel:
        prev_path = project_dir / prev_ext_rel
        if prev_path.exists():
            try:
                old_df = pd.read_csv(str(prev_path), dtype={PATIENT_ID_COL: str})
                old_map = dict(zip(
                    old_df[PATIENT_ID_COL].astype(str).str.strip(),
                    old_df.get('generation', pd.Series(dtype=str)).astype(str)
                ))
                for _, row in new_df.iterrows():
                    mrn = str(row[PATIENT_ID_COL]).strip()
                    new_gen = str(row.get('generation', ''))
                    if mrn in old_map and old_map[mrn] != new_gen:
                        changed += 1
            except Exception:
                pass

    # Update project.json
    now_iso = datetime.now().isoformat(timespec='seconds')
    ext_rel = f'extractions/{ext_filename}'
    existing.append({
        'filename':    ext_rel,
        'loaded_date': now_iso,
        'row_count':   len(new_df),
        'label':       f'v{next_v}',
    })
    meta['extractions'] = existing
    meta['active_extraction'] = ext_rel
    _write_project_json(project_dir, meta)

    # Add new MRNs to patients.db
    new_patients = _populate_patients_db(project_dir, new_df)

    return {
        'new_patients': new_patients,
        'changed_patients': changed,
        'ext_filename': ext_filename,
        'df': new_df,
    }


# =============================================================================
# CHECKPOINT
# =============================================================================

def do_checkpoint(project_dir: Path,
                  annotations_conn: sqlite3.Connection) -> Path:
    """
    Export current annotations to checkpoints/ and prune old checkpoints.
    Updates project.json last_checkpoint.
    Returns the path of the checkpoint file written.
    """
    chk_dir = get_project_checkpoints_dir(project_dir)
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    chk_path = chk_dir / f'annotations_{ts}.csv'

    df_ann = pd.read_sql_query("SELECT * FROM annotations", annotations_conn)
    df_ann.to_csv(str(chk_path), index=False)

    # Prune: keep only the MAX_CHECKPOINTS most recent
    all_chk = sorted(chk_dir.glob('annotations_*.csv'))
    while len(all_chk) > MAX_CHECKPOINTS:
        try:
            all_chk.pop(0).unlink()
        except Exception:
            break

    # Update project.json
    try:
        meta = _read_project_json(project_dir)
        meta['last_checkpoint'] = datetime.now().isoformat(timespec='seconds')
        _write_project_json(project_dir, meta)
    except Exception:
        pass

    return chk_path


# =============================================================================
# LEGACY MIGRATION
# =============================================================================

def migrate_legacy_folder(legacy_dir: Path,
                           new_project_dir: Path,
                           project_name: str,
                           csv_path: Path) -> dict:
    """
    Migrate a legacy watney_annotations/ folder to a proper project.

    - Creates new_project_dir with full project structure
    - Copies legacy DB as annotations.db (adding extraction_version column)
    - Registers the provided csv_path as extraction_v1
    Returns the new project meta dict.
    Raises ProjectError on failure.
    """
    legacy_db = legacy_dir / 'watney_annotations_database.db'
    if not legacy_db.exists():
        raise ProjectError(f'Legacy DB not found: {legacy_db}')

    # Create project structure (without populating annotations — we copy the DB)
    if not csv_path.exists():
        raise ProjectError(f'CSV not found: {csv_path}')
    try:
        df = pd.read_csv(str(csv_path), dtype={PATIENT_ID_COL: str})
    except Exception as e:
        raise ProjectError(f'Could not read CSV: {e}')
    missing = REQUIRED_CSV_COLUMNS - set(df.columns)
    if missing:
        raise ProjectError(f'CSV missing required columns: {missing}')

    new_project_dir.mkdir(parents=True, exist_ok=True)
    _extractions_dir(new_project_dir)
    get_project_checkpoints_dir(new_project_dir)
    get_project_exports_dir(new_project_dir)

    # Copy CSV — prefix with version number: 1_originalfilename.csv
    orig_name = csv_path.name
    ext_filename = f'1_{orig_name}'
    ext_dest = _extractions_dir(new_project_dir) / ext_filename
    shutil.copy2(str(csv_path), str(ext_dest))

    # Copy legacy DB as annotations.db, then add extraction_version column if missing
    ann_db_dest = get_project_annotations_db_path(new_project_dir)
    shutil.copy2(str(legacy_db), str(ann_db_dest))
    conn = sqlite3.connect(str(ann_db_dest), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        conn.execute('ALTER TABLE annotations ADD COLUMN extraction_version TEXT')
    except sqlite3.OperationalError:
        pass  # already exists
    conn.commit()
    conn.close()

    # Populate patients.db from CSV
    _populate_patients_db(new_project_dir, df)

    # Build project.json
    now_iso = datetime.now().isoformat(timespec='seconds')
    ext_rel = f'extractions/{ext_filename}'
    meta = {
        'project_name':               project_name,
        'created':                    now_iso,
        'last_opened':                now_iso,
        'active_extraction':          ext_rel,
        'extractions': [
            {
                'filename':    ext_rel,
                'loaded_date': now_iso,
                'row_count':   len(df),
                'label':       'v1 — migrated from legacy',
            }
        ],
        'checkpoint_interval_minutes': 30,
        'last_checkpoint':            None,
        'watney_version':             '6',
    }
    _write_project_json(new_project_dir, meta)

    return meta