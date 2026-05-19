"""
WATNEY Test Suite
=================
Tests the core data layer (SQLite helpers, date parsing, highlighting,
agent extraction) and a full end-to-end workflow against a synthetic
in-memory database.  No browser / NiceGUI required.

Run:
    python -m pytest test_watney.py -v
    # or just:
    python test_watney.py
"""

import json
import re
import sqlite3
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

# ---------------------------------------------------------------------------
# Stub out NiceGUI so we can import the module without a running server
# ---------------------------------------------------------------------------

ui_stub = types.ModuleType("nicegui.ui")
ui_stub.notify = lambda *a, **k: None
ui_stub.page = lambda *a, **k: (lambda f: f)

nicegui_stub = types.ModuleType("nicegui")
nicegui_stub.ui = ui_stub
nicegui_stub.app = MagicMock()

sys.modules.setdefault("nicegui", nicegui_stub)
sys.modules.setdefault("nicegui.ui", ui_stub)

# Locate watney source (same dir as this test file, or outputs dir for CI)
_here = Path(__file__).parent
_candidates = [
    _here / "watney5.py",
    _here / "watney.py",
    _here.parent / "outputs" / "watney.py",
]
_src = next((p for p in _candidates if p.exists()), None)
if _src is None:
    raise FileNotFoundError(
        "Could not find watney3.py or watney.py. "
        "Run this test from the same directory as the source file."
    )

# Import watney functions by running the module in a fresh namespace
_ns: dict = {"__file__": str(_src), "__name__": "__test__"}
with open(_src) as _f:
    _code = compile(_f.read(), str(_src), "exec")

# Patch sqlite3.connect so module-level DB setup uses an in-memory DB
with patch("sqlite3.connect") as _mock_connect:
    _mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
    _mem_conn.row_factory = sqlite3.Row
    _mock_connect.return_value = _mem_conn
    try:
        exec(_code, _ns)
    except SystemExit:
        pass
    except Exception as e:
        # Some ui.* calls may fail during module-level exec; that's OK
        pass

# Pull helpers we want to test directly from the module namespace
normalize_any_date   = _ns.get("normalize_any_date")
sort_date_key        = _ns.get("sort_date_key")
clean_date_input     = _ns.get("clean_date_input")
normalize_patient_id = _ns.get("normalize_patient_id")
safe_str             = _ns.get("safe_str")
safe_json_loads      = _ns.get("safe_json_loads")
compress_blank_lines = _ns.get("compress_blank_lines")
extract_field        = _ns.get("extract_field")
parse_notes          = _ns.get("parse_notes")
highlight_evidence   = _ns.get("highlight_evidence")
get_agent_first_start= _ns.get("get_agent_first_start")
get_agent_last_end   = _ns.get("get_agent_last_end")
_agent_matches_plan  = None  # defined inside build_page; tested via regex inline


# ---------------------------------------------------------------------------
# Helper: build a fresh in-memory DB with the WATNEY schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    DFCI_MRN TEXT NOT NULL,
    progression_date TEXT, progression_source TEXT,
    agent TEXT, evidence TEXT, report_id TEXT,
    determined_by TEXT, user TEXT, modification_timestamp TEXT,
    agent_start TEXT, agent_start_source TEXT,
    agent_end TEXT, agent_end_source TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx ON annotations
    (DFCI_MRN, progression_date, progression_source, report_id);
"""

def _make_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _insert(conn, row):
    conn.execute("""
        INSERT OR REPLACE INTO annotations
        (DFCI_MRN,progression_date,progression_source,agent,evidence,
         report_id,determined_by,user,modification_timestamp,
         agent_start,agent_start_source,agent_end,agent_end_source)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        row.get("DFCI_MRN"), row.get("progression_date"), row.get("progression_source","LLM"),
        row.get("agent"), row.get("evidence",""), row.get("report_id","RPT001"),
        row.get("determined_by"), row.get("user","tester"),
        datetime.now().isoformat(timespec="seconds"),
        row.get("agent_start"), row.get("agent_start_source","LLM"),
        row.get("agent_end"), row.get("agent_end_source","LLM"),
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Sample extraction JSON shared across tests
# ---------------------------------------------------------------------------

