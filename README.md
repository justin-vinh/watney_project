# WATNEY 6

**Interactive Oncology Annotation Platform**

WATNEY is a browser-based tool for reviewing and annotating LLM-extracted oncology progression events from longitudinal clinical notes. It runs locally, stores all annotations in a per-project SQLite database, and is designed for multi-user annotation workflows in oncology research.

Developed by Justin Vinh at Dana-Farber Cancer Institute.

---

## Features

- **Project system** — each cohort lives in its own folder with its own database, extraction files, exports, and checkpoints
- **Extraction versioning** — load multiple extraction CSVs into one project and switch between them from a dropdown; annotations persist across switches
- **Automatic checkpoints** — DB snapshots saved on a configurable interval (default 30 min); manual checkpoint available in Settings
- Side-by-side review of LLM-extracted progression events and full source clinical notes
- Click **Source** on any progression card to jump to and highlight the supporting evidence in the right panel
- **Agent Intervals box** with **Start source / End source** buttons linking directly to treatment date evidence in the notes
- **Progression card highlighting** — cards are highlighted in amber when the selected agent matches the `treatment_plan_at_time` field, surfacing the most likely progression event for that agent
- **Progression Summary table** — shows all assigned events (date, agent, start/end, source, annotator, extraction version); updates immediately on save or remove
- **Soft-delete** — × button on each Progression Summary row deletes the annotation while preserving the record for audit; excluded from exports
- Assign agents to progression events with auto-populated start/end dates (editable; blur to auto-format to `YYYY-MM-DD`)
- **No End Date** — records `NA` for agents without a confirmed end date
- Add clinician-derived progression events manually; custom agent names supported; optional agent discontinuation note (preset reasons or free text)
- **NO PROGRESSION** — records `NA` progression date for patients with confirmed no-progression
- Undo the last annotation for any patient
- **NLP drug search** — Cmd/Ctrl-F (or Search button) expands drug names to all known aliases; click NLP to toggle
- **Resizable panels** — drag the center divider to redistribute left/right panel widths
- **Sticky note header** — note metadata pins to the top of the right panel while scrolling
- **Extraction data viewer** — collapsible tree view of the full LLM extraction JSON
- **Exclude / un-exclude patients** with required reason; excluded patients show a red banner
- Arrow key navigation between patients; patient list dialog with annotation status
- Import annotations from a prior export CSV or another project's `annotations.db`
- Multi-user support with per-session usernames and logout
- Export annotations to a timestamped CSV (also auto-saved to the project `exports/` folder)
- **Demo mode** — 3 synthetic oncology patients, fully isolated in-memory database, nothing written to disk
- **Interactive tutorial** — 14-step guided walkthrough, launches automatically in demo mode
- Multi-instance warning if more than one browser tab is open
- In-app update checker and one-click update from PyPI (Settings → Check for updates)

---

## Installation

```bash
pip install watney
```

Dependencies installed automatically: `nicegui`, `pandas`, `numpy`, `orjson`, `json-repair`, `pydantic`, `python-dateutil`, `requests`.

---

## Launching

```bash
watney
```

Opens the application in your browser at `http://localhost:8080`.

---

## First-Time Setup

1. Enter your name on the login screen
2. Click **+ New Project** and provide:
   - A project name
   - A folder path (will be created)
   - The path to your extraction CSV
3. WATNEY creates the project folder and opens it — subsequent logins jump straight back in via the recent projects list

To open an existing project, select it from **Recent projects** or type the folder path directly.

---

## Project Folder Structure

```
MyProject/
├── project.json          # project metadata and settings
├── annotations.db        # annotation SQLite database
├── patients.db           # patient exclusion tracking
├── extractions/          # copies of all loaded extraction CSVs
├── exports/              # timestamped export CSVs
└── checkpoints/          # automatic DB snapshots (up to 20 kept)
```

Global config (font size, recent projects, search behavior) is stored at:
```
~/.watney/config.json
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
        "progression_date_rationale": {
          "text": "new enhancing lesion at resection margin",
          "report_id": "RPT005",
          "note_date": "2023-06-15",
          "author": "Dr. Smith"
        }
      }
    ]
  }
}
```

Evidence text in `text` fields should be **verbatim quotes** from the corresponding note — this is what gets highlighted yellow when you click Source. Use `...` or `…` to join non-contiguous passages from the same note; each segment is highlighted separately.

---

## Workflow

### 1. Navigate patients

| Control | Action |
|---|---|
| **Prev / Next** buttons | Move between patients |
| **← → arrow keys** | Move between patients |
| **Patients** button | Patient list dialog — see annotation status; jump to any patient |

### 2. Review Agent Intervals

Select an agent from the **Agent Intervals** dropdown to see its treatment intervals. Each interval row shows:
- Start date → End date
- **Start source** — jumps to and highlights the supporting note for the start date
- **End source** — jumps to and highlights the supporting note for the end date

### 3. Identify the progression event

Progression cards are **highlighted in amber** when the currently selected agent matches the `treatment_plan_at_time` field. A notice above the cards reads:

> ▶ Highlighted card: likely progression event for [Agent]

### 4. Assign an agent to a progression event

In each progression card:
1. Select the responsible agent from the dropdown
2. Review the auto-populated **Start date** and **End date** from the LLM extraction
3. Edit either date if needed — blur the field to auto-format to `YYYY-MM-DD`; click **No End Date** to record `NA`
4. Click **Save Agent Assignment**

