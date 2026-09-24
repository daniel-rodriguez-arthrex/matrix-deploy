"""Localhost web UI for Matrix Deploy.

This package is purely additive: it reuses the existing Qt-free core
(``AppConfig``, ``Deployer``, ``ArtifactoryClient``, ``JenkinsClient``) and
never imports anything from ``gui.py`` or ``workers.py`` (which pull in
PyQt5). See ``run_server.py`` for the entry point.
"""
