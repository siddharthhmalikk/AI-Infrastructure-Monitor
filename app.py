from flask import Flask, jsonify, render_template_string
import os
import sqlite3
import threading
import time
from datetime import datetime

import psutil
from openai import OpenAI

app = Flask(__name__)

# =========================
# CONFIG
# =========================
CPU_LIMIT = 80
RAM_LIMIT = 80
DISK_LIMIT = 90
CRITICAL_CPU = 95
CRITICAL_RAM = 95
CRITICAL_DISK = 98
DATABASE = "incidents.db"
AI_COOLDOWN = 30

# =========================
# NVIDIA CLIENT
# =========================
API_KEY = os.environ.get("NVIDIA_API_KEY")
client = None
if API_KEY:
    try:
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=API_KEY,
            timeout=15,
        )
    except Exception as exc:
        print("NVIDIA client error:", exc)

# =========================
# RUNTIME STATE
# =========================
state_lock = threading.Lock()
active_incident_id = None
last_ai_time = 0.0
last_ai_severity = "Normal"
last_analysis = (
    "Severity: Normal\n\n"
    "Likely Cause: No critical system resource threshold exceeded.\n\n"
    "Recommended Action: Continue monitoring the system."
)
ai_thread_running = False

# =========================
# DATABASE
# =========================
REQUIRED_COLUMNS = {
    "started_at": "TEXT",
    "resolved_at": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'OPEN'",
    "severity": "TEXT NOT NULL DEFAULT 'Warning'",
    "trigger": "TEXT NOT NULL DEFAULT 'System'",
    "start_cpu": "REAL NOT NULL DEFAULT 0",
    "start_ram": "REAL NOT NULL DEFAULT 0",
    "start_disk": "REAL NOT NULL DEFAULT 0",
    "latest_cpu": "REAL NOT NULL DEFAULT 0",
    "latest_ram": "REAL NOT NULL DEFAULT 0",
    "latest_disk": "REAL NOT NULL DEFAULT 0",
    "duration_seconds": "INTEGER",
    "analysis": "TEXT NOT NULL DEFAULT ''",
}


def db_connection():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                resolved_at TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                severity TEXT NOT NULL DEFAULT 'Warning',
                trigger TEXT NOT NULL DEFAULT 'System',
                start_cpu REAL NOT NULL DEFAULT 0,
                start_ram REAL NOT NULL DEFAULT 0,
                start_disk REAL NOT NULL DEFAULT 0,
                latest_cpu REAL NOT NULL DEFAULT 0,
                latest_ram REAL NOT NULL DEFAULT 0,
                latest_disk REAL NOT NULL DEFAULT 0,
                duration_seconds INTEGER,
                analysis TEXT NOT NULL DEFAULT ''
            )
            """
        )
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(incidents)").fetchall()
        }
        for name, definition in REQUIRED_COLUMNS.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE incidents ADD COLUMN {name} {definition}"
                )


def load_active_incident():
    global active_incident_id
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM incidents
            WHERE status = 'OPEN'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    active_incident_id = row["id"] if row else None


def get_counts():
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN severity = 'Critical' THEN 1 ELSE 0 END) AS critical,
                SUM(CASE WHEN status = 'RESOLVED' THEN 1 ELSE 0 END) AS resolved
            FROM incidents
            """
        ).fetchone()
    return {
        "total": row["total"] or 0,
        "active": row["active"] or 0,
        "critical": row["critical"] or 0,
        "resolved": row["resolved"] or 0,
    }


def get_history(limit=30):
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, resolved_at, status, severity, trigger,
                   start_cpu, start_ram, start_disk,
                   latest_cpu, latest_ram, latest_disk,
                   duration_seconds, analysis
            FROM incidents
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]

# =========================
# MONITORING
# =========================
def get_system_stats():
    try:
        cpu = psutil.cpu_percent(interval=0.2)
    except Exception:
        cpu = 0
    try:
        ram = psutil.virtual_memory().percent
    except Exception:
        ram = 0
    try:
        disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/").percent
    except Exception:
        disk = 0
    return {"cpu": round(cpu, 1), "ram": round(ram, 1), "disk": round(disk, 1)}


