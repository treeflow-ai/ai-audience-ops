# LearnDash Integration Notes

The runnable demo uses a local synthetic source because a public GitHub project should not require a real WordPress/LearnDash site or contain student data.

## Data mapping

| Demo field | Likely real source |
|---|---|
| `Student.external_id` | WordPress user ID |
| `Course.external_id` | LearnDash course/post ID |
| course membership | LearnDash course-users or user-courses REST endpoints |
| completion state/date | LearnDash progress/activity/reporting data or reporting warehouse |
| learner profile | organization-specific user meta / CRM / analytics attribute |
| marketing consent | organization-specific consent store / WordPress user meta / marketing preference center |
| email suppression | downstream ESP and/or internal suppression store |

## Included adapter

`app/adapters/learndash.py` demonstrates authenticated reads for the documented V1 endpoints:

```text
GET /wp-json/ldlms/v1/sfwd-courses/{course_id}/users
GET /wp-json/ldlms/v1/users/{user_id}/courses
```

The adapter uses standard HTTP basic credentials suitable for a WordPress application-password style setup when configured.

## Why the repo does not fake a universal completion endpoint

LearnDash exposes progress/activity capabilities, but the exact data available to a given deployment depends on LearnDash version, WordPress authentication, installed add-ons, reporting choices, and custom metadata. The reference implementation therefore keeps completion timestamps in a clearly synthetic local model instead of claiming that every LearnDash site can be imported with one generic call.

A production implementation would add a site-specific ingestion job behind the same domain schema.
