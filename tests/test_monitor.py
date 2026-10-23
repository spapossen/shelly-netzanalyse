import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HIER))
sys.path.insert(0, HIER)

import fake_shelly  # noqa: E402
import shelly_monitor as sm  # noqa: E402


class Basis(unittest.TestCase):
    """Füllt eine Datenbank aus dem simulierten Shelly: Minutenhistorie und zehn Minuten Sekundenwerte."""

    @classmethod
    def setUpClass(cls):
        cls.shelly = fake_shelly.starten()
        cls.tmp = tempfile.TemporaryDirectory()
        k = sm.Konfig(None)
        k.host = "127.0.0.1:%d" % cls.shelly.server_address[1]
        k.datenbank = os.path.join(cls.tmp.name, "daten.db")
        k.sicherung = 35.0
        k.port = 0
        k.adresse = "127.0.0.1"
        cls.k = k
        cls.con = sm.verbinden(k.datenbank)
        cls.zeilen = sm.minuten_abgleichen(cls.con, sm.Shelly(k))

        # Zehn Minuten Sekundenwerte wie im Betrieb erfassen, ohne real zu warten
        cls.jetzt = int(time.time())
        puffer, stufen, vorher = [], [], None
        for ts in range(cls.jetzt - 600, cls.jetzt):
            zeile = {"ts": ts}
            zeile.update({n: float(v) for n, v in fake_shelly.status(ts).items()
                          if n != "id" and isinstance(v, (int, float))})
            if vorher:
                stufen += sm.laststufen(vorher, zeile)
            vorher = zeile
            puffer.append(zeile)
        sm.speichern(cls.con, puffer, stufen)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.shelly.shutdown()
        cls.shelly.server_close()
        cls.tmp.cleanup()


class MonitorTest(Basis):
    def test_historie_wird_vollstaendig_und_inkrementell_geladen(self):
        self.assertGreater(self.zeilen, fake_shelly.TAGE_HISTORIE * 1440 - 5)
        self.assertLess(sm.minuten_abgleichen(self.con, sm.Shelly(self.k)), 5)

    def test_laststufen_ignoriert_kleine_spruenge(self):
        alt = {"ts": 0, "a_current": 5, "a_voltage": 230, "b_current": 1, "b_voltage": 230,
               "c_current": 1, "c_voltage": 230}
        neu = dict(alt, ts=1, a_current=6, b_current=11, b_voltage=227)
        stufen = sm.laststufen(alt, neu)
        self.assertEqual([s[1] for s in stufen], ["b"])
        self.assertAlmostEqual(stufen[0][2], -3)

    def test_bericht(self):
        r = sm.bericht(self.con, self.jetzt - 86400, self.jetzt, self.k)
        self.assertEqual(r["quelle"], "minute")
        l1, l2, l3 = (r["phasen"][p] for p in "abc")
        self.assertAlmostEqual(l1["z"], fake_shelly.Z_NETZ, delta=0.02)
        self.assertGreaterEqual(l1["z_stufen"], 20)
        self.assertIsNone(l2["z"])
        self.assertAlmostEqual(l3["reserve"], 35 - l3["imax"])
        self.assertLess(l3["reserve"], 0)
        self.assertLessEqual(l1["zusatz"], l1["reserve"])
        self.assertAlmostEqual(r["frequenz"]["avg"], 50, delta=0.05)
        self.assertEqual(r["abdeckung"]["sekunden"], 600)
        self.assertTrue(any(e["art"] == "Unterspannung" and e["phasen"] == ["L2"] for e in r["ereignisse"]))
        stufen = {s for s, _, _ in sm.bewertung(r, self.k)}
        self.assertIn("warn", stufen)

    def test_gleichzeitige_ueber_und_unterspannung_bleiben_getrennt(self):
        con = sm.verbinden(":memory:")
        ts = 1_700_000_040
        zeilen = []
        for i in range(3):
            zeile = {"ts": ts + 60 * i}
            for p, u_min, u_max in (("a", 240, 261), ("b", 225, 230), ("c", 185, 205)):
                zeile.update({f"{p}_min_voltage": u_min, f"{p}_max_voltage": u_max, f"{p}_avg_voltage": u_min + 5,
                              f"{p}_max_current": 20, f"{p}_avg_current": 15})
            zeilen.append(zeile)
        sm.einfuegen(con, "minute", zeilen)
        r = sm.bericht(con, ts, ts + 180, self.k)
        con.close()
        arten = {(e["art"], tuple(e["phasen"])): e["extrem"] for e in r["ereignisse"]}
        self.assertEqual(arten, {("Überspannung", ("L1",)): 261, ("Unterspannung", ("L3",)): 185})

    def test_verlauf(self):
        v = sm.verlauf(self.con, self.jetzt - 7 * 86400, self.jetzt, self.k)
        self.assertEqual(v["quelle"], "minute")
        self.assertLessEqual(len(v["t"]), 721)
        self.assertEqual(len(v["u"]["L1"]), len(v["t"]))
        kurz = sm.verlauf(self.con, self.jetzt - 1800, self.jetzt, self.k)
        self.assertEqual(kurz["quelle"], "sekunde")
        self.assertTrue(kurz["f"]["t"])

    def test_leerer_zeitraum(self):
        r = sm.bericht(self.con, 1000, 2000, self.k)
        self.assertEqual(r["phasen"], {})
        seite = sm.bericht_html(r, sm.verlauf(self.con, 1000, 2000, self.k), self.k,
                                {"von": "", "bis": ""}, False)
        self.assertIn("noch keine Daten", seite)


