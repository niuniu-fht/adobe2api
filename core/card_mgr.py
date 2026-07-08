import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CARDS_FILE = DATA_DIR / "payment_cards.json"


class CardManager:
    """Local payment-card metadata store for checkout jobs.

    This project stores card data only on the local machine. The `source` fields
    are intentionally first-class so each card can be traced back to a legitimate
    channel such as a bank-issued card, a business virtual-card provider, or an
    Adobe/processor sandbox test card.
    """

    def __init__(self, file_path: Path = CARDS_FILE) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._cards: List[Dict[str, Any]] = []
        self._logs: List[str] = []
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._file_path.exists():
                self._cards = []
                self._logs = []
                return
            try:
                payload = json.loads(self._file_path.read_text(encoding="utf-8"))
            except Exception:
                self._cards = []
                self._logs = []
                return
            rows = payload.get("cards") if isinstance(payload, dict) else []
            logs = payload.get("logs") if isinstance(payload, dict) else []
            self._cards = [
                self._normalize_card(item)
                for item in (rows if isinstance(rows, list) else [])
                if isinstance(item, dict)
            ]
            self._logs = [
                str(item or "").strip()
                for item in (logs if isinstance(logs, list) else [])
                if str(item or "").strip()
            ][-300:]

    def _save_locked(self) -> None:
        self._file_path.write_text(
            json.dumps(
                {"version": 1, "cards": self._cards, "logs": self._logs[-300:]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _digits(value: Any) -> str:
        return re.sub(r"\D+", "", str(value or ""))

    @classmethod
    def _mask_number(cls, value: Any) -> str:
        digits = cls._digits(value)
        if not digits:
            return ""
        if len(digits) <= 8:
            return "*" * max(0, len(digits) - 4) + digits[-4:]
        return f"{digits[:6]}{'*' * max(4, len(digits) - 10)}{digits[-4:]}"

    @classmethod
    def _normalize_card(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        number = cls._digits(item.get("number") or item.get("card_number"))
        exp_month = str(item.get("exp_month") or item.get("month") or "").strip()
        exp_year = str(item.get("exp_year") or item.get("year") or "").strip()
        if exp_month.isdigit():
            exp_month = str(max(1, min(12, int(exp_month)))).zfill(2)
        if len(exp_year) == 2 and exp_year.isdigit():
            exp_year = f"20{exp_year}"
        label = str(item.get("label") or item.get("name") or "").strip()
        last4 = number[-4:] if number else str(item.get("last4") or "").strip()
        if not label:
            label = f"Card {last4}" if last4 else "Payment Card"
        source_type = (
            str(item.get("source_type") or item.get("sourceType") or "own_card")
            .strip()
            .lower()
            .replace("-", "_")
        )
        if source_type not in {
            "own_card",
            "bank_virtual_card",
            "business_virtual_card",
            "prepaid_card",
            "processor_test_card",
            "other_legal_source",
        }:
            source_type = "other_legal_source"
        return {
            "id": str(item.get("id") or uuid.uuid4().hex[:12]).strip(),
            "label": label,
            "cardholder": str(item.get("cardholder") or item.get("holder") or "").strip(),
            "number": number,
            "masked_number": cls._mask_number(number),
            "last4": last4,
            "exp_month": exp_month,
            "exp_year": exp_year,
            "cvv": cls._digits(item.get("cvv") or item.get("cvc"))[:6],
            "country": str(item.get("country") or "US").strip().upper(),
            "state": str(item.get("state") or "").strip(),
            "city": str(item.get("city") or "").strip(),
            "postal_code": str(item.get("postal_code") or item.get("zip") or "").strip(),
            "address1": str(item.get("address1") or item.get("address") or "").strip(),
            "address2": str(item.get("address2") or "").strip(),
            "phone": str(item.get("phone") or "").strip(),
            "source_type": source_type,
            "source": str(item.get("source") or "").strip(),
            "source_url": str(item.get("source_url") or item.get("sourceUrl") or "").strip(),
            "notes": str(item.get("notes") or "").strip(),
            "status": str(item.get("status") or "active").strip() or "active",
            "created_at": str(item.get("created_at") or item.get("createdAt") or cls._now_text()).strip(),
            "updated_at": int(item.get("updated_at") or time.time()),
        }

    def _append_log_locked(self, message: str) -> None:
        self._logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        self._logs = self._logs[-300:]

    def _safe_card(self, item: Dict[str, Any], include_sensitive: bool = False) -> Dict[str, Any]:
        out = dict(item)
        if not include_sensitive:
            out["number"] = out.get("masked_number", "")
            out["cvv"] = "***" if out.get("cvv") else ""
        return out

    def list_cards(self, include_sensitive: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._safe_card(item, include_sensitive=include_sensitive) for item in self._cards]

    def get_card(self, card_id: str, include_sensitive: bool = True) -> Dict[str, Any]:
        cid = str(card_id or "").strip()
        with self._lock:
            for item in self._cards:
                if str(item.get("id") or "") == cid:
                    return self._safe_card(item, include_sensitive=include_sensitive)
        raise KeyError("card not found")

    def upsert_card(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        card = self._normalize_card(raw or {})
        if not card.get("number") and not card.get("last4"):
            raise ValueError("card number or last4 is required")
        with self._lock:
            for idx, item in enumerate(self._cards):
                if str(item.get("id") or "") != card["id"]:
                    continue
                merged = dict(item)
                merged.update(card)
                merged["updated_at"] = int(time.time())
                self._cards[idx] = self._normalize_card(merged)
                self._append_log_locked(f"UPDATE_CARD id={card['id']} label={card['label']}")
                self._save_locked()
                return self._safe_card(self._cards[idx])
            self._cards.insert(0, card)
            self._append_log_locked(
                f"ADD_CARD id={card['id']} label={card['label']} source={card['source_type']}"
            )
            self._save_locked()
            return self._safe_card(card)

    def import_cards(self, rows: List[Any]) -> Dict[str, Any]:
        if not isinstance(rows, list):
            raise ValueError("cards must be a list")
        imported: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                failed.append({"index": idx, "detail": "invalid card row"})
                continue
            try:
                imported.append(self.upsert_card(row))
            except Exception as exc:
                failed.append({"index": idx, "detail": str(exc)})
        return {
            "imported_count": len(imported),
            "failed_count": len(failed),
            "cards": imported,
            "failed": failed,
        }

    def delete_card(self, card_id: str) -> bool:
        cid = str(card_id or "").strip()
        with self._lock:
            before = len(self._cards)
            self._cards = [item for item in self._cards if str(item.get("id") or "") != cid]
            changed = len(self._cards) != before
            if changed:
                self._append_log_locked(f"DELETE_CARD id={cid}")
                self._save_locked()
            return changed

    def list_logs(self, limit: int = 100) -> List[str]:
        safe_limit = max(1, min(int(limit or 100), 300))
        with self._lock:
            return list(self._logs[-safe_limit:])


card_manager = CardManager()
