Theervu.AI - Single File Hackathon Build
Run: python app.py
Open: http://localhost:8000


# ============================ IMPORTS ============================
import sqlite3
import uuid
import shutil
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from google import genai


# ============================ CONFIG ============================
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "grievances.db"

UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

DEPARTMENTS = {
    "pothole":       {"name": "Roads & Infrastructure Department", "email": "roads@civic.gov",       "sla_hours": 72},
    "garbage":       {"name": "Sanitation & Waste Management",     "email": "sanitation@civic.gov",  "sla_hours": 24},
    "water_leak":    {"name": "Water Supply Department",           "email": "water@civic.gov",       "sla_hours": 12},
    "streetlight":   {"name": "Electrical Department",             "email": "electrical@civic.gov",  "sla_hours": 48},
    "drainage":      {"name": "Drainage & Sewerage Department",    "email": "drainage@civic.gov",    "sla_hours": 24},
    "stray_animal":  {"name": "Animal Control",                    "email": "animals@civic.gov",     "sla_hours": 48},
    "tree_hazard":   {"name": "Parks & Horticulture",              "email": "parks@civic.gov",       "sla_hours": 36},
    "other":         {"name": "General Grievance Cell",            "email": "general@civic.gov",     "sla_hours": 96},
}


# ============================ DATABASE ============================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS grievances (
            tracking_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            department_name TEXT NOT NULL,
            department_email TEXT NOT NULL,
            sla_hours INTEGER,
            description TEXT,
            location TEXT,
            latitude REAL,
            longitude REAL,
            confidence REAL,
            status TEXT DEFAULT 'Submitted',
            file_path TEXT,
            file_type TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_id TEXT,
            status TEXT,
            note TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()


