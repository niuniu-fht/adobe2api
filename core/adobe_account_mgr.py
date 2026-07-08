import json
import secrets
import string
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.tempmail_lol import TempMailLolClient, TempMailLolError


BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "adobe_accounts.json"


class AdobeAccountManager:
    def __init__(self, file_path: Path = ACCOUNTS_FILE) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._accounts: List[Dict[str, Any]] = []
        self._logs: List[str] = []
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._file_path.exists():
                self._accounts = []
                self._logs = []
                return
            try:
                payload = json.loads(self._file_path.read_text(encoding="utf-8"))
            except Exception:
                self._accounts = []
                self._logs = []
                return
            accounts = payload.get("accounts") if isinstance(payload, dict) else None
            logs = payload.get("logs") if isinstance(payload, dict) else None
            self._accounts = [
                self._normalize_account(item)
                for item in (accounts if isinstance(accounts, list) else [])
                if isinstance(item, dict)
            ]
            self._logs = [
                str(item or "").strip()
                for item in (logs if isinstance(logs, list) else [])
                if str(item or "").strip()
            ][-300:]

    def _save_locked(self) -> None:
        payload = {"version": 1, "accounts": self._accounts, "logs": self._logs[-300:]}
        self._file_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _random_ip() -> str:
        pools = (
            (49, 43, 160),
            (23, 105, 77),
            (104, 28, 31),
            (185, 199, 108),
        )
        pool = secrets.choice(pools)
        return f"{pool[0]}.{pool[1]}.{pool[2]}.{secrets.randbelow(180) + 20}"

    @staticmethod
    def _password(seq: int) -> str:
        alphabet = string.ascii_letters + string.digits
        body = "".join(secrets.choice(alphabet) for _ in range(10))
        return f"Aa{body}!{seq}"

    @staticmethod
    def _clean_domain(domain: str) -> str:
        value = str(domain or "").strip().lower().lstrip("@")
        allowed = []
        for ch in value:
            if ch.isalnum() or ch in {".", "-"}:
                allowed.append(ch)
        cleaned = "".join(allowed).strip(".-")
        return cleaned or "trial.local"

    @staticmethod
    def _normalize_account(item: Dict[str, Any]) -> Dict[str, Any]:
        created = str(
            item.get("created_at") or item.get("createdAt") or AdobeAccountManager._now_text()
        ).strip()
        image_status = str(item.get("image_status") or item.get("imageStatus") or "").strip()
        last_action = str(item.get("last_action") or item.get("lastAction") or "").strip()
        return {
            "id": str(item.get("id") or uuid.uuid4().hex).strip(),
            "email": str(item.get("email") or item.get("username") or "").strip(),
            "password": str(item.get("password") or item.get("pass") or "").strip(),
            "status": str(item.get("status") or "registered").strip() or "registered",
            "eligibility": str(item.get("eligibility") or "unknown").strip() or "unknown",
            "plan": str(item.get("plan") or "-").strip() or "-",
            "image_status": image_status or "untested",
            "ip": str(item.get("ip") or item.get("proxy_ip") or AdobeAccountManager._random_ip()).strip(),
            "created_at": created,
            "last_action": last_action or "自动注册",
            "updated_at": int(item.get("updated_at") or time.time()),
            "email_provider": str(item.get("email_provider") or item.get("emailProvider") or "local").strip() or "local",
            "mail_token": str(item.get("mail_token") or item.get("mailToken") or "").strip(),
            "mail_status": str(item.get("mail_status") or item.get("mailStatus") or "").strip(),
            "verification_code": str(item.get("verification_code") or item.get("verificationCode") or "").strip(),
            "verification_link": str(item.get("verification_link") or item.get("verificationLink") or "").strip(),
            "session_state_path": str(item.get("session_state_path") or item.get("sessionStatePath") or "").strip(),
            "cookie_profile_id": str(item.get("cookie_profile_id") or item.get("cookieProfileId") or "").strip(),
            "token_status": str(item.get("token_status") or item.get("tokenStatus") or "").strip(),
            "token_refresh_status": str(item.get("token_refresh_status") or item.get("tokenRefreshStatus") or "").strip(),
            "token_refresh_error": str(item.get("token_refresh_error") or item.get("tokenRefreshError") or "").strip(),
            "token_refresh_updated_at": int(item.get("token_refresh_updated_at") or item.get("tokenRefreshUpdatedAt") or 0),
            "image_test_url": str(item.get("image_test_url") or item.get("imageTestUrl") or "").strip(),
            "image_test_error": str(item.get("image_test_error") or item.get("imageTestError") or "").strip(),
            "image_test_token_id": str(item.get("image_test_token_id") or item.get("imageTestTokenId") or "").strip(),
            "image_test_report_path": str(item.get("image_test_report_path") or item.get("imageTestReportPath") or "").strip(),
            "web_image_status": str(item.get("web_image_status") or item.get("webImageStatus") or "untested").strip() or "untested",
            "web_image_test_url": str(item.get("web_image_test_url") or item.get("webImageTestUrl") or "").strip(),
            "web_image_test_error": str(item.get("web_image_test_error") or item.get("webImageTestError") or "").strip(),
        }

    def _append_log_locked(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self._logs.append(line)
        self._logs = self._logs[-300:]

    def list_accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._accounts]

    def list_logs(self, limit: int = 100) -> List[str]:
        safe_limit = max(1, min(int(limit or 100), 300))
        with self._lock:
            return list(self._logs[-safe_limit:])

    def get_account(self, account_id: str) -> Dict[str, Any]:
        account_id = str(account_id or "").strip()
        with self._lock:
            for item in self._accounts:
                if str(item.get("id") or "") == account_id:
                    return dict(item)
        raise KeyError("account not found")

    def register_accounts(
        self,
        *,
        count: int,
        domain: str = "trial.local",
        email_prefix: str = "adobe_user",
        email_provider: str = "local",
        tempmail_api_key: str = "",
        tempmail_proxy: str = "",
        tempmail_client: Optional[TempMailLolClient] = None,
    ) -> Dict[str, Any]:
        safe_count = max(1, min(int(count or 1), 100))
        safe_domain = self._clean_domain(domain)
        prefix = str(email_prefix or "adobe_user").strip().lower()
        prefix = "".join(ch for ch in prefix if ch.isalnum() or ch in {"_", "-", "."})
        prefix = prefix.strip("._-") or "adobe_user"
        provider = str(email_provider or "local").strip().lower().replace("-", "_")
        if provider in {"temp", "tempmail", "tempmail_lol", "temp_mail_lol"}:
            provider = "tempmail_lol"
        elif provider not in {"local", "mock"}:
            raise ValueError(f"unsupported email provider: {email_provider}")
        if provider == "mock":
            provider = "local"

        created: List[Dict[str, Any]] = []
        if provider == "tempmail_lol":
            client = tempmail_client or TempMailLolClient(
                api_key=tempmail_api_key, proxy=tempmail_proxy
            )
            local_placeholder_domains = {"trial.local", "mailbox.local", "studio.local", "local", "example.test"}
            requested_domain = "" if safe_domain in local_placeholder_domains else safe_domain
            with self._lock:
                base_seq = len(self._accounts) + 1
            for offset in range(safe_count):
                seq = base_seq + offset
                mailbox_prefix = f"{prefix}-{seq:04d}-{secrets.token_hex(3)}"
                try:
                    inbox = client.create_inbox(
                        prefix=mailbox_prefix,
                        domain=requested_domain or None,
                    )
                except TempMailLolError as exc:
                    with self._lock:
                        self._append_log_locked(
                            f"TEMPMAIL_CREATE_FAIL prefix={mailbox_prefix} error={exc}"
                        )
                        self._save_locked()
                    raise ValueError(f"tempmail-lol create inbox failed: {exc}") from exc

                email = str(inbox.get("address") or "").strip()
                mail_token = str(inbox.get("token") or "").strip()
                if not email or not mail_token:
                    raise ValueError("tempmail-lol returned incomplete inbox payload")
                account = self._normalize_account(
                    {
                        "id": uuid.uuid4().hex,
                        "email": email,
                        "password": self._password(seq),
                        "status": "registered",
                        "eligibility": "unknown",
                        "plan": "-",
                        "image_status": "untested",
                        "ip": self._random_ip(),
                        "created_at": self._now_text(),
                        "last_action": "TempMail.lol 自动注册",
                        "email_provider": "tempmail_lol",
                        "mail_token": mail_token,
                        "mail_status": "inbox_created",
                    }
                )
                with self._lock:
                    existing_emails = {
                        str(item.get("email") or "").strip().lower()
                        for item in self._accounts
                    }
                    if account["email"].lower() in existing_emails:
                        self._append_log_locked(
                            f"TEMPMAIL_DUPLICATE_SKIP email={account['email']}"
                        )
                        self._save_locked()
                        continue
                    self._accounts.insert(0, account)
                    created.append(dict(account))
                    self._append_log_locked(
                        f"TEMPMAIL_CREATE_OK email={account['email']} provider=tempmail_lol"
                    )
                    self._append_log_locked(
                        f"REGISTER_ACCOUNT email={account['email']} ip={account['ip']} provider=tempmail_lol"
                    )
                    self._save_locked()
            return {
                "registered_count": len(created),
                "provider": "tempmail_lol",
                "accounts": created,
            }

        created: List[Dict[str, Any]] = []
        with self._lock:
            existing_emails = {
                str(item.get("email") or "").strip().lower() for item in self._accounts
            }
            base_seq = len(self._accounts) + 1
            for offset in range(safe_count):
                seq = base_seq + offset
                for attempt in range(1000):
                    suffix = f"{seq + attempt:04d}"
                    email = f"{prefix}_{suffix}@{safe_domain}"
                    if email.lower() not in existing_emails:
                        break
                account = self._normalize_account(
                    {
                        "id": uuid.uuid4().hex,
                        "email": email,
                        "password": self._password(seq),
                        "status": "registered",
                        "eligibility": "unknown",
                        "plan": "-",
                        "image_status": "untested",
                        "ip": self._random_ip(),
                        "created_at": self._now_text(),
                        "last_action": "自动注册",
                        "email_provider": "local",
                        "mail_status": "local_generated",
                    }
                )
                existing_emails.add(account["email"].lower())
                self._accounts.insert(0, account)
                created.append(dict(account))
                self._append_log_locked(
                    f"REGISTER_ACCOUNT email={account['email']} ip={account['ip']} provider=local"
                )
            self._save_locked()
        return {"registered_count": len(created), "provider": "local", "accounts": created}

    def import_accounts(self, rows: List[Any]) -> Dict[str, Any]:
        if not isinstance(rows, list):
            raise ValueError("accounts must be a list")
        imported: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        with self._lock:
            existing = {
                str(item.get("email") or "").strip().lower() for item in self._accounts
            }
            for idx, raw in enumerate(rows):
                if isinstance(raw, list):
                    raw = {
                        "email": raw[0] if len(raw) > 0 else "",
                        "password": raw[1] if len(raw) > 1 else "",
                        "ip": raw[2] if len(raw) > 2 else "",
                    }
                if not isinstance(raw, dict):
                    skipped.append({"index": idx, "detail": "invalid account row"})
                    continue
                account = self._normalize_account(raw)
                if not account["email"]:
                    skipped.append({"index": idx, "detail": "email is required"})
                    continue
                if account["email"].lower() in existing:
                    skipped.append({"index": idx, "email": account["email"], "detail": "duplicate"})
                    continue
                existing.add(account["email"].lower())
                account["last_action"] = account.get("last_action") or "批量导入"
                self._accounts.insert(0, account)
                imported.append(dict(account))
                self._append_log_locked(f"IMPORT_ACCOUNT email={account['email']}")
            self._save_locked()
        return {
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "accounts": imported,
            "skipped": skipped,
        }

    def update_account(self, account_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        account_id = str(account_id or "").strip()
        if not account_id:
            raise KeyError("account not found")
        allowed = {
            "email",
            "password",
            "status",
            "eligibility",
            "plan",
            "image_status",
            "imageStatus",
            "ip",
            "last_action",
            "lastAction",
            "email_provider",
            "emailProvider",
            "mail_token",
            "mailToken",
            "mail_status",
            "mailStatus",
            "verification_code",
            "verificationCode",
            "verification_link",
            "verificationLink",
            "session_state_path",
            "sessionStatePath",
            "cookie_profile_id",
            "cookieProfileId",
            "token_status",
            "tokenStatus",
            "token_refresh_status",
            "tokenRefreshStatus",
            "token_refresh_error",
            "tokenRefreshError",
            "token_refresh_updated_at",
            "tokenRefreshUpdatedAt",
            "image_test_url",
            "imageTestUrl",
            "image_test_error",
            "imageTestError",
            "image_test_token_id",
            "imageTestTokenId",
            "image_test_report_path",
            "imageTestReportPath",
            "web_image_status",
            "webImageStatus",
            "web_image_test_url",
            "webImageTestUrl",
            "web_image_test_error",
            "webImageTestError",
        }
        with self._lock:
            for idx, item in enumerate(self._accounts):
                if str(item.get("id") or "") != account_id:
                    continue
                merged = dict(item)
                for key, value in (patch or {}).items():
                    if key in allowed:
                        merged[key] = value
                if "imageStatus" in merged:
                    merged["image_status"] = merged.pop("imageStatus")
                if "lastAction" in merged:
                    merged["last_action"] = merged.pop("lastAction")
                if "emailProvider" in merged:
                    merged["email_provider"] = merged.pop("emailProvider")
                if "mailToken" in merged:
                    merged["mail_token"] = merged.pop("mailToken")
                if "mailStatus" in merged:
                    merged["mail_status"] = merged.pop("mailStatus")
                if "verificationCode" in merged:
                    merged["verification_code"] = merged.pop("verificationCode")
                if "verificationLink" in merged:
                    merged["verification_link"] = merged.pop("verificationLink")
                if "sessionStatePath" in merged:
                    merged["session_state_path"] = merged.pop("sessionStatePath")
                if "cookieProfileId" in merged:
                    merged["cookie_profile_id"] = merged.pop("cookieProfileId")
                if "tokenStatus" in merged:
                    merged["token_status"] = merged.pop("tokenStatus")
                if "tokenRefreshStatus" in merged:
                    merged["token_refresh_status"] = merged.pop("tokenRefreshStatus")
                if "tokenRefreshError" in merged:
                    merged["token_refresh_error"] = merged.pop("tokenRefreshError")
                if "tokenRefreshUpdatedAt" in merged:
                    merged["token_refresh_updated_at"] = merged.pop("tokenRefreshUpdatedAt")
                if "imageTestUrl" in merged:
                    merged["image_test_url"] = merged.pop("imageTestUrl")
                if "imageTestError" in merged:
                    merged["image_test_error"] = merged.pop("imageTestError")
                if "imageTestTokenId" in merged:
                    merged["image_test_token_id"] = merged.pop("imageTestTokenId")
                if "imageTestReportPath" in merged:
                    merged["image_test_report_path"] = merged.pop("imageTestReportPath")
                if "webImageStatus" in merged:
                    merged["web_image_status"] = merged.pop("webImageStatus")
                if "webImageTestUrl" in merged:
                    merged["web_image_test_url"] = merged.pop("webImageTestUrl")
                if "webImageTestError" in merged:
                    merged["web_image_test_error"] = merged.pop("webImageTestError")
                merged["updated_at"] = int(time.time())
                normalized = self._normalize_account(merged)
                self._accounts[idx] = normalized
                self._append_log_locked(f"UPDATE_ACCOUNT email={normalized['email']}")
                self._save_locked()
                return dict(normalized)
        raise KeyError("account not found")

    def fetch_account_emails(
        self,
        account_id: str,
        *,
        tempmail_api_key: str = "",
        tempmail_proxy: str = "",
        tempmail_client: Optional[TempMailLolClient] = None,
    ) -> Dict[str, Any]:
        account = self.get_account(account_id)
        provider = str(account.get("email_provider") or "").strip().lower()
        token = str(account.get("mail_token") or "").strip()
        if provider != "tempmail_lol" or not token:
            raise ValueError("account is not backed by a TempMail.lol inbox")
        client = tempmail_client or TempMailLolClient(
            api_key=tempmail_api_key, proxy=tempmail_proxy
        )
        inbox = client.fetch_inbox(token)
        emails = [item for item in (inbox.get("emails") or []) if isinstance(item, dict)]
        patch: Dict[str, Any] = {
            "mail_status": "expired" if inbox.get("expired") else f"emails={len(emails)}",
        }
        verification = None
        for email in emails:
            extracted = client.extract_verification(email)
            if extracted.get("code") or extracted.get("link"):
                verification = extracted
                patch["verification_code"] = extracted.get("code") or ""
                patch["verification_link"] = extracted.get("link") or ""
                patch["mail_status"] = "verification_received"
                break
        updated = self.update_account(account_id, patch)
        with self._lock:
            self._append_log_locked(
                f"TEMPMAIL_FETCH email={updated['email']} count={len(emails)} status={updated['mail_status']}"
            )
            self._save_locked()
        return {
            "account": updated,
            "emails": emails,
            "expired": bool(inbox.get("expired")),
            "verification": verification or {},
        }

    def delete_account(self, account_id: str) -> bool:
        account_id = str(account_id or "").strip()
        with self._lock:
            before = len(self._accounts)
            removed_email = ""
            kept = []
            for item in self._accounts:
                if str(item.get("id") or "") == account_id:
                    removed_email = str(item.get("email") or "")
                    continue
                kept.append(item)
            self._accounts = kept
            changed = len(self._accounts) != before
            if changed:
                self._append_log_locked(f"DELETE_ACCOUNT email={removed_email}")
                self._save_locked()
            return changed

    def clear_logs(self) -> None:
        with self._lock:
            self._logs = []
            self._save_locked()


adobe_account_manager = AdobeAccountManager()
