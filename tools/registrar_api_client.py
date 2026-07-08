import argparse
import json
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT / "data" / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def now() -> str:
    return time.strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


class RegistrarApiClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False

    def request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, url, **kwargs)
        text = resp.text
        try:
            data = resp.json()
        except Exception:
            data = {"text": text}
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:1200]}")
        return data

    def login(self):
        data = self.request(
            "POST",
            "/api/v1/auth/login",
            json={"username": self.username, "password": self.password},
        )
        log(f"LOGIN_OK username={data.get('username') or self.username}")

    def start_registrar_job(self, count: int) -> str:
        data = self.request("POST", "/api/v1/registrar/jobs", json={"count": count})
        job_id = str(data.get("job_id") or "")
        if not job_id:
            raise RuntimeError(f"missing job_id: {data}")
        log(f"JOB_STARTED id={job_id} count={count}")
        return job_id

    def get_job(self, job_id: str) -> dict:
        return self.request("GET", f"/api/v1/registrar/jobs/{job_id}")

    def export_accounts(self, fmt: str = "json") -> Path:
        fmt = fmt.lower().strip()
        if fmt not in {"json", "csv"}:
            raise ValueError("fmt must be json or csv")
        resp = self.session.get(
            f"{self.base_url}/api/v1/registrar/accounts/export",
            params={"format": fmt, "save": "true"},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"export HTTP {resp.status_code}: {resp.text[:1000]}")
        suffix = "csv" if fmt == "csv" else "json"
        out = EXPORT_DIR / f"registrar_accounts_latest.{suffix}"
        out.write_bytes(resp.content)
        log(f"EXPORT_OK format={fmt} path={out}")
        return out


def render_item(item: dict) -> str:
    return (
        f"#{item.get('index')} "
        f"success={item.get('success')} "
        f"image={item.get('image_success')} "
        f"email={item.get('email') or '-'} "
        f"token={item.get('token_status') or '-'} "
        f"image_url={item.get('image_url') or '-'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run adobe2api registrar via backend API only.")
    parser.add_argument("--base-url", default="http://127.0.0.1:6001")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--poll-seconds", type=float, default=1.5)
    parser.add_argument("--export", choices=["none", "json", "csv", "both"], default="both")
    parser.add_argument("--attach", default="", help="Attach to an existing registrar job id instead of starting a new one.")
    args = parser.parse_args()

    count = max(1, min(int(args.count or 1), 50))
    client = RegistrarApiClient(args.base_url, args.username, args.password)
    client.login()
    job_id = args.attach.strip() or client.start_registrar_job(count)

    seen_logs = 0
    seen_items = 0
    final_job = {}
    while True:
        job = client.get_job(job_id)
        final_job = job
        logs = job.get("logs") if isinstance(job.get("logs"), list) else []
        for line in logs[seen_logs:]:
            print(line, flush=True)
        seen_logs = len(logs)

        items = job.get("items") if isinstance(job.get("items"), list) else []
        for item in items[seen_items:]:
            log(f"ITEM {render_item(item)}")
        seen_items = len(items)

        status = str(job.get("status") or "unknown")
        log(
            "PROGRESS "
            f"status={status} current={job.get('current')}/{job.get('total')} "
            f"success={job.get('success_count')} failed={job.get('failed_count')} "
            f"challenge={job.get('challenge_count')} image_success={job.get('image_success_count')}"
        )
        if status != "running":
            break
        time.sleep(max(0.5, float(args.poll_seconds or 1.5)))

    summary_path = EXPORT_DIR / f"registrar_job_{job_id}_summary.json"
    summary_path.write_text(json.dumps(final_job, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"JOB_DONE id={job_id} summary={summary_path}")
    log(
        f"FINAL success={final_job.get('success_count')}/{final_job.get('total')} "
        f"failed={final_job.get('failed_count')} challenge={final_job.get('challenge_count')} "
        f"image_success={final_job.get('image_success_count')}"
    )

    if args.export in {"json", "both"}:
        client.export_accounts("json")
    if args.export in {"csv", "both"}:
        client.export_accounts("csv")

    return 0 if str(final_job.get("status")) == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
