FROM debian:bookworm-slim AS native-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN git clone --depth 1 --branch cpp-v1.3.0 \
    https://github.com/iamahsanmehmood/openskp.git /opt/openskp

COPY native/CMakeLists.txt native/skp2glb.cpp /build/native/

RUN cmake -S /build/native -B /build/native-build \
        -DCMAKE_BUILD_TYPE=Release \
        -DOPENSKP_SOURCE_DIR=/opt/openskp/packages/cpp \
    && cmake --build /build/native-build --config Release -j2

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NATIVE_CONVERTER=/app/bin/skp2glb

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY --from=native-builder /build/native-build/skp2glb /app/bin/skp2glb

RUN chmod +x /app/bin/skp2glb

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
