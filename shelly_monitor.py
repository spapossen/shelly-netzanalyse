#!/usr/bin/env python3
"""Shelly Pro 3EM Monitor: Lastreserve und Netzqualität am Messpunkt.

Läuft als Dienst (z. B. in einem Proxmox-LXC) und
  * holt beim Start und danach regelmäßig die 1-Minuten-Historie aus dem
    Speicher des Shelly (ca. 60 Tage, min/max/Ø von U und I je Phase, Energien),
  * fragt ab Start jede Sekunde alle Momentanwerte ab (U, I, P, S, cos phi, f),
  * speichert alles in SQLite,
  * überwacht Spannung, Sternpunkt und Strom und meldet Grenzverletzungen über ntfy/E-Mail und
  * liefert über einen Webserver jederzeit einen Bericht für einen frei
    wählbaren Zeitraum (auch als HTML-Datei und CSV zum Herunterladen).

Aufruf: shelly_monitor.py [--config /etc/shelly-monitor/config.ini]
"""
import argparse
import base64
import configparser
import csv
import html
import io
import json
import logging
import math
import re
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth

LOG = logging.getLogger("shelly-monitor")

PHASEN = (("a", "L1"), ("b", "L2"), ("c", "L3"))
NAME_OK = re.compile(r"[a-z][a-z0-9_]{0,48}")

# EN 50160: 10-min-Mittel der Spannung in 230 V ±10 %, Frequenz 50 Hz ±1 %,
# Spannungsunsymmetrie ≤ 2 %. 49,8–50,2 Hz ist der normale Regelbereich im Verbundnetz.
U_MIN, U_MAX = 207.0, 253.0
F_MIN, F_MAX = 49.5, 50.5
F_NORMAL = (49.8, 50.2)
UNSYM_MAX = 2.0
# VDE-AR-N 4100: Schieflast einphasiger Verbraucher ≤ 20 A, Spannungsänderung
# beim Schalten einer Last ≤ 3 % (Richtwert)
SCHIEFLAST_A = 20.0
DELTA_U_SCHALT = 0.03
# Referenzimpedanz Außenleiter–Neutralleiter nach IEC 60725 (0,4 + j0,25 Ω)
Z_REFERENZ = 0.47

STATUS = {"start": time.time(), "letzte_messung": None, "letzter_fehler": None,
          "fehler_seit_start": 0, "letzter_abgleich": None, "abgleich_zeilen": 0, "live": None}
STATUS_LOCK = threading.Lock()


def status_setzen(**werte):
    with STATUS_LOCK:
        STATUS.update(werte)


# --------------------------------------------------------------------------- Konfiguration

def abschnitte_lesen(cp):
    """Leitungsabschnitte vom Messpunkt aufwärts: abschnitt1 = Länge m, Querschnitt mm², Material[, N mm²][, Name].
    Die einfache Form laenge_m/querschnitt_mm2/material wird als ein Abschnitt gelesen."""
    if not cp.has_section("zuleitung"):
        return []
    abschnitte = []
    for schluessel, wert in sorted(cp.items("zuleitung")):
        if not schluessel.startswith("abschnitt") or not wert.strip():
            continue
        teile = [t.strip() for t in wert.split(",")]
        try:
            laenge, mm2 = float(teile[0]), float(teile[1])
        except (IndexError, ValueError):
            LOG.warning("Zuleitung %s nicht lesbar: %s", schluessel, wert)
            continue
        material = (teile[2] if len(teile) > 2 and teile[2] else "cu").lower()
        mm2_n = mm2
        name = ""
        for t in teile[3:]:
            try:
                mm2_n = float(t)
            except ValueError:
                name = t
        abschnitte.append({"laenge_m": laenge, "mm2": mm2, "mm2_n": mm2_n, "material": material, "name": name})
    if not abschnitte:
        laenge = float(cp.get("zuleitung", "laenge_m", fallback="0") or 0)
        mm2 = float(cp.get("zuleitung", "querschnitt_mm2", fallback="0") or 0)
        if laenge and mm2:
            mm2_n = float(cp.get("zuleitung", "querschnitt_n_mm2", fallback="0") or 0) or mm2
            abschnitte.append({"laenge_m": laenge, "mm2": mm2, "mm2_n": mm2_n, "name": "",
                               "material": cp.get("zuleitung", "material", fallback="cu").strip().lower()})
    return abschnitte


class Konfig:
    def __init__(self, pfad):
        cp = configparser.ConfigParser()
        if pfad:
            cp.read(pfad, encoding="utf-8")
        g = lambda abschnitt, schluessel, standard: cp.get(abschnitt, schluessel, fallback=standard).strip()
        self.host = g("shelly", "host", "")
        self.shelly_passwort = g("shelly", "passwort", "")
        self.intervall = max(1, int(float(g("erfassung", "intervall", "1"))))
        self.abgleich_minuten = max(1, int(g("erfassung", "abgleich_minuten", "15")))
        self.aufbewahrung_tage = int(g("erfassung", "aufbewahrung_sekundenwerte_tage", "180"))
        self.datenbank = g("speicher", "datenbank", "/var/lib/shelly-monitor/daten.db")
        self.adresse = g("web", "adresse", "0.0.0.0")
        self.port = int(g("web", "port", "8080"))
        self.web_benutzer = g("web", "benutzer", "")
        self.web_passwort = g("web", "passwort", "")
        self.sicherung = float(g("anlage", "sicherung", "63"))
        self.nennspannung = float(g("anlage", "nennspannung", "230"))
        self.min_stufe = float(g("anlage", "min_laststufe_a", "8"))
        self.name = g("anlage", "name", "Messpunkt")
        self.messpunkt = g("anlage", "messpunkt", "")
        self.netzform = g("anlage", "netzform", "").upper()
        self.zuleitung = abschnitte_lesen(cp)
        self.zeitzone = ZoneInfo(g("anlage", "zeitzone", "Europe/Berlin"))
        # Überwachung: Grenzen je Sekundenwert, Benachrichtigung über ntfy und/oder E-Mail
        a = lambda schluessel, standard: float(g("alarm", schluessel, standard) or standard)
        self.alarm_aktiv = g("alarm", "aktiv", "ja").lower() in ("ja", "1", "true", "an", "yes")
        self.alarm_u_min, self.alarm_u_max = a("u_min", "207"), a("u_max", "253")
        self.alarm_u_krit_min, self.alarm_u_krit_max = a("u_krit_min", "195"), a("u_krit_max", "265")
        self.alarm_u_ausfall = a("u_ausfall", "100")
        self.alarm_stern, self.alarm_stern_krit = a("sternpunkt_v", "30"), a("sternpunkt_krit_v", "50")
        self.alarm_strom = a("strom_a", "0") or 0.9 * self.sicherung
        self.alarm_dauer, self.alarm_dauer_krit = a("dauer_s", "5"), a("dauer_krit_s", "1")
        self.alarm_keine_daten = a("keine_daten_s", "30")
        self.alarm_entwarnung = a("entwarnung_s", "120")
        self.alarm_wiederholung = a("wiederholung_min", "30")
        self.ntfy_url = g("alarm", "ntfy_url", "")
        self.ntfy_token = g("alarm", "ntfy_token", "")
        self.smtp_host = g("alarm", "smtp_host", "")
        self.smtp_port = int(a("smtp_port", "587"))
        self.smtp_benutzer = g("alarm", "smtp_benutzer", "")
        self.smtp_passwort = g("alarm", "smtp_passwort", "")
        self.mail_von = g("alarm", "mail_von", "") or self.smtp_benutzer
        self.mail_an = [m.strip() for m in g("alarm", "mail_an", "").split(",") if m.strip()]
        self.heartbeat_url = g("alarm", "heartbeat_url", "")


# --------------------------------------------------------------------------- Datenbank

