FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY unirouter ./unirouter
ENV DATA_DIR=/app/data PORT=20128
VOLUME /app/data
EXPOSE 20128
CMD ["python", "main.py"]
