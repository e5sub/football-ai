FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend /app/backend
COPY tools /app/tools
COPY index.html /app/index.html
COPY admin.html /app/admin.html
COPY sw.js /app/sw.js
COPY data /app/data

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
