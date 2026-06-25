FROM python:3.12-slim

RUN pip install --no-cache-dir flask requests

WORKDIR /app
COPY app.py .

EXPOSE 8080

CMD ["python", "app.py"]
