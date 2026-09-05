from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from ..config import Settings


OperationCheckpoint = Callable[[str, str | None], None]
Heartbeat = Callable[[], None]


class MarketingAdapterError(RuntimeError):
    """Base class for adapter errors that are safe to surface to the sync worker."""


class RetryableMarketingAdapterError(MarketingAdapterError):
    """A transient failure; retrying the same batch/idempotency key is allowed."""


class PermanentMarketingAdapterError(MarketingAdapterError):
    """A configuration or request failure that should not be automatically retried."""


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
    external_operation_id: str | None = None


class MarketingAdapter(ABC):
    @abstractmethod
    def sync_batch(
        self,
        audience_key: str,
        recipients: list[Recipient],
        *,
        idempotency_key: str,
        external_segment_id: str | None = None,
        external_operation_id: str | None = None,
        checkpoint_operation: OperationCheckpoint | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> SyncResult:
        raise NotImplementedError

    # Backward-compatible convenience method for existing callers/tests.
    def sync(self, audience_key: str, recipients: list[Recipient]) -> SyncResult:
        fallback_key = hashlib.sha256(f"legacy:{audience_key}".encode("utf-8")).hexdigest()
        return self.sync_batch(audience_key, recipients, idempotency_key=fallback_key)


class MockMarketingAdapter(MarketingAdapter):
    def __init__(self, settings: Settings, provider: str):
        self.settings = settings
        self.provider = provider

    def sync_batch(
        self,
        audience_key: str,
        recipients: list[Recipient],
        *,
        idempotency_key: str,
        external_segment_id: str | None = None,
        external_operation_id: str | None = None,
        checkpoint_operation: OperationCheckpoint | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> SyncResult:
        if heartbeat:
            heartbeat()
        self.settings.mock_sync_log.parent.mkdir(parents=True, exist_ok=True)
        prefix = "mc-seg" if self.provider == "mock_mailchimp" else "cc-list"
        segment_id = external_segment_id or f"{prefix}-{audience_key.lower()}"

        # The DB batch claim is the primary idempotency guard. The mock log also
        # de-duplicates by key so a local crash/replay does not create duplicate
        # demo side effects.
        if self.settings.mock_sync_log.exists():
            for line in self.settings.mock_sync_log.read_text(encoding="utf-8").splitlines():
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("idempotency_key") == idempotency_key:
                    return SyncResult(
                        provider=self.provider,
                        external_segment_id=existing.get("external_segment_id", segment_id),
                        synced_count=int(existing.get("recipient_count", len(recipients))),
                        detail="Mock sync replay detected; existing idempotent batch result reused.",
                        external_operation_id=existing.get("external_operation_id"),
                    )

        operation_id = external_operation_id or f"mock-{idempotency_key[:20]}"
        if checkpoint_operation:
            checkpoint_operation(operation_id, segment_id)
        if heartbeat:
            heartbeat()

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": self.provider,
            "audience_key": audience_key,
            "external_segment_id": segment_id,
            "external_operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "recipient_count": len(recipients),
            # Deliberately no contact-level identifiers or email addresses.
        }
        with self.settings.mock_sync_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        return SyncResult(
            provider=self.provider,
            external_segment_id=segment_id,
            synced_count=len(recipients),
            detail=f"Mock sync recorded locally at {self.settings.mock_sync_log}; no email was sent.",
            external_operation_id=operation_id,
        )


class MailchimpAdapter(MarketingAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings

    def sync_batch(
        self,
        audience_key: str,
        recipients: list[Recipient],
        *,
        idempotency_key: str,
        external_segment_id: str | None = None,
        external_operation_id: str | None = None,
        checkpoint_operation: OperationCheckpoint | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> SyncResult:
        _guard_real_sync(self.settings, recipients)
        if not all([self.settings.mailchimp_api_key, self.settings.mailchimp_server_prefix, self.settings.mailchimp_list_id]):
            raise PermanentMarketingAdapterError(
                "MAILCHIMP_API_KEY, MAILCHIMP_SERVER_PREFIX and MAILCHIMP_LIST_ID are required"
            )

        base = f"https://{self.settings.mailchimp_server_prefix}.api.mailchimp.com/3.0"
        auth = ("audience-ops", self.settings.mailchimp_api_key)
        tag = external_segment_id or f"audience:{audience_key}"
        operation_id = external_operation_id or f"mailchimp-{idempotency_key[:20]}"
        if checkpoint_operation:
            checkpoint_operation(operation_id, tag)
        if heartbeat:
            heartbeat()

        try:
            with httpx.Client(base_url=base, auth=auth, timeout=30.0) as client:
                for recipient in recipients:
                    if heartbeat:
                        heartbeat()
                    subscriber_hash = hashlib.md5(recipient.email.strip().lower().encode("utf-8")).hexdigest()
                    response = client.put(
                        f"/lists/{self.settings.mailchimp_list_id}/members/{subscriber_hash}",
                        json={
                            "email_address": recipient.email,
                            "status_if_new": "subscribed",
                            "merge_fields": {"FNAME": recipient.first_name, "LNAME": recipient.last_name},
                        }
                    )
                    _raise_for_response(response)
                    tag_response = client.post(
                        f"/lists/{self.settings.mailchimp_list_id}/members/{subscriber_hash}/tags",
                        json={"tags": [{"name": tag, "status": "active"}]}
                    )
                    _raise_for_response(tag_response)
                    if heartbeat:
                        heartbeat()
        except httpx.RequestError as exc:
            raise RetryableMarketingAdapterError(str(exc)) from exc

        return SyncResult(
            provider="mailchimp",
            external_segment_id=tag,
            synced_count=len(recipients),
            detail="Contacts were idempotently upserted to the configured Mailchimp list and tagged with the audience key.",
            external_operation_id=operation_id,
        )


class ConstantContactAdapter(MarketingAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings

    def sync_batch(
        self,
        audience_key: str,
        recipients: list[Recipient],
        *,
        idempotency_key: str,
        external_segment_id: str | None = None,
        external_operation_id: str | None = None,
        checkpoint_operation: OperationCheckpoint | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> SyncResult:
        _guard_real_sync(self.settings, recipients)
        if not self.settings.constant_contact_access_token:
            raise PermanentMarketingAdapterError("CONSTANT_CONTACT_ACCESS_TOKEN is required")

        headers = {
            "Authorization": f"Bearer {self.settings.constant_contact_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(base_url="https://api.cc.email/v3", headers=headers, timeout=30.0) as client:
                if heartbeat:
                    heartbeat()
                list_id = external_segment_id or self.settings.constant_contact_list_id
                if not list_id:
                    list_name = f"Audience {audience_key}"
                    if heartbeat:
                        heartbeat()
                    lookup_response = client.get("/contact_lists", params={"name": list_name, "limit": 50})
                    _raise_for_response(lookup_response)
                    matches = [
                        item
                        for item in lookup_response.json().get("lists", [])
                        if item.get("name") == list_name and item.get("list_id")
                    ]
                    if matches:
                        list_id = matches[0]["list_id"]
                    else:
                        if heartbeat:
                            heartbeat()
                        list_response = client.post(
                            "/contact_lists",
                            json={"name": list_name, "description": "Created by AI Audience Ops demo"},
                        )
                        _raise_for_response(list_response)
                        list_id = list_response.json()["list_id"]

                activity_id = external_operation_id
                if not activity_id:
                    if heartbeat:
                        heartbeat()
                    import_response = client.post(
                        "/activities/contacts_json_import",
                        json={
                            "import_data": [
                                {"email": r.email, "first_name": r.first_name, "last_name": r.last_name}
                                for r in recipients
                            ],
                            "list_ids": [list_id],
                        },
                        # Constant Contact does not document a true idempotency
                        # key for this operation; recovery relies on the persisted
                        # activity_id once the submission response is received.
                    )
                    _raise_for_response(import_response)
                    activity_id = import_response.json().get("activity_id")
                    if not activity_id:
                        raise PermanentMarketingAdapterError(
                            "Constant Contact did not return an activity_id for the bulk import."
                        )
                    if checkpoint_operation:
                        # Commit the external operation reference before polling.
                        # A retry can then resume polling instead of re-submitting.
                        checkpoint_operation(activity_id, list_id)

                activity = _poll_constant_contact_activity(
                    client, activity_id, self.settings, heartbeat=heartbeat
                )
                errors = activity.get("activity_errors") or []
                error_count = int((activity.get("status") or {}).get("error_count") or 0)
                if errors or error_count:
                    raise PermanentMarketingAdapterError(
                        f"Constant Contact activity {activity_id} completed with "
                        f"{error_count or len(errors)} import error(s)."
                    )
        except httpx.RequestError as exc:
            raise RetryableMarketingAdapterError(str(exc)) from exc

        return SyncResult(
            provider="constantcontact",
            external_segment_id=list_id,
            synced_count=len(recipients),
            detail=f"Constant Contact bulk import completed successfully: {activity_id}.",
            external_operation_id=activity_id,
        )


def _poll_constant_contact_activity(
    client: httpx.Client,
    activity_id: str,
    settings: Settings,
    *,
    heartbeat: Heartbeat | None = None,
) -> dict:
    deadline = time.monotonic() + settings.constant_contact_activity_timeout_seconds
    activity: dict = {}
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
        status_response = client.get(f"/activities/{activity_id}")
        _raise_for_response(status_response)
        activity = status_response.json()
        state = str(activity.get("state", "")).lower()
        if state == "completed" or (activity.get("percent_done") == 100 and activity.get("completed_at")):
            return activity
        if state in {"cancelled", "failed"}:
            raise PermanentMarketingAdapterError(
                f"Constant Contact activity {activity_id} ended with state={state}."
            )
        if state in {"timed_out", "time_out"}:
            raise RetryableMarketingAdapterError(
                f"Constant Contact activity {activity_id} ended with state={state}."
            )
        time.sleep(0.75)
    raise RetryableMarketingAdapterError(
        "Constant Contact bulk import did not complete within "
        f"{settings.constant_contact_activity_timeout_seconds} seconds "
        f"(activity_id={activity_id})."
    )


def _raise_for_response(response: httpx.Response) -> None:
    # Keep compatibility with the repository's lightweight fake responses,
    # while classifying real httpx failures into retryable vs permanent errors.
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        response.raise_for_status()
        return
    if status_code < 400:
        return
    text = getattr(response, "text", "")
    message = f"HTTP {status_code}: {text[:500]}"
    if status_code in {408, 409, 425, 429} or status_code >= 500:
        raise RetryableMarketingAdapterError(message)
    raise PermanentMarketingAdapterError(message)


def _guard_real_sync(settings: Settings, recipients: list[Recipient]) -> None:
    if not settings.allow_real_marketing_sync:
        raise PermanentMarketingAdapterError(
            "Real marketing sync is disabled. Set ALLOW_REAL_MARKETING_SYNC=true explicitly."
        )
    if len(recipients) > settings.real_sync_max_recipients:
        raise PermanentMarketingAdapterError(
            f"Real sync recipient count {len(recipients)} exceeds "
            f"REAL_SYNC_MAX_RECIPIENTS={settings.real_sync_max_recipients}."
        )


def get_marketing_adapter(provider: str, settings: Settings) -> MarketingAdapter:
    if provider in {"mock_mailchimp", "mock_constantcontact"}:
        return MockMarketingAdapter(settings, provider)
    if provider == "mailchimp":
        return MailchimpAdapter(settings)
    if provider == "constantcontact":
        return ConstantContactAdapter(settings)
    raise ValueError(f"Unsupported marketing provider: {provider}")
