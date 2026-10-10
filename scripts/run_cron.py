"""Triggers the web service's scheduled jobs: POSTs to its /cron endpoints.

    python scripts/run_cron.py reroute-handoffs
    python scripts/run_cron.py consult-followups

Run by .github/workflows/cron.yml. Needs PUBLIC_BASE_URL and CRON_SECRET.
Standard library only, so the runner needs no dependencies. A serverless host may
need a moment to start the app after idle, hence the long timeout.
"""

import json
import os
import sys
import urllib.request

JOBS = {"reroute-handoffs", "consult-followups"}


def main() -> int:
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job not in JOBS:
        print(f"usage: run_cron.py {{{'|'.join(sorted(JOBS))}}}", file=sys.stderr)
        return 2
    base, secret = os.environ.get("PUBLIC_BASE_URL", ""), os.environ.get("CRON_SECRET", "")
    if not base or not secret:
        print("PUBLIC_BASE_URL / CRON_SECRET not set yet; skipping.")
        return 0
    url = base.rstrip("/") + f"/cron/{job}"
    req = urllib.request.Request(url, method="POST", headers={"X-Cron-Secret": secret})
    with urllib.request.urlopen(req, timeout=150) as resp:
        print(job, resp.status, json.loads(resp.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
