FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY apps/web/package*.json ./apps/web/
RUN npm ci --prefix apps/web
COPY apps/web ./apps/web
RUN npm --prefix apps/web run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SHIFTPATCH_STORAGE=firestore SHIFTPATCH_ENV=production \
    SHIFTPATCH_WEB_DIST=/srv/web PORT=8080
WORKDIR /srv
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY backend ./backend
RUN pip install --no-cache-dir --no-deps ./backend && useradd --uid 10001 --create-home shiftpatch
COPY --from=web /build/apps/web/dist /srv/web
USER shiftpatch
EXPOSE 8080
CMD ["python", "-m", "app.serve"]