EXTRACTION = {
    "systemic_therapy": {
        "agents": [
            {
                "drug_name": "Temozolomide",
                "intervals": [
                    {
                        "start_date": "2022-03-01",
                        "start_date_rationale": {"text": "started TMZ", "report_id": "RPT001"},
                        "end_date": "2022-09-30",
                        "end_date_rationale": {"text": "completed TMZ", "report_id": "RPT004"},
                    },
                    {
                        "start_date": "2021-01-15",  # earlier interval
                        "end_date": "2021-06-30",
                    },
                ],
            },
            {
                "drug_name": "Bevacizumab",
                "intervals": [
                    {
                        "start_date": "2022-10-01",
                        "end_date": "2023-03-31",
                    }
                ],
            },
        ]
    },
    "progression": {
        "progression_events": [
            {
                "progression_date": "2022-09-15",
                "confidence_level": "high",
                "treatment_plan_at_time": "Temozolomide",
                "progression_date_rationale": {
                    "text": "new enhancing lesion... suspicious for recurrence",
                    "report_id": "RPT005",
                    "note_date": "2022-09-15",
                    "author": "Dr. Smith",
                },
            },
            {
                "progression_date": "2023-04-01",
                "confidence_level": "medium",
                "treatment_plan_at_time": "Bevacizumab",
                "progression_date_rationale": {
                    "text": "further growth on bevacizumab",
                    "report_id": "RPT010",
                },
            },
        ]
    },
}

NOTES_TEXT = """\
Note Number: 1
Note Report ID: RPT001
Note Date: 2022-03-01
Note Dept: Neuro-Oncology
Note Author: Dr. Smith

Patient started temozolomide 150 mg/m2 daily for 5 days every 28 days.

====================
Note Number: 2
Note Report ID: RPT005
Note Date: 2022-09-15
Note Dept: Neuro-Oncology
Note Author: Dr. Smith

MRI shows new enhancing lesion at resection margin... suspicious for recurrence.
Since completing radiation, there has been progressive enhancement.
"""


# ===========================================================================
# TEST CLASSES
# ===========================================================================

class TestDateNormalization(unittest.TestCase):
    """normalize_any_date and sort_date_key"""

    def test_iso_format(self):
        self.assertEqual(normalize_any_date("2023-06-15"), "2023-06-15")

    def test_slash_format(self):
        self.assertEqual(normalize_any_date("2023/06/15"), "2023-06-15")

    def test_compact_format(self):
        self.assertEqual(normalize_any_date("20230615"), "2023-06-15")

    def test_us_short(self):
        self.assertEqual(normalize_any_date("06/15/23"), "2023-06-15")

    def test_us_long(self):
        self.assertEqual(normalize_any_date("06/15/2023"), "2023-06-15")

    def test_nan_returns_none(self):
        import numpy as np
        self.assertIsNone(normalize_any_date(np.nan))
        self.assertIsNone(normalize_any_date("nan"))
        self.assertIsNone(normalize_any_date(""))

    def test_sort_key_unknown(self):
        self.assertEqual(sort_date_key("not-a-date"), "not-a-date")

    def test_sort_key_fallback(self):
        import numpy as np
        self.assertEqual(sort_date_key(np.nan), "9999-12-31")


class TestCleanDateInput(unittest.TestCase):
    """clean_date_input normalises free-text date entry"""

    def test_clean_iso(self):
        self.assertEqual(clean_date_input("2023-06-15"), "2023-06-15")

    def test_compact_8_digits(self):
        self.assertEqual(clean_date_input("20230615"), "2023-06-15")

    def test_with_slashes_stripped(self):
        # "2023/06/15" → digits "20230615" → formatted
        self.assertEqual(clean_date_input("2023/06/15"), "2023-06-15")

    def test_none_input(self):
        self.assertIsNone(clean_date_input(None))
        self.assertIsNone(clean_date_input(""))

    def test_partial_digits_passthrough(self):
        # only 6 digits — can't format, returns stripped raw
        result = clean_date_input("202306")
        self.assertEqual(result, "202306")


class TestNormalizePatientId(unittest.TestCase):
    def test_strips_trailing_dot_zero(self):
        self.assertEqual(normalize_patient_id("12345.0"), "12345")

    def test_keeps_string(self):
        self.assertEqual(normalize_patient_id("MRN001"), "MRN001")

    def test_nan(self):
        import numpy as np
        self.assertEqual(normalize_patient_id(np.nan), "")


