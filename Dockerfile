# Runs the watcher in a container, as an unprivileged user. See "Running it" in the README.
FROM python:3.12-slim
WORKDIR /app
COPY stock_alert.py stores_uk.json models_uk.json config.example.json LICENSE ./
USER nobody
# Mount your config.json at /config/config.json and pass the two Telegram
# values as environment variables.
CMD ["python3", "-u", "-I", "-B", "/app/stock_alert.py", "--config", "/config/config.json"]
