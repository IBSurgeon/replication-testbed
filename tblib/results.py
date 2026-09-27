"""Test results: state/results/<test>-<time>/results.json and summary.md."""
import datetime
import json
import os

from . import config as C

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Results:
    def __init__(self, test, params=None):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.dirname(C.state_path("results", f"{test}-{ts}", "x"))
        os.makedirs(self.dir, exist_ok=True)
        self.test = test
        self.params = params or {}
        self.rows = []
        self.started = datetime.datetime.now().isoformat(timespec="seconds")

    def record(self, case, status, **details):
        row = {"case": case, "status": status,
               "time": datetime.datetime.now().isoformat(timespec="seconds")}
        row.update(details)
        self.rows.append(row)
        print(f"  == {status:4} {case}" + (f"  {details.get('note', '')}" if details.get("note") else ""),
              flush=True)
        self.save()
        return status == PASS

    def failed(self):
        return [r for r in self.rows if r["status"] == FAIL]

    def save(self):
        with open(os.path.join(self.dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump({"test": self.test, "started": self.started, "params": self.params,
                       "rows": self.rows}, f, indent=2, default=str)
        lines = [f"# {self.test}", "", f"Started: {self.started}", "",
                 "| Case | Status | Note |", "|---|---|---|"]
        for r in self.rows:
            lines.append(f"| {r['case']} | {r['status']} | {str(r.get('note', '')).replace('|', '/')} |")
        n_fail = len(self.failed())
        lines += ["", f"Total: {len(self.rows)}, failed: {n_fail}"]
        with open(os.path.join(self.dir, "summary.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def finish(self):
        self.save()
        n_fail = len(self.failed())
        print(f"\n{self.test}: {len(self.rows)} case(s), {n_fail} failed. Results: {self.dir}", flush=True)
        return 1 if n_fail else 0
