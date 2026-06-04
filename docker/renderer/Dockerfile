FROM ghcr.io/puppeteer/puppeteer:23.6.0

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json render.mjs ./
RUN npm install --omit=dev

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

ENTRYPOINT ["node", "/app/render.mjs"]
