# IT Department Automation App

Full-stack starter for automating IT department workflows (document parsing,
scheduling, inventory requests) with a **FastAPI** backend, **Next.js**
frontend, **MongoDB Atlas** database, and **Google Gemini** for AI features.

## Project layout

```
secratary_app/
├── backend/                       # FastAPI service
│   ├── main.py                    # App entry point, CORS, /api/health
│   ├── database.py                # Async MongoDB Atlas client + ping
│   ├── routes/
│   │   ├── schedules.py           # PDF debug-parse + MongoDB sync
│   │   ├── classrooms.py          # Availability + AI recommendation
│   │   └── documents.py           # Word blueprints + .docx generation
│   ├── services/
│   │   ├── timetable_parser.py    # pdfplumber geometry-based parser
│   │   ├── schedule_store.py      # Idempotent schedules collection upsert
│   │   ├── classroom_availability.py
│   │   ├── doc_generator.py       # python-docx blueprint editing/building
│   │   └── gemini_service.py      # Gemini calls + local fallbacks
│   ├── blueprints/                # Custom uploaded .docx templates
│   ├── requirements.txt
│   ├── .env.example
│   └── .env                       # local only, git-ignored
├── frontend/                      # Next.js (App Router, TypeScript, Tailwind)
│   └── .env.local                 # NEXT_PUBLIC_API_URL
├── Emploi_du_temps_Classes_Sem2.pdf
└── README.md
```

## Prerequisites

