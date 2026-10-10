"""Called by Render cron jobs: POSTs to the web service's /cron endpoints.

    python scripts/run_cron.py reroute-handoffs
    python scripts/run_cron.py consult-followups

Needs PUBLIC_BASE_URL and CRON_SECRET. Uses only the standard library so the cron
image needs no dependencies.
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
    url = os.environ["PUBLIC_BASE_URL"].rstrip("/") + f"/cron/{job}"
    req = urllib.request.Request(url, method="POST", headers={"X-Cron-Secret": os.environ["CRON_SECRET"]})
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(job, resp.status, json.loads(resp.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
