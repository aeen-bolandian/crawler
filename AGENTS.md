# 🤖 Project Agent Rules

## Project Context
- **Name**: [اسم پروژه رو اینجا بنویس]
- **Description**: [توضیح کوتاه درباره اینکه پروژه چیکار میکنه]
- **Tech Stack**: [مثلاً: Python 3.10+, FastAPI, PostgreSQL]

## Core Principles
- **Code Quality**: Follow PEP 8 (for Python) or the official style guide for the chosen language.
- **Type Safety**: Always add type hints to all function signatures.
- **Documentation**: Every module, class, and public function must have a docstring explaining its purpose.
- **Error Handling**: Never use bare `except:` clauses. Handle specific exceptions and log errors properly.
- **Logging**: Use the `loguru` library for logging (or `logging` if loguru is not available). Never use `print()` for debugging.

## Architecture Constraints
- **Separation of Concerns**: Keep business logic separate from API endpoints and database models.
- **Dependency Injection**: Prefer passing dependencies as arguments rather than importing global instances.
- **Configuration**: All secrets and environment-specific settings must be loaded from environment variables, never hardcoded.

## File Naming Conventions
- Python files: `snake_case.py`
- Test files: `test_*.py` inside a `tests/` directory.
- Config files: `config.yaml` or `.env` (never commit `.env`).

## What NOT to do
- Do NOT write code without first explaining your plan.
- Do NOT modify files outside the `src/` or `app/` directory without explicit permission.
- Do NOT install new dependencies without asking the user first.
