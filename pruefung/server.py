#!/usr/bin/env python3
"""Lokale Plattform für Probeprüfungen (nur Python-Standardbibliothek).

Start:    python3 server.py [port] [--no-browser] [--lan]   → http://127.0.0.1:8765
Prüfen:   python3 server.py --check                 → validiert alle Dateien in exams/
"""
import ipaddress
import json
import random
import re
import secrets
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
EXAMS_DIR = BASE / "exams"
ATTEMPTS_DIR = BASE / "attempts"
DEFAULT_PORT = 8765
TYPES = ("single", "multi", "short", "text")
SECRET_FIELDS = ("correct", "accept", "modelAnswer", "keywords", "explanation", "caseSensitive")
VIRTUAL = {"_zufall", "_fehler", "_training"}
SOURCES = ("all", "wrong", "new")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
LOCK = threading.Lock()
PORT = DEFAULT_PORT
LAN_KEY_FILE = BASE / ".lan-key"
LAN = {"server": None, "ip": None}
LAN_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- Prüfungen

def load_exam(path):
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise ApiError(f"JSON-Fehler: {e}")
    eid = path.stem
    questions, seen = [], set()
    for i, raw in enumerate(data.get("questions") or []):
        q = dict(raw)
        qid = str(q.get("id") or f"q{i + 1}")
        where = f"Frage {i + 1} ({qid})"
        if qid in seen:
            raise ApiError(f"{where}: doppelte id")
        seen.add(qid)
        t = q.get("type")
        if t not in TYPES:
            raise ApiError(f"{where}: type muss eins von {TYPES} sein, nicht {t!r}")
        if not q.get("text"):
            raise ApiError(f"{where}: text fehlt")
        if not isinstance(q.get("points", 1), (int, float)) or q.get("points", 1) <= 0:
            raise ApiError(f"{where}: points muss eine positive Zahl sein")
        if t in ("single", "multi"):
            opts = q.get("options") or []
            if len(opts) < 2:
                raise ApiError(f"{where}: mindestens 2 options nötig")
            c = q.get("correct")
            c = [c] if isinstance(c, int) else list(c or [])
            if not c or any(not isinstance(x, int) or not 0 <= x < len(opts) for x in c):
                raise ApiError(f"{where}: correct muss gültige Options-Indizes (ab 0) enthalten")
            if t == "single" and len(c) != 1:
                raise ApiError(f"{where}: single braucht genau eine richtige Antwort")
            q["correct"] = sorted(set(c))
        elif t == "short":
            if not q.get("accept"):
                raise ApiError(f"{where}: accept (Liste akzeptierter Antworten) fehlt")
            for a in q["accept"]:
                if a.startswith("re:"):
                    try:
                        re.compile(a[3:])
                    except re.error as e:
                        raise ApiError(f"{where}: ungültiger regulärer Ausdruck {a!r}: {e}")
        elif t == "text" and not q.get("modelAnswer"):
            raise ApiError(f"{where}: modelAnswer fehlt")
        q["id"] = qid
        q["key"] = f"{eid}::{qid}"
        q.setdefault("points", 1)
        q.setdefault("topic", "Allgemein")
        questions.append(q)
    if not questions:
        raise ApiError("keine Fragen")
    return {
        "id": eid,
        "title": data.get("title") or eid,
        "description": data.get("description", ""),
        "durationMinutes": data.get("durationMinutes", 60),
        "passPercent": data.get("passPercent", 60),
        "trainingOnly": bool(data.get("trainingOnly")),
        "questions": questions,
    }


