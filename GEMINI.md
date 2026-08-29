# GoldEdge Architecture Rules (Strict Hexagonal Architecture)

When working on this codebase, you **MUST** strictly adhere to the Ports and Adapters (Hexagonal) Architecture.

## 1. Directory Structure Rules
- `src/goldedge/domain/`: Core business logic. **NEVER** import external infrastructure libraries (like `pandas`, `sqlalchemy`, or `requests`) here. Keep it pure Python.
- `src/goldedge/domain/ports/`: Interfaces (`typing.Protocol`) defining what the system needs (e.g., `IAggregationRepository`). No concrete logic goes here.
- `src/goldedge/adapters/`: Concrete implementations of ports. All "dirty" infrastructure code (Pandas, SQL, API wrappers) MUST be isolated here.
- `src/goldedge/application/services/`: Use-case orchestrators. These classes coordinate the flow of data but **MUST NOT** execute SQL or Pandas directly. They must rely exclusively on Dependency Injection via Ports.

## 2. Dependency Injection
- Never manually instantiate an adapter inside a service.
- All wiring and instantiation of classes MUST be done inside `src/goldedge/config/container.py` using `dependency-injector`.
- To use a service, you must ask the container for it (e.g., `container.market_aggregator_service()`).

## 3. Data Science & Aggregations
- For heavy data science workloads (like 1m bar aggregations or ML features), it is acceptable to bypass pure Python domain models (which are slow) and use Pandas vectorization.
- **CRITICAL**: The Pandas code must be strictly isolated inside an adapter (e.g., `pandas_aggregator_repo.py`). The application service should never know Pandas is being used.

By following these rules, the GoldEdge trading system remains infinitely scalable, testable, and infrastructure-agnostic.
