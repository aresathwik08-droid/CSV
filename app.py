#!/usr/bin/env python3
"""
CSV Column Summary Tool  |  FutureReady Hack  |  Project #99
Run:  python app.py            -> opens http://127.0.0.1:8000
Backend : Python standard library only (http.server, csv, json, statistics)
Frontend: single-page web UI served by the backend
Optional: matplotlib (PNG charts). Without it, the UI draws charts itself.
"""
import csv, io, json, math, os, random, re, statistics, sys, threading, uuid, webbrowser
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_FILE = os.path.join(DATA_DIR, "datasets.json")
SAMPLE = os.path.join(BASE, "sample_data", "students_sample.csv")
MAX_BYTES, MAX_ROWS, PAGE_SIZE = 5 * 1024 * 1024, 50000, 15
MISSING = {"", "na", "n/a", "nan", "null", "none", "-", "--", "?"}
LOCK = threading.Lock()

try:  # graceful fallback if matplotlib is missing
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ------------------------------------------------------------------ storage
def load_db():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, dict):
            raise ValueError("bad schema")
        return db
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError):  # corrupted -> back up, start clean
        try:
            os.replace(DB_FILE, DB_FILE + ".corrupt")
        except OSError:
            pass
        return {}

def save_db(db):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f)
    os.replace(tmp, DB_FILE)  # atomic write

# ------------------------------------------------------------------ parsing
def is_missing(v):
    return v.strip().lower() in MISSING

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
DATE_FMTS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d")

def classify(v):
    v = v.strip()
    if INT_RE.match(v): return "integer"
    if FLOAT_RE.match(v): return "float"
    if v.lower() in ("true", "false", "yes", "no"): return "boolean"
    for fmt in DATE_FMTS:
        try:
            datetime.strptime(v, fmt); return "date"
        except ValueError:
            pass
    return "text"

