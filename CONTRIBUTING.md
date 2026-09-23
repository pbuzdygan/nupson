# Contributing

Contributions are welcome through focused pull requests.

## Development checks

Run these checks before opening a pull request:

```bash
ruff check nupson tests
ruff format --check nupson tests
python3 -m unittest discover -s tests -v
NUPSON_USB_GID=995 docker compose config -q
```

Functional changes should include tests and an entry under `Unreleased` in
`changelog.md`. Update `IMPLEMENTATION_PLAN.md` when a milestone item or product
decision changes.

Never commit `.env`, `data/`, databases, generated NUT configuration, backup
archives, logs containing credentials, or real webhook URLs. Use clearly fake
addresses and credentials in tests and documentation.

## Pull requests

- keep each pull request limited to one coherent change;
- explain user-visible behavior and operational risk;
- document migrations, new environment variables, and security implications;
- preserve compatibility with both `linux/amd64` and `linux/arm64`;
- do not publish container images from a pull request.