def verbinden(pfad):
    con = sqlite3.connect(pfad, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("CREATE TABLE IF NOT EXISTS minute (ts INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS sekunde (ts INTEGER PRIMARY KEY)")
    # Laststufen zwischen zwei Sekundenwerten, Grundlage der Impedanzschätzung
    con.execute("CREATE TABLE IF NOT EXISTS stufe (ts INTEGER, phase TEXT, du REAL, di REAL, ruhe REAL, "
                "PRIMARY KEY (ts, phase))")
    # Alarme der Überwachung: Beginn, Ende (NULL = noch aktiv), höchste Stufe und Extremwert
    con.execute("CREATE TABLE IF NOT EXISTS alarm (id INTEGER PRIMARY KEY, start INTEGER, ende INTEGER, "
                "schluessel TEXT, stufe TEXT, text TEXT, extrem REAL)")
    return con


def spalten(con, tabelle):
    return [r[1] for r in con.execute(f"PRAGMA table_info({tabelle})")]


def einfuegen(con, tabelle, zeilen):
    """Speichert Zeilen (dicts mit ts); neue Messgrößen werden als Spalte ergänzt."""
    if not zeilen:
        return
    namen = sorted({k for z in zeilen for k in z if k != "ts" and NAME_OK.fullmatch(k)})
    vorhanden = set(spalten(con, tabelle))
    for n in namen:
        if n not in vorhanden:
            con.execute(f'ALTER TABLE {tabelle} ADD COLUMN "{n}" REAL')
    liste = ", ".join(f'"{c}"' for c in ["ts"] + namen)
    platz = ", ".join("?" * (len(namen) + 1))
    con.executemany(f"INSERT OR REPLACE INTO {tabelle} ({liste}) VALUES ({platz})",
                    [[z["ts"]] + [z.get(n) for n in namen] for z in zeilen])


# --------------------------------------------------------------------------- Shelly

class Shelly:
    def __init__(self, k):
        self.basis = f"http://{k.host}"
        self.s = requests.Session()
        if k.shelly_passwort:
            self.s.auth = HTTPDigestAuth("admin", k.shelly_passwort)

    def _get(self, pfad, timeout, **params):
        r = self.s.get(self.basis + pfad, params=params, timeout=timeout)
        r.raise_for_status()
        return r

    def status(self):
        return self._get("/rpc/EM.GetStatus", 3, id=0).json()

    def geraet(self):
        d = self._get("/rpc/Shelly.GetDeviceInfo", 5).json()
        return {n: d.get(n) for n in ("model", "name", "mac", "fw_id", "ver", "app")}

    def erster_datensatz(self):
        bloecke = self._get("/rpc/EMData.GetRecords", 10, id=0).json().get("data_blocks") or []
        return min((b["ts"] for b in bloecke), default=None)

    def minutenwerte(self, von, bis):
        text = self._get("/emdata/0/data.csv", 120, ts=von, end_ts=bis, add_keys="true").text
        zeilen = []
        for z in csv.DictReader(io.StringIO(text)):
            try:
                ts = int(float(z.pop("timestamp")))
            except (KeyError, TypeError, ValueError):
                continue
            zeile = {"ts": ts}
            for k, v in z.items():
                try:
                    zeile[k] = float(v)
                except (TypeError, ValueError):
                    zeile[k] = None
            zeilen.append(zeile)
        return zeilen


def erfassung(k, stop):
    """Fragt im festen Raster (Standard 1 s) die Momentanwerte ab."""
    con = verbinden(k.datenbank)
    shelly = Shelly(k)
    puffer, stufen, vorher, letzter_log = [], [], None, 0.0
    vorzeichen, vorzeichen_zeit = {}, 0.0
    try:
        status_setzen(geraet=shelly.geraet())
    except Exception as e:
        LOG.warning("Geräteinfo nicht lesbar: %s", e)
    naechster = math.ceil(time.time() / k.intervall) * k.intervall
    while not stop.is_set():
        warte = naechster - time.time()
        if warte > 0:
            stop.wait(warte)
        ts = int(round(naechster))
        naechster += k.intervall
        if time.time() - naechster > k.intervall:
            naechster = math.ceil(time.time() / k.intervall) * k.intervall
        try:
            d = shelly.status()
            zeile = {"ts": ts}
            zeile.update({n: float(v) for n, v in d.items()
                          if n != "id" and isinstance(v, (int, float)) and not isinstance(v, bool)})
            if time.time() - vorzeichen_zeit > 300:
                vorzeichen, vorzeichen_zeit = blind_vorzeichen(con), time.time()
            abgeleitet_sekunde(zeile, vorzeichen)
            puffer.append(zeile)
            if vorher and ts - vorher["ts"] <= 2.5 * k.intervall:
                stufen += laststufen(vorher, zeile)
            vorher = zeile
            status_setzen(letzte_messung=ts, live=zeile)
        except Exception as e:  # Netzwerk, JSON, HTTP – weiterlaufen
            with STATUS_LOCK:
                STATUS["fehler_seit_start"] += 1
                STATUS["letzter_fehler"] = f"{datetime.now():%d.%m. %H:%M:%S} Messung: {e}"
            if time.time() - letzter_log > 60:
                LOG.warning("Messung fehlgeschlagen: %s", e)
                letzter_log = time.time()
        if len(puffer) >= 10:
            speichern(con, puffer, stufen)
            puffer, stufen = [], []
    speichern(con, puffer, stufen)


def laststufen(alt, neu, mindestens=2.0):
    """Stromsprünge je Phase mit zugehöriger Spannungsänderung und Unruhe der anderen Phasen."""
    ergebnis = []
    for p, _ in PHASEN:
        try:
            di = neu[f"{p}_current"] - alt[f"{p}_current"]
            du = neu[f"{p}_voltage"] - alt[f"{p}_voltage"]
        except (KeyError, TypeError):
            continue
        if abs(di) < mindestens:
            continue
        ruhe = sum(abs(neu.get(f"{q}_current", 0) - alt.get(f"{q}_current", 0)) for q, _ in PHASEN if q != p)
        ergebnis.append((neu["ts"], p, du, di, ruhe))
    return ergebnis


def speichern(con, puffer, stufen):
    einfuegen(con, "sekunde", puffer)
    con.executemany("INSERT OR REPLACE INTO stufe VALUES (?, ?, ?, ?, ?)", stufen)
    con.commit()


def minuten_abgleichen(con, shelly):
    letzter = con.execute("SELECT MAX(ts) FROM minute").fetchone()[0]
    jetzt = int(time.time())
    if letzter is None:
        try:
            start = shelly.erster_datensatz() or jetzt - 60 * 86400
        except Exception:
            start = jetzt - 60 * 86400
    else:
        start = letzter + 1
    gesamt = 0
    while start < jetzt:
        ende = min(start + 86400, jetzt)
        zeilen = shelly.minutenwerte(start, ende)
        for zeile in zeilen:
            abgeleitet_minute(zeile)
        einfuegen(con, "minute", zeilen)
        con.commit()
        gesamt += len(zeilen)
        start = ende
    return gesamt


def abgleich(k, stop):
    """Holt die Minutenhistorie aus dem Shelly und räumt alte Sekundenwerte auf."""
    con = verbinden(k.datenbank)
    shelly = Shelly(k)
    letzte_reinigung, nachberechnet = 0.0, False
    while not stop.is_set():
        try:
            n = minuten_abgleichen(con, shelly)
            if not nachberechnet:
                m = nachberechnen(con)
                nachberechnet = True
                if m:
                    LOG.info("%d Datensätze nachberechnet (N-Strom, Sternpunktverschiebung)", m)
            with STATUS_LOCK:
                STATUS["letzter_abgleich"] = int(time.time())
                STATUS["abgleich_zeilen"] += n
            if n:
                LOG.info("%d Minutenwerte aus dem Shelly übernommen", n)
        except Exception as e:
            with STATUS_LOCK:
                STATUS["letzter_fehler"] = f"{datetime.now():%d.%m. %H:%M:%S} Abgleich: {e}"
            LOG.warning("Abgleich der Minutenwerte fehlgeschlagen: %s", e)
        if k.aufbewahrung_tage > 0 and time.time() - letzte_reinigung > 86400:
            grenze = int(time.time()) - k.aufbewahrung_tage * 86400
            geloescht = con.execute("DELETE FROM sekunde WHERE ts < ?", (grenze,)).rowcount
            con.execute("DELETE FROM stufe WHERE ts < ?", (grenze,))
            con.commit()
            letzte_reinigung = time.time()
            if geloescht:
                LOG.info("%d alte Sekundenwerte gelöscht", geloescht)
        stop.wait(k.abgleich_minuten * 60)


# --------------------------------------------------------------------------- Überwachung und Alarm

STUFE_RANG = {"warnung": 1, "kritisch": 2}


def alarm_bedingungen(zeile, k):
    """Grenzverletzungen eines Sekundenwerts: {schlüssel: (stufe, text, wert)}."""
    verletzt = {}
    for p, name in PHASEN:
        u, i = zeile.get(f"{p}_voltage"), zeile.get(f"{p}_current")
        if u is not None:
            if u < k.alarm_u_ausfall:
                verletzt[f"ausfall_{p}"] = ("kritisch", f"Spannungsausfall {name}", u)
            elif u < k.alarm_u_krit_min:
                verletzt[f"unter_{p}"] = ("kritisch", f"Unterspannung {name}", u)
            elif u < k.alarm_u_min:
                verletzt[f"unter_{p}"] = ("warnung", f"Unterspannung {name}", u)
            if u > k.alarm_u_krit_max:
                verletzt[f"ueber_{p}"] = ("kritisch", f"Überspannung {name}", u)
            elif u > k.alarm_u_max:
                verletzt[f"ueber_{p}"] = ("warnung", f"Überspannung {name}", u)
        if i is not None and k.alarm_strom and i > k.alarm_strom:
            verletzt[f"strom_{p}"] = ("warnung", f"Hoher Strom {name}", i)
    s = zeile.get("calc_sternpunkt")
    if s is not None and s > k.alarm_stern:
        verletzt["stern"] = ("kritisch" if s > k.alarm_stern_krit else "warnung", "Sternpunktverschiebung", s)
    return verletzt


def live_text(zeile, k):
    if not zeile:
        return "keine Messwerte"
    teile = []
    for p, name in PHASEN:
        u, i = zeile.get(f"{p}_voltage"), zeile.get(f"{p}_current")
        teile.append(f"{name} {z(u, 1, 'V')} / {z(i, 1, 'A')}")
    if zeile.get("calc_sternpunkt") is not None:
        teile.append(f"Sternpunkt {z(zeile['calc_sternpunkt'], 1, 'V')}")
    if zeile.get("calc_n_current") is not None:
        teile.append(f"N ≈ {z(zeile['calc_n_current'], 1, 'A')}")
    return f"{zeit(zeile.get('ts'), k, '%d.%m. %H:%M:%S')}: " + ", ".join(teile)


class Melder:
    """Verschickt Meldungen über ntfy und/oder E-Mail in einem eigenen Thread, damit ein hängender
    Versand die Überwachung nicht aufhält."""

    PRIORITAET = {"kritisch": "5", "warnung": "4", "entwarnung": "3", "info": "2"}
    TAGS = {"kritisch": "rotating_light", "warnung": "warning", "entwarnung": "white_check_mark", "info": "information_source"}

    def __init__(self, k):
        self.k = k
        self.schlange = []
        self.bedingung = threading.Condition()

    @property
    def aktiv(self):
        return bool(self.k.ntfy_url or (self.k.smtp_host and self.k.mail_an))

    def melden(self, stufe, titel, text):
        LOG.log(logging.WARNING if stufe in ("kritisch", "warnung") else logging.INFO, "ALARM %s: %s – %s",
                stufe, titel, text)
        status_setzen(letzte_meldung=f"{datetime.now(self.k.zeitzone):%d.%m. %H:%M:%S} {stufe}: {titel}")
        with self.bedingung:
            self.schlange.append((stufe, titel, text))
            self.bedingung.notify()

    def senden(self, stufe, titel, text):
        """Sendet sofort über alle eingerichteten Kanäle; liefert die Fehler je Kanal."""
        fehler = {}
        titel = f"{self.k.name}: {titel}"
        if self.k.ntfy_url:
            try:
                kopf = {"Title": "=?UTF-8?B?" + base64.b64encode(titel.encode()).decode() + "?=",
                        "Priority": self.PRIORITAET.get(stufe, "3"), "Tags": self.TAGS.get(stufe, "")}
                if self.k.ntfy_token:
                    kopf["Authorization"] = f"Bearer {self.k.ntfy_token}"
                requests.post(self.k.ntfy_url, data=text.encode("utf-8"), headers=kopf, timeout=15).raise_for_status()
            except Exception as e:
                fehler["ntfy"] = str(e)
        if self.k.smtp_host and self.k.mail_an:
            try:
                import smtplib
                from email.message import EmailMessage
                m = EmailMessage()
                m["Subject"] = f"[{stufe.upper()}] {titel}"
                m["From"] = self.k.mail_von
                m["To"] = ", ".join(self.k.mail_an)
                m.set_content(text)
                with smtplib.SMTP(self.k.smtp_host, self.k.smtp_port, timeout=20) as s:
                    if self.k.smtp_port != 25:
                        s.starttls()
                    if self.k.smtp_benutzer:
                        s.login(self.k.smtp_benutzer, self.k.smtp_passwort)
                    s.send_message(m)
            except Exception as e:
                fehler["mail"] = str(e)
        for kanal, f in fehler.items():
            LOG.warning("Meldung über %s fehlgeschlagen: %s", kanal, f)
        return fehler

    def arbeiten(self, stop):
        while not stop.is_set():
            with self.bedingung:
                while not self.schlange and not stop.is_set():
                    self.bedingung.wait(5)
                if not self.schlange:
                    continue
                stufe, titel, text = self.schlange.pop(0)
            # Ein Fehlversuch (z. B. Internet kurz weg) wird nach 30 s noch einmal versucht
            if self.senden(stufe, titel, text) and not stop.wait(30):
                self.senden(stufe, titel, text)


class Ueberwachung:
    """Zustandsautomat je Grenzverletzung: melden nach Mindestdauer, Eskalation, Erinnerung, Entwarnung."""

    def __init__(self, k, melder, con):
        self.k, self.melder, self.con = k, melder, con
        self.seit = {}      # schlüssel -> Beginn der laufenden Verletzung (noch nicht gemeldet)
        self.aktiv = {}     # schlüssel -> dict(id, stufe, text, extrem, gemeldet, zuletzt_verletzt)

    def _extrem(self, schluessel, alt, neu):
        if alt is None:
            return neu
        return min(alt, neu) if schluessel.startswith(("unter_", "ausfall_")) else max(alt, neu)

    def _text(self, text, wert, schluessel, zeile):
        einheit = "A" if schluessel.startswith("strom_") else ("s" if schluessel == "messung" else "V")
        return f"{text}: {z(wert, 1, einheit)}\n{live_text(zeile, self.k)}"

    def pruefen(self, jetzt, zeile, alter):
        if alter is None or alter > self.k.alarm_keine_daten:
            verletzt = {"messung": ("kritisch", "Keine Messwerte vom Shelly (Spannungsausfall am Messpunkt, "
                                                "Shelly oder Netzwerk gestört)", alter if alter is not None else -1)}
            frisch = False
        else:
            verletzt = alarm_bedingungen(zeile or {}, self.k)
            frisch = True
        for s in list(self.seit):
            if s not in verletzt:
                del self.seit[s]
        for s, (stufe, text, wert) in verletzt.items():
            a = self.aktiv.get(s)
            if a:
                a["zuletzt"] = jetzt
                a["extrem"] = self._extrem(s, a["extrem"], wert)
                if STUFE_RANG[stufe] > STUFE_RANG[a["stufe"]]:
                    a.update(stufe=stufe, text=text, gemeldet=jetzt)
                    self.melder.melden(stufe, text, self._text(text, wert, s, zeile))
                elif a["stufe"] == "kritisch" and jetzt - a["gemeldet"] >= self.k.alarm_wiederholung * 60:
                    a["gemeldet"] = jetzt
                    self.melder.melden(stufe, f"weiterhin: {text}", self._text(text, wert, s, zeile)
                                       + f"\nseit {zeit(a['start'], self.k, '%d.%m. %H:%M:%S')}")
                self.con.execute("UPDATE alarm SET stufe = ?, text = ?, extrem = ? WHERE id = ?",
                                 (a["stufe"], a["text"], a["extrem"], a["id"]))
                continue
            beginn = self.seit.setdefault(s, jetzt)
            mindest = self.k.alarm_dauer_krit if stufe == "kritisch" else self.k.alarm_dauer
            if s == "messung":
                mindest = 0
            if jetzt - beginn + 1 >= mindest:
                del self.seit[s]
                aid = self.con.execute("INSERT INTO alarm (start, schluessel, stufe, text, extrem) VALUES (?, ?, ?, ?, ?)",
                                       (beginn, s, stufe, text, wert)).lastrowid
                self.aktiv[s] = {"id": aid, "start": beginn, "stufe": stufe, "text": text, "extrem": wert,
                                 "gemeldet": jetzt, "zuletzt": jetzt}
                self.melder.melden(stufe, text, self._text(text, wert, s, zeile))
        for s in list(self.aktiv):
            a = self.aktiv[s]
            if s in verletzt:
                continue
            # Grenzwerte gelten erst nach entwarnung_s frischer, unauffälliger Daten als wieder eingehalten;
            # der Messausfall endet mit dem ersten frischen Wert
            if (s == "messung" and frisch) or (frisch and jetzt - a["zuletzt"] >= self.k.alarm_entwarnung):
                self.con.execute("UPDATE alarm SET ende = ? WHERE id = ?", (jetzt, a["id"]))
                del self.aktiv[s]
                self.melder.melden("entwarnung", f"Entwarnung: {a['text']}",
                                   f"{a['text']} von {zeit(a['start'], self.k, '%d.%m. %H:%M:%S')} bis "
                                   f"{zeit(jetzt, self.k, '%H:%M:%S')}, Extremwert {z(a['extrem'], 1)}\n"
                                   + live_text(zeile, self.k))
        self.con.commit()
        status_setzen(alarme_aktiv=[{"schluessel": s, "stufe": a["stufe"], "text": a["text"], "seit": a["start"],
                                     "extrem": a["extrem"]} for s, a in sorted(self.aktiv.items())])


def ueberwachung(k, stop, melder):
    """Prüft jede Sekunde den letzten Messwert, meldet Grenzverletzungen und pingt die Heartbeat-URL."""
    con = verbinden(k.datenbank)
    # Beim Neustart offene Alarme schließen, sie werden bei Bedarf neu erkannt
    con.execute("UPDATE alarm SET ende = ? WHERE ende IS NULL", (int(time.time()),))
    con.commit()
    u = Ueberwachung(k, melder, con)
    melder.melden("info", "Überwachung gestartet",
                  f"Grenzen: U {z(k.alarm_u_min, 0)}–{z(k.alarm_u_max, 0)} V (kritisch {z(k.alarm_u_krit_min, 0)}–"
                  f"{z(k.alarm_u_krit_max, 0)} V), Sternpunkt {z(k.alarm_stern, 0)}/{z(k.alarm_stern_krit, 0)} V, "
                  f"Strom {z(k.alarm_strom, 0)} A, Messausfall nach {z(k.alarm_keine_daten, 0)} s")
    stop.wait(max(5, 3 * k.intervall))  # erste Messungen abwarten
    letzter_ping = 0.0
    while not stop.is_set():
        jetzt = int(time.time())
        with STATUS_LOCK:
            zeile, letzte = STATUS["live"], STATUS["letzte_messung"]
        alter = None if letzte is None else jetzt - letzte
        try:
            u.pruefen(jetzt, zeile, alter)
        except Exception:
            LOG.exception("Überwachung fehlgeschlagen")
        if k.heartbeat_url and alter is not None and alter <= k.alarm_keine_daten and time.time() - letzter_ping >= 60:
            letzter_ping = time.time()
            try:
                requests.get(k.heartbeat_url, timeout=10)
            except Exception as e:
                LOG.warning("Heartbeat fehlgeschlagen: %s", e)
        stop.wait(1)
    con.close()


def alarme(con, grenze=200):
    zeilen = con.execute("SELECT start, ende, schluessel, stufe, text, extrem FROM alarm ORDER BY start DESC LIMIT ?",
                         (grenze,)).fetchall()
    return [dict(zip(("start", "ende", "schluessel", "stufe", "text", "extrem"), r)) for r in zeilen]


def alarm_banner(st, k):
    aktiv = st.get("alarme_aktiv") or []
    if not aktiv:
        return ""
    stufe = "krit" if any(a["stufe"] == "kritisch" for a in aktiv) else "warn"
    punkte = "".join(f"<li>{html.escape(a['text'])} seit {zeit(a['seit'], k, '%d.%m. %H:%M:%S')}"
                     f" (Extremwert {z(a['extrem'], 1)})</li>" for a in aktiv)
    return (f'<div class="alarm {stufe}"><b>Aktiver Alarm</b><ul>{punkte}</ul>'
            f'<a href="alarme">Alle Alarme</a></div>')


def alarme_html(liste, k):
    zeilen = []
    for a in liste:
        stufe = {"kritisch": "krit", "warnung": "warn"}.get(a["stufe"], "info")
        zeilen.append(f"<tr><td>{zeit(a['start'], k, '%d.%m.%Y %H:%M:%S')}</td>"
                      f"<td>{zeit(a['ende'], k, '%d.%m.%Y %H:%M:%S') if a['ende'] else '<b>aktiv</b>'}</td>"
                      f"<td>{ampel(stufe, a['stufe'])}</td><td>{html.escape(a['text'] or '')}</td>"
                      f"<td>{z(a['extrem'], 1)}</td></tr>")
    tabelle = ("<div class='scroll'><table><thead><tr><th>Beginn</th><th>Ende</th><th>Stufe</th><th>Meldung</th>"
               "<th>Extremwert</th></tr></thead><tbody>" + "".join(zeilen) + "</tbody></table></div>"
               if zeilen else "<p class='mut'>Bisher keine Alarme.</p>")
    return [f"<section class='karte'><h2>Alarme (neueste zuerst, höchstens 200)</h2>{tabelle}"
            "<p class='hinweis'>Extremwert: kleinste bzw. größte Spannung in V, Strom in A, "
            "Messausfall in s.</p></section>"]


# --------------------------------------------------------------------------- Neutralleiter und Sternpunkt

# Lage der Spannungszeiger L1, L2, L3 (Rechtsdrehfeld)
WINKEL = {"a": 0.0, "b": -2 * math.pi / 3, "c": 2 * math.pi / 3}
BERECHNUNG_VERSION = 1


def sternpunkt(ua, ub, uc):
    """Verschiebung des Sternpunkts in V aus den drei Spannungen L–N.

    Die Spannungen sind die Abstände des Sternpunkts zu den Ecken des Außenleiter-Dreiecks. Unter der
    Annahme gleich großer Außenleiterspannungen (Seite s) gilt 3(a⁴+b⁴+c⁴+s⁴) = (a²+b²+c²+s²)², und der
    Abstand zum Mittelpunkt ist √((a²+b²+c² − s²)/3)."""
    if None in (ua, ub, uc) or min(ua, ub, uc) < 50:
        return None
    s2, s4 = ua * ua + ub * ub + uc * uc, ua ** 4 + ub ** 4 + uc ** 4
    d = s2 * s2 - 2 * (3 * s4 - s2 * s2)
    if d < 0:
        return None
    seite2 = (s2 + math.sqrt(d)) / 2
    return math.sqrt(max(0.0, (s2 - seite2) / 3))


def n_strom(stroeme, winkel):
    """Neutralleiterstrom (Grundschwingung) als Zeigersumme der Außenleiterströme.

    winkel: Phasenverschiebung φ je Phase in rad, positiv = induktiv (Strom eilt nach)."""
    re = im = 0.0
    for p, _ in PHASEN:
        i = stroeme.get(p)
        if i is None:
            return None
        w = WINKEL[p] - winkel.get(p, 0.0)
        re += i * math.cos(w)
        im += i * math.sin(w)
    return math.hypot(re, im)


def abgeleitet_minute(zeile):
    """Ergänzt eine Minutenzeile um berechneten N-Strom und Sternpunktverschiebung."""
    winkel = {}
    for p, _ in PHASEN:
        wirk = (zeile.get(f"{p}_total_act_energy") or 0) - (zeile.get(f"{p}_total_act_ret_energy") or 0)
        blind = (zeile.get(f"{p}_lag_react_energy") or 0) - (zeile.get(f"{p}_lead_react_energy") or 0)
        winkel[p] = math.atan2(blind, wirk) if (wirk or blind) else 0.0
    zeile["calc_n_current"] = n_strom({p: zeile.get(f"{p}_avg_current") for p, _ in PHASEN}, winkel)
    zeile["calc_sternpunkt"] = sternpunkt(*(zeile.get(f"{p}_avg_voltage") for p, _ in PHASEN))
    zeile["calc_version"] = BERECHNUNG_VERSION


def abgeleitet_sekunde(zeile, vorzeichen):
    """Wie abgeleitet_minute; die Richtung der Blindleistung (induktiv/kapazitiv) stammt aus den Minutenwerten,
    weil die Momentanwerte nur ihren Betrag liefern."""
    winkel = {}
    for p, _ in PHASEN:
        wirk, schein = zeile.get(f"{p}_act_power"), zeile.get(f"{p}_aprt_power")
        if wirk is None or schein is None:
            winkel[p] = 0.0
            continue
        blind = math.sqrt(max(0.0, schein * schein - wirk * wirk)) * vorzeichen.get(p, 1.0)
        winkel[p] = math.atan2(blind, wirk) if (wirk or blind) else 0.0
    zeile["calc_n_current"] = n_strom({p: zeile.get(f"{p}_current") for p, _ in PHASEN}, winkel)
    zeile["calc_sternpunkt"] = sternpunkt(*(zeile.get(f"{p}_voltage") for p, _ in PHASEN))
    zeile["calc_version"] = BERECHNUNG_VERSION


def _blind_ausdruck(con):
    sp = set(spalten(con, "minute"))
    if not all(f"{p}_lag_react_energy" in sp and f"{p}_lead_react_energy" in sp for p, _ in PHASEN):
        return None
    return ", ".join(f'"{p}_lag_react_energy" - "{p}_lead_react_energy"' for p, _ in PHASEN)


def blind_vorzeichen(con):
    """Richtung der Blindleistung je Phase aus dem jüngsten Minutenwert (+1 induktiv, −1 kapazitiv)."""
    ausdruck = _blind_ausdruck(con)
    if not ausdruck:
        return {}
    zeile = con.execute(f"SELECT {ausdruck} FROM minute ORDER BY ts DESC LIMIT 1").fetchone()
    return {p: -1.0 if (q or 0) < 0 else 1.0 for (p, _), q in zip(PHASEN, zeile)} if zeile else {}


def nachberechnen(con, stapel=20000):
    """Ergänzt N-Strom und Sternpunktverschiebung für vorhandene Daten (einmalig nach einem Update)."""
    gesamt = 0
    for tabelle in ("minute", "sekunde"):
        vorhanden = set(spalten(con, tabelle))
        for n in ("calc_version", "calc_n_current", "calc_sternpunkt"):
            if n not in vorhanden:
                con.execute(f'ALTER TABLE {tabelle} ADD COLUMN "{n}" REAL')
        namen = spalten(con, tabelle)
        ausdruck = _blind_ausdruck(con)
        while True:
            zeilen = [dict(zip(namen, z)) for z in con.execute(
                f"SELECT * FROM {tabelle} WHERE calc_version IS NULL ORDER BY ts LIMIT ?", (stapel,))]
            if not zeilen:
                break
            if tabelle == "minute":
                for zeile in zeilen:
                    abgeleitet_minute(zeile)
            else:
                vz = {}
                if ausdruck:
                    for ts, *q in con.execute(f"SELECT ts, {ausdruck} FROM minute WHERE ts >= ? AND ts <= ?",
                                              (zeilen[0]["ts"] - 60, zeilen[-1]["ts"])):
                        vz[ts // 60 * 60] = {p: -1.0 if (w or 0) < 0 else 1.0 for (p, _), w in zip(PHASEN, q)}
                for zeile in zeilen:
                    abgeleitet_sekunde(zeile, vz.get(zeile["ts"] // 60 * 60, {}))
            con.executemany(f"UPDATE {tabelle} SET calc_n_current = ?, calc_sternpunkt = ?, calc_version = ? "
                            "WHERE ts = ?", [(z["calc_n_current"], z["calc_sternpunkt"], BERECHNUNG_VERSION, z["ts"])
                                             for z in zeilen])
            con.commit()
            gesamt += len(zeilen)
    return gesamt


def regression(x, y):
    """Lineare Regression y = a + b·x; liefert (a, b, R², n) oder None."""
    n = len(x)
    if n < 30:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    if sxx <= 0 or syy <= 0:
        return None
    b = sxy / sxx
    return my - b * mx, b, sxy * sxy / (sxx * syy), n


def perzentil(sortiert, q):
    if not sortiert:
        return None
    return sortiert[min(len(sortiert) - 1, max(0, int(round(q * (len(sortiert) - 1)))))]


def neutral_auswertung(con, von, bis, tabelle, punkte=1500):
    """Berechneter N-Strom und Sternpunktverschiebung, ihre Statistik und die Ausgleichsgerade
    Verschiebung = U0 + Z(N)·I(N)."""
    if "calc_sternpunkt" not in set(spalten(con, tabelle)):
        return None
    zeilen = con.execute(f"SELECT ts, calc_n_current, calc_sternpunkt FROM {tabelle} WHERE ts >= ? AND ts < ? "
                         "AND calc_n_current IS NOT NULL AND calc_sternpunkt IS NOT NULL ORDER BY ts",
                         (von, bis)).fetchall()
    if not zeilen:
        return None
    x = [z[1] for z in zeilen]
    y = [z[2] for z in zeilen]
    xs, ys = sorted(x), sorted(y)
    r = {"n": len(zeilen), "quelle": tabelle,
         "i_max": xs[-1], "i_99": perzentil(xs, 0.99), "i_avg": sum(x) / len(x),
         "u_max": ys[-1], "u_95": perzentil(ys, 0.95), "u_avg": sum(y) / len(y), "fit": None}
    fit = regression(x, y)
    if fit and (xs[-1] - xs[0]) >= 3:
        r["fit"] = {"u0": fit[0], "z": fit[1], "r2": fit[2], "n": fit[3]}
    klassen = {}
    for a, b in zip(x, y):
        klassen.setdefault(int(a // 2), []).append(b)
    r["klassen"] = [{"von": 2 * kl, "bis": 2 * kl + 2, "n": len(w), "median": perzentil(sorted(w), 0.5),
                     "p95": perzentil(sorted(w), 0.95)} for kl, w in sorted(klassen.items())]
    schritt = max(1, math.ceil(len(zeilen) / punkte))
    auswahl = zeilen[::schritt]
    r["punkte"] = {"t": [z[0] for z in auswahl], "x": [round(z[1], 2) for z in auswahl],
                   "y": [round(z[2], 2) for z in auswahl]}
    return r


# --------------------------------------------------------------------------- Auswertung

def quelle_waehlen(con, von, bis):
    """Minutenwerte des Shelly sind die Hauptquelle; ohne sie dienen Sekundenwerte."""
    n_min = con.execute("SELECT COUNT(*) FROM minute WHERE ts >= ? AND ts < ?", (von, bis)).fetchone()[0]
    n_sek = con.execute("SELECT COUNT(*) FROM sekunde WHERE ts >= ? AND ts < ?", (von, bis)).fetchone()[0]
    return n_min, n_sek


def felder(con, tabelle):
    """Spaltennamen je Phase für Strom/Spannung/Leistung, abhängig von der Quelle."""
    vorhanden = set(spalten(con, tabelle))
    c = lambda n: f'"{n}"' if n in vorhanden else "NULL"
    f = {}
    for p, _ in PHASEN:
        if tabelle == "minute":
            f[p] = dict(imax=c(f"{p}_max_current"), iavg=c(f"{p}_avg_current"), umin=c(f"{p}_min_voltage"),
                        umax=c(f"{p}_max_voltage"), uavg=c(f"{p}_avg_voltage"), smax=c(f"{p}_max_aprt_power"))
        else:
            f[p] = dict(imax=c(f"{p}_current"), iavg=c(f"{p}_current"), umin=c(f"{p}_voltage"),
                        umax=c(f"{p}_voltage"), uavg=c(f"{p}_voltage"), smax=c(f"{p}_aprt_power"))
    f["n_max"] = c("n_max_current") if tabelle == "minute" else c("n_current")
    f["n_avg"] = c("n_avg_current") if tabelle == "minute" else c("n_current")
    f["spalte"] = c
    return f


def quantil(con, ausdruck, tabelle, von, bis, q, bedingung="1"):
    sql = f"FROM {tabelle} WHERE ts >= ? AND ts < ? AND ({ausdruck}) IS NOT NULL AND {bedingung}"
    n = con.execute(f"SELECT COUNT(*) {sql}", (von, bis)).fetchone()[0]
    if not n:
        return None
    return con.execute(f"SELECT {ausdruck} {sql} ORDER BY 1 LIMIT 1 OFFSET ?",
                       (von, bis, min(n - 1, int(q * n)))).fetchone()[0]


def episoden(zeitpunkte, luecke, bezug):
    """Fasst aufeinanderfolgende Zeitpunkte (ts, wert, phase) zu Ereignissen zusammen."""
    ergebnis = []
    for ts, wert, phase in zeitpunkte:
        e = ergebnis[-1] if ergebnis else None
        if e and ts - e["ende"] <= luecke:
            e["ende"] = ts
            e["phasen"].add(phase)
            if abs(wert - e["bezug"]) > abs(e["extrem"] - e["bezug"]):
                e["extrem"] = wert
        else:
            ergebnis.append({"start": ts, "ende": ts, "extrem": wert, "phasen": {phase}, "bezug": bezug})
    return ergebnis


def impedanz(con, p, von, bis, k):
    """Netzimpedanz L–N aus Laststufen: Z ≈ −ΔU/ΔI zwischen zwei Sekundenwerten,
    nur wenn sich die anderen Phasen dabei kaum ändern."""
    sql = """SELECT -du / di FROM stufe WHERE phase = ? AND ts >= ? AND ts < ?
             AND ABS(di) >= ? AND ruhe < ? AND -du / di > 0 AND -du / di < 5"""
    werte = sorted(r[0] for r in con.execute(sql, (p, von, bis, k.min_stufe, k.min_stufe / 2)))
    if len(werte) < 5:
        return None, len(werte)
    return werte[len(werte) // 2], len(werte)


def bericht(con, von, bis, k):
    n_min, n_sek = quelle_waehlen(con, von, bis)
    tabelle = "minute" if n_min else "sekunde"
    f = felder(con, tabelle)
    bereich = "ts >= ? AND ts < ?"
    r = {"name": k.name, "von": von, "bis": bis, "sicherung": k.sicherung, "quelle": tabelle,
         "abdeckung": {"minuten": n_min, "minuten_soll": max(1, (bis - von) // 60),
                       "sekunden": n_sek, "sekunden_soll": max(1, (bis - von) // k.intervall)},
         "phasen": {}, "ereignisse": []}
    if not (n_min or n_sek):
        return r

    # Strom, Lastreserve, Spannung je Phase
    for p, name in PHASEN:
        ph = f[p]
        zeile = con.execute(f"SELECT ts, {ph['imax']}, {ph['umin']} FROM {tabelle} WHERE {bereich} "
                            f"AND {ph['imax']} IS NOT NULL ORDER BY 2 DESC LIMIT 1", (von, bis)).fetchone()
        agg = con.execute(f"""SELECT MIN({ph['umin']}), MAX({ph['umax']}), AVG({ph['uavg']}), MAX({ph['smax']}),
                                SUM({ph['umin']} < {U_MIN}), SUM({ph['umax']} > {U_MAX}), AVG({ph['iavg']})
                              FROM {tabelle} WHERE {bereich}""", (von, bis)).fetchone()
        i15 = con.execute(f"SELECT MAX(x) FROM (SELECT AVG({ph['iavg']}) x FROM {tabelle} WHERE {bereich} "
                          f"GROUP BY ts / 900)", (von, bis)).fetchone()[0]
        d = {"name": name, "imax": zeile[1] if zeile else None, "imax_ts": zeile[0] if zeile else None,
             "u_bei_imax": zeile[2] if zeile else None,
             "i999": quantil(con, ph["imax"], tabelle, von, bis, 0.999), "i15": i15, "iavg": agg[6],
             "umin": agg[0], "umax": agg[1], "uavg": agg[2], "smax": agg[3],
             "einheiten_unter": agg[4] or 0, "einheiten_ueber": agg[5] or 0}
        d["reserve"] = k.sicherung - d["imax"] if d["imax"] is not None else None
        d["z"], d["z_stufen"] = impedanz(con, p, von, bis, k)
        if d["z"]:
            d["schaltstufe"] = DELTA_U_SCHALT * k.nennspannung / d["z"]
            if d["u_bei_imax"] is not None:
                d["zusatz_spannung"] = max(0.0, (d["u_bei_imax"] - U_MIN) / d["z"])
        grenzen = [x for x in (d["reserve"], d.get("zusatz_spannung")) if x is not None]
        d["zusatz"] = max(0.0, min(grenzen)) if grenzen else None
        d["pf"] = None
        if f"{p}_pf" in spalten(con, "sekunde"):
            d["pf"] = quantil(con, f'ABS("{p}_pf")', "sekunde", von, bis, 0.5, f'ABS("{p}_act_power") > 1000')
        r["phasen"][p] = d

    zusatz = [d["zusatz"] for d in r["phasen"].values() if d["zusatz"] is not None]
    r["zusatz_3ph_kw"] = 3 * min(zusatz) * k.nennspannung / 1000 if len(zusatz) == 3 else None

    # 10-Minuten-Mittel der Spannung (EN 50160) und Unsymmetrie (Näherung über Beträge)
    u10 = con.execute(f"""SELECT ts / 600, {", ".join(f"AVG({f[p]['uavg']})" for p, _ in PHASEN)}
                          FROM {tabelle} WHERE {bereich} GROUP BY ts / 600""", (von, bis)).fetchall()
    for i, (p, _) in enumerate(PHASEN):
        werte = [z[i + 1] for z in u10 if z[i + 1] is not None]
        d = r["phasen"][p]
        d["u10_min"] = min(werte, default=None)
        d["u10_max"] = max(werte, default=None)
        d["u10_band"] = 100 * sum(U_MIN <= w <= U_MAX for w in werte) / len(werte) if werte else None
    unsym = sorted(max(abs(u - sum(z[1:]) / 3) for u in z[1:]) / (sum(z[1:]) / 3) * 100
                   for z in u10 if None not in z[1:])
    r["unsym_max"] = unsym[-1] if unsym else None
    r["unsym_95"] = unsym[int(0.95 * (len(unsym) - 1))] if unsym else None

    a, b, c = (f[p]["iavg"] for p, _ in PHASEN)
    schief = con.execute(f"SELECT MAX(MAX({a},{b},{c}) - MIN({a},{b},{c})), "
                         f"AVG(MAX({a},{b},{c}) - MIN({a},{b},{c}) > {SCHIEFLAST_A}) "
                         f"FROM {tabelle} WHERE {bereich}", (von, bis)).fetchone()
    r["schieflast_max"], r["schieflast_anteil"] = schief[0], (schief[1] or 0) * 100
    n = con.execute(f"SELECT MAX({f['n_max']}), AVG({f['n_avg']}) FROM {tabelle} WHERE {bereich}",
                    (von, bis)).fetchone()
    r["n_max"], r["n_avg"] = n

    # Energie (nur aus den Minutenwerten des Shelly, Wh je Minute)
    if n_min:
        c = felder(con, "minute")["spalte"]
        summe = lambda gr: " + ".join(f"COALESCE(SUM({c(f'{p}_{gr}')}), 0)" for p, _ in PHASEN)
        e = con.execute(f"SELECT {summe('total_act_energy')}, {summe('total_act_ret_energy')}, "
                        f"{summe('lag_react_energy')}, {summe('lead_react_energy')} FROM minute WHERE {bereich}",
                        (von, bis)).fetchone()
        r["energie"] = {"bezug": e[0] / 1000, "einspeisung": e[1] / 1000,
                        "blind_ind": e[2] / 1000, "blind_kap": e[3] / 1000}

    # Frequenz aus den Sekundenwerten
    sek = set(spalten(con, "sekunde"))
    fq = [f'"{p}_freq"' for p, _ in PHASEN if f"{p}_freq" in sek]
    if n_sek and fq:
        fx = f"COALESCE({', '.join(fq)})" if len(fq) > 1 else fq[0]
        z = con.execute(f"""SELECT MIN({fx}), MAX({fx}), AVG({fx}), AVG({fx} * {fx}),
                              SUM({fx} < {F_NORMAL[0]} OR {fx} > {F_NORMAL[1]}),
                              SUM({fx} < {F_MIN} OR {fx} > {F_MAX}), COUNT({fx})
                            FROM sekunde WHERE {bereich}""", (von, bis)).fetchone()
        if z[6]:
            r["frequenz"] = {"min": z[0], "max": z[1], "avg": z[2],
                             "sigma_mhz": math.sqrt(max(0.0, z[3] - z[2] ** 2)) * 1000,
                             "ausserhalb_normal": z[4], "ausserhalb_norm": z[5]}
            punkte = con.execute(f"SELECT ts, {fx} FROM sekunde WHERE {bereich} AND "
                                 f"({fx} < {F_NORMAL[0]} OR {fx} > {F_NORMAL[1]}) ORDER BY ts LIMIT 20000",
                                 (von, bis)).fetchall()
            for e in episoden([(t, w, "Netz") for t, w in punkte], 3 * k.intervall, 50.0):
                e["art"] = "Frequenz"
                r["ereignisse"].append(e)

    # Spannungsereignisse außerhalb 207–253 V
    # Unter- und Überspannung getrennt zusammenfassen: gleichzeitig eine Phase zu hoch und eine
    # andere zu niedrig ist das typische Bild eines hochohmigen Neutralleiters und muss sichtbar bleiben.
    schritt = 60 if tabelle == "minute" else k.intervall
    for art, spalte, vergleich in (("Unterspannung", "umin", f"< {U_MIN}"), ("Überspannung", "umax", f"> {U_MAX}")):
        teile = [f"SELECT ts, {f[p][spalte]}, '{name}' FROM {tabelle} WHERE {bereich} AND {f[p][spalte]} {vergleich}"
                 for p, name in PHASEN]
        punkte = con.execute(" UNION ALL ".join(teile) + " ORDER BY 1 LIMIT 20000", (von, bis) * len(teile)).fetchall()
        for e in episoden(punkte, 2 * schritt, k.nennspannung):
            e["art"] = art
            e["ende"] += schritt
            r["ereignisse"].append(e)
    r["ereignisse"].sort(key=lambda e: e["start"], reverse=True)
    for e in r["ereignisse"]:
        e["phasen"] = sorted(e["phasen"])
    r["neutral"] = neutral_auswertung(con, von, bis, tabelle)
    r["kopplung"] = kopplung(con, von, bis, tabelle, schritt)
    r["zuleitung"] = zuleitung(k)
    return r


def kleinste_quadrate(x, y):
    """Mehrfache lineare Regression y = c0 + Σ cj·xj (Normalgleichungen, Gauß mit Pivotsuche).
    Liefert (Koeffizienten, R²) oder None."""
    m = len(x[0]) + 1
    a = [[0.0] * m for _ in range(m)]
    b = [0.0] * m
    for zeile, w in zip(x, y):
        v = [1.0] + list(zeile)
        for i in range(m):
            b[i] += v[i] * w
            for j in range(m):
                a[i][j] += v[i] * v[j]
    for i in range(m):
        piv = max(range(i, m), key=lambda r: abs(a[r][i]))
        if abs(a[piv][i]) < 1e-12:
            return None
        a[i], a[piv], b[i], b[piv] = a[piv], a[i], b[piv], b[i]
        for r in range(i + 1, m):
            f = a[r][i] / a[i][i]
            for j in range(i, m):
                a[r][j] -= f * a[i][j]
            b[r] -= f * b[i]
    koeff = [0.0] * m
    for i in range(m - 1, -1, -1):
        koeff[i] = (b[i] - sum(a[i][j] * koeff[j] for j in range(i + 1, m))) / a[i][i]
    my = sum(y) / len(y)
    ss_tot = sum((w - my) ** 2 for w in y)
    ss_res = sum((w - koeff[0] - sum(c * v for c, v in zip(koeff[1:], zeile))) ** 2 for zeile, w in zip(x, y))
    return koeff, (1 - ss_res / ss_tot) if ss_tot > 0 else None


def kopplung(con, von, bis, tabelle, raster):
    """Kopplungsmatrix M[k][j] = ΔU(k)/ΔI(j) in Ω aus Änderungen zwischen aufeinanderfolgenden Messwerten.

    Modell (ohmsch, linearisiert): ΔU(k) = −(Z_L + Z_N)·ΔI(k) + ½·Z_N·Σ ΔI(j≠k). Daraus
    Z_N ≈ 2 × Mittel der Nebendiagonale und Z_L(k) ≈ −M[k][k] − Z_N."""
    f = felder(con, tabelle)
    sp = [f[p]["uavg"] for p, _ in PHASEN] + [f[p]["iavg"] for p, _ in PHASEN]
    zeilen = con.execute(f"SELECT ts, {', '.join(sp)} FROM {tabelle} WHERE ts >= ? AND ts < ? ORDER BY ts",
                         (von, bis)).fetchall()
    x, y = [], [[], [], []]
    for alt, neu in zip(zeilen, zeilen[1:]):
        if neu[0] - alt[0] != raster or None in alt or None in neu:
            continue
        x.append([neu[4 + j] - alt[4 + j] for j in range(3)])
        for k in range(3):
            y[k].append(neu[1 + k] - alt[1 + k])
    if len(x) < 100:
        return None
    streuung = [math.sqrt(sum(v[j] ** 2 for v in x) / len(x)) for j in range(3)]
    matrix, r2 = [], []
    for k in range(3):
        ergebnis = kleinste_quadrate(x, y[k])
        if not ergebnis:
            return None
        matrix.append(ergebnis[0][1:])
        r2.append(ergebnis[1])
    neben = [matrix[k][j] for k in range(3) for j in range(3) if j != k]
    z_n = 2 * sum(neben) / len(neben)
    return {"matrix": matrix, "r2": r2, "n": len(x), "streuung_di": streuung, "raster": raster,
            "z_n": z_n, "z_l": [-matrix[k][k] - z_n for k in range(3)]}


# Höchstwerte des Leiterwiderstands bei 20 °C in Ω/km, DIN EN 60228 Klasse 2 (mehrdrähtig bzw. sektorförmig)
LEITER_OHM_KM = {
    "cu": {1.5: 12.1, 2.5: 7.41, 4: 4.61, 6: 3.08, 10: 1.83, 16: 1.15, 25: 0.727, 35: 0.524, 50: 0.387,
           70: 0.268, 95: 0.193, 120: 0.153, 150: 0.124, 185: 0.0991, 240: 0.0754, 300: 0.0601},
    "al": {16: 1.91, 25: 1.20, 35: 0.868, 50: 0.641, 70: 0.443, 95: 0.320, 120: 0.253, 150: 0.206,
           185: 0.164, 240: 0.125, 300: 0.100},
}
TEMPERATURBEIWERT = {"cu": 0.00393, "al": 0.00403}


def leiterwiderstand(laenge_m, mm2, material):
    """Widerstand eines Leiters bei 20 °C; Normwert, sonst aus dem spezifischen Widerstand."""
    tabelle = LEITER_OHM_KM.get(material, LEITER_OHM_KM["cu"])
    if mm2 in tabelle:
        return tabelle[mm2] * laenge_m / 1000
    return (0.0287 if material == "al" else 0.0178) * laenge_m / mm2


def zuleitung(k):
    """Erwartete Widerstände der Zuleitung vom Messpunkt bis zur Einspeisung, Summe über alle Abschnitte."""
    if not k.zuleitung:
        return None
    abschnitte, r_l, r_n, r_l70, r_n70, laenge = [], 0.0, 0.0, 0.0, 0.0, 0.0
    for a in k.zuleitung:
        warm = 1 + TEMPERATURBEIWERT.get(a["material"], 0.00393) * 50
        rl = leiterwiderstand(a["laenge_m"], a["mm2"], a["material"])
        rn = leiterwiderstand(a["laenge_m"], a["mm2_n"], a["material"])
        abschnitte.append(dict(a, r_l=[rl, rl * warm], r_n=[rn, rn * warm]))
        r_l, r_n, r_l70, r_n70 = r_l + rl, r_n + rn, r_l70 + rl * warm, r_n70 + rn * warm
        laenge += a["laenge_m"]
    return {"abschnitte": abschnitte, "laenge_m": laenge,
            "r_l": [r_l, r_l70], "r_n": [r_n, r_n70], "schleife": [r_l + r_n, r_l70 + r_n70],
            "x_l": 0.08 * laenge / 1000,
            "steigung": [r_n + 2 / 3 * r_l, r_n70 + 2 / 3 * r_l70]}


def fachdaten(con, von, bis, k, tabelle):
    """Rohdaten-Auswertung für die Fachkraft: Perzentile, 10-min-Statistik, Extremminuten, Tagesprofil."""
    f = felder(con, tabelle)
    calc = "calc_sternpunkt" in set(spalten(con, tabelle))
    groessen = ("umin", "uavg", "umax", "iavg", "imax", "smax")
    sp = ["ts"] + [f[p][g] for p, _ in PHASEN for g in groessen]
    sp += ["calc_n_current", "calc_sternpunkt"] if calc else ["NULL", "NULL"]
    zeilen = con.execute(f"SELECT {', '.join(sp)} FROM {tabelle} WHERE ts >= ? AND ts < ? ORDER BY ts",
                         (von, bis)).fetchall()
    if not zeilen:
        return None
    idx = {(p, g): 1 + i * len(groessen) + j for i, (p, _) in enumerate(PHASEN) for j, g in enumerate(groessen)}
    i_n, i_sp = len(sp) - 2, len(sp) - 1
    qs = (0, 0.01, 0.05, 0.5, 0.95, 0.99, 1)
    d = {"quelle": tabelle, "n": len(zeilen), "perzentile": [], "quantile": qs}
    namen = {"umin": "U Minimum", "uavg": "U Mittel", "umax": "U Maximum", "iavg": "I Mittel", "imax": "I Maximum",
             "smax": "S Maximum"}
    for g in groessen:
        for p, name in PHASEN:
            werte = sorted(z[idx[p, g]] for z in zeilen if z[idx[p, g]] is not None)
            if g == "smax":
                werte = [w / 1000 for w in werte]
            d["perzentile"].append({"groesse": namen[g], "phase": name, "einheit": {"u": "V", "i": "A", "s": "kVA"}[g[0]],
                                    "werte": [perzentil(werte, q) for q in qs]})
    for g, n in (("calc_n", i_n), ("calc_sp", i_sp)):
        werte = sorted(z[n] for z in zeilen if z[n] is not None)
        if werte:
            d["perzentile"].append({"groesse": "N-Strom (berechnet)" if g == "calc_n" else "Sternpunktverschiebung",
                                    "phase": "–", "einheit": "A" if g == "calc_n" else "V",
                                    "werte": [perzentil(werte, q) for q in qs]})

    # 10-Minuten-Mittel der Spannung wie EN 50160
    gruppen = {}
    for z_ in zeilen:
        gruppen.setdefault(z_[0] // 600, []).append(z_)
    d["zehn_min"] = []
    for p, name in PHASEN:
        werte = sorted(sum(v) / len(v) for v in ([z_[idx[p, "uavg"]] for z_ in g if z_[idx[p, "uavg"]] is not None]
                                                 for g in gruppen.values()) if v)
        if werte:
            d["zehn_min"].append({"phase": name, "n": len(werte), "min": werte[0], "p5": perzentil(werte, 0.05),
                                  "p95": perzentil(werte, 0.95), "max": werte[-1],
                                  "band": 100 * sum(U_MIN <= w <= U_MAX for w in werte) / len(werte),
                                  "unter_85": sum(w < 0.85 * k.nennspannung for w in werte)})

    def zeile_aus(z_):
        return {"ts": z_[0], "umin": [z_[idx[p, "umin"]] for p, _ in PHASEN],
                "uavg": [z_[idx[p, "uavg"]] for p, _ in PHASEN], "umax": [z_[idx[p, "umax"]] for p, _ in PHASEN],
                "iavg": [z_[idx[p, "iavg"]] for p, _ in PHASEN], "imax": [z_[idx[p, "imax"]] for p, _ in PHASEN],
                "n": z_[i_n], "sp": z_[i_sp]}
    gueltig = [z_ for z_ in zeilen if None not in [z_[idx[p, g]] for p, _ in PHASEN for g in ("umin", "umax")]]
    d["tiefste"] = [zeile_aus(z_) for z_ in sorted(gueltig, key=lambda z_: min(z_[idx[p, "umin"]] for p, _ in PHASEN))[:15]]
    d["hoechste"] = [zeile_aus(z_) for z_ in sorted(gueltig, key=lambda z_: -max(z_[idx[p, "umax"]] for p, _ in PHASEN))[:10]]

    # Tagesprofil nach Ortszeit
    stunden = {}
    for z_ in zeilen:
        stunden.setdefault(datetime.fromtimestamp(z_[0], k.zeitzone).hour, []).append(z_)
    d["tagesprofil"] = []
    for h in range(24):
        g = stunden.get(h, [])
        eintrag = {"stunde": h, "n": len(g)}
        for p, name in PHASEN:
            uavg = [z_[idx[p, "uavg"]] for z_ in g if z_[idx[p, "uavg"]] is not None]
            umin = [z_[idx[p, "umin"]] for z_ in g if z_[idx[p, "umin"]] is not None]
            iavg = [z_[idx[p, "iavg"]] for z_ in g if z_[idx[p, "iavg"]] is not None]
            eintrag[name] = {"uavg": sum(uavg) / len(uavg) if uavg else None, "umin": min(umin, default=None),
                             "iavg": sum(iavg) / len(iavg) if iavg else None}
        d["tagesprofil"].append(eintrag)
    return d


def verlauf(con, von, bis, k, punkte=720):
    """Zeitreihen für die Diagramme, auf höchstens `punkte` Zeitschritte verdichtet."""
    n_min, n_sek = quelle_waehlen(con, von, bis)
    tabelle = "sekunde" if (bis - von <= 6 * 3600 and n_sek) or not n_min else "minute"
    raster = 60 if tabelle == "minute" else k.intervall
    schritt = max(raster, math.ceil((bis - von) / punkte / raster) * raster)
    f = felder(con, tabelle)
    sp = []
    for p, _ in PHASEN:
        ph = f[p]
        sp += [f"AVG({ph['uavg']})", f"MIN({ph['umin']})", f"MAX({ph['umax']})", f"MAX({ph['imax']})"]
    s_ges = " + ".join(f"COALESCE({f[p]['smax']}, 0)" for p, _ in PHASEN)
    zeilen = con.execute(f"SELECT (ts / {schritt}) * {schritt}, {', '.join(sp)}, MAX({s_ges}) FROM {tabelle} "
                         f"WHERE ts >= ? AND ts < ? GROUP BY 1 ORDER BY 1", (von, bis)).fetchall()
    rund = lambda x, n=2: None if x is None else round(x, n)
    v = {"quelle": tabelle, "schritt": schritt, "t": [z[0] for z in zeilen], "u": {}, "umin": {}, "umax": {}, "i": {},
         "s": [rund(z[-1] / 1000, 3) for z in zeilen]}
    for i, (p, name) in enumerate(PHASEN):
        v["u"][name] = [rund(z[1 + 4 * i]) for z in zeilen]
        v["umin"][name] = [rund(z[2 + 4 * i]) for z in zeilen]
        v["umax"][name] = [rund(z[3 + 4 * i]) for z in zeilen]
        v["i"][name] = [rund(z[4 + 4 * i]) for z in zeilen]

    v["n"], v["sp"] = [], []
    if "calc_sternpunkt" in set(spalten(con, tabelle)):
        werte = dict((z[0], z[1:]) for z in con.execute(
            f"SELECT (ts / {schritt}) * {schritt}, MAX(calc_n_current), MAX(calc_sternpunkt) FROM {tabelle} "
            "WHERE ts >= ? AND ts < ? GROUP BY 1", (von, bis)))
        v["n"] = [rund(werte.get(t, (None, None))[0]) for t in v["t"]]
        v["sp"] = [rund(werte.get(t, (None, None))[1]) for t in v["t"]]

    sek = set(spalten(con, "sekunde"))
    fq = [f'"{p}_freq"' for p, _ in PHASEN if f"{p}_freq" in sek]
    v["f"] = {"t": [], "avg": [], "min": [], "max": [], "schritt": None}
    if n_sek and fq:
        fx = f"COALESCE({', '.join(fq)})" if len(fq) > 1 else fq[0]
        fs = max(k.intervall, math.ceil((bis - von) / punkte / k.intervall) * k.intervall)
        zeilen = con.execute(f"SELECT (ts / {fs}) * {fs}, AVG({fx}), MIN({fx}), MAX({fx}) FROM sekunde "
                             f"WHERE ts >= ? AND ts < ? AND {fx} IS NOT NULL GROUP BY 1 ORDER BY 1",
                             (von, bis)).fetchall()
        v["f"] = {"t": [z[0] for z in zeilen], "avg": [rund(z[1], 3) for z in zeilen],
                  "min": [rund(z[2], 3) for z in zeilen], "max": [rund(z[3], 3) for z in zeilen], "schritt": fs}
    return v


# --------------------------------------------------------------------------- HTML-Bericht

def z(x, nk=1, einheit=""):
    if x is None:
        return "–"
    text = f"{x:,.{nk}f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{text} {einheit}".strip()


def ampel(stufe, text):
    zeichen = {"ok": "✓", "warn": "!", "krit": "✕", "info": "–"}[stufe]
    return f'<span class="chip {stufe}"><span aria-hidden="true">{zeichen}</span> {html.escape(text)}</span>'


def zeit(ts, k, fmt="%d.%m.%Y %H:%M"):
    return datetime.fromtimestamp(ts, k.zeitzone).strftime(fmt) if ts else "–"


def dauer(sek):
    if sek < 120:
        return f"{sek:.0f} s"
    if sek < 7200:
        return f"{sek / 60:.0f} min"
    return f"{sek / 3600:.1f} h".replace(".", ",")


def neutralleiter(r):
    n_max, schief = r.get("n_max"), r.get("schieflast_max") or 0
    # Der Pro 3EM hat keinen Wandler im Neutralleiter; ein Wert nahe 0 bei deutlicher Schieflast ist nicht gemessen.
    if n_max is None or (n_max < 1 and schief > 5):
        return "vom Shelly nicht gemessen (kein Wandler im Neutralleiter), siehe berechneten Wert unten"
    return f"max {z(n_max, 1, 'A')}, Ø {z(r.get('n_avg'), 1, 'A')}"


def neutral_bewertung(r):
    """Vergleicht die gemessene Neutralleiter-Impedanz mit dem, was die Zuleitung erwarten lässt."""
    ko, ne, zu = r.get("kopplung"), r.get("neutral"), r.get("zuleitung")
    gemessen, erwartet, art = None, None, ""
    if ko and ko["z_n"] > 0:
        gemessen, art = ko["z_n"], "Z(N) aus der Kopplungsmatrix"
        erwartet = zu["r_n"][1] if zu else None
    elif ne and ne.get("fit"):
        gemessen, art = ne["fit"]["z"], "Steigung Sternpunktverschiebung/N-Strom (≈ Z_N + ⅔·Z_L)"
        erwartet = zu["steigung"][1] if zu else None
    if gemessen is None:
        if ne and ne["u_max"] > 10:
            return ("warn", "Neutralleiter", f"Sternpunkt verschiebt sich um bis zu {z(ne['u_max'])} V; für Z(N) "
                                             "fehlen noch genug Laständerungen.")
        return ("info", "Neutralleiter", "Für eine Abschätzung fehlen noch Daten mit unterschiedlicher Schieflast.")
    if erwartet is None:
        return ("info", "Neutralleiter", f"{art}: {z(gemessen, 2)} Ω. Zur Einordnung Länge und Querschnitt der "
                                         "Zuleitung in der Konfiguration eintragen ([zuleitung]).")
    faktor = gemessen / erwartet if erwartet else float("inf")
    text = f"{art}: {z(gemessen, 2)} Ω, die Zuleitung erklärt bei 70 °C {z(erwartet, 2)} Ω (Faktor {z(faktor, 1)})."
    if faktor <= 1.5:
        return ("ok", "Neutralleiter", text + " Durch die Zuleitung erklärbar.")
    if faktor <= 2.5:
        return ("warn", "Neutralleiter", text + " Etwas mehr, als die Zuleitung erklärt.")
    return ("krit", "Neutralleiter", text + " Deutlich mehr, als die Zuleitung erklärt: N-/PEN-Verbindungen prüfen.")


def neutral_html(r, mit_streuung=True):
    ne, ko, zu = r.get("neutral"), r.get("kopplung"), r.get("zuleitung")
    if not ne:
        return ""
    e = html.escape
    zeilen = [("N-Strom, berechnet (Grundschwingung)",
               f"max {z(ne['i_max'], 1, 'A')}, 99 %-Wert {z(ne['i_99'], 1, 'A')}, Ø {z(ne['i_avg'], 1, 'A')}"),
              ("Sternpunktverschiebung", f"max {z(ne['u_max'], 1, 'V')}, 95 %-Wert {z(ne['u_95'], 1, 'V')}, "
                                         f"Ø {z(ne['u_avg'], 1, 'V')}")]
    if ne.get("fit"):
        fit = ne["fit"]
        zeilen.append(("Ausgleichsgerade Verschiebung = U₀ + k·I(N)",
                       f"k = {z(fit['z'], 3, 'Ω')} (≈ Z_N + ⅔·Z_L), U₀ = {z(fit['u0'], 1, 'V')}, "
                       f"R² = {z(fit['r2'], 2)}, n = {fit['n']}"))
    if ko:
        zeilen.append(("Aus der Kopplungsmatrix", f"Z_N ≈ {z(ko['z_n'], 3, 'Ω')}, Z_L ≈ "
                       + " / ".join(z(w, 3) for w in ko["z_l"]) + " Ω (L1/L2/L3)"))
    if zu:
        zeilen.append(("Erwartet aus der Zuleitung", f"R_N {z(zu['r_n'][0], 3)}–{z(zu['r_n'][1], 3)} Ω, "
                       f"R_L {z(zu['r_l'][0], 3)}–{z(zu['r_l'][1], 3)} Ω (20–70 °C)"))
    t = ['<section class="karte"><h2>Neutralleiter und Sternpunkt (berechnet)</h2><dl class="liste">'
         + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in zeilen) + "</dl>"]
    if mit_streuung:
        t.append('<div id="streuung"></div>')
    t.append("<p class='hinweis'>Der Pro 3EM misst den Neutralleiter nicht. Der N-Strom ist die Zeigersumme der drei "
             "Außenleiterströme (nur Grundschwingung, Oberschwingungen fehlen, daher eine Untergrenze). Die "
             "Sternpunktverschiebung folgt aus den drei Spannungen L–N unter der Annahme gleich großer "
             "Außenleiterspannungen. Die Steigung enthält neben Z_N auch etwa ⅔ des Außenleiterwiderstands; die "
             "Kopplungsmatrix trennt beides.</p></section>")
    return "".join(t)


def bewertung(r, k):
    zeilen = []
    ph = r["phasen"].values()
    band = [d["u10_band"] for d in ph if d["u10_band"] is not None]
    if band:
        unter = min(d["umin"] for d in ph if d["umin"] is not None)
        if min(band) >= 100 and unter >= U_MIN:
            zeilen.append(("ok", "Spannung", "Alle Werte im Band 207–253 V."))
        elif min(band) >= 95:
            zeilen.append(("warn", "Spannung", f"10-min-Mittel zu {z(min(band))} % im Band (EN 50160: ≥ 95 %), "
                                               f"einzelne Werte bis {z(unter)} V."))
        else:
            zeilen.append(("krit", "Spannung", f"10-min-Mittel nur zu {z(min(band))} % im Band (EN 50160: ≥ 95 %)."))
    res = [d["reserve"] for d in ph if d["reserve"] is not None]
    dauer15 = max((d["i15"] for d in ph if d["i15"] is not None), default=None)
    if res:
        if dauer15 is not None and dauer15 > k.sicherung:
            zeilen.append(("krit", "Lastreserve", f"15-min-Mittel {z(dauer15)} A über dem Sicherungsnennstrom."))
        elif min(res) < 0:
            zeilen.append(("warn", "Lastreserve", f"Kurzzeitige Spitzen bis {z(-min(res), 0)} A über dem Nennstrom "
                                                  f"(Sicherungen halten das kurz aus, Dauerlast max. {z(dauer15)} A)."))
        elif min(res) < 0.2 * k.sicherung:
            zeilen.append(("warn", "Lastreserve", f"Nur noch {z(min(res), 0)} A Reserve auf der höchstbelasteten Phase."))
        elif any(not d["z"] for d in ph):
            zeilen.append(("info", "Lastreserve", f"Mindestens {z(min(res), 0)} A je Phase bis zur Sicherung; ob die "
                                                  "Spannung das zulässt, zeigt erst die Netzimpedanz."))
        else:
            zeilen.append(("ok", "Lastreserve", f"Mindestens {z(min(res), 0)} A je Phase bis zur Sicherung."))
    zs = [d["z"] for d in ph if d["z"]]
    if zs:
        if max(zs) <= Z_REFERENZ:
            zeilen.append(("ok", "Netzimpedanz", f"Bis {z(max(zs), 2)} Ω, steifer als das Referenznetz (0,47 Ω)."))
        else:
            zeilen.append(("warn", "Netzimpedanz", f"Bis {z(max(zs), 2)} Ω, weicher als das Referenznetz (0,47 Ω): "
                                                   "große Lasten drücken die Spannung spürbar."))
    else:
        zeilen.append(("info", "Netzimpedanz", "Noch zu wenige Laststufen; länger messen oder gezielt Last schalten."))
    if r.get("unsym_95") is not None:
        st = "ok" if r["unsym_95"] <= UNSYM_MAX else "warn" if r["unsym_95"] <= 2 * UNSYM_MAX else "krit"
        text = f"95 %-Wert {z(r['unsym_95'], 2)} % (Grenze 2 %)."
        if st == "krit":
            text += " Zusammen mit Über- und Unterspannung zugleich: Neutralleiter prüfen lassen."
        zeilen.append((st, "Unsymmetrie", text))
    zeilen.append(neutral_bewertung(r))
    if r.get("schieflast_max") is not None:
        st = "ok" if r["schieflast_max"] <= SCHIEFLAST_A else "warn"
        zeilen.append((st, "Schieflast", f"Größte Stromdifferenz zwischen den Phasen {z(r['schieflast_max'])} A "
                                         f"(VDE-AR-N 4100: 20 A)."))
    fr = r.get("frequenz")
    if fr:
        if fr["ausserhalb_norm"]:
            zeilen.append(("krit", "Frequenz", f"{fr['ausserhalb_norm']} Sekunden außerhalb 49,5–50,5 Hz."))
        elif fr["ausserhalb_normal"]:
            zeilen.append(("warn", "Frequenz", f"{fr['ausserhalb_normal']} Sekunden außerhalb 49,8–50,2 Hz."))
        else:
            zeilen.append(("ok", "Frequenz", f"{z(fr['min'], 3)}–{z(fr['max'], 3)} Hz, stabil."))
    return zeilen


def eingabe_query(eingabe, modus):
    return f"von={eingabe['von']}&bis={eingabe['bis']}&modus={modus}"


def bericht_html(r, v, k, eingabe, download):
    e = html.escape
    ph = r["phasen"]
    ab = r["abdeckung"]
    t = []
    if not ph:
        t.append('<section class="karte"><p>Für diesen Zeitraum liegen noch keine Daten vor.</p></section>')
    else:
        t.append('<section class="karte"><h2>Bewertung</h2><table class="bewertung"><tbody>')
        for stufe, was, text in bewertung(r, k):
            t.append(f"<tr><th>{e(was)}</th><td>{ampel(stufe, {'ok': 'in Ordnung', 'warn': 'Hinweis', 'krit': 'kritisch', 'info': 'offen'}[stufe])}</td><td>{e(text)}</td></tr>")
        t.append("</tbody></table></section>")

        z3 = r.get("zusatz_3ph_kw")
        ohne_z = any(not d["z"] for d in ph.values())
        kacheln = [("Zusätzliche Last, 3-phasig symmetrisch", z(z3, 1, "kW") if z3 is not None else "–",
                    "nur Sicherung – Spannungsgrenze offen, Netzimpedanz fehlt" if ohne_z
                    else "begrenzt durch Sicherung und Spannung"),
                   ("Größter Spitzenstrom", z(max((d["imax"] or 0) for d in ph.values()), 1, "A"),
                    f"Sicherung {z(k.sicherung, 0)} A"),
                   ("Niedrigste Spannung", z(min((d["umin"] or 999) for d in ph.values()), 1, "V"), "Grenze 207 V")]
        if r.get("frequenz"):
            kacheln.append(("Frequenz min / max", f"{z(r['frequenz']['min'], 3)} / {z(r['frequenz']['max'], 3)}", "Hz"))
        t.append('<section class="kacheln">' + "".join(
            f'<div class="kachel"><div class="k-titel">{e(a)}</div><div class="k-wert">{e(b)}</div>'
            f'<div class="k-sub">{e(c)}</div></div>' for a, b, c in kacheln) + "</section>")

        t.append('<section class="karte"><h2>Lastreserve je Phase</h2><div class="scroll"><table><thead><tr>'
                 "<th>Phase</th><th>I max</th><th>I 99,9 %</th><th>Ø 15 min max</th><th>Zeitpunkt I max</th>"
                 "<th>Reserve Sicherung</th><th>Z Netz L–N</th><th>Zusatz bis 207 V</th><th>Max. Schaltstufe 3 %</th>"
                 "<th>Zusätzlich möglich</th></tr></thead><tbody>")
        for d in ph.values():
            zz = f'{z(d["z"], 3, "Ω")} <span class="mut">({d["z_stufen"]} Stufen)</span>' if d["z"] else \
                f'<span class="mut">– ({d["z_stufen"]} Stufen)</span>'
            t.append(f"<tr><th>{d['name']}</th><td>{z(d['imax'], 1, 'A')}</td><td>{z(d['i999'], 1, 'A')}</td>"
                     f"<td>{z(d['i15'], 1, 'A')}</td><td>{zeit(d['imax_ts'], k)}</td>"
                     f"<td>{z(d['reserve'], 1, 'A')} <span class='mut'>≈ {z((d['reserve'] or 0) * k.nennspannung / 1000, 1, 'kW')}</span></td>"
                     f"<td>{zz}</td><td>{z(d.get('zusatz_spannung'), 0, 'A')}</td><td>{z(d.get('schaltstufe'), 0, 'A')}</td>"
                     f"<td><strong>{z(d['zusatz'], 0, 'A')}</strong> <span class='mut'>≈ {z((d['zusatz'] or 0) * k.nennspannung / 1000, 1, 'kW')}</span></td></tr>")
        t.append("</tbody></table></div><p class='hinweis'>„Zusätzlich möglich“ ist das Minimum aus Reserve bis zur Sicherung "
                 "und dem Strom, bei dem die Spannung zur Zeit des Spitzenstroms auf 207 V fiele (einphasige Last, "
                 "Schleifenimpedanz L–N). Bei symmetrischer Drehstromlast fällt die Spannung etwa halb so stark. "
                 "„Max. Schaltstufe“: größte Last, die beim Einschalten höchstens 3 % Spannungsänderung verursacht.</p></section>")

        t.append('<section class="karte"><h2>Spannung je Phase</h2><div class="scroll"><table><thead><tr>'
                 "<th>Phase</th><th>U min</th><th>U max</th><th>Ø</th><th>10-min-Ø min / max</th><th>10-min-Ø im Band</th>"
                 f"<th>{'Minuten' if r['quelle'] == 'minute' else 'Werte'} &lt; 207 V / &gt; 253 V</th><th>U bei I max</th>"
                 "<th>cos φ (Median &gt; 1 kW)</th></tr></thead><tbody>")
        for d in ph.values():
            t.append(f"<tr><th>{d['name']}</th><td>{z(d['umin'], 1, 'V')}</td><td>{z(d['umax'], 1, 'V')}</td>"
                     f"<td>{z(d['uavg'], 1, 'V')}</td><td>{z(d['u10_min'])} / {z(d['u10_max'], 1, 'V')}</td>"
                     f"<td>{z(d['u10_band'], 2, '%')}</td><td>{d['einheiten_unter']} / {d['einheiten_ueber']}</td>"
                     f"<td>{z(d['u_bei_imax'], 1, 'V')}</td><td>{z(d['pf'], 2)}</td></tr>")
        t.append("</tbody></table></div>")
        weitere = [("Spannungsunsymmetrie (Näherung über Beträge)", f"max {z(r.get('unsym_max'), 2, '%')}, 95 %-Wert {z(r.get('unsym_95'), 2, '%')}"),
                   ("Schieflast (Differenz der Ø-Ströme)", f"max {z(r.get('schieflast_max'), 1, 'A')}, {z(r.get('schieflast_anteil'), 2, '%')} der Zeit über 20 A"),
                   ("Neutralleiterstrom (Shelly)", neutralleiter(r))]
        if r.get("frequenz"):
            fr = r["frequenz"]
            weitere.append(("Frequenz", f"{z(fr['min'], 3)} – {z(fr['max'], 3)} Hz, Ø {z(fr['avg'], 3)} Hz, "
                                        f"Streuung {z(fr['sigma_mhz'], 1)} mHz"))
        if r.get("energie"):
            en = r["energie"]
            weitere.append(("Energie", f"Bezug {z(en['bezug'], 1, 'kWh')}, Einspeisung {z(en['einspeisung'], 1, 'kWh')}, "
                                       f"Blind ind. {z(en['blind_ind'], 1, 'kvarh')}, kap. {z(en['blind_kap'], 1, 'kvarh')}"))
        t.append('<dl class="liste">' + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in weitere) + "</dl></section>")
        t.append(neutral_html(r))

    t.append('<section class="karte"><h2>Verlauf</h2><div id="diagramme"></div>'
             f'<p class="hinweis">Datenbasis Diagramme: {"Sekundenwerte" if v["quelle"] == "sekunde" else "Minutenwerte des Shelly"}, '
             f'verdichtet auf {dauer(v["schritt"])} je Punkt.</p></section>')

    if ph:
        t.append('<section class="karte"><h2>Ereignisse</h2>')
        if r["ereignisse"]:
            t.append('<div class="scroll"><table><thead><tr><th>Beginn</th><th>Dauer</th><th>Art</th><th>Phase</th>'
                     "<th>Extremwert</th></tr></thead><tbody>")
            for ev in r["ereignisse"][:200]:
                einheit = "Hz" if ev["art"] == "Frequenz" else "V"
                t.append(f"<tr><td>{zeit(ev['start'], k, '%d.%m.%Y %H:%M:%S')}</td>"
                         f"<td>{dauer(max(k.intervall, ev['ende'] - ev['start']))}</td><td>{e(ev['art'])}</td>"
                         f"<td>{e(', '.join(ev['phasen']))}</td><td>{z(ev['extrem'], 3 if einheit == 'Hz' else 1, einheit)}</td></tr>")
            t.append("</tbody></table></div>")
            if len(r["ereignisse"]) > 200:
                t.append(f"<p class='hinweis'>Die neuesten 200 von {len(r['ereignisse'])} Ereignissen.</p>")
        else:
            t.append("<p>Keine Spannung außerhalb 207–253 V und keine Frequenz außerhalb 49,8–50,2 Hz.</p>")
        t.append("</section>")

    t.append(f'<section class="karte"><h2>Datenbasis</h2><dl class="liste">'
             f"<dt>Minutenwerte aus dem Shelly</dt><dd>{ab['minuten']} von {ab['minuten_soll']} "
             f"({z(100 * ab['minuten'] / ab['minuten_soll'], 1, '%')})</dd>"
             f"<dt>Sekundenwerte (Raster {k.intervall} s)</dt><dd>{ab['sekunden']} von {ab['sekunden_soll']} "
             f"({z(100 * ab['sekunden'] / ab['sekunden_soll'], 1, '%')})</dd>"
             f"<dt>Hauptquelle Strom/Spannung</dt><dd>{'Minutenwerte (min/max/Ø des Shelly)' if r['quelle'] == 'minute' else 'Sekundenwerte'}</dd>"
             "</dl><p class='hinweis'>Grenzen: EN 50160 (Spannung, Frequenz, Unsymmetrie), VDE-AR-N 4100 "
             "(Schieflast, Spannungsänderung), IEC 60725 (Referenzimpedanz). Der Shelly ist kein Netzqualitäts-"
             "Messgerät nach EN 61000-4-30; Oberschwingungen und Flicker werden nicht erfasst.</p></section>")

    return seite_bauen(t, r, v, k, eingabe, download, "uebersicht")


def tabelle_html(kopf, zeilen, klasse=""):
    e = html.escape
    return (f'<div class="scroll"><table class="{klasse}"><thead><tr>' + "".join(f"<th>{e(h)}</th>" for h in kopf)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in zeile) + "</tr>"
                                                for zeile in zeilen) + "</tbody></table></div>")


def fachbericht_html(r, fd, v, k, eingabe, download):
    """Bericht für die Elektrofachkraft: Rohdaten und Methoden, keine Bewertung."""
    e = html.escape
    ab = r["abdeckung"]
    with STATUS_LOCK:
        geraet = STATUS.get("geraet") or {}
    t = []
    kopf = [("Messpunkt", k.messpunkt or "– (in der Konfiguration unter [anlage] messpunkt eintragen)"),
            ("Messgerät", " · ".join(str(geraet.get(n)) for n in ("model", "app", "ver", "mac") if geraet.get(n)) or "–"),
            ("Zeitraum", f"{zeit(r['von'], k)} – {zeit(r['bis'], k)}"),
            ("Minutenwerte (Shelly-Speicher)", f"{ab['minuten']} von {ab['minuten_soll']} "
                                               f"({z(100 * ab['minuten'] / ab['minuten_soll'], 1, '%')})"),
            ("Sekundenwerte", f"{ab['sekunden']} von {ab['sekunden_soll']} "
                              f"({z(100 * ab['sekunden'] / ab['sekunden_soll'], 1, '%')}), Raster {k.intervall} s"),
            ("Auswertung beruht auf", "Minutenwerten (min/Ø/max des Shelly je Minute)" if r["quelle"] == "minute"
             else "Sekundenwerten (Momentanwerte)"),
            ("Netzform", k.netzform or "–"),
            ("Eingestellt", f"Vorsicherung {z(k.sicherung, 0, 'A')}, Nennspannung {z(k.nennspannung, 0, 'V')}")]
    t.append('<section class="karte"><h2>Messung</h2><dl class="liste">'
             + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in kopf) + "</dl></section>")
    if not fd:
        t.append('<section class="karte"><p>Für diesen Zeitraum liegen keine Daten vor.</p></section>')
        return seite_bauen(t, r, v, k, eingabe, download, "fach")

    # Perzentile
    qn = ["Min", "1 %", "5 %", "Median", "95 %", "99 %", "Max"]
    zeilen = [[p["groesse"], p["phase"], p["einheit"]] + [z(w, 2 if p["einheit"] == "kVA" else 1) for w in p["werte"]]
              for p in fd["perzentile"]]
    t.append('<section class="karte"><h2>Verteilung der Messwerte</h2>'
             + tabelle_html(["Größe", "Phase", "Einheit"] + qn, zeilen, "zahlen")
             + f"<p class='hinweis'>{fd['n']} {'Minuten' if fd['quelle'] == 'minute' else 'Sekundenwerte'}. "
               "U/I Minimum und Maximum sind die Extremwerte innerhalb jeder Minute, „Mittel“ der Minutenmittelwert."
               "</p></section>")

    # 10-Minuten-Statistik
    zeilen = [[d["phase"], d["n"], z(d["min"]), z(d["p5"]), z(d["p95"]), z(d["max"]), z(d["band"], 2),
               d["unter_85"]] for d in fd["zehn_min"]]
    t.append('<section class="karte"><h2>10-Minuten-Mittel der Spannung (EN 50160)</h2>'
             + tabelle_html(["Phase", "Anzahl", "Min [V]", "5 % [V]", "95 % [V]", "Max [V]", "in 207–253 V [%]",
                             f"< {z(0.85 * k.nennspannung, 1)} V (−15 %)"], zeilen, "zahlen")
             + f"<p class='hinweis'>Spannungsunsymmetrie aus 10-min-Mitteln (Näherung über Beträge): max "
               f"{z(r.get('unsym_max'), 2, '%')}, 95 %-Wert {z(r.get('unsym_95'), 2, '%')}.</p></section>")

    # Kopplungsmatrix
    ko, zu = r.get("kopplung"), r.get("zuleitung")
    t.append('<section class="karte"><h2>Kopplung Spannung–Strom (ΔU/ΔI)</h2>')
    if ko:
        zeilen = [[f"ΔU {name}"] + [z(ko["matrix"][i][j], 3) for j in range(3)] + [z(ko["r2"][i], 2)]
                  for i, (_, name) in enumerate(PHASEN)]
        t.append(tabelle_html(["", "je ΔI L1 [Ω]", "je ΔI L2 [Ω]", "je ΔI L3 [Ω]", "R²"], zeilen, "zahlen"))
        abgeleitet = [("Z_N ≈ 2 × Mittel der Nebendiagonale", z(ko["z_n"], 3, "Ω")),
                      ("Z_L ≈ −Diagonale − Z_N", " / ".join(z(w, 3) for w in ko["z_l"]) + " Ω (L1/L2/L3)"),
                      ("Datenbasis", f"{ko['n']} Änderungen zwischen aufeinanderfolgenden Werten "
                                     f"({dauer(ko['raster'])}); Streuung ΔI "
                                     + " / ".join(z(w, 2) for w in ko["streuung_di"]) + " A")]
        t.append('<dl class="liste">' + "".join(f"<dt>{e(a)}</dt><dd>{e(b)}</dd>" for a, b in abgeleitet) + "</dl>")
        t.append("<p class='hinweis'>Mehrfache Regression ΔU(k) = c + Σ M(k,j)·ΔI(j). Ohmsches, linearisiertes Modell: "
                 "Diagonale ≈ −(Z_L + Z_N), Nebendiagonale ≈ +½·Z_N (Sternpunktverschiebung hebt die anderen Phasen). "
                 "Enthalten ist der gesamte Weg bis zur Spannungsquelle (Zuleitung, vorgelagerte UV, Netz). "
                 "Blindanteile und Lastschwankungen anderer Abnehmer gehen als Streuung ein (R²).</p>")
    else:
        t.append("<p>Zu wenige aufeinanderfolgende Werte mit Laständerung.</p>")
    t.append("</section>")

    # Laststufen
    zeilen = [[d["name"], d["z_stufen"], z(d["z"], 3) if d["z"] else "–", z(d.get("schaltstufe"), 0)]
              for d in r["phasen"].values()]
    t.append('<section class="karte"><h2>Impedanz aus Laststufen (Sekundenwerte)</h2>'
             + tabelle_html(["Phase", "Stufen ≥ " + z(k.min_stufe, 0, "A"), "Median −ΔU/ΔI [Ω]",
                             "ΔI für 3 % ΔU [A]"], zeilen, "zahlen")
             + "<p class='hinweis'>Sprünge des Stroms einer Phase zwischen zwei Sekundenwerten, während sich die anderen "
               "Phasen um weniger als die halbe Stufe ändern. Entspricht der Schleifenimpedanz L–N.</p></section>")

    # Zuleitung
    if zu:
        zeilen = [[a["name"] or f"Abschnitt {i + 1}", z(a["laenge_m"], 0), a["material"].capitalize(), z(a["mm2"], 0),
                   z(a["mm2_n"], 0), z(a["r_l"][0], 4), z(a["r_l"][1], 4), z(a["r_n"][0], 4), z(a["r_n"][1], 4)]
                  for i, a in enumerate(zu["abschnitte"])]
        summe = [["Außenleiter R_L", z(zu["r_l"][0], 3), z(zu["r_l"][1], 3)],
                 ["Neutral-/PEN-Leiter R_N", z(zu["r_n"][0], 3), z(zu["r_n"][1], 3)],
                 ["Schleife R_L + R_N", z(zu["schleife"][0], 3), z(zu["schleife"][1], 3)],
                 ["Erwartete Steigung R_N + ⅔·R_L", z(zu["steigung"][0], 3), z(zu["steigung"][1], 3)]]
        t.append('<section class="karte"><h2>Zuleitung zum Messpunkt (aus der Konfiguration)</h2>'
                 + tabelle_html(["Abschnitt", "Länge [m]", "Material", "L [mm²]", "N [mm²]", "R_L 20 °C [Ω]",
                                 "R_L 70 °C [Ω]", "R_N 20 °C [Ω]", "R_N 70 °C [Ω]"], zeilen, "zahlen klein")
                 + tabelle_html(["Summe", "20 °C [Ω]", "70 °C [Ω]"], summe, "zahlen")
                 + f"<p class='hinweis'>Leiterwiderstände nach DIN EN 60228 (Höchstwerte, 20 °C), 70 °C über den "
                   f"Temperaturbeiwert. Nur ohmsch, ohne Hauptverteilung, Hausanschluss und Netz. Reaktanz zusätzlich etwa "
                   f"{z(zu['x_l'], 3)} Ω je Leiter (0,08 Ω/km).</p></section>")

    # Neutralleiter
    ne = r.get("neutral")
    if ne:
        t.append(neutral_html(r))
        zeilen = [[f"{kl['von']}–{kl['bis']}", kl["n"], z(kl["median"]), z(kl["p95"])] for kl in ne["klassen"]]
        t.append('<section class="karte"><h2>Sternpunktverschiebung nach N-Strom-Klassen</h2>'
                 + tabelle_html(["N-Strom [A]", "Anzahl", "Verschiebung Median [V]", "95 % [V]"], zeilen, "zahlen")
                 + "</section>")

    # Extremminuten
    def extrem(zeilen_):
        return [[zeit(x["ts"], k, "%d.%m.%Y %H:%M")] + [z(w) for w in x["umin"]] + [z(w) for w in x["uavg"]]
                + [z(w) for w in x["umax"]] + [z(w) for w in x["iavg"]] + [z(x["n"]), z(x["sp"])] for x in zeilen_]
    kopf_ex = (["Zeit"] + [f"U min {n}" for _, n in PHASEN] + [f"U Ø {n}" for _, n in PHASEN]
               + [f"U max {n}" for _, n in PHASEN] + [f"I Ø {n}" for _, n in PHASEN] + ["I(N) ber.", "Verschiebung"])
    t.append('<section class="karte"><h2>Minuten mit der niedrigsten Spannung</h2>'
             + tabelle_html(kopf_ex, extrem(fd["tiefste"]), "zahlen klein")
             + "<p class='hinweis'>Alle drei Phasen zur selben Minute: steigt eine Phase, während eine andere einbricht, "
               "verschiebt sich der Sternpunkt.</p></section>")
    t.append('<section class="karte"><h2>Minuten mit der höchsten Spannung</h2>'
             + tabelle_html(kopf_ex, extrem(fd["hoechste"]), "zahlen klein") + "</section>")

    # Tagesprofil
    zeilen = [[f"{h['stunde']:02d}:00", h["n"]] + [z(h[n]["uavg"]) for _, n in PHASEN]
              + [z(h[n]["umin"]) for _, n in PHASEN] + [z(h[n]["iavg"]) for _, n in PHASEN]
              for h in fd["tagesprofil"]]
    t.append('<section class="karte"><h2>Tagesprofil (Ortszeit)</h2>'
             + tabelle_html(["Stunde", "Werte"] + [f"U Ø {n}" for _, n in PHASEN] + [f"U min {n}" for _, n in PHASEN]
                            + [f"I Ø {n}" for _, n in PHASEN], zeilen, "zahlen klein") + "</section>")

    t.append('<section class="karte"><h2>Verlauf</h2><div id="diagramme"></div>'
             f'<p class="hinweis">Datenbasis: {"Sekundenwerte" if v["quelle"] == "sekunde" else "Minutenwerte"}, '
             f'verdichtet auf {dauer(v["schritt"])} je Punkt (Mittelwert, Minimum bzw. Maximum je Punkt).</p></section>')

    # Ereignisse ungekürzt als Rohliste
    zeilen = [[zeit(ev["start"], k, "%d.%m.%Y %H:%M:%S"), dauer(max(k.intervall, ev["ende"] - ev["start"])), ev["art"],
               ", ".join(ev["phasen"]), z(ev["extrem"], 3 if ev["art"] == "Frequenz" else 1)] for ev in r["ereignisse"]]
    t.append('<section class="karte"><h2>Grenzwertverletzungen</h2>'
             + (tabelle_html(["Beginn", "Dauer", "Art", "Phase", "Extremwert"], zeilen, "zahlen klein") if zeilen
                else "<p>Keine.</p>")
             + "<p class='hinweis'>Minuten mit U min &lt; 207 V bzw. U max &gt; 253 V, getrennt nach Art; "
               "aufeinanderfolgende Minuten zusammengefasst.</p></section>")

    t.append("<section class='karte'><h2>Methoden und Annahmen</h2><ul class='methoden'>"
             "<li>Messgerät Shelly Pro 3EM, kein Netzqualitäts-Messgerät nach EN 61000-4-30. Keine Oberschwingungen, kein "
             "Flicker, kein Neutralleiterstrom, keine Spannung N–PE, keine Außenleiterspannungen.</li>"
             "<li>N-Strom: Zeigersumme der Außenleiterströme, Winkel aus Wirk- und Blindenergie je Minute "
             "(Rechtsdrehfeld angenommen). Nur Grundschwingung.</li>"
             "<li>Sternpunktverschiebung: aus den drei Spannungen L–N unter der Annahme gleich großer "
             "Außenleiterspannungen (3(a⁴+b⁴+c⁴+s⁴) = (a²+b²+c²+s²)²).</li>"
             "<li>Kopplungsmatrix: Regression der Änderungen aufeinanderfolgender Mittelwerte; ohmsches, linearisiertes "
             "Modell.</li>"
             "<li>Unsymmetrie: (größte Abweichung vom Mittel) / Mittel der 10-min-Mittelwerte, ohne Phasenwinkel.</li>"
             + ("<li>TT-System: N und PE sind in der Anlage getrennt. Eine Messung N–PE an der Unterverteilung unter "
                "Schieflast zeigt die Verschiebung des N-Potenzials gegen den örtlichen Erder direkt und ergänzt die "
                "berechnete Sternpunktverschiebung.</li>" if k.netzform == "TT" else "") +
             "</ul><p class='hinweis'>Rohdaten: CSV-Export oben (Minuten- und Sekundenwerte, Spalten wie vom Shelly "
             "geliefert, berechnete Größen mit Präfix calc_).</p></section>")
    return seite_bauen(t, r, v, k, eingabe, download, "fach")


def seite_bauen(t, r, v, k, eingabe, download, modus):
    e = html.escape
    with STATUS_LOCK:
        st = dict(STATUS)
    ne = r.get("neutral") or {}
    daten = json.dumps({"verlauf": v, "von": r["von"], "bis": r["bis"], "sicherung": k.sicherung,
                        "zeitzone": str(k.zeitzone), "live": not download, "streuung": ne.get("punkte"),
                        "fit": ne.get("fit")}, separators=(",", ":"))
    ersetzen = {
        "__TITEL__": e(k.name),
        "__ART__": "Fachbericht" if modus == "fach" else "Netzbericht",
        "__MESSPUNKT__": e(k.messpunkt) if k.messpunkt else "",
        "__MODUS__": modus,
        "__UMSCHALTEN__": (f'<a class="knopf" href="?{e(eingabe_query(eingabe, "uebersicht"))}">Übersicht</a>'
                           if modus == "fach" else
                           f'<a class="knopf" href="?{e(eingabe_query(eingabe, "fach"))}">Fachkraft-Ansicht</a>'),
        "__ZEITRAUM__": f"{zeit(r['von'], k)} – {zeit(r['bis'], k)}",
        "__ERSTELLT__": datetime.now(k.zeitzone).strftime("%d.%m.%Y %H:%M"),
        "__VON__": e(eingabe["von"]), "__BIS__": e(eingabe["bis"]),
        "__SICHERUNG__": z(k.sicherung, 0, "A"),
        "__STATUS__": e(f"Letzte Messung {zeit(st['letzte_messung'], k, '%H:%M:%S')}, letzter Abgleich "
                        f"{zeit(st['letzter_abgleich'], k, '%H:%M')}, Fehler seit Start {st['fehler_seit_start']}"
                        + (f" – {st['letzter_fehler']}" if st["letzter_fehler"] else "")),
        "__QUERY__": e(eingabe_query(eingabe, modus)),
        "__STEUERUNG__": "" if not download else "versteckt",
        "__ALARM__": "" if download else alarm_banner(st, k),
        "__INHALT__": "\n".join(t),
        "__DATEN__": daten.replace("</", "<\\/"),
    }
    seite = SEITE
    for a, b in ersetzen.items():
        seite = seite.replace(a, b)
    return seite


SEITE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__ART__ __TITEL__</title>
<style>
:root{color-scheme:light;--bg:#f4f4f2;--surface:#fcfcfb;--line:#e4e3df;--grid:#ecebe7;--text:#0b0b0b;--text2:#52514e;--muted:#7a7975;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--ref:#9a9994;--ok:#008300;--warn:#b87900;--krit:#d03b3b;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#2e2e2c;--grid:#262624;
--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e88;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--ref:#6f6e69;--ok:#3fae3f;--warn:#d9a227;--krit:#e66767;--accent:#3987e5}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#2e2e2c;--grid:#262624;
--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e88;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--ref:#6f6e69;--ok:#3fae3f;--warn:#d9a227;--krit:#e66767;--accent:#3987e5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
header h1{font-size:22px;margin:0 0 2px}
header p{margin:0;color:var(--text2)}
.mut,.hinweis{color:var(--muted)}
.hinweis{font-size:13px;margin:10px 0 0}
.karte{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:16px}
.karte h2{font-size:16px;margin:0 0 12px}
form.steuerung{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:end;margin-top:16px}
form.steuerung label{display:flex;flex-direction:column;font-size:13px;color:var(--text2);gap:3px}
input,button,.knopf{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:6px 10px;text-decoration:none}
button.primaer{background:var(--accent);border-color:var(--accent);color:#fff}
.knoepfe{display:flex;flex-wrap:wrap;gap:6px}
.versteckt{display:none!important}
.kacheln{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}
.kachel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px}
.k-titel{font-size:13px;color:var(--text2)}.k-wert{font-size:26px;font-weight:600;margin:4px 0 2px;font-variant-numeric:tabular-nums}.k-sub{font-size:12px;color:var(--muted)}
.live{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;font-variant-numeric:tabular-nums}
.live div{border-left:3px solid var(--line);padding:2px 8px}.live b{display:block;font-size:13px;color:var(--text2);font-weight:500}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
thead th{font-size:12px;color:var(--text2);font-weight:500}
table.bewertung td:last-child{white-space:normal}
.chip{display:inline-flex;gap:5px;align-items:center;font-size:13px;font-weight:600;padding:1px 8px;border-radius:99px;border:1px solid currentColor}
.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}.chip.krit{color:var(--krit)}.chip.info{color:var(--muted)}
dl.liste{display:grid;grid-template-columns:minmax(180px,max-content) 1fr;gap:6px 18px;margin:14px 0 0}
dl.liste dt{color:var(--text2)}dl.liste dd{margin:0}
.diagramm{position:relative;margin:4px 0 22px}
.diagramm h3{font-size:14px;margin:0 0 4px;font-weight:600}
.legende{display:flex;gap:14px;font-size:12px;color:var(--text2);margin-bottom:4px;flex-wrap:wrap}
.legende span{display:inline-flex;align-items:center;gap:6px}.legende i{display:inline-block;width:14px;height:2px;border-radius:2px}
.diagramm svg{display:block;width:100%;height:auto;overflow:visible;touch-action:pan-y}
.diagramm text{fill:var(--muted);font-size:11px}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:6px 9px;font-size:12px;
box-shadow:0 2px 10px rgba(0,0,0,.12);white-space:nowrap;font-variant-numeric:tabular-nums;display:none;z-index:2}
.tip .z{color:var(--text2);margin-bottom:3px}.tip div{display:flex;gap:6px;align-items:center}.tip i{width:12px;height:2px;display:inline-block}
table.zahlen td{text-align:right}table.zahlen td:first-child{text-align:left}
table.klein{font-size:12.5px}table.klein th,table.klein td{padding:5px 8px 5px 0}
.messpunkt:empty{display:none}.messpunkt{margin:2px 0 4px!important;color:var(--text)!important}
ul.methoden{margin:0;padding-left:18px}ul.methoden li{margin:4px 0}
@media (max-width:600px){dl.liste{grid-template-columns:1fr}dl.liste dd{margin-bottom:6px}}
.alarm{border:2px solid currentColor;border-radius:10px;padding:10px 14px;margin-bottom:14px;background:var(--surface)}
.alarm.krit{color:var(--krit)}.alarm.warn{color:var(--warn)}.alarm ul{margin:4px 0;padding-left:18px;color:var(--text)}
</style></head><body><main>
__ALARM__<header><h1>__ART__ __TITEL__</h1><p class="messpunkt">__MESSPUNKT__</p><p>__ZEITRAUM__ · Sicherung __SICHERUNG__ · erstellt __ERSTELLT__</p>
<p class="hinweis __STEUERUNG__">__STATUS__</p></header>
<form class="steuerung __STEUERUNG__" method="get" action="./">
<label>Von<input type="datetime-local" name="von" value="__VON__"></label>
<label>Bis<input type="datetime-local" name="bis" value="__BIS__"></label>
<input type="hidden" name="modus" value="__MODUS__">
<button class="primaer" type="submit">Bericht erstellen</button>
<div class="knoepfe">
<a class="knopf" href="?zeitraum=1h&modus=__MODUS__">1 h</a><a class="knopf" href="?zeitraum=24h&modus=__MODUS__">24 h</a><a class="knopf" href="?zeitraum=7d&modus=__MODUS__">7 Tage</a>
<a class="knopf" href="?zeitraum=30d&modus=__MODUS__">30 Tage</a><a class="knopf" href="?zeitraum=alles&modus=__MODUS__">Alles</a></div>
<div class="knoepfe">__UMSCHALTEN__</div>
<div class="knoepfe"><a class="knopf" href="?__QUERY__&download=1">Als HTML speichern</a>
<a class="knopf" href="export.csv?tabelle=minute&__QUERY__">CSV Minuten</a><a class="knopf" href="export.csv?tabelle=sekunde&__QUERY__">CSV Sekunden</a></div>
</form>
<section class="karte __STEUERUNG__" id="live-karte"><h2>Jetzt</h2><div class="live" id="live"><span class="mut">wird geladen …</span></div></section>
__INHALT__
</main>
<script id="daten" type="application/json">__DATEN__</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('daten').textContent);
const V=D.verlauf, TZ=D.zeitzone, PH=['L1','L2','L3'], FARBE={L1:'var(--s1)',L2:'var(--s2)',L3:'var(--s3)'};
const NS='http://www.w3.org/2000/svg';
const zahl=(x,n)=>x==null?'–':x.toLocaleString('de-DE',{minimumFractionDigits:n,maximumFractionDigits:n});
const fmt=o=>new Intl.DateTimeFormat('de-DE',Object.assign({timeZone:TZ},o));
const fZeit=fmt({hour:'2-digit',minute:'2-digit'}), fTag=fmt({day:'2-digit',month:'2-digit'}),
      fVoll=fmt({day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
function el(n,a,p){const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function tzOffset(t){const p={};fmt({year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'})
  .formatToParts(new Date(t*1000)).forEach(x=>p[x.type]=x.value);
  return (Date.UTC(+p.year,p.month-1,+p.day,+p.hour,+p.minute,+p.second)/1000)-t}
function niceTicks(lo,hi,n){const r=(hi-lo)||1,s0=r/n,m=Math.pow(10,Math.floor(Math.log10(s0))),
  s=[1,2,2.5,5,10].map(x=>x*m).find(x=>x>=s0),a=[];for(let v=Math.ceil(lo/s)*s;v<=hi+1e-9;v+=s)a.push(+v.toFixed(6));return{a,s}}
function diagramm(ziel,c){
  const box=document.createElement('div');box.className='diagramm';ziel.appendChild(box);
  const h=document.createElement('h3');h.textContent=c.titel;box.appendChild(h);
  if(c.reihen.length>1){const l=document.createElement('div');l.className='legende';
    c.reihen.forEach(r=>{const s=document.createElement('span'),i=document.createElement('i');i.style.background=r.farbe;s.appendChild(i);
      s.appendChild(document.createTextNode(r.name));l.appendChild(s)});box.appendChild(l)}
  const tip=document.createElement('div');tip.className='tip';box.appendChild(tip);
  const t=c.t;if(!t.length){const p=document.createElement('p');p.className='mut';p.textContent='Keine Daten im Zeitraum.';box.appendChild(p);return}
  function zeichnen(){
    box.querySelectorAll('svg').forEach(s=>s.remove());
    const W=Math.max(300,box.clientWidth),H=210,m={l:44,r:10,t:8,b:22},iw=W-m.l-m.r,ih=H-m.t-m.b;
    let lo=Infinity,hi=-Infinity;
    c.reihen.concat(c.band?[{werte:c.band.min},{werte:c.band.max}]:[]).forEach(r=>r.werte.forEach(v=>{if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v)}}));
    (c.refs||[]).forEach(r=>{if(r.immer||(r.wert>=lo-(hi-lo)*0.6&&r.wert<=hi+(hi-lo)*0.6)){lo=Math.min(lo,r.wert);hi=Math.max(hi,r.wert)}});
    if(c.null)lo=Math.min(lo,0);
    const pad=(hi-lo)*0.06||1;lo-=pad;hi+=pad;
    const yt=niceTicks(lo,hi,4);
    const t0=D.von,t1=D.bis,x=v=>m.l+(v-t0)/((t1-t0)||1)*iw,y=v=>m.t+(1-(v-lo)/(hi-lo))*ih;
    const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':c.titel});box.insertBefore(svg,tip);
    yt.a.forEach(v=>{el('line',{x1:m.l,x2:W-m.r,y1:y(v),y2:y(v),stroke:'var(--grid)'},svg);
      const tx=el('text',{x:m.l-6,y:y(v)+4,'text-anchor':'end'},svg);tx.textContent=zahl(v,c.nk)});
    const spanne=t1-t0,stufen=[60,300,900,1800,3600,7200,10800,21600,43200,86400,172800,604800,1209600,2592000],
      st=stufen.find(s=>spanne/s<=Math.max(3,Math.floor(iw/90)))||2592000,off=tzOffset(t0);
    for(let v=Math.ceil((t0+off)/st)*st-off;v<=t1;v+=st){
      el('line',{x1:x(v),x2:x(v),y1:m.t+ih,y2:m.t+ih+4,stroke:'var(--line)'},svg);
      const tx=el('text',{x:x(v),y:H-6,'text-anchor':'middle'},svg);const d=new Date(v*1000);
      tx.textContent=st>=86400?fTag.format(d):(((v+off)%86400===0)?fTag.format(d):fZeit.format(d))}
    el('line',{x1:m.l,x2:W-m.r,y1:m.t+ih,y2:m.t+ih,stroke:'var(--line)'},svg);
    (c.refs||[]).forEach(r=>{if(r.wert<lo||r.wert>hi)return;el('line',{x1:m.l,x2:W-m.r,y1:y(r.wert),y2:y(r.wert),stroke:'var(--ref)','stroke-dasharray':'4 4'},svg);
      const tx=el('text',{x:W-m.r,y:y(r.wert)-4,'text-anchor':'end'},svg);tx.textContent=r.text});
    const luecke=c.schritt*2.5;
    function pfad(werte,farbe){let d='',seg=[];const ende=()=>{if(seg.length===1)el('circle',{cx:x(t[seg[0]]+c.schritt/2),
        cy:y(werte[seg[0]]),r:2.5,fill:farbe},svg);seg=[]};
      for(let i=0;i<t.length;i++){const v=werte[i];if(v==null||(i&&t[i]-t[i-1]>luecke))ende();if(v==null)continue;
        d+=(seg.length?'L':'M')+x(t[i]+c.schritt/2).toFixed(1)+','+y(v).toFixed(1);seg.push(i)}ende();return d}
    if(c.band){let d='',seg=[];const flush=()=>{if(seg.length){d+='M'+seg.map(i=>x(t[i]+c.schritt/2).toFixed(1)+','+y(c.band.max[i]).toFixed(1)).join('L')
      +'L'+seg.slice().reverse().map(i=>x(t[i]+c.schritt/2).toFixed(1)+','+y(c.band.min[i]).toFixed(1)).join('L')+'Z'}seg=[]};
      for(let i=0;i<t.length;i++){if(c.band.min[i]==null||(i&&t[i]-t[i-1]>luecke))flush();if(c.band.min[i]!=null)seg.push(i)}flush();
      el('path',{d,fill:c.band.farbe,'fill-opacity':0.18,stroke:'none'},svg)}
    c.reihen.forEach(r=>el('path',{d:pfad(r.werte,r.farbe),fill:'none',stroke:r.farbe,'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'},svg));
    const hair=el('line',{y1:m.t,y2:m.t+ih,stroke:'var(--text2)','stroke-width':1,visibility:'hidden'},svg);
    const punkte=c.reihen.map(r=>el('circle',{r:4,fill:r.farbe,stroke:'var(--surface)','stroke-width':2,visibility:'hidden'},svg));
    const hit=el('rect',{x:m.l,y:m.t,width:iw,height:ih,fill:'transparent'},svg);
    function zeigen(ev){const b=svg.getBoundingClientRect(),px=(ev.clientX-b.left)*W/b.width,tv=t0+(px-m.l)/iw*(t1-t0);
      let lo2=0,hi2=t.length-1;while(hi2-lo2>1){const mid=(lo2+hi2)>>1;if(t[mid]+c.schritt/2<tv)lo2=mid;else hi2=mid}
      const i=Math.abs(t[lo2]+c.schritt/2-tv)<Math.abs(t[hi2]+c.schritt/2-tv)?lo2:hi2,xx=x(t[i]+c.schritt/2);
      hair.setAttribute('x1',xx);hair.setAttribute('x2',xx);hair.setAttribute('visibility','visible');
      tip.replaceChildren();const kopf=document.createElement('div');kopf.className='z';
      kopf.textContent=fVoll.format(new Date(t[i]*1000))+(c.schritt>c.raster?' (+'+(c.schritt>=3600?zahl(c.schritt/3600,1)+' h':c.schritt>=60?Math.round(c.schritt/60)+' min':c.schritt+' s')+')':'');tip.appendChild(kopf);
      c.reihen.forEach((r,j)=>{const v=r.werte[i];if(v==null){punkte[j].setAttribute('visibility','hidden')}else{
        punkte[j].setAttribute('cx',xx);punkte[j].setAttribute('cy',y(v));punkte[j].setAttribute('visibility','visible')}
        const z=document.createElement('div'),k=document.createElement('i'),s=document.createElement('strong');k.style.background=r.farbe;
        s.textContent=zahl(v,c.nk)+' '+c.einheit;z.append(k,s,document.createTextNode(' '+r.name));tip.appendChild(z)});
      if(c.zusatz)c.zusatz(i).forEach(text=>{const z=document.createElement('div');z.className='z';z.textContent=text;tip.appendChild(z)});
      tip.style.display='block';const tb=tip.getBoundingClientRect(),bb=box.getBoundingClientRect(),sx=b.left-bb.left+xx*b.width/W;
      tip.style.left=Math.max(0,Math.min(bb.width-tb.width,sx+(sx+tb.width+16>bb.width?-tb.width-12:12)))+'px';
      tip.style.top=(b.top-bb.top+8)+'px'}
    function weg(){tip.style.display='none';hair.setAttribute('visibility','hidden');punkte.forEach(p=>p.setAttribute('visibility','hidden'))}
    hit.addEventListener('pointermove',zeigen);hit.addEventListener('pointerdown',zeigen);hit.addEventListener('pointerleave',weg)}
  zeichnen();let rt;new ResizeObserver(()=>{clearTimeout(rt);rt=setTimeout(zeichnen,120)}).observe(box)}
function streu(ziel,c){
  const box=document.createElement('div');box.className='diagramm';ziel.appendChild(box);
  const h=document.createElement('h3');h.textContent=c.titel;box.appendChild(h);
  const l=document.createElement('div');l.className='legende';
  [['Messwerte','var(--s1)'],['Ausgleichsgerade','var(--s2)']].forEach(([n,f])=>{if(n==='Ausgleichsgerade'&&!c.fit)return;
    const sp=document.createElement('span'),i=document.createElement('i');i.style.background=f;sp.append(i,document.createTextNode(n));l.appendChild(sp)});
  box.appendChild(l);
  const tip=document.createElement('div');tip.className='tip';box.appendChild(tip);
  function zeichnen(){
    box.querySelectorAll('svg').forEach(s=>s.remove());
    const W=Math.max(300,box.clientWidth),H=260,m={l:44,r:12,t:8,b:30},iw=W-m.l-m.r,ih=H-m.t-m.b;
    const xhi=Math.max(1,...c.x)*1.05,yhi=Math.max(1,...c.y)*1.08,x=v=>m.l+v/xhi*iw,y=v=>m.t+(1-v/yhi)*ih;
    const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':c.titel});box.insertBefore(svg,tip);
    niceTicks(0,yhi,4).a.forEach(v=>{el('line',{x1:m.l,x2:W-m.r,y1:y(v),y2:y(v),stroke:'var(--grid)'},svg);
      const tx=el('text',{x:m.l-6,y:y(v)+4,'text-anchor':'end'},svg);tx.textContent=zahl(v,1)});
    niceTicks(0,xhi,6).a.forEach(v=>{el('line',{x1:x(v),x2:x(v),y1:m.t+ih,y2:m.t+ih+4,stroke:'var(--line)'},svg);
      const tx=el('text',{x:x(v),y:m.t+ih+16,'text-anchor':'middle'},svg);tx.textContent=zahl(v,0)});
    const ax=el('text',{x:W-m.r,y:H-2,'text-anchor':'end'},svg);ax.textContent=c.xtitel;
    el('line',{x1:m.l,x2:W-m.r,y1:m.t+ih,y2:m.t+ih,stroke:'var(--line)'},svg);
    const g=el('g',{fill:'var(--s1)','fill-opacity':0.45},svg);
    c.x.forEach((v,i)=>el('circle',{cx:x(v).toFixed(1),cy:y(c.y[i]).toFixed(1),r:2.5},g));
    if(c.fit){const x2=xhi/1.05;el('line',{x1:x(0),y1:y(c.fit.u0),x2:x(x2),y2:y(c.fit.u0+c.fit.z*x2),stroke:'var(--s2)','stroke-width':2,'stroke-linecap':'round'},svg);
      const tx=el('text',{x:x(x2)-4,y:y(c.fit.u0+c.fit.z*x2)-6,'text-anchor':'end'},svg);tx.textContent='Steigung '+zahl(c.fit.z,3)+' Ω'}
    const mark=el('circle',{r:5,fill:'var(--s1)',stroke:'var(--surface)','stroke-width':2,visibility:'hidden'},svg);
    const hit=el('rect',{x:m.l,y:m.t,width:iw,height:ih,fill:'transparent'},svg);
    function zeigen(ev){const b=svg.getBoundingClientRect(),px=(ev.clientX-b.left)*W/b.width,py=(ev.clientY-b.top)*H/b.height;
      let best=-1,bd=Infinity;c.x.forEach((v,i)=>{const d=(x(v)-px)**2+(y(c.y[i])-py)**2;if(d<bd){bd=d;best=i}});
      if(best<0||bd>400){weg();return}
      mark.setAttribute('cx',x(c.x[best]));mark.setAttribute('cy',y(c.y[best]));mark.setAttribute('visibility','visible');
      tip.replaceChildren();const k=document.createElement('div');k.className='z';k.textContent=fVoll.format(new Date(c.t[best]*1000));tip.appendChild(k);
      [[zahl(c.x[best],1)+' A','N-Strom'],[zahl(c.y[best],1)+' V','Verschiebung']].forEach(([w,n])=>{const d=document.createElement('div'),s=document.createElement('strong');
        s.textContent=w;d.append(s,document.createTextNode(' '+n));tip.appendChild(d)});
      tip.style.display='block';const tb=tip.getBoundingClientRect(),bb=box.getBoundingClientRect(),sx=b.left-bb.left+x(c.x[best])*b.width/W;
      tip.style.left=Math.max(0,Math.min(bb.width-tb.width,sx+(sx+tb.width+16>bb.width?-tb.width-12:12)))+'px';
      tip.style.top=(b.top-bb.top+8)+'px'}
    function weg(){tip.style.display='none';mark.setAttribute('visibility','hidden')}
    hit.addEventListener('pointermove',zeigen);hit.addEventListener('pointerdown',zeigen);hit.addEventListener('pointerleave',weg)}
  zeichnen();let rt;new ResizeObserver(()=>{clearTimeout(rt);rt=setTimeout(zeichnen,120)}).observe(box)}
const zs=document.getElementById('streuung');
if(zs&&D.streuung&&D.streuung.x.length)streu(zs,{titel:'Sternpunktverschiebung über berechnetem N-Strom',xtitel:'N-Strom [A]',
  x:D.streuung.x,y:D.streuung.y,t:D.streuung.t,fit:D.fit});
const ziel=document.getElementById('diagramme');
if(ziel){const raster=V.quelle==='minute'?60:1,ph=n=>PH.map(p=>({name:p,farbe:FARBE[p],werte:V[n][p]}));
  const uZusatz=i=>PH.map(p=>p+': min '+zahl(V.umin[p][i],1)+' / max '+zahl(V.umax[p][i],1)+' V');
  diagramm(ziel,{titel:'Spannung je Phase, Mittelwert',einheit:'V',nk:1,t:V.t,schritt:V.schritt,raster,reihen:ph('u'),
    refs:[{wert:207,text:'207 V'},{wert:253,text:'253 V'},{wert:230,text:'230 V'}],zusatz:uZusatz});
  diagramm(ziel,{titel:'Spannung je Phase, Minimum (Einbrüche)',einheit:'V',nk:1,t:V.t,schritt:V.schritt,raster,reihen:ph('umin'),
    refs:[{wert:207,text:'207 V',immer:true}]});
  diagramm(ziel,{titel:'Strom je Phase, Maximum',einheit:'A',nk:1,t:V.t,schritt:V.schritt,raster,reihen:ph('i'),null:true,
    refs:[{wert:D.sicherung,text:'Sicherung '+zahl(D.sicherung,0)+' A',immer:true}]});
  diagramm(ziel,{titel:'Scheinleistung gesamt, Maximum',einheit:'kVA',nk:2,t:V.t,schritt:V.schritt,raster,null:true,
    reihen:[{name:'Gesamt',farbe:'var(--s1)',werte:V.s}]});
  if(V.f.t.length)diagramm(ziel,{titel:'Netzfrequenz (Linie Mittelwert, Fläche min–max)',einheit:'Hz',nk:3,t:V.f.t,schritt:V.f.schritt,raster:1,
    reihen:[{name:'Mittelwert',farbe:'var(--s1)',werte:V.f.avg}],band:{min:V.f.min,max:V.f.max,farbe:'var(--s1)'},
    refs:[{wert:50,text:'50 Hz',immer:true},{wert:49.8,text:'49,8 Hz'},{wert:50.2,text:'50,2 Hz'}],
    zusatz:i=>['min '+zahl(V.f.min[i],3)+' / max '+zahl(V.f.max[i],3)+' Hz']});
  if(V.n.some(x=>x!=null)){
    diagramm(ziel,{titel:'Neutralleiterstrom, berechnet (Grundschwingung, Maximum)',einheit:'A',nk:1,t:V.t,schritt:V.schritt,raster,null:true,
      reihen:[{name:'N',farbe:'var(--s1)',werte:V.n}]});
    diagramm(ziel,{titel:'Sternpunktverschiebung (Maximum)',einheit:'V',nk:1,t:V.t,schritt:V.schritt,raster,null:true,
      reihen:[{name:'Verschiebung',farbe:'var(--s1)',werte:V.sp}]})}}
if(D.live){const box=document.getElementById('live');
  async function holen(){try{const r=await fetch('api/live',{cache:'no-store'});const d=await r.json();if(!d)return;box.replaceChildren();
    const feld=(titel,text,farbe)=>{const e=document.createElement('div'),b=document.createElement('b');if(farbe)e.style.borderColor=farbe;
      b.textContent=titel;e.append(b,document.createTextNode(text));box.appendChild(e)};
    [['a','L1'],['b','L2'],['c','L3']].forEach(([p,n])=>feld(n,zahl(d[p+'_voltage'],1)+' V · '+zahl(d[p+'_current'],2)+' A · '+zahl(d[p+'_act_power']/1000,2)+' kW',FARBE[n]));
    feld('Gesamt',zahl(d.total_act_power/1000,2)+' kW · '+zahl(d.total_aprt_power/1000,2)+' kVA');
    feld('Frequenz',zahl(d.a_freq??d.b_freq??d.c_freq,3)+' Hz');
    feld('Stand',fVoll.format(new Date(d.ts*1000)))}catch(e){}}
  holen();setInterval(holen,2000)}
})();
</script></body></html>"""


# --------------------------------------------------------------------------- Webserver

def zeitraum(params, con, k):
    jetzt = int(time.time())
    p = lambda n: (params.get(n) or [""])[0]
    wahl = p("zeitraum")
    spannen = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    if wahl in spannen:
        von, bis = jetzt - spannen[wahl], jetzt
    elif wahl == "alles":
        erste = [con.execute(f"SELECT MIN(ts) FROM {t}").fetchone()[0] for t in ("minute", "sekunde")]
        von, bis = min([e for e in erste if e is not None], default=jetzt - 86400), jetzt
    else:
        def lesen(text, standard):
            try:
                return int(datetime.fromisoformat(text).replace(tzinfo=k.zeitzone).timestamp())
            except ValueError:
                return standard
        von, bis = lesen(p("von"), jetzt - 86400), lesen(p("bis"), jetzt)
    if bis <= von:
        von, bis = bis - 3600, bis
    if p("sicherung"):
        try:
            k.sicherung = float(p("sicherung").replace(",", "."))
        except ValueError:
            pass
    form = lambda ts: datetime.fromtimestamp(ts, k.zeitzone).strftime("%Y-%m-%dT%H:%M")
    return von, bis, {"von": form(von), "bis": form(bis)}


def webserver(k):
    class Handler(BaseHTTPRequestHandler):
        server_version = "shelly-monitor"

        def log_message(self, fmt, *args):
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def senden(self, code, typ, inhalt, kopf=None):
            daten = inhalt.encode("utf-8") if isinstance(inhalt, str) else inhalt
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(daten)))
            self.send_header("Cache-Control", "no-store")
            for a, b in (kopf or {}).items():
                self.send_header(a, b)
            self.end_headers()
            self.wfile.write(daten)

        def angemeldet(self):
            if not k.web_passwort:
                return True
            erwartet = base64.b64encode(f"{k.web_benutzer}:{k.web_passwort}".encode()).decode()
            if self.headers.get("Authorization") == f"Basic {erwartet}":
                return True
            self.senden(401, "text/plain; charset=utf-8", "Anmeldung erforderlich",
                        {"WWW-Authenticate": 'Basic realm="shelly-monitor"'})
            return False

        def do_GET(self):
            if not self.angemeldet():
                return
            url = urlparse(self.path)
            params = parse_qs(url.query)
            pfad = url.path.rstrip("/") or "/"
            try:
                if pfad == "/api/live":
                    with STATUS_LOCK:
                        live = STATUS["live"]
                    return self.senden(200, "application/json", json.dumps(live))
                if pfad == "/api/alarme":
                    con = verbinden(k.datenbank)
                    try:
                        with STATUS_LOCK:
                            aktiv = STATUS.get("alarme_aktiv") or []
                        return self.senden(200, "application/json",
                                           json.dumps({"aktiv": aktiv, "verlauf": alarme(con)}))
                    finally:
                        con.close()
                if pfad == "/api/status":
                    with STATUS_LOCK:
                        return self.senden(200, "application/json", json.dumps(STATUS, default=str))
                con = verbinden(k.datenbank)
                try:
                    kk = Konfig.__new__(Konfig)
                    kk.__dict__.update(k.__dict__)
                    von, bis, eingabe = zeitraum(params, con, kk)
                    if pfad == "/api/bericht":
                        return self.senden(200, "application/json",
                                           json.dumps(bericht(con, von, bis, kk), default=sorted))
                    if pfad == "/api/fachdaten":
                        r = bericht(con, von, bis, kk)
                        return self.senden(200, "application/json", json.dumps(
                            {"bericht": r, "fachdaten": fachdaten(con, von, bis, kk, r["quelle"])}, default=sorted))
                    if pfad == "/api/verlauf":
                        return self.senden(200, "application/json", json.dumps(verlauf(con, von, bis, kk)))
                    if pfad == "/export.csv":
                        return self.export(con, von, bis, kk, (params.get("tabelle") or ["minute"])[0])
                    if pfad == "/alarme":
                        r = {"von": von, "bis": bis, "neutral": None}
                        seite = seite_bauen(alarme_html(alarme(con), kk), r, [], kk, eingabe, False, "uebersicht")
                        return self.senden(200, "text/html; charset=utf-8", seite)
                    if pfad == "/":
                        download = bool(params.get("download"))
                        fach = (params.get("modus") or [""])[0] == "fach"
                        r = bericht(con, von, bis, kk)
                        v = verlauf(con, von, bis, kk)
                        if fach:
                            seite = fachbericht_html(r, fachdaten(con, von, bis, kk, r["quelle"]), v, kk, eingabe, download)
                        else:
                            seite = bericht_html(r, v, kk, eingabe, download)
                        kopf = {}
                        if download:
                            name = f"{'fachbericht' if fach else 'netzbericht'}_" \
                                   f"{datetime.fromtimestamp(von, kk.zeitzone):%Y%m%d-%H%M}_" \
                                   f"{datetime.fromtimestamp(bis, kk.zeitzone):%Y%m%d-%H%M}.html"
                            kopf["Content-Disposition"] = f'attachment; filename="{name}"'
                        return self.senden(200, "text/html; charset=utf-8", seite, kopf)
                finally:
                    con.close()
                self.senden(404, "text/plain; charset=utf-8", "Nicht gefunden")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                LOG.exception("Fehler bei %s", self.path)
                self.senden(500, "text/plain; charset=utf-8", f"Fehler: {e}")

        def export(self, con, von, bis, kk, tabelle):
            if tabelle not in ("minute", "sekunde"):
                return self.senden(400, "text/plain; charset=utf-8", "tabelle=minute oder sekunde")
            namen = spalten(con, tabelle)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="shelly_{tabelle}_{von}_{bis}.csv"')
            self.end_headers()
            puffer = io.StringIO()
            w = csv.writer(puffer, delimiter=";")
            w.writerow(["zeit"] + namen)
            for zeile in con.execute(f"SELECT * FROM {tabelle} WHERE ts >= ? AND ts < ? ORDER BY ts", (von, bis)):
                w.writerow([datetime.fromtimestamp(zeile[0], kk.zeitzone).strftime("%Y-%m-%d %H:%M:%S")]
                           + ["" if x is None else str(x).replace(".", ",") for x in zeile])
                if puffer.tell() > 65536:
                    self.wfile.write(puffer.getvalue().encode("utf-8"))
                    puffer.seek(0)
                    puffer.truncate()
            self.wfile.write(puffer.getvalue().encode("utf-8"))

    return ThreadingHTTPServer((k.adresse, k.port), Handler)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="/etc/shelly-monitor/config.ini")
    ap.add_argument("--nur-web", action="store_true", help="nur Webserver, keine Erfassung (z. B. zum Testen)")
    ap.add_argument("--alarm-test", action="store_true", help="Testmeldung über alle Alarmkanäle senden und beenden")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    k = Konfig(args.config)
    melder = Melder(k)
    if args.alarm_test:
        if not melder.aktiv:
            raise SystemExit(f"In {args.config} ist unter [alarm] weder ntfy_url noch smtp_host/mail_an eingetragen")
        fehler = melder.senden("warnung", "Testalarm", "Testmeldung der Netzüberwachung – Empfang funktioniert.")
        if fehler:
            raise SystemExit(f"Testmeldung fehlgeschlagen: {fehler}")
        print("Testmeldung gesendet")
        return
    if not k.host and not args.nur_web:
        raise SystemExit(f"In {args.config} fehlt [shelly] host")
    verbinden(k.datenbank).close()
    stop = threading.Event()
    if not args.nur_web:
        threading.Thread(target=erfassung, args=(k, stop), name="erfassung", daemon=True).start()
        threading.Thread(target=abgleich, args=(k, stop), name="abgleich", daemon=True).start()
        if k.alarm_aktiv:
            if not melder.aktiv:
                LOG.warning("Überwachung ohne Benachrichtigung: [alarm] ntfy_url oder smtp_host/mail_an fehlt")
            threading.Thread(target=melder.arbeiten, args=(stop,), name="melder", daemon=True).start()
            threading.Thread(target=ueberwachung, args=(k, stop, melder), name="ueberwachung", daemon=True).start()
    server = webserver(k)

    def beenden(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, beenden)
    LOG.info("Shelly %s, Raster %d s, Bericht unter http://%s:%d/", k.host, k.intervall, k.adresse, k.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