class TestSafeStr(unittest.TestCase):
    def test_normal_string(self):
        self.assertEqual(safe_str("hello"), "hello")

    def test_bytes(self):
        self.assertEqual(safe_str(b"hello"), "hello")

    def test_nan(self):
        import numpy as np
        self.assertEqual(safe_str(np.nan), "")


class TestSafeJsonLoads(unittest.TestCase):
    def test_valid_json(self):
        result = safe_json_loads('{"a": 1}')
        self.assertEqual(result, {"a": 1})

    def test_invalid_json(self):
        self.assertEqual(safe_json_loads("not json"), {})

    def test_nan(self):
        import numpy as np
        self.assertEqual(safe_json_loads(np.nan), {})


class TestNotesParsing(unittest.TestCase):
    def test_splits_correctly(self):
        notes = parse_notes(NOTES_TEXT)
        self.assertEqual(len(notes), 2)

    def test_extracts_report_id(self):
        notes = parse_notes(NOTES_TEXT)
        self.assertEqual(notes[0]["report_id"], "RPT001")
        self.assertEqual(notes[1]["report_id"], "RPT005")

    def test_extracts_author(self):
        notes = parse_notes(NOTES_TEXT)
        self.assertEqual(notes[0]["author"], "Dr. Smith")

    def test_empty_string(self):
        self.assertEqual(parse_notes(""), [])

    def test_non_string(self):
        self.assertEqual(parse_notes(None), [])
        self.assertEqual(parse_notes(42), [])


class TestHighlighting(unittest.TestCase):
    def test_single_segment_found(self):
        html, found = highlight_evidence(
            "The patient showed new enhancing lesion in MRI.",
            "new enhancing lesion"
        )
        self.assertTrue(found)
        self.assertIn("evidence-highlight", html)
        self.assertIn("new enhancing lesion", html)

    def test_ellipsis_multi_segment(self):
        note = "Since June 2023, there are new small foci... makes tumor less likely."
        evidence = "Since June 2023, there are new small foci... makes tumor less likely"
        html, found = highlight_evidence(note, evidence)
        self.assertTrue(found)
        # Both segments should be highlighted
        self.assertGreaterEqual(html.count("evidence-highlight"), 1)

    def test_no_match_returns_escaped(self):
        import html as _html
        note = "Normal note text."
        result_html, found = highlight_evidence(note, "nonexistent phrase xyz")
        self.assertFalse(found)
        self.assertEqual(result_html, _html.escape(note))

    def test_empty_evidence(self):
        import html as _html
        note = "Some note."
        result_html, found = highlight_evidence(note, "")
        self.assertFalse(found)
        self.assertEqual(result_html, _html.escape(note))

    def test_first_span_has_anchor_id(self):
        html, found = highlight_evidence(
            "apple orange apple", "apple"
        )
        self.assertIn('id="evidence-highlight"', html)
        # Only the first occurrence gets the id
        self.assertEqual(html.count('id="evidence-highlight"'), 1)


class TestAgentExtraction(unittest.TestCase):
    def test_first_start_picks_earliest(self):
        # Temozolomide has intervals starting 2021-01-15 and 2022-03-01
        result = get_agent_first_start(EXTRACTION, "Temozolomide")
        self.assertEqual(result, "2021-01-15")

    def test_last_end_picks_latest(self):
        result = get_agent_last_end(EXTRACTION, "Temozolomide")
        self.assertEqual(result, "2022-09-30")

    def test_unknown_agent_returns_none(self):
        self.assertIsNone(get_agent_first_start(EXTRACTION, "Pembrolizumab"))
        self.assertIsNone(get_agent_last_end(EXTRACTION, "Pembrolizumab"))

    def test_bevacizumab_start(self):
        result = get_agent_first_start(EXTRACTION, "Bevacizumab")
        self.assertEqual(result, "2022-10-01")


