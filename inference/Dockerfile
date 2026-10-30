FROM debian:trixie-slim AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates cmake git \
    && rm -rf /var/lib/apt/lists/*

# Stable upstream releases, pinned to their source commits (2026-10-29).
RUN git init /src/llama \
    && git -C /src/llama remote add origin https://github.com/ggml-org/llama.cpp.git \
    && git -C /src/llama fetch --depth=1 origin 8642541b255bfb5dc39cd351a178be09013a55d6 \
    && git -C /src/llama checkout --detach FETCH_HEAD \
    && cmake -S /src/llama -B /src/llama/build \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_APP=OFF \
        -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
        -DLLAMA_OPENSSL=OFF -DLLAMA_SUBPROCESS=OFF \
    && cmake --build /src/llama/build --config Release --target llama-server -j2
RUN git init /src/whisper \
    && git -C /src/whisper remote add origin https://github.com/ggml-org/whisper.cpp.git \
    && git -C /src/whisper fetch --depth=1 origin d1be6fde11ac6e0407606b4e42fe72d34add8037 \
    && git -C /src/whisper checkout --detach FETCH_HEAD \
    && cmake -S /src/whisper -B /src/whisper/build \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
        -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=ON \
    && cmake --build /src/whisper/build --config Release --target whisper-server -j2

FROM python:3.14.9-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 inference \
    && useradd --uid 1000 --gid inference --no-create-home inference
COPY --from=build /src/llama/build/bin/llama-server /opt/pandect/llama-server
COPY --from=build /src/whisper/build/bin/whisper-server /opt/pandect/whisper-server
COPY --from=build /src/llama/LICENSE /opt/pandect/licenses/llama.cpp
COPY --from=build /src/whisper/LICENSE /opt/pandect/licenses/whisper.cpp
COPY revisions.json server.py /opt/pandect/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /tmp
USER inference
EXPOSE 8080
CMD ["python", "-I", "/opt/pandect/server.py"]