def insert_grievance(data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute("""
        INSERT INTO grievances (
            tracking_id, category, department_name, department_email,
            sla_hours, description, location, latitude, longitude,
            confidence, status, file_path, file_type, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["tracking_id"], data["category"], data["department_name"],
        data["department_email"], data["sla_hours"], data["description"],
        data["location"], data.get("latitude"), data.get("longitude"),
        data["confidence"], data["status"], data.get("file_path"),
        data.get("file_type"), now, now,
    ))
    c.execute(
        "INSERT INTO history (tracking_id, status, note, timestamp) VALUES (?, ?, ?, ?)",
        (data["tracking_id"], "Submitted", "Grievance received", now),
    )
    conn.commit()
    conn.close()


def get_grievance(tracking_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM grievances WHERE tracking_id = ?", (tracking_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_grievances(limit: int = 100):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM grievances ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_status(tracking_id: str, status: str, note: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute("UPDATE grievances SET status = ?, updated_at = ? WHERE tracking_id = ?",
              (status, now, tracking_id))
    c.execute("INSERT INTO history (tracking_id, status, note, timestamp) VALUES (?, ?, ?, ?)",
              (tracking_id, status, note, now))
    conn.commit()
    conn.close()


def get_history(tracking_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT status, note, timestamp FROM history WHERE tracking_id = ? ORDER BY timestamp ASC",
              (tracking_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT category, COUNT(*) FROM grievances GROUP BY category")
    by_category = dict(c.fetchall())
    c.execute("SELECT COUNT(*) FROM grievances")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM grievances WHERE status = 'Resolved'")
    resolved = c.fetchone()[0]
    conn.close()
    return {"total": total, "resolved": resolved, "pending": total - resolved, "by_category": by_category}


# ============================ AI CLASSIFIER ============================
KEYWORDS = {
    "pothole":      ["pothole", "pot hole", "road damage", "broken road", "crack", "asphalt", "bump", "uneven road", "sinkhole"],
    "garbage":      ["garbage", "trash", "waste", "litter", "dump", "rubbish", "bin overflow", "dirty", "smell", "rotting", "debris"],
    "water_leak":   ["water leak", "leaking", "pipe burst", "water pipe", "tap", "flooding", "sewage", "overflow", "wet road", "leak"],
    "streetlight":  ["streetlight", "street light", "lamp", "bulb", "dark road", "no light", "pole", "flickering light", "broken light"],
    "drainage":     ["drain", "drainage", "clogged", "blocked drain", "manhole", "sewer", "gutter", "stagnant water"],
    "stray_animal": ["stray dog", "stray cat", "cow", "animal", "monkey", "dog bite", "animal menace", "cattle"],
    "tree_hazard":  ["fallen tree", "tree branch", "tree", "branch", "uprooted", "hanging branch", "tree hazard"],
}

SPECIFIC_PHRASES = {
    "pothole":    ["big pothole", "deep pothole", "pothole on"],
    "water_leak": ["pipe burst", "water gushing", "water leaking"],
    "garbage":    ["garbage dump", "trash pile", "waste dumping"],
}


def classify_text(text: str) -> Tuple[str, float]:
    if not text or len(text.strip()) < 3:
        return "other", 0.3
    text_lower = text.lower()
    scores = {}
    for category, words in KEYWORDS.items():
        score = 0
        for kw in words:
            matches = len(re.findall(r"\b" + re.escape(kw) + r"\b", text_lower))
            if matches:
                score += matches * (2 if " " in kw else 1)
        for phrase in SPECIFIC_PHRASES.get(category, []):
            if phrase in text_lower:
                score += 3
        scores[category] = score
    best = max(scores, key=scores.get)
    best_score = scores[best]
    if best_score == 0:
        return "other", 0.4
    total = sum(scores.values()) or 1
    confidence = max(0.55, min(0.99, best_score / total))
    return best, round(confidence, 2)


def classify_from_image_hint(filename: str) -> Tuple[str, float]:
    return classify_text(filename.replace("_", " ").replace("-", " "))


def classify(description: str = "", filename: str = "", voice_text: str = "") -> Tuple[str, float]:
    combined = f"{description} {voice_text}".strip()
    cat_text, conf_text = classify_text(combined)
    if filename:
        cat_file, conf_file = classify_from_image_hint(filename)
    else:
        cat_file, conf_file = "other", 0.0
    if conf_text >= 0.6:
        return cat_text, conf_text
    if cat_file != "other" and conf_file > conf_text:
        return cat_file, conf_file
    return cat_text, conf_text


# ============================ FASTAPI APP ============================
app = FastAPI(title="Theervu.AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


def generate_tracking_id() -> str:
    date_str = datetime.utcnow().strftime("%Y%m%d")
    short = uuid.uuid4().hex[:6].upper()
    return f"GRV-{date_str}-{short}"


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE
  


@app.get("/uploads/{fname}")
def serve_upload(fname: str):
    from fastapi.responses import FileResponse
    p = UPLOAD_DIR / fname
    if not p.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


@app.post("/api/grievances")
async def create_grievance(
    description: str = Form(""),
    location: str = Form(...),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    voice_transcript: str = Form(""),
    file: UploadFile = File(None),
):
    if not location.strip():
        raise HTTPException(400, "Location is required")

    file_path = None
    file_type = None
    filename = ""

    if file and file.filename:
        filename = file.filename
        ext = Path(filename).suffix.lower()
        unique = f"{uuid.uuid4().hex[:8]}{ext}"
        dest = UPLOAD_DIR / unique
        with dest.open("wb") as buf:
            shutil.copyfileobj(file.file, buf)
        file_path = f"/uploads/{unique}"
        file_type = "image" if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else "audio"

    category, confidence = classify(description=description, filename=filename, voice_text=voice_transcript)
    dept = DEPARTMENTS.get(category, DEPARTMENTS["other"])
    tracking_id = generate_tracking_id()

    insert_grievance({
        "tracking_id": tracking_id,
        "category": category,
        "department_name": dept["name"],
        "department_email": dept["email"],
        "sla_hours": dept["sla_hours"],
        "description": description or voice_transcript or "(no description)",
        "location": location,
        "latitude": latitude,
        "longitude": longitude,
        "confidence": confidence,
        "status": "Submitted",
        "file_path": file_path,
        "file_type": file_type,
    })

    return {
        "success": True,
        "tracking_id": tracking_id,
        "category": category,
        "department": dept["name"],
        "department_email": dept["email"],
        "sla_hours": dept["sla_hours"],
        "confidence": confidence,
        "message": f"Routed to {dept['name']}.",
    }


@app.get("/api/grievances/{tracking_id}")
def track_grievance(tracking_id: str):
    g = get_grievance(tracking_id)
    if not g:
        raise HTTPException(404, "Tracking ID not found")
    g["history"] = get_history(tracking_id)
    return g


@app.get("/api/grievances")
def list_grievances(limit: int = 100):
    return get_all_grievances(limit)


@app.post("/api/grievances/{tracking_id}/status")
def change_status(tracking_id: str, status: str = Form(...), note: str = Form("")):
    if not get_grievance(tracking_id):
        raise HTTPException(404, "Not found")
    update_status(tracking_id, status, note)
    return {"success": True, "status": status}


@app.get("/api/stats")
def stats():
    return get_stats()


# ============================ FRONTEND (HTML + CSS + JS) ============================
HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Theervu.AI</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  color: #e2e8f0; min-height: 100vh;
}
.container { max-width: 900px; margin: 0 auto; padding: 0 20px; }
header {
  background: linear-gradient(90deg, #6366f1, #8b5cf6);
  padding: 30px 0; box-shadow: 0 8px 24px rgba(99,102,241,0.3);
}
header h1 { font-size: 2rem; }
.tag { opacity: 0.9; margin-top: 6px; font-size: 0.95rem; }
main { padding: 30px 0; }
.card {
  background: rgba(30, 41, 59, 0.85);
  border: 1px solid rgba(148, 163, 184, 0.15);
  border-radius: 16px; padding: 24px; margin-bottom: 22px;
  backdrop-filter: blur(10px); box-shadow: 0 4px 20px rgba(0,0,0,0.25);
}
.card h2 { margin-bottom: 16px; font-size: 1.25rem; color: #c7d2fe; }
label {
  display: block; margin: 14px 0 6px; font-size: 0.88rem; color: #94a3b8;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
}
input, textarea, select {
  width: 100%; padding: 12px 14px; border-radius: 10px;
  border: 1px solid rgba(148, 163, 184, 0.25);
  background: rgba(15, 23, 42, 0.6); color: #e2e8f0;
  font-size: 0.95rem; font-family: inherit; outline: none; transition: 0.2s;
}
input:focus, textarea:focus {
  border-color: #6366f1; box-shadow: 0 0 0 3px rgba(99,102,241,0.2);
}
.row { display: flex; gap: 10px; align-items: center; }
.row input { flex: 1; }
button {
  cursor: pointer; border: none; border-radius: 10px;
  font-weight: 600; font-size: 0.95rem; transition: 0.2s; font-family: inherit;
}
.btn-primary {
  background: linear-gradient(90deg, #6366f1, #8b5cf6);
  color: white; padding: 12px 24px; margin-top: 18px; width: 100%;
}
.btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(99,102,241,0.4); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary {
  background: rgba(99, 102, 241, 0.15); color: #c7d2fe;
  padding: 12px 18px; border: 1px solid rgba(99,102,241,0.3); white-space: nowrap;
}
.btn-secondary:hover { background: rgba(99,102,241,0.25); }
.muted { color: #64748b; font-size: 0.85rem; }
.hidden { display: none; }
.result-box {
  background: rgba(15, 23, 42, 0.7); border-left: 4px solid #10b981;
  padding: 16px; border-radius: 10px; margin-bottom: 12px;
}
.result-box .label { color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; }
.result-box .value { font-size: 1.05rem; font-weight: 600; color: #f1f5f9; margin-top: 2px; }
.tracking-id {
  font-family: monospace; font-size: 1.3rem; color: #10b981;
  background: rgba(16,185,129,0.1); padding: 10px 14px;
  border-radius: 8px; text-align: center; margin: 12px 0; letter-spacing: 1px;
}
.stats-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px;
}
.stat-card {
  background: rgba(15, 23, 42, 0.6); padding: 18px; border-radius: 12px;
  text-align: center; border: 1px solid rgba(148,163,184,0.15);
}
.stat-card .num { font-size: 2rem; font-weight: 700; color: #8b5cf6; }
.stat-card .lbl { color: #94a3b8; font-size: 0.85rem; margin-top: 4px; }
.timeline { margin-top: 14px; }
.timeline-item {
  display: flex; gap: 12px; padding: 10px 0;
  border-left: 2px solid #6366f1; padding-left: 16px; position: relative;
}
.timeline-item::before {
  content: ''; position: absolute; left: -7px; top: 14px;
  width: 12px; height: 12px; border-radius: 50%; background: #6366f1;
}
.timeline-item .t { font-size: 0.8rem; color: #94a3b8; }
.timeline-item .s { font-weight: 600; }
footer { text-align: center; padding: 20px 0; color: #64748b; font-size: 0.85rem; }
@media (max-width: 600px) {
  header h1 { font-size: 1.5rem; }
  .row { flex-direction: column; align-items: stretch; }
}
</style>
</head>
<body>
<header>
  <div class="container">
    <h1>🏛️ Theervu.AI</h1>
    <p class="tag">AI-powered civic issue routing — snap it, report it, track it.</p>
  </div>
</header>

<main class="container">
  <section class="card">
    <h2>📸 Report a Civic Issue</h2>
    <form id="grievanceForm">
      <label>Description (or speak below)</label>
      <textarea id="description" rows="3" placeholder="e.g., Large pothole on MG Road near bus stop..."></textarea>

      <label>📍 Location</label>
      <div class="row">
        <input id="location" type="text" placeholder="Street / Area / Landmark" required />
        <button type="button" class="btn-secondary" onclick="detectLocation()">Use My GPS</button>
      </div>

      <label>📷 Photo (optional)</label>
      <input id="photo" type="file" accept="image/*" />

      <label>🎤 Voice Note</label>
      <div class="row">
        <button type="button" class="btn-secondary" id="recordBtn">🎙️ Start Recording</button>
        <span id="recordStatus" class="muted">Not recording</span>
      </div>
      <textarea id="voiceTranscript" rows="2" placeholder="Voice transcript will appear here (or type manually)"></textarea>

      <button type="submit" class="btn-primary" id="submitBtn">🚀 Submit Complaint</button>
    </form>
  </section>

  <section id="result" class="card hidden">
    <h2>✅ Complaint Routed</h2>
    <div id="resultBody"></div>
  </section>

  <section class="card">
    <h2>🔍 Track Your Grievance</h2>
    <div class="row">
      <input id="trackInput" placeholder="Enter Tracking ID (e.g., GRV-20250101-A1B2C3)" />
      <button class="btn-primary" onclick="trackGrievance()">Track</button>
    </div>
    <div id="trackResult"></div>
  </section>

  <section class="card">
    <h2>📊 Live Dashboard</h2>
    <div id="stats" class="stats-grid"></div>
  </section>
</main>

<footer>
  <div class="container">
    <p>© 2025 Theervu.AI · Hackathon Build</p>
  </div>
</footer>

<script>
const form = document.getElementById("grievanceForm");
const submitBtn = document.getElementById("submitBtn");
const resultBox = document.getElementById("result");
const resultBody = document.getElementById("resultBody");
const recordBtn = document.getElementById("recordBtn");
const recordStatus = document.getElementById("recordStatus");
const voiceTranscript = document.getElementById("voiceTranscript");

let mediaRecorder = null;
let chunks = [];

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  submitBtn.disabled = true;
  submitBtn.textContent = "⏳ Classifying...";

  const fd = new FormData();
  fd.append("description", document.getElementById("description").value);
  fd.append("location", document.getElementById("location").value);
  fd.append("voice_transcript", voiceTranscript.value);

  const photo = document.getElementById("photo").files[0];
  if (photo) fd.append("file", photo);

  if (window._lat && window._lng) {
    fd.append("latitude", window._lat);
    fd.append("longitude", window._lng);
  }

  try {
    const res = await fetch("/api/grievances", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Error");

    resultBox.classList.remove("hidden");
    resultBody.innerHTML = `
      <div class="tracking-id">${data.tracking_id}</div>
      <div class="result-box"><div class="label">Category</div><div class="value">${formatCategory(data.category)}</div></div>
      <div class="result-box"><div class="label">Routed To</div><div class="value">${data.department}</div></div>
      <div class="result-box"><div class="label">Contact</div><div class="value">${data.department_email}</div></div>
      <div class="result-box"><div class="label">AI Confidence</div><div class="value">${(data.confidence * 100).toFixed(0)}%</div></div>
      <div class="result-box"><div class="label">SLA</div><div class="value">${data.sla_hours} hours</div></div>
      <p class="muted" style="margin-top:10px">Save your tracking ID to check status later.</p>
    `;
    form.reset();
    voiceTranscript.value = "";
    window._lat = window._lng = null;
    loadStats();
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "🚀 Submit Complaint";
  }
});

async function trackGrievance() {
  const id = document.getElementById("trackInput").value.trim();
  if (!id) return;
  const res = await fetch(`/api/grievances/${id}`);
  const box = document.getElementById("trackResult");
  if (!res.ok) {
    box.innerHTML = `<p class="muted" style="margin-top:12px">❌ Tracking ID not found.</p>`;
    return;
  }
  const g = await res.json();
  box.innerHTML = `
    <div class="result-box" style="margin-top:14px">
      <div class="label">Status</div><div class="value">${g.status}</div>
    </div>
    <div class="result-box">
      <div class="label">Category / Dept</div>
      <div class="value">${formatCategory(g.category)} → ${g.department_name}</div>
    </div>
    <div class="result-box">
      <div class="label">Location</div><div class="value">${g.location}</div>
    </div>
    <h3 style="margin-top:16px;color:#c7d2fe">Timeline</h3>
    <div class="timeline">
      ${g.history.map(h => `
        <div class="timeline-item">
          <div>
            <div class="s">${h.status}</div>
            <div class="t">${new Date(h.timestamp).toLocaleString()}</div>
            <div class="muted">${h.note || ""}</div>
          </div>
        </div>`).join("")}
    </div>
  `;
}

function detectLocation() {
  if (!navigator.geolocation) return alert("Geolocation unsupported");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      window._lat = pos.coords.latitude;
      window._lng = pos.coords.longitude;
      document.getElementById("location").value =
        `Lat: ${pos.coords.latitude.toFixed(5)}, Lng: ${pos.coords.longitude.toFixed(5)}`;
    },
    () => alert("Could not get location"),
  );
}

recordBtn.addEventListener("click", async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    chunks = [];
    mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
    mediaRecorder.onstop = () => {
      recordStatus.textContent = "Recorded ✅";
      recordBtn.textContent = "🎙️ Start Recording";
      stream.getTracks().forEach((t) => t.stop());
    };
    mediaRecorder.start();
    recordStatus.textContent = "Recording... speak now";
    recordBtn.textContent = "⏹️ Stop Recording";

    if ("webkitSpeechRecognition" in window || "SpeechRecognition" in window) {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      const recog = new SR();
      recog.continuous = true;
      recog.interimResults = true;
      recog.onresult = (e) => {
        let txt = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          txt += e.results[i][0].transcript;
        }
        voiceTranscript.value = txt;
      };
      recog.start();
      mediaRecorder.addEventListener("stop", () => recog.stop());
    }
  } catch (e) {
    alert("Mic access denied or unavailable");
  }
});

async function loadStats() {
  const res = await fetch("/api/stats");
  const s = await res.json();
  document.getElementById("stats").innerHTML = `
    <div class="stat-card"><div class="num">${s.total}</div><div class="lbl">Total</div></div>
    <div class="stat-card"><div class="num">${s.pending}</div><div class="lbl">Pending</div></div>
    <div class="stat-card"><div class="num">${s.resolved}</div><div class="lbl">Resolved</div></div>
    ${Object.entries(s.by_category).map(([k, v]) =>
      `<div class="stat-card"><div class="num">${v}</div><div class="lbl">${formatCategory(k)}</div></div>`
    ).join("")}
  `;
}

function formatCategory(c) {
  return c.replace(/_/g, " ").replace(/\\b\\w/g, (l) => l.toUpperCase());
}

loadStats();
setInterval(loadStats, 10000);
</script>
</body>
</html>
"""


# ============================ RUN ============================
if __name__ == "__main__":
    print("🏛️  Theervu.AI starting...")
    print("🌐 Open: http://localhost:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)