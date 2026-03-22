# ESP Host Bridge Add-on

This directory is a self-contained Home Assistant add-on build context for ESP Host Bridge.

In the standalone repository copy, this directory is the app Home Assistant installs.

It now exposes a native Home Assistant Ingress entry and a populated add-on Configuration tab.

If you are maintaining this repository from the monorepo, use `sync_from_repo.sh` before publishing so the app build context contains the current Host Bridge runtime files.
