# Enjoyable Lite Web: the Cloud Run service. Built from the folder that cloud/stage.py prepares.
# The two local voices are downloaded here (public releases), so a deploy uploads only the code.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
ARG ST=https://huggingface.co/supertone-oss-archive/supertonic-3/resolve/main
ARG KO=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1
RUN mkdir -p models/supertonic3/onnx models/supertonic3/voice_styles models/kokoro \
 && for f in duration_predictor.onnx text_encoder.onnx vector_estimator.onnx vocoder.onnx tts.json unicode_indexer.json; do \
      curl -fsSL -o models/supertonic3/onnx/$f $ST/onnx/$f; done \
 && for f in M1.json F1.json; do curl -fsSL -o models/supertonic3/voice_styles/$f $ST/voice_styles/$f; done \
 && curl -fsSL -o models/supertonic3/config.json $ST/config.json \
 && curl -fsSL -o models/kokoro/kokoro-v1.1-zh.onnx $KO/kokoro-v1.1-zh.onnx \
 && curl -fsSL -o models/kokoro/voices-v1.1-zh.bin $KO/voices-v1.1-zh.bin \
 && curl -fsSL -o models/kokoro/config.json https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/resolve/main/config.json
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080 PYTHONUNBUFFERED=1 STORE=firestore ENJOYABLE_TIMING=gemini GEMINI_BACKEND=vertex
CMD exec uvicorn app:app --app-dir cloud --host 0.0.0.0 --port ${PORT} --workers 1
