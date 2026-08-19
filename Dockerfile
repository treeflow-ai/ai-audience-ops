FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY policies ./policies
COPY scripts ./scripts
RUN pip install --no-cache-dir .

ENV DATABASE_URL=sqlite:////app/var/audience_ops.db \
    LLM_PROVIDER=mock \
    SYNTHETIC_STUDENT_COUNT=12000 \
    POLICY_DIR=/app/policies \
    MOCK_SYNC_LOG=/app/var/mock_marketing_syncs.jsonl

RUN mkdir -p /app/var
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
