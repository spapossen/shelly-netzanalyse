"""Simulierter Shelly Pro 3EM für Tests und zum Ausprobieren ohne Gerät.

Bildet EM.GetStatus, EMData.GetRecords und /emdata/0/data.csv nach. L1 hat alle
20 s eine Laststufe von 13 A, die Netzimpedanz beträgt Z_NETZ. L2 bricht jeden
Tag um 0 Uhr UTC 90 s lang um 25 V ein, L3 hat täglich um 18 Uhr UTC eine Spitze von 30 A.

Standalone: python3 tests/fake_shelly.py [port]   (Standard 8098)
"""
import json
import math
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

Z_NETZ = 0.32
TAGE_HISTORIE = 3

MINUTEN_FELDER = ["total_act_energy", "fund_act_energy", "total_act_ret_energy", "fund_act_ret_energy",
                  "lag_react_energy", "lead_react_energy", "max_act_power", "min_act_power", "max_aprt_power",
                  "min_aprt_power", "max_voltage", "min_voltage", "avg_voltage", "max_current", "min_current",
                  "avg_current"]


def strom(p, t):
    basis = {"a": 6, "b": 4, "c": 5}[p] + 3 * math.sin(t / 7200)
    if p == "a" and (int(t) // 20) % 2:
        basis += 13
    return basis


def spannung(p, i, t):
    einbruch = 25 if p == "b" and int(t) % 86400 < 90 else 0
    return 231 + 2 * math.sin(t / 3600) - Z_NETZ * i - einbruch


def status(t):
    d = {"id": 0}
    for p in "abc":
        i = strom(p, t)
        u = spannung(p, i, t)
        d.update({f"{p}_current": i, f"{p}_voltage": u, f"{p}_act_power": u * i * 0.95, f"{p}_aprt_power": u * i,
                  f"{p}_pf": 0.95, f"{p}_freq": 50 + 0.03 * math.sin(t / 60)})
    d["n_current"] = None
    d["total_current"] = sum(d[f"{p}_current"] for p in "abc")
    d["total_act_power"] = sum(d[f"{p}_act_power"] for p in "abc")
    d["total_aprt_power"] = sum(d[f"{p}_aprt_power"] for p in "abc")
    d["user_calibrated_phase"] = []
    return d


def minuten_csv(von, bis):
    kopf = ["timestamp"] + [f"{p}_{f}" for p in "abc" for f in MINUTEN_FELDER] + \
        ["n_max_current", "n_min_current", "n_avg_current"]
    zeilen = [",".join(kopf)]
    m = (von + 59) // 60 * 60
    while m < bis:
        werte = []
        for p in "abc":
            i = strom(p, m) + (30 if p == "c" and m % 86400 == 18 * 3600 else 0)
            u = spannung(p, i, m)
            werte += [i * u / 60, i * u / 60, 0, 0, i * 30 / 60, 0, i * u * 1.1, i * u * 0.8, i * u * 1.15,
                      i * u * 0.85, u + 1.5, u - 1.5, u, i * 1.4, i * 0.6, i]
        werte += [3, 0.5, 1.5]
        zeilen.append(str(m) + "," + ",".join(f"{x:.3f}" for x in werte))
        m += 60
    return "\n".join(zeilen) + "\n"


class Handler(BaseHTTPRequestHandler):
    start = int(time.time()) - TAGE_HISTORIE * 86400

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        jetzt = time.time()
        if url.path == "/rpc/EM.GetStatus":
            text = json.dumps(status(jetzt))
        elif url.path == "/rpc/Shelly.GetDeviceInfo":
            text = json.dumps({"id": "shellypro3em-test", "model": "SPEM-003CEBEU", "gen": 2})
        elif url.path == "/rpc/EMData.GetRecords":
            text = json.dumps({"data_blocks": [{"ts": self.start, "period": 60,
                                                "records": TAGE_HISTORIE * 1440}]})
        elif url.path == "/emdata/0/data.csv":
            von = max(self.start, int(q["ts"][0]))
            bis = min(int(q["end_ts"][0]), int(jetzt) - 60)
            text = minuten_csv(von, bis)
        else:
            self.send_response(404)
            self.end_headers()
            return
        daten = text.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)


def starten(port=0):
    """Startet den simulierten Shelly im Hintergrund und gibt den Server zurück."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
    print(f"Simulierter Shelly unter 127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
