FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install requests==2.32.3 mutagen==1.47.0

WORKDIR /app
COPY normalize.py slskd_fetch.py collect.py vpm /app/
RUN chmod +x /app/vpm && ln -s /app/vpm /usr/local/bin/vpm

ENTRYPOINT ["vpm"]
CMD ["ayuda"]
