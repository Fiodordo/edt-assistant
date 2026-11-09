#!/usr/bin/env python3
"""Assistant EDT : rappels de cours, alertes de changement, rappels de rendus, résumé hebdo.

Usage :
    python edt.py rappels      # à lancer toutes les 5 minutes
    python edt.py changements  # à lancer toutes les heures
    python edt.py rendus       # à lancer chaque matin
    python edt.py resume       # à lancer le dimanche soir

Variables d'environnement (facultatives) :
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID : sans elles, les messages s'affichent dans le terminal
    EDT_FILTRE : préfixes de codes à garder, séparés par des virgules (ex: "MR1,MS1"). Vide = tout garder.
"""
import csv
import json
import os
import re
import sys
import datetime as dt
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BASE = Path(__file__).resolve().parent
ICS = BASE / "edt.ics"           # ton export, à remplacer quand l'emploi du temps change
STATE = BASE / "etat.json"       # mémoire des cours vus (pour détecter les changements)
SENT = BASE / "envoyes.json"     # rappels déjà envoyés (pour ne pas les répéter)
RENDUS = BASE / "rendus.csv"     # nom,date (AAAA-MM-JJ),heure (HH:MM, facultatif)
FILTRE = [p.strip() for p in os.environ.get("EDT_FILTRE", "").split(",") if p.strip()]


def send(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)


def to_paris(value):
    if value.endswith("Z"):
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).astimezone(PARIS)
    return dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)


def unfold(txt):
    return re.sub(r"\r?\n[ \t]", "", txt).replace("\r", "")


def parse_ics(path):
    txt = unfold(Path(path).read_text(encoding="utf-8", errors="ignore"))
    events = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)\nEND:VEVENT", txt, re.S):
        d = {}
        for line in block.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                d[k.split(";")[0]] = v
        summary = d.get("SUMMARY", "").replace("\\;", ";").replace("\\,", ",")
        start = to_paris(d["DTSTART"])
        end = to_paris(d["DTEND"])
        events.append({
            "key": f"{d.get('UID', '')}|{start.isoformat()}",
            "start": start,
            "end": end,
            "matiere": summary.split(";")[0].strip(),
            "salle": d.get("LOCATION", ""),
            "annule": "CANCELLED" in d.get("STATUS", ""),
        })
    return sorted(events, key=lambda e: e["start"])


def load_events():
    if not ICS.exists():
        sys.exit(f"Fichier introuvable : {ICS}")
    evs = parse_ics(ICS)
    if FILTRE:
        evs = [e for e in evs if any(e["matiere"].startswith(p) for p in FILTRE)]
    return [e for e in evs if not e["annule"]]


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def fmt(e):
    return f"{e['start']:%a %d/%m} {e['start']:%H:%M}-{e['end']:%H:%M}  {e['matiere']}  ({e['salle']})"


def cmd_rappels():
    now = dt.datetime.now(PARIS)
    sent = load_json(SENT, {})
    for e in load_events():
        delta = (e["start"] - now).total_seconds() / 60
        if 0 < delta <= 25 and e["key"] not in sent:
            send(f"Dans {int(delta)} min : {e['matiere']}\nSalle : {e['salle'] or 'non indiquée'}\nDébut {e['start']:%H:%M}")
            sent[e["key"]] = True
    save_json(SENT, sent)


def cmd_changements():
    current = {e["key"]: fmt(e) for e in load_events()}
    if not STATE.exists():
        save_json(STATE, current)
        print("État initial enregistré, aucune alerte envoyée.")
        return
    previous = load_json(STATE, {})
    added = [current[k] for k in current if k not in previous]
    removed = [previous[k] for k in previous if k not in current]
    if added or removed:
        lines = ["Changements dans ton emploi du temps :"]
        lines += [f"+ {x}" for x in added]
        lines += [f"- {x}" for x in removed]
        send("\n".join(lines))
    save_json(STATE, current)


def load_rendus():
    if not RENDUS.exists():
        return []
    rows = []
    with RENDUS.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            heure = r.get("heure") or "23:59"
            when = dt.datetime.strptime(f"{r['date']} {heure}", "%Y-%m-%d %H:%M").replace(tzinfo=PARIS)
            rows.append({"nom": r["nom"], "when": when})
    return sorted(rows, key=lambda r: r["when"])


def cmd_rendus():
    now = dt.datetime.now(PARIS)
    sent = load_json(SENT, {})
    for r in load_rendus():
        jours = (r["when"].date() - now.date()).days
        cle = f"rendu|{r['nom']}|{jours}"
        if jours in (7, 2, 1) and now.hour >= 9 and cle not in sent:
            send(f"Rendu dans {jours} jour(s) : {r['nom']} (échéance {r['when']:%d/%m %H:%M})")
            sent[cle] = True
    save_json(SENT, sent)


def cmd_resume():
    now = dt.datetime.now(PARIS)
    fin = now + dt.timedelta(days=7)
    semaine = [e for e in load_events() if now <= e["start"] <= fin]
    heures = {}
    for e in load_events():
        if e["start"] >= now:
            heures[e["matiere"]] = heures.get(e["matiere"], 0) + (e["end"] - e["start"]).total_seconds() / 3600
    lignes = ["Résumé de ta semaine :"] + ([fmt(e) for e in semaine] or ["Rien cette semaine."])
    lignes += ["", "Heures restantes par matière :"]
    lignes += [f"{h:5.1f} h  {m}" for m, h in sorted(heures.items(), key=lambda x: -x[1])]
    rendus = [r for r in load_rendus() if r["when"] >= now][:5]
    if rendus:
        lignes += ["", "Prochains rendus :"] + [f"{r['when']:%d/%m %H:%M}  {r['nom']}" for r in rendus]
    send("\n".join(lignes))


COMMANDES = {"rappels": cmd_rappels, "changements": cmd_changements, "rendus": cmd_rendus, "resume": cmd_resume}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDES:
        sys.exit("Usage : python edt.py [rappels|changements|rendus|resume]")
    COMMANDES[sys.argv[1]]()
