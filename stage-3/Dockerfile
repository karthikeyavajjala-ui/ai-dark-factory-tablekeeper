# TableKeeper -- stage 3.
#
# Every runtime dependency is installed here, at build time, because the container
# has no outbound network once it is running. The service itself imports nothing but
# the Python standard library, so the only thing this image adds is the IANA time
# zone database -- and the build fails loudly if `zoneinfo` cannot read it, rather
# than at the first booking in a restaurant that observes DST.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

RUN pip install --no-cache-dir tzdata \
 && python -c "from zoneinfo import ZoneInfo; \
ZoneInfo('Europe/Berlin'); ZoneInfo('America/New_York')"

WORKDIR /app
COPY app ./app

# Run as a non-root user: the service keeps every byte of state in memory and never
# needs to write to its own filesystem.
RUN useradd --create-home --uid 10001 tablekeeper
USER 10001

EXPOSE 8080

# No curl in a slim image, and none needed -- the stdlib can ask the question.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 CMD ["python", "-c", "import os,urllib.request,sys;r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=2);sys.exit(0 if r.status==200 else 1)"]

CMD ["python", "-u", "-m", "app"]
