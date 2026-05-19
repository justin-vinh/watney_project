# WATNEY

**Interactive Oncology Annotation Platform**

WATNEY is a browser-based tool for reviewing and annotating LLM-extracted oncology progression events from longitudinal clinical notes. It runs locally, stores all annotations in a local SQLite database, and is designed for multi-user annotation workflows in oncology research.

Developed by Justin Vinh at Dana-Farber Cancer Institute.

---

## Features

- Side-by-side review of LLM-extracted progression events and source clinical notes
- Click **Source** on any progression card to jump to the supporting note and highlight the evidence quote
- **Agent Intervals box** with **Start source / End source** buttons linking directly to treatment date evidence
- **Progression card highlighting** — cards are highlighted in amber when the selected agent matches the `treatment_plan_at_time` field, identifying the most likely progression event for that agent
- Assign agents to progression events with auto-populated start/end dates (editable, auto-formatting)
- Add clinician-derived progression events manually
- Progression Summary table updates immediately on save or remove
- Undo the last annotation for any patient
- Arrow key navigation between patients
- Multi-user support with per-session usernames and logout
- Persistent configuration (CSV path, font size) via local JSON
- Export annotations to CSV (demo and real data are always separate)
- **Demo mode** — 3 synthetic brain cancer patients, fully isolated in-memory database
- **Interactive tutorial** — 10-step guided walkthrough, launches inside demo mode
- Multi-instance warning if more than one browser tab is open
- In-app update checker (Settings → Check for updates)

---

## Installation

```bash
pip install watney
```

Required dependencies are installed automatically: `nicegui`, `pandas`.

---

## Launching

```bash
watney
```

Opens the application in your browser at `http://localhost:8080`.

---

## First-Time Setup

1. Enter your username on the login screen
2. Enter the full path to your extraction CSV
3. WATNEY saves the path — subsequent logins go straight in

Configuration is stored at:
```
./watney_annotations/watney_config.json
```

---

## Required Input Format

WATNEY expects a CSV with these three columns:

| Column | Description |
|---|---|
| `DFCI_MRN` | Patient identifier |
| `all_notes` | Concatenated longitudinal clinical notes |
| `generation` | LLM-extracted JSON (see schema below) |

### Note Format

Notes within `all_notes` must be separated by a line of at least 20 `=` characters, with metadata headers:

```
Note Number: 1
Note Report ID: RPT001
Note Date: 2024-01-15
Note Dept: Neuro-Oncology
Note Author: Dr. Smith

[note text here]

====================
Note Number: 2
Note Report ID: RPT002
...
```

### JSON Schema (`generation` column)

The fields WATNEY reads from:

```json
{
  "systemic_therapy": {
    "agents": [
      {
        "drug_name": "Temozolomide",
        "intervals": [
          {
            "start_date": "2023-01-01",
            "start_date_rationale": {
              "text": "initiated temozolomide 150 mg/m2",
              "report_id": "RPT001"
            },
            "end_date": "2023-06-01",
            "end_date_rationale": {
              "text": "discontinued due to progression",
              "report_id": "RPT004"
            }
          }
        ]
      }
    ]
  },
  "progression": {
    "progression_events": [
      {
        "progression_date": "2023-06-15",
        "confidence_level": "high",
        "treatment_plan_at_time": "Temozolomide",
        "treatment_plan_at_time_rationale": {
          "text": "patient was on temozolomide at time of imaging"
        },
        "progression_date_rationale": {
          "text": "new enhancing lesion at resection margin...",
          "report_id": "RPT005",
          "note_date": "2023-06-15",
          "author": "Dr. Smith"
        }
      }
    ]
  }
}
```

Only the fields above are used by WATNEY. The full schema supports additional fields (diagnosis, radiation, surgery, PFS) which are ignored.

---

## Workflow

### 1. Navigate patients

| Control | Action |
|---|---|
| **Prev / Next** buttons | Move between patients |
| **← → arrow keys** | Move between patients |
| **Patient List** | See all patients; jump to any |

### 2. Review Agent Intervals

