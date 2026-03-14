FROM python:3.11-slim

# System deps for Playwright/Chromium + Pillow (Debian Trixie compatible)
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libglib2.0-0t64 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2t64 \
    libpango-1.0-0 libcairo2 libxshmfence1 \
    fonts-liberation libfontconfig1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 \
    libxrender1 libxi6 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install chromium WITHOUT --with-deps (deps already installed above)
RUN playwright install chromium

COPY . .

RUN mkdir -p recordings screenshots voice

ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "app.py"]
