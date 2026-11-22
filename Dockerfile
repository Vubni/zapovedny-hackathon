FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements-container.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements-container.txt \
    && groupadd --gid 10001 meteo \
    && useradd --uid 10001 --gid meteo --no-create-home --shell /usr/sbin/nologin meteo

COPY *.py ./
COPY web/ web/
COPY data/meteo_hack_dataset.zip data/
COPY docs/МЕТОДИКА.md docs/
COPY results/weather.sqlite results/build.json results/
COPY results/model/temperature.json results/model/
COPY results/examples/kr1-station-2024-04-01.json results/examples/
COPY results/review/review-example.xlsx results/review/
COPY results/review/external/ results/review/external/
USER 10001:10001
EXPOSE 8766
CMD ["python", "serve_container.py"]
