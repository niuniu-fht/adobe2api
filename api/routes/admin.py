import json
import os
import subprocess
import sys
import threading
import time
import uuid
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.responses import RedirectResponse

from api.schemas import (
    AdobeAccountsImportRequest,
    AdobeAccountUpdateRequest,
    AdobeRegisterRequest,
    PaymentCardsImportRequest,
    PaymentCardUpsertRequest,
    AdminLoginRequest,
    ConfigUpdateRequest,
    ExportSelectionRequest,
    RefreshCookieBatchImportRequest,
    RefreshCookieImportRequest,
    RefreshProfileEnabledRequest,
    TokenAddRequest,
    TokenBatchAddRequest,
    TokenCreditsBatchRefreshRequest,
)
from core.adobe_account_mgr import adobe_account_manager
from core.card_mgr import card_manager


def build_admin_router(
    *,
    static_dir: Path,
    token_manager,
    config_manager,
    refresh_manager,
    log_store,
    error_store,
    live_log_store,
    require_admin_auth: Callable[[Request], None],
    is_admin_authenticated: Callable[[Request], bool],
    apply_client_config: Callable[[], None],
    get_generated_storage_stats: Callable[[], dict[str, Any]],
) -> APIRouter:
    router = APIRouter()
    cloak_register_jobs: dict[str, dict[str, Any]] = {}
    cloak_register_jobs_lock = threading.Lock()
    registrar_jobs: dict[str, dict[str, Any]] = {}
    registrar_jobs_lock = threading.Lock()
    membership_jobs: dict[str, dict[str, Any]] = {}
    membership_jobs_lock = threading.Lock()
    account_jobs: dict[str, dict[str, Any]] = {}
    account_jobs_lock = threading.Lock()

    def get_batch_concurrency() -> int:
        try:
            value = int(config_manager.get("batch_concurrency", 5) or 5)
        except Exception:
            value = 5
        return max(1, min(100, value))

    def delete_token_and_linked_profile(token_id: str) -> bool:
        token_info = token_manager.get_by_id(token_id)
        if not token_info:
            return False

        profile_id = str(token_info.get("refresh_profile_id") or "").strip()
        if token_info.get("auto_refresh") and profile_id:
            try:
                refresh_manager.remove_profile(profile_id)
            except KeyError:
                token_manager.remove(token_id)
        else:
            token_manager.remove(token_id)
        return True

    def build_cloak_register_env() -> tuple[Path, Path, str, dict[str, str], int]:
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "cloak_adobe_register.py"
        if not script.exists():
            raise FileNotFoundError("cloak register script not found")

        proxy = (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        )
        env = os.environ.copy()
        if proxy:
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            ):
                env[key] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        for cfg_key, env_key in {
            "cloak_browser_binary_path": "CLOAKBROWSER_BINARY_PATH",
            "cloak_browser_license_key": "CLOAKBROWSER_LICENSE_KEY",
            "cloak_browser_version": "CLOAKBROWSER_VERSION",
        }.items():
            value = str(config_manager.get(cfg_key, "") or "").strip()
            if value:
                env[env_key] = value

        try:
            timeout = int(config_manager.get("cloak_browser_timeout_seconds", 900) or 900)
        except Exception:
            timeout = 900
        timeout = max(120, min(timeout, 3600))
        return root_dir, script, proxy, env, timeout

    def build_http_register_env() -> tuple[Path, Path, str, dict[str, str], int]:
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "http_adobe_register.py"
        if not script.exists():
            raise FileNotFoundError("http register script not found")

        proxy = (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        )
        env = os.environ.copy()
        if proxy:
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            ):
                env[key] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])

        try:
            timeout = int(config_manager.get("http_register_timeout_seconds", 240) or 240)
        except Exception:
            timeout = 240
        timeout = max(60, min(timeout, 1200))
        return root_dir, script, proxy, env, timeout

    def build_signup_token_capture_env() -> tuple[Path, Path, str, dict[str, str], int]:
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "capture_adobe_signup_tokens.py"
        if not script.exists():
            raise FileNotFoundError("signup token capture script not found")
        proxy = (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        )
        env = os.environ.copy()
        if proxy:
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            ):
                env[key] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        try:
            timeout = int(config_manager.get("signup_token_capture_timeout_seconds", 120) or 120)
        except Exception:
            timeout = 120
        timeout = max(30, min(timeout, 900))
        return root_dir, script, proxy, env, timeout

    @router.get("/api/v1/health")
    def health():
        return {"status": "ok", "pool_size": len(token_manager.list_all())}

    def _latest_gpt_image2_probe() -> dict[str, Any]:
        root_dir = Path(__file__).resolve().parents[2]
        probe_dir = root_dir / "data" / "gpt_image2_probe"
        if not probe_dir.exists():
            return {}
        try:
            latest = sorted(
                probe_dir.glob("probe_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:1]
        except Exception:
            latest = []
        if not latest:
            return {}
        path = latest[0]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "path": str(path),
                "error": f"failed to parse latest probe: {exc}",
            }
        return {
            "path": str(path),
            "started_at": payload.get("started_at"),
            "submit_url": payload.get("submit_url"),
            "x_api_key": payload.get("x_api_key"),
            "proxy": payload.get("proxy"),
            "summary": payload.get("summary") or {},
            "first_item": ((payload.get("items") or [])[:1] or [None])[0],
        }

    def _summarize_recent_image_logs(limit: int = 100) -> dict[str, Any]:
        rows, total = log_store.list(limit=min(max(int(limit or 100), 1), 100), page=1)
        gpt_rows: list[dict[str, Any]] = []
        ordinary_success: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        token_counts: dict[str, int] = {}
        for row in rows:
            if str(row.get("path") or "") != "/v1/images/generations":
                continue
            model = str(row.get("model") or "").strip()
            status_code = str(row.get("status_code") or "0")
            if model == "gpt-image-2":
                gpt_rows.append(row)
                status_counts[status_code] = status_counts.get(status_code, 0) + 1
                err = str(row.get("error") or "").strip()[:160]
                if err:
                    error_counts[err] = error_counts.get(err, 0) + 1
                token_id = str(row.get("token_id") or "").strip() or "-"
                token_counts[token_id] = token_counts.get(token_id, 0) + 1
            elif int(row.get("status_code") or 0) == 200 and row.get("preview_url"):
                ordinary_success.append(row)
        latest_gpt = gpt_rows[0] if gpt_rows else None
        latest_success = ordinary_success[0] if ordinary_success else None
        return {
            "total_log_rows": total,
            "inspected_rows": len(rows),
            "gpt_image_2": {
                "recent_attempts": len(gpt_rows),
                "status_counts": status_counts,
                "error_counts": error_counts,
                "token_counts": token_counts,
                "latest": latest_gpt,
            },
            "ordinary_image_latest_success": latest_success,
        }

    def _summarize_token_clients() -> dict[str, Any]:
        tokens = token_manager.list_all()
        by_client: dict[str, dict[str, int]] = {}
        for item in tokens:
            client_id = str(item.get("refresh_client_id") or "unknown").strip() or "unknown"
            status = str(item.get("status") or "unknown").strip() or "unknown"
            bucket = by_client.setdefault(client_id, {"total": 0})
            bucket["total"] = bucket.get("total", 0) + 1
            bucket[status] = bucket.get(status, 0) + 1
        return {
            "total": len(tokens),
            "by_client_id": by_client,
            "projectx_active": int(
                (by_client.get("projectx_webapp") or {}).get("active", 0)
            ),
            "clio_active": int(
                (by_client.get("clio-playground-web") or {}).get("active", 0)
            ),
        }

    @router.get("/api/v1/diagnostics/gpt-image-2")
    def gpt_image_2_diagnostics(request: Request, limit: int = 100):
        """Summarize whether gpt-image-2 failures are local, token, or Adobe upstream."""
        require_admin_auth(request)
        log_summary = _summarize_recent_image_logs(limit=limit)
        probe = _latest_gpt_image2_probe()
        token_summary = _summarize_token_clients()

        gpt_summary = log_summary.get("gpt_image_2") or {}
        status_counts = gpt_summary.get("status_counts") or {}
        accepted_probe_count = int(
            ((probe.get("summary") or {}).get("accepted_count") or 0)
            if isinstance(probe, dict)
            else 0
        )
        local_interface_ok = bool(log_summary.get("ordinary_image_latest_success"))
        gpt_has_success = int(status_counts.get("200") or status_counts.get(200) or 0) > 0
        all_known_gpt_failures_are_temporary = bool(status_counts) and all(
            str(code) in {"408", "429", "451", "500", "502", "503", "504"}
            for code in status_counts.keys()
        )
        has_temporary_gpt_failures = any(
            str(code) in {"408", "429", "451", "500", "502", "503", "504"}
            and int(count or 0) > 0
            for code, count in status_counts.items()
        )
        conclusion = "unknown"
        if gpt_has_success or accepted_probe_count > 0:
            conclusion = "gpt-image-2 accepted by upstream"
        elif local_interface_ok and (
            all_known_gpt_failures_are_temporary or has_temporary_gpt_failures
        ):
            conclusion = "local image pipeline works; gpt-image-2 is failing at Adobe upstream submit/probe"
        elif not token_summary.get("projectx_active"):
            conclusion = "no active projectx_webapp token available for gpt-image-2"
        elif not local_interface_ok:
            conclusion = "ordinary image path has no recent success; verify base image pipeline first"

        return {
            "status": "ok",
            "checked_at": int(time.time()),
            "conclusion": conclusion,
            "proxy": (
                str(config_manager.get("proxy", "") or "").strip()
                if bool(config_manager.get("use_proxy", False))
                else ""
            ),
            "tokens": token_summary,
            "logs": log_summary,
            "latest_probe": probe,
        }

    @router.post("/api/v1/diagnostics/gpt-image-2/probe")
    def gpt_image_2_probe(req: dict[str, Any], request: Request):
        """Run the direct upstream gpt-image-2 probe script and return its saved summary."""
        require_admin_auth(request)
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "probe_gpt_image2_upstream.py"
        if not script.exists():
            raise HTTPException(status_code=500, detail="probe script not found")

        payload = req or {}
        size = str(payload.get("size") or "1024x1024").strip() or "1024x1024"
        quality = str(payload.get("quality") or config_manager.get("gpt_image_quality", "low") or "low").strip().lower()
        if quality not in {"low", "medium", "high"}:
            raise HTTPException(status_code=400, detail="quality must be one of: low, medium, high")
        timeout = int(payload.get("timeout_seconds") or 240)
        timeout = max(30, min(timeout, 900))

        cmd = [sys.executable, str(script), "--size", size, "--quality", quality]
        token_id = str(payload.get("token_id") or "").strip()
        if token_id:
            cmd.extend(["--token-id", token_id])
        if bool(payload.get("all_variants", False)):
            cmd.append("--all-variants")
        if bool(payload.get("header_variants", False)):
            cmd.append("--header-variants")
        if bool(payload.get("no_proxy", False)):
            cmd.append("--no-proxy")

        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "timeout",
                "duration_sec": round(time.time() - started, 3),
                "timeout_seconds": timeout,
                "stdout": str(exc.stdout or "")[-12000:],
                "stderr": str(exc.stderr or "")[-12000:],
                "latest_probe": _latest_gpt_image2_probe(),
            }

        latest_probe = _latest_gpt_image2_probe()
        summary = latest_probe.get("summary") or {}
        return {
            "status": "ok" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - started, 3),
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-12000:],
            "summary": summary,
            "latest_probe": latest_probe,
        }

    @router.get("/login", include_in_schema=False)
    def page_login(request: Request):
        if is_admin_authenticated(request):
            return RedirectResponse(url="/")
        return FileResponse(static_dir / "login.html")

    @router.post("/api/v1/auth/login")
    def admin_login(req: AdminLoginRequest, request: Request):
        username = str(req.username or "").strip()
        password = str(req.password or "")
        expected_username = str(
            config_manager.get("admin_username", "admin") or "admin"
        ).strip()
        expected_password = str(
            config_manager.get("admin_password", "admin") or "admin"
        )

        if username != expected_username or password != expected_password:
            raise HTTPException(status_code=401, detail="Invalid username or password")

        request.session.clear()
        request.session["admin_auth"] = True
        request.session["username"] = username
        request.session["login_at"] = int(time.time())
        return {"status": "ok", "username": username}

    @router.get("/api/v1/auth/me")
    def admin_me(request: Request):
        if not is_admin_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {
            "authenticated": True,
            "username": str((request.session or {}).get("username") or ""),
        }

    @router.post("/api/v1/auth/logout")
    def admin_logout(request: Request):
        request.session.clear()
        return {"status": "ok"}

    @router.get("/", include_in_schema=False)
    def page_root(request: Request):
        if not is_admin_authenticated(request):
            return RedirectResponse(url="/login")
        return FileResponse(static_dir / "admin.html")

    @router.get("/adobe", include_in_schema=False)
    @router.get("/adobe/", include_in_schema=False)
    def page_adobe(request: Request):
        if not is_admin_authenticated(request):
            return RedirectResponse(url="/login")
        return FileResponse(static_dir / "adobe-pay" / "index.html")

    @router.get("/registrar", include_in_schema=False)
    @router.get("/registrar/", include_in_schema=False)
    def page_registrar(request: Request):
        if not is_admin_authenticated(request):
            return RedirectResponse(url="/login")
        return FileResponse(static_dir / "registrar" / "index.html")

    @router.get("/api/v1/adobe/accounts")
    def adobe_accounts_list(request: Request):
        require_admin_auth(request)
        accounts = []
        for account in adobe_account_manager.list_accounts():
            item = dict(account)
            item["token_summary"] = _token_summary_for_account_email(
                str(item.get("email") or "")
            )
            accounts.append(item)
        return {
            "status": "ok",
            "accounts": accounts,
            "logs": adobe_account_manager.list_logs(limit=100),
        }

    @router.get("/api/v1/adobe/register/logs")
    def adobe_register_logs(request: Request, limit: int = 100):
        require_admin_auth(request)
        return {
            "status": "ok",
            "logs": adobe_account_manager.list_logs(limit=limit),
        }

    @router.delete("/api/v1/adobe/register/logs")
    def adobe_register_logs_clear(request: Request):
        require_admin_auth(request)
        adobe_account_manager.clear_logs()
        return {"status": "ok"}

    @router.post("/api/v1/adobe/register")
    def adobe_register(req: AdobeRegisterRequest, request: Request):
        require_admin_auth(request)
        try:
            email_provider = str(
                req.email_provider
                or config_manager.get("adobe_register_email_provider", "tempmail_lol")
                or "tempmail_lol"
            ).strip()
            tempmail_api_key = str(
                req.tempmail_api_key
                or config_manager.get("tempmail_lol_api_key", "")
                or ""
            ).strip()
            result = adobe_account_manager.register_accounts(
                count=req.count,
                domain=str(req.domain or "trial.local"),
                email_prefix=str(req.email_prefix or "adobe_user"),
                email_provider=email_provider,
                tempmail_api_key=tempmail_api_key,
                tempmail_proxy=(
                    str(config_manager.get("proxy", "") or "").strip()
                    if bool(config_manager.get("use_proxy", False))
                    else ""
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "ok", **result, "logs": adobe_account_manager.list_logs()}

    @router.post("/api/v1/adobe/register/cloak")
    def adobe_register_cloak(request: Request):
        require_admin_auth(request)
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "cloak_adobe_register.py"
        result_file = root_dir / "data" / "cloak_adobe_register_result.json"
        if not script.exists():
            raise HTTPException(status_code=500, detail="cloak register script not found")

        proxy = (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        )
        env = os.environ.copy()
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        for cfg_key, env_key in {
            "cloak_browser_binary_path": "CLOAKBROWSER_BINARY_PATH",
            "cloak_browser_license_key": "CLOAKBROWSER_LICENSE_KEY",
            "cloak_browser_version": "CLOAKBROWSER_VERSION",
        }.items():
            value = str(config_manager.get(cfg_key, "") or "").strip()
            if value:
                env[env_key] = value

        try:
            timeout = int(config_manager.get("cloak_browser_timeout_seconds", 900) or 900)
        except Exception:
            timeout = 900
        timeout = max(120, min(timeout, 3600))

        try:
            if result_file.exists():
                result_file.unlink()
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(root_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            payload = {
                "status": "timeout",
                "used_proxy": bool(proxy),
                "proxy": proxy,
                "timeout_seconds": timeout,
                "stdout": (exc.stdout or "")[-12000:],
                "stderr": (exc.stderr or "")[-12000:],
            }
            if result_file.exists():
                try:
                    payload["result"] = json.loads(result_file.read_text(encoding="utf-8"))
                except Exception:
                    payload["result_raw"] = result_file.read_text(encoding="utf-8")[-12000:]
            raise HTTPException(status_code=504, detail=payload)
        payload: dict[str, Any] = {
            "status": "ok" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "used_proxy": bool(proxy),
            "proxy": proxy,
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-12000:],
        }
        if result_file.exists():
            try:
                payload["result"] = json.loads(result_file.read_text(encoding="utf-8"))
            except Exception:
                payload["result_raw"] = result_file.read_text(encoding="utf-8")[-12000:]
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=payload)
        return payload

    @router.post("/api/v1/adobe/register/cloak/job")
    def adobe_register_cloak_job(request: Request):
        """Start CloakBrowser registration in background for UI live output."""
        require_admin_auth(request)
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "cloak_adobe_register.py"
        result_file = root_dir / "data" / "cloak_adobe_register_result.json"
        if not script.exists():
            raise HTTPException(status_code=500, detail="cloak register script not found")

        proxy = (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        )
        env = os.environ.copy()
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        for cfg_key, env_key in {
            "cloak_browser_binary_path": "CLOAKBROWSER_BINARY_PATH",
            "cloak_browser_license_key": "CLOAKBROWSER_LICENSE_KEY",
            "cloak_browser_version": "CLOAKBROWSER_VERSION",
        }.items():
            value = str(config_manager.get(cfg_key, "") or "").strip()
            if value:
                env[env_key] = value

        try:
            timeout = int(config_manager.get("cloak_browser_timeout_seconds", 900) or 900)
        except Exception:
            timeout = 900
        timeout = max(120, min(timeout, 3600))
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "running",
            "returncode": None,
            "used_proxy": bool(proxy),
            "proxy": proxy,
            "started_at": int(time.time()),
            "updated_at": int(time.time()),
            "stdout_lines": [],
            "stderr_lines": [],
            "result": None,
            "error": "",
        }
        with cloak_register_jobs_lock:
            cloak_register_jobs[job_id] = job

        def _append(kind: str, line: str) -> None:
            with cloak_register_jobs_lock:
                target = cloak_register_jobs.get(job_id)
                if not target:
                    return
                lines = target.setdefault(f"{kind}_lines", [])
                lines.append(line.rstrip("\r\n"))
                del lines[:-240]
                target["updated_at"] = int(time.time())

        def _reader(pipe, kind: str) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    if not line:
                        break
                    _append(kind, line)
            except Exception as exc:
                _append(kind, f"[reader-error] {exc}")

        def _runner() -> None:
            proc = None
            try:
                if result_file.exists():
                    result_file.unlink()
                proc = subprocess.Popen(
                    [sys.executable, str(script)],
                    cwd=str(root_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                threads = []
                for pipe, kind in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                    if pipe is None:
                        continue
                    t = threading.Thread(target=_reader, args=(pipe, kind), daemon=True)
                    t.start()
                    threads.append(t)
                try:
                    returncode = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    returncode = -9
                    _append("stderr", f"[timeout] exceeded {timeout}s")
                for t in threads:
                    t.join(timeout=2)
                payload_result = None
                if result_file.exists():
                    try:
                        payload_result = json.loads(result_file.read_text(encoding="utf-8"))
                    except Exception:
                        payload_result = {"raw": result_file.read_text(encoding="utf-8")[-12000:]}
                with cloak_register_jobs_lock:
                    target = cloak_register_jobs.get(job_id)
                    if target:
                        target["returncode"] = returncode
                        target["status"] = "succeeded" if returncode == 0 else "failed"
                        target["result"] = payload_result
                        target["updated_at"] = int(time.time())
            except Exception as exc:
                with cloak_register_jobs_lock:
                    target = cloak_register_jobs.get(job_id)
                    if target:
                        target["status"] = "failed"
                        target["error"] = str(exc)
                        target["updated_at"] = int(time.time())
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        threading.Thread(target=_runner, daemon=True).start()
        return {
            "status": "running",
            "job_id": job_id,
            "used_proxy": bool(proxy),
            "proxy": proxy,
            "timeout_seconds": timeout,
        }

    @router.get("/api/v1/adobe/register/cloak/jobs/{job_id}")
    def adobe_register_cloak_job_status(job_id: str, request: Request):
        require_admin_auth(request)
        with cloak_register_jobs_lock:
            job = cloak_register_jobs.get(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            return dict(job)

    @router.post("/api/v1/registrar/jobs")
    def registrar_job_start(req: dict[str, Any], request: Request):
        """Batch registrar: run the selected script flow and aggregate logs."""
        require_admin_auth(request)
        payload = req or {}
        mode = str(payload.get("mode") or payload.get("register_mode") or "http_request").strip().lower()
        if mode in {"http", "request", "requests", "script", "http_script"}:
            mode = "http_request"
        elif mode in {"cloak", "cloakbrowser", "browser"}:
            mode = "cloakbrowser"
        if mode not in {"http_request", "cloakbrowser"}:
            raise HTTPException(status_code=400, detail="mode must be http_request or cloakbrowser")
        try:
            if mode == "http_request":
                root_dir, script, proxy, env_template, timeout = build_http_register_env()
            else:
                root_dir, script, proxy, env_template, timeout = build_cloak_register_env()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        try:
            total = int(payload.get("count") or 1)
        except Exception:
            total = 1
        total = max(1, min(total, 50))
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "mode": mode,
            "status": "running",
            "total": total,
            "current": 0,
            "success_count": 0,
            "failed_count": 0,
            "challenge_count": 0,
            "image_success_count": 0,
            "web_image_success_count": 0,
            "used_proxy": bool(proxy),
            "proxy": proxy,
            "started_at": int(time.time()),
            "updated_at": int(time.time()),
            "finished_at": None,
            "logs": [],
            "items": [],
            "error": "",
        }
        with registrar_jobs_lock:
            registrar_jobs[job_id] = job

        def _append(line: str) -> None:
            with registrar_jobs_lock:
                target = registrar_jobs.get(job_id)
                if not target:
                    return
                logs = target.setdefault("logs", [])
                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}".rstrip())
                del logs[:-3000]
                target["updated_at"] = int(time.time())

        def _reader(pipe, index: int, kind: str) -> None:
            try:
                for raw in iter(pipe.readline, ""):
                    if not raw:
                        break
                    _append(f"#{index} {kind} {raw.rstrip()}")
            except Exception as exc:
                _append(f"#{index} {kind} [reader-error] {exc}")

        def _run_one(index: int) -> dict[str, Any]:
            prefix = "http_adobe_register_result" if mode == "http_request" else "cloak_adobe_register_result"
            result_path = root_dir / "data" / f"{prefix}_{job_id}_{index}.json"
            try:
                if result_path.exists():
                    result_path.unlink()
            except Exception:
                pass
            env = dict(env_template)
            if mode == "http_request":
                env["HTTP_REGISTER_RESULT_FILE"] = str(result_path)
            else:
                env["CLOAK_REGISTER_RESULT_FILE"] = str(result_path)
            _append(f"#{index} START 注册流程启动 mode={mode} result={result_path}")
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(root_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            threads = []
            for pipe, kind in ((proc.stdout, "STDOUT"), (proc.stderr, "STDERR")):
                if pipe is None:
                    continue
                t = threading.Thread(target=_reader, args=(pipe, index, kind), daemon=True)
                t.start()
                threads.append(t)
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = -9
                _append(f"#{index} TIMEOUT 超过 {timeout}s，已结束进程")
            for t in threads:
                t.join(timeout=2)

            parsed: dict[str, Any] = {}
            if result_path.exists():
                try:
                    parsed = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    parsed = {"stage": "parse_failed", "detail": str(exc)}
            account = parsed.get("account") if isinstance(parsed, dict) else {}
            if not isinstance(account, dict):
                account = {}
            image_test = parsed.get("image_test") if isinstance(parsed, dict) else {}
            if not isinstance(image_test, dict):
                image_test = {}
            web_image_status = str(account.get("web_image_status") or "").strip()
            web_image_success = web_image_status.lower() in {"passed", "ok", "success", "succeeded"}
            classification = str(parsed.get("classification") or "").strip().lower() if isinstance(parsed, dict) else ""
            has_challenge = (
                bool(parsed.get("has_challenge")) if isinstance(parsed, dict) else False
            ) or classification == "challenge"
            register_success = bool(parsed.get("success")) and not has_challenge
            image_success = str(image_test.get("status") or "").lower() == "ok"
            item = {
                "index": index,
                "mode": mode,
                "returncode": returncode,
                "success": register_success,
                "has_challenge": has_challenge,
                "classification": classification,
                "email": str(account.get("email") or ""),
                "account_status": str(account.get("status") or ""),
                "mail_status": str(account.get("mail_status") or ""),
                "token_status": str(account.get("token_status") or ""),
                "web_image_status": web_image_status,
                "web_image_success": web_image_success,
                "web_image_url": str(account.get("web_image_test_url") or ""),
                "web_image_error": str(account.get("web_image_test_error") or "")[:500],
                "image_status": str(account.get("image_status") or ""),
                "image_success": image_success,
                "image_url": str(account.get("image_test_url") or image_test.get("image_url") or ""),
                "result_path": str(result_path),
                "last_action": str(account.get("last_action") or ""),
                "error": str(parsed.get("error") or image_test.get("detail") or "")[:500],
                "interface_catalog_path": str(((parsed.get("assets") or {}).get("catalog_path") or "")) if isinstance(parsed, dict) else "",
            }
            _append(
                f"#{index} DONE mode={mode} success={item['success']} classification={item['classification'] or '-'} "
                f"web_image={item['web_image_success']} "
                f"api6001_image={item['image_success']} "
                f"email={item['email'] or '-'} status={item['account_status'] or '-'}"
            )
            if item.get("error"):
                _append(f"#{index} RESULT_ERROR {item['error']}")
            if item.get("interface_catalog_path"):
                _append(f"#{index} INTERFACE_CATALOG {item['interface_catalog_path']}")
            return item

        def _runner() -> None:
            try:
                for index in range(1, total + 1):
                    with registrar_jobs_lock:
                        target = registrar_jobs.get(job_id)
                        if target:
                            target["current"] = index
                            target["updated_at"] = int(time.time())
                    item = _run_one(index)
                    with registrar_jobs_lock:
                        target = registrar_jobs.get(job_id)
                        if not target:
                            continue
                        target.setdefault("items", []).append(item)
                        if item.get("success"):
                            target["success_count"] = int(target.get("success_count") or 0) + 1
                        else:
                            target["failed_count"] = int(target.get("failed_count") or 0) + 1
                        if item.get("has_challenge"):
                            target["challenge_count"] = int(target.get("challenge_count") or 0) + 1
                        if item.get("image_success"):
                            target["image_success_count"] = int(target.get("image_success_count") or 0) + 1
                        if item.get("web_image_success"):
                            target["web_image_success_count"] = int(target.get("web_image_success_count") or 0) + 1
                        target["updated_at"] = int(time.time())
                with registrar_jobs_lock:
                    target = registrar_jobs.get(job_id)
                    if target:
                        target["status"] = "succeeded"
                        target["finished_at"] = int(time.time())
                        target["updated_at"] = int(time.time())
                        final_snapshot = dict(target)
                    else:
                        final_snapshot = {}
                if final_snapshot:
                    jobs_dir = root_dir / "data" / "registrar_jobs"
                    jobs_dir.mkdir(parents=True, exist_ok=True)
                    (jobs_dir / f"{job_id}.json").write_text(
                        json.dumps(final_snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                _append("BATCH_DONE 注册机任务完成")
            except Exception as exc:
                with registrar_jobs_lock:
                    target = registrar_jobs.get(job_id)
                    if target:
                        target["status"] = "failed"
                        target["error"] = str(exc)
                        target["finished_at"] = int(time.time())
                        target["updated_at"] = int(time.time())
                        failed_snapshot = dict(target)
                    else:
                        failed_snapshot = {}
                if failed_snapshot:
                    jobs_dir = root_dir / "data" / "registrar_jobs"
                    jobs_dir.mkdir(parents=True, exist_ok=True)
                    (jobs_dir / f"{job_id}.json").write_text(
                        json.dumps(failed_snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                _append(f"BATCH_FAILED {exc}")

        threading.Thread(target=_runner, daemon=True).start()
        return {"status": "running", "job_id": job_id, "total": total, "mode": mode}

    @router.get("/api/v1/registrar/jobs/{job_id}")
    def registrar_job_status(job_id: str, request: Request):
        require_admin_auth(request)
        with registrar_jobs_lock:
            job = registrar_jobs.get(job_id)
            if job:
                return dict(job)
        job_path = Path(__file__).resolve().parents[2] / "data" / "registrar_jobs" / f"{job_id}.json"
        if job_path.exists():
            try:
                return json.loads(job_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        raise HTTPException(status_code=404, detail="job not found")

    @router.get("/api/v1/registrar/jobs/{job_id}/export")
    def registrar_job_export(job_id: str, request: Request):
        require_admin_auth(request)
        job: dict[str, Any] | None = None
        with registrar_jobs_lock:
            if job_id in registrar_jobs:
                job = dict(registrar_jobs[job_id])
        if job is None:
            job_path = Path(__file__).resolve().parents[2] / "data" / "registrar_jobs" / f"{job_id}.json"
            if job_path.exists():
                try:
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                except Exception:
                    job = None
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        content = json.dumps(job, ensure_ascii=False, indent=2)
        return Response(
            content,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="registrar_job_{job_id}.json"'},
        )

    @router.get("/api/v1/registrar/jobs")
    def registrar_jobs_list(request: Request, limit: int = 20):
        require_admin_auth(request)
        safe_limit = max(1, min(int(limit or 20), 100))
        with registrar_jobs_lock:
            by_id = {str(item.get("id") or ""): dict(item) for item in registrar_jobs.values()}
        jobs_dir = Path(__file__).resolve().parents[2] / "data" / "registrar_jobs"
        if jobs_dir.exists():
            for path in sorted(jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:safe_limit]:
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                jid = str(item.get("id") or path.stem)
                by_id.setdefault(jid, item)
        rows = sorted(
            by_id.values(),
            key=lambda item: int(item.get("updated_at") or item.get("started_at") or 0),
            reverse=True,
        )[:safe_limit]
        return {"status": "ok", "jobs": rows}

    @router.post("/api/v1/membership/jobs")
    def membership_job_start(req: dict[str, Any], request: Request):
        require_admin_auth(request)
        root_dir = Path(__file__).resolve().parents[2]
        script = root_dir / "tools" / "adobe_membership_flow.py"
        if not script.exists():
            raise HTTPException(status_code=500, detail="membership script not found")
        action = str((req or {}).get("action") or "eligibility").strip().lower()
        if action not in {"eligibility", "open"}:
            raise HTTPException(status_code=400, detail="action must be eligibility or open")
        account_id = str((req or {}).get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(status_code=400, detail="account_id is required")
        card_id = str((req or {}).get("card_id") or "").strip()
        plan_url = str((req or {}).get("plan_url") or "").strip()
        submit_payment = bool((req or {}).get("submit_payment", False))
        if action == "open" and not card_id:
            raise HTTPException(status_code=400, detail="card_id is required for open")

        proxy = (
            str(config_manager.get("proxy", "") or "").strip()
            if bool(config_manager.get("use_proxy", False))
            else ""
        )
        env = os.environ.copy()
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = proxy
            env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        for cfg_key, env_key in {
            "cloak_browser_binary_path": "CLOAKBROWSER_BINARY_PATH",
            "cloak_browser_license_key": "CLOAKBROWSER_LICENSE_KEY",
            "cloak_browser_version": "CLOAKBROWSER_VERSION",
        }.items():
            value = str(config_manager.get(cfg_key, "") or "").strip()
            if value:
                env[env_key] = value
        job_id = uuid.uuid4().hex[:12]
        result_path = root_dir / "data" / "membership_jobs" / f"{job_id}.result.json"
        env.update(
            {
                "MEMBERSHIP_FLOW_RESULT_FILE": str(result_path),
                "MEMBERSHIP_ACTION": action,
                "MEMBERSHIP_ACCOUNT_ID": account_id,
                "MEMBERSHIP_CARD_ID": card_id,
                "MEMBERSHIP_PLAN_URL": plan_url,
                "MEMBERSHIP_SUBMIT_PAYMENT": "true" if submit_payment else "false",
            }
        )
        try:
            timeout = int(config_manager.get("cloak_browser_timeout_seconds", 900) or 900)
        except Exception:
            timeout = 900
        timeout = max(120, min(timeout, 3600))
        job = {
            "id": job_id,
            "status": "running",
            "action": action,
            "account_id": account_id,
            "card_id": card_id,
            "plan_url": plan_url or "https://www.adobe.com/creativecloud/plans.html",
            "submit_payment": submit_payment,
            "used_proxy": bool(proxy),
            "proxy": proxy,
            "started_at": int(time.time()),
            "updated_at": int(time.time()),
            "finished_at": None,
            "returncode": None,
            "logs": [],
            "result": None,
            "error": "",
        }
        with membership_jobs_lock:
            membership_jobs[job_id] = job

        def _append(line: str):
            with membership_jobs_lock:
                target = membership_jobs.get(job_id)
                if not target:
                    return
                logs = target.setdefault("logs", [])
                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}".rstrip())
                del logs[:-2000]
                target["updated_at"] = int(time.time())

        def _reader(pipe, kind: str):
            try:
                for raw in iter(pipe.readline, ""):
                    if not raw:
                        break
                    _append(f"{kind} {raw.rstrip()}")
            except Exception as exc:
                _append(f"{kind} [reader-error] {exc}")

        def _runner():
            proc = None
            try:
                result_path.parent.mkdir(parents=True, exist_ok=True)
                if result_path.exists():
                    result_path.unlink()
                proc = subprocess.Popen(
                    [sys.executable, str(script)],
                    cwd=str(root_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                threads = []
                for pipe, kind in ((proc.stdout, "STDOUT"), (proc.stderr, "STDERR")):
                    if pipe is None:
                        continue
                    t = threading.Thread(target=_reader, args=(pipe, kind), daemon=True)
                    t.start()
                    threads.append(t)
                try:
                    returncode = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    returncode = -9
                    _append(f"TIMEOUT exceeded {timeout}s")
                for t in threads:
                    t.join(timeout=2)
                result = None
                if result_path.exists():
                    try:
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                    except Exception:
                        result = {"raw": result_path.read_text(encoding="utf-8")[-12000:]}
                with membership_jobs_lock:
                    target = membership_jobs.get(job_id)
                    if target:
                        target["returncode"] = returncode
                        target["status"] = "succeeded" if returncode == 0 else "failed"
                        target["result"] = result
                        target["finished_at"] = int(time.time())
                        target["updated_at"] = int(time.time())
                        snapshot = dict(target)
                    else:
                        snapshot = {}
                if snapshot:
                    summary_path = root_dir / "data" / "membership_jobs" / f"{job_id}.json"
                    summary_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                with membership_jobs_lock:
                    target = membership_jobs.get(job_id)
                    if target:
                        target["status"] = "failed"
                        target["error"] = str(exc)
                        target["finished_at"] = int(time.time())
                        target["updated_at"] = int(time.time())
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        threading.Thread(target=_runner, daemon=True).start()
        return {"status": "running", "job_id": job_id}

    @router.get("/api/v1/membership/jobs/{job_id}")
    def membership_job_status(job_id: str, request: Request):
        require_admin_auth(request)
        with membership_jobs_lock:
            if job_id in membership_jobs:
                return dict(membership_jobs[job_id])
        path = Path(__file__).resolve().parents[2] / "data" / "membership_jobs" / f"{job_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="job not found")

    @router.get("/api/v1/adobe/accounts/{account_id}/emails")
    def adobe_account_emails(account_id: str, request: Request):
        require_admin_auth(request)
        try:
            result = adobe_account_manager.fetch_account_emails(
                account_id,
                tempmail_api_key=str(config_manager.get("tempmail_lol_api_key", "") or ""),
                tempmail_proxy=(
                    str(config_manager.get("proxy", "") or "").strip()
                    if bool(config_manager.get("use_proxy", False))
                    else ""
                ),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="account not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "ok", **result}

    @router.post("/api/v1/adobe/accounts/import")
    def adobe_accounts_import(req: AdobeAccountsImportRequest, request: Request):
        require_admin_auth(request)
        try:
            result = adobe_account_manager.import_accounts(req.accounts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        status = "ok" if not result.get("skipped_count") else "partial"
        return {"status": status, **result}

    @router.put("/api/v1/adobe/accounts/{account_id}")
    def adobe_account_update(
        account_id: str, req: AdobeAccountUpdateRequest, request: Request
    ):
        require_admin_auth(request)
        patch = req.model_dump(exclude_unset=True)
        try:
            account = adobe_account_manager.update_account(account_id, patch)
        except KeyError:
            raise HTTPException(status_code=404, detail="account not found")
        return {"status": "ok", "account": account}

    @router.delete("/api/v1/adobe/accounts/{account_id}")
    def adobe_account_delete(account_id: str, request: Request):
        require_admin_auth(request)
        if not adobe_account_manager.delete_account(account_id):
            raise HTTPException(status_code=404, detail="account not found")
        return {"status": "ok"}

    def _token_client_id(token_item: dict[str, Any]) -> str:
        value = str(token_item.get("value") or "").strip()
        client_id = str(token_item.get("refresh_client_id") or "").strip()
        if client_id:
            return client_id
        try:
            payload = token_manager._decode_jwt_payload(value)
            return str(payload.get("client_id") or payload.get("cid") or "").strip()
        except Exception:
            return ""

    def _tokens_for_account_email(email: str, *, prefer_client_id: str = "") -> list[dict[str, Any]]:
        target = str(email or "").strip().lower()
        preferred = str(prefer_client_id or "").strip()
        if not target:
            return []
        matches: list[dict[str, Any]] = []
        for item in token_manager.export_tokens():
            if str(item.get("status") or "").strip() != "active":
                continue
            token_email = str(item.get("refresh_profile_email") or "").strip().lower()
            if token_email != target:
                continue
            item = dict(item)
            item["client_id"] = _token_client_id(item)
            matches.append(item)
        if preferred:
            matches.sort(key=lambda x: 0 if str(x.get("client_id") or "") == preferred else 1)
        return matches

    def _profile_id_for_account(account: dict[str, Any]) -> str:
        direct = str(account.get("cookie_profile_id") or "").strip()
        if direct:
            return direct
        email = str(account.get("email") or "").strip().lower()
        if not email:
            return ""
        for item in token_manager.list_all():
            if str(item.get("refresh_profile_email") or "").strip().lower() != email:
                continue
            pid = str(item.get("refresh_profile_id") or "").strip()
            if pid:
                return pid
        return ""

    def _account_has_active_clio(account: dict[str, Any]) -> bool:
        summary = _token_summary_for_account_email(str(account.get("email") or ""))
        active_clients = summary.get("active_by_client_id") or {}
        return bool(active_clients.get("clio-playground-web"))

    def _token_summary_for_account_email(email: str) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        summary: dict[str, Any] = {
            "email": target,
            "total": 0,
            "active": 0,
            "by_status": {},
            "active_by_client_id": {},
            "active_token_ids": [],
            "preferred_image_token_id": "",
            "preferred_gpt_token_id": "",
        }
        if not target:
            return summary
        for item in token_manager.list_all():
            token_email = str(item.get("refresh_profile_email") or "").strip().lower()
            if token_email != target:
                continue
            status = str(item.get("status") or "unknown").strip() or "unknown"
            client_id = str(item.get("refresh_client_id") or "unknown").strip() or "unknown"
            token_id = str(item.get("id") or "").strip()
            summary["total"] += 1
            summary["by_status"][status] = int(summary["by_status"].get(status, 0)) + 1
            if status == "active":
                summary["active"] += 1
                summary["active_token_ids"].append(token_id)
                bucket = summary["active_by_client_id"].setdefault(client_id, [])
                bucket.append(token_id)
        clio_ids = summary["active_by_client_id"].get("clio-playground-web") or []
        projectx_ids = summary["active_by_client_id"].get("projectx_webapp") or []
        summary["preferred_image_token_id"] = (clio_ids or projectx_ids or [""])[0]
        summary["preferred_gpt_token_id"] = (projectx_ids or [""])[0]
        return summary

    @router.post("/api/v1/registrar/accounts/{account_id}/refresh-clio-token")
    @router.post("/api/v1/adobe/accounts/{account_id}/refresh-clio-token")
    def registrar_account_refresh_clio_token(account_id: str, request: Request):
        require_admin_auth(request)
        try:
            account = adobe_account_manager.get_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="account not found")
        profile_id = _profile_id_for_account(account)
        if not profile_id:
            patch = {
                "token_refresh_status": "failed",
                "token_refresh_error": "no refresh profile id found for account",
                "token_refresh_updated_at": int(time.time()),
                "token_status": "missing_profile",
                "last_action": "刷新 Clio token 失败：未找到 refresh profile",
            }
            updated = adobe_account_manager.update_account(account_id, patch)
            return {"status": "failed", "account": updated, "detail": patch["token_refresh_error"]}
        client_id = str(getattr(refresh_manager, "CLIO_CLIENT_ID", "clio-playground-web"))
        try:
            result = refresh_manager.refresh_once(profile_id, client_id=client_id)
        except KeyError:
            patch = {
                "token_refresh_status": "failed",
                "token_refresh_error": f"profile not found: {profile_id}",
                "token_refresh_updated_at": int(time.time()),
                "token_status": "profile_not_found",
                "last_action": "刷新 Clio token 失败：profile 不存在",
            }
            updated = adobe_account_manager.update_account(account_id, patch)
            return {"status": "failed", "account": updated, "detail": patch["token_refresh_error"]}
        except Exception as exc:
            patch = {
                "token_refresh_status": "failed",
                "token_refresh_error": str(exc)[:1000],
                "token_refresh_updated_at": int(time.time()),
                "token_status": "refresh_failed",
                "last_action": "刷新 Clio token 失败",
            }
            updated = adobe_account_manager.update_account(account_id, patch)
            return {"status": "failed", "account": updated, "detail": str(exc)}

        token_summary = _token_summary_for_account_email(str(account.get("email") or ""))
        patch = {
            "token_refresh_status": "ok",
            "token_refresh_error": "",
            "token_refresh_updated_at": int(time.time()),
            "token_status": "active" if token_summary.get("active") else "refreshed",
            "last_action": "刷新 Clio token 成功",
        }
        updated = adobe_account_manager.update_account(account_id, patch)
        return {
            "status": "ok",
            "account": updated,
            "profile_id": profile_id,
            "client_id": client_id,
            "result": result,
            "token_summary": _token_summary_for_account_email(str(account.get("email") or "")),
        }

    @router.post("/api/v1/registrar/accounts/refresh-clio-batch")
    @router.post("/api/v1/adobe/accounts/refresh-clio-batch")
    def registrar_accounts_refresh_clio_batch(req: dict[str, Any], request: Request):
        require_admin_auth(request)
        payload = req or {}
        try:
            limit = int(payload.get("limit") or 10)
        except Exception:
            limit = 10
        limit = max(1, min(limit, 50))
        only_missing_clio = bool(payload.get("only_missing_clio", True))
        explicit_ids = payload.get("account_ids") or payload.get("accountIds") or []
        wanted_ids = {
            str(x or "").strip()
            for x in explicit_ids
            if str(x or "").strip()
        } if isinstance(explicit_ids, list) else set()

        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for account in adobe_account_manager.list_accounts():
            aid = str(account.get("id") or "").strip()
            if wanted_ids and aid not in wanted_ids:
                continue
            if only_missing_clio and _account_has_active_clio(account):
                skipped.append({"account_id": aid, "email": account.get("email"), "reason": "already has active clio token"})
                continue
            profile_id = _profile_id_for_account(account)
            if not profile_id:
                skipped.append({"account_id": aid, "email": account.get("email"), "reason": "no refresh profile id"})
                continue
            candidates.append(account)
            if len(candidates) >= limit:
                break

        items: list[dict[str, Any]] = []
        for account in candidates:
            try:
                items.append(registrar_account_refresh_clio_token(str(account.get("id") or ""), request))
            except HTTPException as exc:
                items.append(
                    {
                        "status": "failed",
                        "account_id": account.get("id"),
                        "email": account.get("email"),
                        "status_code": exc.status_code,
                        "detail": str(exc.detail),
                    }
                )
            except Exception as exc:
                items.append(
                    {
                        "status": "failed",
                        "account_id": account.get("id"),
                        "email": account.get("email"),
                        "status_code": 0,
                        "detail": str(exc),
                    }
                )
        success_count = sum(1 for item in items if str(item.get("status") or "") == "ok")
        return {
            "status": "ok" if success_count == len(items) else "partial",
            "limit": limit,
            "only_missing_clio": only_missing_clio,
            "candidate_count": len(candidates),
            "success_count": success_count,
            "failed_count": len(items) - success_count,
            "skipped_count": len(skipped),
            "items": items,
            "skipped": skipped,
        }

    def _plan_account_image_tests(payload: dict[str, Any]) -> dict[str, Any]:
        only_failed = bool(payload.get("only_failed", True))
        model = str(payload.get("model") or "firefly-nano-banana-1k-1x1").strip()
        try:
            limit = int(payload.get("limit") or 5)
        except Exception:
            limit = 5
        limit = max(1, min(limit, 50))
        explicit_ids = payload.get("account_ids") or payload.get("accountIds") or []
        wanted_ids = {
            str(x or "").strip()
            for x in explicit_ids
            if str(x or "").strip()
        } if isinstance(explicit_ids, list) else set()
        preferred = "projectx_webapp" if model == "gpt-image-2" else "clio-playground-web"

        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        counters = {
            "total_accounts": 0,
            "matched_scope": 0,
            "passed_skipped": 0,
            "no_token": 0,
            "clio_ready": 0,
            "projectx_only": 0,
            "selected": 0,
        }
        for account in adobe_account_manager.list_accounts():
            counters["total_accounts"] += 1
            account_id_val = str(account.get("id") or "").strip()
            email = str(account.get("email") or "").strip()
            if wanted_ids and account_id_val not in wanted_ids:
                continue
            counters["matched_scope"] += 1
            token_summary = _token_summary_for_account_email(email)
            image_status = str(account.get("image_status") or "").strip().lower()
            if only_failed and image_status in {"passed", "ok", "success", "succeeded"}:
                counters["passed_skipped"] += 1
                skipped.append(
                    {
                        "account_id": account_id_val,
                        "email": email,
                        "image_status": image_status,
                        "reason": f"skip image_status={image_status}",
                        "token_summary": token_summary,
                    }
                )
                continue

            matches = _tokens_for_account_email(email, prefer_client_id=preferred)
            if not matches:
                counters["no_token"] += 1
                skipped.append(
                    {
                        "account_id": account_id_val,
                        "email": email,
                        "image_status": image_status,
                        "reason": "no active matched token",
                        "token_summary": token_summary,
                    }
                )
                continue
            selected_token = matches[0]
            selected_client_id = str(selected_token.get("client_id") or "").strip()
            active_clients = token_summary.get("active_by_client_id") or {}
            if active_clients.get("clio-playground-web"):
                counters["clio_ready"] += 1
            elif active_clients.get("projectx_webapp"):
                counters["projectx_only"] += 1
            if len(candidates) < limit:
                candidates.append(
                    {
                        "account_id": account_id_val,
                        "email": email,
                        "image_status": image_status,
                        "selected_token_id": str(selected_token.get("id") or ""),
                        "selected_client_id": selected_client_id,
                        "token_summary": token_summary,
                    }
                )

        counters["selected"] = len(candidates)
        return {
            "model": model,
            "only_failed": only_failed,
            "limit": limit,
            "preferred_client_id": preferred,
            "counters": counters,
            "candidates": candidates,
            "skipped": skipped,
        }

    @router.post("/api/v1/registrar/accounts/{account_id}/image-test")
    @router.post("/api/v1/adobe/accounts/{account_id}/image-test")
    def registrar_account_image_test(account_id: str, req: dict[str, Any], request: Request):
        require_admin_auth(request)
        try:
            account = adobe_account_manager.get_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="account not found")

        model = str((req or {}).get("model") or "firefly-nano-banana-1k-1x1").strip()
        prompt = str(
            (req or {}).get("prompt")
            or f"acceptance test image for {account.get('email')}: a small blue crystal cube on white background"
        ).strip()
        size = str((req or {}).get("size") or "1024x1024").strip() or "1024x1024"
        is_gpt = model == "gpt-image-2"
        prefer_client_id = "projectx_webapp" if is_gpt else "clio-playground-web"
        token_id = str((req or {}).get("token_id") or (req or {}).get("tokenId") or "").strip()
        token_item: dict[str, Any] | None = None
        if token_id:
            raw_item = token_manager.get_by_id(token_id)
            if not raw_item:
                raise HTTPException(status_code=400, detail=f"token not found: {token_id}")
            if str(raw_item.get("status") or "").strip() != "active":
                raise HTTPException(status_code=400, detail=f"token is not active: {token_id}")
            token_item = dict(raw_item)
            token_item["token"] = str(raw_item.get("value") or "").strip()
            token_item["client_id"] = _token_client_id(raw_item)
        else:
            matches = _tokens_for_account_email(str(account.get("email") or ""), prefer_client_id=prefer_client_id)
            if not matches:
                patch = {
                    "image_status": "failed",
                    "image_test_error": "no active token matched this account email",
                    "token_status": "missing",
                    "last_action": "账号出图测试失败：未找到 active token",
                }
                updated = adobe_account_manager.update_account(account_id, patch)
                return {
                    "status": "failed",
                    "account": updated,
                    "detail": patch["image_test_error"],
                    "matched_tokens": 0,
                }
            token_item = matches[0]
            token_id = str(token_item.get("id") or "").strip()

        client_id = str((token_item or {}).get("client_id") or "").strip()
        if is_gpt and client_id and client_id != "projectx_webapp":
            raise HTTPException(
                status_code=400,
                detail=f"gpt-image-2 requires projectx_webapp token, got {client_id}",
            )

        base_url = str(config_manager.get("public_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            base_url = "http://127.0.0.1:6001"
        body = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "url",
        }
        headers = {
            "Authorization": f"Bearer {str(config_manager.get('api_key', 'your-api-key') or 'your-api-key')}",
            "Content-Type": "application/json",
            "x-adobe-token-id": token_id,
        }
        started = time.time()
        try:
            resp = requests.post(
                f"{base_url}/v1/images/generations",
                headers=headers,
                json=body,
                timeout=max(60, int(config_manager.get("generate_timeout", 300) or 300) + 90),
            )
            try:
                payload = resp.json()
            except Exception:
                payload = {"text": resp.text[:2000]}
        except Exception as exc:
            payload = {"error": {"message": str(exc), "type": "connection_error"}}
            resp = None

        image_url = ""
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            image_url = str(data[0].get("url") or "").strip()
        ok = bool(resp is not None and 200 <= int(resp.status_code) < 300 and image_url)
        status_code = int(resp.status_code) if resp is not None else 0
        error_text = ""
        if not ok:
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    error_text = str(err.get("message") or err)[:1000]
                else:
                    error_text = json.dumps(payload, ensure_ascii=False)[:1000]
            else:
                error_text = str(payload)[:1000]
            try:
                rows, _ = log_store.list(limit=30, page=1)
                for row in rows:
                    if str(row.get("path") or "") != "/v1/images/generations":
                        continue
                    if str(row.get("token_id") or "") != token_id:
                        continue
                    if str(row.get("model") or "") != model:
                        continue
                    if float(row.get("ts") or 0) < started - 2:
                        continue
                    row_error = str(row.get("error") or "").strip()
                    if row_error:
                        error_text = row_error[:1000]
                        break
            except Exception:
                pass
        report_dir = Path(__file__).resolve().parents[2] / "data" / "account_image_tests"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{account_id}_{int(time.time())}.json"
        report = {
            "account_id": account_id,
            "email": account.get("email"),
            "token_id": token_id,
            "client_id": client_id,
            "model": model,
            "prompt": prompt,
            "size": size,
            "status_code": status_code,
            "ok": ok,
            "image_url": image_url,
            "duration_sec": round(time.time() - started, 3),
            "response": payload,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        patch = {
            "image_status": "passed" if ok else "failed",
            "image_test_url": image_url,
            "image_test_error": "" if ok else error_text,
            "image_test_token_id": token_id,
            "image_test_report_path": str(report_path),
            "token_status": "active",
            "last_action": "账号出图测试成功" if ok else f"账号出图测试失败：HTTP {status_code}",
        }
        updated = adobe_account_manager.update_account(account_id, patch)
        return {
            "status": "ok" if ok else "failed",
            "account": updated,
            "token_id": token_id,
            "client_id": client_id,
            "model": model,
            "status_code": status_code,
            "image_url": image_url,
            "error": error_text,
            "report_path": str(report_path),
            "duration_sec": report["duration_sec"],
        }

    @router.post("/api/v1/registrar/accounts/image-test-batch")
    @router.post("/api/v1/adobe/accounts/image-test-batch")
    def registrar_accounts_image_test_batch(req: dict[str, Any], request: Request):
        require_admin_auth(request)
        payload = req or {}
        model = str(payload.get("model") or "firefly-nano-banana-1k-1x1").strip()
        plan = _plan_account_image_tests(payload)
        candidate_ids = {
            str(item.get("account_id") or "") for item in (plan.get("candidates") or [])
        }
        candidates = [
            account
            for account in adobe_account_manager.list_accounts()
            if str(account.get("id") or "") in candidate_ids
        ]
        skipped: list[dict[str, Any]] = list(plan.get("skipped") or [])

        items: list[dict[str, Any]] = []
        for account in candidates:
            account_id_val = str(account.get("id") or "").strip()
            try:
                result = registrar_account_image_test(
                    account_id_val,
                    {
                        "model": model,
                        "size": str(payload.get("size") or "1024x1024"),
                        "prompt": str(
                            payload.get("prompt")
                            or f"batch account acceptance test for {account.get('email')}: a small blue crystal cube"
                        ),
                    },
                    request,
                )
                items.append(result)
            except HTTPException as exc:
                items.append(
                    {
                        "status": "failed",
                        "account_id": account_id_val,
                        "email": account.get("email"),
                        "status_code": exc.status_code,
                        "error": str(exc.detail),
                    }
                )
            except Exception as exc:
                items.append(
                    {
                        "status": "failed",
                        "account_id": account_id_val,
                        "email": account.get("email"),
                        "status_code": 0,
                        "error": str(exc),
                    }
                )
        success_count = sum(1 for item in items if str(item.get("status") or "") == "ok")
        return {
            "status": "ok" if success_count == len(items) else "partial",
            "plan": plan,
            "tested_count": len(items),
            "success_count": success_count,
            "failed_count": len(items) - success_count,
            "skipped_count": len(skipped),
            "items": items,
            "skipped": skipped,
        }

    @router.post("/api/v1/registrar/accounts/image-test-plan")
    @router.post("/api/v1/adobe/accounts/image-test-plan")
    def registrar_accounts_image_test_plan(req: dict[str, Any], request: Request):
        require_admin_auth(request)
        plan = _plan_account_image_tests(req or {})
        return {"status": "ok", **plan}

    def _persist_account_job_snapshot(job_id: str, snapshot: dict[str, Any]) -> None:
        jobs_dir = Path(__file__).resolve().parents[2] / "data" / "account_maintenance_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / f"{job_id}.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @router.post("/api/v1/registrar/account-jobs")
    @router.post("/api/v1/adobe/account-jobs")
    def registrar_account_job_start(req: dict[str, Any], request: Request):
        """Run account-maintenance actions in the background so the registrar UI can stream logs."""
        require_admin_auth(request)
        payload = req or {}
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"refresh-clio-batch", "image-test-batch"}:
            raise HTTPException(status_code=400, detail="action must be refresh-clio-batch or image-test-batch")

        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "action": action,
            "status": "running",
            "total": 0,
            "current": 0,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "started_at": int(time.time()),
            "updated_at": int(time.time()),
            "finished_at": None,
            "logs": [],
            "items": [],
            "skipped": [],
            "plan": None,
            "error": "",
        }
        with account_jobs_lock:
            account_jobs[job_id] = job

        def _append(line: str) -> None:
            with account_jobs_lock:
                target = account_jobs.get(job_id)
                if not target:
                    return
                logs = target.setdefault("logs", [])
                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}".rstrip())
                del logs[:-3000]
                target["updated_at"] = int(time.time())

        def _patch(**kwargs: Any) -> None:
            with account_jobs_lock:
                target = account_jobs.get(job_id)
                if not target:
                    return
                target.update(kwargs)
                target["updated_at"] = int(time.time())

        def _add_item(item: dict[str, Any]) -> None:
            with account_jobs_lock:
                target = account_jobs.get(job_id)
                if not target:
                    return
                target.setdefault("items", []).append(item)
                if str(item.get("status") or "").lower() == "ok":
                    target["success_count"] = int(target.get("success_count") or 0) + 1
                else:
                    target["failed_count"] = int(target.get("failed_count") or 0) + 1
                target["updated_at"] = int(time.time())

        def _finish(status: str, error: str = "") -> None:
            with account_jobs_lock:
                target = account_jobs.get(job_id)
                if not target:
                    return
                target["status"] = status
                target["error"] = error
                target["finished_at"] = int(time.time())
                target["updated_at"] = int(time.time())
                snapshot = dict(target)
            try:
                _persist_account_job_snapshot(job_id, snapshot)
            except Exception:
                pass

        def _refresh_runner() -> None:
            try:
                _append(f"ACCOUNT_JOB_START id={job_id} action=refresh-clio-batch")
                only_missing_clio = bool(payload.get("only_missing_clio", True))
                try:
                    limit = int(payload.get("limit") or 10)
                except Exception:
                    limit = 10
                limit = max(1, min(limit, 50))
                explicit_ids = payload.get("account_ids") or payload.get("accountIds") or []
                wanted_ids = {
                    str(x or "").strip()
                    for x in explicit_ids
                    if str(x or "").strip()
                } if isinstance(explicit_ids, list) else set()

                candidates: list[dict[str, Any]] = []
                skipped: list[dict[str, Any]] = []
                for account in adobe_account_manager.list_accounts():
                    aid = str(account.get("id") or "").strip()
                    if wanted_ids and aid not in wanted_ids:
                        continue
                    if only_missing_clio and _account_has_active_clio(account):
                        skipped.append({"account_id": aid, "email": account.get("email"), "reason": "already has active clio token"})
                        continue
                    profile_id = _profile_id_for_account(account)
                    if not profile_id:
                        skipped.append({"account_id": aid, "email": account.get("email"), "reason": "no refresh profile id"})
                        continue
                    candidates.append(account)
                    if len(candidates) >= limit:
                        break

                _patch(total=len(candidates), skipped=skipped, skipped_count=len(skipped))
                _append(f"REFRESH_CLIO_BATCH_PLAN candidates={len(candidates)} skipped={len(skipped)} only_missing_clio={only_missing_clio}")
                for item in skipped[:20]:
                    _append(f"REFRESH_CLIO_SKIPPED email={item.get('email') or '-'} reason={item.get('reason')}")
                for idx, account in enumerate(candidates, start=1):
                    account_id_val = str(account.get("id") or "").strip()
                    email = str(account.get("email") or "").strip()
                    _patch(current=idx)
                    _append(f"REFRESH_CLIO_START #{idx}/{len(candidates)} account={account_id_val} email={email or '-'}")
                    try:
                        result = registrar_account_refresh_clio_token(account_id_val, request)
                    except Exception as exc:
                        result = {
                            "status": "failed",
                            "account_id": account_id_val,
                            "email": email,
                            "detail": str(exc),
                        }
                    account_row = result.get("account") if isinstance(result, dict) else {}
                    token_summary = result.get("token_summary") if isinstance(result, dict) else {}
                    item = {
                        "status": str(result.get("status") or "failed") if isinstance(result, dict) else "failed",
                        "account_id": account_id_val,
                        "email": str((account_row or {}).get("email") or email),
                        "profile_id": str(result.get("profile_id") or "") if isinstance(result, dict) else "",
                        "preferred_image_token_id": str((token_summary or {}).get("preferred_image_token_id") or ""),
                        "detail": str(result.get("detail") or "")[:1000] if isinstance(result, dict) else "",
                    }
                    _add_item(item)
                    _append(
                        f"REFRESH_CLIO_DONE status={item['status']} email={item['email'] or '-'} "
                        f"profile={item['profile_id'] or '-'} token={item['preferred_image_token_id'] or '-'}"
                    )
                _append("REFRESH_CLIO_BATCH_DONE")
                _finish("succeeded")
            except Exception as exc:
                _append(f"REFRESH_CLIO_BATCH_FAILED {exc}")
                _finish("failed", str(exc))

        def _image_runner() -> None:
            try:
                _append(f"ACCOUNT_JOB_START id={job_id} action=image-test-batch")
                model = str(payload.get("model") or "firefly-nano-banana-1k-1x1").strip()
                size = str(payload.get("size") or "1024x1024").strip() or "1024x1024"
                prompt = str(payload.get("prompt") or "batch account acceptance test: a small blue crystal cube").strip()
                plan = _plan_account_image_tests(payload)
                candidate_ids = [str(item.get("account_id") or "") for item in (plan.get("candidates") or [])]
                by_id = {
                    str(account.get("id") or ""): account
                    for account in adobe_account_manager.list_accounts()
                }
                candidates = [by_id[aid] for aid in candidate_ids if aid in by_id]
                skipped = list(plan.get("skipped") or [])
                counters = plan.get("counters") if isinstance(plan.get("counters"), dict) else {}
                _patch(total=len(candidates), plan=plan, skipped=skipped, skipped_count=len(skipped))
                _append(
                    "IMAGE_TEST_BATCH_PLAN "
                    f"selected={len(candidates)} matched={counters.get('matched_scope', 0)} "
                    f"clio_ready={counters.get('clio_ready', 0)} projectx_only={counters.get('projectx_only', 0)} "
                    f"no_token={counters.get('no_token', 0)} passed_skipped={counters.get('passed_skipped', 0)}"
                )
                for item in (plan.get("candidates") or []):
                    _append(
                        f"IMAGE_TEST_CANDIDATE email={item.get('email') or '-'} "
                        f"token={item.get('selected_token_id') or '-'} client={item.get('selected_client_id') or '-'}"
                    )
                for item in skipped[:20]:
                    _append(f"IMAGE_TEST_SKIPPED email={item.get('email') or '-'} reason={item.get('reason')}")
                for idx, account in enumerate(candidates, start=1):
                    account_id_val = str(account.get("id") or "").strip()
                    email = str(account.get("email") or "").strip()
                    _patch(current=idx)
                    _append(f"IMAGE_TEST_START #{idx}/{len(candidates)} account={account_id_val} email={email or '-'}")
                    try:
                        result = registrar_account_image_test(
                            account_id_val,
                            {"model": model, "size": size, "prompt": f"{prompt} for {email or account_id_val}"},
                            request,
                        )
                    except Exception as exc:
                        result = {
                            "status": "failed",
                            "account_id": account_id_val,
                            "email": email,
                            "status_code": 0,
                            "error": str(exc),
                        }
                    account_row = result.get("account") if isinstance(result, dict) else {}
                    item = {
                        "status": str(result.get("status") or "failed") if isinstance(result, dict) else "failed",
                        "account_id": account_id_val,
                        "email": str((account_row or {}).get("email") or email),
                        "token_id": str(result.get("token_id") or (account_row or {}).get("image_test_token_id") or "") if isinstance(result, dict) else "",
                        "status_code": int(result.get("status_code") or 0) if isinstance(result, dict) else 0,
                        "image_url": str(result.get("image_url") or (account_row or {}).get("image_test_url") or "") if isinstance(result, dict) else "",
                        "error": str(result.get("error") or (account_row or {}).get("image_test_error") or "")[:1000] if isinstance(result, dict) else "",
                        "report_path": str(result.get("report_path") or (account_row or {}).get("image_test_report_path") or "") if isinstance(result, dict) else "",
                    }
                    _add_item(item)
                    _append(
                        f"IMAGE_TEST_DONE status={item['status']} http={item['status_code']} "
                        f"email={item['email'] or '-'} token={item['token_id'] or '-'} url={item['image_url'] or '-'}"
                    )
                    if item["error"]:
                        _append(f"IMAGE_TEST_ERROR email={item['email'] or '-'} error={item['error']}")
                _append("IMAGE_TEST_BATCH_DONE")
                _finish("succeeded")
            except Exception as exc:
                _append(f"IMAGE_TEST_BATCH_FAILED {exc}")
                _finish("failed", str(exc))

        threading.Thread(
            target=_refresh_runner if action == "refresh-clio-batch" else _image_runner,
            daemon=True,
        ).start()
        return {"status": "running", "job_id": job_id, "action": action}

    @router.get("/api/v1/registrar/account-jobs/{job_id}")
    @router.get("/api/v1/adobe/account-jobs/{job_id}")
    def registrar_account_job_status(job_id: str, request: Request):
        require_admin_auth(request)
        with account_jobs_lock:
            job = account_jobs.get(job_id)
            if job:
                return dict(job)
        job_path = Path(__file__).resolve().parents[2] / "data" / "account_maintenance_jobs" / f"{job_id}.json"
        if job_path.exists():
            try:
                return json.loads(job_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        raise HTTPException(status_code=404, detail="job not found")

    @router.get("/api/v1/registrar/account-jobs")
    @router.get("/api/v1/adobe/account-jobs")
    def registrar_account_jobs_list(request: Request, limit: int = 20):
        require_admin_auth(request)
        safe_limit = max(1, min(int(limit or 20), 100))
        with account_jobs_lock:
            by_id = {str(item.get("id") or ""): dict(item) for item in account_jobs.values()}
        jobs_dir = Path(__file__).resolve().parents[2] / "data" / "account_maintenance_jobs"
        if jobs_dir.exists():
            for path in sorted(jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:safe_limit]:
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                jid = str(item.get("id") or path.stem)
                by_id.setdefault(jid, item)
        rows = sorted(
            by_id.values(),
            key=lambda item: int(item.get("updated_at") or item.get("started_at") or 0),
            reverse=True,
        )[:safe_limit]
        return {"status": "ok", "jobs": rows}

    def _account_export_rows() -> list[dict[str, Any]]:
        rows = adobe_account_manager.list_accounts()
        normalized: list[dict[str, Any]] = []
        for item in rows:
            normalized.append(
                {
                    "id": item.get("id", ""),
                    "email": item.get("email", ""),
                    "password": item.get("password", ""),
                    "status": item.get("status", ""),
                    "eligibility": item.get("eligibility", ""),
                    "plan": item.get("plan", ""),
                    "image_status": item.get("image_status", ""),
                    "image_test_url": item.get("image_test_url", ""),
                    "image_test_error": item.get("image_test_error", ""),
                    "image_test_token_id": item.get("image_test_token_id", ""),
                    "image_test_report_path": item.get("image_test_report_path", ""),
                    "web_image_status": item.get("web_image_status", ""),
                    "web_image_test_url": item.get("web_image_test_url", ""),
                    "web_image_test_error": item.get("web_image_test_error", ""),
                    "email_provider": item.get("email_provider", ""),
                    "mail_status": item.get("mail_status", ""),
                    "mail_token": item.get("mail_token", ""),
                    "verification_code": item.get("verification_code", ""),
                    "verification_link": item.get("verification_link", ""),
                    "session_state_path": item.get("session_state_path", ""),
                    "cookie_profile_id": item.get("cookie_profile_id", ""),
                    "token_status": item.get("token_status", ""),
                    "ip": item.get("ip", ""),
                    "created_at": item.get("created_at", ""),
                    "updated_at": item.get("updated_at", ""),
                    "last_action": item.get("last_action", ""),
                }
            )
        return normalized

    @router.get("/api/v1/adobe/accounts/export")
    @router.get("/api/v1/registrar/accounts/export")
    def adobe_accounts_export(request: Request, format: str = "json", save: bool = True):
        require_admin_auth(request)
        rows = _account_export_rows()
        export_dir = Path(__file__).resolve().parents[2] / "data" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fmt = str(format or "json").strip().lower()
        if fmt == "csv":
            output = io.StringIO()
            fieldnames = list(rows[0].keys()) if rows else [
                "id",
                "email",
                "password",
                "status",
                "eligibility",
                "plan",
                "image_status",
                "image_test_url",
                "mail_status",
                "token_status",
                "last_action",
            ]
            writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            content = output.getvalue()
            filename = f"adobe_accounts_{timestamp}.csv"
            if save:
                (export_dir / filename).write_text(content, encoding="utf-8-sig")
            return Response(
                content,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        payload = {
            "status": "ok",
            "exported_at": int(time.time()),
            "count": len(rows),
            "accounts": rows,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        filename = f"adobe_accounts_{timestamp}.json"
        if save:
            (export_dir / filename).write_text(content, encoding="utf-8")
        return Response(
            content,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _cookie_payload_for_account(account: dict[str, Any]) -> dict[str, Any]:
        email = str(account.get("email") or "").strip()
        storage_path = str(account.get("session_state_path") or "").strip()
        candidates: list[Path] = []
        if storage_path:
            p = Path(storage_path)
            name = p.name
            if name.endswith(".storage.json"):
                candidates.append(p.with_name(name[: -len(".storage.json")] + ".cookies.json"))
            candidates.append(p.with_suffix(".cookies.json"))
        safe_email = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in email)[:120]
        sessions_dir = Path(__file__).resolve().parents[2] / "data" / "adobe_sessions"
        if safe_email:
            candidates.extend(sorted(sessions_dir.glob(f"{safe_email}*.cookies.json")))
        candidates.extend(
            path
            for path in sorted(sessions_dir.glob("*.cookies.json"), key=lambda x: x.stat().st_mtime, reverse=True)
            if email and email.replace("@", "_").split("_", 1)[0] in path.name
        )
        source_path = None
        raw = {}
        for path in candidates:
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                source_path = path
                break
            except Exception:
                continue
        if not raw:
            raise FileNotFoundError("cookie file not found")
        cookie_string = str(raw.get("cookie") or "").strip()
        cookies = raw.get("cookies") if isinstance(raw.get("cookies"), list) else []
        headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
        if not cookie_string and cookies:
            pairs = []
            for item in cookies:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name:
                    pairs.append(f"{name}={value}")
            cookie_string = "; ".join(pairs)
        cookie_input = {
            "cookie": cookie_string,
            "cookies": cookies,
            "headers": headers,
            "email": email,
            "display_name": "Li Ming",
        }
        return {
            "name": email or str(account.get("id") or ""),
            "account_id": account.get("id", ""),
            "email": email,
            "cookie": cookie_input,
            "source_path": str(source_path or ""),
            "import_example": {
                "endpoint": "/api/v1/refresh-profiles/import-cookie",
                "body": {"name": email, "cookie": cookie_input},
            },
        }

    @router.get("/api/v1/registrar/accounts/{account_id}/cookie-export")
    @router.get("/api/v1/adobe/accounts/{account_id}/cookie-export")
    def registrar_account_cookie_export(account_id: str, request: Request, download: bool = False):
        require_admin_auth(request)
        try:
            account = adobe_account_manager.get_account(account_id)
            payload = {"status": "ok", **_cookie_payload_for_account(account)}
        except KeyError:
            raise HTTPException(status_code=404, detail="account not found")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        if download:
            filename = f"adobe_cookie_{account_id}.json"
            return Response(
                content,
                media_type="application/json; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return Response(content, media_type="application/json; charset=utf-8")

    @router.get("/api/v1/registrar/accounts/cookies-export")
    @router.get("/api/v1/adobe/accounts/cookies-export")
    def registrar_accounts_cookies_export(request: Request, download: bool = False):
        require_admin_auth(request)
        items = []
        failed = []
        for account in adobe_account_manager.list_accounts():
            try:
                item = _cookie_payload_for_account(account)
                items.append({"name": item["name"], "cookie": item["cookie"], "account_id": item["account_id"], "email": item["email"]})
            except Exception as exc:
                failed.append({"account_id": account.get("id"), "email": account.get("email"), "detail": str(exc)})
        payload = {
            "status": "ok" if not failed else "partial",
            "total": len(items),
            "failed_count": len(failed),
            "items": items,
            "failed": failed,
            "import_endpoint": "/api/v1/refresh-profiles/import-cookie-batch",
            "import_body": {"items": [{"name": x["name"], "cookie": x["cookie"]} for x in items]},
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        if download:
            filename = f"adobe_cookies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            return Response(
                content,
                media_type="application/json; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return Response(content, media_type="application/json; charset=utf-8")

    @router.post("/api/v1/registrar/accounts/{account_id}/verification-code")
    @router.get("/api/v1/registrar/accounts/{account_id}/verification-code")
    def registrar_account_verification_code(
        account_id: str, request: Request, wait_seconds: int = 0
    ):
        require_admin_auth(request)
        deadline = time.time() + max(0, min(int(wait_seconds or 0), 180))
        last_result: dict[str, Any] = {}
        while True:
            try:
                result = adobe_account_manager.fetch_account_emails(
                    account_id,
                    tempmail_api_key=str(config_manager.get("tempmail_lol_api_key", "") or ""),
                    tempmail_proxy=(
                        str(config_manager.get("proxy", "") or "").strip()
                        if bool(config_manager.get("use_proxy", False))
                        else ""
                    ),
                )
                last_result = result
            except KeyError:
                raise HTTPException(status_code=404, detail="account not found")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            verification = result.get("verification") if isinstance(result, dict) else {}
            account = result.get("account") if isinstance(result, dict) else {}
            code = str((verification or {}).get("code") or (account or {}).get("verification_code") or "").strip()
            link = str((verification or {}).get("link") or (account or {}).get("verification_link") or "").strip()
            if code or link or time.time() >= deadline:
                return {
                    "status": "ok" if (code or link) else "pending",
                    "account": account,
                    "code": code,
                    "link": link,
                    "email_count": len(result.get("emails") or []) if isinstance(result, dict) else 0,
                    "verification": verification or {},
                }
            time.sleep(5)

    @router.get("/api/v1/payment-cards")
    def payment_cards_list(request: Request, include_sensitive: bool = False):
        require_admin_auth(request)
        return {
            "status": "ok",
            "cards": card_manager.list_cards(include_sensitive=include_sensitive),
            "logs": card_manager.list_logs(),
        }

    @router.post("/api/v1/payment-cards")
    def payment_card_upsert(req: PaymentCardUpsertRequest, request: Request):
        require_admin_auth(request)
        try:
            card = card_manager.upsert_card(req.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "ok", "card": card}

    @router.post("/api/v1/payment-cards/import")
    def payment_cards_import(req: PaymentCardsImportRequest, request: Request):
        require_admin_auth(request)
        try:
            result = card_manager.import_cards(req.cards)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "ok" if not result.get("failed_count") else "partial", **result}

    @router.delete("/api/v1/payment-cards/{card_id}")
    def payment_card_delete(card_id: str, request: Request):
        require_admin_auth(request)
        if not card_manager.delete_card(card_id):
            raise HTTPException(status_code=404, detail="card not found")
        return {"status": "ok"}

    @router.get("/api/v1/payment-cards/sources")
    def payment_card_sources(request: Request):
        require_admin_auth(request)
        return {
            "status": "ok",
            "sources": [
                {
                    "type": "own_card",
                    "name": "本人/公司名下信用卡或借记卡",
                    "use_case": "真实订阅/试用开通，账单地址必须和发卡行记录一致。",
                },
                {
                    "type": "bank_virtual_card",
                    "name": "银行官方虚拟卡",
                    "use_case": "由开户银行或信用卡 App 发放的一次性/限额虚拟卡。",
                },
                {
                    "type": "business_virtual_card",
                    "name": "企业支出管理虚拟卡",
                    "use_case": "公司账户下的员工/项目虚拟卡，可设置限额、地区、商户。",
                },
                {
                    "type": "prepaid_card",
                    "name": "合规预付卡",
                    "use_case": "可用于线上订阅的实名预付卡；部分地区/商户会拒绝。",
                },
                {
                    "type": "processor_test_card",
                    "name": "支付处理器沙盒测试卡",
                    "use_case": "只用于沙盒/测试环境，不能用于真实 Adobe 付款。",
                },
            ],
        }

    @router.get("/api/v1/logs")
    def list_logs(request: Request, limit: int = 20, page: int = 1):
        require_admin_auth(request)
        logs, total = log_store.list(limit=limit, page=page)
        safe_limit = min(max(int(limit or 20), 1), 100)
        safe_page = max(int(page or 1), 1)
        total_pages = (total + safe_limit - 1) // safe_limit if total > 0 else 1
        if safe_page > total_pages:
            safe_page = total_pages
        return {
            "logs": logs,
            "page": safe_page,
            "limit": safe_limit,
            "total": total,
            "total_pages": total_pages,
        }

    @router.get("/api/v1/logs/errors/{code}")
    def get_error_detail(code: str, request: Request):
        require_admin_auth(request)
        item = error_store.get(code)
        if not item:
            raise HTTPException(status_code=404, detail="error code not found")
        return item

    @router.get("/api/v1/logs/running")
    def list_running_logs(request: Request, limit: int = 200):
        require_admin_auth(request)
        rows = live_log_store.list(limit=limit)
        items = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            status = str(item.get("task_status") or "").upper()
            if status != "IN_PROGRESS":
                continue
            items.append(item)
        return {"items": items, "total": len(items)}

    def _resolve_logs_stats_range(range_key: str) -> tuple[str, float, float]:
        now_dt = datetime.now()
        now_ts = time.time()
        key = str(range_key or "today").strip().lower()
        if key == "today":
            start_dt = datetime(now_dt.year, now_dt.month, now_dt.day)
        elif key == "7d":
            start_dt = now_dt - timedelta(days=7)
        elif key == "30d":
            start_dt = now_dt - timedelta(days=30)
        else:
            raise HTTPException(
                status_code=400, detail="range must be one of: today, 7d, 30d"
            )
        return key, start_dt.timestamp(), now_ts

    @router.get("/api/v1/logs/stats")
    def logs_stats(request: Request, range: str = "today"):
        require_admin_auth(request)
        range_key, start_ts, end_ts = _resolve_logs_stats_range(range)
        payload = log_store.stats(start_ts=start_ts, end_ts=end_ts)
        payload["in_progress_requests"] = live_log_store.count_in_progress()
        payload.update({"range": range_key, "start_ts": start_ts, "end_ts": end_ts})
        return payload

    @router.delete("/api/v1/logs")
    def clear_logs(request: Request):
        require_admin_auth(request)
        log_store.clear()
        return {"status": "ok"}

    @router.get("/api/v1/tokens")
    def list_tokens(request: Request):
        require_admin_auth(request)
        tokens = token_manager.list_all()
        for item in tokens:
            if not bool(item.get("auto_refresh")):
                item["auto_refresh_enabled"] = None
                continue
            pid = str(item.get("refresh_profile_id") or "").strip()
            item["auto_refresh_enabled"] = refresh_manager.is_profile_enabled(pid)
        total_count = len(tokens)
        active_count = 0
        credits_available_total = 0.0
        for item in tokens:
            if str(item.get("status") or "").strip().lower() == "active":
                active_count += 1
            try:
                available = item.get("credits_available")
                if available is not None:
                    credits_available_total += float(available)
            except Exception:
                pass
        return {
            "tokens": tokens,
            "summary": {
                "total": total_count,
                "active": active_count,
                "credits_available_total": credits_available_total,
            },
        }

    @router.post("/api/v1/tokens")
    def add_token(req: TokenAddRequest, request: Request):
        require_admin_auth(request)
        if not req.token.strip():
            raise HTTPException(status_code=400, detail="Empty token")
        meta: dict[str, Any] = {}
        for key in (
            "source",
            "refresh_profile_id",
            "refresh_profile_name",
            "refresh_profile_email",
            "refresh_client_id",
            "account_id",
        ):
            value = getattr(req, key, None)
            if value is None:
                continue
            text = str(value or "").strip()
            if text:
                meta[key] = text
        if req.auto_refresh is not None:
            meta["auto_refresh"] = bool(req.auto_refresh)
        token = token_manager.add(req.token, meta=meta)
        safe_token = dict(token)
        value = str(safe_token.get("value") or "")
        safe_token["value"] = (
            value[:15] + "..." + value[-10:] if len(value) > 30 else "***"
        )
        return {"status": "ok", "token": safe_token}

    @router.post("/api/v1/tokens/batch")
    def add_tokens_batch(req: TokenBatchAddRequest, request: Request):
        require_admin_auth(request)
        if not req.tokens:
            raise HTTPException(status_code=400, detail="tokens is required")

        added_count = 0
        for raw in req.tokens:
            token = str(raw or "").strip()
            if not token:
                continue
            token_manager.add(token)
            added_count += 1

        if added_count == 0:
            raise HTTPException(status_code=400, detail="no valid token provided")

        return {"status": "ok", "added_count": added_count}

    @router.post("/api/v1/tokens/export")
    def export_tokens(req: ExportSelectionRequest, request: Request):
        require_admin_auth(request)
        token_ids = req.ids if isinstance(req.ids, list) else None
        exported = token_manager.export_tokens(token_ids)
        return {
            "status": "ok",
            "total": len(exported),
            "selected": bool(token_ids),
            "tokens": exported,
        }

    @router.post("/api/v1/tokens/delete-batch")
    def delete_tokens_batch(req: ExportSelectionRequest, request: Request):
        require_admin_auth(request)
        token_ids = req.ids if isinstance(req.ids, list) else None
        normalized_ids = [
            str(x or "").strip() for x in (token_ids or []) if str(x or "").strip()
        ]
        if not normalized_ids:
            raise HTTPException(status_code=400, detail="ids is required")

        deleted = []
        missing = []
        for tid in normalized_ids:
            if delete_token_and_linked_profile(tid):
                deleted.append(tid)
            else:
                missing.append(tid)

        if not deleted:
            raise HTTPException(status_code=404, detail="no token deleted")

        return {
            "status": "ok" if not missing else "partial",
            "deleted_count": len(deleted),
            "missing_count": len(missing),
            "deleted_ids": deleted,
            "missing_ids": missing,
        }

    @router.delete("/api/v1/tokens/{tid}")
    def delete_token(tid: str, request: Request):
        require_admin_auth(request)
        if not delete_token_and_linked_profile(tid):
            raise HTTPException(status_code=404, detail="token not found")
        return {"status": "ok"}

    @router.put("/api/v1/tokens/{tid}/status")
    def set_token_status(tid: str, status: str, request: Request):
        require_admin_auth(request)
        if status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="Invalid status")
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")
        if status == "active" and token_info.get("status") in {"exhausted", "invalid"}:
            raise HTTPException(
                status_code=400,
                detail="exhausted/invalid token cannot be reactivated; replace with a fresh token",
            )
        token_manager.set_status(tid, status)
        return {"status": "ok"}

    @router.post("/api/v1/tokens/{tid}/refresh")
    def refresh_token_now(tid: str, request: Request):
        require_admin_auth(request)
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")

        profile_id = str(token_info.get("refresh_profile_id") or "").strip()
        if not profile_id:
            raise HTTPException(
                status_code=400,
                detail="this token is not bound to an auto refresh profile",
            )

        try:
            result = refresh_manager.refresh_once(profile_id)
            return {"status": "ok", "result": result}
        except KeyError:
            raise HTTPException(status_code=404, detail="refresh profile not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/api/v1/tokens/{tid}/auto-refresh")
    def set_token_auto_refresh_enabled(tid: str, enabled: bool, request: Request):
        require_admin_auth(request)
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")

        profile_id = str(token_info.get("refresh_profile_id") or "").strip()
        if not profile_id:
            raise HTTPException(
                status_code=400,
                detail="this token is not bound to an auto refresh profile",
            )
        try:
            profile = refresh_manager.set_enabled(profile_id, bool(enabled))
            return {"status": "ok", "profile": profile}
        except KeyError:
            raise HTTPException(status_code=404, detail="refresh profile not found")

    @router.post("/api/v1/tokens/{tid}/credits/refresh")
    def refresh_token_credits(tid: str, request: Request):
        require_admin_auth(request)
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")
        try:
            result = refresh_manager.refresh_credits_for_token_id(tid)
            return {"status": "ok", **result}
        except KeyError:
            raise HTTPException(status_code=404, detail="token not found")
        except Exception as exc:
            token_manager.set_credits_error(tid, str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/v1/tokens/credits/refresh-batch")
    def refresh_tokens_credits_batch(
        req: TokenCreditsBatchRefreshRequest, request: Request
    ):
        require_admin_auth(request)
        ids = req.ids if isinstance(req.ids, list) else None
        token_ids: List[str] = []
        if ids:
            token_ids = [str(x or "").strip() for x in ids if str(x or "").strip()]
        else:
            token_ids = token_manager.list_active_ids()

        if not token_ids:
            raise HTTPException(status_code=400, detail="no token to refresh")

        refreshed = []
        failed = []
        max_workers = min(get_batch_concurrency(), len(token_ids))

        def refresh_one(index: int, tid: str):
            try:
                return index, "ok", refresh_manager.refresh_credits_for_token_id(tid)
            except Exception as exc:
                token_manager.set_credits_error(tid, str(exc))
                return index, "failed", {"token_id": tid, "detail": str(exc)}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(refresh_one, index, tid)
                for index, tid in enumerate(token_ids)
            ]
            done_items = [future.result() for future in as_completed(futures)]

        done_items.sort(key=lambda item: item[0])
        for _, status, payload in done_items:
            if status == "ok":
                refreshed.append(payload)
            else:
                failed.append(payload)

        return {
            "status": "ok" if not failed else "partial",
            "total": len(token_ids),
            "refreshed_count": len(refreshed),
            "failed_count": len(failed),
            "refreshed": refreshed,
            "failed": failed,
        }

    @router.get("/api/v1/config")
    def get_config(request: Request):
        require_admin_auth(request)
        cfg = config_manager.get_all()
        cfg.pop("admin_session_secret", None)
        try:
            cfg.update(get_generated_storage_stats())
        except Exception:
            pass
        return cfg

    @router.put("/api/v1/config")
    def update_config(req: ConfigUpdateRequest, request: Request):
        require_admin_auth(request)
        incoming = req.model_dump(exclude_unset=True)
        if not incoming:
            return config_manager.get_all()

        update_data = {}
        if "api_key" in incoming:
            update_data["api_key"] = str(incoming["api_key"] or "").strip()
        if "admin_username" in incoming:
            admin_username = str(incoming["admin_username"] or "").strip()
            if not admin_username:
                raise HTTPException(
                    status_code=400, detail="admin_username cannot be empty"
                )
            update_data["admin_username"] = admin_username
        if "admin_password" in incoming:
            admin_password = str(incoming["admin_password"] or "")
            if not admin_password:
                raise HTTPException(
                    status_code=400, detail="admin_password cannot be empty"
                )
            update_data["admin_password"] = admin_password
        if "public_base_url" in incoming:
            update_data["public_base_url"] = str(
                incoming["public_base_url"] or ""
            ).strip()
        if "proxy" in incoming:
            update_data["proxy"] = str(incoming["proxy"] or "").strip()
        if "use_proxy" in incoming:
            update_data["use_proxy"] = bool(incoming["use_proxy"])
        if "generate_timeout" in incoming:
            try:
                timeout_val = int(incoming["generate_timeout"])
            except Exception:
                timeout_val = 300
            update_data["generate_timeout"] = timeout_val if timeout_val > 0 else 300
        if "refresh_interval_hours" in incoming:
            try:
                interval_hours = int(incoming["refresh_interval_hours"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="refresh_interval_hours must be an integer between 1 and 24",
                )
            if interval_hours < 1 or interval_hours > 24:
                raise HTTPException(
                    status_code=400,
                    detail="refresh_interval_hours must be between 1 and 24",
                )
            update_data["refresh_interval_hours"] = interval_hours
        if "retry_enabled" in incoming:
            update_data["retry_enabled"] = bool(incoming["retry_enabled"])
        if "retry_max_attempts" in incoming:
            try:
                retry_max_attempts = int(incoming["retry_max_attempts"])
            except Exception:
                raise HTTPException(
                    status_code=400, detail="retry_max_attempts must be an integer"
                )
            if retry_max_attempts < 1 or retry_max_attempts > 10:
                raise HTTPException(
                    status_code=400,
                    detail="retry_max_attempts must be between 1 and 10",
                )
            update_data["retry_max_attempts"] = retry_max_attempts
        if "retry_backoff_seconds" in incoming:
            try:
                retry_backoff_seconds = float(incoming["retry_backoff_seconds"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="retry_backoff_seconds must be a number",
                )
            if retry_backoff_seconds < 0 or retry_backoff_seconds > 30:
                raise HTTPException(
                    status_code=400,
                    detail="retry_backoff_seconds must be between 0 and 30",
                )
            update_data["retry_backoff_seconds"] = retry_backoff_seconds
        if "retry_on_status_codes" in incoming:
            raw_codes = incoming["retry_on_status_codes"] or []
            if not isinstance(raw_codes, list):
                raise HTTPException(
                    status_code=400, detail="retry_on_status_codes must be a list"
                )
            status_codes: list[int] = []
            for item in raw_codes:
                try:
                    code = int(item)
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail="retry_on_status_codes contains invalid value",
                    )
                if code < 100 or code > 599:
                    raise HTTPException(
                        status_code=400,
                        detail="retry_on_status_codes must be HTTP status codes",
                    )
                status_codes.append(code)
            update_data["retry_on_status_codes"] = sorted(set(status_codes))
        if "retry_on_error_types" in incoming:
            raw_types = incoming["retry_on_error_types"] or []
            if not isinstance(raw_types, list):
                raise HTTPException(
                    status_code=400, detail="retry_on_error_types must be a list"
                )
            error_types: list[str] = []
            for item in raw_types:
                txt = str(item or "").strip().lower()
                if txt:
                    error_types.append(txt)
            update_data["retry_on_error_types"] = sorted(set(error_types))
        if "token_rotation_strategy" in incoming:
            strategy = str(incoming["token_rotation_strategy"] or "").strip().lower()
            if strategy not in {"round_robin", "random"}:
                raise HTTPException(
                    status_code=400,
                    detail="token_rotation_strategy must be one of: round_robin, random",
                )
            update_data["token_rotation_strategy"] = strategy
        if "batch_concurrency" in incoming:
            try:
                batch_concurrency = int(incoming["batch_concurrency"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="batch_concurrency must be an integer between 1 and 100",
                )
            if batch_concurrency < 1 or batch_concurrency > 100:
                raise HTTPException(
                    status_code=400,
                    detail="batch_concurrency must be between 1 and 100",
                )
            update_data["batch_concurrency"] = batch_concurrency
        if "generated_max_size_mb" in incoming:
            try:
                generated_max_size_mb = int(incoming["generated_max_size_mb"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="generated_max_size_mb must be an integer between 100 and 102400",
                )
            if generated_max_size_mb < 100 or generated_max_size_mb > 102400:
                raise HTTPException(
                    status_code=400,
                    detail="generated_max_size_mb must be between 100 and 102400",
                )
            update_data["generated_max_size_mb"] = generated_max_size_mb
        if "generated_prune_size_mb" in incoming:
            try:
                generated_prune_size_mb = int(incoming["generated_prune_size_mb"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="generated_prune_size_mb must be an integer between 10 and 10240",
                )
            if generated_prune_size_mb < 10 or generated_prune_size_mb > 10240:
                raise HTTPException(
                    status_code=400,
                    detail="generated_prune_size_mb must be between 10 and 10240",
                )
            update_data["generated_prune_size_mb"] = generated_prune_size_mb
        if "gpt_image_quality" in incoming:
            gpt_image_quality = str(incoming["gpt_image_quality"] or "").strip().lower()
            if gpt_image_quality not in {"low", "medium", "high"}:
                raise HTTPException(
                    status_code=400,
                    detail="gpt_image_quality must be one of: low, medium, high",
                )
            update_data["gpt_image_quality"] = gpt_image_quality
        if "adobe_register_email_provider" in incoming:
            provider = (
                str(incoming["adobe_register_email_provider"] or "tempmail_lol")
                .strip()
                .lower()
                .replace("-", "_")
            )
            if provider in {"temp", "tempmail", "temp_mail_lol"}:
                provider = "tempmail_lol"
            if provider not in {"local", "tempmail_lol"}:
                raise HTTPException(
                    status_code=400,
                    detail="adobe_register_email_provider must be one of: tempmail_lol, local",
                )
            update_data["adobe_register_email_provider"] = provider
        if "tempmail_lol_api_key" in incoming:
            update_data["tempmail_lol_api_key"] = str(
                incoming["tempmail_lol_api_key"] or ""
            ).strip()
        if "cloak_browser_headless" in incoming:
            update_data["cloak_browser_headless"] = bool(incoming["cloak_browser_headless"])
        if "cloak_browser_timeout_seconds" in incoming:
            try:
                cloak_timeout = int(incoming["cloak_browser_timeout_seconds"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="cloak_browser_timeout_seconds must be an integer",
                )
            if cloak_timeout < 120 or cloak_timeout > 3600:
                raise HTTPException(
                    status_code=400,
                    detail="cloak_browser_timeout_seconds must be between 120 and 3600",
                )
            update_data["cloak_browser_timeout_seconds"] = cloak_timeout
        if "cloak_browser_binary_path" in incoming:
            update_data["cloak_browser_binary_path"] = str(
                incoming["cloak_browser_binary_path"] or ""
            ).strip()
        if "cloak_browser_license_key" in incoming:
            update_data["cloak_browser_license_key"] = str(
                incoming["cloak_browser_license_key"] or ""
            ).strip()
        if "cloak_browser_version" in incoming:
            update_data["cloak_browser_version"] = str(
                incoming["cloak_browser_version"] or ""
            ).strip()
        if "cloak_register_test_image" in incoming:
            update_data["cloak_register_test_image"] = bool(incoming["cloak_register_test_image"])
        if "cloak_register_test_model" in incoming:
            update_data["cloak_register_test_model"] = str(
                incoming["cloak_register_test_model"] or "firefly-nano-banana-1k-1x1"
            ).strip() or "firefly-nano-banana-1k-1x1"
        if "cloak_register_image_timeout_seconds" in incoming:
            try:
                image_timeout = int(incoming["cloak_register_image_timeout_seconds"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="cloak_register_image_timeout_seconds must be an integer",
                )
            if image_timeout < 60 or image_timeout > 1200:
                raise HTTPException(
                    status_code=400,
                    detail="cloak_register_image_timeout_seconds must be between 60 and 1200",
                )
            update_data["cloak_register_image_timeout_seconds"] = image_timeout
        effective_max = int(
            update_data.get(
                "generated_max_size_mb",
                config_manager.get("generated_max_size_mb", 1024),
            )
            or 1024
        )
        effective_prune = int(
            update_data.get(
                "generated_prune_size_mb",
                config_manager.get("generated_prune_size_mb", 200),
            )
            or 200
        )
        if effective_prune >= effective_max:
            raise HTTPException(
                status_code=400,
                detail="generated_prune_size_mb must be smaller than generated_max_size_mb",
            )
        config_manager.update_all(update_data)
        apply_client_config()
        return config_manager.get_all()

    @router.get("/api/v1/refresh-profiles")
    def refresh_profiles_list(request: Request):
        require_admin_auth(request)
        return {"profiles": refresh_manager.list_profiles()}

    @router.post("/api/v1/refresh-profiles/export-cookies")
    def refresh_profiles_export_cookies(req: ExportSelectionRequest, request: Request):
        require_admin_auth(request)
        token_ids = req.ids if isinstance(req.ids, list) else None
        profile_ids = None
        if token_ids:
            profile_ids = []
            seen = set()
            for tid in token_ids:
                token_info = token_manager.get_by_id(str(tid or "").strip())
                if not token_info:
                    continue
                profile_id = str(token_info.get("refresh_profile_id") or "").strip()
                if not profile_id or profile_id in seen:
                    continue
                seen.add(profile_id)
                profile_ids.append(profile_id)
        exported = refresh_manager.export_cookies(profile_ids)
        return {
            "status": "ok",
            "total": len(exported),
            "selected": bool(token_ids),
            "items": exported,
        }

    @router.post("/api/v1/refresh-profiles/import-cookie")
    def refresh_profiles_import_cookie(
        req: RefreshCookieImportRequest, request: Request
    ):
        require_admin_auth(request)
        try:
            profile = refresh_manager.import_cookie(req.cookie, name=req.name)
            refresh_result = None
            refresh_error = ""
            try:
                refresh_result = refresh_manager.refresh_once(
                    str(profile.get("id") or "")
                )
            except Exception as exc:
                refresh_error = str(exc)
            return {
                "status": "ok" if not refresh_error else "partial",
                "profile": profile,
                "refresh_result": refresh_result,
                "refresh_error": refresh_error,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/api/v1/refresh-profiles/import-cookie-batch")
    def refresh_profiles_import_cookie_batch(
        req: RefreshCookieBatchImportRequest, request: Request
    ):
        require_admin_auth(request)
        if not req.items:
            raise HTTPException(status_code=400, detail="items is required")

        imported = []
        failed = []
        refreshed = []
        refresh_failed = []

        def import_one(idx: int, item):
            try:
                profile = refresh_manager.import_cookie(item.cookie, name=item.name)
            except ValueError as exc:
                return {
                    "index": idx,
                    "imported": None,
                    "failed": {
                        "index": idx,
                        "name": item.name,
                        "detail": str(exc),
                    },
                    "refreshed": None,
                    "refresh_failed": None,
                }

            refreshed_item = None
            refresh_failed_item = None
            try:
                refresh_result = refresh_manager.refresh_once(
                    str(profile.get("id") or "")
                )
                refreshed_item = {
                    "index": idx,
                    "profile_id": profile.get("id"),
                    "profile_name": profile.get("name"),
                    "result": refresh_result,
                }
            except Exception as exc:
                refresh_failed_item = {
                    "index": idx,
                    "profile_id": profile.get("id"),
                    "profile_name": profile.get("name"),
                    "detail": str(exc),
                }

            return {
                "index": idx,
                "imported": profile,
                "failed": None,
                "refreshed": refreshed_item,
                "refresh_failed": refresh_failed_item,
            }

        max_workers = min(get_batch_concurrency(), len(req.items))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(import_one, idx, item)
                for idx, item in enumerate(req.items)
            ]
            done_items = [future.result() for future in as_completed(futures)]

        done_items.sort(key=lambda item: item["index"])
        for item in done_items:
            if item["imported"] is not None:
                imported.append(item["imported"])
            if item["failed"] is not None:
                failed.append(item["failed"])
            if item["refreshed"] is not None:
                refreshed.append(item["refreshed"])
            if item["refresh_failed"] is not None:
                refresh_failed.append(item["refresh_failed"])

        result = {
            "status": (
                "ok"
                if (not failed and not refresh_failed)
                else ("partial" if imported else "failed")
            ),
            "total": len(req.items),
            "imported_count": len(imported),
            "failed_count": len(failed),
            "refreshed_count": len(refreshed),
            "refresh_failed_count": len(refresh_failed),
            "profiles": imported,
            "failed": failed,
            "refreshed": refreshed,
            "refresh_failed": refresh_failed,
        }
        if not imported:
            raise HTTPException(status_code=400, detail=result)
        return result

    @router.post("/api/v1/refresh-profiles/{profile_id}/refresh-now")
    def refresh_profiles_refresh_now(profile_id: str, request: Request, client_id: str = ""):
        require_admin_auth(request)
        try:
            return refresh_manager.refresh_once(profile_id, client_id=(client_id or None))
        except KeyError:
            raise HTTPException(status_code=404, detail="profile not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/api/v1/refresh-profiles/{profile_id}/enabled")
    def refresh_profiles_set_enabled(
        profile_id: str, req: RefreshProfileEnabledRequest, request: Request
    ):
        require_admin_auth(request)
        try:
            profile = refresh_manager.set_enabled(profile_id, req.enabled)
            return {"status": "ok", "profile": profile}
        except KeyError:
            raise HTTPException(status_code=404, detail="profile not found")

    @router.delete("/api/v1/refresh-profiles/{profile_id}")
    def refresh_profiles_delete(profile_id: str, request: Request):
        require_admin_auth(request)
        try:
            refresh_manager.remove_profile(profile_id)
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(status_code=404, detail="profile not found")

    return router
