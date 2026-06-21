FROM python:3.13-slim-trixie AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile --only-binary=:all: --root-user-action=ignore \
        --target=/packages -r requirements.txt \
    && find /packages -type d -name "__pycache__" -prune -exec rm -rf '{}' +

FROM gcr.io/distroless/python3-debian13:nonroot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/usr/local/lib/python3.13/dist-packages

WORKDIR /app

COPY --from=builder /packages /usr/local/lib/python3.13/dist-packages
COPY kasa_exporter.py .

EXPOSE 9233

CMD ["kasa_exporter.py"]
