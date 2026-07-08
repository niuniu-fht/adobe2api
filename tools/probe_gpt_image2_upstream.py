from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adobe_client import AdobeClient
from core.models.payloads import build_image_payload_candidates
from core.token_mgr import token_manager


OUT_DIR = ROOT / "data" / "gpt_image2_probe"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str, **extra: Any) -> None:
    tail = ""
    if extra:
        tail = " " + " ".join(f"{k}={v}" for k, v in extra.items())
    print(f"[{time.strftime('%H:%M:%S')}] {message}{tail}", flush=True)


def mask_token(token: str) -> str:
    token = str(token or "")
    return token[:18] + "..." + token[-10:] if len(token) > 32 else "***"


def trim_response(resp) -> dict:
    try:
        body = resp.json()
    except Exception:
        body = {"text": resp.text[:1200]}
    return {
        "status_code": resp.status_code,
        "headers": {
            "x-request-id": resp.headers.get("x-request-id"),
            "x-task-status": resp.headers.get("x-task-status"),
            "retry-after": resp.headers.get("retry-after"),
            "x-access-error": resp.headers.get("x-access-error"),
            "x-access-user-context": resp.headers.get("x-access-user-context"),
            "x-access-consumption-status": resp.headers.get("x-access-consumption-status"),
            "x-override-status-link": resp.headers.get("x-override-status-link"),
        },
        "body": body,
    }


def payload_variants(prompt: str, size: dict[str, int], quality: str) -> list[tuple[str, dict]]:
    base = build_image_payload_candidates(
        prompt=prompt,
        aspect_ratio="1:1",
        output_resolution="1K",
        upstream_model_id="gpt-image",
        upstream_model_version="2",
        upstream_model="openai:firefly:gpt-image",
        quality_level=quality,
        requested_size=size,
    )[0]

    variants: list[tuple[str, dict]] = []
    variants.append(("current_with_model", dict(base)))

    no_model = dict(base)
    no_model.pop("model", None)
    variants.append(("without_model", no_model))

    colligo_model = dict(base)
    colligo_model["model"] = "openai:firefly:colligo:gpt-image"
    variants.append(("colligo_model", colligo_model))

    no_output_resolution = dict(base)
    no_output_resolution.pop("outputResolution", None)
    variants.append(("without_output_resolution", no_output_resolution))

    settings_in_model_specific = dict(base)
    settings_in_model_specific["modelSpecificPayload"] = {
        **dict(settings_in_model_specific.get("modelSpecificPayload") or {}),
        "parameters": {"detailLevel": int((base.get("generationSettings") or {}).get("detailLevel") or 1)},
    }
    variants.append(("detail_in_model_specific_parameters", settings_in_model_specific))

    numeric_version = dict(base)
    numeric_version["modelVersion"] = 2
    variants.append(("numeric_model_version", numeric_version))

    named_version = dict(base)
    named_version["modelVersion"] = "gpt-image-2"
    variants.append(("named_model_version_gpt_image_2", named_version))

    image2_version = dict(base)
    image2_version["modelVersion"] = "image2"
    variants.append(("model_version_image2", image2_version))

    id_as_model_name = dict(base)
    id_as_model_name["modelId"] = "gpt-image-2"
    id_as_model_name.pop("modelVersion", None)
    variants.append(("model_id_gpt_image_2_no_version", id_as_model_name))

    no_generation_settings = dict(base)
    no_generation_settings.pop("generationSettings", None)
    variants.append(("without_generation_settings", no_generation_settings))

    parameters_detail_only = dict(base)
    parameters_detail_only.pop("generationSettings", None)
    parameters_detail_only["modelSpecificPayload"] = {
        "parameters": {"detailLevel": int((base.get("generationSettings") or {}).get("detailLevel") or 1)}
    }
    variants.append(("parameters_detail_only", parameters_detail_only))

    return variants


