FROM python:3.13-slim-trixie AS nut-builder

ARG NUT_VERSION=2.8.5
ARG NUT_SHA256=18bf32e59eb764b13da3c4fa70384926d7fa584cb31d2fe7f137a570633eeec1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libltdl-dev \
        libsnmp-dev \
        libssl-dev \
        libusb-1.0-0-dev \
        pkgconf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/nut-source
RUN curl -fsSL "https://networkupstools.org/source/2.8/nut-${NUT_VERSION}.tar.gz" \
        -o /tmp/nut.tar.gz \
    && echo "${NUT_SHA256}  /tmp/nut.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/nut.tar.gz --strip-components=1 \
    && ./configure \
        --prefix=/usr \
        --sysconfdir=/etc/nut \
        --libexecdir=/usr/libexec \
        --with-statepath=/run/nut \
        --with-altpidpath=/run/nut \
        --with-pidpath=/run/nut \
        --with-drvpath=/usr/libexec/nut \
        --with-user=nut \
        --with-group=nut \
        --with-drivers=usbhid-ups,dummy-ups,snmp-ups \
        --with-usb=yes \
        --with-snmp=yes \
        --with-nut-scanner=yes \
        --with-openssl \
        --without-cgi \
        --without-dev \
        --without-python2 \
        --without-python3 \
        --with-doc=no \
    && make -j2 \
    && make DESTDIR=/opt/nut-root install

FROM python:3.13-slim-trixie

ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        iputils-ping \
        libltdl7 \
        libsnmp40t64 \
        libssl3t64 \
        libusb-1.0-0 \
        passwd \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system nut \
    && useradd --system --gid nut --home-dir /var/lib/nut --shell /usr/sbin/nologin nut \
    && mkdir -p /app /data /run/nut \
    && chown -R nut:nut /data /run/nut

COPY --from=nut-builder /opt/nut-root/ /

WORKDIR /app
COPY --chown=nut:nut pyproject.toml README.md ./
COPY --chown=nut:nut nupson ./nupson
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN ldconfig \
    && find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} + \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && python -m compileall -q /app/nupson \
    && test -x /usr/libexec/nut/usbhid-ups \
    && test -x /usr/libexec/nut/dummy-ups \
    && test -x /usr/libexec/nut/snmp-ups \
    && test "$(upsd -V 2>&1 | awk 'NR == 1 {print $5}')" = "2.8.5"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NUPSON_DATA_DIR=/data \
    NUPSON_HTTP_HOST=0.0.0.0 \
    NUPSON_HTTP_PORT=8080 \
    NUPSON_MANAGE_NUT=true

VOLUME ["/data"]
EXPOSE 8080 3493

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "nupson.app"]
