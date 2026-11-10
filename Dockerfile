FROM python:3.13.6-slim
WORKDIR /app
COPY requirements.txt requirements-ml.txt ./
ARG INSTALL_SENTIMENT=false
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_SENTIMENT" = true ]; then pip install --no-cache-dir -r requirements-ml.txt; fi
COPY . .
RUN useradd --create-home agent && mkdir -p logs data journals oauth /home/agent/.cache/huggingface && chown -R agent:agent /app /home/agent
USER agent
ENV HF_HOME=/home/agent/.cache/huggingface
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s CMD ["python", "scripts/healthcheck.py"]
CMD ["python", "main.py"]
