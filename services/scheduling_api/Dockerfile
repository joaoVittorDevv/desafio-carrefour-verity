FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir '.[api]'
# Local PII detector (spaCy pt_core_news_sm) is required; writes are rejected without it.
RUN python -m spacy download pt_core_news_sm
CMD ["uvicorn", "services.scheduling_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
