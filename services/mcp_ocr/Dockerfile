FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y libgomp1 && rm -rf /var/lib/apt/lists/*
COPY . /app
RUN pip install --no-cache-dir '.[ocr]' \
 && pip uninstall -y opencv-python opencv-python-headless \
 && pip install --no-cache-dir opencv-python-headless \
 && python -c "import cv2; assert cv2.__version__"
# Local PII detector (spaCy pt_core_news_sm) is required; the service fails closed without it.
RUN python -m spacy download pt_core_news_sm
CMD ["python", "-m", "services.mcp_ocr.server"]