- Python 3.11+ (tested on 3.14)
- Node.js 20+ and npm
- A MongoDB Atlas cluster ([free tier](https://www.mongodb.com/cloud/atlas/register))
- A Gemini API key ([Google AI Studio](https://aistudio.google.com/apikey)) —
  optional; classroom checks fall back to a local summary without it

## 1. Backend setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Environment

A local `backend/.env` already exists with empty secrets. Fill in the values
below (the same keys are documented in `backend/.env.example`):

```dotenv
MONGODB_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority&appName=it-department-automation
MONGODB_DB=it_automation
MONGODB_TIMEOUT_MS=5000

GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.8-flash
GEMINI_TIMEOUT_SECONDS=20

# Relative paths are resolved from the project root, backend/ and cwd.
TIMETABLE_PDF_PATH=Emploi_du_temps_Classes_Sem2.pdf
AUTO_SYNC_SCHEDULES=false

# Optional: used by the capacity_needed check.
ROOM_CAPACITIES_JSON={"I6": 30, "I5": 40, "C9": 20, "TI1": 25}

CORS_ORIGINS=http://localhost:3000
LOG_LEVEL=INFO
```

**MongoDB Atlas checklist**

1. Create a free cluster and a database user (Database Access).
2. Allow your IP under Network Access (`0.0.0.0/0` only for development).
3. Copy the Python driver connection string from *Connect*.
4. URL-encode special characters in the password (`@` → `%40`, `:` → `%3A`).

**Gemini**

Create a key at <https://aistudio.google.com/apikey> and paste it into
`GEMINI_API_KEY`. Never commit the real `.env` file. If the key is missing or
Gemini is unreachable, `/api/classrooms/check` still returns a deterministic
local recommendation with `"source": "fallback"`, and Word generation uses a
local document plan.

### Run the API

```bash
uvicorn main:app --reload --port 8000
```

- API docs: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/api/health>
- Standalone DB smoke test: `python database.py`

`GET /api/health` returns `200` when the API and Atlas are reachable, and
`503` with the connection error when Atlas is down. `GET /health` is an
unlisted compatibility alias that delegates to the same handler; frontend calls
and canonical documentation use `/api/health`:

```json
{
  "status": "ok",
  "service": "IT Department Automation API",
  "version": "0.4.0",
  "timestamp": "2026-11-15T16:00:00+00:00",
  "uptime_seconds": 12.4,
  "dependencies": {
    "mongodb": { "status": "connected", "database": "it_automation", "latency_ms": 38.2 }
  }
}
```

## 2. Timetable parsing and classroom availability

### 2.1 Inspect the parsed PDF (no MongoDB needed)

```bash
curl http://localhost:8000/api/schedules/debug-parse
```

`GET /api/schedules/debug-parse` parses
`Emploi_du_temps_Classes_Sem2.pdf` on the fly and returns the raw structured
shape:

```json
{
  "status": "parsed",
  "source_file": "Emploi_du_temps_Classes_Sem2.pdf",
  "page_count": 15,
  "class_count": 15,
  "entry_count": 265,
  "week_start": "2026-09-07",
  "department": "Département TI - ISET Radès",
  "warnings": [],
  "classes": [
    {
      "name": "L1-TI-1",
      "page": 1,
      "entry_count": 19,
      "entries": [
        {
          "entry_key": "02afe5c2…",
          "class_name": "L1-TI-1",
          "day": "lundi",
          "time_slot": "08:30-10:00",
          "start_time": "08:30",
          "end_time": "10:00",
          "slots": ["08:30-10:00"],
          "subject": "Algorithmique et résolut de Pb",
          "teacher": "ELOUEDI",
          "room": "I1",
          "week": null,
          "group": null,
          "page": 1,
          "week_start": "2026-09-07",
          "source_file": "Emploi_du_temps_Classes_Sem2.pdf"
        }
      ]
    }
  ]
}
```

The current PDF produces **15 classes and 265 course entries**. Counts may
change if the PDF is replaced.

**Schedule schema**

- `subject` is the course title (`module` was renamed to `subject`).
- `room` is the department room identifier (`I1`, `I5`, `I6`, `I8`, `TI1`,
  `TI2`, `C9`, `E2`, `G6`, numeric rooms, `ABS`, ...).
- `week` is the alternating-week badge (`Q1` or `Q2`) when the block has one;
  it is `null` when the session runs every week.
- `group` is derived from `week`: `Q1` → `G1`, `Q2` → `G2`, otherwise `null`.
  Room codes are never written to `group`.

For example, the lab entry `L1-TI-1` on `lundi 13:30-16:45` parses as:

```json
{
  "subject": "At. Système d'exploitation 1",
  "teacher": "KOUKA",
  "room": "I6",
  "week": "Q2",
  "group": "G2"
}
```

### 2.2 Store / upsert the schedules in MongoDB

```bash
curl -X POST http://localhost:8000/api/schedules/sync
```

The endpoint parses the PDF and writes one document per course entry to the
`schedules` collection. It is idempotent:

- entries are keyed by a deterministic `entry_key` and replaced on every sync;
- rows belonging to the same `source_file` that are no longer in the PDF are
  deleted;
- indexes are created on `entry_key` (unique), `day + slots + week`, `room`,
  and `class_name + day`.

Check the collection without loading the full payload:

```bash
curl http://localhost:8000/api/schedules/status
```

The status payload also includes the stored `week_start` used for the active
Q1/Q2 calculation, plus the distinct room codes currently in the collection.

Set `AUTO_SYNC_SCHEDULES=true` to run the same parse + upsert during FastAPI
startup. Startup failures are logged and do not prevent the API from booting.

### 2.3 Check classroom availability

```bash
curl -X POST http://localhost:8000/api/classrooms/check \
  -H "Content-Type: application/json" \
  -d '{"day":"lundi","time_slot":"13:30-16:45","capacity_needed":30,"target_date":"2026-09-07"}'
```

- `day` accepts `lundi` … `samedi` (also `Lundi`, `lun.`, and English day names).
- `time_slot` accepts `08:30-10:00`, `8h30-10h00`, and en/em-dash variants.
- A range can overlap several periods, e.g. `08:30-11:45`; every overlapping
  course entry is considered occupied.
- `capacity_needed` is optional. Rooms with a configured capacity smaller than
  the request are marked `suitable_for_capacity: false`; rooms with unknown
  capacity remain available but the response tells the caller to verify seats.
- `target_date` is optional and defaults to the server's current date. It is
  used to choose the active alternating week, so the endpoint can answer
  “what is free next Monday?” rather than only “right now”.

**Active week filtering.** The stored `week_start` (`2026-09-07` for the
current PDF) is week 0 = `Q1`. Every seven days the week alternates:

```text
weeks_elapsed = (target_date - week_start).days // 7
active_week   = "Q1" if weeks_elapsed % 2 == 0 else "Q2"
```

Occupancy is filtered by this active week. An entry marked `Q1` does not
occupy its room during a `Q2` week (and vice versa), while entries with
`week: null` run every week and always occupy their room. For example, `I6`
is free on `lundi 13:30-16:45` on `2026-09-07` (Q1) because its only booking
in that slot is the `Q2` lab entry above.

Abbreviated response for the Q1 request:

```json
{
  "day": "lundi",
  "time_slot": "13:30-16:45",
  "requested_slots": ["13:30-15:00", "15:15-16:45"],
  "target_date": "2026-09-07",
  "week_start": "2026-09-07",
  "active_week": "Q1",
  "capacity_needed": 30,
  "total_rooms": 28,
  "available_count": 16,
  "available_room_names": ["11", "12", "ABS", "C1", "C9", "E2", "E4", "G5", "GE10", "GE11", "GER19", "I6", "I8", "I11", "I12", "TI1"],
  "suitable_room_names": ["11", "12", "ABS", "C1", "C9", "E2", "E4", "G5", "GE10", "GE11", "GER19", "I6", "I8", "I11", "I12", "TI1"],
  "occupied_rooms": ["G6", "I1", "I2", "I3", "I4", "I5", "I7", "I9", "I10", "I11(SP1)", "I12(AB17)", "TI2"],
  "occupancies": [
    {
      "room": "I1",
      "class_name": "L2-RSI-2",
      "subject": "At. BD",
      "teacher": "MSEKNI",
      "week": "Q1",
      "group": "G1",
      "time_slot": "13:30-16:45"
    }
  ],
  "source": "mongodb",
  "ai": {
    "summary": "16 of 28 rooms are free on lundi during 13:30-16:45 in the active Q1 week.",
    "recommendation": "Suggested room: 11. Capacity data is not configured for 11, so verify that it seats at least 30 people. Occupied rooms: G6, I1, I2, I3, I4, I5, I7, I9, I10, I11(SP1), I12(AB17), TI2.",
    "recommended_room": "11",
    "source": "fallback",
    "model": null,
    "error": "GEMINI_API_KEY is not configured"
  }
}
```

Without `target_date`, the server date on this machine (2026-11-15) falls in a
`Q2` week, so the same request reports `active_week: "Q2"` and marks `I6` as
occupied by the `L1-TI-1` lab session.

When Gemini is configured, `ai.source` is `"gemini"` and the model only sees
the availability data computed from MongoDB. Any room it recommends is
validated against the free-room list and the capacity filter; a hallucinated
or undersized room triggers the local fallback instead of reaching the user.

### Endpoint error behaviour

| Status | Meaning |
| --- | --- |
| `404` | Timetable PDF not found at the configured/default path |
| `409` | MongoDB is reachable but no schedules are stored yet |
| `422` | The PDF exists but could not be parsed |
| `503` | MongoDB Atlas is unavailable or schedules cannot be queried |

### Parser design

`backend/services/timetable_parser.py` uses `pdfplumber` geometry instead of
the PDF text order:

- detects the six time-slot column edges and six day-row edges from vector
  lines;
- reads each filled course rectangle, maps it to its day and one or more time
  slots;
- groups words into visual lines, then separates subject, teacher, room and
  the optional alternating-week badge;
- maps department room identifiers (`I1`, `I5`, `I6`, `I8`, `TI1`, `TI2`,
  `C9`, `E2`, `G6`, numeric rooms, ...) directly to `room`, never to `group`;
- derives `group` from `week` (`Q1` → `G1`, `Q2` → `G2`, no badge → `null`);
- handles multi-slot blocks (`slots` list), parallel sessions in half-height
  cells, alternating-week badges (`Q1`, `Q2`) and a text-layer overflow glitch
  in the source PDF;
- returns warnings per page instead of failing the whole document when one page
  is malformed; a completely unparseable PDF returns `422`.

## 3. Word document generation

Part 3 adds Word-only document automation. Excel generation is intentionally
not implemented yet: `doc_type: "excel"` returns `400`.

Generation is prompt-driven. Gemini (`GEMINI_MODEL`, default
`gemini-3.8-flash`) returns a structured routing decision:

| Action | Result |
| --- | --- |
| `use_blueprint` | An allowlisted `.docx` is copied with no changes or an optional date update. |
| `modify_blueprint` | Targeted edits are applied on a copy of an allowlisted `.docx`. |
| `create_new` | A clean `.docx` is built from the enriched `new_doc` structure. |

With no `blueprint_id` (or an empty string) the router is in **Auto mode**: it
uses a blueprint only when the request clearly matches an allowlisted template.
Phrases such as `from scratch`, `without blueprint`, `new document`, `sans
modèle` or `nouveau document` always force `create_new`. Low-confidence or
invalid blueprint selections are hard-gated to `create_new`, which is built
from a complete `new_doc` payload: `title`, optional `subtitle`, `date_line`,
`header_note`, `sections` (`heading`, `paragraphs`, `bullets`,
`numbered_items`), `tables` (`title`, `headers`, `rows`), and an optional
`signature_block`. Date/signature lines are only rendered when they are
present in the payload, so unrelated documents never receive a forced
`Radès, le ...` line. An explicit allowlisted `blueprint_id` still overrides
Auto mode.

Blueprints are restricted to an explicit allowlist: two bundled `Liste des
Besoins` templates plus templates registered through the upload endpoint
(`backend/blueprints/registry.json`). A blueprint reference outside that
allowlist is rejected, and any route that points to a missing/non-allowlisted
id is hard-gated to `create_new`. Templates are opened in memory, so originals
are never modified.

### 3.1 List allowlisted `.docx` blueprints

```bash
curl http://localhost:8000/api/documents/blueprints
```

```json
{
  "count": 2,
  "allowlisted": true,
  "blueprints": [
    {
      "id": "liste-besoins-2024",
      "name": "Liste des Besoins 2024",
      "filename": "Liste des Besoins 2024.docx",
      "title": "Liste des Besoins 2024",
      "description": "Modèle historique de liste des besoins ...",
      "source": "builtin",
      "location": "project_root",
      "size_bytes": 48633,
      "modified_at": "2026-11-14T19:55:01+00:00",
      "structure": {
        "headings": ["LISTE DES BESOINS"],
        "tables": [
          {
            "index": 0,
            "header": ["N°", "Désignation", "Quantité"],
            "row_count": 6,
            "column_count": 3,
            "kind": "department_list"
          }
        ],
        "paragraph_count": 12,
        "table_count": 4
      }
    }
  ],
  "upload_directory": "backend/blueprints"
}
```

### 3.2 Upload a custom blueprint

Only `.docx` files are accepted; anything else returns `400`. Files are saved
to `backend/blueprints/` and explicitly registered in the allowlist. Use
`?overwrite=true` to replace an existing file with the same name; otherwise a
numbered filename is chosen.

```bash
curl -X POST http://localhost:8000/api/documents/blueprints/upload \
  -F "file=@/path/to/Custom Template.docx"
```

### 3.3 Generate a Word document

Free-form prompt; the router chooses the mode automatically:

```bash
curl -X POST http://localhost:8000/api/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "doc_type": "word",
    "prompt": "Add 10 HDMI cables and remove scotch from the Liste des Besoins, date 22/11/2026",
    "output_name": "Liste des Besoins - novembre 2026.docx"
  }' \
  --output "Liste des Besoins - novembre 2026.docx"
```

Force an allowlisted template with `blueprint_id`:

```bash
curl -X POST http://localhost:8000/api/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Use this template and update the date to today.",
    "blueprint_id": "liste-besoins-sept-2024"
  }' \
  --output "Liste des Besoins - aujourd hui.docx"
```

Standalone document, optionally enriched with stored schedules for a class:

```bash
curl -X POST http://localhost:8000/api/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Prepare a clean timetable handout with a notes section.",
    "target_class": "L1-TI-1"
  }' \
  --output "Emploi du temps L1-TI-1.docx"
```

The legacy fields `subject`, `instructions`, `blueprint_name` and `output_name`
are still accepted. `blueprint_name` resolves only against the allowlist.
`target_class` only triggers a read-only MongoDB `schedules` lookup — stored
schedules are never modified.

The response is a downloadable `.docx`:

```text
Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document
Content-Disposition: attachment; filename="..."
X-Doc-Action: modify_blueprint
X-Blueprint-Used: liste-besoins-2024
X-AI-Source: gemini | fallback
X-AI-Model: gemini-2.5-flash-lite | fallback
X-Applied-Edits: 3
X-Doc-Summary: <percent-encoded UTF-8 summary>
X-Doc-Explanation: <percent-encoded UTF-8 routing explanation>
```

**Error behaviour**

| Status | Meaning |
| --- | --- |
| `400` | Excel requested, invalid/empty upload, unreadable blueprint, or explicit non-allowlisted blueprint |
| `404` | Explicit allowlisted blueprint file is missing |
| `503` | MongoDB is unavailable for `target_class`, or the router failed |

Without `GEMINI_API_KEY`, or if every Gemini attempt fails, routing and planning
continue with a deterministic local fallback. The `X-AI-Source` response header
reports `gemini` or `fallback`, and `X-AI-Model` reports the model that succeeded
(or `fallback`). The `X-Doc-Summary` and `X-Doc-Explanation` headers are
percent-encoded UTF-8 so the UI can show them safely.

Routing tries `GEMINI_MODEL` first, then `GEMINI_FALLBACK_MODELS` (default:
`gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-2.5-flash`).
Each attempt uses `GEMINI_TIMEOUT_SECONDS` (default `30`); timeouts, `404`,
`429` and `5xx` responses rotate to the next model within the overall
`GEMINI_TOTAL_TIMEOUT_SECONDS` budget (default `75`).

## 4. Frontend setup

```bash
cd frontend
npm install
```

`frontend/.env.local` already contains:

```dotenv
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Start the dev server:

```bash
npm run dev
```

Open <http://localhost:3000>; the dashboard shows the live API + Atlas status
from `GET /api/health`, plus a **Word Document Assistant** section. Enter a
free-form prompt, pick an optional allowlisted template override and optional
`target_class`, then download the generated `.docx`. The interface intentionally
does not display routing/mode badges; it only shows a concise summary and the
download link.

## Notes

- **Driver:** `database.py` uses PyMongo's native async client
  (`AsyncMongoClient`). Motor is included in `requirements.txt` for
  compatibility but is deprecated and not imported by the app.
- **CORS:** add every frontend origin to `CORS_ORIGINS` (comma-separated).
- **Secrets:** `.env` files are git-ignored; only `.env.example` is committed.
- **MongoDB collections:** `schedules` is the only collection written in
  Part 2. Each document is one course entry plus `imported_at`. Part 3 only
  reads schedules when `target_class` is supplied.
- **Word blueprints:** bundled examples stay in the project root; custom
  uploads are stored in `backend/blueprints/` and take resolution priority.
  All usable templates must be in the explicit allowlist; routing never accepts
  an invented blueprint id.
- **Excel:** skipped in Part 3. `openpyxl` remains installed for future work,
  but `doc_type: "excel"` returns `400`.
