FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir '.[rag]'
# Pre-download the prescribed E5 384-d ONNX export into the image cache.
RUN python -c "from services.mcp_rag.hybrid_search import load_embedder; load_embedder()"
CMD ["python", "-m", "services.mcp_rag.server"]
