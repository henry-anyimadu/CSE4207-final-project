FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY gamesim ./gamesim
RUN pip install . && useradd --create-home --uid 10001 sim
COPY scenarios ./scenarios

USER sim
# Override with -e GAMESIM_CONFIG=/app/scenarios/rocket.yaml, -e ORIGIN_URL=..., etc.
ENV GAMESIM_CONFIG=/app/scenarios/benchmark.yaml
EXPOSE 9100
ENTRYPOINT ["gamesim"]
CMD ["run"]