Select an agent from the **Agent Intervals** dropdown to see its treatment intervals. Each interval row shows:
- Start date → End date
- **Start source** button — jumps to and highlights the supporting note for the start date
- **End source** button — jumps to and highlights the supporting note for the end date

### 3. Identify the progression event

Progression cards are **highlighted in amber** when the currently selected agent matches the `treatment_plan_at_time` field. A notice above the cards reads:

> ▶ Highlighted card: likely progression event for [Agent]

### 4. Assign an agent to a progression event

In each progression card:
1. Select the responsible agent from the dropdown
2. Review the auto-populated **Start date** and **End date** — these come from the LLM extraction
3. Edit either date if needed — blur the field to auto-format to `YYYY-MM-DD`
4. Click **Save Agent Assignment**

The **Progression Summary** table at the top of the left panel updates immediately.

### 5. Source navigation

Click **Source** on any progression card to scroll the right panel to the supporting note and highlight the evidence text. All evidence segments (separated by `...` or `…`) are highlighted individually.

### 6. Add clinician progression events

Use the form at the bottom of the left panel to manually record:

- **Agent** *(required)*
- **Progression Date** *(required, auto-formats to YYYY-MM-DD)*
- Agent Start date and End date
- Supporting evidence and Report ID
- Who determined the progression

### 7. Remove or undo

- **Remove Agent** — clears the assignment from a specific card in place; summary updates immediately
- **Undo** (nav bar) — deletes the most recently modified annotation for the current patient

### 8. Export

Click **Export** on the nav bar to download all annotations as a timestamped CSV. In demo mode, only demo annotations are exported — your real database is never touched.

### 9. Logout

Click **Logout** (top-right of the left panel) to return to the login screen. Demo annotations are discarded permanently on logout.

---

## Demo Mode

Click **Try Demo** on the login screen to explore WATNEY with 3 synthetic brain cancer patients:

| Patient | Diagnosis | Agents | Progression Events |
|---|---|---|---|
| MRN000001 | GBM (IDH-wt, MGMT-meth) | Temozolomide → Bevacizumab | 2 |
| MRN000002 | Anaplastic astrocytoma | PCV → Temozolomide | 2 |
| MRN000003 | Grade 3 oligodendroglioma | Temozolomide → Lomustine | 2 |

Demo annotations are stored in an isolated in-memory database and are permanently discarded on logout. They are never written to your real database.

Place `watney_demo_data.csv` in the same directory as `watney3.py` (or wherever WATNEY is installed).

---

## Tutorial

Click **Tutorial + Demo** on the login screen to launch demo mode with a guided 10-step walkthrough that opens automatically inside the app. Steps are navigated with Back / Next buttons or by clicking the dot indicators.

---

## Settings

Open **Settings** from the nav bar to:

- Adjust report text font size (8–20 px)
- View current file paths (CSV, database, annotations folder)
- Change the input CSV path
- Move the annotations folder (copies DB + config, removes old files)
- Rename the database file
- Check for and install WATNEY updates from PyPI

---

## Annotation Database

Stored at:
```
./watney_annotations/watney_annotations_database.db
```

### Schema

| Column | Description |
|---|---|
| `DFCI_MRN` | Patient identifier |
| `progression_date` | Date of the progression event |
| `progression_source` | `LLM` or `manual` |
| `agent` | Assigned therapeutic agent |
| `agent_start` | Agent start date |
| `agent_start_source` | `LLM` or `manual` |
| `agent_end` | Agent end date |
| `agent_end_source` | `LLM` or `manual` |
| `evidence` | Supporting text from the note |
| `report_id` | Source note report ID |
| `determined_by` | Who determined the event (clinician entries) |
| `user` | Annotator username |
| `modification_timestamp` | ISO timestamp of last change |

---

## Multiple Browser Tabs

If more than one browser tab connects to the WATNEY server simultaneously, a warning banner appears on the login screen. Multiple active sessions sharing a single server can cause unexpected UI behavior — close any extra tabs before working.

---

## Citation

```
Justin Vinh. WATNEY: Interactive Oncology Annotation Platform.
https://github.com/justin-vinh/watney_project
```

---

## License

MIT License
