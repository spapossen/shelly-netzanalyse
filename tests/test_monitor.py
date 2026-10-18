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
        _, kopf, text = self.holen(basis + "/export.csv?tabelle=sekunde&zeitraum=1h")
        self.assertTrue(text.startswith("zeit;ts;"))
        self.assertEqual(len(text.strip().splitlines()), 601)

    def test_anmeldung(self):
        basis = self.server(web_benutzer="admin", web_passwort="geheim")
        with self.assertRaises(urllib.error.HTTPError) as fehler:
            self.holen(basis + "/")
        self.assertEqual(fehler.exception.code, 401)
        token = base64.b64encode(b"admin:geheim").decode()
        code, _, _ = self.holen(basis + "/api/status", {"Authorization": "Basic " + token})
        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