def parse_csv(text):
    """Returns (columns, rows, error_log). Every bad row is logged, never crashes."""
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("File is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("No header row found.")
    cols, seen = [], Counter()
    for i, h in enumerate(header):
        h = h.strip() or f"column_{i + 1}"
        seen[h] += 1
        cols.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
    rows, errors = [], []
    for n, r in enumerate(reader, start=2):  # line 1 is the header
        if not any(c.strip() for c in r):
            errors.append({"row": n, "issue": "Blank row skipped"}); continue
        if len(rows) >= MAX_ROWS:
            errors.append({"row": n, "issue": f"Row limit {MAX_ROWS} reached; rest ignored"}); break
        if len(r) < len(cols):
            errors.append({"row": n, "issue": f"Expected {len(cols)} fields, got {len(r)}; padded"})
            r = r + [""] * (len(cols) - len(r))
        elif len(r) > len(cols):
            errors.append({"row": n, "issue": f"Expected {len(cols)} fields, got {len(r)}; extras trimmed"})
            r = r[:len(cols)]
        rows.append(r)
    if not rows:
        raise ValueError("No data rows found.")
    return cols, rows, errors

# ------------------------------------------------------------------ analysis
def infer(kinds, n):
    if n == 0: return "empty", False, ""
    if set(kinds) <= {"integer", "float"}:
        return ("float" if "float" in kinds else "integer"), True, ""
    if len(kinds) == 1: return next(iter(kinds)), False, ""
    num = kinds["integer"] + kinds["float"]
    if num / n >= 0.9:
        return "mixed", True, f"{n - num} non-numeric value(s) in numeric column"
    return "text", False, ""

def num_stats(nums):
    nums.sort()
    n = len(nums)
    q1, _, q3 = statistics.quantiles(nums, n=4) if n >= 2 else (nums[0],) * 3
    iqr = q3 - q1
    outliers = sum(1 for x in nums if x < q1 - 1.5 * iqr or x > q3 + 1.5 * iqr) if iqr else 0
    lo, hi = nums[0], nums[-1]
    bins = 10
    hist = [0] * bins
    for x in nums:
        hist[min(bins - 1, int((x - lo) / (hi - lo) * bins)) if hi > lo else 0] += 1
    r = lambda x: round(x, 4)
    return {"count": n, "min": r(lo), "max": r(hi), "mean": r(statistics.fmean(nums)),
            "median": r(statistics.median(nums)), "std": r(statistics.stdev(nums)) if n > 1 else 0,
            "sum": r(math.fsum(nums)), "q1": r(q1), "q3": r(q3), "outliers": outliers,
            "hist": hist, "hist_range": [r(lo), r(hi)]}

def analyse(ds):
    cols, rows = ds["columns"], ds["rows"]
    out, warns, total_missing = [], [], 0
    for i, name in enumerate(cols):
        vals = [r[i] for r in rows]
        present = [v.strip() for v in vals if not is_missing(v)]
        miss = len(vals) - len(present)
        total_missing += miss
        kinds = Counter(classify(v) for v in present)
        ctype, numeric, note = infer(kinds, len(present))
        c = {"name": name, "type": ctype, "missing": miss, "unique": len(set(present)),
             "missing_pct": round(100 * miss / len(vals), 1), "warnings": []}
        if numeric:
            nums = [float(v) for v in present if classify(v) in ("integer", "float")]
            c["stats"] = num_stats(nums)
            if c["stats"]["outliers"]:
                c["warnings"].append(f"{c['stats']['outliers']} outlier(s) (IQR rule)")
        else:
            c["top"] = Counter(present).most_common(8)
        if note: c["warnings"].append(note)
        if c["missing_pct"] > 20: c["warnings"].append(f"High missing rate ({c['missing_pct']}%)")
        if len(present) > 1 and c["unique"] == 1: c["warnings"].append("Constant column")
        warns += [f"{name}: {w}" for w in c["warnings"]]
        out.append(c)
    cells = len(rows) * len(cols)
    dup = len(rows) - len({tuple(r) for r in rows})
    if dup: warns.append(f"{dup} duplicate row(s) found")
    return {"id": ds["id"], "name": ds["name"], "uploaded": ds["uploaded"], "rows": len(rows),
            "cols": len(cols), "missing_total": total_missing,
            "completeness": round(100 * (1 - total_missing / cells), 1) if cells else 100,
            "duplicates": dup, "numeric_cols": sum(1 for c in out if "stats" in c),
            "errors": ds["errors"], "warnings": warns, "columns": out}

def search(ds, q, col, missing_only, page):
    cols, q = ds["columns"], q.lower()
    ci = cols.index(col) if col in cols else None
    hits = []
    for n, r in enumerate(ds["rows"], start=1):
        if missing_only and not any(is_missing(v) for v in r): continue
        hay = [r[ci]] if ci is not None else r
        if q and not any(q in v.lower() for v in hay): continue
        hits.append([n] + r)
    pages = max(1, math.ceil(len(hits) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    return {"columns": cols, "total": len(hits), "page": page, "pages": pages,
            "rows": hits[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]}

# ------------------------------------------------------------------ exports
def export(ds, fmt):
    a = analyse(ds)
    if fmt == "json":
        return json.dumps({k: v for k, v in a.items()}, indent=2), "application/json", "json"
    if fmt == "csv":
        b = io.StringIO(); w = csv.writer(b)
        w.writerow(["column", "type", "missing", "missing_pct", "unique", "count", "min", "max", "mean", "median", "std"])
        for c in a["columns"]:
            s = c.get("stats", {})
            w.writerow([c["name"], c["type"], c["missing"], c["missing_pct"], c["unique"]] +
                       [s.get(k, "") for k in ("count", "min", "max", "mean", "median", "std")])
        return b.getvalue(), "text/csv", "csv"
    L = [f"CSV COLUMN SUMMARY - {a['name']}", f"Generated: {datetime.now():%Y-%m-%d %H:%M}",
         f"Rows: {a['rows']} | Columns: {a['cols']} | Missing cells: {a['missing_total']} | "
         f"Completeness: {a['completeness']}%", ""]
    if fmt == "md":
        L = [f"# CSV Column Summary: {a['name']}", f"*Generated {datetime.now():%Y-%m-%d %H:%M}*", "",
             f"- **Rows:** {a['rows']}  \n- **Columns:** {a['cols']}  \n- **Missing cells:** {a['missing_total']}  \n"
             f"- **Completeness:** {a['completeness']}%", "",
             "| Column | Type | Missing | Unique | Min | Max | Mean | Median |", "|---|---|---|---|---|---|---|---|"]
        for c in a["columns"]:
            s = c.get("stats", {})
            L.append(f"| {c['name']} | {c['type']} | {c['missing']} ({c['missing_pct']}%) | {c['unique']} | "
                     + " | ".join(str(s.get(k, "-")) for k in ("min", "max", "mean", "median")) + " |")
        L += ["", "## Warnings"] + ([f"- {w}" for w in a["warnings"]] or ["- None"])
        return "\n".join(L), "text/markdown", "md"
    for c in a["columns"]:
        s = c.get("stats")
        L.append(f"{c['name']:<20} {c['type']:<8} missing={c['missing']:<4} unique={c['unique']:<4}" +
                 (f" min={s['min']} max={s['max']} mean={s['mean']} median={s['median']}" if s else ""))
    L += ["", "WARNINGS"] + (a["warnings"] or ["None"]) + ["", f"IMPORT LOG ({len(a['errors'])} issue(s))"]
    L += [f"line {e['row']}: {e['issue']}" for e in a["errors"]]
    return "\n".join(L), "text/plain", "txt"

def chart_png(ds, col):
    a = analyse(ds)
    c = next((x for x in a["columns"] if x["name"] == col), None)
    if not c: raise KeyError("Unknown column")
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=110)
    if "stats" in c:
        h, (lo, hi) = c["stats"]["hist"], c["stats"]["hist_range"]
        w = (hi - lo) / len(h) if hi > lo else 1
        ax.bar([lo + w * (i + .5) for i in range(len(h))], h, width=w * .9, color="#6366f1")
        ax.axvline(c["stats"]["mean"], color="#ef4444", ls="--", label="mean")
        ax.legend(); ax.set_ylabel("Frequency")
    else:
        top = c.get("top", [])
        ax.barh([str(k) for k, _ in top][::-1], [v for _, v in top][::-1], color="#14b8a6")
        ax.set_xlabel("Count")
    ax.set_title(f"{col} ({c['type']})"); fig.tight_layout()
    b = io.BytesIO(); fig.savefig(b, format="png"); plt.close(fig)
    return b.getvalue()

# ------------------------------------------------------------------ seed data
def seed_sample():
    os.makedirs(os.path.dirname(SAMPLE), exist_ok=True)
    random.seed(42)
    depts = ["CSE", "ECE", "Mech", "Civil", "IT"]
    rows = []
    for i in range(1, 41):
        rows.append([1000 + i, f"Student {i}", random.choice(depts), random.randint(18, 24),
                     round(random.uniform(5.5, 9.8), 2), random.randint(60, 100),
                     f"2024-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
                     random.choice(["yes", "no"]), f"student{i}@college.edu"])
    rows[3][3], rows[7][4], rows[12][5], rows[18][2], rows[25][8] = "", "N/A", "", "", ""   # missing
    rows[9][3] = 99; rows[14][4] = 10.0; rows[20][4] = 0                                  # boundary/outliers
    rows[30][5] = "abc"                                                                   # type error
    rows.append(list(rows[0]))                                                            # duplicate
    rows.append([1042, "Short Row"])                                                      # ragged row
    with open(SAMPLE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "name", "department", "age", "cgpa", "attendance_pct", "enrolled_on", "scholarship", "email"])
        w.writerows(rows)

def add_dataset(db, name, text):
    cols, rows, errors = parse_csv(text)
    did = uuid.uuid4().hex[:8]
    db[did] = {"id": did, "name": name, "uploaded": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "columns": cols, "rows": rows, "errors": errors}
    return did

# ------------------------------------------------------------------ HTTP server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)): body = json.dumps(body)
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def route(self, method):
        u = urlparse(self.path); qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        if method == "GET" and not parts:
            return self.send(200, PAGE, "text/html")
        if parts[:2] != ["api", "datasets"]:
            return self.send(404, {"error": "Not found"})
        with LOCK:
            db = load_db()
            if len(parts) == 2:
                if method == "GET":
                    return self.send(200, [{"id": d["id"], "name": d["name"], "rows": len(d["rows"]),
                                            "cols": len(d["columns"]), "uploaded": d["uploaded"]} for d in db.values()])
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n <= 0 or n > MAX_BYTES * 2:
                        return self.send(413, {"error": f"File must be 1 byte - {MAX_BYTES // 1048576} MB"})
                    body = json.loads(self.rfile.read(n))
                    name, text = str(body.get("name", "")).strip(), body.get("content", "")
                    if not re.search(r"\.(csv|tsv|txt)$", name, re.I):
                        return self.send(400, {"error": "Please upload a .csv, .tsv or .txt file"})
                    did = add_dataset(db, name, text)
                    save_db(db)
                    return self.send(201, {"id": did, "errors": len(db[did]["errors"])})
            ds = db.get(parts[2]) if len(parts) > 2 else None
            if ds is None:
                return self.send(404, {"error": "Dataset not found"})
            if len(parts) == 3 and method == "GET":
                return self.send(200, analyse(ds))
            if len(parts) == 3 and method == "DELETE":
                del db[ds["id"]]; save_db(db)
                return self.send(200, {"ok": True})
            if len(parts) == 4 and method == "GET":
                if parts[3] == "search":
                    return self.send(200, search(ds, qs.get("q", ""), qs.get("col", ""),
                                                 qs.get("missing") == "1", int(qs.get("page", 1) or 1)))
                if parts[3] == "export":
                    body, ctype, ext = export(ds, qs.get("fmt", "md"))
                    fn = re.sub(r"\W+", "_", os.path.splitext(ds["name"])[0]) + "_summary." + ext
                    return self.send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{fn}"'})
                if parts[3] == "chart":
                    if not HAS_MPL:
                        return self.send(501, {"error": "matplotlib not installed"})
                    return self.send(200, chart_png(ds, qs.get("col", "")), "image/png")
        self.send(404, {"error": "Not found"})

    def handle_any(self, method):
        try: self.route(method)
        except (ValueError, KeyError, json.JSONDecodeError) as e: self.send(400, {"error": str(e)})
        except Exception as e: self.send(500, {"error": f"Server error: {e}"})

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_DELETE(self): self.handle_any("DELETE")

# ------------------------------------------------------------------ frontend
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CSV Column Summary Tool</title><style>
:root{--bg:#f5f6fb;--card:#fff;--tx:#1e2235;--mu:#6b7090;--ac:#6366f1;--bd:#e3e5f0;--ok:#16a34a;--wa:#d97706;--er:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#12141f;--card:#1c1f2e;--tx:#e8e9f3;--mu:#9a9eb8;--bd:#2c3045}}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--tx)}
header{background:linear-gradient(120deg,#6366f1,#14b8a6);color:#fff;padding:18px 24px}header h1{margin:0;font-size:20px}header p{margin:4px 0 0;opacity:.9}
.wrap{display:grid;grid-template-columns:260px 1fr;gap:16px;padding:16px;max-width:1300px;margin:auto}@media(max-width:800px){.wrap{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;margin-bottom:14px}
button,select,input[type=text]{font:inherit;padding:7px 11px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--tx)}
button{cursor:pointer;background:var(--ac);color:#fff;border:0}button.alt{background:var(--card);color:var(--tx);border:1px solid var(--bd)}
#drop{border:2px dashed var(--ac);border-radius:10px;padding:16px;text-align:center;color:var(--mu);cursor:pointer}#drop.on{background:rgba(99,102,241,.12)}
.ds{display:flex;justify-content:space-between;align-items:center;padding:8px;border-radius:8px;cursor:pointer}.ds:hover,.ds.sel{background:rgba(99,102,241,.14)}.ds small{color:var(--mu);display:block}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}.tabs button{background:var(--card);color:var(--tx);border:1px solid var(--bd)}.tabs button.on{background:var(--ac);color:#fff}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}.kpi{background:var(--bg);border-radius:10px;padding:12px}.kpi b{font-size:22px;display:block}.kpi span{color:var(--mu);font-size:12px}
table{border-collapse:collapse;width:100%}th,td{padding:7px 9px;border-bottom:1px solid var(--bd);text-align:left;white-space:nowrap}th{color:var(--mu);font-size:12px;text-transform:uppercase}.sc{overflow-x:auto}
.b{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;background:rgba(99,102,241,.15)}.w{background:rgba(217,119,6,.18);color:var(--wa);margin:2px}
.bar{height:14px;background:var(--ac);border-radius:3px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
#toast{position:fixed;bottom:18px;right:18px;padding:10px 16px;border-radius:8px;color:#fff;display:none}.ok{color:var(--ok)}.miss{color:var(--er);font-style:italic}
</style></head><body>
<header><h1>📊 CSV Column Summary Tool</h1><p>Upload a CSV → column names, data types, missing counts &amp; numeric summaries. Data is saved locally.</p></header>
<div class="wrap"><aside>
<div class="card"><div id="drop">⬆ Drop CSV here or click to upload<input id="file" type="file" accept=".csv,.tsv,.txt" hidden></div></div>
<div class="card"><b>Saved datasets</b><div id="list" style="margin-top:8px"></div></div></aside>
<main id="main"><div class="card">Select or upload a dataset to begin.</div></main></div><div id="toast"></div>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let cur=null,sum=null,tab='overview',pg=1;
function toast(m,bad){const t=$('#toast');t.textContent=m;t.style.background=bad?'#dc2626':'#16a34a';t.style.display='block';setTimeout(()=>t.style.display='none',3500)}
async function api(u,o){const r=await fetch(u,o),j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function loadList(){const l=await api('/api/datasets');$('#list').innerHTML=l.map(d=>`<div class="ds ${d.id==cur?'sel':''}" onclick="pick('${d.id}')"><div>${esc(d.name)}<small>${d.rows} rows · ${d.cols} cols</small></div><button class="alt" title="Delete" onclick="event.stopPropagation();del('${d.id}')">✕</button></div>`).join('')||'<small>No datasets yet</small>';return l}
async function pick(id){cur=id;try{sum=await api('/api/datasets/'+id);tab='overview';pg=1;render();loadList()}catch(e){toast(e.message,1)}}
async function del(id){if(!confirm('Delete this dataset?'))return;await api('/api/datasets/'+id,{method:'DELETE'});if(cur==id){cur=null;$('#main').innerHTML='<div class="card">Select or upload a dataset.</div>'}loadList()}
function upload(f){if(!f)return;if(f.size>5*1048576)return toast('File too large (max 5 MB)',1);
 const r=new FileReader();r.onload=async()=>{try{const j=await api('/api/datasets',{method:'POST',body:JSON.stringify({name:f.name,content:r.result})});toast('Imported'+(j.errors?` with ${j.errors} row issue(s) logged`:' successfully'));await pick(j.id)}catch(e){toast(e.message,1)}};r.readAsText(f)}
$('#drop').onclick=()=>$('#file').click();$('#file').onchange=e=>{upload(e.target.files[0]);e.target.value=''};
$('#drop').ondragover=e=>{e.preventDefault();$('#drop').classList.add('on')};$('#drop').ondragleave=()=>$('#drop').classList.remove('on');
$('#drop').ondrop=e=>{e.preventDefault();$('#drop').classList.remove('on');upload(e.dataTransfer.files[0])};
function render(){const T=['overview','columns','records','charts','log'];
 $('#main').innerHTML=`<div class="tabs">${T.map(t=>`<button class="${t==tab?'on':''}" onclick="tab='${t}';render()">${{overview:'Overview',columns:'Column Summary',records:'Search Records',charts:'Charts',log:'Import Log ('+sum.errors.length+')'}[t]}</button>`).join('')}
 <span style="flex:1"></span>${['md','txt','csv','json'].map(f=>`<a href="/api/datasets/${cur}/export?fmt=${f}"><button class="alt">⬇ ${f.toUpperCase()}</button></a>`).join('')}</div><div id="tabbody"></div>`;
 ({overview,columns,records,charts,log})[tab]()}
function overview(){const s=sum,k=(v,l)=>`<div class="kpi"><b>${v}</b><span>${l}</span></div>`;
 $('#tabbody').innerHTML=`<div class="card"><h3 style="margin-top:0">Executive summary — ${esc(s.name)}</h3><div class="kpis">${k(s.rows,'Rows')}${k(s.cols,'Columns')}${k(s.numeric_cols,'Numeric columns')}${k(s.missing_total,'Missing cells')}${k(s.completeness+'%','Completeness')}${k(s.duplicates,'Duplicate rows')}</div></div>
 <div class="card"><b>⚠ Threshold warnings</b><div style="margin-top:8px">${s.warnings.map(w=>`<span class="b w">${esc(w)}</span>`).join('')||'<span class="ok">✔ No issues detected</span>'}</div></div>`}
function columns(){const f=v=>v===undefined?'<span class="miss">–</span>':v;
 $('#tabbody').innerHTML=`<div class="card sc"><table><tr><th>Column</th><th>Type</th><th>Missing</th><th>Unique</th><th>Min</th><th>Max</th><th>Mean</th><th>Median</th><th>Std</th><th>Flags</th></tr>${sum.columns.map(c=>{const s=c.stats||{};return`<tr><td><b>${esc(c.name)}</b></td><td><span class="b">${c.type}</span></td><td>${c.missing} (${c.missing_pct}%)</td><td>${c.unique}</td><td>${f(s.min)}</td><td>${f(s.max)}</td><td>${f(s.mean)}</td><td>${f(s.median)}</td><td>${f(s.std)}</td><td>${c.warnings.map(w=>`<span class="b w">${esc(w)}</span>`).join('')}</td></tr>`}).join('')}</table></div>`}
async function records(){$('#tabbody').innerHTML=`<div class="card"><div class="row"><input id="q" type="text" placeholder="Keyword…" onkeydown="if(event.key=='Enter'){pg=1;runSearch()}"><select id="col"><option value="">All columns</option>${sum.columns.map(c=>`<option>${esc(c.name)}</option>`).join('')}</select><label><input type="checkbox" id="ms"> only rows with missing values</label><button onclick="pg=1;runSearch()">Search</button></div><div id="res"></div></div>`;runSearch()}
async function runSearch(){try{const r=await api(`/api/datasets/${cur}/search?q=${encodeURIComponent($('#q').value)}&col=${encodeURIComponent($('#col').value)}&missing=${$('#ms').checked?1:0}&page=${pg}`);pg=r.page;
 $('#res').innerHTML=`<p>${r.total} matching row(s)</p><div class="sc"><table><tr><th>#</th>${r.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>${r.rows.map(x=>`<tr>${x.map(v=>`<td>${v===''?'<span class="miss">empty</span>':esc(v)}</td>`).join('')}</tr>`).join('')}</table></div>
 <div class="row" style="margin-top:10px"><button class="alt" ${pg<=1?'disabled':''} onclick="pg--;runSearch()">◀ Prev</button>Page ${r.page} / ${r.pages}<button class="alt" ${pg>=r.pages?'disabled':''} onclick="pg++;runSearch()">Next ▶</button></div>`}catch(e){toast(e.message,1)}}
function charts(){$('#tabbody').innerHTML=`<div class="card"><div class="row"><select id="cc" onchange="drawChart()">${sum.columns.map(c=>`<option>${esc(c.name)}</option>`).join('')}</select></div><div id="chart"></div></div>`;drawChart()}
function drawChart(){const n=$('#cc').value,c=sum.columns.find(x=>x.name==n),box=$('#chart');
 box.innerHTML=`<img style="max-width:100%" src="/api/datasets/${cur}/chart?col=${encodeURIComponent(n)}">`;
 box.firstChild.onerror=()=>{ // fallback when matplotlib is unavailable
  const d=c.stats?c.stats.hist.map((v,i)=>[(c.stats.hist_range[0]+(c.stats.hist_range[1]-c.stats.hist_range[0])*i/10).toFixed(1),v]):(c.top||[]);const m=Math.max(1,...d.map(x=>x[1]));
  box.innerHTML=`<p><small>Built-in chart (install matplotlib for PNG charts)</small></p>`+d.map(([k,v])=>`<div class="row" style="margin:3px 0;flex-wrap:nowrap"><span style="width:110px;overflow:hidden">${esc(k)}</span><div class="bar" style="width:${v/m*60}%"></div> ${v}</div>`).join('')}}
function log(){$('#tabbody').innerHTML=`<div class="card">${sum.errors.length?`<table><tr><th>Line</th><th>Issue</th></tr>${sum.errors.map(e=>`<tr><td>${e.row}</td><td>${esc(e.issue)}</td></tr>`).join('')}</table>`:'<span class="ok">✔ Every row imported cleanly.</span>'}</div>`}
loadList().then(l=>l.length&&pick(l[0].id));
</script></body></html>"""

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
    with LOCK:
        db = load_db()
        if not db:  # first run: seed realistic demo data
            if not os.path.exists(SAMPLE): seed_sample()
            with open(SAMPLE, encoding="utf-8") as f:
                add_dataset(db, "students_sample.csv", f.read())
            save_db(db)
    url = f"http://127.0.0.1:{port}"
    print(f"CSV Column Summary Tool running at {url}  (Ctrl+C to stop)")
    print("Charts: " + ("matplotlib PNG" if HAS_MPL else "built-in fallback (pip install matplotlib for PNG charts)"))
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
