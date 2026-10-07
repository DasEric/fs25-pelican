# syntax=docker/dockerfile:1

# The prebuilt Wine runtime needs FFmpeg 4's ABI. Keep it private to Wine.
FROM        ubuntu:22.04 AS wine-compat
RUN         apt-get update \
            && apt-get install -y --no-install-recommends \
                libavcodec58 libavformat58 libavutil56 \
                libcapi20-3 libmfx1 libpcap0.8 libssh-gcrypt-4 \
            && mkdir -p /opt/fs25/wine-compat \
            && cp -a /usr/lib/x86_64-linux-gnu/libavcodec.so.58* \
                /usr/lib/x86_64-linux-gnu/libavformat.so.58* \
                /usr/lib/x86_64-linux-gnu/libavutil.so.56* \
                /usr/lib/x86_64-linux-gnu/libswresample.so.3* \
                /usr/lib/x86_64-linux-gnu/libcodec2.so.1.0* \
                /usr/lib/x86_64-linux-gnu/libdav1d.so.5* \
                /usr/lib/x86_64-linux-gnu/libvpx.so.7* \
                /usr/lib/x86_64-linux-gnu/libx264.so.163* \
                /usr/lib/x86_64-linux-gnu/libx265.so.199* \
                /usr/lib/x86_64-linux-gnu/libsrt-gnutls.so.1.4* \
                /usr/lib/x86_64-linux-gnu/libssh-gcrypt.so.4* \
                /usr/lib/x86_64-linux-gnu/libmfx*.so.* \
                /usr/lib/x86_64-linux-gnu/libcapi20.so.3* \
                /usr/lib/x86_64-linux-gnu/libpcap.so.* /opt/fs25/wine-compat/ \
            && dpkg-query -W -f='${binary:Package} ${Version} ${source:Package} ${source:Version}\n' \
                > /opt/fs25/wine-compat/PACKAGES.txt \
            && apt-get clean \
            && rm -rf /var/lib/apt/lists/*

FROM        ghcr.io/pelican-eggs/yolks:wine_11

LABEL       org.opencontainers.image.authors="Eric <DasEric@users.noreply.github.com>" \
            org.opencontainers.image.source="https://github.com/DasEric/fs25-pelican" \
            org.opencontainers.image.licenses="MIT AND LGPL-2.1-or-later"

USER        root

# Independent Wine-Proton runtime: no Steam launcher and no source compilation.
ADD         --checksum=sha256:f166f7daa1a37b3c8e0ade213a6e7915e582091804071a58a4a32c6c672e1595 \
            https://github.com/Kron4ek/Wine-Builds/releases/download/proton-11.0-2/wine-proton-11.0-2-amd64-wow64.tar.xz \
            /tmp/fs25-prebuilt-wine.tar.xz
ADD         --checksum=sha256:a07652adb965e657837e8013ecb525303fc733555c78a5295a63d48b0ca9a120 \
            https://codeload.github.com/ValveSoftware/wine/tar.gz/dc26e61847081a1b5cb0733dc30feba6ee575482 \
            /opt/fs25/wine-source/valve-wine.tar.gz
ADD         --checksum=sha256:c450f920ef7380f12dca742d8c96bcc672aec36b24b2f10d4d68b9495cd1e20f \
            https://codeload.github.com/Kron4ek/Wine-Builds/tar.gz/fe137c65b411ff3079d1b26573dcb70bf16814a5 \
            /opt/fs25/wine-source/prebuilt-build-recipe.tar.gz

# Let Debian select its FFmpeg library ABI instead of pinning release-specific package names.
RUN         apt update \
            && if apt-cache show libgphoto2-6t64 >/dev/null 2>&1; then \
                gphoto_package=libgphoto2-6t64; else gphoto_package=libgphoto2-6; fi \
            && apt install -y --no-install-recommends \
                dbus-x11 \
                ffmpeg \
                libgstreamer1.0-0 \
                libgstreamer-plugins-base1.0-0 \
                libgstreamer-gl1.0-0 \
                "$gphoto_package" \
                libpcsclite1 \
                libsane1 \
                libwayland-client0 \
                libwayland-egl1 \
                libxkbcommon0 \
                libxkbregistry0 \
                ocl-icd-libopencl1 \
                libgcrypt20 \
                libgssapi-krb5-2 \
                libnuma1 \
                libsoxr0 \
                patchelf \
                xz-utils \
                firefox-esr \
                fonts-dejavu-core \
                libarchive-tools \
                netcat-openbsd \
                novnc \
                p7zip-full \
                procps \
                socat \
                sysstat \
                tigervnc-standalone-server \
                tigervnc-tools \
                websockify \
                x11-utils \
                xdg-user-dirs \
                xfonts-base \
                xterm \
                exo-utils \
                thunar \
                xfce4-appfinder \
                xfce4-panel \
                xfce4-session \
                xfce4-settings \
                xfce4-terminal \
                xfdesktop4 \
                xfwm4 \
            && update-alternatives --set x-terminal-emulator /usr/bin/xterm \
            && apt clean \
            && rm -rf /var/lib/apt/lists/*

RUN         mkdir -p /opt/fs25/wine /tmp/fs25-source /tmp/fs25-recipe \
            && tar -xJf /tmp/fs25-prebuilt-wine.tar.xz -C /opt/fs25/wine --strip-components=1 \
            && tar -xzf /opt/fs25/wine-source/valve-wine.tar.gz -C /tmp/fs25-source --strip-components=1 \
            && tar -xzf /opt/fs25/wine-source/prebuilt-build-recipe.tar.gz -C /tmp/fs25-recipe --strip-components=1 \
            && cp /tmp/fs25-source/COPYING.LIB /tmp/fs25-source/LICENSE /tmp/fs25-source/AUTHORS /opt/fs25/wine-source/ \
            && cp /tmp/fs25-recipe/LICENSE /opt/fs25/wine-source/BUILD_RECIPE_LICENSE \
            && cp /tmp/fs25-recipe/fix-proton-compilation-and-version-output.patch /opt/fs25/wine-source/ \
            && printf 'Prebuilt: wine-proton-11.0-2-amd64-wow64\nWine source: dc26e61847081a1b5cb0733dc30feba6ee575482\nRecipe: fe137c65b411ff3079d1b26573dcb70bf16814a5\nBinary SHA256: f166f7daa1a37b3c8e0ade213a6e7915e582091804071a58a4a32c6c672e1595\n' \
                > /opt/fs25/wine-source/RUNTIME.txt \
            && chmod 0444 /opt/fs25/wine-source/* \
            && rm -rf /tmp/fs25-source /tmp/fs25-recipe \
            && rm /tmp/fs25-prebuilt-wine.tar.xz

COPY        --from=wine-compat /opt/fs25/wine-compat /opt/fs25/wine-compat
COPY        --from=wine-compat /usr/share/doc /opt/fs25/wine-compat/licenses
# Scope compatibility lookup to Wine ELF files, not Python, XFCE or Firefox.
RUN         find /opt/fs25/wine-compat -type f -name '*.so.*' \
                -print0 | xargs -0 -r -n 1 patchelf --add-rpath '$ORIGIN' \
            && find /opt/fs25/wine/lib/wine/x86_64-unix -type f -name '*.so' \
                -print0 | xargs -0 -r -n 1 patchelf --add-rpath '$ORIGIN:/opt/fs25/wine-compat' \
            && patchelf --replace-needed libpcap.so.1 libpcap.so.0.8 \
                /opt/fs25/wine/lib/wine/x86_64-unix/wpcap.so
RUN         if { ldd /opt/fs25/wine/bin/wine /opt/fs25/wine/bin/wineserver \
                && find /opt/fs25/wine/lib/wine/x86_64-unix -type f -name '*.so' \
                    -print0 | xargs -0 -r -n 1 env LD_LIBRARY_PATH=/opt/fs25/wine/lib/wine/x86_64-unix ldd; \
            } > /tmp/fs25-wine-libs.txt 2>&1; then \
                cat /tmp/fs25-wine-libs.txt \
                && ! grep -q 'not found' /tmp/fs25-wine-libs.txt \
                && rm /tmp/fs25-wine-libs.txt; \
            else \
                cat /tmp/fs25-wine-libs.txt; exit 1; \
            fi
COPY        LICENSE /opt/fs25/LICENSE
RUN         chmod 0444 /opt/fs25/LICENSE

COPY        entrypoint.py fs25ctl.py /opt/fs25/
RUN         chmod 0555 /opt/fs25/entrypoint.py /opt/fs25/fs25ctl.py

ENV         HOME=/home/container \
            USER=container \
            LOGNAME=container \
            SHELL=/bin/bash \
            WINEPREFIX=/home/container/.fs25server \
            WINEARCH=win64 \
            WINEDEBUG=-all \
            DISPLAY=:0 \
            XDG_RUNTIME_DIR=/tmp/xdg-runtime-fs25

USER        container
WORKDIR     /home/container

# Verify both Windows architectures against the final runtime, without touching server data.
RUN         WINEPREFIX=/tmp/fs25-wine-smoke WINEDEBUG=-all \
            WINEFSYNC=0 PROTON_NO_NTSYNC=1 WINESERVER=/opt/fs25/wine/bin/wineserver \
            PATH=/opt/fs25/wine/bin:$PATH \
            timeout 240 xvfb-run -a /bin/sh -c \
                'wineboot --init && wine cmd /d /s /c ver && wine "C:\windows\syswow64\cmd.exe" /d /s /c ver && wineserver -w' \
            && rm -rf /tmp/fs25-wine-smoke

STOPSIGNAL  SIGINT

CMD         ["/opt/fs25/entrypoint.py"]
