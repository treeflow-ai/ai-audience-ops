from __future__ import annotations

import re
from pathlib import Path

from .schemas import RetrievedPolicy

STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "is", "are",
    "with", "who", "all", "this", "that", "please", "within", "last", "can", "may",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_]+", text.lower())
        if len(token) > 2 and token not in STOP_WORDS
    }


class PolicyRetriever:
    """Small local retrieval layer for transparent, dependency-light RAG.

    Markdown policy sections are scored lexically. In OpenAI mode the retrieved
    context can be shown to the user and extended into a model prompt, while the
    final allow/block decision remains deterministic.
    """

    def __init__(self, policy_dir: Path):
        self.sections: list[tuple[str, str, str]] = []
        policy_paths = sorted(policy_dir.glob("*.md"))
        if not policy_paths:
            raise FileNotFoundError(f"No Markdown policy files found in {policy_dir}.")
        for path in policy_paths:
            text = path.read_text(encoding="utf-8")
            current_title = path.stem.replace("-", " ").title()
            current: list[str] = []
            for line in text.splitlines():
                if line.startswith("## "):
                    if current:
                        self.sections.append((path.stem, current_title, "\n".join(current).strip()))
                    current_title = line.removeprefix("## ").strip()
                    current = []
                elif not line.startswith("# "):
                    current.append(line)
            if current:
                self.sections.append((path.stem, current_title, "\n".join(current).strip()))

    def search(self, query: str, limit: int = 5) -> list[RetrievedPolicy]:
        q = _tokens(query)
        ranked: list[RetrievedPolicy] = []
        for policy_id, title, body in self.sections:
            title_tokens = _tokens(title)
            body_tokens = _tokens(body)
            score = 3 * len(q & title_tokens) + len(q & body_tokens)
            if score > 0:
                excerpt = re.sub(r"\s+", " ", body).strip()[:360]
                ranked.append(RetrievedPolicy(policy_id=policy_id, title=title, excerpt=excerpt, score=score))
        ranked.sort(key=lambda item: (-item.score, item.policy_id, item.title))
        return ranked[:limit]