class NeutralleiterTest(unittest.TestCase):
    """Synthetische Anlage mit bekannten Leiterwiderständen: Z_L = 0,25 Ω, Z_N = 0,6 Ω."""

    Z_L, Z_N = 0.25, 0.6

    @classmethod
    def setUpClass(cls):
        import cmath
        import random
        zufall = random.Random(7)
        cls.k = sm.Konfig(None)
        cls.k.zuleitung = [{"laenge_m": 250, "mm2": 25, "mm2_n": 25, "material": "cu", "name": ""}]
        cls.con = sm.verbinden(":memory:")
        cls.start = 1_700_000_040
        stroeme = [10.0, 10.0, 10.0]
        zeilen = []
        for m in range(3000):
            stroeme = [min(40, max(0, i + zufall.gauss(0, 3))) for i in stroeme]
            zeiger = {p: cmath.rect(1, sm.WINKEL[p]) for p, _ in sm.PHASEN}
            i_n = sum(i * zeiger[p] for i, (p, _) in zip(stroeme, sm.PHASEN))
            zeile = {"ts": cls.start + 60 * m}
            for i, (p, _) in zip(stroeme, sm.PHASEN):
                u = abs(230 * zeiger[p] - cls.Z_L * i * zeiger[p] - cls.Z_N * i_n)
                zeile.update({f"{p}_avg_voltage": u, f"{p}_min_voltage": u - 0.5, f"{p}_max_voltage": u + 0.5,
                              f"{p}_avg_current": i, f"{p}_max_current": i + 1, f"{p}_max_aprt_power": u * i,
                              f"{p}_total_act_energy": u * i / 60, f"{p}_total_act_ret_energy": 0,
                              f"{p}_lag_react_energy": 0, f"{p}_lead_react_energy": 0})
            sm.abgeleitet_minute(zeile)
            zeilen.append(zeile)
        sm.einfuegen(cls.con, "minute", zeilen)
        cls.r = sm.bericht(cls.con, cls.start, cls.start + 60 * 3000, cls.k)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_sternpunkt_und_n_strom(self):
        self.assertAlmostEqual(sm.sternpunkt(230, 230, 230), 0, places=6)
        self.assertAlmostEqual(sm.n_strom({"a": 10, "b": 10, "c": 10}, {}), 0, places=6)
        self.assertAlmostEqual(sm.n_strom({"a": 10, "b": 0, "c": 0}, {}), 10, places=6)
        self.assertIsNone(sm.sternpunkt(230, None, 230))

    def test_kopplungsmatrix_trennt_z_l_und_z_n(self):
        ko = self.r["kopplung"]
        self.assertAlmostEqual(ko["z_n"], self.Z_N, delta=0.06)
        for z_l in ko["z_l"]:
            self.assertAlmostEqual(z_l, self.Z_L, delta=0.06)

    def test_steigung_entspricht_z_n_plus_zwei_drittel_z_l(self):
        fit = self.r["neutral"]["fit"]
        self.assertAlmostEqual(fit["z"], self.Z_N + 2 / 3 * self.Z_L, delta=0.08)
        self.assertGreater(fit["r2"], 0.9)

    def test_bewertung_gegen_zuleitung(self):
        stufe, was, text = sm.neutral_bewertung(self.r)
        self.assertEqual(was, "Neutralleiter")
        self.assertEqual(stufe, "krit")  # 0,6 Ω gegenüber ≈ 0,21 Ω aus 250 m 25 mm² Cu
        self.assertIn("Faktor", text)

    def test_nachberechnung(self):
        self.con.execute("UPDATE minute SET calc_version = NULL, calc_sternpunkt = NULL")
        self.assertEqual(sm.nachberechnen(self.con), 3000)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM minute WHERE calc_sternpunkt IS NULL").fetchone()[0], 0)

    def test_zuleitung_aus_mehreren_abschnitten(self):
        import configparser
        cp = configparser.ConfigParser()
        cp.read_string("[zuleitung]\nabschnitt1 = 150, 35, al, UV Halle\nabschnitt2 = 100, 70, al, Hauptverteilung\n")
        k = sm.Konfig(None)
        k.zuleitung = sm.abschnitte_lesen(cp)
        zu = sm.zuleitung(k)
        self.assertEqual([a["name"] for a in zu["abschnitte"]], ["UV Halle", "Hauptverteilung"])
        self.assertAlmostEqual(zu["r_l"][0], 0.150 * 0.868 + 0.100 * 0.443, places=6)
        self.assertAlmostEqual(zu["laenge_m"], 250)
        cp = configparser.ConfigParser()
        cp.read_string("[zuleitung]\nlaenge_m = 250\nquerschnitt_mm2 = 25\nmaterial = cu\n")
        self.assertEqual(sm.abschnitte_lesen(cp)[0]["mm2_n"], 25)

    def test_fachdaten(self):
        fd = sm.fachdaten(self.con, self.start, self.start + 60 * 3000, self.k, "minute")
        self.assertEqual(len(fd["tagesprofil"]), 24)
        self.assertEqual(len(fd["tiefste"]), 15)
        self.assertTrue(any(p["groesse"] == "Sternpunktverschiebung" for p in fd["perzentile"]))
        seite = sm.fachbericht_html(self.r, fd, sm.verlauf(self.con, self.start, self.start + 180000, self.k),
                                    self.k, {"von": "", "bis": ""}, True)
        self.assertIn("Kopplung Spannung–Strom", seite)
        self.assertNotIn("Bewertung", seite)


