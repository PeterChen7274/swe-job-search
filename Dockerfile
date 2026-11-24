
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simplify_job_search.py .

ENV OUTPUT_DIR=/output
ENV STATE_FILE=/data/seen_jobs.json

CMD ["python", "-u", "simplify_job_search.py"]