def get_top_processes():
    result = []
    for process in psutil.process_iter(["pid", "name", "memory_percent"]):
        try:
            name = process.info.get("name") or "Unknown"
            if name.lower() == "system idle process":
                continue
            cpu = process.cpu_percent(interval=None)
            memory = process.info.get("memory_percent") or 0
            result.append({
                "pid": process.info.get("pid"),
                "name": name,
                "cpu": round(cpu, 1),
                "memory": round(memory, 2),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    result.sort(key=lambda x: (x["cpu"], x["memory"]), reverse=True)
    return result[:5]


def severity_for(stats):
    if (
        stats["cpu"] >= CRITICAL_CPU
        or stats["ram"] >= CRITICAL_RAM
        or stats["disk"] >= CRITICAL_DISK
    ):
        return "Critical"
    if (
        stats["cpu"] >= CPU_LIMIT
        or stats["ram"] >= RAM_LIMIT
        or stats["disk"] >= DISK_LIMIT
    ):
        return "Warning"
    return "Normal"


def triggers_for(stats):
    out = []
    if stats["cpu"] >= CPU_LIMIT:
        out.append("CPU")
    if stats["ram"] >= RAM_LIMIT:
        out.append("RAM")
    if stats["disk"] >= DISK_LIMIT:
        out.append("Disk")
    return out

# =========================
# AI
# =========================
def fallback_analysis(stats, severity):
    problems = []
    if stats["cpu"] >= CPU_LIMIT:
        problems.append(f"high CPU usage ({stats['cpu']}%)")
    if stats["ram"] >= RAM_LIMIT:
        problems.append(f"high RAM usage ({stats['ram']}%)")
    if stats["disk"] >= DISK_LIMIT:
        problems.append(f"high disk usage ({stats['disk']}%)")
    cause = ", ".join(problems) if problems else "elevated system resource usage"
    return (
        f"Severity: {severity}\n\n"
        f"Likely Cause: {cause}.\n\n"
        "Recommended Action: Check the top resource-consuming processes and reduce unnecessary system load."
    )


def analyze_with_nvidia(stats, processes):
    if client is None:
        raise RuntimeError("NVIDIA_API_KEY is not configured.")
    process_text = "\n".join(
        f"{p['name']} (PID {p['pid']}) | CPU: {p['cpu']}% | RAM: {p['memory']}%"
        for p in processes
    )
    prompt = f"""
You are an expert IT infrastructure monitoring assistant.

System metrics:
CPU: {stats['cpu']}%
RAM: {stats['ram']}%
Disk: {stats['disk']}%

Top processes:
{process_text}

Return ONLY:
Severity: Normal, Warning, or Critical
Likely Cause: Brief explanation
Recommended Action: Brief practical action

Do not provide reasoning, a thinking process, markdown, or extra text.
"""
    response = client.chat.completions.create(
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        messages=[
            {
                "role": "system",
                "content": "You are an expert IT infrastructure monitoring assistant.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=150,
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": False
            }
        },
    )
    text = response.choices[0].message.content
    if not text:
        raise RuntimeError("NVIDIA returned an empty response.")
    return text.replace("\\n", "\n").strip()


def ai_worker(incident_id, stats, processes):
    global ai_thread_running, last_ai_time, last_ai_severity, last_analysis
    try:
        try:
            analysis = analyze_with_nvidia(stats, processes)
        except Exception as exc:
            print("AI analysis error:", exc)
            analysis = fallback_analysis(stats, severity_for(stats))
        with state_lock:
            last_analysis = analysis
            last_ai_time = time.time()
            last_ai_severity = severity_for(stats)
        with db_connection() as conn:
            conn.execute(
                "UPDATE incidents SET analysis = ? WHERE id = ?",
                (analysis, incident_id),
            )
    finally:
        with state_lock:
            ai_thread_running = False

# =========================
# INCIDENT LIFECYCLE
# =========================
def open_incident(stats, severity, analysis):
    global active_incident_id
    trigger = ", ".join(triggers_for(stats)) or "System"
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO incidents (
                started_at, status, severity, trigger,
                start_cpu, start_ram, start_disk,
                latest_cpu, latest_ram, latest_disk, analysis
            ) VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started,
                severity,
                trigger,
                stats["cpu"],
                stats["ram"],
                stats["disk"],
                stats["cpu"],
                stats["ram"],
                stats["disk"],
                analysis,
            ),
        )
        active_incident_id = cur.lastrowid
    return active_incident_id