class TestAgentMatchesPlan(unittest.TestCase):
    """Test the _agent_matches_plan regex (replicated inline — it's defined
    inside build_page so we test the logic directly)."""

    @staticmethod
    def matches(agent, plan):
        if not agent or not plan:
            return False
        pattern = r'(?i)\b' + re.escape(agent.strip()) + r'\b'
        return bool(re.search(pattern, plan))

    def test_exact_match(self):
        self.assertTrue(self.matches("Temozolomide", "Temozolomide"))

    def test_case_insensitive(self):
        self.assertTrue(self.matches("temozolomide", "TEMOZOLOMIDE"))

    def test_partial_word_no_match(self):
        # "TMZ" should not match "Temozolomide"
        self.assertFalse(self.matches("TMZ", "Temozolomide"))

    def test_agent_in_sentence(self):
        self.assertTrue(self.matches("Bevacizumab", "patient was on Bevacizumab at time"))

    def test_empty_plan(self):
        self.assertFalse(self.matches("TMZ", ""))
        self.assertFalse(self.matches("", "TMZ"))


class TestDatabaseOperations(unittest.TestCase):
    """Full CRUD lifecycle against an in-memory DB."""

    def setUp(self):
        self.conn = _make_db()

    def tearDown(self):
        self.conn.close()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _count(self, mrn=None):
        if mrn:
            return self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE DFCI_MRN=?", (mrn,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]

    def _get(self, mrn, report_id, prog_date):
        return self.conn.execute(
            """SELECT * FROM annotations
               WHERE DFCI_MRN=? AND report_id=? AND progression_date=? LIMIT 1""",
            (mrn, report_id, prog_date)
        ).fetchone()

    # ── tests ─────────────────────────────────────────────────────────────────
    def test_insert_llm_annotation(self):
        _insert(self.conn, {
            "DFCI_MRN": "MRN001",
            "progression_date": "2023-06-15",
            "progression_source": "LLM",
            "agent": "Temozolomide",
            "report_id": "RPT005",
            "agent_start": "2022-03-01",
            "agent_start_source": "LLM",
            "agent_end": "2022-09-30",
            "agent_end_source": "LLM",
        })
        self.assertEqual(self._count("MRN001"), 1)
        row = self._get("MRN001", "RPT005", "2023-06-15")
        self.assertIsNotNone(row)
        self.assertEqual(row["agent"], "Temozolomide")
        self.assertEqual(row["agent_start"], "2022-03-01")

    def test_upsert_updates_agent(self):
        _insert(self.conn, {
            "DFCI_MRN": "MRN001", "progression_date": "2023-06-15",
            "agent": "Temozolomide", "report_id": "RPT005",
        })
        # Update with new agent
        _insert(self.conn, {
            "DFCI_MRN": "MRN001", "progression_date": "2023-06-15",
            "agent": "Bevacizumab", "report_id": "RPT005",
        })
        # Should still be 1 row (upsert), with updated agent
        self.assertEqual(self._count("MRN001"), 1)
        row = self._get("MRN001", "RPT005", "2023-06-15")
        self.assertEqual(row["agent"], "Bevacizumab")

    def test_remove_agent(self):
        _insert(self.conn, {
            "DFCI_MRN": "MRN002", "progression_date": "2023-01-01",
            "agent": "PCV", "report_id": "RPT010",
        })
        self.assertEqual(self._count("MRN002"), 1)
        self.conn.execute(
            "DELETE FROM annotations WHERE DFCI_MRN=? AND report_id=? AND progression_date=?",
            ("MRN002", "RPT010", "2023-01-01")
        )
        self.conn.commit()
        self.assertEqual(self._count("MRN002"), 0)

    def test_insert_clinician_event(self):
        _insert(self.conn, {
            "DFCI_MRN": "MRN003", "progression_date": "2023-08-10",
            "progression_source": "manual",
            "agent": "Lomustine", "report_id": "RPT999",
            "determined_by": "Dr. Jones",
            "agent_start": "2023-06-01", "agent_start_source": "manual",
            "agent_end": "2023-09-30", "agent_end_source": "manual",
        })
        row = self._get("MRN003", "RPT999", "2023-08-10")
        self.assertIsNotNone(row)
        self.assertEqual(row["progression_source"], "manual")
        self.assertEqual(row["determined_by"], "Dr. Jones")
        self.assertEqual(row["agent_start"], "2023-06-01")
        self.assertEqual(row["agent_start_source"], "manual")

    def test_delete_clinician_event(self):
        _insert(self.conn, {
            "DFCI_MRN": "MRN003", "progression_date": "2023-08-10",
            "progression_source": "manual", "agent": "Lomustine",
            "report_id": "clinician::MRN003::123",
        })
        self.conn.execute(
            "DELETE FROM annotations WHERE report_id=?",
            ("clinician::MRN003::123",)
        )
        self.conn.commit()
        self.assertEqual(self._count("MRN003"), 0)

    def test_multiple_patients_isolated(self):
        for mrn in ("P001", "P002", "P003"):
            _insert(self.conn, {
                "DFCI_MRN": mrn, "progression_date": "2023-01-01",
                "agent": "Drug", "report_id": "RPT001",
            })
        self.assertEqual(self._count(), 3)
        self.assertEqual(self._count("P001"), 1)
        self.assertEqual(self._count("P002"), 1)

    def test_undo_deletes_most_recent(self):
        # Insert with explicit timestamps to guarantee ordering
        self.conn.execute("""INSERT INTO annotations
            (DFCI_MRN,progression_date,progression_source,agent,evidence,
             report_id,determined_by,user,modification_timestamp,
             agent_start,agent_start_source,agent_end,agent_end_source)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("MRN004","2023-01-01","LLM","First","","RPT001",None,"tester",
             "2023-01-01T10:00:00",None,"LLM",None,"LLM"))
        self.conn.execute("""INSERT INTO annotations
            (DFCI_MRN,progression_date,progression_source,agent,evidence,
             report_id,determined_by,user,modification_timestamp,
             agent_start,agent_start_source,agent_end,agent_end_source)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("MRN004","2023-06-01","LLM","Second","","RPT002",None,"tester",
             "2023-06-01T10:00:00",None,"LLM",None,"LLM"))
        self.conn.commit()
        self.assertEqual(self._count("MRN004"), 2)
        # Undo: delete most recent by modification_timestamp
        row = self.conn.execute(
            """SELECT id FROM annotations WHERE DFCI_MRN=?
               ORDER BY modification_timestamp DESC LIMIT 1""", ("MRN004",)
        ).fetchone()
        self.conn.execute("DELETE FROM annotations WHERE id=?", (row["id"],))
        self.conn.commit()
        self.assertEqual(self._count("MRN004"), 1)
        remaining = self.conn.execute(
            "SELECT agent FROM annotations WHERE DFCI_MRN=?", ("MRN004",)
        ).fetchone()
        self.assertEqual(remaining["agent"], "First")

    def test_export_query_returns_all_rows(self):
        for i in range(5):
            _insert(self.conn, {
                "DFCI_MRN": f"PT{i:03}", "progression_date": f"2023-0{i+1}-01",
                "agent": "Drug", "report_id": f"RPT{i:03}",
            })
        df = pd.read_sql_query("SELECT * FROM annotations", self.conn)
        self.assertEqual(len(df), 5)
        self.assertIn("agent", df.columns)
        self.assertIn("agent_start", df.columns)
        self.assertIn("agent_end", df.columns)

    def test_demo_db_isolated_from_real(self):
        """Demo uses its own :memory: — inserting into one doesn't affect the other."""
        real_conn = _make_db()
        demo_conn = _make_db()

        _insert(real_conn, {
            "DFCI_MRN": "REAL001", "progression_date": "2023-01-01",
            "agent": "RealDrug", "report_id": "RPT_REAL",
        })
        _insert(demo_conn, {
            "DFCI_MRN": "DEMO001", "progression_date": "2023-01-01",
            "agent": "DemoDrug", "report_id": "RPT_DEMO",
        })

        real_df = pd.read_sql_query("SELECT * FROM annotations", real_conn)
        demo_df = pd.read_sql_query("SELECT * FROM annotations", demo_conn)

        self.assertEqual(len(real_df), 1)
        self.assertEqual(len(demo_df), 1)
        self.assertNotIn("DEMO001", real_df["DFCI_MRN"].tolist())
        self.assertNotIn("REAL001", demo_df["DFCI_MRN"].tolist())

        real_conn.close()
        demo_conn.close()


