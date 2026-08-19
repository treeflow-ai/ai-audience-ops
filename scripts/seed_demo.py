from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import build_engine, init_db
from app.seed import seed_synthetic_data


def main() -> None:
    settings = Settings()
    engine = build_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        seed_synthetic_data(session, count=settings.synthetic_student_count, force=True)
    print(f"Seeded {settings.synthetic_student_count:,} synthetic students into {settings.database_url}")


if __name__ == "__main__":
    main()
