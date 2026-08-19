from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import build_engine, init_db
from app.seed import seed_synthetic_data


@pytest.fixture()
def demo(tmp_path: Path):
    db_path = tmp_path / "test.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        llm_provider="mock",
        approval_threshold=500,
        synthetic_student_count=2000,
        policy_dir=Path("policies"),
        mock_sync_log=tmp_path / "sync.jsonl",
    )
    engine = build_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        seed_synthetic_data(session, count=settings.synthetic_student_count)
    return settings, engine
