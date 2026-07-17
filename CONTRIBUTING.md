# Contributing to agent-shelf

Thank you for your interest in contributing! We welcome all contributions — bug reports, feature requests, and pull requests.

## Guidelines

### Issues and Pull Requests
- Issue や PR は日本語・英語どちらでも歓迎です。

### Before Submitting
- Direct pushes to `main` are not allowed — changes must go through a PR with a green CI.
- Changes must pass all tests:
  ```bash
  uv run pytest
  ```
- Code must pass linting and formatting checks:
  ```bash
  uv run ruff check .
  ```

### Commit Message Style
- Japanese preferred; concise 1-2 sentence format
- Focus on WHY, not just WHAT
- Keep commits atomic — one logical change per commit

### License
By contributing, you agree that your contributions will be licensed under the same MIT License as the project.

Thank you for your contribution!
