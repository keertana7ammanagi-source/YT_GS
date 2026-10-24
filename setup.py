"""Setuptools metadata for installation and environment diagnostics."""
from setuptools import setup

setup(
    name="astrasensenexus-ground-station",
    version="1.0.0",
    description="AstrasenseNexus CanSat telemetry ground station",
    py_modules=["main", "station_control", "database_handling", "setup_map", "deploy_graph", "offline_navigation", "anomaly_detection", "telemetry_graphs"],
    install_requires=["customtkinter>=5.2", "pyserial>=3.5", "matplotlib>=3.8", "tkintermapview>=1.29", "Pillow>=10.0"],
    python_requires=">=3.10",
)
