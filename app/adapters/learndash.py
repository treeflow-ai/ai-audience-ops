from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings


@dataclass(slots=True)
class LearnDashClient:
    """Thin optional adapter around documented LearnDash REST endpoints.

    The demo does not require a live WordPress/LearnDash instance. This class is
    intentionally small because real deployments often need site-specific user
    meta for marketing consent and learner attributes.
    """

    settings: Settings

    def _auth(self) -> tuple[str, str] | None:
        if self.settings.learndash_username and self.settings.learndash_app_password:
            return (self.settings.learndash_username, self.settings.learndash_app_password)
        return None

    def _url(self, path: str) -> str:
        if not self.settings.learndash_base_url:
            raise RuntimeError("LEARNDASH_BASE_URL is not configured")
        return self.settings.learndash_base_url.rstrip("/") + "/wp-json" + path

    def list_course_users(self, course_id: int, per_page: int = 100) -> list[int]:
        url = self._url(f"/ldlms/v1/sfwd-courses/{course_id}/users")
        user_ids: list[int] = []
        page = 1
        with httpx.Client(auth=self._auth(), timeout=20.0) as client:
            while True:
                response = client.get(url, params={"fields": "ids", "per_page": per_page, "page": page})
                response.raise_for_status()
                payload = response.json()
                chunk = payload if isinstance(payload, list) else payload.get("user_ids", [])
                if not chunk:
                    break
                user_ids.extend(int(x) for x in chunk)
                if len(chunk) < per_page:
                    break
                page += 1
        return user_ids

    def list_user_courses(self, user_id: int, per_page: int = 100) -> list[int]:
        url = self._url(f"/ldlms/v1/users/{user_id}/courses")
        course_ids: list[int] = []
        page = 1
        with httpx.Client(auth=self._auth(), timeout=20.0) as client:
            while True:
                response = client.get(
                    url,
                    params={"fields": "ids", "per_page": per_page, "page": page},
                )
                response.raise_for_status()
                payload = response.json()
                chunk = payload if isinstance(payload, list) else payload.get("course_ids", [])
                if not chunk:
                    break
                course_ids.extend(int(x) for x in chunk)
                if len(chunk) < per_page:
                    break
                page += 1
        return course_ids