class WebTest(Basis):
    def server(self, **einstellungen):
        k = sm.Konfig(None)
        k.__dict__.update(self.k.__dict__)
        k.__dict__.update(einstellungen)
        server = sm.webserver(k)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def holen(self, url, kopf=None):
        with urllib.request.urlopen(urllib.request.Request(url, headers=kopf or {}), timeout=60) as antwort:
            return antwort.status, dict(antwort.headers), antwort.read().decode("utf-8")

    def test_seiten(self):
        basis = self.server()
        code, _, seite = self.holen(basis + "/?zeitraum=24h")
        self.assertEqual(code, 200)
        self.assertIn("Netzbericht", seite)
        self.assertIn("Lastreserve je Phase", seite)
        _, kopf, seite = self.holen(basis + "/?zeitraum=24h&download=1")
        self.assertIn("attachment", kopf["Content-Disposition"])
        self.assertIn('"live":false', seite)
        _, _, text = self.holen(basis + "/api/bericht?von=2020-01-01T00:00&bis=2030-01-01T00:00")
        self.assertIn("phasen", json.loads(text))
        _, kopf, seite = self.holen(basis + "/?zeitraum=24h&modus=fach&download=1")
        self.assertIn("fachbericht_", kopf["Content-Disposition"])
        self.assertIn("Methoden und Annahmen", seite)
        _, kopf, text = self.holen(basis + "/export.csv?tabelle=sekunde&zeitraum=1h")
        self.assertTrue(text.startswith("zeit;ts;"))
        self.assertEqual(len(text.strip().splitlines()), 601)
        sm.status_setzen(alarme_aktiv=[{"schluessel": "ueber_a", "stufe": "kritisch", "text": "Überspannung L1",
                                        "seit": self.jetzt, "extrem": 270.0}])
        self.addCleanup(sm.status_setzen, alarme_aktiv=[])
        _, _, seite = self.holen(basis + "/?zeitraum=1h")
        self.assertIn('class="alarm krit"', seite)
        code, _, seite = self.holen(basis + "/alarme")
        self.assertEqual(code, 200)
        self.assertIn("Alarme", seite)
        _, _, text = self.holen(basis + "/api/alarme")
        self.assertEqual(json.loads(text)["aktiv"][0]["schluessel"], "ueber_a")

    def test_anmeldung(self):
        basis = self.server(web_benutzer="admin", web_passwort="geheim")
        with self.assertRaises(urllib.error.HTTPError) as fehler:
            self.holen(basis + "/")
        self.assertEqual(fehler.exception.code, 401)
        token = base64.b64encode(b"admin:geheim").decode()
        code, _, _ = self.holen(basis + "/api/status", {"Authorization": "Basic " + token})
        self.assertEqual(code, 200)


class FakeMelder:
    def __init__(self):
        self.meldungen = []

    def melden(self, stufe, titel, text):
        self.meldungen.append((stufe, titel))


class AlarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.k = sm.Konfig(None)
        self.k.sicherung = 80.0
        self.k.alarm_strom = 72.0
        self.con = sm.verbinden(os.path.join(self.tmp.name, "daten.db"))
        self.addCleanup(self.con.close)
        self.melder = FakeMelder()
        self.u = sm.Ueberwachung(self.k, self.melder, self.con)

    @staticmethod
    def zeile(ts, ua=230.0, ub=230.0, uc=230.0, i=5.0):
        z = {"ts": ts, "a_voltage": ua, "b_voltage": ub, "c_voltage": uc,
             "a_current": i, "b_current": i, "c_current": i}
        z["calc_sternpunkt"] = sm.sternpunkt(ua, ub, uc)
        return z

    def test_bedingungen(self):
        b = sm.alarm_bedingungen(self.zeile(0, ua=270, ub=205, uc=40, i=75), self.k)
        self.assertEqual(b["ueber_a"][0], "kritisch")
        self.assertEqual(b["unter_b"][0], "warnung")
        self.assertEqual(b["ausfall_c"][0], "kritisch")
        self.assertEqual(b["strom_a"][0], "warnung")
        self.assertEqual(sm.alarm_bedingungen(self.zeile(0, ua=280, ub=230, uc=180), self.k)["stern"][0], "kritisch")
        self.assertEqual(sm.alarm_bedingungen(self.zeile(0, ua=260, ub=230, uc=200), self.k)["stern"][0], "warnung")
        self.assertEqual(sm.alarm_bedingungen(self.zeile(0), self.k), {})

    def test_warnung_eskalation_entwarnung(self):
        t = 1000
        for _ in range(4):  # 4 s Unterspannung: noch keine Meldung
            self.u.pruefen(t, self.zeile(t, ua=205), 0)
            t += 1
        self.assertEqual(self.melder.meldungen, [])
        self.u.pruefen(t, self.zeile(t, ua=205), 0)
        self.assertEqual(self.melder.meldungen[-1], ("warnung", "Unterspannung L1"))
        t += 1
        self.u.pruefen(t, self.zeile(t, ua=190), 0)
        self.assertEqual(self.melder.meldungen[-1], ("kritisch", "Unterspannung L1"))
        for _ in range(119):
            t += 1
            self.u.pruefen(t, self.zeile(t), 0)
        self.assertEqual(len(self.melder.meldungen), 2)
        t += 1
        self.u.pruefen(t, self.zeile(t), 0)
        self.assertEqual(self.melder.meldungen[-1][0], "entwarnung")
        (start, ende, stufe, extrem), = self.con.execute("SELECT start, ende, stufe, extrem FROM alarm").fetchall()
        self.assertEqual((start, stufe, extrem), (1000, "kritisch", 190))
        self.assertIsNotNone(ende)

    def test_kurze_spitze_wird_nicht_gemeldet(self):
        self.u.pruefen(1, self.zeile(1, ub=255), 0)
        self.u.pruefen(2, self.zeile(2), 0)
        self.u.pruefen(3, self.zeile(3, ub=255), 0)
        self.assertEqual(self.melder.meldungen, [])

    def test_messausfall_und_erinnerung(self):
        self.u.pruefen(100, self.zeile(60), 40)
        self.assertEqual(self.melder.meldungen[-1][0], "kritisch")
        self.assertIn("Keine Messwerte", self.melder.meldungen[-1][1])
        self.u.pruefen(100 + 30 * 60, self.zeile(60), 40 + 30 * 60)
        self.assertTrue(self.melder.meldungen[-1][1].startswith("weiterhin"))
        self.u.pruefen(2000, self.zeile(2000), 0)
        self.assertEqual(self.melder.meldungen[-1][0], "entwarnung")
        self.assertEqual(self.u.aktiv, {})

    def test_ntfy_versand(self):
        empfangen = []

        class Handler(sm.BaseHTTPRequestHandler):
            def do_POST(self):
                laenge = int(self.headers["Content-Length"])
                empfangen.append((dict(self.headers), self.rfile.read(laenge).decode("utf-8")))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = sm.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.k.ntfy_url = "http://127.0.0.1:%d/netz" % server.server_address[1]
        self.assertEqual(sm.Melder(self.k).senden("kritisch", "Überspannung L1", "U = 270 V"), {})
        kopf, text = empfangen[0]
        self.assertEqual(kopf["Priority"], "5")
        self.assertEqual(text, "U = 270 V")
        self.assertTrue(kopf["Title"].startswith("=?UTF-8?B?"))
        self.assertIn("Überspannung L1", base64.b64decode(kopf["Title"][10:-2]).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
