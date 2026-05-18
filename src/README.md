# WATNEY

WATNEY is an interactive NiceGUI-based oncology annotation platform for reviewing and validating LLM-extracted progression events and systemic therapy timelines from longitudinal clinical notes.

Developed by Justin Vinh at Dana-Farber Cancer Institute.

---

# Features

- Review LLM-extracted progression events
- Assign progression events to systemic therapy agents
- Add clinician-derived progression events manually
- View longitudinal oncology notes with evidence highlighting
- Track reviewer annotations in SQLite
- Export annotations to CSV
- Multi-user annotation support
- Persistent configuration and session settings
- Interactive patient navigation interface

---

# Installation

## Install from PyPI

```bash
pip install watney
```

---

# Launching WATNEY

After installation:

```bash
watney
```

This launches the NiceGUI application locally in your browser.

---

# Required Input Data

WATNEY expects a CSV containing:

- Longitudinal note text
- LLM extraction JSON
- Patient identifiers

The following columns are required:

| Column Name | Description |
|---|---|
| `DFCI_MRN` | Patient identifier |
| `all_notes` | Concatenated longitudinal notes |
| `generation` | LLM-generated JSON extraction |

---

# Expected Note Formatting

Notes should contain metadata fields similar to:

```text
Note Number: 1
Note Report ID: REPORT123
Note Date: 2024-01-01
Note Dept: Medical Oncology
Note Author: Jane Smith
```

Notes should be separated using:

```text
====================
```

---

# Expected JSON Structure

The `generation` column should contain JSON with structures similar to:

```json
{
  "systemic_therapy": {
    "agents": [
      {
        "drug_name": "Pembrolizumab",
        "intervals": [
          {
            "start_date": "2023-01-01",
            "end_date": "2023-06-01"
          }
        ]
      }
    ]
  },
  "progression": {
    "progression_events": [
      {
        "progression_date": "2023-07-01",
        "confidence_level": "high",
        "progression_date_rationale": {
          "report_id": "REPORT123",
          "note_date": "2023-07-01",
          "author": "Dr. Smith",
          "text": "Evidence excerpt here"
        }
      }
    ]
  }
}
```

---

# First-Time Setup

When WATNEY launches:

1. Enter your username
2. Provide the path to your extraction CSV
3. WATNEY will persist this configuration locally
4. Future launches automatically reload the prior CSV

Configuration is stored in:

```text
./watney_annotations/watney_config.json
```

---

# Annotation Database

Annotations are automatically stored in SQLite.

Default database location:

```text
./watney_annotations/progression_annotations_database.db
```

---

# User Workflow

## 1. Review LLM Progression Events

The left panel displays:

- Extracted progression dates
- Confidence levels
- Supporting evidence
- Source metadata

---

## 2. Navigate to Source Notes

Click:

```text
Source
```

WATNEY automatically:

- Navigates to the originating note
- Scrolls to the evidence
- Highlights the relevant text

---

## 3. Assign Progression Events to Agents

For each progression event:

1. Select a systemic therapy agent
2. Click:

```text
Save Agent Assignment
```

Assignments are immediately persisted to SQLite.

---

## 4. Add Clinician-Derived Events

Use:

```text
Add Clinician Progression Event
```

Fields include:

- Progression date
- Agent
- Evidence
- Report ID
- Determined by

---

## 5. Export Annotations

Click:

```text
Export
```

WATNEY exports all annotations as a timestamped CSV.

---

# Navigation Controls

| Button | Function |
|---|---|
| Prev | Previous patient |
| Next | Next patient |
| Patient List | Open patient navigator |
| Export | Export annotations |
| Settings | Configure paths and database |

---

# License

MIT License

---

# Citation

```text
Justin Vinh. WATNEY: Interactive Oncology Annotation Platform.
https://github.com/justin-vinh/watney
```
