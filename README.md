# GoldEdge

An institutional-grade, high-frequency algorithmic trading system for XAU/USD (Spot Gold) CFD.
Engineered using Hexagonal Architecture, cTrader Open API Protobufs over TCP, Redis Streams, and a 2D Volume-Adaptive Kalman Filter with LightGBM Meta-Labeling.

## Quickstart

1. Spin up the databases:
   ```bash
   docker-compose up -d redis timescaledb
   ```
2. Install the environment:
   ```bash
   pip install -e .
   ```
3. Run the Tick Collector:
   ```bash
   python scripts/tick_collector_daemon.py
   ```
