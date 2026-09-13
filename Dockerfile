# WSJT-X QSO Analyzer (MMANA-GAL & Solar Companion)
FROM python:3.12-alpine

WORKDIR /app

# The service uses standard library modules (socket, struct, http.server, urllib, threading)
# No heavy third-party dependencies required.

COPY app.py index.html cty.dat grids_na.json ./
COPY patterns/ ./patterns/

ENV PYTHONUNBUFFERED=1 \
    HTTP_PORT=8080 \
    UDP_PORT=2237 \
    HOME_GRID=EL09 \
    NORTH_OFFSET_DEG=0.0 \
    PATTERNS_DIR=/app/patterns

EXPOSE 8080
EXPOSE 2237/udp

CMD ["python", "app.py"]
