FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# В образе Playwright браузер уже установлен; системные зависимости — тоже.
COPY . .

RUN mkdir -p data logs

CMD ["python", "-m", "src.main"]