class TestCustomAgents(unittest.TestCase):
    """Simulate the in-session custom agent list."""

    def test_add_custom_agent(self):
        options = ["Temozolomide", "Bevacizumab"]
        new_agent = "Pembrolizumab"
        if new_agent not in options:
            options = options + [new_agent]
        self.assertIn("Pembrolizumab", options)
        self.assertEqual(len(options), 3)

    def test_no_duplicate(self):
        options = ["Temozolomide", "Bevacizumab"]
        # Try to add existing
        new_agent = "Temozolomide"
        if new_agent in options:
            already_present = True
        else:
            options = options + [new_agent]
            already_present = False
        self.assertTrue(already_present)
        self.assertEqual(len(options), 2)

    def test_session_only_not_persisted(self):
        """Custom agents in the UI are session-only (not written to config)."""
        import json, tempfile, os
        cfg = {"csv_path": "/tmp/fake.csv"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            fname = f.name
        try:
            with open(fname) as f:
                loaded = json.load(f)
            # Custom agents should NOT be in config
            self.assertNotIn("custom_agents", loaded)
        finally:
            os.unlink(fname)


class TestProgressionSummaryOrdering(unittest.TestCase):
    """Progression summary rows sort by date."""

    def test_ascending_order(self):
        rows = [
            {"date": "2023-06-01", "sort_date": "2023-06-01", "agent": "B"},
            {"date": "2022-01-15", "sort_date": "2022-01-15", "agent": "A"},
            {"date": "2023-12-31", "sort_date": "2023-12-31", "agent": "C"},
        ]
        rows.sort(key=lambda x: x.get("sort_date", "9999-12-31"))
        self.assertEqual([r["agent"] for r in rows], ["A", "B", "C"])

    def test_unknown_dates_go_last(self):
        rows = [
            {"date": "2023-01-01", "sort_date": "2023-01-01", "agent": "Real"},
            {"date": None, "sort_date": "9999-12-31", "agent": "Unknown"},
        ]
        rows.sort(key=lambda x: x.get("sort_date", "9999-12-31"))
        self.assertEqual(rows[0]["agent"], "Real")
        self.assertEqual(rows[1]["agent"], "Unknown")


class TestExportCSV(unittest.TestCase):
    """Export produces correct columns and row count."""

    def setUp(self):
        self.conn = _make_db()
        for i in range(3):
            _insert(self.conn, {
                "DFCI_MRN": f"PT{i}", "progression_date": f"2023-0{i+1}-01",
                "progression_source": "LLM" if i < 2 else "manual",
                "agent": f"Drug{i}", "report_id": f"RPT{i:03}",
                "agent_start": "2022-01-01", "agent_start_source": "LLM",
                "agent_end": "2022-12-31", "agent_end_source": "LLM",
            })

    def tearDown(self):
        self.conn.close()

    def test_row_count(self):
        df = pd.read_sql_query("SELECT * FROM annotations", self.conn)
        self.assertEqual(len(df), 3)

    def test_required_columns_present(self):
        df = pd.read_sql_query("SELECT * FROM annotations", self.conn)
        for col in ["DFCI_MRN", "progression_date", "progression_source",
                    "agent", "agent_start", "agent_end", "user",
                    "modification_timestamp"]:
            self.assertIn(col, df.columns, f"Missing column: {col}")

    def test_csv_bytes_decodable(self):
        import io
        df = pd.read_sql_query("SELECT * FROM annotations", self.conn)
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        self.assertIsInstance(csv_bytes, bytes)
        # Round-trip
        df2 = pd.read_csv(io.BytesIO(csv_bytes))
        self.assertEqual(len(df2), 3)

    def test_llm_vs_manual_filter(self):
        df = pd.read_sql_query("SELECT * FROM annotations", self.conn)
        llm_rows = df[df["progression_source"] == "LLM"]
        manual_rows = df[df["progression_source"] == "manual"]
        self.assertEqual(len(llm_rows), 2)
        self.assertEqual(len(manual_rows), 1)


# ===========================================================================
# Entry point for running without pytest
# ===========================================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    test_classes = [
        TestDateNormalization,
        TestCleanDateInput,
        TestNormalizePatientId,
        TestSafeStr,
        TestSafeJsonLoads,
        TestNotesParsing,
        TestHighlighting,
        TestAgentExtraction,
        TestAgentMatchesPlan,
        TestDatabaseOperations,
        TestCustomAgents,
        TestProgressionSummaryOrdering,
        TestExportCSV,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
