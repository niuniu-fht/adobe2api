import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adobe_account_mgr import AdobeAccountManager


class FakeTempMailClient:
    def __init__(self):
        self.created = []

    def create_inbox(self, *, prefix=None, domain=None):
        index = len(self.created) + 1
        address = f"{prefix or 'user'}@tempmail.test"
        token = f"token-{index}"
        self.created.append({"prefix": prefix, "domain": domain, "address": address})
        return {"address": address, "token": token}

    def fetch_inbox(self, token):
        return {
            "expired": False,
            "emails": [
                {
                    "from": "noreply@example.test",
                    "subject": "Verify your email",
                    "body": "Your code is 123456",
                    "html": "",
                    "date": 1,
                }
            ],
        }

    @staticmethod
    def extract_verification(email):
        return {"code": "123456", "link": ""}


class AdobeAccountManagerTest(unittest.TestCase):
    def test_register_import_update_and_delete_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = AdobeAccountManager(Path(tmp) / "adobe_accounts.json")

            result = manager.register_accounts(
                count=2, domain="example.test", email_prefix="trial"
            )
            self.assertEqual(result["registered_count"], 2)
            self.assertEqual(len(manager.list_accounts()), 2)
            self.assertTrue(
                all(item["email"].endswith("@example.test") for item in result["accounts"])
            )

            imported = manager.import_accounts(
                [
                    {
                        "email": "manual@example.test",
                        "password": "Aa123456!",
                        "ip": "127.0.0.10",
                    },
                    {
                        "email": "manual@example.test",
                        "password": "Aa123456!",
                        "ip": "127.0.0.11",
                    },
                ]
            )
            self.assertEqual(imported["imported_count"], 1)
            self.assertEqual(imported["skipped_count"], 1)

            account_id = imported["accounts"][0]["id"]
            updated = manager.update_account(
                account_id,
                {
                    "eligibility": "eligible",
                    "status": "ready",
                    "imageStatus": "ok",
                    "lastAction": "测试完成",
                },
            )
            self.assertEqual(updated["eligibility"], "eligible")
            self.assertEqual(updated["status"], "ready")
            self.assertEqual(updated["image_status"], "ok")
            self.assertEqual(updated["last_action"], "测试完成")

            self.assertTrue(manager.delete_account(account_id))
            self.assertFalse(manager.delete_account(account_id))

    def test_register_accounts_with_tempmail_lol_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = AdobeAccountManager(Path(tmp) / "adobe_accounts.json")
            fake = FakeTempMailClient()

            result = manager.register_accounts(
                count=2,
                email_prefix="adobe",
                email_provider="tempmail_lol",
                tempmail_client=fake,
            )

            self.assertEqual(result["provider"], "tempmail_lol")
            self.assertEqual(result["registered_count"], 2)
            self.assertEqual(len(fake.created), 2)
            self.assertTrue(
                all(item["email_provider"] == "tempmail_lol" for item in result["accounts"])
            )
            self.assertTrue(all(item["mail_token"] for item in result["accounts"]))

            account_id = result["accounts"][0]["id"]
            inbox = manager.fetch_account_emails(account_id, tempmail_client=fake)
            self.assertEqual(len(inbox["emails"]), 1)
            self.assertEqual(inbox["account"]["verification_code"], "123456")


if __name__ == "__main__":
    unittest.main()
