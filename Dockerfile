FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /code/
COPY pyproject.toml uv.lock ./

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
RUN uv sync --all-groups --frozen

COPY src/ src/
COPY scripts/ scripts/
COPY tests/ tests/

RUN uv run ruff check src/ tests/

CMD ["python", "-u", "/code/src/component.py"]
