"""Setuptools metadata for installation and environment diagnostics."""
from setuptools import setup

setup(
    name="cansat-ground-station",
    version="1.0.0",
    description="Modular CanSat telemetry ground station",
    py_modules=["main", "station_control", "database_handling", "setup_map", "deploy_graph", "anomaly_detection", "telemetry_graphs", "environmental_fingerprinting", "fingerprint_charts", "ai_report"],
    install_requires=["customtkinter>=5.2", "pyserial>=3.5", "matplotlib>=3.8", "tkintermapview>=1.29", "numpy>=1.25", "reportlab>=4.0"],
    python_requires=">=3.10",
)
