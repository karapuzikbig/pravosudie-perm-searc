FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# tkinter + системные библиотеки для GUI
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-tk xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# В образе Playwright браузер уже установлен; системные зависимости — тоже.
COPY . .

RUN mkdir -p data logs

# GUI требует X-сервер. На сервере без графики запускайте через Xvfb/VNC
# или локально (см. README). Здесь — запуск под виртуальным дисплеем.
CMD ["xvfb-run", "-a", "python", "-m", "src.main"]
