FROM python:3.12-slim

LABEL org.opencontainers.image.title="μplot" \
      org.opencontainers.image.description="Weboberfläche zum Stiftplotten mit dem Prusa MK3S+ (vpype + vpype-gcode)" \
      org.opencontainers.image.source="https://github.com/dmyrenne/uplot" \
      org.opencontainers.image.authors="Daniel Myrenne"

# Ohne UPLOT_PROFILES_DIR speichert der Server nichts (eigene Profile im Browser); für eine eigene Instanz mit
# Profilen auf dem Server UPLOT_PROFILES_DIR=/data setzen und ein Volume einhängen (compose.yaml)
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

# Beispiel-SVGs aus github.com/brianlow/plotter (werden beim Bauen geladen, nicht im Repo mitgeliefert)
ARG EXAMPLES=https://raw.githubusercontent.com/brianlow/plotter/main
ADD $EXAMPLES/waves/waves.svg $EXAMPLES/flower/flower.svg $EXAMPLES/city/city.svg \
    $EXAMPLES/calibration/calibration.svg $EXAMPLES/calibration/pen-width.svg examples/
RUN chmod 755 examples && chmod 644 examples/*

COPY app.py raster.py ./
COPY static static

RUN useradd --uid 1000 --create-home uplot && mkdir /data && chown uplot /data
USER uplot
EXPOSE 5055

# vpype braucht bei großen SVGs etwas länger, daher großzügiges Timeout
CMD ["gunicorn", "--bind", "0.0.0.0:5055", "--workers", "2", "--timeout", "300", "app:app"]
