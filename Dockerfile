# The toolchain, pinned, so "same bytes in, same text out" covers the converters too.
#
# `requirements.txt` is pinned exactly because a floating PyMuPDF is a floating text layer.
# LibreOffice and Tesseract need the same treatment: installed by `apt-get` in each job they
# would be whatever Ubuntu ships that day, and LibreOffice's version decides what the text of
# a Word 97 attachment looks like. Baking them into an image fixes them, and a layer pull is
# also faster and steadier than an apt install per job.
#
# Base is pinned by digest rather than by tag: `ubuntu:24.04` is a moving target, and a
# moving base defeats the whole purpose of this file.
FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# curl is the transport (see eis_fetch.Curl), git is for actions/checkout, ca-certificates
# so TLS works at all. LibreOffice reads legacy Office formats; p7zip opens the .7z a buyer
# ships a building project in; tesseract with `lav` reads the scans no decoder can.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      ca-certificates curl git \
      python3 python3-pip python3-venv \
      p7zip-full \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      tesseract-ocr tesseract-ocr-lav tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

# Installed into the system interpreter on purpose: this image IS the environment, so a
# virtualenv would add a layer of indirection with nothing on the other side of it.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# Fail the build rather than ship an image that cannot do the job. Each line here is
# something a run depends on.
RUN set -e; \
    curl --version > /dev/null; \
    git --version > /dev/null; \
    soffice --version > /dev/null; \
    7z i > /dev/null; \
    tesseract --list-langs 2>&1 | grep -q '^lav$'; \
    python3 -c "import fitz, docx, openpyxl, pptx, py7zr; print('converters import')"

WORKDIR /work
