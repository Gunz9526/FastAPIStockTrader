# ===== Build Stage with Conda =====
FROM continuumio/miniconda3:latest AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create conda environment with Python 3.14
RUN conda create -n trading python=3.14 -y && \
    conda clean -afy

# Activate environment and install CatBoost via conda-forge
RUN /bin/bash -c "source activate trading && \
    conda install -c conda-forge catboost=1.2.8 -y && \
    conda clean -afy"

# Install other Python packages via pip
COPY requirements.txt ./
RUN /bin/bash -c "source activate trading && \
    pip install --no-cache-dir -r requirements.txt"

# ===== Runtime Stage =====
FROM continuumio/miniconda3:latest

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy conda environment from builder
COPY --from=builder /opt/conda/envs/trading /opt/conda/envs/trading

# Set environment
ENV PATH=/opt/conda/envs/trading/bin:$PATH
ENV CONDA_DEFAULT_ENV=trading

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]