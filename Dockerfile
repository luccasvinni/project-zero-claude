FROM mcr.microsoft.com/playwright/python:v1.52.0

# Diretório de trabalho
WORKDIR /app

# Dependências extras do sistema
RUN apt-get update && apt-get install -y \
    poppler-utils \
    libzbar0 \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements
COPY requirements.txt .

# Instala dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia projeto
COPY . .

# Cria pasta de output
RUN mkdir -p output

# Porta do FastAPI
EXPOSE 8502

# Inicialização
CMD ["python", "src/dashboard/app.py"]