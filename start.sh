#!/bin/bash
# Startet die Plotter-Weboberfläche auf http://127.0.0.1:5055 und öffnet den Browser.
cd "$(dirname "$0")"
exec .venv/bin/python app.py "$@"
