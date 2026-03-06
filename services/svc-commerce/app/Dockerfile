# services/svc-commerce/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app
ENV PYTHONPATH=/app

# --------------------------------------------------------------------
# System deps
# --------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    jq \
    \
    # EGL/GL + X libs for Blender headless/Eevee-next
    libegl1 \
    libgl1 \
    libglx0 \
    libgles2 \
    libgbm1 \
    libdrm2 \
    mesa-utils \
    libx11-6 \
    libxi6 \
    libxxf86vm1 \
    libxrender1 \
    libxfixes3 \
    libxext6 \
    libsm6 \
    libice6 \
    libglib2.0-0 \
    libstdc++6 \
    libgcc-s1 \
    \
    # mediapipe/opencv runtime deps (kept conservative; you can slim later)
    libopencv-core-dev \
    libopencv-imgproc-dev \
    libopencv-imgcodecs-dev \
 && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------
# Install Blender (pinned) — runtime availability.
# NOTE: This is still fine; the issue was running Blender to generate assets during build.
# --------------------------------------------------------------------
ARG BLENDER_VERSION=4.3.2
RUN set -eux; \
    major_minor="$(echo "${BLENDER_VERSION}" | awk -F. '{print $1"."$2}')" ; \
    url="https://download.blender.org/release/Blender${major_minor}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" ; \
    echo "Downloading Blender from: ${url}" ; \
    curl -L "${url}" -o /tmp/blender.tar.xz ; \
    mkdir -p /opt/blender ; \
    tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 ; \
    ln -sf /opt/blender/blender /usr/local/bin/blender ; \
    blender --version | head -n 1

# Helpful defaults for headless CPU rendering (safe for EEVEE_NEXT).
# If you later do GPU/EGL tuning, keep these overrideable in compose env.
ENV LIBGL_ALWAYS_SOFTWARE=1 \
    MESA_LOADER_DRIVER_OVERRIDE=llvmpipe

# --------------------------------------------------------------------
# Python deps (layer cache friendly)
# --------------------------------------------------------------------
COPY app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# Copy service code
COPY app /app/app

# Optional: log blender version at build time
RUN blender --version | head -n 1 || true

# --------------------------------------------------------------------
# IMPORTANT CHANGE:
# Do NOT generate template assets during image build by default.
# (This was causing BuildKit "context deadline exceeded".)
# If you still want build-time generation for dev, set build-arg:
#   --build-arg DF_BUILD_GENERATE_NIVI_TEMPLATE=1
# --------------------------------------------------------------------
ARG DF_BUILD_GENERATE_NIVI_TEMPLATE=0
RUN mkdir -p /app/app/services/drape/templates/nivi/v1 && \
    if [ "$DF_BUILD_GENERATE_NIVI_TEMPLATE" = "1" ]; then \
      echo "Generating nivi template at build time..." && \
      blender --background --factory-startup \
        --python /app/app/services/drape/blender_scripts/make_nivi_template.py -- \
        --out /app/app/services/drape/templates/nivi/v1/nivi.blend \
        --preview /app/app/services/drape/templates/nivi/v1/preview.png ; \
    else \
      echo "Skipping build-time nivi template generation (production default)."; \
    fi

# Keep this as a *fallback* path only.
# In production you should override with:
#   COMMERCE_SAREE_TEMPLATE_PATH=az://commerce-training/drape_templates/saree/nivi/v1/nivi.blend
ENV DF_SAREE_TEMPLATE_NIVI_BLEND=/app/app/services/drape/templates/nivi/v1/nivi.blend

EXPOSE 8008
CMD ["bash", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8008}"]