def header_variants(client: AdobeClient, token: str, prompt: str) -> list[tuple[str, dict]]:
    current = client._submit_headers(token, prompt=prompt)
    variants: list[tuple[str, dict]] = [("current", dict(current))]

    minimal = client._submit_headers_minimal(token)
    variants.append(("minimal", dict(minimal)))

    firefly_origin = dict(current)
    firefly_origin["origin"] = "https://firefly.adobe.com"
    firefly_origin["referer"] = "https://firefly.adobe.com/"
    variants.append(("firefly_origin", firefly_origin))

    clio_api_key = dict(firefly_origin)
    clio_api_key["x-api-key"] = "clio-playground-web"
    variants.append(("firefly_origin_clio_api_key", clio_api_key))

    no_arp = dict(current)
    no_arp.pop("x-arp-session-id", None)
    variants.append(("without_arp_session", no_arp))

    return variants


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Adobe upstream gpt-image-2 payload variants directly.")
    parser.add_argument("--prompt", default="a tiny blue crystal cube icon on a clean white background")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--token-id", default="", help="Optional token id from config/tokens.json")
    parser.add_argument("--all-variants", action="store_true", help="Try all payload variants instead of current payload only")
    parser.add_argument("--header-variants", action="store_true", help="Try selected header variants as well")
    parser.add_argument("--no-proxy", action="store_true", help="Disable configured proxy for this probe only")
    args = parser.parse_args()

    width, height = [int(x) for x in args.size.lower().split("x", 1)]
    token = ""
    if args.token_id:
        item = token_manager.get_by_id(args.token_id)
        token = str((item or {}).get("value") or "")
    if not token:
        token = token_manager.get_available(strategy="round_robin") or ""
    if not token:
        raise SystemExit("no available token")

    client = AdobeClient()
    if args.no_proxy:
        client.proxy = ""
    variants = payload_variants(args.prompt, {"width": width, "height": height}, args.quality)
    if not args.all_variants:
        variants = variants[:1]
    h_variants = header_variants(client, token, args.prompt) if args.header_variants else [
        ("current", client._submit_headers(token, prompt=args.prompt))
    ]

    result = {
        "started_at": int(time.time()),
        "token": mask_token(token),
        "submit_url": client.submit_url,
        "x_api_key": client.api_key,
        "proxy": client.proxy,
        "items": [],
    }
    log(
        "PROBE_START",
        token=result["token"],
        variants=len(variants),
        header_variants=len(h_variants),
        x_api_key=client.api_key,
    )

    for name, payload in variants:
      for header_name, headers in h_variants:
        combo_name = f"{name}__headers_{header_name}"
        log("SUBMIT", variant=combo_name, model=payload.get("model"), modelId=payload.get("modelId"), modelVersion=payload.get("modelVersion"))
        started = time.time()
        try:
            resp = client._post_json(
                client.submit_url,
                headers=headers,
                payload=payload,
            )
            item = {
                "variant": name,
                "headers_variant": header_name,
                "duration_sec": round(time.time() - started, 3),
                "payload": payload,
                "submit": trim_response(resp),
            }
            log("SUBMIT_DONE", variant=combo_name, status=resp.status_code, task=resp.headers.get("x-task-status") or "-")
        except Exception as exc:
            item = {
                "variant": name,
                "headers_variant": header_name,
                "duration_sec": round(time.time() - started, 3),
                "payload": payload,
                "error": str(exc),
            }
            log("SUBMIT_ERROR", variant=combo_name, error=exc)
        result["items"].append(item)

    out = OUT_DIR / f"probe_{int(time.time())}.json"
    counts: dict[str, int] = {}
    for item in result["items"]:
        code = str(((item.get("submit") or {}).get("status_code")) or "error")
        counts[code] = counts.get(code, 0) + 1
    result["summary"] = {
        "status_counts": counts,
        "accepted_count": sum(
            1
            for x in result["items"]
            if (x.get("submit") or {}).get("status_code") in (200, 201)
        ),
        "total": len(result["items"]),
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("PROBE_SAVED", path=out)
    log("PROBE_SUMMARY", total=result["summary"]["total"], accepted=result["summary"]["accepted_count"], counts=counts)
    return 0 if result["summary"]["accepted_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
