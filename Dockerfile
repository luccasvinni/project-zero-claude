FROM python:3.12-slim

# Dependências do sistema
RUN apt-get update && apt-get install -y \
    # Poppler — necessário para pdf2image
    poppler-utils \
    # ZBar — necessário para pyzbar (leitura de QR codes)
    libzbar0 \
    # Dependências do Playwright/Chromium
    chromium \
    chromium-driver \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    # Utilitários
    curl \
    && rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# Copiar e instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Playwright e o Chromium dele (usado como fallback no Agente 1)
RUN playwright install chromium --with-deps || true

# Copiar o código-fonte
COPY . .

# Criar diretório de output (dados persistidos via volume)
RUN mkdir -p output

# Porta do dashboard FastAPI
EXPOSE 8502

# Comando de inicialização
CMD ["python", "src/dashboard/app.py"]