def all_exams():
    """Dateien, die mit '_' beginnen, werden ignoriert (z.B. _beispiel.json)."""
    exams, errors = {}, []
    for p in sorted(EXAMS_DIR.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            exams[p.stem] = load_exam(p)
        except ApiError as e:
            errors.append({"id": p.stem, "error": str(e)})
    return exams, errors


def question_pool(exams):
    return {q["key"]: q for e in exams.values() for q in e["questions"]}


def public_exam(exam):
    """Prüfung ohne Lösungen für die Bearbeitung."""
    qs = []
    for q in exam["questions"]:
        pq = {k: v for k, v in q.items() if k not in SECRET_FIELDS}
        if q["type"] == "multi":
            pq["select"] = len(q["correct"])
        qs.append(pq)
    return {**exam, "questions": qs}


def build_virtual(kind, exams, n, topic):
    pool = question_pool(exams)
    if kind == "_fehler":
        latest = latest_results()
        keys = [k for k, r in latest.items() if r["status"] != "correct" and k in pool]
        title = "Fehler-Training"
        desc = "Fragen, die du beim letzten Mal nicht (voll) richtig hattest."
    else:
        keys = [k for k, q in pool.items() if not topic or q["topic"] == topic]
        title = "Zufallsprüfung" + (f": {topic}" if topic else "")
        desc = "Zufällig gemischte Fragen aus allen Prüfungen."
    random.shuffle(keys)
    keys = keys[:n]
    if not keys:
        raise ApiError("Keine passenden Fragen vorhanden.", 404)
    return {
        "id": kind,
        "title": title,
        "description": desc,
        "durationMinutes": max(5, round(len(keys) * 1.5)),
        "passPercent": 60,
        "questions": [pool[k] for k in keys],
    }


# ---------------------------------------------------------------- Bewertung

def _norm(s, case_sensitive):
    s = " ".join(str(s).split())
    return s if case_sensitive else s.lower()


def grade(q, a):
    mx = q["points"]
    t = q["type"]
    unanswered = {"points": 0, "max": mx, "status": "unanswered"}
    if t == "single":
        if not isinstance(a, int):
            return unanswered
        ok = [a] == q["correct"]
    elif t == "multi":
        sel = sorted({x for x in (a or []) if isinstance(x, int)})
        if not sel:
            return unanswered
        ok = sel == q["correct"]
    elif t == "short":
        a = str(a or "").strip()
        if not a:
            return unanswered
        cs = q.get("caseSensitive", False)
        ok = False
        for acc in q["accept"]:
            if acc.startswith("re:"):
                ok = re.fullmatch(acc[3:], a, 0 if cs else re.IGNORECASE) is not None
            else:
                ok = _norm(acc, cs) == _norm(a, cs)
            if ok:
                break
    else:  # text → Selbstbewertung
        if not str(a or "").strip():
            return unanswered
        return {"points": None, "max": mx, "status": "pending"}
    return {"points": mx if ok else 0, "max": mx, "status": "correct" if ok else "wrong"}


def status_for(points, mx):
    return "correct" if points >= mx else "partial" if points > 0 else "wrong"


# ---------------------------------------------------------------- Versuche

def summarize(att):
    res = att["results"].values()
    score = sum(r["points"] for r in res if r["points"] is not None)
    mx = sum(r["max"] for r in res)
    pct = round(100 * score / mx, 1) if mx else 0
    att.update(
        score=score,
        maxScore=mx,
        pending=sum(1 for r in res if r["points"] is None),
        percent=pct,
        passed=pct >= att.get("passPercent", 60),
    )
    return att


def attempt_path(aid):
    if not SAFE_ID.match(aid):
        raise ApiError("ungültige Versuchs-ID")
    p = ATTEMPTS_DIR / f"{aid}.json"
    if not p.exists():
        raise ApiError("Versuch nicht gefunden", 404)
    return p


def load_attempt(aid):
    return summarize(json.loads(attempt_path(aid).read_text("utf-8")))


def save_attempt(att):
    ATTEMPTS_DIR.mkdir(exist_ok=True)
    summarize(att)
    path = ATTEMPTS_DIR / f"{att['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(att, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def load_attempts():
    out = []
    for p in ATTEMPTS_DIR.glob("*.json"):
        try:
            out.append(summarize(json.loads(p.read_text("utf-8"))))
        except (json.JSONDecodeError, KeyError):
            pass
    return sorted(out, key=lambda a: a["submittedAt"])


def attempt_summary(a):
    keys = ("id", "examId", "title", "startedAt", "submittedAt", "elapsedSec", "durationMinutes",
            "score", "maxScore", "pending", "percent", "passed", "passPercent")
    s = {k: a.get(k) for k in keys}
    s["questionCount"] = len(a["results"])
    s["hasReview"] = bool(a.get("review", {}).get("overall") or a.get("review", {}).get("questions"))
    return s


def latest_results(attempts=None):
    latest = {}
    for att in attempts if attempts is not None else load_attempts():
        for key, r in att["results"].items():
            if r["points"] is not None:
                latest[key] = r
    return latest


def create_attempt(body):
    exams, _ = all_exams()
    pool = question_pool(exams)
    eid = str(body.get("examId") or "")
    if eid not in exams and eid not in VIRTUAL:
        raise ApiError(f"Unbekannte Prüfung {eid!r}")
    keys = body.get("questionKeys") or [q["key"] for q in exams[eid]["questions"]]
    missing = [k for k in keys if k not in pool]
    if missing:
        raise ApiError(f"Fragen nicht (mehr) vorhanden: {', '.join(missing)}")
    qs = [pool[k] for k in keys]
    answers = body.get("answers") or {}
    meta = exams.get(eid) or body
    now = datetime.now()
    aid = f"{now:%Y%m%d-%H%M%S}_{eid}"
    while (ATTEMPTS_DIR / f"{aid}.json").exists():
        aid += "x"
    started = body.get("startedAt")
    att = {
        "id": aid,
        "examId": eid,
        "title": meta.get("title") or eid,
        "startedAt": datetime.fromtimestamp(started / 1000).isoformat(timespec="seconds")
        if isinstance(started, (int, float)) else None,
        "submittedAt": now.isoformat(timespec="seconds"),
        "elapsedSec": body.get("elapsedSec"),
        "durationMinutes": meta.get("durationMinutes", 60),
        "passPercent": meta.get("passPercent", 60),
        "questions": qs,
        "answers": {q["key"]: answers.get(q["key"]) for q in qs},
        "flagged": body.get("flagged") or [],
        "results": {q["key"]: grade(q, answers.get(q["key"])) for q in qs},
        "review": {"overall": "", "questions": {}},
    }
    save_attempt(att)
    return att


def set_grade(aid, body):
    with LOCK:
        att = load_attempt(aid)
        key = body.get("key")
        if key not in att["results"]:
            raise ApiError("Frage nicht in diesem Versuch")
        q = next(q for q in att["questions"] if q["key"] == key)
        if q["type"] not in ("text", "short"):
            raise ApiError("Nur Freitext- und Kurzantworten können manuell bewertet werden")
        r = att["results"][key]
        pts = body.get("points")
        if pts is None:  # Bewertung zurücknehmen
            r.update(grade(q, att["answers"].get(key)))
            r.pop("gradedBy", None)
        else:
            if not isinstance(pts, (int, float)) or not 0 <= pts <= r["max"]:
                raise ApiError("Punkte außerhalb des gültigen Bereichs")
            r.update(points=pts, status=status_for(pts, r["max"]), gradedBy="self")
        save_attempt(att)
        return att


def training_candidates(pool, topic, source, attempts):
    keys = [k for k, q in pool.items() if not topic or q["topic"] == topic]
    if source == "wrong":
        latest = latest_results(attempts)
        keys = [k for k in keys if k in latest and latest[k]["status"] != "correct"]
    elif source == "new":
        seen = {k for a in attempts for k in a["results"]}
        keys = [k for k in keys if k not in seen]
    return keys


def training_next(body):
    exams, _ = all_exams()
    pool = question_pool(exams)
    source = body.get("source") if body.get("source") in SOURCES else "all"
    exclude = set(body.get("exclude") or [])
    keys = [k for k in training_candidates(pool, body.get("topic") or None, source, load_attempts())
            if k not in exclude]
    if not keys:
        return {"done": True, "remaining": 0}
    q = pool[random.choice(keys)]
    return {"question": public_exam({"questions": [q]})["questions"][0], "remaining": len(keys)}


def training_answer(body):
    exams, _ = all_exams()
    pool = question_pool(exams)
    key = body.get("key")
    if key not in pool:
        raise ApiError("Frage nicht gefunden", 404)
    q = pool[key]
    with LOCK:
        now = datetime.now()
        aid = body.get("sessionId")
        if aid:
            att = load_attempt(aid)
        else:
            aid = f"{now:%Y%m%d-%H%M%S}_training"
            while (ATTEMPTS_DIR / f"{aid}.json").exists():
                aid += "x"
            topic = body.get("topic")
            att = {
                "id": aid, "examId": "_training",
                "title": "Training" + (f": {topic}" if topic else ""),
                "startedAt": now.isoformat(timespec="seconds"),
                "durationMinutes": None, "passPercent": 60,
                "questions": [], "answers": {}, "flagged": [], "results": {},
                "review": {"overall": "", "questions": {}},
            }
        if key not in att["results"]:
            att["questions"].append(q)
        att["answers"][key] = body.get("answer")
        att["results"][key] = grade(q, body.get("answer"))
        att["submittedAt"] = now.isoformat(timespec="seconds")
        att["elapsedSec"] = round((now - datetime.fromisoformat(att["startedAt"])).total_seconds())
        save_attempt(att)
        return att


def stats(exams):
    atts = load_attempts()
    topics = {}
    for e in exams.values():
        for q in e["questions"]:
            topics.setdefault(q["topic"], {"topic": q["topic"], "pool": 0, "answered": 0,
                                           "correct": 0, "earned": 0, "max": 0})["pool"] += 1
    for att in atts:
        qmap = {q["key"]: q for q in att["questions"]}
        for key, r in att["results"].items():
            if r["points"] is None:
                continue
            t = qmap.get(key, {}).get("topic", "Allgemein")
            s = topics.setdefault(t, {"topic": t, "pool": 0, "answered": 0, "correct": 0, "earned": 0, "max": 0})
            s["answered"] += 1
            s["correct"] += r["status"] == "correct"
            s["earned"] += r["points"]
            s["max"] += r["max"]
    for s in topics.values():
        s["percent"] = round(100 * s["earned"] / s["max"], 1) if s["max"] else None
    return {
        "topics": sorted(topics.values(), key=lambda s: (s["percent"] is None, s["percent"] or 0, s["topic"])),
        "attempts": [attempt_summary(a) for a in reversed(atts)],
    }


def exam_list():
    exams, errors = all_exams()
    atts = load_attempts()
    pool = question_pool(exams)
    out = []
    for e in exams.values():
        if e["trainingOnly"]:
            continue
        mine = [a for a in atts if a["examId"] == e["id"]]
        out.append({
            "id": e["id"],
            "title": e["title"],
            "description": e["description"],
            "durationMinutes": e["durationMinutes"],
            "questionCount": len(e["questions"]),
            "maxPoints": sum(q["points"] for q in e["questions"]),
            "attempts": len(mine),
            "best": max((a["percent"] for a in mine), default=None),
            "last": mine[-1]["percent"] if mine else None,
        })
    topics = {}
    for q in pool.values():
        topics[q["topic"]] = topics.get(q["topic"], 0) + 1
    fehler = len(training_candidates(pool, None, "wrong", atts))
    new = len(training_candidates(pool, None, "new", atts))
    return {
        "exams": out,
        "errors": errors,
        "poolSize": len(pool),
        "topics": [{"name": k, "count": v} for k, v in sorted(topics.items())],
        "fehlerCount": fehler,
        "newCount": new,
        "recent": [attempt_summary(a) for a in reversed(atts[-8:])],
    }


# ---------------------------------------------------------------- Zugriff aus dem lokalen Netzwerk
# Lokal läuft der Server immer auf 127.0.0.1. Auf Wunsch wird zusätzlich ein zweiter Server auf
# der LAN-IP gestartet. Geräte im Netzwerk brauchen einmalig den geheimen Schlüssel aus dem
# Link/QR-Code und bekommen dann ein Cookie.

def lan_key():
    try:
        key = LAN_KEY_FILE.read_text().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(9)
    LAN_KEY_FILE.write_text(key)
    return key


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # UDP-connect sendet nichts, ermittelt nur die ausgehende Schnittstelle
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") or ip == "0.0.0.0" else ip
    except OSError:
        return None
    finally:
        s.close()


def lan_url():
    return f"http://{LAN['ip']}:{PORT}/?key={lan_key()}"


def lan_status(local):
    st = {"local": local, "enabled": LAN["server"] is not None}
    if local and LAN["server"]:
        st.update(url=lan_url(), ip=LAN["ip"])
    return st


def set_lan(enabled):
    with LAN_LOCK:
        if enabled and not LAN["server"]:
            ip = lan_ip()
            if not ip:
                raise ApiError("Kein lokales Netzwerk gefunden – ist der Rechner mit dem WLAN verbunden?")
            try:
                srv = ThreadingHTTPServer((ip, PORT), Handler)
            except OSError as e:
                raise ApiError(f"Konnte {ip}:{PORT} nicht öffnen: {e}")
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            LAN.update(server=srv, ip=ip)
            print(f"Im lokalen Netzwerk erreichbar: {lan_url()}")
        elif not enabled and LAN["server"]:
            srv = LAN["server"]
            LAN.update(server=None, ip=None)
            srv.shutdown()
            srv.server_close()
            print("Zugriff aus dem lokalen Netzwerk beendet.")


DENIED_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kein Zugang</title><body style="font:16px/1.5 -apple-system,sans-serif;max-width:480px;margin:40px auto;padding:0 16px">
<h1 style="font-size:20px">Kein Zugang</h1><p>Öffne die Seite über den Link oder QR-Code, der am Rechner unter
„Vom Handy lernen“ angezeigt wird.</p></body>"""


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ApiError("ungültiges JSON")

    def handle_api(self, method):
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if parts == ["api", "lan"]:
            if method == "POST":
                if not self.local:
                    raise ApiError("Der Netzwerkzugriff lässt sich nur am Rechner selbst umschalten.", 403)
                set_lan(bool(self.read_json().get("enabled")))
            return lan_status(self.local)
        if method == "GET" and parts == ["api", "exams"]:
            return exam_list()
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "exams"]:
            eid = parts[2]
            exams, errors = all_exams()
            if eid in VIRTUAL:
                n = int(qs.get("n") or 9999)
                return public_exam(build_virtual(eid, exams, max(1, n), qs.get("topic") or None))
            if eid not in exams:
                err = next((e["error"] for e in errors if e["id"] == eid), None)
                raise ApiError(f"Prüfung fehlerhaft: {err}" if err else "Prüfung nicht gefunden", 404)
            return public_exam(exams[eid])
        if method == "GET" and parts == ["api", "stats"]:
            return stats(all_exams()[0])
        if method == "POST" and parts == ["api", "training", "next"]:
            return training_next(self.read_json())
        if method == "POST" and parts == ["api", "training", "answer"]:
            return training_answer(self.read_json())
        if method == "POST" and parts == ["api", "attempts"]:
            with LOCK:
                return create_attempt(self.read_json())
        if len(parts) >= 3 and parts[:2] == ["api", "attempts"]:
            aid = parts[2]
            if method == "GET" and len(parts) == 3:
                return load_attempt(aid)
            if method == "DELETE" and len(parts) == 3:
                attempt_path(aid).unlink()
                return {"ok": True}
            if method == "POST" and parts[3:] == ["grade"]:
                return set_grade(aid, self.read_json())
        raise ApiError("nicht gefunden", 404)

    def lan_authorized(self):
        """Für Geräte im Netzwerk: Schlüssel aus ?key= oder Cookie prüfen. Antwortet selbst, wenn nicht erlaubt."""
        key = lan_key()
        u = urlparse(self.path)
        given = parse_qs(u.query).get("key", [""])[0]
        if given and secrets.compare_digest(given, key):
            self.send_response(302)
            self.send_header("Location", u.path or "/")
            self.send_header("Set-Cookie", f"lpic_key={key}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        if "lpic_key" in cookie and secrets.compare_digest(cookie["lpic_key"].value, key):
            return True
        data = DENIED_PAGE.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return False

    def dispatch(self, method):
        self.local = ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:")).is_loopback
        if not self.local and (LAN["server"] is None or not self.lan_authorized()):
            if LAN["server"] is None:
                self.send_error(403)
            return
        path = urlparse(self.path).path
        if method == "GET" and path in ("/", "/index.html"):
            data = (BASE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            self.send_json(self.handle_api(method))
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"Serverfehler: {e!r}"}, 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")


def check():
    exams, errors = all_exams()
    for e in exams.values():
        kind = " (nur Training)" if e["trainingOnly"] else ""
        print(f"OK     {e['id']}{kind}: {len(e['questions'])} Fragen, {sum(q['points'] for q in e['questions'])} Punkte")
    for e in errors:
        print(f"FEHLER {e['id']}: {e['error']}")
    return 1 if errors else 0


def main():
    args = sys.argv[1:]
    if "--check" in args:
        sys.exit(check())
    EXAMS_DIR.mkdir(exist_ok=True)
    ATTEMPTS_DIR.mkdir(exist_ok=True)
    global PORT
    PORT = next((int(a) for a in args if a.isdigit()), DEFAULT_PORT)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Prüfungsplattform läuft auf {url}  (Beenden mit Strg+C)")
    if "--lan" in args:
        try:
            set_lan(True)
        except ApiError as e:
            print(f"Netzwerkzugriff nicht möglich: {e}")
    if "--no-browser" not in args:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
