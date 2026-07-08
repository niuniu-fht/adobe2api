from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config_mgr import config_manager


OUT_DIR = ROOT / "data" / "acceptance"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def now_ts() -> int:
    return int(time.time())


def log(message: str, **extra: Any) -> None:
    suffix = ""
    if extra:
        suffix = " " + " ".join(f"{k}={v}" for k, v in extra.items())
    print(f"[{time.strftime('%H:%M:%S')}] {message}{suffix}", flush=True)


def safe_json_response(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text[:4000]}


def request_json(
    *,
    method: str,
    url: str,
    timeout: int,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    started = time.time()
    sess = session or requests
    try:
        resp = sess.request(method, url, timeout=timeout, **kwargs)
        return {
            "ok": 200 <= int(resp.status_code) < 300,
            "status_code": int(resp.status_code),
            "duration_sec": round(time.time() - started, 3),
            "body": safe_json_response(resp),
            "headers": {
                "content-type": resp.headers.get("content-type"),
                "x-request-id": resp.headers.get("x-request-id"),
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "duration_sec": round(time.time() - started, 3),
            "error": str(exc),
        }


def extract_first_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if isinstance(data, list) and data:
        item = data[0]
        if isinstance(item, dict):
            return str(item.get("url") or "").strip()
    return ""


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    health_ok = bool((result.get("health") or {}).get("ok"))
    ordinary = result.get("ordinary_image") or {}
    gpt = result.get("gpt_image_2_interface") or {}
    diagnostics = result.get("diagnostics") or {}
    probe = result.get("probe") or {}

    ordinary_ok = bool(ordinary.get("ok")) and bool(ordinary.get("image_url"))
    gpt_skipped = bool((result.get("gpt_image_2_interface") or {}).get("skipped"))
    probe_skipped = bool((result.get("probe") or {}).get("skipped"))
    gpt_ok = bool(gpt.get("ok")) and bool(gpt.get("image_url"))
    probe_summary = ((probe.get("body") or {}).get("summary") or {}) if isinstance(probe.get("body"), dict) else {}
    probe_accepted = int(probe_summary.get("accepted_count") or 0)
    diag_conclusion = ""
    if isinstance(diagnostics.get("body"), dict):
        diag_conclusion = str(diagnostics["body"].get("conclusion") or "")

    if gpt_ok:
        verdict = "PASS:gpt-image-2 interface generated image"
    elif ordinary_ok and gpt_skipped:
        verdict = "PASS_BASE_ONLY:ordinary image works; gpt-image-2 interface was skipped"
    elif ordinary_ok and probe_accepted <= 0 and "upstream" in diag_conclusion:
        verdict = "UPSTREAM_BLOCKED:ordinary image works, gpt-image-2 upstream probe/interface failed"
    elif ordinary_ok and not gpt_ok:
        verdict = "GPT_INTERFACE_FAILED:ordinary image works, gpt-image-2 interface failed"
    elif health_ok and not ordinary_ok:
        verdict = "BASE_IMAGE_FAILED:service healthy but ordinary image failed"
    else:
        verdict = "SERVICE_FAILED:health check failed"

    return {
        "health_ok": health_ok,
        "ordinary_image_ok": ordinary_ok,
        "gpt_image_2_interface_skipped": gpt_skipped,
        "gpt_image_2_interface_ok": gpt_ok,
        "probe_skipped": probe_skipped,
        "probe_accepted_count": probe_accepted,
        "diagnostic_conclusion": diag_conclusion,
        "verdict": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one-click Adobe2API image acceptance: health, ordinary image, "
            "gpt-image-2 interface, diagnostics, and optional upstream probe."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:6001")
    parser.add_argument("--api-key", default=str(config_manager.get("api_key", "your-api-key") or "your-api-key"))
    parser.add_argument("--admin-username", default=str(config_manager.get("admin_username", "admin") or "admin"))
    parser.add_argument("--admin-password", default=str(config_manager.get("admin_password", "admin") or "admin"))
    parser.add_argument("--ordinary-model", default="firefly-nano-banana-1k-1x1")
    parser.add_argument("--ordinary-prompt", default="a small blue crystal cube on a clean white background")
    parser.add_argument("--gpt-prompt", default="a tiny blue crystal cube icon on a clean white background")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--token-id", default="", help="Optional token id to force for /v1/images requests")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--skip-gpt-interface", action="store_true")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument("--probe-no-proxy", action="store_true")
    parser.add_argument("--out", default="", help="Optional JSON report path")
    args = parser.parse_args()

    base_url = str(args.base_url or "").rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json"}
    if str(args.token_id or "").strip():
        headers["x-adobe-token-id"] = str(args.token_id or "").strip()
    result: dict[str, Any] = {
        "started_at": now_ts(),
        "base_url": base_url,
        "forced_token_id": str(args.token_id or "").strip(),
        "proxy": (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        ),
        "steps": [],
    }

    log("HEALTH_START", url=base_url)
    health = request_json(method="GET", url=f"{base_url}/api/v1/health", timeout=30)
    result["health"] = health
    result["steps"].append({"name": "health", "ok": health.get("ok"), "status_code": health.get("status_code")})
    log("HEALTH_DONE", ok=health.get("ok"), status=health.get("status_code"))

    ordinary_body = {
        "model": args.ordinary_model,
        "prompt": args.ordinary_prompt,
        "n": 1,
        "size": args.size,
        "response_format": "url",
    }
    log("ORDINARY_IMAGE_START", model=args.ordinary_model)
    ordinary = request_json(
        method="POST",
        url=f"{base_url}/v1/images/generations",
        timeout=max(60, int(args.timeout)),
        headers=headers,
        json=ordinary_body,
    )
    ordinary["image_url"] = extract_first_url(ordinary.get("body"))
    result["ordinary_image"] = ordinary
    result["steps"].append(
        {
            "name": "ordinary_image",
            "ok": ordinary.get("ok") and bool(ordinary.get("image_url")),
            "status_code": ordinary.get("status_code"),
            "image_url": ordinary.get("image_url"),
        }
    )
    log("ORDINARY_IMAGE_DONE", ok=ordinary.get("ok"), status=ordinary.get("status_code"), url=ordinary.get("image_url") or "-")

    if not args.skip_gpt_interface:
        gpt_body = {
            "model": "gpt-image-2",
            "prompt": args.gpt_prompt,
            "n": 1,
            "size": args.size,
            "response_format": "url",
        }
        log("GPT_IMAGE_2_INTERFACE_START")
        gpt = request_json(
            method="POST",
            url=f"{base_url}/v1/images/generations",
            timeout=max(60, int(args.timeout)),
            headers=headers,
            json=gpt_body,
        )
        gpt["image_url"] = extract_first_url(gpt.get("body"))
        result["gpt_image_2_interface"] = gpt
        result["steps"].append(
            {
                "name": "gpt_image_2_interface",
                "ok": gpt.get("ok") and bool(gpt.get("image_url")),
                "status_code": gpt.get("status_code"),
                "image_url": gpt.get("image_url"),
            }
        )
        log("GPT_IMAGE_2_INTERFACE_DONE", ok=gpt.get("ok"), status=gpt.get("status_code"), url=gpt.get("image_url") or "-")
    else:
        result["gpt_image_2_interface"] = {"skipped": True}
        result["steps"].append({"name": "gpt_image_2_interface", "skipped": True})

    session = requests.Session()
    login_body = {"username": args.admin_username, "password": args.admin_password}
    log("ADMIN_LOGIN_START")
    login = request_json(
        method="POST",
        url=f"{base_url}/api/v1/auth/login",
        timeout=30,
        session=session,
        json=login_body,
    )
    result["admin_login"] = login
    result["steps"].append({"name": "admin_login", "ok": login.get("ok"), "status_code": login.get("status_code")})
    log("ADMIN_LOGIN_DONE", ok=login.get("ok"), status=login.get("status_code"))

    if login.get("ok"):
        log("DIAGNOSTICS_START")
        diagnostics = request_json(
            method="GET",
            url=f"{base_url}/api/v1/diagnostics/gpt-image-2?limit=100",
            timeout=60,
            session=session,
        )
        result["diagnostics"] = diagnostics
        conclusion = ""
        if isinstance(diagnostics.get("body"), dict):
            conclusion = str(diagnostics["body"].get("conclusion") or "")
        result["steps"].append(
            {
                "name": "diagnostics",
                "ok": diagnostics.get("ok"),
                "status_code": diagnostics.get("status_code"),
                "conclusion": conclusion,
            }
        )
        log("DIAGNOSTICS_DONE", ok=diagnostics.get("ok"), status=diagnostics.get("status_code"), conclusion=conclusion or "-")

        if not args.skip_probe:
            probe_body = {
                "size": args.size,
                "quality": "low",
                "timeout_seconds": 240,
                "no_proxy": bool(args.probe_no_proxy),
            }
            if str(args.token_id or "").strip():
                probe_body["token_id"] = str(args.token_id or "").strip()
            log("UPSTREAM_PROBE_START", no_proxy=bool(args.probe_no_proxy))
            probe = request_json(
                method="POST",
                url=f"{base_url}/api/v1/diagnostics/gpt-image-2/probe",
                timeout=300,
                session=session,
                json=probe_body,
            )
            result["probe"] = probe
            probe_summary = {}
            if isinstance(probe.get("body"), dict):
                probe_summary = probe["body"].get("summary") or {}
            result["steps"].append(
                {
                    "name": "upstream_probe",
                    "ok": probe.get("ok"),
                    "status_code": probe.get("status_code"),
                    "summary": probe_summary,
                }
            )
            log(
                "UPSTREAM_PROBE_DONE",
                ok=probe.get("ok"),
                status=probe.get("status_code"),
                counts=json.dumps(probe_summary.get("status_counts") or {}, ensure_ascii=False),
                accepted=probe_summary.get("accepted_count", 0),
            )
        else:
            result["probe"] = {"skipped": True}
            result["steps"].append({"name": "upstream_probe", "skipped": True})

    result["finished_at"] = now_ts()
    result["summary"] = summarize(result)
    out_path = Path(args.out) if args.out else OUT_DIR / f"image_acceptance_{result['finished_at']}.json"
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["report_path"] = str(out_path)
    log("REPORT_SAVED", path=out_path)
    log("VERDICT", verdict=result["summary"]["verdict"])
    print(json.dumps({"summary": result["summary"], "report_path": str(out_path)}, ensure_ascii=False, indent=2))

    # Return success for definitive gpt-image-2 success or for a clean upstream-blocked classification.
    # Return non-zero when the base service/image path itself is broken or the failure is ambiguous.
    return 0 if result["summary"]["verdict"].startswith(("PASS:", "PASS_BASE_ONLY:", "UPSTREAM_BLOCKED:")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
