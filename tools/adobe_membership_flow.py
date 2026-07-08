import json
import os
import re
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config" / "config.json"
RESULT_FILE = Path(
    os.environ.get("MEMBERSHIP_FLOW_RESULT_FILE")
    or str(DATA_DIR / "membership_flow_result.json")
)


def log(stage: str, **data) -> None:
    print(json.dumps({"stage": stage, **data}, ensure_ascii=True), flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_config() -> dict:
    return load_json(CONFIG_FILE, {})


def apply_proxy_env(cfg: dict) -> str:
    proxy = str(cfg.get("proxy") or "").strip() if cfg.get("use_proxy") else ""
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[key] = proxy
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])
    return proxy


def save_result(payload: dict) -> None:
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def visible_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=7000)
    except Exception:
        return ""


def classify_eligibility(text: str, url: str) -> tuple[str, str]:
    hay = f"{text}\n{url}".lower()
    if any(x in hay for x in ("verification required", "verify your identity", "captcha", "challenge")):
        return "need_verify", "页面出现验证/挑战"
    if any(x in hay for x in ("free trial", "7-day free trial", "7 day free trial", "try for free", "start free trial", "us$0", "$0.00")):
        return "eligible", "页面出现免费试用/0 元今日付款提示"
    if any(x in hay for x in ("not eligible", "new subscribers only", "already had a trial", "ineligible")):
        return "not_eligible", "页面提示仅新订阅者/不可试用"
    if any(x in hay for x in ("payment method", "credit card", "card number", "paypal", "billing address")):
        return "need_payment", "进入付款/账单信息阶段"
    if any(x in hay for x in ("buy now", "purchase", "checkout")):
        return "paid_only_or_checkout", "页面出现购买/结账入口"
    return "unknown", "未识别出明确试用资格"


def update_account(account_id: str, patch: dict) -> None:
    try:
        import requests

        s = requests.Session()
        s.trust_env = False
        base = "http://127.0.0.1:6001"
        s.post(base + "/api/v1/auth/login", json={"username": "admin", "password": "admin"}, timeout=10)
        s.put(base + f"/api/v1/adobe/accounts/{account_id}", json=patch, timeout=15)
    except Exception:
        pass


def main() -> None:
    action = str(os.environ.get("MEMBERSHIP_ACTION") or "eligibility").strip().lower()
    account_id = str(os.environ.get("MEMBERSHIP_ACCOUNT_ID") or "").strip()
    card_id = str(os.environ.get("MEMBERSHIP_CARD_ID") or "").strip()
    plan_url = str(os.environ.get("MEMBERSHIP_PLAN_URL") or "").strip() or "https://www.adobe.com/creativecloud/plans.html"
    submit_payment = str(os.environ.get("MEMBERSHIP_SUBMIT_PAYMENT") or "").strip().lower() in {"1", "true", "yes"}

    accounts_payload = load_json(DATA_DIR / "adobe_accounts.json", {})
    accounts = accounts_payload.get("accounts") if isinstance(accounts_payload, dict) else []
    account = next((x for x in accounts if str(x.get("id") or "") == account_id), None)
    if not isinstance(account, dict):
        raise RuntimeError("account not found")

    cards_payload = load_json(DATA_DIR / "payment_cards.json", {})
    cards = cards_payload.get("cards") if isinstance(cards_payload, dict) else []
    card = next((x for x in cards if str(x.get("id") or "") == card_id), None) if card_id else None
    if action == "open" and not isinstance(card, dict):
        raise RuntimeError("card is required for membership open")

    cfg = load_config()
    proxy = apply_proxy_env(cfg)
    log("membership_start", action=action, account=account.get("email"), plan_url=plan_url, card_id=card_id, submit_payment=submit_payment)
    log("proxy_configured", use_proxy=bool(proxy), proxy=proxy)

    from cloakbrowser import launch

    browser = launch(
        headless=bool(cfg.get("cloak_browser_headless", False)),
        proxy=proxy or None,
        locale="zh-Hans-CN",
        timezone="Asia/Tokyo",
        humanize=True,
        args=["--window-size=1280,900", *([f"--proxy-server={proxy}"] if proxy else [])],
    )
    try:
        storage_state = str(account.get("session_state_path") or "").strip()
        if storage_state and Path(storage_state).exists():
            context = browser.new_context(storage_state=storage_state)
            page = context.new_page()
            log("storage_loaded", path=storage_state)
        else:
            page = browser.new_page()
            log("storage_missing", path=storage_state)

        requests_seen = []

        def capture_request(req):
            try:
                url = str(req.url or "")
                if any(key in url.lower() for key in ("commerce", "checkout", "offers", "plans", "billing")):
                    requests_seen.append({"method": req.method, "url": url[:500]})
                    del requests_seen[:-80]
            except Exception:
                pass

        try:
            page.on("request", capture_request)
        except Exception:
            pass

        log("goto_plan", url=plan_url)
        page.goto(plan_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        text = visible_text(page)
        eligibility, reason = classify_eligibility(text, page.url)
        log("eligibility_detected", eligibility=eligibility, reason=reason, url=page.url)

        if action == "open":
            if not submit_payment:
                log(
                    "open_paused",
                    reason="submit_payment=false，已完成资格/结账页探测，未提交付款",
                    card_source=(card or {}).get("source_type", ""),
                )
                membership_status = "paused_before_payment"
            else:
                # The final submit step is intentionally selector-driven because Adobe checkout
                # varies by region/product. The logs and result store all page evidence so a
                # concrete selector map can be added per checkout variant.
                log("open_submit_not_configured", reason="checkout selector map not configured")
                membership_status = "submit_selector_required"
        else:
            membership_status = "checked"

        patch = {
            "eligibility": eligibility,
            "last_action": f"会员{action}流程：{reason}",
        }
        if action == "open":
            patch["plan"] = "membership_flow:" + membership_status
        update_account(account_id, patch)

        result = {
            "status": "ok",
            "action": action,
            "account_id": account_id,
            "email": account.get("email"),
            "card_id": card_id,
            "card_source_type": (card or {}).get("source_type", ""),
            "plan_url": plan_url,
            "final_url": page.url,
            "title": page.title(),
            "eligibility": eligibility,
            "reason": reason,
            "membership_status": membership_status,
            "submit_payment": submit_payment,
            "requests_seen": requests_seen[-80:],
            "text_excerpt": re.sub(r"\s+", " ", text)[:3000],
            "account_patch": patch,
        }
        save_result(result)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        time.sleep(3)
    finally:
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
