FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app
ENV PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    jq \
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
    libopencv-core-dev \
    libopencv-imgproc-dev \
    libopencv-imgcodecs-dev \
 && rm -rf /var/lib/apt/lists/*

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

ENV LIBGL_ALWAYS_SOFTWARE=1 \
    MESA_LOADER_DRIVER_OVERRIDE=llvmpipe


COPY services/svc-commerce/app/app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install --no-cache-dir -r /app/requirements.txt

# copy full repo so shared imports like desifaces_shared are available
COPY . /repo

# keep runtime import layout expected by uvicorn app.main:app
COPY services/svc-commerce/app/app /app/app
COPY services/shared/python/desifaces_shared /app/desifaces_shared

RUN blender --version | head -n 1 || true

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

ENV DF_SAREE_TEMPLATE_NIVI_BLEND=/app/app/services/drape/templates/nivi/v1/nivi.blend

EXPOSE 8008
CMD ["bash", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8008}"]