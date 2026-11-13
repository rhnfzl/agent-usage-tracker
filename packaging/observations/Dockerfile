FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY src/agent_usage_tracker/ /app/src/agent_usage_tracker/
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home aut && mkdir /data && chown aut:aut /data && chmod 700 /data
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=3)"
CMD ["aut", "observe", "--db", "/data/observations.sqlite3", "serve", "--host", "0.0.0.0"]
