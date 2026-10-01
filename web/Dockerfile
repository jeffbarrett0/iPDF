FROM node:22-slim
WORKDIR /srv/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ .
# /api/* is proxied to the api container; these values are baked in at build time.
ARG API_URL=http://api:8000
ARG MAX_UPLOAD_MB=200
ENV API_URL=${API_URL} MAX_UPLOAD_MB=${MAX_UPLOAD_MB} NEXT_TELEMETRY_DISABLED=1
RUN npx next build && chown -R node:node /srv/web/.next
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["npx", "next", "start", "-H", "0.0.0.0", "-p", "3000"]
