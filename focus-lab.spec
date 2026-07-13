# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Focus Lab Feed Collector macOS app."""

import os
import sys

block_cipher = None

a = Analysis(
    ['app/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('app/static', 'app/static'),
        ('config.json', '.'),
        ('src', 'src'),
        ('skills', 'skills'),
        ('viewer', 'viewer'),
        ('assets/focuslab-logo.svg', 'assets'),
        ('assets/focuslab.icns', 'assets'),
    ],
    hiddenimports=[
        # FastAPI + Starlette
        'fastapi',
        'fastapi.middleware',
        'fastapi.middleware.cors',
        'fastapi.staticfiles',
        'fastapi.responses',
        'starlette',
        'starlette.middleware',
        'starlette.middleware.cors',
        'starlette.staticfiles',
        'starlette.responses',
        'starlette.routing',
        # Uvicorn (all submodules — dynamically imported)
        'uvicorn',
        'uvicorn.config',
        'uvicorn.logging',
        'uvicorn.server',
        'uvicorn.main',
        'uvicorn.lifespan.off',
        'uvicorn.lifespan.on',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols.utils',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.http.httptools_impl',
        'uvicorn.protocols.http.flow_control',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.wsproto_impl',
        'uvicorn.protocols.websockets.websockets_impl',
        # Pydantic
        'pydantic',
        'pydantic.main',
        'pydantic_core',
        # PyWebView
        'webview',
        # Playwright (we don't bundle browsers, just the Python package)
        'playwright',
        'playwright.async_api',
        'playwright._impl',
        # aiohttp (for media downloads)
        'aiohttp',
        # App modules
        'app',
        'app.server',
        'app.paths',
        'app.setup',
        'app.api',
        'app.api.auth',
        'app.api.collection',
        'app.api.config',
        'app.api.data',
        'app.api.export',
        'app.api.setup',
        'app.tasks',
        'app.tasks.manager',
        'app.tasks.auth_task',
        # Source modules (collectors)
        'src.collect',
        'src.models',
        'src.storage',
        'src.media_downloader',
        'src.browser',
        'src.platforms.base',
        'src.platforms.x.auth',
        'src.platforms.x.collector',
        'src.platforms.x.interceptor',
        'src.platforms.x.replies',
        'src.platforms.x.scroller',
        'src.platforms.threads.auth',
        'src.platforms.threads.collector',
        'src.platforms.threads.interceptor',
        'src.platforms.threads.replies',
        'src.platforms.instagram.auth',
        'src.platforms.instagram.collector',
        'src.platforms.instagram.interceptor',
        'src.platforms.instagram.replies',
        'src.platforms.youtube.auth',
        'src.platforms.youtube.collector',
        'src.platforms.youtube.interceptor',
        'src.platforms.linkedin.auth',
        'src.platforms.linkedin.collector',
        'src.platforms.linkedin.interceptor',
        # Standard library / common
        'multiprocessing',
        'email.mime.multipart',
        'email.mime.text',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'PIL',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Focus Lab Feed',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # Windowed app, no terminal
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Focus Lab Feed',
)

app = BUNDLE(
    coll,
    name='Focus Lab Feed.app',
    icon='assets/focuslab.icns',
    bundle_identifier='com.focuslab.feed',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'CFBundleName': 'Focus Lab Feed',
        'CFBundleDisplayName': 'Focus Lab Feed',
        'CFBundleShortVersionString': '0.2.0',
        'CFBundleVersion': '0.2.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.15',
    },
)
