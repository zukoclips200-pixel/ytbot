FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
WORKDIR /app
ADD https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx models/yunet.onnx
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# YouTube changes things often: get the newest yt-dlp every time the bot starts
CMD ["sh", "-c", "pip install -q --no-cache-dir -U --pre 'yt-dlp[default]' || true; python bot.py"]
