FROM python:3.12-alpine

WORKDIR /app

COPY locale /app/locale
COPY requirements.txt /tmp/requirements.txt

# Install system dependencies for Pillow and CA certificates
RUN apk add --no-cache \
    ca-certificates \
    jpeg-dev \
    zlib-dev \
    freetype-dev \
    lcms2-dev \
    openjpeg-dev \
    tiff-dev \
    tk-dev \
    tcl-dev \
    harfbuzz-dev \
    fribidi-dev \
    libimagequant-dev \
    libxcb-dev \
    libpng-dev \
    gcc \
    musl-dev \
    && update-ca-certificates

RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && mkdir -p /app/data \
    && find /app/locale -name '*.po' -type f -delete

ADD main.py /app
ADD src /app/src
ADD db_migrate /app/db_migrate

ENV TOKEN=""
ENV GROUP_ID=""
ENV LANGUAGE="en_US"
ENV TG_API=""
ENV WORKER="2"

CMD python -u /app/main.py -token "$TOKEN" -group_id "$GROUP_ID" -language "$LANGUAGE" -tg_api "$TG_API" -worker "$WORKER"