The **Progression Summary** table updates immediately.

### 5. Source navigation

Click **Source** on any progression card to scroll the right panel to the supporting note and highlight the evidence text. The search bar grays out while source highlight is active — click it to resume search.

### 6. Add clinician progression events

Use the form at the bottom of the left panel to manually record:

- **Agent** *(required)*; add a custom agent name with the Add button
- **Agent note** — optional discontinuation reason; choose from a preset list or select Other and type free text; click **No Note** to clear
- **Progression Date** *(required, auto-formats to YYYY-MM-DD)*; click **NO PROGRESSION** to record `NA`
- Agent Start date and End date (auto-filled from LLM data when you pick an agent)
- Supporting evidence and Report ID
- Who determined the progression

### 7. Exclude a patient

Click **Exclude Patient** near the patient header. A dialog asks for a required reason. Excluded patients show a red banner and their MRN is flagged in exports. Click **Un-exclude Patient** and provide a reason to reverse.

### 8. Remove or undo

- **Remove Agent** — clears the assignment from a specific card; Progression Summary updates immediately
- **× button** on a Progression Summary row — soft-deletes that annotation (preserved for audit, excluded from exports)
- **Undo** (nav bar) — hard-deletes the most recently modified annotation for the current patient

### 9. Export

Click **Export** in the nav bar to download all annotations as a timestamped CSV. The file is also saved to the project `exports/` folder automatically. In demo mode, only demo annotations are exported — your real database is never touched.

### 10. Logout

Click **Logout** (top-right of the left panel) to return to the login screen. Demo annotations are permanently discarded on logout.

---

## Demo Mode

Click **Try Demo** on the login screen to explore WATNEY with 3 synthetic oncology patients:

| Patient | Diagnosis | Agents | Progression Events |
|---|---|---|---|
| D000001 | NSCLC, EGFR exon 19 del | erlotinib → osimertinib | 2 |
| D000002 | DLBCL, GCB subtype | R-CHOP → R-ICE → liso-cel | 2 |
| D000003 | HR+/HER2− metastatic breast cancer | palbociclib + letrozole → abemaciclib + fulvestrant → everolimus + exemestane | 2 |

Demo annotations are stored in an isolated in-memory database and permanently discarded on logout. Nothing is written to your real project database.

---

## Tutorial

Click **Tutorial + Demo** on the login screen to launch demo mode with a 14-step interactive walkthrough. Steps are navigated with **Back / Next** buttons or by clicking the dot indicators. Steps that reference a specific UI element will highlight it in the main interface.

---

## Settings

Open **Settings** from the nav bar to:

- **Report Text Size** — font size slider (8–20 px), applied immediately
- **Search Behavior** — toggle whether Cmd/Ctrl-F opens the WATNEY search bar or the browser's native find
- **Project** — rename the project; view the annotations DB path
- **Checkpoints** — adjust the auto-checkpoint interval; trigger a manual checkpoint immediately; view last checkpoint timestamp
- **Extractions** — see all loaded extraction versions; load a new extraction CSV
- **Import Annotations** — import from a prior export CSV or another project's `annotations.db`
- **Legacy Migration** — migrate data from an old-style `watney_annotations/` folder into a new project
- **Updates** — check PyPI for a newer version; one-click update and restart prompt
- **Annotation Stats** — total annotations, unique patients, LLM-sourced vs clinician-sourced counts

Settings can be toggled closed by clicking the Settings button again.

---

## Annotation Database

Each project stores two SQLite databases:

**`annotations.db`** — all progression event annotations

| Column | Description |
|---|---|
| `DFCI_MRN` | Patient identifier |
| `progression_date` | Date of the progression event |
| `progression_source` | `LLM`, `manual`, or `exclusion_placeholder` |
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
| `extraction_version` | Filename of the extraction CSV the event came from |
| `deleted` | `1` if soft-deleted; excluded from exports and summary |
| `deletion_reason` | Required reason entered at soft-delete time |
| `deletion_timestamp` | ISO timestamp of soft-delete |
| `import_source` | Filename of the import source (imported rows only) |
| `exclusion_flag` | `True` if patient excluded (legacy / demo fallback only) |
| `exclusion_reason` | Reason for exclusion (legacy / demo fallback only) |
| `unexclusion_reason` | Reason for un-exclusion (legacy / demo fallback only) |
| `agent_note` | Discontinuation reason for the agent (clinician entries); `NA` if not set |

**`patients.db`** — one row per patient; used for exclusion tracking in project mode

| Column | Description |
|---|---|
| `DFCI_MRN` | Patient identifier |
| `date_added` | When the patient was first loaded |
| `exclusion_flag` | `True` if excluded |
| `exclusion_reason` | Reason for exclusion |
| `excluded_by` | Username who excluded |
| `excluded_at` | ISO timestamp of exclusion |
| `unexclusion_reason` | Reason for un-exclusion |
| `unexcluded_by` | Username who un-excluded |
| `unexcluded_at` | ISO timestamp of un-exclusion |

---

## Multiple Browser Tabs

If more than one browser tab connects to the WATNEY server simultaneously, a warning banner appears on the login screen. Multiple active sessions sharing a single server can cause unexpected UI behavior — close any extra tabs before annotating.

---

## Citation

```
Justin Vinh. WATNEY: Interactive Oncology Annotation Platform.
https://github.com/justin-vinh/watney
```

---

## License

MIT License