def update_active_incident(stats, severity, analysis):
    if active_incident_id is None:
        return
    trigger = ", ".join(triggers_for(stats)) or "System"
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE incidents
            SET severity = ?, trigger = ?,
                latest_cpu = ?, latest_ram = ?, latest_disk = ?, analysis = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (
                severity,
                trigger,
                stats["cpu"],
                stats["ram"],
                stats["disk"],
                analysis,
                active_incident_id,
            ),
        )


def resolve_active(stats):
    global active_incident_id
    if active_incident_id is None:
        return
    now = datetime.now()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT started_at FROM incidents WHERE id = ?",
            (active_incident_id,),
        ).fetchone()
        if row:
            started = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
            duration = max(0, int((now - started).total_seconds()))
            conn.execute(
                """
                UPDATE incidents
                SET status = 'RESOLVED', resolved_at = ?,
                    latest_cpu = ?, latest_ram = ?, latest_disk = ?,
                    duration_seconds = ?
                WHERE id = ?
                """,
                (
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    stats["cpu"],
                    stats["ram"],
                    stats["disk"],
                    duration,
                    active_incident_id,
                ),
            )
    active_incident_id = None

# =========================
# DASHBOARD HTML
# =========================
HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Infrastructure Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;background:#111827;color:#f9fafb;font-family:Arial,sans-serif}.main{max-width:1200px;margin:auto;padding:35px 25px 60px}h1{text-align:center;font-size:36px;margin:0 0 25px}h2{margin-top:0}.status{text-align:center;margin-bottom:25px}.badge{display:inline-block;padding:9px 20px;border-radius:30px;font-weight:bold}.normal{background:#14532d;color:#86efac}.warning{background:#78350f;color:#fde68a}.critical{background:#7f1d1d;color:#fecaca}.cards,.incident-cards,.charts{display:grid;gap:20px}.cards{grid-template-columns:repeat(3,1fr)}.incident-cards{grid-template-columns:repeat(4,1fr);margin-top:20px}.card,.stat-card,.section{background:#1f2937;border-radius:14px}.card{text-align:center;padding:25px}.card-title,.stat-title{color:#d1d5db}.value{font-size:36px;color:#60a5fa;font-weight:bold;margin-top:10px}.stat-card{text-align:center;padding:20px}.stat-value{font-size:30px;font-weight:bold;margin-top:8px}.total{color:#93c5fd}.active{color:#fde68a}.crit{color:#fca5a5}.resolved{color:#86efac}.charts{grid-template-columns:1fr 1fr;margin-top:25px}.section{padding:25px;margin-top:25px}table{width:100%;border-collapse:collapse}th,td{padding:13px;text-align:left;border-bottom:1px solid #374151}th{color:#93c5fd}.analysis{white-space:pre-line;line-height:1.8;color:#d1fae5}.empty{text-align:center;color:#9ca3af;padding:20px}.clickable{cursor:pointer}.clickable:hover{background:#293548}.detail{display:none;background:#172033;border:1px solid #374151;border-radius:12px;padding:20px;margin-top:20px}.detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.metric{background:#1f2937;padding:14px;border-radius:10px}.metric-label{font-size:13px;color:#9ca3af}.metric-value{margin-top:5px;font-weight:bold}.timestamp{text-align:center;color:#9ca3af;font-size:14px;margin-top:25px}@media(max-width:900px){.cards,.incident-cards,.charts{grid-template-columns:1fr 1fr}}@media(max-width:600px){.cards,.incident-cards,.charts,.detail-grid{grid-template-columns:1fr}h1{font-size:28px}}
</style>
</head>
<body>
<div class="main">
<h1>AI Infrastructure Monitor</h1>
<div class="status"><span id="severity" class="badge normal">System Status: Normal</span></div>
<div class="cards">
<div class="card"><div class="card-title">CPU Usage</div><div id="cpu" class="value">0%</div></div>
<div class="card"><div class="card-title">RAM Usage</div><div id="ram" class="value">0%</div></div>
<div class="card"><div class="card-title">Disk Usage</div><div id="disk" class="value">0%</div></div>
</div>
<div class="incident-cards">
<div class="stat-card"><div class="stat-title">Total Incidents</div><div id="total" class="stat-value total">0</div></div>
<div class="stat-card"><div class="stat-title">Active Incidents</div><div id="active" class="stat-value active">0</div></div>
<div class="stat-card"><div class="stat-title">Critical Incidents</div><div id="critical" class="stat-value crit">0</div></div>
<div class="stat-card"><div class="stat-title">Resolved Incidents</div><div id="resolved" class="stat-value resolved">0</div></div>
</div>
<div class="charts">
<div class="section"><h2>CPU Usage History</h2><canvas id="cpuChart"></canvas></div>
<div class="section"><h2>RAM Usage History</h2><canvas id="ramChart"></canvas></div>
</div>
<div class="section"><h2>Top Processes</h2><table><thead><tr><th>Process</th><th>CPU %</th><th>RAM %</th></tr></thead><tbody id="process-list"></tbody></table></div>
<div class="section"><h2>AI System Analysis</h2><div id="analysis" class="analysis">Loading...</div></div>
<div class="section"><h2>Incident History</h2><p style="color:#9ca3af">Click an incident to view full details.</p><table><thead><tr><th>Started</th><th>Status</th><th>Severity</th><th>Trigger</th><th>Duration</th><th>Details</th></tr></thead><tbody id="history-list"></tbody></table><div id="detail" class="detail"></div></div>
<div id="timestamp" class="timestamp">Waiting for data...</div>
</div>
<script>
const MAX_POINTS=20;
const cpuChart=new Chart(document.getElementById('cpuChart'),{type:'line',data:{labels:[],datasets:[{label:'CPU %',data:[],borderColor:'#60a5fa',backgroundColor:'#60a5fa',borderWidth:3,tension:.3}]},options:{responsive:true,animation:false,scales:{y:{min:0,max:100}}}});
const ramChart=new Chart(document.getElementById('ramChart'),{type:'line',data:{labels:[],datasets:[{label:'RAM %',data:[],borderColor:'#34d399',backgroundColor:'#34d399',borderWidth:3,tension:.3}]},options:{responsive:true,animation:false,scales:{y:{min:0,max:100}}}});
function addPoint(chart,label,value){chart.data.labels.push(label);chart.data.datasets[0].data.push(value);if(chart.data.labels.length>MAX_POINTS){chart.data.labels.shift();chart.data.datasets[0].data.shift()}chart.update()}
function updateProcesses(items){const list=document.getElementById('process-list');list.innerHTML='';items.forEach(p=>{const tr=document.createElement('tr');['name','cpu','memory'].forEach((key,i)=>{const td=document.createElement('td');td.textContent=i===0?p[key]:p[key]+'%';tr.appendChild(td)});list.appendChild(tr)})}
function formatDuration(seconds){if(seconds===null||seconds===undefined)return'OPEN';const m=Math.floor(seconds/60);const s=seconds%60;return m?`${m}m ${s}s`:`${s}s`}
function updateHistory(history){const list=document.getElementById('history-list');list.innerHTML='';if(!history.length){list.innerHTML='<tr><td colspan="6" class="empty">No incidents recorded yet.</td></tr>';return}history.forEach(event=>{const tr=document.createElement('tr');tr.className='clickable';tr.onclick=()=>showDetails(event);tr.innerHTML=`<td>${event.started_at}</td><td>${event.status}</td><td class="${event.severity.toLowerCase()}">${event.severity}</td><td>${event.trigger}</td><td>${formatDuration(event.duration_seconds)}</td><td>View</td>`;list.appendChild(tr)})}
function showDetails(event){const panel=document.getElementById('detail');panel.style.display='block';panel.innerHTML=`<h3>Incident #${event.id}</h3><div class="detail-grid"><div class="metric"><div class="metric-label">Status</div><div class="metric-value">${event.status}</div></div><div class="metric"><div class="metric-label">Severity</div><div class="metric-value">${event.severity}</div></div><div class="metric"><div class="metric-label">Trigger</div><div class="metric-value">${event.trigger}</div></div><div class="metric"><div class="metric-label">Started</div><div class="metric-value">${event.started_at}</div></div><div class="metric"><div class="metric-label">Resolved</div><div class="metric-value">${event.resolved_at||'Not resolved yet'}</div></div><div class="metric"><div class="metric-label">Duration</div><div class="metric-value">${formatDuration(event.duration_seconds)}</div></div><div class="metric"><div class="metric-label">Start CPU</div><div class="metric-value">${event.start_cpu}%</div></div><div class="metric"><div class="metric-label">Start RAM</div><div class="metric-value">${event.start_ram}%</div></div><div class="metric"><div class="metric-label">Start Disk</div><div class="metric-value">${event.start_disk}%</div></div><div class="metric"><div class="metric-label">Latest CPU</div><div class="metric-value">${event.latest_cpu}%</div></div><div class="metric"><div class="metric-label">Latest RAM</div><div class="metric-value">${event.latest_ram}%</div></div><div class="metric"><div class="metric-label">Latest Disk</div><div class="metric-value">${event.latest_disk}%</div></div></div><h3 style="margin-top:20px">AI Analysis</h3><div class="analysis"></div>`;panel.querySelector('.analysis').textContent=event.analysis||'Analysis pending...';panel.scrollIntoView({behavior:'smooth',block:'nearest'})}
let running=false;
async function updateDashboard(){if(running)return;running=true;try{const res=await fetch('/api/stats',{cache:'no-store'});if(!res.ok)throw new Error('Server error '+res.status);const data=await res.json();document.getElementById('cpu').textContent=data.stats.cpu+'%';document.getElementById('ram').textContent=data.stats.ram+'%';document.getElementById('disk').textContent=data.stats.disk+'%';document.getElementById('total').textContent=data.counts.total;document.getElementById('active').textContent=data.counts.active;document.getElementById('critical').textContent=data.counts.critical;document.getElementById('resolved').textContent=data.counts.resolved;const badge=document.getElementById('severity');badge.textContent='System Status: '+data.severity;badge.className='badge '+data.severity.toLowerCase();updateProcesses(data.processes);document.getElementById('analysis').textContent=data.analysis;updateHistory(data.history);document.getElementById('timestamp').textContent='Last Updated: '+data.timestamp;addPoint(cpuChart,data.time,data.stats.cpu);addPoint(ramChart,data.time,data.stats.ram)}catch(err){console.error(err);document.getElementById('analysis').textContent='Unable to update dashboard. Retrying...'}finally{running=false}}
updateDashboard();setInterval(updateDashboard,5000);
</script>
</body>
</html>
"""

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/stats")
def api_stats():
    global last_analysis, last_ai_time, last_ai_severity, ai_thread_running
    stats = get_system_stats()
    processes = get_top_processes()
    severity = severity_for(stats)
    now = time.time()

    with state_lock:
        current_analysis = last_analysis
        ai_running = ai_thread_running

    if severity == "Normal":
        if active_incident_id is not None:
            resolve_active(stats)
        current_analysis = (
            "Severity: Normal\n\n"
            "Likely Cause: No critical system resource threshold exceeded.\n\n"
            "Recommended Action: Continue monitoring the system."
        )
        with state_lock:
            last_analysis = current_analysis
            last_ai_severity = "Normal"
    else:
        with state_lock:
            should_start_ai = (
                not ai_running
                and (
                    active_incident_id is None
                    or severity != last_ai_severity
                    or now - last_ai_time >= AI_COOLDOWN
                )
            )
        if active_incident_id is None:
            current_analysis = fallback_analysis(stats, severity)
            incident_id = open_incident(stats, severity, current_analysis)
        else:
            incident_id = active_incident_id
            current_analysis = last_analysis
            update_active_incident(stats, severity, current_analysis)

        if should_start_ai:
            with state_lock:
                ai_thread_running = True
            threading.Thread(
                target=ai_worker,
                args=(incident_id, stats, processes),
                daemon=True,
            ).start()

        with state_lock:
            current_analysis = last_analysis

    return jsonify({
        "stats": stats,
        "processes": processes,
        "severity": severity,
        "analysis": current_analysis,
        "history": get_history(),
        "counts": get_counts(),
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "time": datetime.now().strftime("%H:%M:%S"),
    })


# =========================
# START
# =========================
init_database()
load_active_incident()

if __name__ == "__main__":
    print("AI Infrastructure Monitor")
    print("Database: incidents.db")
    print("Open: http://127.0.0.1:5000")
    app.run(
        debug=False,
        use_reloader=False,
        host="127.0.0.1",
        port=5000,
    )
