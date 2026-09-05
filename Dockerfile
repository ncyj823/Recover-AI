FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache-busting: this ARG's value changes on every build (Render passes a
# fresh timestamp), which invalidates Docker's layer cache for everything
# below it — guarantees COPY . . always picks up the latest source instead
# of reusing a stale cached layer from a previous deploy.
ARG CACHE_BUST=1
COPY . .

CMD ["uvicorn", "worker_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
