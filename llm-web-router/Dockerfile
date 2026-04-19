FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# We rely on playwright image, so chromium is already installed
# RUN playwright install chromium --with-deps

COPY . .

# Expose API port
EXPOSE 8000

CMD ["python", "server.py"]
