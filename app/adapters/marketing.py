from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import Settings


@dataclass(slots=True)
class Recipient:
    external_id: str
    email: str
    first_name: str
    last_name: str


@dataclass(slots=True)
class SyncResult:
    provider: str
    external_segment_id: str
    synced_count: int
    detail: str


class MarketingAdapter(ABC):
    @abstractmethod
    def sync(self, audience_key: str, recipients: list[Recipient]) -> SyncResult:
        raise NotImplementedError


class MockMarketingAdapter(MarketingAdapter):
    def __init__(self, settings: Settings, provider: str):
        self.settings = settings
        self.provider = provider

    def sync(self, audience_key: str, recipients: list[Recipient]) -> SyncResult:
        self.settings.mock_sync_log.parent.mkdir(parents=True, exist_ok=True)
        prefix = "mc-seg" if self.provider == "mock_mailchimp" else "cc-list"
        segment_id = f"{prefix}-{audience_key.lower()}"
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": self.provider,
            "audience_key": audience_key,
            "external_segment_id": segment_id,
            "recipient_count": len(recipients),
            # Privacy-preserving mock log: no contact-level identifiers or
            # email addresses are written. The real adapter receives governed
            # records in memory, but the public demo log stays aggregate-only.
        }
        with self.settings.mock_sync_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        return SyncResult(
            provider=self.provider,
            external_segment_id=segment_id,
            synced_count=len(recipients),
            detail=f"Mock sync recorded locally at {self.settings.mock_sync_log}; no email was sent.",
        )


class MailchimpAdapter(MarketingAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings

    def sync(self, audience_key: str, recipients: list[Recipient]) -> SyncResult:
        _guard_real_sync(self.settings, recipients)
        if not all([self.settings.mailchimp_api_key, self.settings.mailchimp_server_prefix, self.settings.mailchimp_list_id]):
            raise RuntimeError("MAILCHIMP_API_KEY, MAILCHIMP_SERVER_PREFIX and MAILCHIMP_LIST_ID are required")

        base = f"https://{self.settings.mailchimp_server_prefix}.api.mailchimp.com/3.0"
        auth = ("audience-ops", self.settings.mailchimp_api_key)
        tag = f"audience:{audience_key}"
        with httpx.Client(base_url=base, auth=auth, timeout=30.0) as client:
            for recipient in recipients:
                subscriber_hash = hashlib.md5(recipient.email.strip().lower().encode("utf-8")).hexdigest()
                response = client.put(
                    f"/lists/{self.settings.mailchimp_list_id}/members/{subscriber_hash}",
                    json={
                        "email_address": recipient.email,
                        "status_if_new": "subscribed",
                        "merge_fields": {"FNAME": recipient.first_name, "LNAME": recipient.last_name},
                    },
                )
                response.raise_for_status()
                tag_response = client.post(
                    f"/lists/{self.settings.mailchimp_list_id}/members/{subscriber_hash}/tags",
                    json={"tags": [{"name": tag, "status": "active"}]},
                )
                tag_response.raise_for_status()
        return SyncResult(
            provider="mailchimp",
            external_segment_id=tag,
            synced_count=len(recipients),
            detail="Contacts were upserted to the configured Mailchimp list and tagged with the audience key.",
        )


class ConstantContactAdapter(MarketingAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings

    def sync(self, audience_key: str, recipients: list[Recipient]) -> SyncResult:
        _guard_real_sync(self.settings, recipients)
        if not self.settings.constant_contact_access_token:
            raise RuntimeError("CONSTANT_CONTACT_ACCESS_TOKEN is required")

        headers = {
            "Authorization": f"Bearer {self.settings.constant_contact_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client(base_url="https://api.cc.email/v3", headers=headers, timeout=30.0) as client:
            list_id = self.settings.constant_contact_list_id
            if not list_id:
                # The audience key makes the downstream list name stable. Look
                # for an existing list first so a retry after a partial failure
                # does not create a duplicate or fail on a name conflict.
                list_name = f"Audience {audience_key}"
                lookup_response = client.get("/contact_lists", params={"name": list_name, "limit": 50})
                lookup_response.raise_for_status()
                matches = [
                    item for item in lookup_response.json().get("lists", [])
                    if item.get("name") == list_name and item.get("list_id")
                ]
                if matches:
                    list_id = matches[0]["list_id"]
                else:
                    list_response = client.post(
                        "/contact_lists",
                        json={"name": list_name, "description": "Created by AI Audience Ops demo"},
                    )
                    list_response.raise_for_status()
                    list_id = list_response.json()["list_id"]

            import_response = client.post(
                "/activities/contacts_json_import",
                json={
                    "import_data": [
                        {
                            "email": r.email,
                            "first_name": r.first_name,
                            "last_name": r.last_name,
                        }
                        for r in recipients
                    ],
                    "list_ids": [list_id],
                },
            )
            import_response.raise_for_status()
            activity_id = import_response.json().get("activity_id")
            if not activity_id:
                raise RuntimeError("Constant Contact did not return an activity_id for the bulk import.")

            deadline = time.monotonic() + self.settings.constant_contact_activity_timeout_seconds
            activity: dict = {}
            while time.monotonic() < deadline:
                status_response = client.get(f"/activities/{activity_id}")
                status_response.raise_for_status()
                activity = status_response.json()
                state = str(activity.get("state", "")).lower()
                if state == "completed" or (
                    activity.get("percent_done") == 100 and activity.get("completed_at")
                ):
                    break
                if state in {"cancelled", "failed", "timed_out", "time_out"}:
                    raise RuntimeError(f"Constant Contact activity {activity_id} ended with state={state}.")
                time.sleep(0.75)
            else:
                raise TimeoutError(
                    "Constant Contact bulk import did not complete within "
                    f"{self.settings.constant_contact_activity_timeout_seconds} seconds "
                    f"(activity_id={activity_id})."
                )

            errors = activity.get("activity_errors") or []
            error_count = int((activity.get("status") or {}).get("error_count") or 0)
            if errors or error_count:
                raise RuntimeError(
                    f"Constant Contact activity {activity_id} completed with "
                    f"{error_count or len(errors)} import error(s)."
                )
        return SyncResult(
            provider="constantcontact",
            external_segment_id=list_id,
            synced_count=len(recipients),
            detail=f"Constant Contact bulk import completed successfully: {activity_id}.",
        )


def _guard_real_sync(settings: Settings, recipients: list[Recipient]) -> None:
    if not settings.allow_real_marketing_sync:
        raise RuntimeError("Real marketing sync is disabled. Set ALLOW_REAL_MARKETING_SYNC=true explicitly.")
    if len(recipients) > settings.real_sync_max_recipients:
        raise RuntimeError(
            f"Real sync recipient count {len(recipients)} exceeds REAL_SYNC_MAX_RECIPIENTS={settings.real_sync_max_recipients}."
        )


def get_marketing_adapter(provider: str, settings: Settings) -> MarketingAdapter:
    if provider in {"mock_mailchimp", "mock_constantcontact"}:
        return MockMarketingAdapter(settings, provider)
    if provider == "mailchimp":
        return MailchimpAdapter(settings)
    if provider == "constantcontact":
        return ConstantContactAdapter(settings)
    raise ValueError(f"Unsupported marketing provider: {provider}")
