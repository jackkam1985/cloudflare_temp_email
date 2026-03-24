#!/usr/bin/env python3
"""
Batch deploy the Email Relay Bridge Worker and configure Email Routing
across multiple Cloudflare accounts using their Global API Keys.

What this script does for each account:
  1. Discovers the CF Account ID (or uses the provided one)
  2. Uploads/updates the bridge Worker script with MAIN_WORKER_URL and
     MAIL_RELAY_SECRET already bound as environment bindings
  3. For each domain:
     a. Looks up the Zone ID
     b. Enables Email Routing (sets up required MX/TXT DNS records)
     c. Sets the Catch-all rule → Send to Worker

Usage:
    pip install -r requirements.txt
    cp accounts.example.yaml accounts.yaml   # fill in your credentials
    python batch-deploy.py                   # executes for real
    python batch-deploy.py --dry-run         # preview only, no changes made
    python batch-deploy.py --config /path/to/other.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Bridge Worker — bundled inline so no build step is needed.
# Matches worker-bridge/src/worker.ts
# ---------------------------------------------------------------------------
BRIDGE_WORKER_JS = r"""
export default {
  async fetch(request, env, _ctx) {
    const url = new URL(request.url);

    if (url.pathname === '/health') {
      return new Response(
        JSON.stringify({
          status: 'ok',
          MAIN_WORKER_URL: env.MAIN_WORKER_URL || null,
          MAIL_RELAY_SECRET: env.MAIL_RELAY_SECRET || null,
        }, null, 2),
        {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
          },
        }
      );
    }

    return new Response('Not Found', { status: 404 });
  },

  async email(message, env, _ctx) {
    if (!env.MAIN_WORKER_URL || !env.MAIL_RELAY_SECRET) {
      console.error(
        'Bridge misconfigured: MAIN_WORKER_URL and MAIL_RELAY_SECRET must be set'
      );
      message.setReject('Bridge misconfigured');
      return;
    }

    let rawEmail;
    try {
      rawEmail = await new Response(message.raw).text();
    } catch (error) {
      console.error('Failed to read raw email:', error);
      message.setReject('Failed to read email');
      return;
    }

    const body = JSON.stringify({
      from: message.from,
      to: message.to,
      rawEmail,
      messageId: message.headers.get('Message-ID'),
    });

    try {
      const res = await fetch(
        `${env.MAIN_WORKER_URL.replace(/\/$/, '')}/external/api/relay_email`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Relay-Secret': env.MAIL_RELAY_SECRET,
          },
          body,
        }
      );
      if (!res.ok) {
        const respText = await res.text();
        console.error(
          `Relay rejected: HTTP ${res.status} - ${respText} from=${message.from} to=${message.to}`
        );
      } else {
        console.log(`Relayed ok: from=${message.from} to=${message.to}`);
      }
    } catch (error) {
      console.error('Relay fetch error:', error);
    }
  },
};
""".strip()

CF_API = "https://api.cloudflare.com/client/v4"
REQUEST_DELAY = 0.4   # seconds between API calls to avoid rate limiting


# ---------------------------------------------------------------------------
# Cloudflare API client
# ---------------------------------------------------------------------------
class CloudflareClient:
    def __init__(self, email: str, api_key: str):
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Auth-Email": email, "X-Auth-Key": api_key}
        )

    def _get(self, path: str, **params) -> dict:
        r = self.session.get(f"{CF_API}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # ── Account ──────────────────────────────────────────────────────────────

    def get_account_id(self) -> str:
        """Return the first account ID visible to these credentials."""
        data = self._get("/accounts")
        accounts = data.get("result", [])
        if not accounts:
            raise RuntimeError("No accounts found for these credentials")
        if len(accounts) > 1:
            ids = ", ".join(a["id"] for a in accounts)
            print(
                f"    ⚠ Multiple accounts found ({ids}). "
                "Using the first one. Set account_id in config to override."
            )
        return accounts[0]["id"]

    # ── Worker ────────────────────────────────────────────────────────────────

    def upload_worker(
        self,
        account_id: str,
        worker_name: str,
        main_worker_url: str,
        mail_relay_secret: str,
        dry_run: bool = False,
    ) -> None:
        """Upload (or replace) the bridge Worker with env vars bound."""
        path = f"/accounts/{account_id}/workers/scripts/{worker_name}"
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": "2025-04-01",
            # plain_text is fine for the URL; secret_text prevents the value
            # being read back through the API.
            "bindings": [
                {
                    "type": "plain_text",
                    "name": "MAIN_WORKER_URL",
                    "text": main_worker_url,
                },
                {
                    "type": "secret_text",
                    "name": "MAIL_RELAY_SECRET",
                    "text": mail_relay_secret,
                },
            ],
        }

        if dry_run:
            print(
                f"    [dry-run] Would upload Worker '{worker_name}' "
                f"to account {account_id}"
            )
            return

        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": (
                "worker.js",
                BRIDGE_WORKER_JS,
                "application/javascript+module",
            ),
        }
        r = self.session.put(f"{CF_API}{path}", files=files, timeout=60)
        if not r.ok:
            raise RuntimeError(
                f"Worker upload failed: HTTP {r.status_code} — {r.text[:300]}"
            )
        print(f"    ✓ Worker '{worker_name}' uploaded/updated")

    # ── Zone ─────────────────────────────────────────────────────────────────

    def get_zone_id(self, domain: str) -> str:
        data = self._get("/zones", name=domain, per_page=1)
        zones = data.get("result", [])
        if not zones:
            raise RuntimeError(f"Zone not found (not managed by this account?): {domain}")
        return zones[0]["id"]

    # ── Email Routing ─────────────────────────────────────────────────────────

    def enable_email_routing(self, zone_id: str, dry_run: bool = False) -> None:
        if dry_run:
            print(f"    [dry-run] Would enable Email Routing for zone {zone_id}")
            return

        r = self.session.post(
            f"{CF_API}/zones/{zone_id}/email/routing/enable",
            json={},
            timeout=30,
        )
        if r.ok:
            print("    ✓ Email Routing enabled")
        elif r.status_code in (400, 409):
            # CF returns 409/400 when already enabled — treat as ok
            print("    ✓ Email Routing already enabled")
        else:
            # Non-fatal: domain DNS may need manual verification
            print(
                f"    ⚠ Enable Email Routing returned {r.status_code}: "
                f"{r.text[:200]}"
            )

    def get_email_routing_dns(self, zone_id: str) -> list[dict]:
        """Return the DNS records CF Email Routing needs for this zone."""
        r = self.session.get(
            f"{CF_API}/zones/{zone_id}/email/routing/dns", timeout=30
        )
        if r.ok:
            return r.json().get("result", [])
        return []

    def set_catch_all_worker(
        self, zone_id: str, worker_name: str, dry_run: bool = False
    ) -> None:
        if dry_run:
            print(
                f"    [dry-run] Would set Catch-all → Worker '{worker_name}' "
                f"for zone {zone_id}"
            )
            return

        body = {
            "actions": [{"type": "worker", "value": [worker_name]}],
            "enabled": True,
            "matchers": [{"type": "all"}],
        }
        r = self.session.put(
            f"{CF_API}/zones/{zone_id}/email/routing/rules/catch_all",
            json=body,
            timeout=30,
        )
        if r.ok:
            print(f"    ✓ Catch-all → Worker '{worker_name}'")
        else:
            raise RuntimeError(
                f"Set catch-all failed: HTTP {r.status_code} — {r.text[:300]}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    required = ("main_worker_url", "mail_relay_secret")
    for key in required:
        if not cfg.get(key):
            print(f"[error] '{key}' is required in config", file=sys.stderr)
            sys.exit(1)
    return cfg


def process_account(
    idx: int,
    total: int,
    acct: dict,
    worker_name: str,
    main_worker_url: str,
    mail_relay_secret: str,
    dry_run: bool,
) -> list[str]:
    """Deploy worker + configure email routing for one account. Returns error strings."""
    errors: list[str] = []
    email = acct["email"]
    api_key = acct["api_key"]
    domains: list[str] = acct.get("domains", [])
    account_id_override: str | None = acct.get("account_id")

    print(f"\n[{idx}/{total}] Account: {email}")
    print(f"  Domains  : {', '.join(domains) if domains else '(none)'}")

    client = CloudflareClient(email, api_key)

    # 1. Resolve account ID
    try:
        if dry_run and not account_id_override:
            account_id = "<account-id-would-be-resolved>"
            print(f"  Account ID: {account_id} (dry-run, skipping API call)")
        else:
            account_id = account_id_override or client.get_account_id()
            print(f"  Account ID: {account_id}")
    except Exception as exc:
        print(f"  ✗ Cannot resolve account ID: {exc}")
        errors.append(f"{email}: {exc}")
        return errors

    # 2. Upload / update the bridge Worker
    try:
        client.upload_worker(
            account_id, worker_name, main_worker_url, mail_relay_secret, dry_run
        )
    except Exception as exc:
        print(f"  ✗ Worker upload failed: {exc}")
        errors.append(f"{email} [worker]: {exc}")
        return errors   # No point configuring domains if worker upload failed

    # 3. Per-domain: Email Routing configuration
    for domain in domains:
        print(f"\n  ── Domain: {domain}")
        try:
            if dry_run:
                zone_id = "<zone-id-would-be-resolved>"
                print(f"    Zone ID: {zone_id} (dry-run, skipping API call)")
            else:
                zone_id = client.get_zone_id(domain)
                print(f"    Zone ID: {zone_id}")
        except Exception as exc:
            print(f"    ✗ Zone lookup failed: {exc}")
            errors.append(f"{email}/{domain} [zone]: {exc}")
            continue

        # Enable Email Routing (idempotent; shows required DNS records on failure)
        client.enable_email_routing(zone_id, dry_run)
        if not dry_run:
            time.sleep(REQUEST_DELAY)

        if not dry_run:
            dns_records = client.get_email_routing_dns(zone_id)
            if dns_records:
                missing = [r for r in dns_records if not r.get("eligible")]
                if missing:
                    print(
                        "    ℹ Required Email Routing DNS records not yet active "
                        "(Email Routing may not accept mail until propagated):"
                    )
                    for rec in missing:
                        print(f"      {rec.get('type', '?')} {rec.get('name', '?')} → {rec.get('value', {})}")

        # Set Catch-all → Worker
        try:
            client.set_catch_all_worker(zone_id, worker_name, dry_run)
        except Exception as exc:
            print(f"    ✗ Catch-all setup failed: {exc}")
            errors.append(f"{email}/{domain} [catch-all]: {exc}")

        time.sleep(REQUEST_DELAY)

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch deploy bridge Worker + Email Routing across CF accounts"
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "accounts.yaml"),
        help="Path to accounts YAML config (default: accounts.yaml next to this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without making any changes",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            f"[error] Config file not found: {config_path}\n"
            f"Copy accounts.example.yaml → accounts.yaml and fill in your credentials.",
            file=sys.stderr,
        )
        sys.exit(1)

    cfg = load_config(config_path)
    main_worker_url: str = cfg["main_worker_url"].rstrip("/")
    mail_relay_secret: str = cfg["mail_relay_secret"]
    worker_name: str = cfg.get("worker_name", "cloudflare-email-relay-bridge")
    accounts: list[dict] = cfg.get("accounts", [])

    if not accounts:
        print("[error] No accounts defined in config", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("=" * 60)
        print("DRY RUN — no changes will be made")
        print("=" * 60)

    all_errors: list[str] = []
    total = len(accounts)

    for idx, acct in enumerate(accounts, start=1):
        errs = process_account(
            idx, total, acct,
            worker_name, main_worker_url, mail_relay_secret,
            args.dry_run,
        )
        all_errors.extend(errs)

    # Summary
    print("\n" + "─" * 60)
    if all_errors:
        print(f"Finished with {len(all_errors)} error(s):")
        for err in all_errors:
            print(f"  ✗ {err}")
        sys.exit(1)
    else:
        if args.dry_run:
            print("Dry-run complete — no changes made.")
        else:
            print("All accounts configured successfully ✓")


if __name__ == "__main__":
    main()
