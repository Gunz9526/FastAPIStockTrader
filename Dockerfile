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

# Copy environment file first (better layer caching)
COPY environment.yml ./

# Create conda environment from environment.yml (includes Python 3.14 + CatBoost)
RUN conda env create -f environment.yml && \
    conda clean -afy

# Verify CatBoost installation
RUN /bin/bash -c "source activate trading && python -c 'import catboost; print(catboost.__version__)'"

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