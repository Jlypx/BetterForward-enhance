FROM python:3.12-alpine

ARG ENABLE_REDIS=false

WORKDIR /app

COPY locale /app/locale
COPY requirements.txt requirements-redis.txt /tmp/

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
    && if [ "$ENABLE_REDIS" = "true" ]; then pip install --no-cache-dir -r /tmp/requirements-redis.txt; fi \
    && rm -f /tmp/requirements.txt /tmp/requirements-redis.txt \
    && mkdir -p /app/data \
    && find /app/locale -name '*.po' -type f -delete

ADD main.py /app
ADD src /app/src
ADD db_migrate /app/db_migrate

# Default environment variables (non-sensitive)
ENV LANGUAGE="en_US"
ENV WORKERS="2"
ENV QUEUE_SIZE="1000"
ENV GROUP_QUEUE_SIZE="200"
ENV PER_USER_QUEUE_SIZE="5"
ENV UNVERIFIED_RATE="0.1"
ENV UNVERIFIED_BURST="1"
ENV VERIFIED_RATE="0.1666666667"
ENV VERIFIED_BURST="3"
ENV PRIORITY_RATE="0.5"
ENV PRIORITY_BURST="10"
ENV PRIORITY_INACTIVITY_SECONDS="86400"
ENV GLOBAL_RATE="10"
ENV GLOBAL_BURST="20"
ENV ABUSE_BLOCK_THRESHOLD="20"
ENV ABUSE_BLOCK_SECONDS="3600"
ENV RATE_LIMIT_STATE_SIZE="10000"
ENV REDIS_URL=""
ENV REDIS_PREFIX="betterforward-enhance"
ENV WEBAPP_ENABLED="disable"
ENV WEBAPP_PUBLIC_URL=""
ENV TURNSTILE_SITE_KEY=""
ENV TURNSTILE_SECRET_KEY=""
ENV TURNSTILE_HOSTNAME=""
ENV WEBAPP_HOST="0.0.0.0"
ENV WEBAPP_PORT="8080"
ENV WEBAPP_AUTH_MAX_AGE="300"

EXPOSE 8080

# Sensitive variables should be passed at runtime via -e flag
# ENV TOKEN="" and ENV GROUP_ID="" removed for security

CMD ["sh", "-c", "python -u /app/main.py -token \"${TOKEN}\" -group_id \"${GROUP_ID}\" -language \"${LANGUAGE}\" -tg_api \"${TG_API}\" -workers \"${WORKERS}\" -queue_size \"${QUEUE_SIZE}\" -group_queue_size \"${GROUP_QUEUE_SIZE}\" -per_user_queue_size \"${PER_USER_QUEUE_SIZE}\" -unverified_rate \"${UNVERIFIED_RATE}\" -unverified_burst \"${UNVERIFIED_BURST}\" -verified_rate \"${VERIFIED_RATE}\" -verified_burst \"${VERIFIED_BURST}\" -priority_rate \"${PRIORITY_RATE}\" -priority_burst \"${PRIORITY_BURST}\" -priority_inactivity_seconds \"${PRIORITY_INACTIVITY_SECONDS}\" -global_rate \"${GLOBAL_RATE}\" -global_burst \"${GLOBAL_BURST}\" -abuse_block_threshold \"${ABUSE_BLOCK_THRESHOLD}\" -abuse_block_seconds \"${ABUSE_BLOCK_SECONDS}\" -rate_limit_state_size \"${RATE_LIMIT_STATE_SIZE}\" -redis_url \"${REDIS_URL}\" -redis_prefix \"${REDIS_PREFIX}\" -webapp_enabled \"${WEBAPP_ENABLED}\" -webapp_public_url \"${WEBAPP_PUBLIC_URL}\" -turnstile_site_key \"${TURNSTILE_SITE_KEY}\" -turnstile_secret_key \"${TURNSTILE_SECRET_KEY}\" -turnstile_hostname \"${TURNSTILE_HOSTNAME}\" -webapp_host \"${WEBAPP_HOST}\" -webapp_port \"${WEBAPP_PORT}\" -webapp_auth_max_age \"${WEBAPP_AUTH_MAX_AGE}\""]
